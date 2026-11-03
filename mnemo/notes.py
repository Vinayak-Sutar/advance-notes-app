"""Note rows, their embeddings, and topic assignments."""
from __future__ import annotations

import re
import sqlite3

from .checklist import semantic_sig
from .db import now_iso
from .vault import Note
from .vecs import Vector, pack, unpack


def upsert(conn: sqlite3.Connection, note: Note, path: str | None,
           needs_review: bool = False) -> None:
    """Index `note` exactly as its file says. Due, reminder and done live in
    the file, so the file is the one place to change them; a reminder that
    moves is armed again."""
    conn.execute(
        """
        INSERT INTO notes(id, title, body, kind, status, path, source_url, media_path,
                          content_hash, needs_review, due_at, remind_at,
                          created_at, updated_at)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, body=excluded.body, kind=excluded.kind,
            status=excluded.status, path=excluded.path,
            source_url=excluded.source_url, media_path=excluded.media_path,
            content_hash=excluded.content_hash,
            needs_review=excluded.needs_review,
            due_at=excluded.due_at,
            reminded=CASE WHEN excluded.remind_at IS notes.remind_at
                          THEN notes.reminded ELSE 0 END,
            remind_at=excluded.remind_at,
            created_at=excluded.created_at,
            updated_at=excluded.updated_at
        """,
        (note.id, note.title, note.body, note.kind, note.status, path, note.source,
         note.media, semantic_sig(note.title, note.body), int(needs_review),
         note.due, note.remind, note.created, note.updated or now_iso()),
    )
    _sync_order(conn, note)
    _sync_meta(conn, note)


# ------------------------------------------------------------- reminders ----
def due_soon(conn: sqlite3.Connection, before_iso: str) -> list[sqlite3.Row]:
    """Reminders whose time has arrived and that have not fired yet."""
    return conn.execute(
        "SELECT * FROM notes WHERE remind_at IS NOT NULL AND reminded = 0"
        " AND status = 'open' AND remind_at <= ? ORDER BY remind_at",
        (before_iso,),
    ).fetchall()


def mark_reminded(conn: sqlite3.Connection, note_id: str) -> None:
    conn.execute("UPDATE notes SET reminded = 1 WHERE id = ?", (note_id,))


def agenda(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Everything with a time on it, soonest first -- drives the Today view."""
    return conn.execute(
        "SELECT * FROM notes WHERE due_at IS NOT NULL AND status <> 'archived'"
        " ORDER BY (status = 'done'), due_at"
    ).fetchall()


def find_by_title(conn: sqlite3.Connection, phrase: str,
                  limit: int = 5) -> list[sqlite3.Row]:
    """Loose title match, for 'add this to the elden ring note'."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", phrase.lower()) if len(w) > 1]
    if not words:
        return []
    clause = " AND ".join("LOWER(title) LIKE ?" for _ in words)
    rows = conn.execute(
        f"SELECT * FROM notes WHERE {clause} ORDER BY LENGTH(title) LIMIT ?",
        [f"%{w}%" for w in words] + [limit],
    ).fetchall()
    return rows


def get(conn: sqlite3.Connection, note_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()


def delete(conn: sqlite3.Connection, note_id: str) -> None:
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))


def set_vector(conn: sqlite3.Connection, note_id: str, vec: Vector, model: str) -> None:
    conn.execute(
        "INSERT INTO note_vec(note_id, embedding, dim, model) VALUES(?,?,?,?)"
        " ON CONFLICT(note_id) DO UPDATE SET embedding=excluded.embedding,"
        " dim=excluded.dim, model=excluded.model",
        (note_id, pack(vec), len(vec), model),
    )


def has_vector(conn: sqlite3.Connection, note_id: str) -> bool:
    return conn.execute("SELECT 1 FROM note_vec WHERE note_id = ?",
                        (note_id,)).fetchone() is not None


def vector(conn: sqlite3.Connection, note_id: str) -> Vector:
    row = conn.execute(
        "SELECT embedding FROM note_vec WHERE note_id = ?", (note_id,)
    ).fetchone()
    return unpack(row["embedding"]) if row else []


def all_vectors(conn: sqlite3.Connection, exclude: str | None = None
                ) -> list[tuple[str, Vector]]:
    rows = conn.execute(
        "SELECT note_id, embedding FROM note_vec WHERE note_id <> IFNULL(?, '')",
        (exclude,),
    ).fetchall()
    return [(r["note_id"], unpack(r["embedding"])) for r in rows]


def vectors_for_topic(conn: sqlite3.Connection, topic_id: str) -> list[tuple[str, Vector]]:
    rows = conn.execute(
        "SELECT nv.note_id, nv.embedding FROM note_vec nv"
        " JOIN note_topics nt ON nt.note_id = nv.note_id WHERE nt.topic_id = ?",
        (topic_id,),
    ).fetchall()
    return [(r["note_id"], unpack(r["embedding"])) for r in rows]


# ------------------------------------------------------------ assignment ----
def assign(conn: sqlite3.Connection, note_id: str, topic_id: str,
           is_primary: bool = True, confidence: float = 0.0,
           assigned_by: str = "llm") -> None:
    """Attach a note to a topic. A note has exactly one primary topic (which
    decides its folder) and any number of secondary ones."""
    if is_primary:
        conn.execute(
            "UPDATE note_topics SET is_primary = 0 WHERE note_id = ?", (note_id,)
        )
    conn.execute(
        "INSERT INTO note_topics(note_id, topic_id, is_primary, confidence, assigned_by)"
        " VALUES(?,?,?,?,?)"
        " ON CONFLICT(note_id, topic_id) DO UPDATE SET is_primary=excluded.is_primary,"
        " confidence=excluded.confidence, assigned_by=excluded.assigned_by",
        (note_id, topic_id, int(is_primary), confidence, assigned_by),
    )


def unassign(conn: sqlite3.Connection, note_id: str, topic_id: str) -> None:
    conn.execute(
        "DELETE FROM note_topics WHERE note_id = ? AND topic_id = ?", (note_id, topic_id)
    )


def primary_topic(conn: sqlite3.Connection, note_id: str) -> str | None:
    row = conn.execute(
        "SELECT topic_id FROM note_topics WHERE note_id = ? AND is_primary = 1",
        (note_id,),
    ).fetchone()
    return row["topic_id"] if row else None


def topics_of(conn: sqlite3.Connection, note_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT topic_id FROM note_topics WHERE note_id = ? ORDER BY is_primary DESC",
        (note_id,),
    ).fetchall()
    return [r["topic_id"] for r in rows]


def in_topic(conn: sqlite3.Connection, topic_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT n.* FROM notes n JOIN note_topics nt ON nt.note_id = n.id"
        f" WHERE nt.topic_id = ? ORDER BY {ORDER}",
        (topic_id,),
    ).fetchall()


def inbox(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT n.* FROM notes n LEFT JOIN note_topics nt ON nt.note_id = n.id"
        f" WHERE nt.note_id IS NULL ORDER BY {ORDER}"
    ).fetchall()


# ------------------------------------------------------------ your order ----
# A note's place is where you dragged it, or else the moment it was made (in
# seconds), so notes you never moved stay newest-first and new ones land on top.
_MADE = "(julianday(n.created_at) - 2440587.5) * 86400.0"
ORDER = f"COALESCE((SELECT pos FROM note_order WHERE note_id = n.id), {_MADE}) DESC"


def position(conn: sqlite3.Connection, note_id: str) -> float | None:
    row = conn.execute(
        f"SELECT COALESCE((SELECT pos FROM note_order WHERE note_id = n.id), {_MADE})"
        " AS p FROM notes n WHERE n.id = ?", (note_id,)).fetchone()
    return row["p"] if row else None


def _sync_meta(conn: sqlite3.Connection, note: Note) -> None:
    values = (note.repeat, note.checkin, note.quiet_until, note.parent)
    if not any(values):
        conn.execute("DELETE FROM note_meta WHERE note_id = ?", (note.id,))
        return
    conn.execute(
        "INSERT INTO note_meta(note_id, repeat, checkin, quiet_until, parent) VALUES(?,?,?,?,?)"
        " ON CONFLICT(note_id) DO UPDATE SET repeat = excluded.repeat,"
        " checkin = excluded.checkin, quiet_until = excluded.quiet_until,"
        " parent = excluded.parent", (note.id, *values))


def meta(conn: sqlite3.Connection, note_id: str) -> dict:
    row = conn.execute("SELECT * FROM note_meta WHERE note_id = ?", (note_id,)).fetchone()
    keys = ("repeat", "checkin", "quiet_until", "parent")
    return {k: (row[k] if row else None) for k in keys}


def lists(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every to-do list, in your order, with its header settings alongside."""
    return conn.execute(
        "SELECT n.*, m.repeat, m.checkin, m.quiet_until, m.parent FROM notes n"
        f" LEFT JOIN note_meta m ON m.note_id = n.id WHERE n.kind = 'list' ORDER BY {ORDER}"
    ).fetchall()


def _sync_order(conn: sqlite3.Connection, note: Note) -> None:
    if note.order is None:
        conn.execute("DELETE FROM note_order WHERE note_id = ?", (note.id,))
    else:
        conn.execute("INSERT INTO note_order(note_id, pos) VALUES(?, ?)"
                     " ON CONFLICT(note_id) DO UPDATE SET pos = excluded.pos",
                     (note.id, float(note.order)))


def needing_review(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notes WHERE needs_review = 1 ORDER BY created_at DESC"
    ).fetchall()


# ----------------------------------------------------------------- edges ----
def link(conn: sqlite3.Connection, src: str, dst: str, rel: str = "related",
         weight: float = 1.0, created_by: str = "system") -> None:
    if src == dst:
        return
    conn.execute(
        "INSERT INTO edges(src, dst, rel, weight, created_by) VALUES(?,?,?,?,?)"
        " ON CONFLICT(src, dst, rel) DO UPDATE SET weight = excluded.weight",
        (src, dst, rel, weight, created_by),
    )


def related(conn: sqlite3.Connection, note_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT n.*, e.weight FROM edges e
          JOIN notes n ON n.id = CASE WHEN e.src = ? THEN e.dst ELSE e.src END
         WHERE (e.src = ? OR e.dst = ?) AND e.rel = 'related'
         ORDER BY e.weight DESC
        """,
        (note_id, note_id, note_id),
    ).fetchall()
