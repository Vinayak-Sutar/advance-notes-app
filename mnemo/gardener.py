"""Background compaction for the taxonomy.

The gate stops obvious duplicates at creation time, but it can only judge with
the information available at that moment. As the vault grows, better structure
becomes visible: two topics turn out to be one, one topic turns out to be two,
a topic clearly belongs under a different parent.

So routing decisions are revisited rather than treated as final. Everything
here produces *proposals* -- nothing is applied without a decision, and every
application goes through the journal so it can be undone.

Note the division of labour: the maths finds the structure (k-means finds that
a topic has split in two), and the LLM is only ever asked to *name* what the
maths found. That ordering is deliberate -- clustering is reliable, naming is
what models are actually good at.
"""
from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass, field

from . import gate as _gate
from . import notes as N
from . import topics as T
from .config import Config
from .db import now_iso, ulid
from .vecs import Vector, centred, cosine, mean

MERGE = "merge"
SPLIT = "split"
REPARENT = "reparent"
DISSOLVE = "dissolve"


@dataclass
class Proposal:
    kind: str
    payload: dict
    score: float
    summary: str
    id: str = field(default_factory=ulid)


# ------------------------------------------------------------- clustering ----
def kmeans(vectors: list[Vector], k: int, iters: int = 30, seed: int = 3
           ) -> tuple[list[int], list[Vector]]:
    """k-means with k-means++ style seeding, on cosine similarity.

    Used by the cold-start bootstrap: rather than letting the first 20 notes
    each invent a topic, we wait, cluster them, and derive the starting
    taxonomy from structure that is actually visible in the data.
    """
    if k <= 1 or len(vectors) <= k:
        return [0] * len(vectors), [mean(vectors)] if vectors else []
    rng = random.Random(seed)

    centres = [vectors[rng.randrange(len(vectors))]]
    while len(centres) < k:
        # Pick the point furthest from anything already chosen.
        far_idx, far_score = 0, 2.0
        for i, vec in enumerate(vectors):
            closest = max(cosine(vec, c) for c in centres)
            if closest < far_score:
                far_score, far_idx = closest, i
        centres.append(vectors[far_idx])

    assign = [0] * len(vectors)
    for _ in range(iters):
        changed = False
        for i, vec in enumerate(vectors):
            best = max(range(len(centres)), key=lambda c: cosine(vec, centres[c]))
            if best != assign[i]:
                assign[i], changed = best, True
        for c in range(len(centres)):
            members = [v for v, a in zip(vectors, assign) if a == c]
            if members:
                centres[c] = mean(members)
        if not changed:
            break
    return assign, centres


def cluster_quality(vectors: list[Vector], assign: list[int],
                    centres: list[Vector]) -> float:
    """Mean (own-centre similarity - best other-centre similarity).

    Higher is a cleaner clustering. Comparing this across k is how the
    bootstrap decides how many topics the data actually supports, instead of
    being told a number up front.
    """
    if len(centres) < 2:
        return 0.0
    scores = []
    for vec, own in zip(vectors, assign):
        own_sim = cosine(vec, centres[own])
        other = max(cosine(vec, c) for i, c in enumerate(centres) if i != own)
        scores.append(own_sim - other)
    return sum(scores) / len(scores) if scores else 0.0


def choose_k(vectors: list[Vector], k_min: int = 2, k_max: int = 12,
             min_cluster: int = 2) -> tuple[int, list[int], list[Vector]]:
    """Pick the k whose clustering separates best, ignoring any k that
    produces a cluster too small to be a real topic.

    Clusters on MEAN-CENTRED vectors. Without that correction a trained
    embedder's shared component swamps the differences and this collapses to a
    handful of huge mixed clusters -- measured: k=6 at 45.8% purity raw, versus
    k=12 at 85.8% centred, against a ground truth of 10 topics.
    """
    vectors = centred(vectors)
    best = (0, -2.0, [0] * len(vectors), [])
    for k in range(k_min, min(k_max, len(vectors) // max(1, min_cluster)) + 1):
        assign, centres = kmeans(vectors, k)
        sizes = [assign.count(c) for c in range(len(centres))]
        if min(sizes) < min_cluster:
            continue
        score = cluster_quality(vectors, assign, centres)
        if score > best[1]:
            best = (k, score, assign, centres)
    if best[0] == 0:
        assign, centres = kmeans(vectors, k_min)
        return k_min, assign, centres
    return best[0], best[2], best[3]


def kmeans2(vectors: list[Vector], iters: int = 25, seed: int = 3
            ) -> tuple[list[int], float]:
    """Two-way k-means. Returns (assignments, separation).

    `separation` is 1 - cosine(centroid_a, centroid_b): how genuinely different
    the two halves are. Low separation means the topic is coherent and should
    NOT be split, however many notes it holds.
    """
    if len(vectors) < 4:
        return [0] * len(vectors), 0.0
    vectors = centred(vectors)          # see choose_k: anisotropy correction
    rng = random.Random(seed)

    # Seed with the two most dissimilar vectors we find in a small sample,
    # so the split starts from a real axis of variation rather than noise.
    sample = rng.sample(range(len(vectors)), min(12, len(vectors)))
    a_idx, b_idx, worst = sample[0], sample[1], 2.0
    for i in sample:
        for j in sample:
            if i < j:
                sim = cosine(vectors[i], vectors[j])
                if sim < worst:
                    worst, a_idx, b_idx = sim, i, j
    centre_a, centre_b = vectors[a_idx], vectors[b_idx]

    assign = [0] * len(vectors)
    for _ in range(iters):
        changed = False
        for i, vec in enumerate(vectors):
            side = 0 if cosine(vec, centre_a) >= cosine(vec, centre_b) else 1
            if side != assign[i]:
                assign[i], changed = side, True
        group_a = [v for v, s in zip(vectors, assign) if s == 0]
        group_b = [v for v, s in zip(vectors, assign) if s == 1]
        if not group_a or not group_b:
            return assign, 0.0
        centre_a, centre_b = mean(group_a), mean(group_b)
        if not changed:
            break
    return assign, 1.0 - cosine(centre_a, centre_b)


# -------------------------------------------------------------- detectors ----
def find_merges(conn: sqlite3.Connection, cfg: Config) -> list[Proposal]:
    """Topic pairs whose centroids have converged."""
    entries = [(tid, vec) for tid, vec in T.centroids(conn) if vec]
    out: list[Proposal] = []
    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            a_id, a_vec = entries[i]
            b_id, b_vec = entries[j]
            sim = cosine(a_vec, b_vec)
            if sim < cfg.merge_threshold:
                continue
            a, b = T.get(conn, a_id), T.get(conn, b_id)
            if a is None or b is None:
                continue
            # Never propose merging a topic into its own ancestor/descendant --
            # that is a reparent question, not a merge.
            if b_id in T.descendants(conn, a_id) or a_id in T.descendants(conn, b_id):
                continue
            keep, drop = (a, b) if a["note_count"] >= b["note_count"] else (b, a)
            out.append(Proposal(
                kind=MERGE,
                payload={"keep": keep["id"], "drop": drop["id"]},
                score=round(sim, 4),
                summary=(f'Merge "{drop["name"]}" ({drop["note_count"]} notes) into '
                         f'"{keep["name"]}" ({keep["note_count"]} notes) — '
                         f"centroids {sim:.2f} similar"),
            ))
    return out


def find_splits(conn: sqlite3.Connection, cfg: Config) -> list[Proposal]:
    """Topics large enough, and internally varied enough, to be two topics."""
    out: list[Proposal] = []
    for row in T.all_active(conn):
        members = N.vectors_for_topic(conn, row["id"])
        if len(members) < cfg.split_min_notes:
            continue
        ids = [note_id for note_id, _ in members]
        vectors = [vec for _, vec in members]
        assign, separation = kmeans2(vectors)
        if separation < cfg.split_min_separation:
            continue
        group_a = [ids[i] for i, s in enumerate(assign) if s == 0]
        group_b = [ids[i] for i, s in enumerate(assign) if s == 1]
        if min(len(group_a), len(group_b)) < max(3, cfg.min_mass):
            continue
        out.append(Proposal(
            kind=SPLIT,
            payload={"topic": row["id"], "group_a": group_a, "group_b": group_b},
            score=round(separation, 4),
            summary=(f'"{row["name"]}" looks like two clusters '
                     f"({len(group_a)} and {len(group_b)} notes, "
                     f"separation {separation:.2f}) — split?"),
        ))
    return out


def find_reparents(conn: sqlite3.Connection, cfg: Config) -> list[Proposal]:
    """A topic sitting closer to some other parent than to its own."""
    out: list[Proposal] = []
    centroids = dict(T.centroids(conn))
    for row in T.all_active(conn):
        vec = centroids.get(row["id"])
        if not vec or row["parent_id"] is None:
            continue
        own = centroids.get(row["parent_id"])
        own_sim = cosine(vec, own) if own else 0.0
        blocked = set(T.descendants(conn, row["id"])) | {row["parent_id"]}
        best_id, best_sim = None, own_sim + 0.10   # require a clear improvement
        for other in T.all_active(conn):
            if other["id"] in blocked or other["depth"] >= cfg.max_depth - 1:
                continue
            other_vec = centroids.get(other["id"])
            if other_vec and cosine(vec, other_vec) > best_sim:
                best_id, best_sim = other["id"], cosine(vec, other_vec)
        if best_id:
            out.append(Proposal(
                kind=REPARENT,
                payload={"topic": row["id"], "new_parent": best_id},
                score=round(best_sim - own_sim, 4),
                summary=(f'Move "{row["name"]}" under '
                         f'"{T.path_of(conn, best_id)}" '
                         f"({best_sim:.2f} vs {own_sim:.2f} to its current parent)"),
            ))
    return out


def find_dissolves(conn: sqlite3.Connection, cfg: Config) -> list[Proposal]:
    """Provisional topics that never reached critical mass."""
    return [
        Proposal(
            kind=DISSOLVE,
            payload={"topic": row["id"]},
            score=1.0,
            summary=(f'Dissolve "{row["name"]}" — still provisional with '
                     f'{row["n"]} note(s) after {cfg.provisional_ttl_days} days'),
        )
        for row in _gate.stale_provisional(conn, cfg)
    ]


# ------------------------------------------------------------------ scan ----
def _signature(kind: str, payload: dict) -> tuple:
    """What a proposal is about, independent of its id and score."""
    if kind == MERGE:
        return (kind, frozenset((payload.get("keep"), payload.get("drop"))))
    if kind == REPARENT:
        return (kind, payload.get("topic"), payload.get("new_parent"))
    return (kind, payload.get("topic"))


def scan(conn: sqlite3.Connection, cfg: Config) -> list[Proposal]:
    """Run every detector and persist the results as pending proposals.

    Each scan replaces the pending set with fresh ids, so a suggestion the
    user dismissed is remembered by what it proposes, not by its id --
    otherwise "Dismiss" would only last until the next scan.
    """
    dismissed = {
        _signature(r["kind"], json.loads(r["payload"]))
        for r in conn.execute(
            "SELECT kind, payload FROM proposals WHERE state = 'dismissed'")
    }
    found = [p for p in (find_merges(conn, cfg) + find_splits(conn, cfg)
                         + find_reparents(conn, cfg) + find_dissolves(conn, cfg))
             if _signature(p.kind, p.payload) not in dismissed]
    conn.execute("DELETE FROM proposals WHERE state = 'pending'")
    for proposal in found:
        conn.execute(
            "INSERT INTO proposals(id, kind, payload, score, state, created_at)"
            " VALUES(?,?,?,?, 'pending', ?)",
            (proposal.id, proposal.kind,
             json.dumps({**proposal.payload, "summary": proposal.summary}),
             proposal.score, now_iso()),
        )
    return found


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM proposals WHERE state = 'pending' ORDER BY score DESC"
    ).fetchall()


def dismiss(conn: sqlite3.Connection, proposal_id: str) -> None:
    conn.execute("UPDATE proposals SET state = 'dismissed' WHERE id = ?", (proposal_id,))


def describe(conn: sqlite3.Connection, kind: str, payload: dict) -> tuple[str, str] | None:
    """A suggestion in the user's words: (summary, detail), names in *italics*.

    Returns None for kinds the app can't carry out yet, so they're not offered.
    """
    def name(topic_id):
        row = T.get(conn, topic_id) if topic_id else None
        return row["name"] if row else None

    def count(topic_id):
        row = T.get(conn, topic_id) if topic_id else None
        n = row["note_count"] if row else 0
        return f"{n} note{'' if n == 1 else 's'}"

    if kind == MERGE:
        keep, drop = name(payload.get("keep")), name(payload.get("drop"))
        if not keep or not drop:
            return None
        return (f"Merge *{drop}* into *{keep}*?",
                f"{drop} has {count(payload['drop'])} that read a lot like your "
                f"{keep} notes.")
    if kind == DISSOLVE:
        topic = name(payload.get("topic"))
        if not topic:
            return None
        row = T.get(conn, payload["topic"])
        home = name(row["parent_id"]) if row else None
        return (f"Fold *{topic}* back into *{home or 'Inbox'}*?",
                f"It only has {count(payload['topic'])} and hasn't grown in a while.")
    return None
