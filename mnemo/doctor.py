"""Verify the local model stack actually behaves as the design assumes.

    python -m mnemo.doctor

The Ollama code paths were written against documented behaviour long before a
model existed to run them. This is what turns them from "written" into
"verified" -- and it checks the specific properties the design depends on, not
just that the server answers:

  * embeddings have the expected dimension and are deterministic
  * embeddings are SEMANTIC -- 'car' ~ 'automobile' with no shared characters,
    which is exactly what the offline hashing embedder cannot do
  * schema-constrained decoding returns parseable JSON every time
  * the router picks a topic_id from the menu instead of inventing one
  * latency is inside the budget for "capture feels instant"
"""
from __future__ import annotations

import json
import statistics
import sys
import time

from . import runtime
from .llm import Candidate, OllamaRouter, RouteRequest
from . import models
from .models import EMBED_DIM, EMBED_MODEL, resident_estimate
from .paths import REPO_ROOT, describe, ollama_binary
from .vecs import OllamaEmbedder, cosine

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def check_runtime() -> bool:
    binary = ollama_binary()
    if binary is None:
        record(FAIL, "bundled runtime", "not installed; run: python -m mnemo.setup")
        return False
    record(PASS, "bundled runtime", str(binary.relative_to(REPO_ROOT)))

    if not runtime.start():
        record(FAIL, "server", "could not start")
        return False
    record(PASS, "server", f"ollama {runtime.version()}")

    models = runtime.installed_models()
    record(PASS if models else WARN, "models in repo", ", ".join(models) or "none")
    return True


def check_embeddings() -> bool:
    if not runtime.has_model(EMBED_MODEL):
        record(FAIL, "embed model", f"{EMBED_MODEL} not pulled")
        return False

    embedder = OllamaEmbedder(model=EMBED_MODEL)
    try:
        started = time.perf_counter()
        vectors = embedder.embed(["hello world", "hello world"])
        elapsed = (time.perf_counter() - started) * 1000
    except Exception as exc:
        record(FAIL, "embed call", repr(exc)[:120])
        return False

    dim = len(vectors[0])
    record(PASS if dim == EMBED_DIM else WARN, "embed dimension",
           f"{dim}" + ("" if dim == EMBED_DIM else f" (expected {EMBED_DIM}; "
                       f"set MNEMO_EMBED_DIM={dim})"))

    identical = max(abs(a - b) for a, b in zip(vectors[0], vectors[1])) < 1e-6
    record(PASS if identical else FAIL, "embed determinism",
           "same text -> same vector" if identical else "NON-DETERMINISTIC")
    record(PASS, "embed latency", f"{elapsed:.0f} ms for 2 texts")

    # The property that matters: real semantics, which feature hashing lacks.
    pairs = [("car", "automobile"), ("sourdough bread", "baking loaves"),
             ("machine learning", "artificial intelligence")]
    controls = [("car", "sourdough bread"), ("guitar chords", "tax deduction")]
    near = [cosine(*embedder.embed([a, b])) for a, b in pairs]
    far = [cosine(*embedder.embed([a, b])) for a, b in controls]
    gap = min(near) - max(far)
    record(PASS if gap > 0.05 else FAIL, "embed semantics",
           f"synonyms {min(near):.3f}-{max(near):.3f} vs unrelated "
           f"{max(far):.3f}  (gap {gap:+.3f})")
    return True


def check_router() -> bool:
    brain = models.ROUTER_MODEL
    if not runtime.has_model(brain):
        record(FAIL, "router model", f"{brain} not pulled")
        return False

    router = OllamaRouter(model=brain)
    candidates = [
        Candidate("t_ai", "Technology/AI", 0.81, 24, "RAG chunking tricks"),
        Candidate("t_web", "Technology/Web Dev", 0.63, 11, "htmx vs React"),
        Candidate("t_game", "Gaming", 0.22, 31, "Malenia strategy"),
    ]
    request = RouteRequest(
        text="Notes on reranking retrieved chunks with a cross-encoder before "
             "passing them to the model.",
        candidates=candidates,
    )

    latencies, valid, on_menu = [], 0, 0
    allowed = {c.topic_id for c in candidates}
    for _ in range(3):
        started = time.perf_counter()
        try:
            decision = router.route(request)
        except Exception as exc:
            record(FAIL, "router call", repr(exc)[:120])
            return False
        latencies.append((time.perf_counter() - started) * 1000)
        valid += 1
        if decision.action != "file" or decision.topic_id in allowed:
            on_menu += 1
        last = decision

    record(PASS if valid == 3 else FAIL, "constrained JSON",
           f"{valid}/3 parsed cleanly")
    record(PASS if on_menu == 3 else FAIL, "stays on the menu",
           f"{on_menu}/3 chose a real topic_id")
    record(PASS, "router decision",
           f"{last.action} -> {last.topic_id or last.name} "
           f"(confidence {last.confidence:.2f})")
    median = statistics.median(latencies)
    record(PASS if median < 6000 else WARN, "router latency",
           f"median {median:.0f} ms")
    return True


def check_offmenu_rejection() -> bool:
    """The router is only trusted because invalid ids are refused downstream."""
    from . import router as R
    from .db import connect
    from .llm import RouteDecision
    import os
    import tempfile

    conn = connect(os.path.join(tempfile.mkdtemp(), "i.db"))

    class Liar:
        name = "liar"

        def route(self, req):
            return RouteDecision(action="file", topic_id="t_hallucinated",
                                 confidence=0.99)

    from .config import ONLINE
    outcome = R.route(conn, Liar(), ONLINE, [0.0] * 8, "text")
    ok = outcome.topic_id is None and outcome.outcome == R.TO_INBOX
    record(PASS if ok else FAIL, "off-menu id refused",
           "hallucinated topic_id -> Inbox" if ok else "ACCEPTED A FAKE ID")
    return ok


def main() -> int:
    print(f"mnemo doctor — {REPO_ROOT}\n")
    for key, value in describe().items():
        print(f"  {key:10} {value}")
    if runtime.is_up():
        models.refresh()          # the quality profile decides the brain
    print(f"\n  profile  {models.ACTIVE.label}  (brain {models.ROUTER_MODEL})")
    print(f"  estimated resident VRAM: {resident_estimate():.1f} GB "
          f"({EMBED_MODEL} + {models.ROUTER_MODEL})\n")

    print("runtime")
    if check_runtime():
        print("\nembeddings")
        check_embeddings()
        print("\nrouter")
        check_router()
    print("\nguardrails")
    check_offmenu_rejection()

    failed = [r for r in _results if r[0] == FAIL]
    warned = [r for r in _results if r[0] == WARN]
    print(f"\n{len(_results) - len(failed) - len(warned)} passed, "
          f"{len(warned)} warned, {len(failed)} failed")
    if failed:
        print("\nfailures:")
        for _, name, detail in failed:
            print(f"  {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
