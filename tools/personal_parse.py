"""Split WhatsApp exports into clean data for the Personal notes page.

Not part of the app or the engine: a helper run by hand when new files arrive.

    python tools/personal_parse.py D:/temp/whatsapp_notes [--profile=<id>]

It copies what it reads into the profile's _personal/sources/ (the originals
are left alone), hides private details, and writes:

    data/<source>.json     messages, moments, stats (private text replaced by a label)
    data/links.json        one entry per distinct link
    private/private.json   the hidden text, for the page's Show button only
    manifest.json          what has been read, so reruns only report what's new

Reading a source's masked text is safe by construction: nothing flagged here
ever reaches data/. Anything Claude reads comes from data/, never from private/.
Previews and other fields already in links.json are kept on a rerun.
"""
from __future__ import annotations

import collections
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mnemo.links import PRIVATE_LINK_HOSTS, TRACKING, clean_url, link_info  # noqa: E402,F401
from mnemo.paths import DEFAULT_VAULT, PROFILES_DIR  # noqa: E402

# The first profile, or --profile=<id> for another.
_chosen = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--profile=")), None)
OUT = (PROFILES_DIR / _chosen if _chosen else DEFAULT_VAULT) / "_personal"

HEADER = re.compile(r"^(\d{2}/\d{2}/\d{2}), (\d{1,2}):(\d{2})[\u202f ](am|pm) - (.*)$", re.I)
SENDER = re.compile(r"^([^:\n]{1,40}): (.*)$", re.S)
URL = re.compile(r"https?://[^\s<>\"]+", re.I)
BARE = re.compile(r"(?<![\w@/.-])((?:[a-z0-9-]+\.)+(?:com|io|dev|ai|gg|co|xyz|app|today|org|net|in)\b(?:/[^\s]*)?)", re.I)
FILE_ATTACHED = re.compile(r"^(.+?) \(file attached\)$", re.M)
EDITED = "<This message was edited>"
MOMENT_GAP = 600       # seconds between messages that still belong to one thought

# Checked on the message with its links taken out, except where a link is the
# secret. Order matters only for which label is shown. Leans towards hiding.
PRIVATE = [
    ("Wi-Fi password", re.compile(r"\bWIFI:", re.I)),
    ("Password", re.compile(r"(?i)\b(pass(word|code|wd)?|pwd|pin|otp|cvv)\b\s*[:=-]?\s*\S+")),
    ("Password-like text", re.compile(r"(?<![\w.@])[A-Za-z][A-Za-z0-9]{2,}[@#$!%&*][A-Za-z0-9@#$!%&*]*\d[A-Za-z0-9@#$!%&*]*(?![\w.])")),
    ("Bank details", re.compile(r"(?i)\b(a/c|ac\s*no|account\s*(no|number|name)|ifsc|upi)\b|\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    ("ID number", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b|(?i:\b(aadhaar|aadhar|passport\s*no)\b)")),
    ("Card-length number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("Phone number", re.compile(r"(?<![\d.])(?:\+?\d{1,3}[ -]?)?[6-9]\d{4}[ -]?\d{5}(?![\d.])")),
    ("Long number", re.compile(r"(?<![\d.])\d{8,}(?![\d.])")),
    ("Email address", re.compile(r"[\w.+-]+@[\w-]+\.\w+")),
    ("Address", re.compile(r"(?i)\b(room\s*no|flat\s*no|house\s*no|ground\s*floor|\d{6}\s*(india)?\s*$)|\b\d{6}\b.*\b(india|road|rd|nagar|campus)\b")),
    ("Possible date of birth", re.compile(r"(?i)\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+(19|20)\d{2}\b")),
]
PRIVATE_IN_LINK = [
    ("Invite link", re.compile(r"chat\.whatsapp\.com/|meet\.google\.com/", re.I)),
    ("Link with an access key", re.compile(r"[?&](token|sig|signature|key|auth)=", re.I)),
]
# A lone word mixing letters and digits, eight or more long, is usually a
# password or a code. Short ones like "Bh3" or "T2" are not worth hiding.
LONE_CODE = re.compile(r"^(?=\S*[A-Za-z])(?=\S*\d)\S{8,32}$")
NOT_SECRET = re.compile(r"\S+\.(?:pdf|zip|jpe?g|png|mp4|mp3|docx?|pptx?|xlsx?|odt|ipynb|py|txt|csv)\b"
                        r"|(?<![\w.])@[\w.]+|<Media omitted>", re.I)


def private_label(text: str) -> str | None:
    for label, rule in PRIVATE_IN_LINK:
        if rule.search(text):
            return label
    # Links, file names and @handles are not secrets, and their numbers and
    # underscores would otherwise look like them.
    plain = NOT_SECRET.sub(" ", FILE_ATTACHED.sub(" ", URL.sub(" ", text)))
    for label, rule in PRIVATE:
        if rule.search(plain):
            return label
    stripped = plain.strip()
    if LONE_CODE.match(stripped) and not BARE.fullmatch(stripped):
        return "Password-like text"
    return None


def source_name(folder: Path) -> str:
    return re.sub(r"^WhatsApp Chat with ", "", folder.name).strip()


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "source"


def sha(text: str, size: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:size]


def read_lines(path: Path) -> tuple[list[dict], list[str], int]:
    """(raw messages, system lines, line count). Every line lands in one or the other."""
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    out: list[dict] = []
    for line in lines:
        m = HEADER.match(line)
        if not m:
            if out:
                out[-1]["raw"] += "\n" + line
            continue
        d, hour, minute, ap, rest = m.groups()
        hour = int(hour) % 12 + (12 if ap.lower() == "pm" else 0)
        when = datetime.strptime(d, "%d/%m/%y").replace(hour=hour, minute=int(minute))
        out.append({"when": when, "raw": rest})
    messages, system = [], []
    for item in out:
        raw = item["raw"]
        sender = SENDER.match(raw)
        if not sender or raw.startswith(("You ", "Messages and calls")):
            system.append(raw.strip())
            continue
        messages.append({"when": item["when"], "text": sender.group(2).rstrip()})
    return messages, system, len(lines)


def note_type(text: str) -> str:
    lines = [l for l in text.split("\n") if l.strip()]
    if re.search(r"#include|```|\bdef \w+\(|\bclass \w+|;\s*$|\bint main\b", text, re.M):
        return "code"
    if len(text) > 240:
        return "long"
    if len(lines) >= 3:
        return "list"
    return "idea"


def parse_source(folder: Path, seen_links: dict) -> dict:
    name, key = source_name(folder), slug(source_name(folder))
    dest = OUT / "sources" / key
    txt = next(folder.glob("*.txt"))
    raw_msgs, system, line_count = read_lines(txt)

    (dest / "exports").mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(txt.stat().st_mtime).strftime("%Y-%m-%d")
    shutil.copy2(txt, dest / "exports" / f"{stamp}.txt")
    present = {}
    for f in folder.iterdir():
        if f.is_file() and f != txt:
            (dest / "files").mkdir(exist_ok=True)
            shutil.copy2(f, dest / "files" / f.name)
            is_pdf = f.read_bytes()[:5] == b"%PDF-"
            present[f.name] = {"name": f.name, "size": f.stat().st_size,
                               "type": "application/pdf" if is_pdf else None}

    messages, hidden, counts = [], {}, collections.Counter()
    occurrences: collections.Counter = collections.Counter()
    first_seen: dict = {}
    for raw in raw_msgs:
        text = raw["text"]
        edited = EDITED in text
        text = text.replace(EDITED, "").strip()
        iso = raw["when"].strftime("%Y-%m-%dT%H:%M:00")
        base = f"{key}|{iso}|{text}"
        occurrences[base] += 1
        mid = sha(f"{base}|{occurrences[base]}")
        msg = {"id": mid, "ts": iso, "kind": "text", "text": text}
        if edited:
            msg["edited"] = True

        if text in ("You deleted this message", "This message was deleted"):
            msg.update(kind="deleted", text="")
        elif text.startswith("<Media omitted>"):
            rest = text[len("<Media omitted>"):].strip()
            msg.update(kind="missing_file", text=rest)
            looks_like_name = rest and "\n" not in rest and re.search(r"\.\w{1,5}$", rest)
            msg["file"] = {"name": rest if looks_like_name else None, "present": False}
            if looks_like_name:
                msg["text"] = ""
        else:
            names = [n.strip().rstrip(".") if n.strip().rstrip(".") in present else n.strip()
                     for n in FILE_ATTACHED.findall(text)]
            if names:
                known = present.get(names[0])
                msg.update(kind="file", text="",
                           file={"name": names[0], "present": bool(known),
                                 "type": (known or {}).get("type"),
                                 "size": (known or {}).get("size")})

        label = private_label(text)
        if label:
            hidden[mid] = text
            msg.update(private=label, text="")
            msg.pop("file", None) if msg["kind"] != "file" else None
            counts["private"] += 1
            messages.append(msg)
            continue

        body = msg["text"]
        urls = [clean_url(u) for u in URL.findall(body)]
        handle = re.search(r"See this Instagram post by @([\w.]+)", body)
        no_urls = URL.sub(" ", body)
        bare = [] if msg["kind"] != "text" else [
            "https://" + b.rstrip(".,;:)").lower() for b in BARE.findall(no_urls)]
        ids = []
        for url in urls + bare:
            info = link_info(url)
            lid = sha(info["key"])
            entry = seen_links.setdefault(lid, {"id": lid, "key": info["key"], "saves": []})
            entry.update({k: v for k, v in info.items() if k in ("site", "kind", "private")})
            entry["url"] = entry.get("url") or info.get("url") or url
            if handle:
                entry["handle"] = handle.group(1)
            elif info.get("handle"):
                entry["handle"] = info["handle"]
            entry["saves"].append({"source": key, "msg": mid, "ts": iso})
            if lid not in ids:
                ids.append(lid)
        if ids:
            msg["links"] = ids
            # What's left of the message besides the link and the share wrapper.
            left = re.sub(r"See this Instagram post by @[\w.]+:?", " ", URL.sub(" ", body))
            left = " ".join(left.split()).strip(" :-")
            msg["kind"], msg["text"] = "link", left if left.lower() != (bare[0][8:] if bare else "") else ""
        if msg["kind"] == "text":
            msg["type"] = note_type(body)
            norm = " ".join(body.lower().split())
            if len(norm) >= 25:
                if norm in first_seen:
                    msg["dup_of"] = first_seen[norm]
                else:
                    first_seen[norm] = mid
        messages.append(msg)

    # Moments: a burst of messages is usually one thought.
    moments, cur = [], None
    prev = None
    for m in messages:
        when = datetime.fromisoformat(m["ts"])
        if prev is None or (when - prev).total_seconds() > MOMENT_GAP:
            cur = {"id": sha(key + m["id"]), "start": m["ts"], "end": m["ts"], "msgs": []}
            moments.append(cur)
        cur["msgs"].append(m["id"])
        cur["end"] = m["ts"]
        m["moment"] = cur["id"]
        prev = when

    months = collections.Counter(m["ts"][:7] for m in messages if m["kind"] != "deleted")
    kinds = collections.Counter(m["kind"] for m in messages)
    data = {
        "source": key, "name": name, "from": "WhatsApp",
        "start": messages[0]["ts"], "end": messages[-1]["ts"],
        "counts": {"messages": len(messages), **kinds, "private": counts["private"],
                   "moments": len(moments), "lines": line_count},
        "months": dict(sorted(months.items())),
        "system": [s for s in system if not s.startswith("Messages and calls")],
        "messages": messages, "moments": moments,
    }
    return {"data": data, "hidden": hidden, "key": key, "txt": txt}


def main() -> None:
    folders = [a for a in sys.argv[1:] if not a.startswith("--")]
    inbox = Path(folders[0] if folders else "D:/temp/whatsapp_notes")
    (OUT / "data").mkdir(parents=True, exist_ok=True)
    (OUT / "private").mkdir(parents=True, exist_ok=True)
    links_file = OUT / "data" / "links.json"
    old_links = json.loads(links_file.read_text(encoding="utf-8")) if links_file.exists() else {}
    # Every save is recounted below; what was fetched or written about a link stays.
    links = {k: {**v, "saves": []} for k, v in old_links.items()}
    hidden_all: dict = {}
    manifest = {}
    for folder in sorted(p for p in inbox.iterdir() if p.is_dir()):
        if not any(folder.glob("*.txt")):
            print("skipped, no text export:", folder.name)
            continue
        result = parse_source(folder, links)
        data = result["data"]
        (OUT / "data" / f"{result['key']}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
        hidden_all.update(result["hidden"])
        manifest[str(result["txt"])] = hashlib.sha1(result["txt"].read_bytes()).hexdigest()
        print(result["key"], data["counts"])
    links = {k: v for k, v in links.items() if v["saves"]}
    links_file.write_text(json.dumps(links, ensure_ascii=False, indent=0), encoding="utf-8")
    (OUT / "private" / "private.json").write_text(
        json.dumps(hidden_all, ensure_ascii=False, indent=0), encoding="utf-8")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print("distinct links:", len(links), "| private-type:", sum(1 for v in links.values() if v.get("private")),
          "| saved more than once:", sum(1 for v in links.values() if len(v["saves"]) > 1))


if __name__ == "__main__":
    main()
