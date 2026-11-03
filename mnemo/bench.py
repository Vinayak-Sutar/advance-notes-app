"""Which local models should Notes use? Measured on the jobs it actually does.

Blog rankings disagree with each other and test other things, so each
candidate runs Notes' own work, with known right answers:

    note AI   answer / add / rewrite, and never lose a line   tests/bench/noteai.json
    chat      answers from notes, honest "don't know"          tests/bench/chat.json
    tools     picks the right tool with sensible arguments     tests/bench/tools.json
    vision    describes a picture and copies its text          tests/bench/vision.json
    speed     load time, first token, tokens/s, video memory

and every embedder runs filing accuracy (calibrate.measure) and chat
retrieval (does search put the right note in the top 5?).

Run:   python -m mnemo.bench                      everything installed
       python -m mnemo.bench --brains qwen3.5:4b  just these
Writes docs/bench-results.md and docs/bench-results.json.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import runtime as R
from .paths import OLLAMA_HOST, REPO_ROOT

DATA = REPO_ROOT / "tests" / "bench"
OUT = REPO_ROOT / "docs"

BRAINS = ["gemma3:4b", "qwen3.5:4b", "gemma4:e4b", "qwen3.5:2b", "deepseek-r1:8b", "qwen3.5:9b"]
EMBEDDERS = ["nomic-embed-text", "qwen3-embedding:0.6b", "embeddinggemma"]

# Models write both ' and ’ (curly) apostrophes; both must count.
HONEST = re.compile(
    r"don['’]?t (?:know|have|see|find|mention|say|include|contain|specify)|"
    r"do not (?:know|have|see|mention|contain)|"
    r"not sure|couldn['’]?t (?:find|see)|can['’]?t (?:find|see)|cannot (?:find|see)|"
    r"no (?:notes?|information|record|mention|details?)|"
    r"(?:isn['’]?t|is not|aren['’]?t|not) (?:in|mentioned|saved|listed|recorded)|"
    r"doesn['’]?t (?:say|mention|include|have|contain|specify)|nothing (?:about|in|on)|"
    r"haven['’]?t (?:saved|noted)|unable to find|not (?:available|specified|included)", re.I)
SPEED_PROMPT = "In three short sentences, explain why writing things down helps you remember."


# ------------------------------------------------------------------ scoring ----
def _low(s) -> str:
    return " ".join(str(s or "").split()).lower()


def score_noteai(case: dict, out: dict) -> tuple[bool, str]:
    """Right action, and the content checks that action implies."""
    from .noteai import _REMOVAL, _dropped
    action = out.get("action")
    if action not in case["expect"]:
        return False, f"did {action}, expected {'/'.join(case['expect'])}"
    reply, text = _low(out.get("reply")), _low(out.get("text"))
    if case.get("reply_has") and not all(w in reply for w in case["reply_has"]):
        return False, f"reply lacks {case['reply_has']}"
    if case.get("reply_any") and not any(w in reply for w in case["reply_any"]):
        return False, f"reply lacks any of {case['reply_any']}"
    if action != "answer":
        if case.get("text_has") and not all(w.lower() in text for w in case["text_has"]):
            return False, f"text lacks {case['text_has']}"
        if case.get("text_any") and not any(w in text for w in case["text_any"]):
            return False, f"text lacks any of {case['text_any']}"
        if case.get("text_not") and any(w in text for w in case["text_not"]):
            return False, f"text still has {case['text_not']}"
        if case.get("text_order"):
            pos = [text.find(w) for w in case["text_order"]]
            if -1 in pos or pos != sorted(pos):
                return False, "wrong order"
        if (action == "replace" and case.get("keep_lines")
                and not _REMOVAL.search(case["request"])):
            lost = _dropped(case["body"], out.get("text") or "")
            # Changing every line's marker (checklist, bullets) is the point,
            # so _dropped compares without markers; anything else lost fails.
            if lost:
                return False, f"lost lines {lost[:2]}"
    return True, ""


def score_chat(case: dict, answer: str, from_notes: bool) -> tuple[bool, str]:
    text = _low(answer)
    if case.get("no_answer"):
        if HONEST.search(text):
            return True, ""
        return False, f"made something up: {answer.strip()[:90]!r}"
    if case.get("all") and not all(w in text for w in case["all"]):
        return False, f"missing {[w for w in case['all'] if w not in text]}"
    if case.get("any") and not any(w in text for w in case["any"]):
        return False, f"missing any of {case['any']}"
    return True, ""


def score_tool(case: dict, calls: list[dict]) -> tuple[bool, str]:
    wanted = case["tool"]
    if not calls:
        return (None in wanted), "no tool used" if None not in wanted else ""
    name = (calls[0].get("function") or {}).get("name")
    if name not in wanted:
        return False, f"used {name}, expected {wanted}"
    args = json.dumps((calls[0].get("function") or {}).get("arguments") or {}).lower()
    if case.get("args_any") and not any(w in args for w in case["args_any"]):
        return False, f"odd arguments {args[:80]}"
    return True, ""


def similarity(got: str, truth: str) -> float:
    return difflib.SequenceMatcher(None, _low(got), _low(truth)).ratio()


# ------------------------------------------------------------------ helpers ----
def unload_all() -> None:
    """Free video memory so each candidate is measured on its own."""
    for m in R.loaded():
        for path in ("/api/generate", "/api/embed"):
            try:
                body = {"model": m["name"], "keep_alive": 0}
                if path == "/api/embed":
                    body["input"] = ""
                R.api(path, body, timeout=60)
                break
            except Exception:
                continue
    for _ in range(20):
        if not R.loaded():
            return
        time.sleep(0.5)


def _stream_chat(model: str, prompt: str) -> dict:
    payload = {"model": model, "stream": True, "keep_alive": -1,
               "messages": [{"role": "user", "content": prompt}],
               "options": {"temperature": 0.2, "num_predict": 160},
               **R.think_option(model, False)}
    req = urllib.request.Request(f"{OLLAMA_HOST}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    start, first, final = time.time(), None, {}
    with urllib.request.urlopen(req, timeout=600) as resp:
        for line in resp:
            if not line.strip():
                continue
            data = json.loads(line)
            if first is None and (data.get("message") or {}).get("content"):
                first = time.time() - start
            if data.get("done"):
                final = data
    return {"first": first, "final": final, "total": time.time() - start}


def gpu_used_gb() -> float | None:
    """What the GPU itself says is in use. Ollama's own report can be far off
    (it says 0.26 GB for gemma4:e4b, which really takes about 4.4 GB)."""
    import subprocess
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return round(float(out.stdout.strip().splitlines()[0]) / 1024, 2)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return None


def bench_speed(model: str) -> dict:
    unload_all()
    before = gpu_used_gb()
    cold = _stream_chat(model, "Say OK.")
    warm = _stream_chat(model, SPEED_PROMPT)
    after = gpu_used_gb()
    f = warm["final"]
    tok_s = f.get("eval_count", 0) / max(f.get("eval_duration", 1) / 1e9, 1e-6)
    mem = next((m for m in R.loaded() if m["name"].split(":")[0] == model.split(":")[0]), {})
    size, vram = mem.get("size", 0), mem.get("size_vram", 0)
    return {"load_s": round((cold["final"].get("load_duration") or 0) / 1e9, 1),
            "first_token_s": round(warm["first"] or warm["total"], 2),
            "tokens_per_s": round(tok_s, 1),
            "size_gb": round(size / 1e9, 2),
            "vram_gb": round(after - before, 2) if before is not None and after is not None
            else round(vram / 1e9, 2),
            "on_gpu_pct": round(100 * vram / size) if size else 0}


def bench_noteai(model: str, cases: list[dict]) -> list[dict]:
    from . import noteai
    from .websearch import WebConfig
    client = noteai.OllamaJson(model)
    out = []
    for case in cases:
        t = time.time()
        try:
            res = noteai.respond(case["title"], case["body"], case["request"],
                                 model=client, web=WebConfig(mode="off"))
            ok, why = score_noteai(case, res)
        except Exception as exc:
            res, ok, why = {}, False, f"error {type(exc).__name__}: {exc}"[:120]
        out.append({"request": case["request"], "ok": ok, "why": why,
                    "action": res.get("action"), "secs": round(time.time() - t, 1)})
    return out


def build_chat_vault(embed_model: str, notes: list[dict]):
    """A throwaway vault with the sample notes, embedded by `embed_model`."""
    from . import notes as N
    from .config import for_embedder
    from .engine import Engine
    from .llm import OfflineRouter
    from .vecs import OllamaEmbedder
    root = tempfile.mkdtemp(prefix="mnemo-bench-")
    embedder = OllamaEmbedder(embed_model)
    engine = Engine(root, embedder=embedder, provider=OfflineRouter(),
                    cfg=for_embedder(embedder), enrich=False)
    for n in notes:
        made = engine.create_note(n["title"], n["body"], n.get("topic"))
        N.set_vector(engine.conn, made["id"], engine._embed_one(f"{n['title']}\n{n['body']}"),
                     embedder.name)
        if n.get("due"):
            engine.schedule(made["id"], n["due"], remind=True)
    return engine, root


def bench_chat(model: str, engine, questions: list[dict]) -> list[dict]:
    """The chat as the app runs it: with tools when the model has them."""
    from . import agent as A
    from .chat import Chat, OllamaChat
    from .websearch import WebConfig
    use_tools = "tools" in R.caps(model)
    out = []
    for case in questions:
        t, text, from_notes = time.time(), [], False
        chat = (Chat(engine, tool_model=A.OllamaTools(model)) if use_tools
                else Chat(engine, model=OllamaChat(model)))
        try:
            for ev in chat.events(case["q"], [], web=WebConfig(mode="off")):
                if ev["type"] == "token":
                    text.append(ev["text"])
                elif ev["type"] == "done":
                    from_notes = ev.get("fromNotes", False)
                elif ev["type"] == "error":
                    text.append(f"[error: {ev['message']}]")
            ok, why = score_chat(case, "".join(text), from_notes)
        except Exception as exc:
            ok, why = False, f"error {type(exc).__name__}: {exc}"[:120]
        # The full answer is kept, so a judging rule can be fixed and re-applied
        # without asking every model again.
        out.append({"q": case["q"], "ok": ok, "why": why, "no_answer": bool(case.get("no_answer")),
                    "answer": "".join(text).strip(), "secs": round(time.time() - t, 1)})
    return out


def bench_tools(model: str, cases: list[dict]) -> list[dict]:
    from . import agent as A
    system = ("You are the assistant inside a private notes app on the user's computer. "
              "Use a tool when it helps; for small talk just reply.")
    out = []
    for case in cases:
        t, calls = time.time(), []
        try:
            for kind, val in A.OllamaTools(model, temperature=0.0).turn(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": case["q"]}], list(A.SPECS.values())):
                if kind == "calls":
                    calls = val
            ok, why = score_tool(case, calls)
        except Exception as exc:
            ok, why = False, f"error {type(exc).__name__}: {exc}"[:120]
        out.append({"q": case["q"], "ok": ok, "why": why,
                    "tool": (calls[0]["function"]["name"] if calls else None),
                    "secs": round(time.time() - t, 1)})
    return out


def bench_vision(model: str, cases: list[dict]) -> list[dict]:
    from .enrich import DESCRIBE_PROMPT, OCR_PROMPT, _vision_call
    out = []
    for case in cases:
        data = (REPO_ROOT / case["image"]).read_bytes()
        t = time.time()
        desc, err1 = _vision_call(data, DESCRIBE_PROMPT, model, OLLAMA_HOST, 300)
        text, err2 = _vision_call(data, OCR_PROMPT, model, OLLAMA_HOST, 300)
        sim = similarity(text, case["text"])
        described = any(w in _low(desc) for w in case["describe_any"])
        out.append({"image": Path(case["image"]).name, "ok": sim >= 0.9 and described,
                    "ocr_similarity": round(sim, 3), "described": described,
                    "why": err1 or err2 or ("" if sim >= 0.9 else f"text only {sim:.0%} right"),
                    "secs": round(time.time() - t, 1)})
    return out


def bench_embedder(model: str, chat: dict) -> dict:
    from . import search as S
    from .calibrate import measure
    from .vecs import OllamaEmbedder
    unload_all()
    t = time.time()
    m = measure(OllamaEmbedder(model), n=240)
    engine, root = build_chat_vault(model, chat["notes"])
    hits1 = hits5 = total = 0
    try:
        for case in chat["questions"]:
            want = case.get("note")              # the note that answers it
            if not want:
                continue
            total += 1
            hits = [h.title for h in S.search(engine.conn, case["q"],
                                               engine._embed_one(case["q"]), limit=5)]
            hits1 += bool(hits) and hits[0] == want
            hits5 += want in hits
    finally:
        engine.close()
        shutil.rmtree(root, ignore_errors=True)
    mem = next((x for x in R.loaded() if x["name"].split(":")[0] == model.split(":")[0]), {})
    return {"filing_top1": round(m["accuracy"]["top1"], 3), "filing_top3": round(m["accuracy"]["top3"], 3),
            "same_p10": round(m["same"]["p10"], 3), "diff_p99": round(m["diff"]["p99"], 3),
            "gap": round(m["same"]["p10"] - m["diff"]["p99"], 3),
            "chat_recall1": round(hits1 / max(total, 1), 3), "chat_recall5": round(hits5 / max(total, 1), 3),
            "vram_gb": round(mem.get("size_vram", 0) / 1e9, 2), "secs": round(time.time() - t)}


# ------------------------------------------------------------------- report ----
def _pct(rows: list[dict], where=lambda r: True) -> str:
    rows = [r for r in rows if where(r)]
    return f"{sum(r['ok'] for r in rows)}/{len(rows)}" if rows else "–"


def report(results: dict) -> str:
    lines = ["# Model benchmark", "",
             f"Run {results['when']} on {results['machine']}. Ollama {results['ollama']}.",
             "Scores are passes out of cases. See `tests/bench/` for the cases and "
             "`mnemo/bench.py` for how each one is judged.", "",
             "## Brains", "",
             "| Model | Note AI | Chat answers | Honest \"don't know\" | Tools | Picture text | "
             "First token | Tokens/s | Load | VRAM (on GPU) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in results["brains"].items():
        sp = r.get("speed") or {}
        vision = r.get("vision")
        vis = (" / ".join(f"{v['ocr_similarity']:.0%}" for v in vision) if vision else "–")
        lines.append(
            f"| {name} | {_pct(r.get('noteai', []))} | "
            f"{_pct(r.get('chat', []), lambda x: not x['no_answer'])} | "
            f"{_pct(r.get('chat', []), lambda x: x['no_answer'])} | "
            f"{_pct(r['tools']) if r.get('tools') else '–'} | {vis} | "
            f"{sp.get('first_token_s', '–')} s | {sp.get('tokens_per_s', '–')} | "
            f"{sp.get('load_s', '–')} s | {sp.get('vram_gb', '–')} GB ({sp.get('on_gpu_pct', '–')}%) |")
    lines += ["", "## Embedders", "",
              "| Model | Filing top-1 | Filing top-3 | Related vs unrelated gap | "
              "Chat recall@1 | Chat recall@5 | VRAM |", "|---|---|---|---|---|---|---|"]
    for name, e in results["embedders"].items():
        lines.append(f"| {name} | {e['filing_top1']:.0%} | {e['filing_top3']:.0%} | "
                     f"{e['gap']:+.3f} | {e['chat_recall1']:.0%} | {e['chat_recall5']:.0%} | "
                     f"{e['vram_gb']} GB |")
    lines += ["", "## Where each brain went wrong", ""]
    for name, r in results["brains"].items():
        misses = [f"- {k}: {x.get('request') or x.get('q') or x.get('image')} → {x['why']}"
                  for k in ("noteai", "chat", "tools", "vision") for x in r.get(k) or []
                  if not x["ok"]]
        if r.get("error"):
            misses.insert(0, f"- could not run: {r['error']}")
        lines += [f"### {name}", ""] + (misses or ["- no misses"]) + [""]
    return "\n".join(lines)


def rescore(results: dict, chat: dict) -> None:
    """Re-judge saved chat answers with the current rules (no model calls)."""
    cases = {c["q"]: c for c in chat["questions"]}
    for r in results["brains"].values():
        for row in r.get("chat", []):
            if "answer" in row and row["q"] in cases:
                row["ok"], row["why"] = score_chat(cases[row["q"]], row["answer"], False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--brains", default=",".join(BRAINS))
    ap.add_argument("--embedders", default=",".join(EMBEDDERS))
    ap.add_argument("--skip", default="", help="comma list of: noteai,chat,tools,vision,speed,embed")
    ap.add_argument("--merge", action="store_true",
                    help="add to the last results instead of starting over")
    args = ap.parse_args(argv)
    skip = set(filter(None, args.skip.split(",")))
    if not R.start(wait=60):
        print("Ollama isn't running and couldn't be started.", file=sys.stderr)
        return 1

    load = lambda f: json.loads((DATA / f).read_text(encoding="utf-8"))  # noqa: E731
    noteai_cases, chat, tools, vision = (load("noteai.json"), load("chat.json"),
                                         load("tools.json"), load("vision.json"))
    import platform
    results = {"when": time.strftime("%Y-%m-%d %H:%M"), "ollama": R.version(),
               "machine": platform.processor() or platform.machine(), "brains": {}, "embedders": {}}
    previous = OUT / "bench-results.json"
    if args.merge and previous.exists():
        old = json.loads(previous.read_text(encoding="utf-8"))
        results["brains"], results["embedders"] = old.get("brains", {}), old.get("embedders", {})

    for model in [m for m in args.brains.split(",") if m]:
        if not R.has_model(model):
            print(f"skip {model}: not installed", flush=True)
            continue
        caps = R.caps(model)
        r: dict = results["brains"].get(model) or {}      # --merge keeps earlier parts
        r["caps"] = sorted(caps)
        r.pop("error", None)
        print(f"== {model} {sorted(caps)}", flush=True)
        try:
            if "speed" not in skip:
                r["speed"] = bench_speed(model)
                print("   speed", r["speed"], flush=True)
            if "noteai" not in skip:
                r["noteai"] = bench_noteai(model, noteai_cases)
                print("   note AI", _pct(r["noteai"]), flush=True)
            if "chat" not in skip:
                engine, root = build_chat_vault(EMBEDDERS[0], chat["notes"])
                try:
                    r["chat"] = bench_chat(model, engine, chat["questions"])
                finally:
                    engine.close()
                    shutil.rmtree(root, ignore_errors=True)
                print("   chat", _pct(r["chat"], lambda x: not x["no_answer"]), "honest",
                      _pct(r["chat"], lambda x: x["no_answer"]), flush=True)
            if "tools" not in skip and "tools" in caps:
                r["tools"] = bench_tools(model, tools)
                print("   tools", _pct(r["tools"]), flush=True)
            if "vision" not in skip and "vision" in caps:
                r["vision"] = bench_vision(model, vision)
                print("   vision", [v["ocr_similarity"] for v in r["vision"]], flush=True)
        except Exception as exc:
            r["error"] = f"{type(exc).__name__}: {exc}"[:200]
            print("   ERROR", r["error"], flush=True)
        results["brains"][model] = r

    if "embed" not in skip:
        for model in [m for m in args.embedders.split(",") if m]:
            if not R.has_model(model):
                print(f"skip {model}: not installed", flush=True)
                continue
            print(f"== embedder {model}", flush=True)
            try:
                results["embedders"][model] = bench_embedder(model, chat)
                print("  ", results["embedders"][model], flush=True)
            except Exception as exc:
                print("   ERROR", exc, flush=True)

    rescore(results, chat)              # earlier runs are judged by today's rules too
    OUT.mkdir(exist_ok=True)
    (OUT / "bench-results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    (OUT / "bench-results.md").write_text(report(results), encoding="utf-8")
    print(f"wrote {OUT / 'bench-results.md'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
