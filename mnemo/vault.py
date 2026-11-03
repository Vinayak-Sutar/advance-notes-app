"""The Markdown vault -- the actual source of truth.

Notes are plain .md files with YAML-ish frontmatter, living in directories that
mirror their primary topic. The SQLite index is a rebuildable cache of what is
written here; if the DB is lost, `scan()` reconstructs everything.

Frontmatter is written and parsed by hand (no PyYAML dependency). Only the
subset we emit is supported: scalars, and flow-style lists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .db import now_iso, slugify, ulid

FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)
MAX_FILENAME = 60
MAX_DIRNAME = 48

# Windows refuses these as path components regardless of extension.
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_dirname(name: str) -> str:
    """Filesystem-safe directory component that still *reads* like the topic.

    Unlike `slugify` this keeps case and spaces, so the vault browses as
    `Technology/AI` rather than `technology/ai`. Only genuinely illegal
    characters are stripped.
    """
    cleaned = _ILLEGAL_FS.sub("", name).strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)[:MAX_DIRNAME].rstrip(". ")
    if not cleaned:
        return "Untitled"
    if cleaned.upper() in _WIN_RESERVED or cleaned.split(".")[0].upper() in _WIN_RESERVED:
        cleaned += "_"
    return cleaned


@dataclass
class Note:
    id: str = field(default_factory=ulid)
    title: str = ""
    body: str = ""
    kind: str = "note"
    status: str = "open"
    topics: list[str] = field(default_factory=list)   # slug paths; [0] is primary
    entities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source: str | None = None
    media: str | None = None
    created: str = field(default_factory=now_iso)
    updated: str = field(default_factory=now_iso)
    # Times are local, without a zone ("2026-11-15T18:00:00"), like a clock on
    # the wall. Each is written to the file only when set.
    due: str | None = None          # when it's due; for a to-do list, the deadline
    remind: str | None = None       # when to remind -- apart from due, so a snooze
                                    # moves the reminder and not the deadline
    repeat: str | None = None       # "daily", "weekly", "every monday"...
    checkin: str | None = None      # to-do lists: daily check-in "09:00", or "off"
    quiet_until: str | None = None  # to-do lists: no check-ins before this date
    parent: str | None = None       # to-do lists: id of the group list it sits in
    order: float | None = None      # your own position among notes (tiles)

    @property
    def primary_topic(self) -> str | None:
        return self.topics[0] if self.topics else None


# ------------------------------------------------------- frontmatter ----
# Header fields most notes don't have; written only when set, so older files
# and plain notes keep exactly the header they always had.
OPTIONAL_FIELDS = ("due", "remind", "repeat", "checkin", "quiet_until", "parent", "order")


def _fm_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return '""'
    text = str(value)
    if text == "" or re.search(r"""[:#\[\]{},"']|^\s|\s$""", text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _parse_scalar(raw: str):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if raw in ("true", "false"):
        return raw == "true"
    if raw in ("", "~", "null"):
        return None
    return raw


def _split_flow_list(raw: str) -> list[str]:
    """Split `[a, "b, c", d]` respecting quotes."""
    out, buf, in_quotes, escaped = [], [], False, False
    for ch in raw:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            buf.append(ch)
            escaped = True
        elif ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [v for v in (_parse_scalar(x) for x in out) if v not in (None, "")]


def parse_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    data: dict = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if not key:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            data[key] = _split_flow_list(raw[1:-1])
        else:
            data[key] = _parse_scalar(raw)
    return data, text[match.end():]


def render(note: Note) -> str:
    lines = ["---"]
    lines.append(f"id: {note.id}")
    lines.append(f"title: {_fm_scalar(note.title)}")
    lines.append(f"kind: {note.kind}")
    lines.append(f"status: {note.status}")
    lines.append("topics: [" + ", ".join(_fm_scalar(t) for t in note.topics) + "]")
    lines.append("entities: [" + ", ".join(_fm_scalar(e) for e in note.entities) + "]")
    lines.append("tags: [" + ", ".join(_fm_scalar(t) for t in note.tags) + "]")
    if note.source:
        lines.append(f"source: {_fm_scalar(note.source)}")
    if note.media:
        lines.append(f"media: {_fm_scalar(note.media)}")
    for key in OPTIONAL_FIELDS:
        value = getattr(note, key)
        if value is not None and value != "":
            if key == "order":
                value = f"{float(value):.6f}".rstrip("0").rstrip(".")
            lines.append(f"{key}: {_fm_scalar(value)}")
    lines.append(f"created: {note.created}")
    lines.append(f"updated: {note.updated}")
    lines.append("---")
    lines.append("")
    body = note.body.rstrip()
    return "\n".join(lines) + ("\n" + body + "\n" if body else "\n")


def loads(text: str) -> Note:
    data, body = parse_frontmatter(text)
    return Note(
        id=data.get("id") or ulid(),
        title=data.get("title") or "",
        body=body.strip("\n"),
        kind=data.get("kind") or "note",
        status=data.get("status") or "open",
        topics=list(data.get("topics") or []),
        entities=list(data.get("entities") or []),
        tags=list(data.get("tags") or []),
        source=data.get("source"),
        media=data.get("media"),
        created=data.get("created") or now_iso(),
        updated=data.get("updated") or now_iso(),
        **{key: _optional(key, data.get(key)) for key in OPTIONAL_FIELDS},
    )


def _optional(key: str, value):
    if value in (None, ""):
        return None
    if key == "order":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return str(value)


# ------------------------------------------------- the vault on disk ----
class Vault:
    """Filesystem side. Every write goes through here so path projection and
    the traversal guard are enforced in exactly one place."""

    INBOX = "Inbox"

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".mnemo" / "media").mkdir(parents=True, exist_ok=True)
        (self.root / self.INBOX).mkdir(exist_ok=True)

    # -- paths --
    def abs_path(self, rel: str) -> Path:
        """Resolve a vault-relative path, refusing anything that escapes root."""
        candidate = (self.root / rel).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"path escapes vault root: {rel}")
        return candidate

    def dir_for_topic(self, topic_path: str | None) -> str:
        """'Technology/AI' -> 'Technology/AI', made filesystem-safe but still
        readable. None/'' -> Inbox."""
        if not topic_path:
            return self.INBOX
        segments = [s for s in re.split(r"[/\\]+", topic_path) if s.strip()]
        if not segments:
            return self.INBOX
        return "/".join(safe_dirname(s) for s in segments)

    def path_for(self, note: Note, topic_path: str | None) -> str:
        """Project a note onto a vault-relative file path, avoiding collisions
        with a *different* note that already owns the obvious filename."""
        directory = self.dir_for_topic(topic_path)
        stem = slugify(note.title or note.body[:MAX_FILENAME] or "untitled")[:MAX_FILENAME]
        rel = f"{directory}/{stem}.md"
        if self._owned_by_other(rel, note.id):
            rel = f"{directory}/{stem}-{note.id[-6:].lower()}.md"
        return rel

    def _owned_by_other(self, rel: str, note_id: str) -> bool:
        path = self.abs_path(rel)
        if not path.exists():
            return False
        try:
            return loads(path.read_text(encoding="utf-8")).id != note_id
        except OSError:
            return True

    # -- io --
    def write(self, note: Note, rel: str) -> str:
        path = self.abs_path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(render(note), encoding="utf-8")
        tmp.replace(path)                     # atomic on the same volume
        return rel

    def read(self, rel: str) -> Note:
        return loads(self.abs_path(rel).read_text(encoding="utf-8"))

    def move(self, old_rel: str, new_rel: str) -> str:
        if old_rel == new_rel:
            return new_rel
        src, dst = self.abs_path(old_rel), self.abs_path(new_rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)
        self._prune_empty(src.parent)
        return new_rel

    def delete(self, rel: str) -> None:
        path = self.abs_path(rel)
        if path.exists():
            path.unlink()
            self._prune_empty(path.parent)

    def _prune_empty(self, directory: Path) -> None:
        """Remove now-empty topic dirs, but never the vault root or Inbox."""
        protected = {self.root, self.root / self.INBOX, self.root / ".mnemo"}
        while directory not in protected and self.root in directory.parents:
            try:
                next(directory.iterdir())
                return
            except StopIteration:
                directory.rmdir()
                directory = directory.parent
            except OSError:
                return

    def scan(self):
        """Yield (vault-relative path, Note) for every .md file. Drives rebuild."""
        for path in sorted(self.root.rglob("*.md")):
            # _personal holds imported files, which are not notes (see personal_view.py).
            if ".mnemo" in path.parts or "_personal" in path.parts:
                continue
            try:
                yield path.relative_to(self.root).as_posix(), loads(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError):
                continue
