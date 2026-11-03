"""Hybrid search: FTS5 for exact terms, vectors for meaning, fused with RRF.

Neither half is sufficient alone. Keyword search misses "how do I chunk
documents" -> "RAG splitting strategies"; vector search misses an exact error
code or a proper noun it has never seen. Reciprocal Rank Fusion combines the
two orderings without needing their scores to be on comparable scales -- which
matters, because an FTS5 bm25 score and a cosine similarity are not.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from . import notes as N
from . import topics as T
from .vecs import Vector, top_k

RRF_K = 60          # standard damping constant; larger = flatter rank weighting


@dataclass
class Hit:
    note_id: str
    title: str
    path: str | None
    topic_path: str | None
    score: float
    snippet: str = ""
    via: str = ""      # fts | vector | both


def _fts_query(raw: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    User input goes in raw, so every token is quoted -- otherwise a stray '*',
    'NEAR' or unbalanced quote is a syntax error at best.
    """
    tokens = re.findall(r"[\w']+", raw.lower())
    return " OR ".join(f'"{t}"' for t in tokens if len(t) > 1)


def keyword(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[tuple[str, float]]:
    expr = _fts_query(query)
    if not expr:
        return []
    try:
        rows = conn.execute(
            "SELECT n.id, bm25(notes_fts) AS score FROM notes_fts"
            " JOIN notes n ON n.rowid = notes_fts.rowid"
            " WHERE notes_fts MATCH ? ORDER BY score LIMIT ?",
            (expr, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["id"], r["score"]) for r in rows]      # bm25: lower is better


def semantic(conn: sqlite3.Connection, vec: Vector, limit: int = 20
             ) -> list[tuple[str, float]]:
    return top_k(vec, N.all_vectors(conn), k=limit)


def search(conn: sqlite3.Connection, query: str, vec: Vector | None = None,
           limit: int = 20) -> list[Hit]:
    kw = keyword(conn, query, limit * 2)
    sem = semantic(conn, vec, limit * 2) if vec else []

    ranks: dict[str, float] = {}
    seen_in: dict[str, set[str]] = {}
    for source, ordered in (("fts", [i for i, _ in kw]), ("vector", [i for i, _ in sem])):
        for rank, note_id in enumerate(ordered):
            ranks[note_id] = ranks.get(note_id, 0.0) + 1.0 / (RRF_K + rank + 1)
            seen_in.setdefault(note_id, set()).add(source)

    hits: list[Hit] = []
    for note_id, score in sorted(ranks.items(), key=lambda kv: kv[1], reverse=True)[:limit]:
        row = N.get(conn, note_id)
        if row is None:
            continue
        sources = seen_in.get(note_id, set())
        hits.append(Hit(
            note_id=note_id,
            title=row["title"],
            path=row["path"],
            topic_path=T.path_of(conn, N.primary_topic(conn, note_id)),
            score=round(score, 5),
            snippet=(row["body"] or "")[:160].replace("\n", " "),
            via="both" if len(sources) > 1 else next(iter(sources), ""),
        ))
    return hits


# ---------------------------------------------------------------- views ----
def topic_view(conn: sqlite3.Connection, topic_id: str, kind: str | None = None,
               status: str | None = None, include_subtopics: bool = True
               ) -> list[sqlite3.Row]:
    """The auto-generated sidebar lists.

    'Gaming > YouTube' is not a folder -- it is this query with kind='video'.
    That is what lets one note appear under both its topic and every relevant
    type view without being duplicated on disk.
    """
    topic_ids = T.descendants(conn, topic_id) if include_subtopics else [topic_id]
    placeholders = ",".join("?" * len(topic_ids))
    sql = (f"SELECT DISTINCT n.* FROM notes n JOIN note_topics nt ON nt.note_id = n.id"
           f" WHERE nt.topic_id IN ({placeholders})")
    params: list = list(topic_ids)
    if kind:
        sql += " AND n.kind = ?"
        params.append(kind)
    if status:
        sql += " AND n.status = ?"
        params.append(status)
    sql += f" ORDER BY {N.ORDER}"
    return conn.execute(sql, params).fetchall()


def view_counts(conn: sqlite3.Connection, topic_id: str) -> dict[str, int]:
    """Counts for the auto-views under one topic, for the sidebar badges."""
    topic_ids = T.descendants(conn, topic_id)
    placeholders = ",".join("?" * len(topic_ids))
    rows = conn.execute(
        f"SELECT n.kind, COUNT(DISTINCT n.id) AS c FROM notes n"
        f" JOIN note_topics nt ON nt.note_id = n.id"
        f" WHERE nt.topic_id IN ({placeholders}) GROUP BY n.kind",
        topic_ids,
    ).fetchall()
    counts = {r["kind"]: r["c"] for r in rows}
    pending = conn.execute(
        f"SELECT COUNT(DISTINCT n.id) AS c FROM notes n"
        f" JOIN note_topics nt ON nt.note_id = n.id"
        f" WHERE nt.topic_id IN ({placeholders}) AND n.status = 'open'"
        f" AND n.kind = 'task'",
        topic_ids,
    ).fetchone()["c"]
    if pending:
        counts["pending"] = pending
    return counts
