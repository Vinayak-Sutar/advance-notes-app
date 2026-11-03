"""The topic graph: a tree of topic nodes, each with an embedding centroid.

`topics.parent_id` IS the hierarchy the user browses, and the primary topic's
path is what the vault projects onto disk. Topics stay few (dozens), so
recursive CTEs over this table are effectively free.

Centroids are maintained as running means, so folding a note into a topic is
O(dim) and never needs to re-read the topic's members.
"""
from __future__ import annotations

import sqlite3

from .db import (key_contains, key_similarity, normal_key, now_iso,
                 slugify, ulid)
from .vecs import Vector, pack, running_mean, unpack

MAX_DEPTH = 3          # root = depth 0, so at most Domain/Sub/SubSub
MAX_CHILDREN = 12      # breadth cap per parent


class TopicError(Exception):
    pass


# ------------------------------------------------------------ creation ----
def create(conn: sqlite3.Connection, name: str, parent_id: str | None = None,
           state: str = "provisional", description: str = "",
           name_vec: Vector | None = None) -> str:
    """Insert a topic. Callers should go through `gate.propose_topic` rather
    than calling this directly -- the gate is what stops taxonomy sprawl."""
    depth = 0 if parent_id is None else depth_of(conn, parent_id) + 1
    if depth >= MAX_DEPTH:
        raise TopicError(f"max depth {MAX_DEPTH} exceeded")
    slug = slugify(name)
    existing = by_slug(conn, slug, parent_id)
    if existing:
        return existing["id"]
    topic_id = ulid()
    conn.execute(
        "INSERT INTO topics(id, parent_id, name, slug, norm_key, name_vec, description,"
        " state, depth, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (topic_id, parent_id, name.strip(), slug, normal_key(name),
         pack(name_vec) if name_vec else None, description, state, depth, now_iso()),
    )
    return topic_id


def by_norm_key(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """Exact normalised-name match ANYWHERE in the tree.

    Deliberately global rather than sibling-scoped: a second 'Gaming' under a
    different parent is still the sprawl we are trying to prevent.
    """
    return conn.execute(
        "SELECT * FROM topics WHERE norm_key = ? AND state <> 'dissolved'"
        " ORDER BY note_count DESC LIMIT 1",
        (normal_key(name),),
    ).fetchone()


def near_key(conn: sqlite3.Connection, name: str, threshold: float = 0.5
             ) -> tuple[sqlite3.Row, float] | None:
    """Best topic whose normalised name overlaps `name` by more than
    `threshold` (prefix-aware Jaccard). Catches 'Web Dev' vs 'Web Development'
    without needing embeddings."""
    key = normal_key(name)
    best, best_score = None, threshold
    for row in conn.execute(
        "SELECT * FROM topics WHERE state <> 'dissolved'"
    ).fetchall():
        score = key_similarity(key, row["norm_key"])
        if score > best_score:
            best, best_score = row, score
    return (best, best_score) if best else None


def containing_topic(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """The most specific existing topic that `name` is a specialisation of.

    'Gaming Hardware' -> Gaming. Used by the gate to nest a proposal under the
    topic it elaborates, instead of creating a rival root-level folder.
    """
    key = normal_key(name)
    best = None
    for row in conn.execute(
        "SELECT * FROM topics WHERE state <> 'dissolved'"
    ).fetchall():
        if key_contains(key, row["norm_key"]):
            if best is None or len(row["norm_key"].split()) > len(best["norm_key"].split()):
                best = row
    return best


def name_vecs(conn: sqlite3.Connection) -> list[tuple[str, Vector]]:
    """Topic NAME embeddings -- compared against a proposed name. Distinct from
    `centroids()`, which holds the mean of member-note embeddings."""
    rows = conn.execute(
        "SELECT id, name_vec FROM topics WHERE name_vec IS NOT NULL"
        " AND state <> 'dissolved'"
    ).fetchall()
    return [(r["id"], unpack(r["name_vec"])) for r in rows]


def set_name_vec(conn: sqlite3.Connection, topic_id: str, vec: Vector) -> None:
    conn.execute("UPDATE topics SET name_vec = ? WHERE id = ?", (pack(vec), topic_id))


def by_slug(conn: sqlite3.Connection, slug: str, parent_id: str | None) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM topics WHERE slug = ? AND IFNULL(parent_id,'~root~') = IFNULL(?,'~root~')"
        " AND state <> 'dissolved'",
        (slug, parent_id),
    ).fetchone()


def get(conn: sqlite3.Connection, topic_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()


def by_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    """Resolve 'Technology/AI' (or 'technology/ai') to a topic row."""
    parent_id = None
    row = None
    for segment in [s for s in path.replace("\\", "/").split("/") if s.strip()]:
        row = by_slug(conn, slugify(segment), parent_id)
        if row is None:
            return None
        parent_id = row["id"]
    return row


def ensure_path(conn: sqlite3.Connection, path: str, state: str = "active") -> str:
    """Create every missing segment of a path. Used by rebuild and by explicit
    user steering, both of which bypass the gate on purpose."""
    parent_id = None
    for segment in [s for s in path.replace("\\", "/").split("/") if s.strip()]:
        row = by_slug(conn, slugify(segment), parent_id)
        parent_id = row["id"] if row else create(conn, segment, parent_id, state=state)
    if parent_id is None:
        raise TopicError(f"empty topic path: {path!r}")
    return parent_id


# ------------------------------------------------------------ traversal ----
def depth_of(conn: sqlite3.Connection, topic_id: str) -> int:
    row = get(conn, topic_id)
    if row is None:
        raise TopicError(f"unknown topic {topic_id}")
    return row["depth"]


def path_of(conn: sqlite3.Connection, topic_id: str | None) -> str | None:
    """Display path, e.g. 'Technology/AI'. None for an unfiled note."""
    if topic_id is None:
        return None
    rows = conn.execute(
        """
        WITH RECURSIVE up(id, parent_id, name, lvl) AS (
            SELECT id, parent_id, name, 0 FROM topics WHERE id = ?
            UNION ALL
            SELECT t.id, t.parent_id, t.name, up.lvl + 1
              FROM topics t JOIN up ON t.id = up.parent_id
        )
        SELECT name FROM up ORDER BY lvl DESC
        """,
        (topic_id,),
    ).fetchall()
    return "/".join(r["name"] for r in rows) if rows else None


def slug_path_of(conn: sqlite3.Connection, topic_id: str | None) -> str | None:
    path = path_of(conn, topic_id)
    return "/".join(slugify(p) for p in path.split("/")) if path else None


def children(conn: sqlite3.Connection, parent_id: str | None) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM topics WHERE IFNULL(parent_id,'~root~') = IFNULL(?,'~root~')"
        " AND state <> 'dissolved' ORDER BY name",
        (parent_id,),
    ).fetchall()


def descendants(conn: sqlite3.Connection, topic_id: str, include_self: bool = True) -> list[str]:
    rows = conn.execute(
        """
        WITH RECURSIVE down(id) AS (
            SELECT id FROM topics WHERE id = ?
            UNION ALL
            SELECT t.id FROM topics t JOIN down ON t.parent_id = down.id
        )
        SELECT id FROM down
        """,
        (topic_id,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    return ids if include_self else ids[1:]


def all_active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM topics WHERE state <> 'dissolved' ORDER BY depth, name"
    ).fetchall()


def sibling_count(conn: sqlite3.Connection, parent_id: str | None) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM topics WHERE IFNULL(parent_id,'~root~') = IFNULL(?,'~root~')"
        " AND state <> 'dissolved'",
        (parent_id,),
    ).fetchone()["c"]


# ------------------------------------------------------------ centroids ----
def centroid(conn: sqlite3.Connection, topic_id: str) -> tuple[Vector, int]:
    row = conn.execute(
        "SELECT centroid, n FROM topic_vec WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    return (unpack(row["centroid"]), row["n"]) if row else ([], 0)


def fold_into_centroid(conn: sqlite3.Connection, topic_id: str, vec: Vector) -> None:
    """Add one note's embedding to a topic's running-mean centroid."""
    current, n = centroid(conn, topic_id)
    updated = running_mean(current, n, vec) if current else list(vec)
    conn.execute(
        "INSERT INTO topic_vec(topic_id, centroid, dim, n) VALUES(?,?,?,?)"
        " ON CONFLICT(topic_id) DO UPDATE SET centroid=excluded.centroid,"
        " dim=excluded.dim, n=excluded.n",
        (topic_id, pack(updated), len(updated), n + 1),
    )


def set_centroid(conn: sqlite3.Connection, topic_id: str, vec: Vector, n: int) -> None:
    conn.execute(
        "INSERT INTO topic_vec(topic_id, centroid, dim, n) VALUES(?,?,?,?)"
        " ON CONFLICT(topic_id) DO UPDATE SET centroid=excluded.centroid,"
        " dim=excluded.dim, n=excluded.n",
        (topic_id, pack(vec), len(vec), n),
    )


def centroids(conn: sqlite3.Connection, states: tuple[str, ...] = ("active", "provisional")
              ) -> list[tuple[str, Vector]]:
    """Every candidate topic's centroid, for brute-force retrieval."""
    placeholders = ",".join("?" * len(states))
    rows = conn.execute(
        f"SELECT tv.topic_id, tv.centroid FROM topic_vec tv"
        f" JOIN topics t ON t.id = tv.topic_id WHERE t.state IN ({placeholders})",
        states,
    ).fetchall()
    return [(r["topic_id"], unpack(r["centroid"])) for r in rows]


# ------------------------------------------------------------- counting ----
def recount(conn: sqlite3.Connection, topic_id: str) -> int:
    n = conn.execute(
        "SELECT COUNT(*) c FROM note_topics WHERE topic_id = ?", (topic_id,)
    ).fetchone()["c"]
    conn.execute("UPDATE topics SET note_count = ? WHERE id = ?", (n, topic_id))
    return n


def recount_all(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE topics SET note_count = ("
        "  SELECT COUNT(*) FROM note_topics nt WHERE nt.topic_id = topics.id)"
    )


# --------------------------------------------------------------- aliases ----
def resolve_alias(conn: sqlite3.Connection, name: str) -> str | None:
    """A previously-refused synonym resolves straight to the real topic."""
    row = conn.execute(
        "SELECT topic_id FROM topic_aliases WHERE alias = ?", (slugify(name),)
    ).fetchone()
    return row["topic_id"] if row else None


def add_alias(conn: sqlite3.Connection, name: str, topic_id: str) -> None:
    conn.execute(
        "INSERT INTO topic_aliases(alias, topic_id, created_at) VALUES(?,?,?)"
        " ON CONFLICT(alias) DO UPDATE SET topic_id = excluded.topic_id",
        (slugify(name), topic_id, now_iso()),
    )
