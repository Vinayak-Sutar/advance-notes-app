"""The new-topic gate -- the thing that lets the LLM create folders freely
without the taxonomy degenerating.

A `create_topic` from the router is a *request*, not a command. Six checks run
before anything is created, cheapest first -- most duplicates are caught by
string normalisation before any embedding is compared:

  1. alias     a synonym we refused once resolves instantly next time
  2. sibling   exact slug match under the same parent
  3. norm_key  'Games', 'Gamer', 'My Games', 'Gaming Stuff' all reduce to the
               key 'gam' and collapse into the existing Gaming topic
  4. near_key  prefix-aware token overlap: 'Web Dev' ~ 'Web Development'
  5. embedding two axes -- proposed name vs topic NAME vectors, and vs topic
               content CENTROIDS. These occupy different regions of the space,
               so checking only centroids misses obvious duplicates.
  6. shape     depth and breadth caps

Every refusal writes an alias, so the same synonym never costs a comparison
twice. Anything that survives is created as `provisional`: it exists in the
graph and notes can be assigned to it, but it gets no folder on disk until
`min_mass` notes accumulate -- which is what prevents thirty folders each
holding a single note.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import topics as T
from .config import Config
from .db import normal_key, slugify
from .vecs import Vector, top_k

REUSE_ALIAS = "reuse:alias"
REUSE_SIBLING = "reuse:sibling"
REUSE_NORMKEY = "reuse:normkey"
REUSE_NEARKEY = "reuse:nearkey"
REUSE_NAMEVEC = "reuse:namevec"
REUSE_CENTROID = "reuse:centroid"
REJECT_DEPTH = "reject:depth"
REJECT_BREADTH = "reject:breadth"
CREATED = "created"


@dataclass
class GateDecision:
    action: str                    # 'reuse' | 'create' | 'reject'
    topic_id: str | None
    reason: str
    detail: dict = field(default_factory=dict)

    @property
    def created(self) -> bool:
        return self.action == "create"


def propose_topic(
    conn: sqlite3.Connection,
    name: str,
    parent_id: str | None,
    vec: Vector,
    cfg: Config,
) -> GateDecision:
    """Decide what actually happens when the router asks for a new topic.

    `vec` is the embedding of the proposed topic (its name plus the router's
    rationale), used for the centroid-similarity check.
    """
    name = (name or "").strip()
    if not name:
        return GateDecision("reject", None, "reject:empty-name")
    slug = slugify(name)

    # 1. Alias: we have refused this exact synonym before.
    aliased = T.resolve_alias(conn, slug)
    if aliased and T.get(conn, aliased) is not None:
        return GateDecision("reuse", aliased, REUSE_ALIAS, {"alias": slug})

    # 2. Exact sibling name under the same parent.
    sibling = T.by_slug(conn, slug, parent_id)
    if sibling is not None:
        return GateDecision("reuse", sibling["id"], REUSE_SIBLING, {"slug": slug})

    # 2b. Same normalised name anywhere in the tree. 'Games', 'Gamer',
    #     'My Games' and 'Gaming Stuff' all reduce to the key 'gam', so this
    #     catches the classic sprawl case with no embedding comparison at all.
    twin = T.by_norm_key(conn, name)
    if twin is not None:
        T.add_alias(conn, slug, twin["id"])
        return GateDecision("reuse", twin["id"], REUSE_NORMKEY,
                            {"key": normal_key(name), "matched": twin["name"]})

    # 2c. Overlapping normalised names, prefix-aware: 'Web Dev' vs
    #     'Web Development', 'Tech' vs 'Technology'.
    near = T.near_key(conn, name)
    if near is not None:
        row, score = near
        T.add_alias(conn, slug, row["id"])
        return GateDecision("reuse", row["id"], REUSE_NEARKEY,
                            {"overlap": round(score, 3), "matched": row["name"]})

    # 2d. A proposal that elaborates an existing topic ('Gaming Hardware' over
    #     'Gaming') is a subtopic, not a duplicate. Nest it rather than letting
    #     it become a competing root-level folder.
    host = T.containing_topic(conn, name)
    host_id: str | None = None
    if host is not None and parent_id is None:
        if T.depth_of(conn, host["id"]) + 1 < cfg.max_depth:
            parent_id = host_id = host["id"]

    # 3. Embedding similarity, on two axes. A proposed NAME sits in a different
    #    part of the space than a topic's content CENTROID, so comparing only
    #    against centroids misses obvious duplicates. Check both and take the
    #    stronger signal.
    if vec:
        best_id, best_sim, axis = None, 0.0, ""
        for label, candidates, threshold in (
            ("namevec", T.name_vecs(conn), cfg.dedupe_threshold),
            ("centroid", T.centroids(conn), cfg.dedupe_centroid_threshold),
        ):
            # If containment already identified this as a specialisation of
            # `host`, then resembling `host` is expected -- that is not
            # evidence of being a duplicate of it.
            if host_id is not None:
                candidates = [(tid, v) for tid, v in candidates if tid != host_id]
            if not candidates:
                continue
            ranked = top_k(vec, candidates, k=1)
            # Each axis is scored against its OWN threshold, then compared by
            # how far past that threshold it reached.
            if ranked and ranked[0][1] >= threshold:
                headroom = ranked[0][1] - threshold
                if best_id is None or headroom > best_sim - cfg.dedupe_threshold:
                    best_id, best_sim, axis = ranked[0][0], ranked[0][1], label
        if best_id is not None:
            T.add_alias(conn, slug, best_id)         # remember the refusal
            return GateDecision(
                "reuse", best_id,
                REUSE_NAMEVEC if axis == "namevec" else REUSE_CENTROID,
                {"similarity": round(best_sim, 4), "axis": axis,
                 "threshold": cfg.dedupe_threshold if axis == "namevec"
                              else cfg.dedupe_centroid_threshold,
                 "refused_name": name},
            )

    # 4. Shape caps.
    depth = 0 if parent_id is None else T.depth_of(conn, parent_id) + 1
    if depth >= cfg.max_depth:
        return GateDecision("reject", parent_id, REJECT_DEPTH,
                            {"depth": depth, "max": cfg.max_depth})

    if T.sibling_count(conn, parent_id) >= cfg.max_children:
        return GateDecision("reject", parent_id, REJECT_BREADTH,
                            {"siblings": T.sibling_count(conn, parent_id),
                             "max": cfg.max_children, "wanted": name})

    topic_id = T.create(conn, name, parent_id, state="provisional", name_vec=vec)
    return GateDecision("create", topic_id, CREATED, {"name": name, "depth": depth})


# ------------------------------------------------------------ promotion ----
def promote_ready(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    """Flip provisional topics that have reached `min_mass` to active.

    Returns the ids that graduated. The engine is responsible for physically
    moving their notes out of the parent folder and into the new directory.
    """
    rows = conn.execute(
        """
        SELECT t.id, COUNT(nt.note_id) AS n
          FROM topics t
          LEFT JOIN note_topics nt ON nt.topic_id = t.id
         WHERE t.state = 'provisional'
         GROUP BY t.id
        HAVING n >= ?
        """,
        (cfg.min_mass,),
    ).fetchall()
    promoted = []
    for row in rows:
        conn.execute("UPDATE topics SET state = 'active' WHERE id = ?", (row["id"],))
        promoted.append(row["id"])
    return promoted


def stale_provisional(conn: sqlite3.Connection, cfg: Config) -> list[sqlite3.Row]:
    """Provisional topics that never grew, past their TTL. Gardener dissolves these."""
    return conn.execute(
        """
        SELECT t.*, COUNT(nt.note_id) AS n
          FROM topics t
          LEFT JOIN note_topics nt ON nt.topic_id = t.id
         WHERE t.state = 'provisional'
           AND julianday('now') - julianday(t.created_at) > ?
         GROUP BY t.id
        HAVING n < ?
        """,
        (cfg.provisional_ttl_days, cfg.min_mass),
    ).fetchall()


def effective_dir(conn: sqlite3.Connection, topic_id: str | None) -> str | None:
    """Where a note assigned to `topic_id` actually lives on disk right now.

    A provisional topic has no folder of its own, so its notes sit in the
    nearest active ancestor's directory. This is the mechanism that keeps the
    filesystem clean while the graph stays expressive.
    """
    while topic_id is not None:
        row = T.get(conn, topic_id)
        if row is None:
            return None
        if row["state"] == "active":
            return T.path_of(conn, topic_id)
        topic_id = row["parent_id"]
    return None
