"""Turning a pasted link or image into text the router can actually reason about.

A bare URL is nearly signal-free: routing "https://youtu.be/dQw4w9WgXcQ" on the
characters in the URL is hopeless. Fetching the title and article text is what
makes a link route as well as a typed note. Same for images -- a screenshot is
opaque until a vision model describes it.

Stdlib only: `html.parser` instead of BeautifulSoup, `urllib` instead of
requests. Every function degrades to "return what we already knew" on failure,
because enrichment must never be able to lose a capture.
"""
from __future__ import annotations

import base64
import gzip
import platform
import html
import io
import re
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (compatible; Mnemo/0.1; +local note capture)"
TIMEOUT = 15
MAX_BYTES = 2_000_000
MAX_TEXT = 8000

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")
IS_WINDOWS = platform.system() == "Windows"

# Nothing inside these contributes to what a page is about.
_SKIP_TAGS = {"script", "style", "nav", "footer", "header", "aside", "noscript",
              "form", "svg", "iframe", "button"}
_BLOCK_TAGS = {"p", "div", "section", "article", "li", "br", "h1", "h2", "h3",
               "h4", "h5", "h6", "tr", "blockquote"}


@dataclass
class Enrichment:
    title: str = ""
    text: str = ""
    kind: str = "link"
    site: str = ""
    author: str = ""
    ok: bool = False
    error: str = ""
    meta: dict = field(default_factory=dict)


class _Extractor(HTMLParser):
    """Minimal readability: drop chrome, keep block text, remember metadata."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.meta: dict[str, str] = {}
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            attributes = dict(attrs)
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content") or ""
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        elif data.strip():
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        joined = re.sub(r"\n\s*\n+", "\n\n", joined)
        return joined.strip()


def _decode(raw: bytes, encoding_header: str, charset: str | None) -> str:
    if encoding_header == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    elif encoding_header == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
    for candidate in (charset, "utf-8", "cp1252", "latin-1"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


_ssl_context = None


def _tls_context():
    """A TLS context that still works on a machine with a stale cert store.

    Windows ships expired roots (13 on the reference machine: old Thawte,
    Baltimore CyberTrust, a retired Microsoft root). OpenSSL 1.1.1 picks one of
    those as the chain anchor and fails with "certificate has expired" even
    though a perfectly valid shorter chain exists -- so Wikipedia and Hacker
    News were unreachable while example.com worked fine.

    The default context is tried first and used whenever it works. Only if it
    does not do we rebuild trust from the Windows store minus anything already
    expired. Verification stays fully on either way; nothing here weakens it.
    """
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context

    import ssl

    default = ssl.create_default_context()
    if not IS_WINDOWS or _tls_works(default):
        _ssl_context = default
        return _ssl_context

    from datetime import datetime

    now = datetime.utcnow()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    kept = 0
    try:
        for der, _enc, _trust in ssl.enum_certificates("ROOT"):
            probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            try:
                probe.load_verify_locations(cadata=der)
                info = probe.get_ca_certs()[0]
                if datetime.strptime(info["notAfter"], "%b %d %H:%M:%S %Y %Z") <= now:
                    continue
                ctx.load_verify_locations(cadata=der)
                kept += 1
            except Exception:
                continue
    except Exception:
        _ssl_context = default
        return _ssl_context

    _ssl_context = ctx if kept else default
    return _ssl_context


def _tls_works(ctx, host: str = "en.wikipedia.org") -> bool:
    import socket
    try:
        with socket.create_connection((host, 443), timeout=6) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return True
    except Exception:
        return False


def fetch_url(url: str, timeout: int = TIMEOUT) -> Enrichment:
    """Retrieve a page and reduce it to title + readable text."""
    result = Enrichment(kind=_kind_for_url(url))
    result.site = re.sub(r"^www\.", "", (re.match(r"https?://([^/]+)", url) or
                                         re.match(r"()", url)).group(1) or "")
    try:
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en",
        })
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=_tls_context()) as response:
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset()
            encoding = (response.headers.get("Content-Encoding") or "").lower()
            raw = response.read(MAX_BYTES)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        result.error = str(exc)[:200]
        return result

    if content_type.startswith("image/"):
        result.kind = "image"
        result.ok = True
        return result
    if not content_type.startswith(("text/", "application/xhtml")):
        result.error = f"unsupported content type {content_type}"
        return result

    parser = _Extractor()
    try:
        parser.feed(_decode(raw, encoding, charset))
    except Exception as exc:                       # malformed HTML is common
        result.error = f"parse: {exc}"[:200]

    meta = parser.meta
    result.meta = meta
    result.title = html.unescape(
        meta.get("og:title") or meta.get("twitter:title") or parser.title or ""
    ).strip()
    description = html.unescape(
        meta.get("og:description") or meta.get("description") or ""
    ).strip()
    result.author = meta.get("author") or meta.get("article:author") or ""
    result.site = meta.get("og:site_name") or result.site

    body = parser.text()
    # The description is usually a hand-written summary, so it earns its place
    # at the front of the text the router sees.
    result.text = "\n\n".join(part for part in (description, body) if part)[:MAX_TEXT]
    result.ok = bool(result.title or result.text)
    if not result.ok and not result.error:
        result.error = "no readable content"
    return result


def _kind_for_url(url: str) -> str:
    lowered = url.lower()
    if any(host in lowered for host in VIDEO_HOSTS):
        return "video"
    if any(lowered.split("?")[0].endswith(ext) for ext in IMAGE_SUFFIXES):
        return "image"
    return "link"


# ----------------------------------------------------------------- images ----
DESCRIBE_PROMPT = (
    "Describe this image for a note-taking index. One line naming what it is, "
    "then the concrete things visible: product or software names, people, "
    "places, subject matter. Be specific and factual. No preamble."
)

# Transcription is asked for SEPARATELY from description. Asking one prompt to
# both summarise and transcribe reliably produces a summary with a few quoted
# fragments -- the model treats the text as something to describe rather than
# something to copy. Two focused calls beat one mixed one.
OCR_PROMPT = (
    "Transcribe every piece of text visible in this image, exactly as written. "
    "Preserve line breaks and reading order. Include labels, buttons, menu "
    "items, headings, numbers and code. Do not summarise, translate, explain "
    "or add commentary. If there is no text, reply with exactly: NO_TEXT"
)


def _vision_call(data: bytes, prompt: str, model: str, host: str,
                 timeout: int) -> tuple[str, str]:
    """One image+prompt call. Returns (text, error)."""
    import json

    from .runtime import think_option

    payload = {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(data).decode()],
        "stream": False,
        "keep_alive": -1,
        "options": {"temperature": 0.0},
        **think_option(model, False),       # describing a picture needs no essay
    }
    try:
        request = urllib.request.Request(
            f"{host}/api/generate", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except Exception as exc:
        return "", repr(exc)[:200]
    return (body.get("response") or "").strip(), ""


def read_image(path: str | Path, model: str | None = None,
               host: str | None = None, timeout: int = 240,
               describe: bool = True, ocr: bool = True) -> Enrichment:
    """Describe an image and transcribe any text in it.

    Both halves matter for different reasons. The description is what makes an
    image routable at all -- a screenshot is otherwise opaque to every
    similarity comparison in the system. The transcription is what makes it
    *findable*: searching for an error message you saw in a screenshot only
    works if those exact words are in the note.
    """
    from .models import VISION_MODEL
    from .paths import OLLAMA_HOST

    model = model or VISION_MODEL
    host = (host or OLLAMA_HOST).rstrip("/")
    result = Enrichment(kind="image")

    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        result.error = str(exc)[:200]
        return result

    description = transcript = ""
    errors = []

    if describe:
        description, err = _vision_call(data, DESCRIBE_PROMPT, model, host, timeout)
        if err:
            errors.append(f"describe: {err}")

    if ocr:
        transcript, err = _vision_call(data, OCR_PROMPT, model, host, timeout)
        if err:
            errors.append(f"ocr: {err}")
        if transcript.strip().upper().startswith("NO_TEXT"):
            transcript = ""

    parts = []
    if description:
        parts.append(description)
    if transcript:
        # Labelled so it is obvious in the note which part the model read off
        # the image rather than inferred about it.
        parts.append("**Text in image**\n\n" + transcript)

    if not parts:
        result.error = "; ".join(errors) or "no output"
        return result

    result.text = "\n\n".join(parts)[:MAX_TEXT]
    result.meta = {"description": description, "transcript": transcript}
    result.title = (description.splitlines()[0] if description
                    else transcript.splitlines()[0])[:70].rstrip(".,")
    result.ok = True
    if errors:
        result.error = "; ".join(errors)
    return result


def caption_image(path: str | Path, model: str | None = None,
                  host: str | None = None, timeout: int = 240) -> Enrichment:
    """Backwards-compatible alias for `read_image`."""
    return read_image(path, model=model, host=host, timeout=timeout)


def is_image_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES
