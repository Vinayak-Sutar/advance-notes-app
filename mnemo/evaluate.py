"""Measured quality, not vibes.

Run:  python -m mnemo.evaluate            (200 notes, quick)
      python -m mnemo.evaluate 500        (the full anti-sprawl stress test)

Two questions, both answered numerically:

1. ROUTING -- when a note arrives, does it end up with the other notes that
   belong with it?  Measured as cluster purity against known ground truth,
   because the router invents its own topic names and comparing those to the
   corpus's names would measure naming, not filing.

2. SPRAWL  -- does the taxonomy stay the size it should be?  500 notes drawn
   from 10 topics, phrased with deliberately inconsistent vocabulary, must not
   produce 40 folders.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from collections import Counter, defaultdict

from . import gardener as Gd
from . import notes as N
from . import topics as T
from .corpus import expected_topic_count, generate
from .engine import Engine


def purity(assignments: list[tuple[str, str]]) -> tuple[float, int]:
    """assignments: [(discovered_topic, ground_truth_topic), ...]

    Weighted mean over discovered topics of "what fraction of this topic's
    notes share the same true topic". 1.0 means every discovered topic is
    internally consistent, even if its name is machine-generated.
    """
    groups: dict[str, list[str]] = defaultdict(list)
    for discovered, truth in assignments:
        groups[discovered].append(truth)
    if not groups:
        return 0.0, 0
    total = sum(len(v) for v in groups.values())
    correct = sum(Counter(v).most_common(1)[0][1] for v in groups.values())
    return correct / total, len(groups)


def coverage(assignments: list[tuple[str, str]]) -> float:
    """Inverse view: how often do notes of one TRUE topic get scattered across
    many discovered topics? 1.0 = each true topic lives in a single place."""
    groups: dict[str, list[str]] = defaultdict(list)
    for discovered, truth in assignments:
        groups[truth].append(discovered)
    if not groups:
        return 0.0
    scores = [Counter(v).most_common(1)[0][1] / len(v) for v in groups.values()]
    return sum(scores) / len(scores)


def run(n: int = 200, seed: int = 7, verbose: bool = True,
        online: bool | None = False) -> dict:
    root = tempfile.mkdtemp(prefix="mnemo-eval-")
    engine = Engine(root, enrich=False, online=online)
    samples = generate(n, seed=seed)

    outcomes = Counter()
    assignments: list[tuple[str, str]] = []
    try:
        for i, sample in enumerate(samples):
            result = engine.capture(sample.text)
            outcomes[result.outcome] += 1
            if result.topic_path:
                assignments.append((result.topic_path, sample.topic))
            if verbose and (i + 1) % 50 == 0:
                print(f"  ...{i + 1}/{n} captured", flush=True)

        # The gardener's compaction pass: does it spot the duplicate topics
        # that slipped past the gate as the vault grew?
        proposals = Gd.scan(engine.conn, engine.cfg)
        merges = [p for p in proposals if p.kind == Gd.MERGE]

        active = [r for r in T.all_active(engine.conn) if r["state"] == "active"]
        provisional = [r for r in T.all_active(engine.conn) if r["state"] == "provisional"]
        pure, discovered = purity(assignments)

        stats = {
            "notes": n,
            "embedder": engine.embedder.name,
            "router": engine.provider.name,
            "expected_topics": expected_topic_count(),
            "active_topics": len(active),
            "provisional_topics": len(provisional),
            "discovered_used": discovered,
            "purity": round(pure, 4),
            "coverage": round(coverage(assignments), 4),
            "filed": outcomes["filed"],
            "review": outcomes["review"],
            "inbox": outcomes["inbox"],
            "inbox_rate": round(outcomes["inbox"] / n, 4),
            "unfiled": len(N.inbox(engine.conn)),
            "topic_names": sorted(r["name"] for r in active),
            "merge_proposals": [p.summary for p in merges],
            "split_proposals": [p.summary for p in proposals if p.kind == Gd.SPLIT],
        }
        return stats
    finally:
        engine.close()
        shutil.rmtree(root, ignore_errors=True)


def main() -> None:
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    online = "--online" in sys.argv
    n = int(positional[0]) if positional else 200
    print(f"routing {n} notes from {expected_topic_count()} known topics "
          f"through a cold vault{' with local models' if online else ''}...\n")
    stats = run(n, online=True if online else False)

    print(f"\n  stack                {stats['embedder']} / {stats['router']}")
    print("\n--- taxonomy ---")
    print(f"  expected topics      {stats['expected_topics']}")
    print(f"  active topics        {stats['active_topics']}")
    print(f"  provisional topics   {stats['provisional_topics']}  (no folder on disk)")
    print(f"  names                {', '.join(stats['topic_names'][:12])}")

    print("\n--- routing quality ---")
    print(f"  purity               {stats['purity']:.1%}  "
          "(notes sharing a topic really do belong together)")
    print(f"  coverage             {stats['coverage']:.1%}  "
          "(a true topic lands in one place, not scattered)")

    print("\n--- gardener compaction ---")
    if stats["merge_proposals"]:
        for line in stats["merge_proposals"][:8]:
            print(f"  MERGE  {line}")
    else:
        print("  no merges proposed")
    for line in stats["split_proposals"][:4]:
        print(f"  SPLIT  {line}")

    print("\n--- outcomes ---")
    for key in ("filed", "review", "inbox"):
        print(f"  {key:20} {stats[key]:4}  ({stats[key] / stats['notes']:.1%})")
    print(f"  unfiled in Inbox     {stats['unfiled']:4}")


if __name__ == "__main__":
    main()
