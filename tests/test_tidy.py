"""Accepting Tidy-up suggestions, the organise setting, and blank-note cleanup."""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import gardener as Gd
from mnemo import notes as N
from mnemo import topics as T
from mnemo.config import OFFLINE
from mnemo.db import now_iso, ulid
from mnemo.engine import Engine


def topic_of(engine, note_id):
    return T.path_of(engine.conn, N.primary_topic(engine.conn, note_id))


class ApplyProposal(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)
        self.films = [self.engine.capture(f'@"Films" {t}').note.id
                      for t in ("dune trailer", "interstellar with sam")]
        self.movies = self.engine.capture('@"Movies" oppenheimer imax').note.id

    def tearDown(self):
        self.engine.close()

    def propose(self, kind, payload):
        pid = ulid()
        self.engine.conn.execute(
            "INSERT INTO proposals(id, kind, payload, score, state, created_at)"
            " VALUES(?,?,?,?, 'pending', ?)",
            (pid, kind, json.dumps(payload), 0.9, now_iso()))
        return pid

    def topic_id(self, path):
        return T.by_path(self.engine.conn, path)["id"]

    def test_merge_moves_every_note_and_one_undo_restores_them(self):
        films, movies = self.topic_id("Films"), self.topic_id("Movies")
        pid = self.propose(Gd.MERGE, {"keep": movies, "drop": films})
        result = self.engine.apply_proposal(pid)
        self.assertEqual(result["moved"], 2)
        for note_id in self.films:
            self.assertEqual(topic_of(self.engine, note_id), "Movies")
            self.assertTrue(N.get(self.engine.conn, note_id)["path"].startswith("Movies"))
        self.assertEqual(T.get(self.engine.conn, films)["state"], "dissolved")
        # The old name now leads to the new home.
        self.assertEqual(T.resolve_alias(self.engine.conn, "Films"), movies)

        self.engine.undo()
        for note_id in self.films:
            self.assertEqual(topic_of(self.engine, note_id), "Films")
        self.assertNotEqual(T.get(self.engine.conn, films)["state"], "dissolved")
        self.assertIsNone(T.resolve_alias(self.engine.conn, "Films"))

    def test_dissolve_sends_notes_to_inbox(self):
        films = self.topic_id("Films")
        pid = self.propose(Gd.DISSOLVE, {"topic": films})
        self.engine.apply_proposal(pid)
        for note_id in self.films:
            self.assertIsNone(N.primary_topic(self.engine.conn, note_id))

    def test_unsupported_kinds_are_refused(self):
        pid = self.propose(Gd.SPLIT, {"topic": self.topic_id("Films")})
        with self.assertRaises(ValueError):
            self.engine.apply_proposal(pid)

    def test_a_dismissed_suggestion_stays_dismissed_after_a_rescan(self):
        films, movies = self.topic_id("Films"), self.topic_id("Movies")
        self.assertIn((Gd.MERGE, frozenset((movies, films))),
                      {Gd._signature(Gd.MERGE, {"keep": films, "drop": movies})})
        pid = self.propose(Gd.MERGE, {"keep": movies, "drop": films})
        Gd.dismiss(self.engine.conn, pid)
        cfg = dataclasses.replace(self.engine.cfg, merge_threshold=-1.0)
        found = Gd.scan(self.engine.conn, cfg)
        self.assertFalse([p for p in found if p.kind == Gd.MERGE
                          and {p.payload["keep"], p.payload["drop"]} == {films, movies}])

    def test_describe_speaks_plainly(self):
        films, movies = self.topic_id("Films"), self.topic_id("Movies")
        summary, detail = Gd.describe(self.engine.conn, Gd.MERGE,
                                      {"keep": movies, "drop": films})
        self.assertEqual(summary, "Merge *Films* into *Movies*?")
        self.assertIn("2 notes", detail)
        self.assertIsNone(Gd.describe(self.engine.conn, Gd.SPLIT, {"topic": films}))


class OrganiseSetting(unittest.TestCase):
    def test_suggest_only_keeps_new_notes_in_inbox(self):
        cfg = dataclasses.replace(OFFLINE, cold_start_notes=0)
        engine = Engine(tempfile.mkdtemp(), cfg=cfg, enrich=False)
        try:
            for t in ("rag chunking overlap", "vector store retrieval", "embedding models"):
                engine.capture(f'@"AI" {t}')
            self.assertFalse(engine._in_cold_start())
            held = engine.capture("hybrid retrieval with embeddings", auto_file=False)
            self.assertIsNone(held.topic_path)
            steered = engine.capture('@"AI" rerankers', auto_file=False)
            self.assertEqual(steered.topic_path, "AI")
        finally:
            engine.close()


class BlankNotes(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)

    def tearDown(self):
        self.engine.close()

    def test_an_untouched_new_note_is_discarded_without_an_undo_trail(self):
        kept = self.engine.capture("something real").note.id
        blank = self.engine.create_note()["id"]
        self.assertTrue(self.engine.discard_if_blank(blank))
        self.assertIsNone(N.get(self.engine.conn, blank))
        # The next undo reaches the capture, not the vanished blank page.
        self.engine.undo()
        self.assertIsNone(N.get(self.engine.conn, kept))

    def test_a_note_with_words_is_kept(self):
        note = self.engine.create_note(title="Groceries")["id"]
        self.assertFalse(self.engine.discard_if_blank(note))
        self.assertIsNotNone(N.get(self.engine.conn, note))


if __name__ == "__main__":
    unittest.main(verbosity=2)
