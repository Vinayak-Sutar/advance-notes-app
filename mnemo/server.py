"""Local HTTP server for the web GUI.

Stdlib `http.server` only -- no Flask, no FastAPI, no build step. That is what
keeps a checkout runnable on any machine with nothing but Python.

Three things here are security, not decoration:

* The socket binds to loopback unless `--lan` is given. Even then, only this
  laptop and devices paired with it get anything but the "connect" page, see
  devices.py. That check runs before anything else reads a request.
* Host and Origin are validated on every request. A loopback bind alone does
  NOT protect a local server: any web page the user visits can issue requests
  to 127.0.0.1, and DNS rebinding lets a hostile page pass a Host check it
  controls. Rejecting unexpected Host/Origin values is what actually closes
  that, and this server can read and write the user's notes.
* Wi-Fi isn't encrypted here, so what a phone must never receive stays laptop
  only: private text, and changes to the setup, see LAPTOP_ONLY.
"""
from __future__ import annotations

import ipaddress
import json
import mimetypes
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import checklist as CL
from . import devices as D
from . import gardener as Gd
from . import notes as N
from . import reminders as Rem
from . import search as S
from . import sections as Sec
from . import topics as T
from . import aisettings as AI
from . import models as M
from . import noteai as NoteAI
from . import profiles as P
from . import qr
from . import websearch as Web
from .chat import Chat
from .engine import Engine
from .paths import DEFAULT_VAULT, IS_WINDOWS, PROFILES_DIR, describe
from .personal_view import Personal

WEB_DIR = Path(__file__).parent / "web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_UPLOAD = 12_000_000
PROFILE_COOKIE = "notes_profile"    # not HttpOnly: the page keys its settings by it
ICONS = {"/static/icon-192.png", "/static/icon-512.png", "/static/apple-touch-icon.png"}
LAPTOP_ONLY = {("GET", "/api/personal/reveal"), ("GET", "/api/connect"),
               ("POST", "/api/ai"), ("POST", "/api/ai/remove"), ("POST", "/api/rebuild"),
               ("POST", "/api/profile/new"), ("POST", "/api/device/remove")}

# Sniff the real type from magic bytes rather than trusting a client-supplied
# name or mime: the extension decides where the file lands on disk.
MAGIC = {
    bytes.fromhex("89504e470d0a1a0a"): "png",
    bytes.fromhex("ffd8ff"): "jpeg",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"BM": "bmp",
}
SUFFIX_FOR = {"png": ".png", "jpeg": ".jpg", "gif": ".gif",
              "bmp": ".bmp", "webp": ".webp"}


def _sniff(blob: bytes) -> str:
    for magic, kind in MAGIC.items():
        if blob.startswith(magic):
            return kind
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return ""


@dataclass
class Room:
    """One open vault: its engine, and the lock that guards its connection."""
    engine: Engine
    lock: threading.Lock = field(default_factory=threading.Lock)
    profile: P.Profile | None = None


_engine: Engine | None = None     # a single vault: `--vault`, and the tests
_lock = threading.Lock()          # the SQLite connection is shared, not reentrant
_rooms: dict[str, Room] = {}      # profile id -> Room; the first is the default
_online: bool | None = None       # how later profiles open their engine
_devices: D.Devices | None = None # only with `--lan`
_chat_model = None                # factory for the chat model; tests swap in a fake
_note_ai_model = None             # same, for the AI box inside a note
_web_searcher = None              # (query, WebConfig) -> results; tests swap in a fake


def _room_for(profile_id: str | None) -> Room:
    """The profile a request belongs to: the one its browser chose, or the first."""
    if profile_id in _rooms:
        return _rooms[profile_id]
    if _rooms:
        return next(iter(_rooms.values()))
    return Room(_engine, _lock)


def _open_engines() -> list[Engine]:
    return [room.engine for room in _rooms.values()] or [_engine]


def _is_local(address: str) -> bool:
    return ipaddress.ip_address(address).is_loopback


def _device_cookie(key: str) -> str:
    return f"{D.COOKIE}={key}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"


def _connect_payload(port: int) -> dict:
    """How a phone connects right now. The address is looked up each time,
    because it can change when the laptop rejoins the Wi-Fi."""
    if _devices is None:
        return {"lan": False}
    address = D.lan_address()
    url = D.pair_url(address, port, _devices.code) if address else None
    return {"lan": True, "address": address, "code": _devices.code, "port": port, "url": url,
            "qr": qr.svg(qr.encode(url)) if url else None, "devices": _devices.listing()}


def _profile_cookie(profile_id: str) -> str:
    return f"{PROFILE_COOKIE}={profile_id}; Path=/; Max-Age=31536000; SameSite=Lax"


def _profiles_payload(room: Room) -> dict:
    return {"profiles": [{"id": r.profile.id, "name": r.profile.name}
                         for r in _rooms.values()],
            "current": room.profile.id if room.profile else None}


# --------------------------------------------------------------- payloads ----
def _subtree_count(conn, topic_id: str) -> int:
    """Notes in this topic and everything under it.

    A parent showing 0 while its children hold notes reads as broken, even
    though `note_count` is correctly the topic's own total.
    """
    ids = T.descendants(conn, topic_id)
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT COUNT(DISTINCT note_id) c FROM note_topics"
        f" WHERE topic_id IN ({placeholders})", ids).fetchone()["c"]


def _topic_node(conn, row) -> dict:
    counts = S.view_counts(conn, row["id"])
    return {
        "id": row["id"],
        "name": row["name"],
        "path": T.path_of(conn, row["id"]),
        "state": row["state"],
        "count": _subtree_count(conn, row["id"]),
        "own": row["note_count"],
        "views": {k: v for k, v in counts.items()
                  if k in ("video", "link", "article", "task", "image", "pending")},
        "children": [_topic_node(conn, child)
                     for child in T.children(conn, row["id"])],
    }


# Markdown syntax reads as noise in a one-line preview ("## Just type ...").
_MD_MARKS = re.compile(r"^\s{0,3}(?:#{1,6}|[-*+]|\d+[.)])\s+|\*\*|__|`", re.MULTILINE)


_SECTION_HEADS = re.compile(
    rf"(?m)^(?:{re.escape(Sec.DESC_MARK)}|{re.escape(Sec.OCR_MARK)})[ \t]*$")


def _snippet(body: str) -> str:
    body = _SECTION_HEADS.sub("", body)
    return " ".join(_MD_MARKS.sub("", body).split())[:220]


def _local_time(value) -> str | None:
    """A time from the page, as local wall-clock "YYYY-MM-DDTHH:MM:SS".
    A zone, if one is sent, is converted to this computer's time."""
    if not value:
        return None
    from datetime import datetime
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not a time: {value!r}")
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)
    return when.strftime("%Y-%m-%dT%H:%M:%S")


def _auto_file(payload: dict) -> bool:
    """Settings > "How much I organise": anything but Automatic means new
    notes wait in Inbox unless the user names a collection."""
    return (payload.get("organise") or "auto") == "auto"


def _kind_of(conn, note_id: str) -> str:
    row = N.get(conn, note_id)
    return row["kind"] if row else "note"


def _preview(user_body: str, limit: int = 6) -> list[dict]:
    """The first few lines of what the user wrote, for a tile. To-do items
    keep their line number so they can be ticked right on the tile."""
    out: list[dict] = []
    in_code = False
    for i, line in enumerate(user_body.split("\n")):
        if CL.FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code or not line.strip():
            continue
        m = CL.CHECK_RE.match(line)
        h = None if m else CL.HEAD_RE.match(line)
        if m:
            text, tok = CL.split_tokens(m.group(4) or "")
            out.append({"t": "check", "line": i, "text": text,
                        "done": m.group(3) != " ", "remind": tok.get("remind")})
        elif h:
            out.append({"t": "head", "text": CL.strip_tokens(h.group(2))})
        else:
            out.append({"t": "text", "text": _MD_MARKS.sub("", line).strip()[:240]})
        if len(out) >= limit:
            break
    return out


def _list_info(conn, row) -> dict:
    """What a to-do list needs beyond a note: its settings, sections with their
    own progress, pace against the deadline, the next item, its child lists."""
    from datetime import datetime
    meta = N.meta(conn, row["id"])
    parsed = CL.parse(_note_parts(row)["body"])
    done, total = parsed.progress()
    children = [r["note_id"] for r in conn.execute(
        "SELECT note_id FROM note_meta WHERE parent = ?", (row["id"],)).fetchall()]
    # A group counts the items of the lists inside it too.
    for child in children:
        crow = N.get(conn, child)
        if crow is not None:
            d, t = CL.progress(_note_parts(crow)["body"])
            done, total = done + d, total + t
    nxt = next((it for it in parsed.items if not it.done), None)
    return {
        "checkin": meta["checkin"], "quietUntil": meta["quiet_until"],
        "repeat": meta["repeat"], "parent": meta["parent"], "children": children,
        "done": done, "total": total,
        "pace": Rem.pace(done, total, row["due_at"], row["created_at"], datetime.now()),
        "next": nxt.text if nxt else None,
        "sections": [{"title": s.title, "line": s.line, "due": s.due,
                      "done": s.progress(parsed.items)[0], "total": s.progress(parsed.items)[1]}
                     for s in parsed.sections],
    }


def _note_brief(conn, row) -> dict:
    user_body = _note_parts(row)["body"]
    done, total = CL.progress(user_body)
    return {
        "id": row["id"],
        "title": row["title"] or "(untitled)",
        "kind": row["kind"],
        "status": row["status"],
        "path": row["path"],
        "topic": T.path_of(conn, N.primary_topic(conn, row["id"])),
        "snippet": _snippet(row["body"] or ""),
        "created": row["created_at"],
        "updated": row["updated_at"],
        "review": bool(row["needs_review"]),
        "due": row["due_at"],
        "remind": bool(row["remind_at"]),
        "done": row["status"] == "done",
        "media": row["media_path"],
        "source": row["source_url"],
        "lines": _preview(user_body),
        "progress": [done, total] if total else None,
    }


def _is_image(row) -> bool:
    return row["kind"] == "image" and bool(row["media_path"])


def _is_link(row) -> bool:
    return bool(row["source_url"]) and not _is_image(row)


def _note_parts(row) -> dict:
    """The note view shows machine-written parts apart from the user's text."""
    body = row["body"] or ""
    if _is_image(row):
        parts = Sec.split_image(body)
        return {"body": parts.user, "description": parts.description,
                "ocr": parts.ocr}
    if _is_link(row):
        parts = Sec.split_link(body)
        return {"body": parts.user, "summary": parts.summary}
    return {"body": body}


def _rejoin(row, user_body: str) -> str:
    """Put an edit of the user's part back together with the rest."""
    body = row["body"] or ""
    if _is_image(row):
        parts = Sec.split_image(body)
        return Sec.join_image(user_body, parts.description, parts.ocr)
    if _is_link(row):
        return Sec.join_link(user_body, Sec.split_link(body).fetched)
    return user_body


def _state(engine: Engine) -> dict:
    conn = engine.conn
    total = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
    active = [r for r in T.all_active(conn) if r["state"] == "active"]
    return {
        "tree": [_topic_node(conn, row) for row in T.children(conn, None)],
        "inbox": len(N.inbox(conn)),
        "review": len(N.needing_review(conn)),
        "notes": total,
        "topics": len(active),
        "embedder": engine.embedder.name,
        "router": engine.provider.name,
        "profile": engine.cfg.name,
        "vault": str(engine.vault.root),
        "coldStart": max(0, engine.cfg.cold_start_notes - total) if not active else 0,
        "embedderChanged": engine.embedder_changed,
        "paths": {**describe(), "vault": str(engine.vault.root)},
    }


# ---------------------------------------------------------------- handler ----
class Handler(BaseHTTPRequestHandler):
    server_version = "Mnemo"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):          # quiet by default
        if self.server.verbose:                 # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # -- safety --
    def _origin_ok(self) -> bool:
        # The address this request arrived on is the only other name for this
        # server. A hostile site's name fails here even if it resolves to us.
        allowed = ALLOWED_HOSTS | {self.connection.getsockname()[0]}
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin:
            hostname = urlparse(origin).hostname
            if hostname not in allowed:
                return False
        return True

    def _cookie(self, name: str) -> str | None:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except CookieError:
            return None
        return jar[name].value if name in jar else None

    def _enter(self) -> None:
        """Settle who is asking, and whose notes they mean, before anything reads them."""
        self.local = _is_local(self.client_address[0])
        self.device = None if self.local or _devices is None \
            else _devices.known(self._cookie(D.COOKIE))
        self.trusted = self.local or self.device is not None
        # The page names the profile it loaded, so a tab left open stays on it
        # after another tab switches this browser. Pictures only have the cookie.
        self.room = _room_for(self.headers.get("X-Notes-Profile")
                              or self._cookie(PROFILE_COOKIE))
        self.engine, self.lock = self.room.engine, self.room.lock

    def _send(self, code: int, body: bytes, content_type: str, headers: tuple = ()) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200, headers: tuple = ()) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json", headers)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- routing --
    def do_GET(self):
        if not self._origin_ok():
            return self._json({"error": "forbidden origin"}, 403)
        self._enter()
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path

        if route == "/pair" or route.startswith("/pair/"):
            return self._pair(route, query)
        if route.startswith("/open/"):
            return self._open_from_home_screen(route[len("/open/"):])
        if route in ICONS:      # phones fetch these on their own, and they hold nothing
            return self._static(route[len("/static/"):])
        if not self.trusted:
            return self._connect_page(route)
        if self._refused_here(route):
            return

        if route in ("/", "/index.html") or route == "/personal-notes" \
                or route.startswith("/personal-notes/"):
            return self._page()                   # the page reads the address itself
        if route == "/manifest.webmanifest":
            return self._manifest()
        if route.startswith("/personal-file/"):
            return self._personal_file(route[len("/personal-file/"):])
        if route.startswith("/static/"):
            return self._static(route[len("/static/"):])
        if route.startswith("/media/"):
            return self._media(route[len("/media/"):])
        if not route.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if route == "/api/ai":              # asks Ollama and the GPU, not the notes
            try:
                return self._json(AI.state())
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        try:
            with self.lock:
                return self._api_get(route, query)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        if not self._origin_ok():
            return self._json({"error": "forbidden origin"}, 403)
        self._enter()
        route = urlparse(self.path).path
        if not self.trusted:
            self.close_connection = True        # its body stays unread
            return self._json({"error": "not connected"}, 401)
        if self._refused_here(route):
            return
        payload = self._body()
        if route == "/api/chat":
            return self._chat(payload)      # takes the lock itself, briefly
        if route in ("/api/ai", "/api/ai/remove"):      # Settings → AI
            try:
                if route == "/api/ai/remove":
                    return self._json(AI.remove(str(payload.get("model") or "")))
                name = str(payload.get("profile") or "auto")
                return self._json(AI.choose(name if name in M.PROFILES else "auto",
                                            _open_engines()))
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        if route == "/api/note/ai":
            return self._note_ai(payload)   # no lock while the model thinks
        if route == "/api/web/test":
            return self._web_test(payload)  # network only, no notes involved
        if route == "/api/profile/new":
            return self._new_profile(payload)
        try:
            with self.lock:
                return self._api_post(route, payload)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    # -- who may do what --
    def _refused_here(self, route: str) -> bool:
        if self.local or (self.command, route) not in LAPTOP_ONLY:
            return False
        self.close_connection = True
        self._json({"error": "Open this on your laptop."}, 403)
        return True

    def _connect_page(self, route: str) -> None:
        """All a device that isn't paired can get."""
        if route.startswith("/api/"):
            return self._json({"error": "not connected"}, 401)
        self._send(401, (WEB_DIR / "connect.html").read_bytes(), "text/html")

    def _pair(self, route: str, query: dict) -> None:
        """Scanning the QR code lands here, as does the typed code."""
        if self.trusted:
            return self._redirect("/")
        if _devices is None:
            return self._connect_page(route)
        code = route[len("/pair/"):] or (query.get("code") or [""])[0]
        key = _devices.pair(unquote(code), self.headers.get("User-Agent") or "")
        if key is None:
            return self._redirect("/?pair=" + ("paused" if _devices.paused() else "wrong"))
        return self._redirect("/", (("Set-Cookie", _device_cookie(key)),))

    def _open_from_home_screen(self, key: str) -> None:
        """An iPhone's Home Screen icon doesn't share Safari's cookies, so each
        paired device's icon carries its key and hands it over here."""
        if _devices is None or not _devices.known(key):
            return self._redirect("/")
        return self._redirect("/", (("Set-Cookie", _device_cookie(key)),))

    def _manifest(self) -> None:
        key = self._cookie(D.COOKIE) if self.device else None
        icons = [{"src": f"/static/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png"}
                 for n in (192, 512)]
        self._send(200, json.dumps({
            "name": "Notes", "short_name": "Notes", "display": "standalone", "scope": "/",
            "start_url": f"/open/{key}" if key else "/",
            "background_color": "#F7F5F0", "theme_color": "#F7F5F0", "icons": icons,
        }).encode(), "application/manifest+json")

    def _redirect(self, target: str, headers: tuple = ()) -> None:
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()

    # -- chat --
    def _chat(self, payload: dict) -> None:
        """Stream an answer as newline-delimited JSON, one event per line.

        The lock is held only while the chat reads or changes notes, never
        while the model is talking, so the rest of the app stays responsive
        during a long answer. Chunked encoding keeps HTTP/1.1 framing intact.
        """
        history = payload.get("history")
        events = Chat(self.engine, self.lock, model=_chat_model() if _chat_model else None,
                      json_model=_note_ai_model() if _note_ai_model else None,
                      searcher=_web_searcher,
                      think=bool(payload.get("think")) or M.ACTIVE.thinks).events(
            str(payload.get("message") or ""),
            history if isinstance(history, list) else [],
            organise=str(payload.get("organise") or "auto"),
            web=Web.WebConfig.from_payload(payload.get("web")))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            for event in events:
                self._write_chunk((json.dumps(event) + "\n").encode())
            self._write_chunk(b"")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True    # the user pressed Stop
        finally:
            events.close()                  # stops the model mid-answer

    def _note_ai(self, payload: dict) -> None:
        """Answer, or say how to change the note. The page applies any change
        to what it is showing and saves it like a normal edit."""
        title, body = payload.get("title"), payload.get("body")
        if body is None:
            with self.lock:
                row = N.get(self.engine.conn, payload.get("id") or "")
                if row is None:
                    return self._json({"error": "not found"}, 404)
                title, body = row["title"], _note_parts(row)["body"]
        history = payload.get("history")
        cursor = payload.get("cursorLine")
        try:
            result = NoteAI.respond(
                str(title or ""), str(body or ""), str(payload.get("request") or ""),
                history if isinstance(history, list) else [],
                cursor_line=cursor if isinstance(cursor, str) else None,
                model=_note_ai_model() if _note_ai_model else None,
                web=Web.WebConfig.from_payload(payload.get("web")),
                searcher=_web_searcher)
        except NoteAI.ModelUnavailable:
            return self._json({"error": "The local AI isn't running."}, 503)
        except Exception as exc:
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json(result)

    def _new_profile(self, payload: dict) -> None:
        """A new, separate set of notes, which this browser then opens."""
        if not _rooms:
            return self._json({"error": "This copy runs a single vault."}, 409)
        try:
            profile = P.create(str(payload.get("name") or "")[:40], online=_online)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 409)
        _rooms[profile.id] = Room(Engine(profile.path, online=_online), profile=profile)
        return self._json(_profiles_payload(_rooms[profile.id]),
                          headers=(("Set-Cookie", _profile_cookie(profile.id)),))

    def _web_test(self, payload: dict) -> None:
        """Settings > Web search > Test: one real search with the chosen setup."""
        cfg = Web.WebConfig.from_payload(payload.get("web"))
        query = str(payload.get("q") or "Wikipedia")
        try:
            results = (_web_searcher or Web.search)(query, cfg)
        except Web.SearchError as exc:
            return self._json({"ok": False, "error": str(exc)})
        except Exception as exc:
            return self._json({"ok": False, "error": type(exc).__name__})
        return self._json({"ok": bool(results), "count": len(results),
                           "results": [r.brief() for r in results[:3]],
                           "error": None if results else "No results came back."})

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    # -- api --
    def _api_get(self, route: str, query: dict):
        engine = self.engine
        conn = engine.conn

        if route == "/api/state":
            return self._json({**_state(engine), "laptop": self.local})

        if route == "/api/connect":
            return self._json(_connect_payload(self.server.server_address[1]))

        if route == "/api/profiles":
            return self._json(_profiles_payload(self.room))

        if route == "/api/notes":
            topic_path = (query.get("topic") or [None])[0]
            kind = (query.get("kind") or [None])[0]
            status = (query.get("status") or [None])[0]
            if topic_path:
                row = T.by_path(conn, topic_path)
                if row is None:
                    return self._json({"notes": []})
                rows = S.topic_view(conn, row["id"], kind=kind, status=status)
            elif kind:
                rows = conn.execute(
                    f"SELECT n.* FROM notes n WHERE n.kind = ? ORDER BY {N.ORDER}",
                    (kind,)).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT n.* FROM notes n ORDER BY {N.ORDER} LIMIT 300").fetchall()
            return self._json({"notes": [_note_brief(conn, r) for r in rows]})

        if route == "/api/inbox":
            return self._json({
                "notes": [_note_brief(conn, r) for r in N.inbox(conn)],
                "review": [_note_brief(conn, r) for r in N.needing_review(conn)],
            })

        if route == "/api/note":
            note_id = (query.get("id") or [""])[0]
            row = N.get(conn, note_id)
            if row is None:
                return self._json({"error": "not found"}, 404)
            brief = _note_brief(conn, row)
            brief.update(_note_parts(row))
            brief["source"] = row["source_url"]
            brief["related"] = [_note_brief(conn, r) for r in N.related(conn, note_id)]
            brief["topics"] = [T.path_of(conn, t) for t in N.topics_of(conn, note_id)]
            if row["kind"] == "list" or brief.get("progress"):
                brief["list"] = _list_info(conn, row)
            return self._json(brief)

        if route == "/api/personal":
            return self._json(Personal(engine.vault.root).overview())

        if route == "/api/personal/messages":
            data = Personal(engine.vault.root).messages((query.get("source") or [""])[0])
            return self._json(data) if data else self._json({"error": "not found"}, 404)

        if route == "/api/personal/reveal":
            text = Personal(engine.vault.root).reveal((query.get("id") or [""])[0])
            return self._json({"text": text}) if text is not None \
                else self._json({"error": "not found"}, 404)

        if route == "/api/lists":
            rows = N.lists(conn)
            return self._json({"lists": [dict(_note_brief(conn, r), list=_list_info(conn, r))
                                         for r in rows]})

        if route == "/api/search":
            q = (query.get("q") or [""])[0].strip()
            if not q:
                return self._json({"hits": []})
            hits = S.search(conn, q, engine._embed_one(q), limit=40)
            return self._json({"hits": [
                {"id": h.note_id, "title": h.title, "topic": h.topic_path,
                 "kind": _kind_of(conn, h.note_id),
                 "snippet": _snippet(h.snippet), "via": h.via, "score": h.score}
                for h in hits]})

        if route == "/api/suggest":
            # Where WOULD this note go? Used to turn Inbox from a list into a
            # one-click triage queue: the work is picking, not typing a path.
            note_id = (query.get("id") or [""])[0]
            vec = N.vector(conn, note_id)
            if not vec:
                return self._json({"suggestions": []})
            from . import router as R
            candidates, _ = R.retrieve(conn, vec, engine.cfg, exclude_note=note_id)
            return self._json({"suggestions": [
                {"path": c.path, "similarity": round(c.similarity, 3),
                 "count": c.note_count}
                for c in candidates[:3] if c.similarity > 0]})

        if route == "/api/agenda":
            rows = N.agenda(conn)
            anytime = conn.execute(
                "SELECT n.* FROM notes n WHERE n.kind = 'task' AND n.due_at IS NULL AND n.status = 'open'"
                f" ORDER BY {N.ORDER}").fetchall()
            return self._json({"notes": [_note_brief(conn, r) for r in rows],
                               "items": Rem.agenda_items(conn), "lists": Rem.active_lists(conn),
                               "anytime": [_note_brief(conn, r) for r in anytime]})

        if route == "/api/due":
            # Polled by the page so a reminder can surface while it is open.
            # Reminder times are wall-clock local time, so compare with local
            # now -- UTC here made reminders fire hours late east of Greenwich.
            reminders = Rem.due_now(conn)
            for r in reminders:
                if r["type"] == "task":
                    r["topic"] = T.path_of(conn, N.primary_topic(conn, r["noteId"]))
            return self._json({"reminders": reminders})

        if route == "/api/resolve":
            phrase = (query.get("q") or [""])[0]
            return self._json({"matches": engine.resolve_note(phrase)
                               if phrase else []})

        if route == "/api/garden":
            # Only suggestions the app can carry out are offered, in plain words.
            out = []
            for p in Gd.scan(conn, engine.cfg):
                words = Gd.describe(conn, p.kind, p.payload)
                if words:
                    out.append({"id": p.id, "kind": p.kind, "summary": words[0],
                                "detail": words[1], "score": min(1.0, p.score)})
            return self._json({"proposals": out})

        if route == "/api/graph":
            return self._json(_graph(conn))

        return self._json({"error": "not found"}, 404)

    def _api_post(self, route: str, payload: dict):
        engine = self.engine
        conn = engine.conn

        if route == "/api/device/remove":
            if _devices is None or not _devices.remove(str(payload.get("id") or "")):
                return self._json({"error": "not found"}, 404)
            return self._json({"devices": _devices.listing()})

        if route == "/api/profile":
            # This browser only: another device keeps the one it chose.
            profile_id = str(payload.get("id") or "")
            if profile_id not in _rooms:
                return self._json({"error": "not found"}, 404)
            return self._json({"ok": True},
                              headers=(("Set-Cookie", _profile_cookie(profile_id)),))

        if route == "/api/capture":
            text = (payload.get("text") or "").strip()
            if not text:
                return self._json({"error": "empty"}, 400)
            result = engine.capture(text, auto_file=_auto_file(payload))
            return self._json({
                "id": result.note.id,
                "title": result.note.title,
                "body": result.note.body,
                "kind": result.note.kind,
                "due": result.due_at,
                "reminder": result.reminder,
                "recurrence": result.recurrence,
                "appendedTo": result.note.title if result.appended_to else None,
                "listItems": result.list_items,
                "pausedUntil": result.paused_until,
                "appendCandidates": result.append_candidates,
                "matched": result.matched,
                "summary": result.summary(),
                "topic": result.topic_path,
                "path": result.path,
                "outcome": result.outcome,
                "related": len(result.related),
                "steered": result.steered,
                "bootstrapped": len(result.bootstrapped),
                "promoted": [T.get(engine.conn, t)["name"] for t in result.promoted],
            })

        if route == "/api/move":
            note_id = payload.get("id")
            topic = (payload.get("topic") or "").strip()
            if not note_id or not topic:
                return self._json({"error": "id and topic required"}, 400)
            return self._json({"path": engine.move_note(note_id, topic)})

        if route == "/api/note/update":
            row = N.get(conn, payload.get("id") or "")
            if row is None:
                return self._json({"error": "not found"}, 404)
            body = payload.get("body")
            if body is not None:
                body = _rejoin(row, body)
            return self._json(engine.update_note(
                payload["id"], body=body, title=payload.get("title")))

        if route == "/api/note/append":
            return self._json(engine.append_to_note(payload["id"],
                                                    payload.get("text", "")))

        if route == "/api/note/schedule":
            if N.get(conn, payload.get("id") or "") is None:
                return self._json({"error": "not found"}, 404)
            engine.schedule(payload["id"], _local_time(payload.get("when")),
                            remind=payload.get("remind", True),
                            snooze=bool(payload.get("snooze")))
            return self._json({"ok": True})

        if route == "/api/list/item":
            # Tick or untick one to-do line: {id, line, text, done}. The text
            # guards against a page that's a moment out of date.
            row = N.get(conn, payload.get("id") or "")
            if row is None:
                return self._json({"error": "not found"}, 404)
            user = _note_parts(row)["body"]
            try:
                user = CL.toggle(user, payload.get("line"), payload.get("text") or "",
                                 payload.get("done"))
            except LookupError:
                return self._json({"error": "That item changed. Open the note to see it."}, 409)
            engine.update_note(row["id"], body=_rejoin(row, user))
            done, total = CL.progress(user)
            return self._json({"ok": True, "progress": [done, total]})

        if route == "/api/personal/layout":
            order = payload.get("order")
            return self._json(Personal(engine.vault.root).set_layout(
                str(payload.get("key") or ""), order if isinstance(order, list) else []))

        if route == "/api/list/settings":
            # {id, due?, checkin?, quiet_until?, repeat?, parent?, kind?}
            if N.get(conn, payload.get("id") or "") is None:
                return self._json({"error": "not found"}, 404)
            fields = {k: payload[k] for k in ("due", "checkin", "quiet_until", "repeat",
                                              "parent", "kind") if k in payload}
            if fields.get("due"):
                fields["due"] = _local_time(fields["due"])
            engine.set_list(payload["id"], **fields)
            return self._json({"ok": True})

        if route == "/api/list/item/when":
            # {id, line, text, remind?, due?, repeat?} -- None clears each one.
            row = N.get(conn, payload.get("id") or "")
            if row is None:
                return self._json({"error": "not found"}, 404)
            try:
                user = CL.set_when(_note_parts(row)["body"], payload.get("line"),
                                   payload.get("text") or "",
                                   remind=_local_time(payload.get("remind")),
                                   due=payload.get("due"), repeat=payload.get("repeat"))
            except LookupError:
                return self._json({"error": "That item changed. Open the note to see it."}, 409)
            engine.update_note(row["id"], body=_rejoin(row, user))
            return self._json({"ok": True})

        if route == "/api/note/order":
            # A tile dropped between two others: {id, before, after, view}.
            if N.get(conn, payload.get("id") or "") is None:
                return self._json({"error": "not found"}, 404)
            pos = engine.set_order(payload["id"], before=payload.get("before"),
                                   after=payload.get("after"),
                                   view=payload.get("view") or None)
            return self._json({"ok": True, "pos": pos})

        if route == "/api/note/complete":
            if N.get(conn, payload.get("id") or "") is None:
                return self._json({"error": "not found"}, 404)
            engine.complete(payload["id"], payload.get("done", True))
            return self._json({"ok": True})

        if route == "/api/note/new":
            return self._json(engine.create_note(
                title=payload.get("title") or "Untitled",
                body=payload.get("body") or "",
                topic_path=(payload.get("topic") or "").strip() or None,
                kind="list" if payload.get("kind") == "list" else "note"))

        if route == "/api/note/file":
            if N.get(conn, payload.get("id") or "") is None:
                return self._json({"error": "not found"}, 404)
            return self._json(engine.auto_file(payload["id"]))

        if route == "/api/note/delete":
            if N.get(conn, payload.get("id") or "") is None:
                return self._json({"error": "not found"}, 404)
            return self._json(engine.delete_note(payload["id"]))

        if route == "/api/reminded":
            # {keys}: what the page has just shown, so none of it fires twice.
            Rem.mark(conn, [k for k in payload.get("keys") or [] if isinstance(k, str)])
            for note_id in payload.get("ids") or []:
                N.mark_reminded(conn, note_id)
            return self._json({"ok": True})

        if route == "/api/upload":
            # Base64 in JSON rather than multipart: the payload is one image
            # from a paste or a drop, and stdlib multipart parsing is far more
            # code than it is worth for a single field.
            import base64, tempfile
            raw = payload.get("data") or ""
            if "," in raw[:64]:                 # strip a data: URL prefix
                raw = raw.split(",", 1)[1]
            try:
                blob = base64.b64decode(raw, validate=True)
            except Exception:
                return self._json({"error": "not valid base64"}, 400)
            if not blob:
                return self._json({"error": "empty image"}, 400)
            if len(blob) > MAX_UPLOAD:
                return self._json(
                    {"error": f"image is {len(blob)//1_000_000} MB; "
                              f"limit is {MAX_UPLOAD//1_000_000} MB"}, 413)

            suffix = SUFFIX_FOR.get(_sniff(blob), "")
            if not suffix:
                return self._json({"error": "unsupported image type"}, 415)

            tmp = Path(tempfile.gettempdir()) / f"mnemo-upload{suffix}"
            tmp.write_bytes(blob)
            try:
                result = engine.capture_image(tmp, payload.get("text", ""),
                                              auto_file=_auto_file(payload))
            finally:
                tmp.unlink(missing_ok=True)
            return self._json({
                "id": result.note.id,
                "title": result.note.title,
                "topic": result.topic_path,
                "outcome": result.outcome,
                "media": result.note.media,
                "ocr": result.ocr,
                "description": result.description,
                "error": None,
                "enrichError": result.enrich_error,
            })

        if route == "/api/note/discard":
            return self._json({"discarded": engine.discard_if_blank(payload.get("id") or "")})

        if route == "/api/garden/apply":
            try:
                return self._json(engine.apply_proposal(payload.get("id") or ""))
            except KeyError:
                return self._json({"error": "That suggestion is out of date."}, 404)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 409)

        if route == "/api/garden/dismiss":
            Gd.dismiss(conn, payload.get("id") or "")
            return self._json({"ok": True})

        if route == "/api/undo":
            record = engine.undo()
            return self._json({"undone": record["op"] if record else None})

        if route == "/api/rebuild":
            return self._json(engine.rebuild())

        return self._json({"error": "not found"}, 404)

    # -- media --
    def _media(self, name: str):
        """Serve an image out of the vault's media store."""
        root = (self.engine.vault.root / ".mnemo" / "media").resolve()
        target = (root / name).resolve()
        if root not in target.parents or not target.is_file():
            return self._json({"error": "not found"}, 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def _personal_file(self, rest: str):
        """An attachment or thumbnail from Personal notes."""
        source, _, name = unquote(rest).partition("/")
        got = Personal(self.engine.vault.root).file(source, name)
        if got is None:
            return self._json({"error": "not found"}, 404)
        body, ctype, inline = got
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", "inline" if inline else "attachment")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("Vary", "Cookie")      # the same address in each profile
        self.end_headers()
        self.wfile.write(body)

    # -- static --
    def _page(self):
        """The app. A browser with no profile yet is given the default, so the
        page always knows which profile its saved settings belong to."""
        profile = self.room.profile
        chosen = profile and self._cookie(PROFILE_COOKIE) == profile.id
        headers = () if not profile or chosen else (("Set-Cookie", _profile_cookie(profile.id)),)
        if self.device:         # in use, so it shouldn't run out
            headers += (("Set-Cookie", _device_cookie(self._cookie(D.COOKIE))),)
        self._send(200, (WEB_DIR / "app.html").read_bytes(), "text/html", headers)

    def _static(self, name: str):
        target = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            return self._json({"error": "not found"}, 404)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), content_type)


def _graph(conn) -> dict:
    """Topic and note nodes plus their edges, for the canvas view."""
    nodes, links = [], []
    for row in T.all_active(conn):
        nodes.append({"id": row["id"], "label": row["name"], "type": "topic",
                      "path": T.path_of(conn, row["id"]),
                      "size": _subtree_count(conn, row["id"]),
                      "state": row["state"]})
        if row["parent_id"]:
            links.append({"source": row["parent_id"], "target": row["id"],
                          "type": "parent"})
    for row in conn.execute(
            "SELECT id, title FROM notes ORDER BY created_at DESC LIMIT 400"):
        topic = N.primary_topic(conn, row["id"])
        nodes.append({"id": row["id"], "label": row["title"] or "(untitled)",
                      "type": "note", "size": 1,
                      "topic": T.path_of(conn, topic) if topic else None})
        if topic:
            links.append({"source": topic, "target": row["id"], "type": "about"})
    known = {n["id"] for n in nodes}
    for row in conn.execute(
            "SELECT src, dst, weight FROM edges WHERE rel = 'related' LIMIT 600"):
        if row["src"] in known and row["dst"] in known:
            links.append({"source": row["src"], "target": row["dst"],
                          "type": "related", "weight": row["weight"]})
    return {"nodes": nodes, "links": links}


class _Server(ThreadingHTTPServer):
    # On Windows this flag lets a second copy share the port, and an old one
    # then quietly keeps answering. Elsewhere it only makes restarts quicker.
    allow_reuse_address = not IS_WINDOWS
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Phones drop connections all the time (screen off, Wi-Fi switching).
        # That isn't a fault, and a traceback for each would push the QR code
        # off the window.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _daily_backup(profile: P.Profile) -> None:
    """In the background, so a big vault doesn't slow the start."""
    try:
        P.backup(profile)
    except (OSError, ValueError) as exc:
        print(f"  WARNING: backup of {profile.name} failed: {exc}")


def serve(vault: str | Path = DEFAULT_VAULT, port: int = 7777, host: str = "127.0.0.1",
          open_browser: bool = True, online: bool | None = None,
          verbose: bool = False, profiles: list[P.Profile] | None = None,
          lan: bool = False) -> None:
    global _engine, _online, _devices
    try:
        httpd = _Server(("0.0.0.0" if lan else host, port), Handler)
    except OSError:
        print(f"  Port {port} is in use, so Notes is probably already running.")
        return
    httpd.verbose = verbose                      # type: ignore[attr-defined]
    _online = online
    _devices = D.Devices(PROFILES_DIR / "devices.json") if lan else None
    for profile in profiles or []:
        _rooms[profile.id] = Room(Engine(profile.path, online=online), profile=profile)
    _engine = next(iter(_rooms.values())).engine if _rooms else Engine(vault, online=online)
    if profiles:
        threading.Thread(target=_daily_backup, args=(profiles[0],), daemon=True).start()

    url = f"http://{'127.0.0.1' if lan else host}:{port}/"
    print(f"  mnemo  {url}")
    print(f"  vault  {_engine.vault.root}")
    if _rooms:
        print(f"  profiles  {', '.join(r.profile.name for r in _rooms.values())}")
    print(f"  stack  {_engine.embedder.name} / {_engine.provider.name}")
    for engine in _open_engines():
        if engine.embedder_changed:
            print(f"  WARNING: {engine.vault.root.name} was indexed with "
                  f"{engine.embedder_changed!r}; run rebuild")
    if _devices:
        D.show(D.banner(port, D.lan_address(), _devices.code))
    print("  ctrl-c to stop")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()
        for engine in _open_engines():
            engine.close()
