"""Links: every link in your notes, as tiles.

A tile is one link, however many notes it's in: a pasted link (a link note) or
a link written inside any note. The notes stay the truth; tiles are worked out
from them each time, so there's nothing to keep in step and nothing a rebuild
can lose. What's kept beside the notes, in the profile's `.mnemo/links/`:

    previews.json     each link's preview, fetched once: title, caption, picture
    thumbs/           the pictures
    layout.json       the order you dragged the tiles into

Fetching a preview contacts only the link's own site, with no note text, and
never a link that may need your account (Drive, Docs, invites) or one on a
local network. The same rules, and this same code, serve Personal notes.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from .enrich import USER_AGENT, _tls_context, fetch_url

# Query parts that only track who shared what.
TRACKING = re.compile(r"^(utm_.*|igsh|igshid|igsi|stkn|exln|si|feature|fbclid|gclid|ref|usp)$", re.I)
# Never fetched for a preview: opening them may need your account, or the
# address itself is the key.
PRIVATE_LINK_HOSTS = ("drive.google.com", "docs.google.com", "forms.gle", "meet.google.com",
                      "chat.whatsapp.com", "chatgpt.com", "claude.ai", "sendy.cuvette.tech", "t.me")
URL_IN_TEXT = re.compile(r"https?://[^\s<>()\[\]{}\"'`]+", re.I)
MAX_THUMB = 1_500_000
RETRY_DAYS = 1                     # a refused or failed preview is tried again after this

_files_lock = threading.Lock()     # previews.json and layout.json, written from several requests


# ------------------------------------------------------------ the address ----
def clean_url(raw: str) -> str:
    raw = raw.rstrip(".,;:)!'\"")
    p = urllib.parse.urlparse(raw)
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
            if not TRACKING.match(k)]
    host = p.netloc.lower()
    return p._replace(netloc=host, query=urllib.parse.urlencode(keep), fragment="").geturl()


def link_info(url: str) -> dict:
    """What a link is, from the address alone: its site, type and a key that
    matches the same post or video however it was shared."""
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower().removeprefix("www.")
    if host.startswith("m."):
        host = host[2:]
    path = p.path.rstrip("/")
    parts = [x for x in path.split("/") if x]
    query = dict(urllib.parse.parse_qsl(p.query))
    info = {"site": host, "kind": "page", "key": host + path}
    if host == "instagram.com":
        info["site"] = "Instagram"
        if parts and parts[0] in ("reel", "reels", "p", "tv") and len(parts) > 1:
            info.update(kind="reel" if parts[0] != "p" else "post", key="ig:" + parts[1],
                        url=f"https://www.instagram.com/{'reel' if parts[0] != 'p' else 'p'}/{parts[1]}/")
        elif parts:
            info.update(kind="profile", key="ig:@" + parts[0], handle=parts[0],
                        url=f"https://www.instagram.com/{parts[0]}/")
    elif host in ("youtube.com", "youtu.be", "music.youtube.com"):
        info["site"] = "YouTube"
        vid = parts[0] if host == "youtu.be" and parts else query.get("v")
        if parts and parts[0] in ("shorts", "live") and len(parts) > 1:
            vid = parts[1]
            info["kind"] = "short" if parts[0] == "shorts" else "live"
        else:
            info["kind"] = "video"
        if vid:
            info.update(key="yt:" + vid, url=f"https://www.youtube.com/watch?v={vid}")
    elif host == "docs.google.com":
        info.update(site="Google Docs", kind={"spreadsheets": "sheet", "presentation": "slides",
                                              "forms": "form"}.get(parts[0] if parts else "", "doc"))
    elif host == "drive.google.com":
        info.update(site="Google Drive", kind="drive")
    elif host == "forms.gle":
        info.update(site="Google Forms", kind="form")
    elif host in ("linkedin.com", "lnkd.in"):
        info["site"] = "LinkedIn"
    elif host == "github.com" or host.endswith(".github.io"):
        info.update(site="GitHub", kind="code")
    elif host == "maps.app.goo.gl":
        info.update(site="Google Maps", kind="place")
    info["private"] = any(h == host or host.endswith("." + h) for h in PRIVATE_LINK_HOSTS)
    return info


def find(text: str) -> list[str]:
    """Web addresses written in a note, cleaned, each once, in order."""
    seen, out = set(), []
    for raw in URL_IN_TEXT.findall(text or ""):
        url = clean_url(raw)
        if urllib.parse.urlparse(url).netloc and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# --------------------------------------------------------------- previews ----
def is_public(url: str) -> bool | None:
    """True for a public address, False for a local one, None if the name
    can't be looked up (usually the connection, not the link)."""
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return None
    return bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)


def save_thumb(folder: Path, name: str, url: str) -> str | None:
    """Keep a preview's picture, so the tile never asks the site again."""
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
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (name + ext)).write_bytes(data)
    return name + ext


IG_DESC = re.compile(r'^(?:(?P<likes>[\d,.KMkm]+) likes?, )?(?:(?P<comments>[\d,.KMkm]+) comments? - )?'
                     r'(?P<user>[\w.]+) on (?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})(?:: "?(?P<caption>.*?)"?\.?\s*)?$', re.S)
IG_TITLE = re.compile(r'^(?P<name>.*?) on Instagram(?:: "?(?P<caption>.*?)"?)?$', re.S)


def youtube(url: str) -> dict:
    api = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(api, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15, context=_tls_context()) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        # 401 and 403 only mean the owner blocked embedding; the video may be
        # fine, so ask its own page. Only a real "not found" counts as gone.
        return page(url, "YouTube") if exc.code in (401, 403) else {
            "status": "gone" if exc.code in (400, 404) else "failed"}
    except Exception:
        return {"status": "failed"}
    return {"status": "ok", "title": data.get("title"), "creator": data.get("author_name"),
            "_thumb": data.get("thumbnail_url")}


def instagram_embed(url: str) -> dict:
    """Instagram's public embed page, for posts whose normal page asks to log in."""
    req = urllib.request.Request(url.rstrip("/") + "/embed/captioned/",
                                 headers={"User-Agent": USER_AGENT})
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
        caption = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", block.group(1))).split())
        if user and caption.startswith(user.group(1)):
            caption = caption[len(user.group(1)):].strip()
    return {"status": "ok", "creator": html.unescape(user.group(1)) if user else None,
            "caption": caption, "_thumb": html.unescape(image.group(1))}


def page(url: str, site: str | None = None) -> dict:
    got = fetch_url(url)
    error = got.error or ""
    if "HTTP Error 404" in error or "HTTP Error 410" in error:
        return {"status": "gone"}
    if re.search(r"HTTP Error (401|403|429)", error):
        return instagram_embed(url) if site == "Instagram" else {"status": "refused"}
    meta = got.meta or {}
    if not got.ok and not meta:
        return {"status": "failed"}
    unesc = lambda s: html.unescape(s or "").strip()
    title = unesc(meta.get("og:title")) or got.title
    desc = unesc(meta.get("og:description") or meta.get("description"))
    out = {"status": "ok", "title": title, "caption": desc, "_thumb": unesc(meta.get("og:image"))}
    if site == "YouTube" and title.strip() in ("", "YouTube", "- YouTube"):
        return {"status": "gone"}
    if site == "Instagram":
        if "Login" in title[:12] or title.strip() in ("", "Instagram"):
            return instagram_embed(url)
        m = IG_DESC.match(desc)
        if m:
            out.update(caption=m.group("caption") or "", likes=m.group("likes"),
                       comments=m.group("comments"), creator=m.group("user"))
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


def preview(url: str) -> dict:
    """One link's preview, from its own site only. `_thumb` is the picture's address."""
    info = link_info(url)
    if info["private"]:
        return {"status": "private"}
    public = is_public(url)
    if public is None:
        return {"status": "offline"}         # the connection, not the link: try again later
    if not public:
        return {"status": "failed"}
    return youtube(url) if info["site"] == "YouTube" else page(url, info["site"])


# ----------------------------------------------------- tiles from the notes ----
class Links:
    """The Links page for one vault."""

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).resolve() / ".mnemo" / "links"
        self.thumbs = self.root / "thumbs"

    def _read(self, name: str, default):
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(self.root / name)

    def tiles(self, conn, note_id: str | None = None) -> list[dict]:
        """Every link, newest first unless you've arranged them; or one note's."""
        from . import notes as N
        from . import sections as Sec
        from . import topics as T
        where = "status <> 'archived' AND (source_url IS NOT NULL OR body LIKE '%http%')"
        args: tuple = ()
        if note_id:
            where, args = "id = ?", (note_id,)
        tiles: dict[str, dict] = {}
        for row in conn.execute(f"SELECT * FROM notes WHERE {where}", args):
            body = row["body"] or ""
            image = row["kind"] == "image" and row["media_path"]
            # Only what you wrote: a link note's fetched page text has links of its own.
            mine = Sec.split_image(body).user if image else (
                Sec.split_link(body).user if row["source_url"] else body)
            found = [(u, "saved") for u in ([clean_url(row["source_url"])]
                                            if row["source_url"] and not image else [])]
            found += [(u, "in") for u in find(mine)]
            if not found:
                continue
            topic = T.path_of(conn, N.primary_topic(conn, row["id"]))
            for url, how in found:
                info = link_info(url)
                tile = tiles.setdefault(info["key"], {
                    "key": info["key"], "url": info.get("url", url), "site": info["site"],
                    "kind": info["kind"], "private": info["private"], "handle": info.get("handle"),
                    "saved": row["created_at"], "notes": []})
                tile["saved"] = min(tile["saved"], row["created_at"])
                if how == "saved":
                    tile.setdefault("noteTitle", row["title"])
                if not any(n["id"] == row["id"] for n in tile["notes"]):
                    tile["notes"].append({"id": row["id"], "title": row["title"] or "Untitled",
                                          "topic": topic, "how": how})
        previews = self._read("previews.json", {})
        out = []
        for tile in tiles.values():
            tile.update({k: v for k, v in (previews.get(tile["key"]) or {}).items() if v not in (None, "")})
            tile["title"] = tile.get("title") or tile.get("noteTitle")
            out.append(tile)
        out.sort(key=lambda t: t["saved"], reverse=True)
        if note_id:
            return out
        order = {key: i for i, key in enumerate(self.layout())}
        # Links saved since you arranged them come first, like new notes.
        return ([t for t in out if t["key"] not in order]
                + sorted((t for t in out if t["key"] in order), key=lambda t: order[t["key"]]))

    def layout(self) -> list[str]:
        return self._read("layout.json", [])

    def set_layout(self, order: list, known: set[str]) -> None:
        clean = list(dict.fromkeys(str(k) for k in order if str(k) in known))
        with _files_lock:
            self._write("layout.json", clean)

    def missing(self, tiles: list[dict], now: datetime | None = None) -> list[dict]:
        """Tiles whose preview is worth fetching now: never tried, or tried and
        refused long enough ago. Private links never."""
        today = (now or datetime.now()).date()
        out = []
        for t in tiles:
            if t["private"] or t.get("status") in ("ok", "gone", "private"):
                continue
            tried = t.get("fetched")
            if tried and (today - datetime.fromisoformat(tried).date()).days < RETRY_DAYS \
                    and t.get("status") != "offline":
                continue
            out.append(t)
        return out

    def fetch(self, tile: dict, getter=preview) -> dict:
        """Fetch one tile's preview and keep it. Runs without any database lock:
        it's slow, and only touches this folder."""
        result = dict(getter(tile["url"]))
        thumb = result.pop("_thumb", None)
        result["fetched"] = datetime.now().strftime("%Y-%m-%d")
        if result.get("status") == "ok" and thumb:
            name = save_thumb(self.thumbs, hashlib.sha1(tile["key"].encode()).hexdigest()[:16], thumb)
            if name:
                result["thumb"] = name
        with _files_lock:
            previews = self._read("previews.json", {})
            previews[tile["key"]] = {k: v for k, v in result.items() if v not in (None, "")}
            self._write("previews.json", previews)
        return previews[tile["key"]]

    def thumb(self, name: str) -> tuple[bytes, str] | None:
        folder = self.thumbs.resolve()
        target = (folder / name).resolve()
        if target.parent != folder or not target.is_file():
            return None
        kinds = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
        kind = kinds.get(target.suffix.lower())
        return (target.read_bytes(), kind) if kind else None
