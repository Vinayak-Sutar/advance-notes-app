"""Fetch link previews for the Personal notes page, once, and keep them.

Not part of the app: a helper run by hand after tools/personal_parse.py.

    python tools/personal_previews.py [--profile=<id>]

Each public link goes only to its own site, with tracking codes already
removed. Private-type links (Drive, Docs, forms, invites...) are never touched,
nor is anything on a local network address. Titles, captions and thumbnails are
stored in the profile's _personal, so tiles keep working if a post later disappears and
the page itself never contacts these sites. Safe to stop and rerun: links
already done are skipped, and a site that starts refusing is left alone for the
rest of the run.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mnemo import links as L  # noqa: E402
from mnemo.paths import DEFAULT_VAULT, PROFILES_DIR  # noqa: E402

# The first profile, or --profile=<id> for another.
_chosen = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--profile=")), None)
OUT = (PROFILES_DIR / _chosen if _chosen else DEFAULT_VAULT) / "_personal"
LINKS = OUT / "data" / "links.json"
THUMBS = OUT / "thumbs"
# Seconds between requests. --slow suits a big batch: Instagram stopped
# answering after about 130 posts at the normal pace.
PAUSE = 8.0 if "--slow" in sys.argv else 3.0
GIVE_UP_AFTER = 6            # refusals in a row before stopping for the day

def main() -> None:
    THUMBS.mkdir(parents=True, exist_ok=True)
    links = json.loads(LINKS.read_text(encoding="utf-8"))
    again = "--recheck-gone" in sys.argv        # e.g. after a rule for "gone" was fixed
    todo = [l for l in links.values() if not l.get("private")
            and (l.get("status") not in ("ok", "gone") or (again and l.get("status") == "gone"))]
    # Never-tried links first; --first=<source> puts one chat's links ahead of the rest.
    first = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--first=")), None)
    todo.sort(key=lambda l: (l.get("status") is not None,
                             not any(save["source"] == first for save in l["saves"])))
    print(f"{len(todo)} links to fetch, about {round(len(todo) * PAUSE / 60)} minutes", flush=True)
    refused: dict = {}          # refusals in a row, per site
    lookups_failed = 0          # addresses in a row that couldn't be looked up
    for i, link in enumerate(todo, 1):
        site = link.get("site")
        if refused.get(site, 0) >= GIVE_UP_AFTER:
            continue            # that site has had enough for today; the others carry on
        public = L.is_public(link["url"])
        if public is None:
            lookups_failed += 1
            if lookups_failed >= 5:
                print("stopping: the connection seems to be down; run again later", flush=True)
                break
            time.sleep(PAUSE)
            continue
        lookups_failed = 0
        if not public:
            link["status"] = "failed"
            continue
        result = L.youtube(link["url"]) if link.get("site") == "YouTube" else L.page(link["url"], link.get("site"))
        thumb = result.pop("_thumb", None)
        link.update({k: v for k, v in result.items() if v not in (None, "")})
        link["fetched"] = datetime.now().strftime("%Y-%m-%d")
        if result["status"] == "ok" and thumb:
            name = L.save_thumb(THUMBS, link["id"], thumb)
            if name:
                link["thumb"] = name
        refused[site] = refused.get(site, 0) + 1 if result["status"] == "refused" else 0
        if i % 10 == 0 or i == len(todo):
            LINKS.write_text(json.dumps(links, ensure_ascii=False, indent=0), encoding="utf-8")
            print(i, "done", flush=True)
        if refused[site] >= GIVE_UP_AFTER:
            print(f"{site} is refusing; skipping it for now, run again later", flush=True)
        time.sleep(PAUSE)
    LINKS.write_text(json.dumps(links, ensure_ascii=False, indent=0), encoding="utf-8")
    counts: dict = {}
    for l in links.values():
        key = "private" if l.get("private") else l.get("status", "not fetched")
        counts[key] = counts.get(key, 0) + 1
    print(counts, "| thumbnails:", sum(1 for l in links.values() if l.get("thumb")), flush=True)


if __name__ == "__main__":
    main()
