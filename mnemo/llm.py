"""Routing providers.

The router never asks an open question like "what folder should this go in?" --
that is how you get invented topics. It hands over a *menu* of real candidate
topics retrieved by embedding similarity, and the provider's only job is to pick
one, or to say explicitly that none fit.

Two implementations:

* `OfflineRouter`  -- no model at all. Picks the top retrieved candidate when it
  clears the floor and beats the runner-up by a margin. Exercises every
  downstream code path (gate, provisional promotion, journal) deterministically,
  which is what makes the offline test suite meaningful.

* `OllamaRouter`   -- a real local model, constrained to a JSON schema so it is
  structurally incapable of emitting anything but a valid decision.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from .db import _STOPWORDS
from .vecs import _COMMON

FILE = "file"
CREATE_TOPIC = "create_topic"
INBOX = "inbox"

NEW_TOPIC = "NEW_TOPIC"
NONE_FIT = "NONE_FIT"

KINDS = ["link", "video", "article", "image", "task", "idea", "reference",
         "snippet", "note"]


def decision_schema(topic_ids: list[str]) -> dict:
    """Build the response schema around THIS request's real candidate ids.

    Two lessons are baked in here, both learned the hard way with gemma3:4b.

    First, `choice` is an enum of the actual candidate ids plus two sentinels.
    The model is therefore structurally incapable of naming a topic that does
    not exist -- hallucination is impossible rather than merely rejected.

    Second, every field is REQUIRED. An earlier schema marked only `action` and
    `confidence` required, and the model answered `{"action":"inbox",
    "confidence":0.0}` to literally every note -- the shortest string that
    satisfies the grammar. A constrained decoder will find the cheapest legal
    output, so anything optional is effectively an invitation to skip the work.
    """
    return {
        "type": "object",
        "properties": {
            "choice": {"type": "string", "enum": [*topic_ids, NEW_TOPIC, NONE_FIT]},
            "new_topic_name": {"type": "string"},
            "reason": {"type": "string"},
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": KINDS},
            "confidence": {"type": "number"},
        },
        "required": ["choice", "new_topic_name", "reason", "title", "kind",
                     "confidence"],
    }


@dataclass
class Candidate:
    topic_id: str
    path: str
    similarity: float
    note_count: int
    example: str = ""


@dataclass
class SimilarNote:
    note_id: str
    title: str
    similarity: float
    topic_path: str | None


@dataclass
class RouteRequest:
    text: str
    title: str = ""
    kind_hint: str = "note"
    candidates: list[Candidate] = field(default_factory=list)
    similar: list[SimilarNote] = field(default_factory=list)


@dataclass
class RouteDecision:
    action: str = INBOX
    topic_id: str | None = None
    parent_id: str | None = None
    name: str = ""
    rationale: str = ""
    title: str = ""
    kind: str = "note"
    status: str = "open"
    entities: list[str] = field(default_factory=list)
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)


class Router(Protocol):
    name: str

    def route(self, req: RouteRequest) -> RouteDecision: ...


# --------------------------------------------------------------- offline ----
_TITLE_STOP = _STOPWORDS | _COMMON | {
    "this", "that", "with", "from", "into", "about", "have", "just", "some",
    "what", "when", "very", "your", "them", "then", "than", "http", "https",
    "www", "com", "org", "net", "html", "watch", "here", "there", "make",
}


def _salient_terms(text: str, limit: int = 2) -> list[str]:
    """Most frequent meaningful words, for naming a topic without a model."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text.lower())
    counts: dict[str, int] = {}
    for word in words:
        if word in _TITLE_STOP or len(word) < 4:
            continue
        counts[word] = counts.get(word, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [w for w, _ in ranked[:limit]]


def name_from_texts(texts: list[str], limit: int = 2) -> str:
    """Name a CLUSTER from the terms its members share.

    Far better than naming a topic from one note: a term that recurs across
    twenty notes is what the cluster is actually about, whereas a single note's
    salient words are mostly incidental.
    """
    counts: dict[str, int] = {}
    doc_freq: dict[str, int] = {}
    for text in texts:
        seen = set()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", text.lower()):
            if word in _TITLE_STOP or len(word) < 4:
                continue
            counts[word] = counts.get(word, 0) + 1
            seen.add(word)
        for word in seen:
            doc_freq[word] = doc_freq.get(word, 0) + 1
    if not counts:
        return "Notes"
    # Rank by how many DIFFERENT notes use the term, not raw frequency.
    ranked = sorted(counts, key=lambda w: (-doc_freq.get(w, 0), -counts[w], w))
    return " ".join(w.capitalize() for w in ranked[:limit]) or "Notes"


class OfflineRouter:
    """Retrieval-only routing. No model, no network, fully deterministic."""

    name = "offline"

    def __init__(self, file_floor: float = 0.06, min_margin: float = 0.02):
        self.file_floor = file_floor
        self.min_margin = min_margin

    def route(self, req: RouteRequest) -> RouteDecision:
        title = req.title or " ".join(req.text.split()[:8])
        entities = _salient_terms(req.text, 3)

        if req.candidates:
            best = req.candidates[0]
            runner_up = req.candidates[1].similarity if len(req.candidates) > 1 else 0.0
            margin = best.similarity - runner_up
            if best.similarity >= self.file_floor and margin >= self.min_margin:
                # Confidence blends absolute fit with how decisively it won.
                confidence = min(1.0, best.similarity + margin)
                return RouteDecision(
                    action=FILE, topic_id=best.topic_id, title=title,
                    kind=req.kind_hint, entities=entities, confidence=confidence,
                    rationale=f"closest of {len(req.candidates)} candidates",
                )

        terms = _salient_terms(req.text, 2)
        if terms:
            return RouteDecision(
                action=CREATE_TOPIC, name=" ".join(t.capitalize() for t in terms),
                parent_id=None, title=title, kind=req.kind_hint, entities=entities,
                confidence=0.4, rationale="nothing retrieved above the floor",
            )
        return RouteDecision(action=INBOX, title=title, kind=req.kind_hint,
                             confidence=0.0, rationale="no signal")


# ---------------------------------------------------------------- ollama ----
SYSTEM_PROMPT = """You sort notes into folders. You are given a note and a
numbered list of existing folders, and you pick the one it belongs in.

Almost every note belongs in one of the listed folders. Pick that folder.

Use NEW_TOPIC only if the note is about a subject none of the folders cover,
and give new_topic_name a short name (1-3 words).
Use NONE_FIT only if the note is too vague to be about anything.

Fill in every field:
  choice           the folder id, or NEW_TOPIC, or NONE_FIT
  new_topic_name   a name if you chose NEW_TOPIC, otherwise ""
  reason           a few words on why
  title            a short title for the note
  kind             what the note is
  confidence       0.0 to 1.0, how sure you are"""


def build_prompt(req: RouteRequest) -> str:
    """The note goes FIRST.

    With the instructions and folder list ahead of it, a 4B model tends to
    answer about the list rather than about the note.
    """
    lines = ["NOTE:", req.text[:3000], "", "FOLDERS:"]
    if req.candidates:
        for c in req.candidates:
            example = f' — e.g. "{c.example}"' if c.example else ""
            lines.append(f"  {c.topic_id} = {c.path} ({c.note_count} notes){example}")
    else:
        lines.append("  (none yet)")

    if req.similar:
        lines.append("")
        lines.append("Similar notes already filed:")
        for s in req.similar:
            lines.append(f'  "{s.title}" -> {s.topic_path}')

    lines += ["", "Which folder does the NOTE belong in?"]
    return "\n".join(lines)


class OllamaRouter:
    """Schema-constrained routing against a local Ollama model.

    Verified by `python -m mnemo.doctor`, which asserts that responses parse as
    JSON every time and that the model picks a topic_id from the supplied menu
    rather than inventing one.
    """

    def __init__(self, model: str | None = None, host: str | None = None,
                 timeout: int = 120):
        from .models import ROUTER_MODEL
        from .paths import OLLAMA_HOST
        self.model = model or ROUTER_MODEL
        self.host = (host or OLLAMA_HOST).rstrip("/")
        self.timeout = timeout
        self.name = f"ollama:{self.model}"

    def route(self, req: RouteRequest) -> RouteDecision:
        import urllib.request

        from .runtime import think_option

        ids = [c.topic_id for c in req.candidates]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(req)},
            ],
            "format": decision_schema(ids),   # ids are an enum, not free text
            "stream": False,
            "keep_alive": -1,                 # never swap the model out
            "options": {"temperature": 0.0},
            **think_option(self.model, False),   # filing must stay fast
        }
        request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            body = json.loads(resp.read())
        data = json.loads(body["message"]["content"])

        choice = data.get("choice") or NONE_FIT
        title = data.get("title") or req.title
        kind = data.get("kind") or req.kind_hint
        confidence = float(data.get("confidence", 0.0))
        reason = data.get("reason", "")

        if choice in ids:
            return RouteDecision(action=FILE, topic_id=choice, title=title,
                                 kind=kind, confidence=confidence,
                                 rationale=reason, raw=data)
        if choice == NEW_TOPIC and (data.get("new_topic_name") or "").strip():
            return RouteDecision(action=CREATE_TOPIC,
                                 name=data["new_topic_name"].strip(),
                                 title=title, kind=kind, confidence=confidence,
                                 rationale=reason, raw=data)
        return RouteDecision(action=INBOX, title=title, kind=kind,
                             confidence=confidence, rationale=reason, raw=data)
