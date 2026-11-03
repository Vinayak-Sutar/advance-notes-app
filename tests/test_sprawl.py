"""The anti-sprawl stress test.

This is the executable form of the actual requirement: "the LLM should create
folders freely, but we need to build a system so that we don't face issues."

500 notes drawn from 10 known topics, phrased with deliberately inconsistent
vocabulary, must not produce forty folders. It is slow (pure-Python embedding
of every note plus brute-force similarity over a growing corpus), so it is
skipped unless MNEMO_SLOW=1.

Run:  MNEMO_SLOW=1 python -m unittest tests.test_sprawl
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import gardener as Gd
from mnemo import gate as G
from mnemo import topics as T
from mnemo.config import OFFLINE
from mnemo.corpus import alias_stream, expected_topic_count
from mnemo.engine import Engine
from mnemo.evaluate import run
from mnemo.vecs import HashingEmbedder

SLOW = os.environ.get("MNEMO_SLOW") == "1"


class TestGateUnderVocabularyDrift(unittest.TestCase):
    """The gate alone, hit with 120 drifting topic names. Fast, always runs."""

    def test_alias_stream_does_not_explode_the_taxonomy(self):
        from mnemo.db import connect

        conn = connect(os.path.join(tempfile.mkdtemp(), "i.db"))
        embedder = HashingEmbedder()
        created = 0
        for name, _truth in alias_stream(120, seed=11):
            decision = G.propose_topic(conn, name, None,
                                       embedder.embed_one(name), OFFLINE)
            if decision.created:
                created += 1

        active = T.all_active(conn)
        # 120 proposals covering 10 real subjects. Without the gate this is
        # 120 folders; the target is that it stays in the same order of
        # magnitude as the truth.
        self.assertLessEqual(len(active), 30,
                             f"taxonomy sprawled to {len(active)}: "
                             f"{[r['name'] for r in active]}")
        self.assertGreaterEqual(len(active), 5,
                                "gate collapsed everything into too few topics")

    def test_repeated_identical_proposals_create_once(self):
        from mnemo.db import connect

        conn = connect(os.path.join(tempfile.mkdtemp(), "i.db"))
        embedder = HashingEmbedder()
        for _ in range(25):
            G.propose_topic(conn, "Sourdough Baking", None,
                            embedder.embed_one("Sourdough Baking"), OFFLINE)
        self.assertEqual(len(T.all_active(conn)), 1)


class TestFullPipelineSprawl(unittest.TestCase):
    @unittest.skipUnless(SLOW, "set MNEMO_SLOW=1 to run the 500-note stress test")
    def test_500_notes_stay_within_topic_budget(self):
        stats = run(500, seed=7, verbose=False)
        expected = expected_topic_count()

        self.assertLessEqual(
            stats["active_topics"], expected + 8,
            f"taxonomy sprawled to {stats['active_topics']} topics "
            f"(expected ~{expected}): {stats['topic_names']}")
        self.assertGreaterEqual(
            stats["active_topics"], 4,
            "everything collapsed into too few topics")
        self.assertGreaterEqual(
            stats["purity"], 0.60,
            f"notes filed together do not belong together (purity "
            f"{stats['purity']:.1%})")

    @unittest.skipUnless(SLOW, "set MNEMO_SLOW=1 to run the 200-note pipeline test")
    def test_200_notes_quality_floor(self):
        stats = run(200, seed=7, verbose=False)
        self.assertGreaterEqual(stats["purity"], 0.60)
        self.assertLessEqual(stats["active_topics"], expected_topic_count() + 6)


class TestGardenerFindsStructure(unittest.TestCase):
    @unittest.skipUnless(SLOW, "set MNEMO_SLOW=1")
    def test_gardener_proposes_only_defensible_merges(self):
        """A false merge destroys structure, so the bar is zero false merges
        rather than maximum recall."""
        from collections import Counter

        from mnemo.corpus import generate

        engine = Engine(tempfile.mkdtemp(), enrich=False)
        truth: dict[str, list[str]] = {}
        try:
            for sample in generate(200, seed=7):
                result = engine.capture(sample.text)
                if result.routing and result.routing.topic_id:
                    truth.setdefault(result.routing.topic_id, []).append(sample.topic)

            dominant = {tid: Counter(v).most_common(1)[0][0]
                        for tid, v in truth.items() if v}
            merges = [p for p in Gd.scan(engine.conn, engine.cfg)
                      if p.kind == Gd.MERGE]

            false_merges = [
                p for p in merges
                if dominant.get(p.payload["keep"]) != dominant.get(p.payload["drop"])
                and p.payload["keep"] in dominant and p.payload["drop"] in dominant
            ]
            self.assertEqual(
                false_merges, [],
                "gardener proposed merging genuinely different topics: "
                + "; ".join(p.summary for p in false_merges))
        finally:
            engine.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
