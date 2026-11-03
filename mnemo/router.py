"""Routing orchestration: retrieve -> decide -> validate -> gate -> tier.

This is the module that turns "the LLM picks a folder" into something with
guardrails on every side of the model:

    retrieval   builds a menu of REAL topics, so nothing has to be invented
    validation  a topic_id that was not on the menu is rejected outright
    gate        a proposed new topic must survive six duplicate checks
    tiering     low-confidence results land in Inbox instead of being guessed

Confidence is deliberately MARGIN-based rather than a raw similarity cutoff.
Absolute cosine values are scale-dependent (0.19 is a strong match for the
hashing embedder, a weak one for nomic), but "the winner beat the runner-up by
a clear gap" means the same thing on any scale.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from . import gate as G
from . import notes as N
from . import topics as T
from .config import Config
from .db import now_iso
from .llm import (CREATE_TOPIC, FILE, INBOX, Candidate, RouteDecision,
                  RouteRequest, Router, SimilarNote)
from .vecs import Vector, top_k

FILED = "filed"
REVIEW = "review"
TO_INBOX = "inbox"


@dataclass
class RoutingOutcome:
    outcome: str                      # filed | review | inbox
    topic_id: str | None = None
    topic_path: str | None = None
    confidence: float = 0.0
    decision: RouteDecision | None = None
    gate: G.GateDecision | None = None
    candidates: list[Candidate] = field(default_factory=list)
    similar: list[SimilarNote] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # human-readable trace

    @property
    def needs_review(self) -> bool:
        return self.outcome == REVIEW


# ------------------------------------------------------------- retrieval ----
def retrieve(conn: sqlite3.Connection, vec: Vector, cfg: Config,
             exclude_note: str | None = None
             ) -> tuple[list[Candidate], list[SimilarNote]]:
    """Build the candidate menu the provider is allowed to choose from."""
    candidates: list[Candidate] = []
    for topic_id, sim in top_k(vec, T.centroids(conn), k=cfg.topic_candidates):
        row = T.get(conn, topic_id)
        if row is None:
            continue
        example = conn.execute(
            "SELECT n.title FROM notes n JOIN note_topics nt ON nt.note_id = n.id"
            " WHERE nt.topic_id = ? AND n.title <> '' ORDER BY n.created_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        candidates.append(Candidate(
            topic_id=topic_id,
            path=T.path_of(conn, topic_id) or row["name"],
            similarity=sim,
            note_count=row["note_count"],
            example=example["title"] if example else "",
        ))

    similar: list[SimilarNote] = []
    for note_id, sim in top_k(vec, N.all_vectors(conn, exclude=exclude_note),
                              k=cfg.similar_notes):
        row = N.get(conn, note_id)
        if row is None:
            continue
        similar.append(SimilarNote(
            note_id=note_id, title=row["title"], similarity=sim,
            topic_path=T.path_of(conn, N.primary_topic(conn, note_id)),
        ))
    return candidates, similar


# ---------------------------------------------------------------- routing ----
def route(conn: sqlite3.Connection, provider: Router, cfg: Config, vec: Vector,
          text: str, title: str = "", kind_hint: str = "note",
          embed_name=None, exclude_note: str | None = None,
          allow_new_topics: bool = True) -> RoutingOutcome:
    """Decide where one note belongs. Does NOT write the note or move files --
    that is the engine's job. This function is pure decision-making so it can
    be replayed offline by the eval harness.

    `embed_name` is a callable used to embed a *proposed topic name* for the
    gate's duplicate check; it is a separate embedding from the note's own.
    """
    candidates, similar = retrieve(conn, vec, cfg, exclude_note=exclude_note)
    req = RouteRequest(text=text, title=title, kind_hint=kind_hint,
                       candidates=candidates, similar=similar)
    trace: list[str] = []

    best_sim = candidates[0].similarity if candidates else 0.0
    runner_up = candidates[1].similarity if len(candidates) > 1 else 0.0
    margin = best_sim - runner_up

    # Skip the model entirely when retrieval has already decided.
    #
    # Its answer would be discarded for placement anyway (trust_retrieval), and
    # a local 4B model costs ~4s per call against ~30ms for an embedding. That
    # is the difference between capture feeling instant and feeling broken, on
    # the overwhelmingly common path. The model is still called when it has
    # something real to contribute: naming a topic that does not exist yet.
    retrieval_confident = (
        cfg.trust_retrieval and candidates
        and best_sim >= cfg.review_floor and margin >= cfg.min_margin
    )
    if retrieval_confident:
        decision = RouteDecision(
            action=FILE, topic_id=candidates[0].topic_id, title=title,
            kind=kind_hint, confidence=min(1.0, best_sim + margin),
            rationale="retrieval confident; model not consulted",
        )
        trace.append(f"model skipped (retrieval {best_sim:.2f}, "
                     f"margin {margin:.2f})")
    else:
        decision = provider.route(req)

    topic_id: str | None = None
    gate_decision: G.GateDecision | None = None

    if decision.action == FILE:
        # The provider may only choose from the menu it was given. Anything
        # else is a hallucinated id and is refused rather than trusted.
        allowed = {c.topic_id for c in candidates}
        if decision.topic_id in allowed:
            topic_id = decision.topic_id
        else:
            trace.append(f"rejected off-menu topic_id {decision.topic_id!r}")
            decision.action = INBOX

    elif decision.action == CREATE_TOPIC and not allow_new_topics:
        # Still warming up. Creating a topic from a single note's incidental
        # wording is how vaults end up with "Abc123 Databases"; hold it in
        # Inbox until there is enough material to name a topic from.
        trace.append("warm-up: new topics suppressed")
        decision.action = INBOX

    elif decision.action == CREATE_TOPIC:
        # Embed the NAME ALONE. Appending the rationale here was a real bug:
        # a provider that emits boilerplate rationale ("nothing matched") makes
        # every proposal vector nearly identical, and the gate then merges
        # unrelated topics into whichever one happened to be created first.
        name_vec = embed_name(decision.name) if embed_name else vec
        gate_decision = G.propose_topic(conn, decision.name, decision.parent_id,
                                        name_vec, cfg)
        trace.append(f"gate: {gate_decision.reason}")
        if gate_decision.action in ("reuse", "create"):
            topic_id = gate_decision.topic_id
        else:
            # Rejected by a shape cap: fall back to the parent it wanted to sit
            # under, rather than inventing somewhere else.
            topic_id = gate_decision.topic_id

    # ---- tiering ----
    # "inbox" must mean genuinely unfiled. Leaving a topic_id attached to an
    # inbox outcome silently files the note anyway and empties the triage queue.
    if topic_id is None:
        outcome = TO_INBOX
    elif gate_decision is not None and gate_decision.created:
        # A brand-new topic is never a silent decision.
        outcome = REVIEW
        trace.append("new topic -> flagged for review")
    elif best_sim >= cfg.review_floor and margin >= cfg.min_margin:
        outcome = FILED
    elif best_sim >= cfg.file_floor:
        outcome = REVIEW
    else:
        outcome = TO_INBOX

    if outcome == TO_INBOX:
        topic_id = None

    confidence = round(min(1.0, best_sim + margin), 4) if candidates else decision.confidence

    return RoutingOutcome(
        outcome=outcome,
        topic_id=topic_id,
        topic_path=T.path_of(conn, topic_id),
        confidence=confidence,
        decision=decision,
        gate=gate_decision,
        candidates=candidates,
        similar=similar,
        notes=trace,
    )


def log(conn: sqlite3.Connection, note_id: str, outcome: RoutingOutcome) -> None:
    """Record what we offered and what was chosen. This is the signal that
    later becomes few-shot exemplars and centroid nudges."""
    conn.execute(
        "INSERT INTO routing_log(note_id, candidates, chosen, action, confidence,"
        " outcome, ts) VALUES(?,?,?,?,?,?,?)",
        (
            note_id,
            json.dumps([
                {"topic_id": c.topic_id, "path": c.path,
                 "sim": round(c.similarity, 4), "n": c.note_count}
                for c in outcome.candidates
            ]),
            outcome.topic_id,
            outcome.decision.action if outcome.decision else INBOX,
            outcome.confidence,
            outcome.outcome,
            now_iso(),
        ),
    )


def record_correction(conn: sqlite3.Connection, note_id: str, topic_id: str) -> None:
    """The user moved a note. Mark the most recent routing decision for it as
    corrected, so the router can learn from the disagreement."""
    conn.execute(
        "UPDATE routing_log SET user_corrected_to = ?"
        " WHERE id = (SELECT id FROM routing_log WHERE note_id = ?"
        "             ORDER BY id DESC LIMIT 1)",
        (topic_id, note_id),
    )
