"""SQLite connection, schema bootstrap, and small shared helpers."""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = "1"

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_now_iso() -> str:
    """Wall-clock time here, no zone -- the same form due and reminder times
    are stored in, so the two compare correctly."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def ulid() -> str:
    """Lexicographically sortable 26-char id: 48-bit ms timestamp + 80 bits random."""
    ms = int(time.time() * 1000)
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    value = (ms << 80) | rand
    out = []
    for _ in range(26):
        out.append(_ULID_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Lowercase ascii slug. Used for topic dirs, filenames and alias keys."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return _SLUG_STRIP.sub("-", text).strip("-") or "untitled"


# Words that carry no topical meaning in a folder name. "Gaming Stuff" and
# "Gaming" must normalise to the same key.
_STOPWORDS = {
    "stuff", "notes", "note", "misc", "miscellaneous", "general", "other",
    "others", "various", "my", "the", "a", "an", "and", "of", "for", "related",
    "things", "thing", "list", "lists", "links", "link", "info", "information",
    "random", "assorted", "collection", "folder",
}

# Longest-first so "ational" wins over "al". Deliberately cruder than Porter:
# consistency between variants matters far more here than linguistic accuracy.
_SUFFIXES = (
    "ational", "ization", "iveness", "fulness", "ousness", "ements", "ities",
    "ement", "ings", "edly", "ies", "ing", "ers", "est", "ment", "ity",
    "er", "es", "ed", "ly", "s",
)


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2]:
        word = word[:-1]
    return word


def normal_key(name: str) -> str:
    """Canonical identity of a topic name, for exact-duplicate detection.

    'Gaming', 'Games', 'Gamer', 'My Games', 'Gaming Stuff' all collapse to the
    same key, so the gate can refuse them without any embedding comparison.
    Order-independent (sorted) so 'AI Notes' == 'Notes AI'.
    """
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    stems = {_stem(w) for w in words if w not in _STOPWORDS}
    stems.discard("")
    return " ".join(sorted(stems)) or "untitled"


def key_similarity(key_a: str, key_b: str) -> float:
    """Jaccard over normalised tokens, treating prefix-matching tokens as equal
    so 'dev'~'develop' and 'tech'~'technology' line up."""
    a, b = set(key_a.split()), set(key_b.split())
    if not a or not b:
        return 0.0

    def matches(x: str, other: set[str]) -> bool:
        return any(x == y or (len(min(x, y, key=len)) >= 3 and
                              (x.startswith(y) or y.startswith(x))) for y in other)

    shared = sum(1 for x in a if matches(x, b))
    union = len(a) + len(b) - shared
    return shared / union if union else 0.0


def key_contains(outer: str, inner: str) -> bool:
    """True when `inner`'s tokens all appear in `outer`, and `outer` says more.

    'Gaming Hardware' (gam hardwar) contains 'Gaming' (gam). That is the shape
    of a SUBTOPIC, not a duplicate -- so the gate nests it under the containing
    topic rather than merging it away or creating a competing root folder.
    """
    outer_tokens, inner_tokens = set(outer.split()), set(inner.split())
    if not inner_tokens or len(outer_tokens) <= len(inner_tokens):
        return False

    def present(token: str) -> bool:
        return any(token == o or (len(min(token, o, key=len)) >= 3 and
                                  (o.startswith(token) or token.startswith(o)))
                   for o in outer_tokens)

    return all(present(t) for t in inner_tokens)


def connect(db_path: str | os.PathLike) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the web server handles requests on a pool of
    # threads. Callers must serialise access themselves -- server.py holds a
    # lock around every engine call.
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    return conn


class transaction:
    """`with transaction(conn):` — commits on success, rolls back on error."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
