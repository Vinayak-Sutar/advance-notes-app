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

import html
import ipaddress
import json
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mnemo.enrich import USER_AGENT, _tls_context, fetch_url  # noqa: E402
from mnemo.paths import DEFAULT_VAULT, PROFILES_DIR  # noqa: E402

# The first profile, or --profile=<id> for another.
_chosen = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--profile=")), None)
OUT = (PROFILES_DIR / _chosen if _chosen else DEFAULT_VAULT) / "_personal"
LINKS = OUT / "data" / "links.json"
THUMBS = OUT / "thumbs"
# Seconds between requests. --slow suits a big batch: Instagram stopped
# answering after about 130 posts at the normal pace.
PAUSE = 8.0 if "--slow" in sys.argv else 3.0
MAX_THUMB = 1_500_000
GIVE_UP_AFTER = 6            # refusals in a row before stopping for the day

# "3,695 likes, 70 comments - someone on June 16, 2015: "the caption". "
IG_DESC = re.compile(r'^(?:(?P<likes>[\d,.KMkm]+) likes?, )?(?:(?P<comments>[\d,.KMkm]+) comments? - )?'
                     r'(?P<user>[\w.]+) on (?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})(?:: "?(?P<caption>.*?)"?\.?\s*)?$', re.S)
IG_TITLE = re.compile(r'^(?P<name>.*?) on Instagram(?:: "?(?P<caption>.*?)"?)?$', re.S)


def is_public(url: str) -> bool | None:
    """True for a public address, False for a local one, None if the name
    can't be looked up (usually the connection, not the link)."""
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return None
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


def save_thumb(link_id: str, url: str) -> str | None:
    if not url or not url.startswith("https://") or not is_public(url):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15, context=_tls_context()) as resp:
            kind = resp.headers.get_content_type()
            data = resp.read(MAX_THUMB + 1)
    except Exception:
        return None
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(kind)
    if not ext or len(data) > MAX_THUMB:
        return None
    name = link_id + ext
    (THUMBS / name).write_bytes(data)
    return name


def youtube(link: dict) -> dict:
    api = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(link["url"], safe="")
    req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15, context=_tls_context()) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        # 401 and 403 only mean the owner blocked embedding; the video may be
        # fine, so ask its own page. Only a real "not found" counts as gone.
        return page(link) if exc.code in (401, 403) else {"status": "gone" if exc.code in (400, 404) else "failed"}
    except Exception:
        return {"status": "failed"}
    return {"status": "ok", "title": data.get("title"), "creator": data.get("author_name"),
            "_thumb": data.get("thumbnail_url")}


def instagram_embed(link: dict) -> dict:
    """Instagram's public embed page, for posts whose normal page asks to log in."""
    url = link["url"].rstrip("/") + "/embed/captioned/"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15, context=_tls_context()) as resp:
            text = resp.read(1_500_000).decode("utf-8", "replace")
    except Exception:
        return {"status": "refused"}
    image = re.search(r'class="EmbeddedMediaImage"[^>]*?src="([^"]+)"', text)
    if not image:
        return {"status": "refused"}
    user = re.search(r'class="UsernameText"[^>]*>([^<]*)', text)
    block = re.search(r'class="Caption"[^>]*>(.*?)<div class="CaptionComments"', text, re.S)
    caption = ""
    if block:
        caption = html.unescape(re.sub(r"<[^>]+>", " ", block.group(1)))
        caption = " ".join(caption.split())
        if user and caption.startswith(user.group(1)):
            caption = caption[len(user.group(1)):].strip()
    return {"status": "ok", "creator": html.unescape(user.group(1)) if user else None,
            "caption": caption, "_thumb": html.unescape(image.group(1))}


def page(link: dict) -> dict:
    got = fetch_url(link["url"])
    error = got.error or ""
    if "HTTP Error 404" in error or "HTTP Error 410" in error:
        return {"status": "gone"}
    if re.search(r"HTTP Error (401|403|429)", error):
        return instagram_embed(link) if link.get("site") == "Instagram" else {"status": "refused"}
    meta = got.meta if hasattr(got, "meta") else {}
    if not got.ok and not meta:
        return {"status": "failed"}
    unesc = lambda s: html.unescape(s or "").strip()
    title, desc = unesc(meta.get("og:title")) or got.title, unesc(meta.get("og:description") or meta.get("description"))
    out = {"status": "ok", "title": title, "caption": desc, "_thumb": unesc(meta.get("og:image"))}
    if link.get("site") == "YouTube" and title.strip() in ("", "YouTube", "- YouTube"):
        return {"status": "gone"}
    if link.get("site") == "Instagram":
        if "Login" in title[:12] or title.strip() in ("", "Instagram"):
            return instagram_embed(link)
        m = IG_DESC.match(desc)
        if m:
            out.update(caption=m.group("caption") or "", likes=m.group("likes"), comments=m.group("comments"),
                       creator=m.group("user"))
            try:
                out["posted"] = datetime.strptime(m.group("date"), "%B %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
        n = IG_TITLE.match(title)
        if n:
            out["creatorName"] = n.group("name")
            out["caption"] = out.get("caption") or n.group("caption") or ""
        out["title"] = None
    return out


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
        public = is_public(link["url"])
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
        result = youtube(link) if link.get("site") == "YouTube" else page(link)
        thumb = result.pop("_thumb", None)
        link.update({k: v for k, v in result.items() if v not in (None, "")})
        link["fetched"] = datetime.now().strftime("%Y-%m-%d")
        if result["status"] == "ok" and thumb:
            name = save_thumb(link["id"], thumb)
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
