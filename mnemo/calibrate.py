"""Derive routing thresholds from measured data instead of guessing them.

Run:  python -m mnemo.calibrate

Cosine values are not comparable across embedders -- a 0.25 that means
"unrelated" for a trained model can mean "strongly related" for feature
hashing. So rather than hardcode the plan's 0.86, this measures the actual
distribution of same-topic and different-topic note pairs and places the
thresholds between them.
"""
from __future__ import annotations

import statistics as stats

from .corpus import generate
from .vecs import HashingEmbedder, cosine
from .vecs import mean as vec_mean


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))
    return ordered[idx]


def _embed_all(embedder, texts: list[str], batch: int = 64) -> list:
    """Embed in batches. A remote embedder charges a round trip per call, so
    one-at-a-time turns a 30-second calibration into several minutes."""
    if hasattr(embedder, "embed_one") and not hasattr(embedder, "model"):
        return [embedder.embed_one(t) for t in texts]
    out = []
    for start in range(0, len(texts), batch):
        out.extend(embedder.embed(texts[start:start + batch]))
    return out


def measure(embedder=None, n: int = 400, seed: int = 7, train_frac: float = 0.6) -> dict:
    """Measure the distribution the ROUTER actually sees.

    Routing compares a note against topic CENTROIDS, not against individual
    notes. A centroid aggregates a topic's whole vocabulary, so it matches a
    new note far more reliably than any single sibling note would. Calibrating
    on note-to-note pairs measures a problem the router never solves, and
    produces thresholds that are far too high.
    """
    embedder = embedder or HashingEmbedder()
    samples = generate(n, seed=seed)
    vectors = _embed_all(embedder, [s.text for s in samples])

    # Split each topic into centroid-builders and held-out queries.
    by_topic: dict[str, list[int]] = {}
    for idx, sample in enumerate(samples):
        by_topic.setdefault(sample.topic, []).append(idx)

    centroids: dict[str, list[float]] = {}
    queries: list[tuple[int, str]] = []
    for topic, idxs in by_topic.items():
        cut = max(1, int(len(idxs) * train_frac))
        centroids[topic] = vec_mean([vectors[i] for i in idxs[:cut]])
        queries.extend((i, topic) for i in idxs[cut:])

    same: list[float] = []
    diff: list[float] = []
    for idx, true_topic in queries:
        for topic, centroid in centroids.items():
            sim = cosine(vectors[idx], centroid)
            (same if topic == true_topic else diff).append(sim)

    # Retrieval accuracy: does the true topic actually rank first?
    hits = top3 = 0
    for idx, true_topic in queries:
        ranked = sorted(((cosine(vectors[idx], c), t) for t, c in centroids.items()),
                        reverse=True)
        if ranked and ranked[0][1] == true_topic:
            hits += 1
        if true_topic in [t for _, t in ranked[:3]]:
            top3 += 1
    accuracy = {"top1": hits / len(queries), "top3": top3 / len(queries),
                "queries": len(queries)}

    return {
        "embedder": embedder.name,
        "accuracy": accuracy,
        "pairs": {"same": len(same), "diff": len(diff)},
        "same": {"mean": stats.mean(same), "p10": percentile(same, 0.10),
                 "p50": percentile(same, 0.50), "p90": percentile(same, 0.90)},
        "diff": {"mean": stats.mean(diff), "p50": percentile(diff, 0.50),
                 "p95": percentile(diff, 0.95), "p99": percentile(diff, 0.99),
                 "max": max(diff)},
        "_same": same,
        "_diff": diff,
    }


def derive(m: dict) -> dict:
    """Place thresholds BETWEEN the two measured distributions.

    An earlier version multiplied the noise floor (`diff_p99 * 3` and friends).
    That happened to work for the hashing embedder, whose unrelated pairs sit
    near 0.05 -- but nomic-embed-text puts unrelated text at ~0.56, and the same
    formula produced a dedupe threshold of 1.97 and a review floor of 1.05.
    Cosine cannot exceed 1.0, so those thresholds could never fire and the whole
    gate would have silently stopped refusing anything.

    Every value here is now interpolated within the gap between the
    distributions and clamped to [0, 1], which is correct for any embedder
    regardless of where its noise floor sits.
    """
    diff_p99, diff_p50 = m["diff"]["p99"], m["diff"]["p50"]
    same_p10, same_p50, same_p90 = m["same"]["p10"], m["same"]["p50"], m["same"]["p90"]

    def between(low: float, high: float, frac: float) -> float:
        return round(max(0.0, min(1.0, low + (high - low) * frac)), 3)

    # The usable gap: above the noise, below where real matches live.
    lo, hi = diff_p99, max(same_p10, diff_p99 + 1e-6)
    return {
        # Below the noise ceiling nothing is distinguishable from chance.
        "file_floor": between(lo, hi, 0.0),
        # Far enough into the gap that filing silently is defensible.
        "review_floor": between(lo, hi, 0.6),
        # Two topics this alike are one topic.
        "dedupe_threshold": round(min(1.0, same_p90), 3),
        "related_edge_floor": round(min(1.0, same_p50), 3),
        # Margin is a SPREAD, not an absolute similarity: how much the winner
        # must beat the runner-up. Scaled to the separation actually observed.
        "min_margin": round(max(0.01, min(0.25, (same_p50 - diff_p50) * 0.12)), 3),
    }


def measure_names(embedder=None, n: int = 400, seed: int = 7) -> dict:
    """Calibrate the GATE's two axes, which are distributions of their own.

    When the router proposes a topic name, the gate compares it against:
      - name axis:     existing topic NAME embeddings  (short string vs short string)
      - centroid axis: existing topic CONTENT centroids (short string vs long text)

    These score very differently for the same true duplicate, so a single
    dedupe threshold cannot serve both.
    """
    from .corpus import TOPICS, alias_stream

    embedder = embedder or HashingEmbedder()
    samples = generate(n, seed=seed)

    canonical = {path: path.split("/")[-1] for path in TOPICS}
    paths = list(TOPICS)
    name_list = _embed_all(embedder, [canonical[p] for p in paths])
    name_vecs = dict(zip(paths, name_list))
    centroids: dict[str, list[float]] = {}
    for path in paths:
        texts = [s.text for s in samples if s.topic == path]
        centroids[path] = vec_mean(_embed_all(embedder, texts))

    axes = {"name": ([], []), "centroid": ([], [])}
    stream = alias_stream(120, seed=seed)
    alias_vecs = _embed_all(embedder, [a for a, _ in stream])
    for (alias, true_path), avec in zip(stream, alias_vecs):
        for path in paths:
            bucket = 0 if path == true_path else 1
            axes["name"][bucket].append(cosine(avec, name_vecs[path]))
            axes["centroid"][bucket].append(cosine(avec, centroids[path]))

    out = {}
    for axis, (same, diff) in axes.items():
        out[axis] = {
            "same_p50": percentile(same, 0.50), "same_p25": percentile(same, 0.25),
            "diff_p99": percentile(diff, 0.99), "diff_max": max(diff),
            "suggested": round(max(percentile(diff, 0.99) * 1.05,
                                   percentile(same, 0.25)), 3),
        }
    return out


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="mnemo.calibrate", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--online", action="store_true",
                        help="calibrate the real embedder instead of the offline one")
    parser.add_argument("-n", type=int, default=400, help="corpus size")
    args = parser.parse_args()

    embedder = None
    if args.online:
        from . import runtime
        from .models import EMBED_MODEL
        from .vecs import OllamaEmbedder
        if not (runtime.start() and runtime.has_model(EMBED_MODEL)):
            raise SystemExit("local models unavailable; run: python -m mnemo.setup")
        embedder = OllamaEmbedder()
        print(f"calibrating {embedder.name} "
              f"(this issues one embed call per corpus note)\n")

    m = measure(embedder, n=args.n)
    print(f"embedder: {m['embedder']}")
    print(f"queries:  {m['accuracy']['queries']} held-out notes vs {len(m['_diff'])//m['accuracy']['queries']+1} topic centroids")
    print(f"RETRIEVAL top-1 {m['accuracy']['top1']:.1%}   top-3 {m['accuracy']['top3']:.1%}")
    print()
    print("  same-topic   mean %.3f   p10 %.3f  p50 %.3f  p90 %.3f"
          % (m["same"]["mean"], m["same"]["p10"], m["same"]["p50"], m["same"]["p90"]))
    print("  diff-topic   mean %.3f   p50 %.3f  p95 %.3f  p99 %.3f  max %.3f"
          % (m["diff"]["mean"], m["diff"]["p50"], m["diff"]["p95"],
             m["diff"]["p99"], m["diff"]["max"]))

    overlap = sum(1 for d in m["_diff"] if d >= m["same"]["p10"]) / len(m["_diff"])
    print(f"\n  different-topic pairs above the same-topic p10: {overlap:.2%}"
          "   (lower is better separation)")

    print("\nderived thresholds (routing):")
    for key, value in derive(m).items():
        print(f"  {key:26} {value}")

    print("\ngate axes (proposed topic name vs existing topics):")
    for axis, stat in measure_names(embedder, n=args.n).items():
        print(f"  {axis + ' axis':16} true-dupe p25 {stat['same_p25']:.3f}"
              f"  p50 {stat['same_p50']:.3f}  |  non-dupe p99 {stat['diff_p99']:.3f}"
              f"  max {stat['diff_max']:.3f}  ->  {stat['suggested']}")


if __name__ == "__main__":
    main()
