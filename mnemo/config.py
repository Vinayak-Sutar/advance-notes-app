"""Tunables, and why they differ per embedder.

The plan quoted a 0.86 centroid-dedupe threshold. That number is calibrated for
a trained sentence embedder (nomic-embed-text), where unrelated text still
scores ~0.5 and near-duplicates score ~0.9. The offline `HashingEmbedder` has a
completely different scale -- measured on this machine, same-topic pairs average
0.44 and different-topic pairs average 0.01.

So thresholds are a property of the embedder, not a global constant. Hardcoding
0.86 would mean the gate never refuses anything in offline mode, and the vault
would sprawl exactly as feared.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Config:
    # -- gate --
    # Two axes, two thresholds. Measured on this machine: for a true duplicate
    # topic name, similarity to the existing topic's NAME embedding has median
    # 0.672, but to its content CENTROID only 0.077 -- lower than the 0.216 that
    # unrelated names reach. One shared threshold would either miss every real
    # duplicate or merge unrelated topics.
    dedupe_threshold: float          # name-vs-name axis
    dedupe_centroid_threshold: float = 0.99   # name-vs-content axis
    min_mass: int = 3         # notes a provisional topic needs before it gets a folder
    # Cold start: an empty vault has no centroids to retrieve against, so the
    # first notes would each invent a topic from their own incidental wording.
    # Instead they queue in Inbox until there is enough data to cluster.
    cold_start_notes: int = 20
    max_depth: int = 3
    max_children: int = 12
    provisional_ttl_days: int = 30

    # -- routing confidence --
    # Absolute similarity is scale-dependent, so the primary signal is the
    # MARGIN between the best candidate and the runner-up. A clear winner is a
    # confident decision even when the absolute number is small.
    file_floor: float = 0.10      # below this, nothing is similar enough to file
    review_floor: float = 0.20    # below this, file but flag needs_review
    min_margin: float = 0.03      # winner must beat runner-up by this much

    # When retrieval is confident, its pick WINS over the model's.
    #
    # Measured on 40 held-out notes with gemma3:4b choosing from retrieval's
    # top 5: retrieval alone was right 40/40 (100%), the model 34/40 (85%) --
    # and every single deviation from retrieval's rank-1 pick was a mistake.
    # Small models also show strong position bias, defaulting to the first
    # option in the list.
    #
    # So the model does what it is good at (naming a new topic, writing a
    # title, classifying kind) and does not do what it is measurably bad at
    # (overruling a similarity ranking). This is the plan's own division of
    # labour -- the maths finds the structure, the model names it.
    trust_retrieval: bool = True

    # -- retrieval --
    topic_candidates: int = 8
    similar_notes: int = 5
    related_edge_floor: float = 0.25   # auto-link notes at least this similar
    # Chat: a question must be at least this similar to a note for the note to
    # be shown to the model. Questions and notes are a fifth distribution;
    # measured with nomic, relevant pairs scored 0.48-0.83 and unrelated ones
    # 0.29-0.46, so 0.45 keeps "capital of Japan?" from citing your grocery list.
    chat_floor: float = 0.45

    # -- gardener --
    # Centroid-vs-centroid: a third distribution again, and measured on a real
    # 200-note vault rather than assumed. Topic pairs that were genuinely the
    # same subject scored 0.18-0.41; genuinely distinct pairs topped out at
    # 0.23. The distributions overlap, so 0.28 is chosen to catch 3 of 4 real
    # duplicates while proposing ZERO false merges -- a false merge destroys
    # structure, a missed one just leaves an extra folder for the next pass.
    merge_threshold: float = 0.28
    split_min_notes: int = 40
    split_min_separation: float = 0.35   # how distinct the two clusters must be

    name: str = "custom"

    @property
    def effective_merge_threshold(self) -> float:
        return self.merge_threshold or self.dedupe_threshold


# Measured, not guessed. `python -m mnemo.calibrate` re-derives all of these
# against the synthetic corpus; the numbers below are that run's output.
#
#   retrieval   top-1 98.8%, top-3 100% on held-out notes vs topic centroids
#   same-topic  mean 0.450 (p10 0.292)
#   diff-topic  mean 0.051 (p99 0.231)
#
# Known offline limitation: ~25% of true synonym pairs ("Souls Game" for
# "Elden Ring", "ML" for "AI") share no characters and score ~0 on the name
# axis, so the gate cannot catch them. Real embeddings close that gap; until
# then those land as separate topics and the gardener's merge pass is what
# cleans them up.
OFFLINE = Config(
    name="offline-hashing",
    dedupe_threshold=0.25,             # name axis: non-dupe p99 is 0.201
    dedupe_centroid_threshold=0.35,    # centroid axis: non-dupe max is 0.326
    file_floor=0.23,
    review_floor=0.37,
    min_margin=0.067,
    related_edge_floor=0.46,
    chat_floor=0.15,                   # character n-grams score everything lower
)

# MEASURED against nomic-embed-text on this machine -- `python -m mnemo.calibrate
# --online` reproduces it. These replace the plan's assumed numbers, which were
# badly wrong: the plan put file_floor at 0.45, but nomic scores *unrelated*
# text at 0.557 on average, so 0.45 was below the noise floor and would have
# filed everything into whatever ranked first.
#
#   retrieval   top-1 99.2%, top-3 100%
#   same-topic  mean 0.754 (p10 0.682, p90 0.825)
#   diff-topic  mean 0.557 (p99 0.656, max 0.710)
#
# The name axis is transformed by real embeddings: a true duplicate name scores
# p25 0.647 / p50 0.855, where the offline embedder scored p25 0.000. That is
# what finally catches "Souls Game" for Elden Ring.
ONLINE = Config(
    name="ollama-nomic",
    # Above the observed non-duplicate maximum (0.724), so no false merges.
    dedupe_threshold=0.73,
    # The centroid axis stays nearly useless even with real embeddings: true
    # duplicates score p50 0.612 while unrelated names reach 0.628. The
    # distributions overlap, so this is set high enough to almost never fire
    # rather than allowed to merge unrelated topics.
    dedupe_centroid_threshold=0.75,
    # This floor leaves ~39% of the synthetic corpus in Inbox, which looks high
    # until you measure the alternative. Sweeping it over 500 notes:
    #
    #   file_floor   topics   purity   inbox
    #     0.656        12      77.6%    178
    #     0.630        14      64.2%    101
    #     0.600        14      57.9%    101
    #     0.560         7      19.8%     19
    #
    # Loosening it to empty the Inbox collapses purity to chance (1/7 topics).
    # A high Inbox rate is the system declining to guess, which is the whole
    # point -- unfiled is recoverable in one click, misfiled is not.
    file_floor=0.656,
    review_floor=0.672,
    min_margin=0.024,
    related_edge_floor=0.76,
    # UNMEASURED for nomic: centroid-vs-centroid is a fourth distribution and
    # needs a populated vault to sample. Set conservatively high so the gardener
    # under-proposes rather than proposing a destructive merge.
    merge_threshold=0.88,
)


def for_embedder(embedder) -> Config:
    return ONLINE if getattr(embedder, "name", "").startswith("ollama") else OFFLINE
