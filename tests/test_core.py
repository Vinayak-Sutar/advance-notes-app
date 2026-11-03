"""Unit tests for the deterministic core: vault, keys, gate, journal, search."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import gate as G
from mnemo import journal as J
from mnemo import notes as N
from mnemo import search as S
from mnemo import topics as T
from mnemo.config import OFFLINE
from mnemo.db import (connect, key_contains, key_similarity, normal_key,
                      slugify, ulid)
from mnemo.engine import Engine, parse_input
from mnemo.vault import Note, Vault, loads, render, safe_dirname
from mnemo.vecs import HashingEmbedder, cosine, pack, unpack


class TestKeys(unittest.TestCase):
    def test_normal_key_collapses_variants(self):
        variants = ["Gaming", "Games", "Game", "Gamer", "gaming stuff",
                    "My Games", "Games Notes", "GAMES"]
        keys = {normal_key(v) for v in variants}
        self.assertEqual(len(keys), 1, f"expected one key, got {keys}")

    def test_normal_key_keeps_distinct_topics_apart(self):
        for a, b in [("Gaming", "Music"), ("Web Dev", "AI"),
                     ("Sourdough", "Guitar"), ("Finance", "Health")]:
            self.assertNotEqual(normal_key(a), normal_key(b), f"{a} vs {b}")

    def test_key_similarity_prefix_aware(self):
        self.assertEqual(key_similarity(normal_key("Web Dev"),
                                        normal_key("Web Development")), 1.0)
        self.assertEqual(key_similarity(normal_key("Gaming"),
                                        normal_key("Music")), 0.0)

    def test_key_contains_detects_specialisation(self):
        self.assertTrue(key_contains(normal_key("Gaming Hardware"), normal_key("Gaming")))
        self.assertFalse(key_contains(normal_key("Gaming"), normal_key("Gaming Hardware")))
        self.assertFalse(key_contains(normal_key("Music"), normal_key("Gaming")))

    def test_slugify(self):
        self.assertEqual(slugify("Technology / AI  Stuff!"), "technology-ai-stuff")
        self.assertEqual(slugify("   "), "untitled")

    def test_ulid_is_unique(self):
        self.assertEqual(len({ulid() for _ in range(2000)}), 2000)

    def test_ulid_sorts_by_time(self):
        # ULIDs are ordered ACROSS milliseconds; within one millisecond the
        # random tail dominates, which is expected and fine.
        import time
        first = ulid()
        time.sleep(0.005)
        self.assertLess(first, ulid())


class TestVault(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(tempfile.mkdtemp())

    def test_frontmatter_round_trip(self):
        note = Note(title='Tricky: "quoted", [bracketed]', body="Line 1\n\nLine 2",
                    kind="article", topics=["Technology/AI"], entities=["RAG"],
                    tags=["x"], source="https://e.com/a?b=1&c=2")
        back = loads(render(note))
        for field in ("id", "title", "body", "kind", "topics", "entities",
                      "tags", "source"):
            self.assertEqual(getattr(back, field), getattr(note, field), field)

    def test_empty_body_round_trip(self):
        note = Note(title="only a title")
        self.assertEqual(loads(render(note)).body, "")

    def test_path_projection_is_readable_and_stable(self):
        note = Note(title="RAG chunking tricks")
        path = self.vault.path_for(note, "Technology/AI")
        self.assertEqual(path, "Technology/AI/rag-chunking-tricks.md")
        self.assertEqual(self.vault.path_for(note, "Technology/AI"), path)

    def test_unfiled_goes_to_inbox(self):
        self.assertTrue(self.vault.path_for(Note(title="x"), None).startswith("Inbox/"))

    def test_collision_with_different_note(self):
        a, b = Note(title="Same Title"), Note(title="Same Title")
        pa = self.vault.path_for(a, "T")
        self.vault.write(a, pa)
        self.assertNotEqual(self.vault.path_for(b, "T"), pa)

    def test_traversal_is_blocked(self):
        for bad in ("../evil.md", "..\\..\\evil.md", "a/../../evil.md"):
            with self.assertRaises(ValueError):
                self.vault.abs_path(bad)

    def test_windows_reserved_names(self):
        # Windows refuses these as path components with or without an
        # extension, so the output must differ from the input in every case.
        for name in ("con", "PRN", "nul.txt", "COM1", "LPT9"):
            self.assertNotEqual(safe_dirname(name).lower(), name.lower(), name)

    def test_safe_dirname_keeps_readable_names_intact(self):
        for name in ("Technology", "Elden Ring", "Web Dev"):
            self.assertEqual(safe_dirname(name), name)

    def test_scan_round_trips_through_disk(self):
        note = Note(title="Findable", body="body text", topics=["Gaming"])
        self.vault.write(note, self.vault.path_for(note, "Gaming"))
        found = {n.id: n for _, n in self.vault.scan()}
        self.assertIn(note.id, found)
        self.assertEqual(found[note.id].title, "Findable")


class TestVectors(unittest.TestCase):
    def setUp(self):
        self.embedder = HashingEmbedder()

    def test_deterministic_across_calls(self):
        self.assertEqual(self.embedder.embed_one("hello world"),
                         self.embedder.embed_one("hello world"))

    def test_blob_round_trip(self):
        vec = self.embedder.embed_one("some text")
        self.assertLess(max(abs(a - b) for a, b in zip(vec, unpack(pack(vec)))), 1e-6)

    def test_related_scores_above_unrelated(self):
        anchor = self.embedder.embed_one("elden ring malenia boss bleed build")
        near = self.embedder.embed_one("elden ring boss fight bleed dexterity")
        far = self.embedder.embed_one("sourdough starter hydration levain crumb")
        self.assertGreater(cosine(anchor, near), cosine(anchor, far))


class TestGate(unittest.TestCase):
    def setUp(self):
        self.conn = connect(os.path.join(tempfile.mkdtemp(), "i.db"))
        self.embedder = HashingEmbedder()
        self.cfg = OFFLINE
        self.gaming = T.create(self.conn, "Gaming", None, state="active",
                               name_vec=self.embedder.embed_one("Gaming"))
        for text in ["elden ring boss", "gaming pc rtx build", "best games 2026"]:
            T.fold_into_centroid(self.conn, self.gaming, self.embedder.embed_one(text))
        T.create(self.conn, "Web Development", None, state="active",
                 name_vec=self.embedder.embed_one("Web Development"))

    def propose(self, name, parent=None):
        return G.propose_topic(self.conn, name, parent,
                               self.embedder.embed_one(name), self.cfg)

    def test_refuses_name_variants(self):
        for name in ("Games", "Gaming Stuff", "Gamer", "My Games"):
            decision = self.propose(name)
            self.assertEqual(decision.action, "reuse", name)
            self.assertEqual(decision.topic_id, self.gaming, name)

    def test_refuses_abbreviation(self):
        decision = self.propose("Web Dev")
        self.assertEqual(decision.action, "reuse")

    def test_allows_genuinely_new_topic(self):
        decision = self.propose("Sourdough Baking")
        self.assertEqual(decision.action, "create")
        self.assertEqual(T.get(self.conn, decision.topic_id)["state"], "provisional")

    def test_specialisation_is_nested_not_duplicated(self):
        decision = self.propose("Gaming Hardware")
        self.assertIsNotNone(decision.topic_id)
        path = T.path_of(self.conn, decision.topic_id)
        self.assertTrue(path.startswith("Gaming"), path)

    def test_refusal_is_remembered_as_alias(self):
        self.propose("Games")
        self.assertEqual(T.resolve_alias(self.conn, "games"), self.gaming)

    def test_depth_cap(self):
        deep = T.ensure_path(self.conn, "A/B/C")
        decision = G.propose_topic(self.conn, "TooDeep", deep,
                                   self.embedder.embed_one("qwerty zxcv"), self.cfg)
        self.assertEqual(decision.reason, G.REJECT_DEPTH)

    def test_breadth_cap(self):
        parent = T.create(self.conn, "Parent", None, state="active")
        for i in range(self.cfg.max_children):
            T.create(self.conn, f"Child{i}", parent, state="active")
        decision = G.propose_topic(self.conn, "OneTooMany", parent,
                                   self.embedder.embed_one("zzz qqq"), self.cfg)
        self.assertEqual(decision.reason, G.REJECT_BREADTH)

    def test_provisional_has_no_folder_until_it_earns_one(self):
        decision = self.propose("Sourdough Baking")
        topic_id = decision.topic_id
        self.assertIsNone(G.effective_dir(self.conn, topic_id))
        for i in range(self.cfg.min_mass):
            note_id = ulid()
            self.conn.execute(
                "INSERT INTO notes(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                (note_id, f"n{i}", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
            N.assign(self.conn, note_id, topic_id)
        promoted = G.promote_ready(self.conn, self.cfg)
        self.assertIn(topic_id, promoted)
        self.assertEqual(G.effective_dir(self.conn, topic_id), "Sourdough Baking")


class TestCaptureAndUndo(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)

    def tearDown(self):
        self.engine.close()

    def test_parse_input_directives(self):
        parsed = parse_input("@gaming rig is broken #pc !task")
        self.assertEqual(parsed["topic"], "gaming")
        self.assertEqual(parsed["tags"], ["pc"])
        self.assertEqual(parsed["kind"], "task")
        self.assertNotIn("@", parsed["text"])
        self.assertNotIn("#pc", parsed["text"])

    def test_detects_kinds(self):
        self.assertEqual(parse_input("https://youtu.be/x talk")["kind"], "video")
        self.assertEqual(parse_input("https://a.com/b post")["kind"], "link")
        self.assertEqual(parse_input("plain thought")["kind"], "note")

    def test_capture_always_writes_a_file(self):
        result = self.engine.capture("a brand new isolated thought")
        self.assertTrue(self.engine.vault.abs_path(result.path).exists())

    def test_steering_beats_the_router(self):
        result = self.engine.capture("@Gaming something about anything at all")
        self.assertTrue(result.steered)
        self.assertEqual(result.topic_path, "Gaming")
        self.assertTrue(result.path.startswith("Gaming/"))

    def test_undo_removes_note_and_file(self):
        result = self.engine.capture("@Gaming undo me please")
        path = result.path
        self.assertTrue(self.engine.vault.abs_path(path).exists())
        self.engine.undo()
        self.assertFalse(self.engine.vault.abs_path(path).exists())
        self.assertIsNone(N.get(self.engine.conn, result.note.id))

    def test_move_note_is_undoable(self):
        result = self.engine.capture("@Gaming movable note")
        moved = self.engine.move_note(result.note.id, "Music")
        self.assertTrue(moved.startswith("Music/"))
        self.engine.undo()
        row = N.get(self.engine.conn, result.note.id)
        self.assertEqual(row["path"], result.path)
        self.assertTrue(self.engine.vault.abs_path(result.path).exists())

    def test_rebuild_reconstructs_index_from_disk(self):
        for i in range(4):
            self.engine.capture(f'@"Gaming/Elden Ring" note number {i} about malenia')
        self.engine.capture('@"Music/Guitar" pentatonic scale practice')
        before = {r["id"] for r in self.engine.conn.execute("SELECT id FROM notes")}

        self.engine.conn.execute("DELETE FROM notes")
        self.engine.conn.execute("DELETE FROM topics")
        stats = self.engine.rebuild()

        after = {r["id"] for r in self.engine.conn.execute("SELECT id FROM notes")}
        self.assertEqual(before, after)
        self.assertEqual(stats["notes"], len(before))
        self.assertIsNotNone(T.by_path(self.engine.conn, "Gaming/Elden Ring"))


class TestSearch(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)
        for text in ["@Technology/AI rag chunking strategies for retrieval",
                     "@Technology/AI vector database embedding similarity",
                     "@Gaming elden ring malenia boss bleed build",
                     "@Cooking sourdough starter hydration levain"]:
            self.engine.capture(text)

    def tearDown(self):
        self.engine.close()

    def test_keyword_match(self):
        hits = S.search(self.engine.conn, "malenia",
                        self.engine._embed_one("malenia"))
        self.assertTrue(hits)
        self.assertIn("malenia", hits[0].snippet.lower())

    def test_semantic_match_without_exact_words(self):
        query = "splitting documents up for retrieval"
        hits = S.search(self.engine.conn, query, self.engine._embed_one(query))
        self.assertTrue(hits)
        self.assertTrue(any("chunking" in h.snippet.lower() or
                            "embedding" in h.snippet.lower() for h in hits[:3]))

    def test_fts_special_characters_do_not_crash(self):
        for query in ['"', "AND OR NOT", "a*b", "NEAR(", "'''", ")("]:
            S.search(self.engine.conn, query, self.engine._embed_one(query))

    def test_topic_view_filters_by_kind(self):
        topic = T.by_path(self.engine.conn, "Technology/AI")
        everything = S.topic_view(self.engine.conn, topic["id"])
        videos = S.topic_view(self.engine.conn, topic["id"], kind="video")
        self.assertEqual(len(everything), 2)
        self.assertEqual(len(videos), 0)


class TestEnrich(unittest.TestCase):
    """HTML extraction, exercised on fixtures so the suite stays offline."""

    def parse(self, html_text):
        from mnemo.enrich import _Extractor
        parser = _Extractor()
        parser.feed(html_text)
        return parser

    def test_extracts_title_and_body(self):
        parser = self.parse("""
            <html><head><title>RAG chunking tricks</title></head>
            <body><p>Overlap of 200 tokens beats 50.</p></body></html>""")
        self.assertEqual(parser.title.strip(), "RAG chunking tricks")
        self.assertIn("Overlap of 200 tokens", parser.text())

    def test_drops_scripts_navigation_and_styles(self):
        parser = self.parse("""
            <html><body>
              <nav>Home About Contact</nav>
              <script>var tracking = 'NOISE_SCRIPT';</script>
              <style>.x{color:red} /* NOISE_STYLE */</style>
              <p>The actual article body.</p>
              <footer>NOISE_FOOTER</footer>
            </body></html>""")
        text = parser.text()
        self.assertIn("The actual article body", text)
        for noise in ("NOISE_SCRIPT", "NOISE_STYLE", "NOISE_FOOTER", "About"):
            self.assertNotIn(noise, text)

    def test_prefers_opengraph_metadata(self):
        parser = self.parse("""
            <html><head>
              <title>site name | page</title>
              <meta property="og:title" content="The Real Title">
              <meta property="og:description" content="A hand written summary.">
            </head><body><p>body</p></body></html>""")
        self.assertEqual(parser.meta.get("og:title"), "The Real Title")
        self.assertEqual(parser.meta.get("og:description"), "A hand written summary.")

    def test_malformed_html_does_not_raise(self):
        for broken in ("<p>unclosed", "<<>>", "<div><span>x</div>", ""):
            self.parse(broken).text()

    def test_url_kind_detection(self):
        from mnemo.enrich import _kind_for_url
        self.assertEqual(_kind_for_url("https://youtu.be/abc"), "video")
        self.assertEqual(_kind_for_url("https://youtube.com/watch?v=a"), "video")
        self.assertEqual(_kind_for_url("https://a.com/pic.PNG"), "image")
        self.assertEqual(_kind_for_url("https://a.com/post"), "link")

    def test_capture_survives_a_dead_url(self):
        """Enrichment failure must never lose a note."""
        engine = Engine(tempfile.mkdtemp(), enrich=True)
        try:
            result = engine.capture(
                "@Gaming http://127.0.0.1:9/definitely-not-listening some thoughts")
            self.assertTrue(engine.vault.abs_path(result.path).exists())
            self.assertEqual(result.topic_path, "Gaming")
        finally:
            engine.close()


class TestPortability(unittest.TestCase):
    """The project must run from wherever it is checked out, for any user."""

    def test_paths_resolve_under_the_repo(self):
        from mnemo import paths
        for path in (paths.VENDOR_DIR, paths.OLLAMA_DIR, paths.MODELS_DIR,
                     paths.CACHE_DIR):
            self.assertTrue(str(path).startswith(str(paths.REPO_ROOT)),
                            f"{path} escapes the repo")

    def test_no_absolute_paths_hardcoded_in_source(self):
        """A drive letter or /home/ in the source would break on another machine."""
        import re as _re
        package = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "mnemo")
        bad = _re.compile(r"""["'](?:[A-Za-z]:[\\/]|/home/|/Users/)""")
        offenders = []
        for name in os.listdir(package):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(package, name), encoding="utf-8") as handle:
                text = handle.read()
            for match in bad.finditer(text):
                line = text[:match.start()].count("\n") + 1
                offenders.append(f"{name}:{line}")
        self.assertEqual(offenders, [], f"absolute paths in source: {offenders}")

    def test_ollama_env_points_models_into_the_repo(self):
        from mnemo import paths
        env = paths.ollama_env()
        self.assertEqual(env["OLLAMA_MODELS"], str(paths.MODELS_DIR))
        self.assertTrue(str(paths.MODELS_DIR).startswith(str(paths.REPO_ROOT)))

    def test_vault_is_relocatable(self):
        """A vault carries its own index; moving the folder must not break it."""
        import shutil as _shutil
        first = tempfile.mkdtemp()
        engine = Engine(first, enrich=False)
        engine.capture("@Gaming a note that should survive the move")
        engine.close()

        second = os.path.join(tempfile.mkdtemp(), "moved-vault")
        _shutil.copytree(first, second)
        moved = Engine(second, enrich=False)
        try:
            self.assertEqual(moved.conn.execute(
                "SELECT COUNT(*) c FROM notes").fetchone()["c"], 1)
            self.assertIsNotNone(T.by_path(moved.conn, "Gaming"))
        finally:
            moved.close()

    def test_engine_falls_back_when_models_absent(self):
        """`online=None` means 'use models if present', never 'fail'."""
        from mnemo.engine import build_stack
        embedder, router, cfg = build_stack(online=False)
        self.assertEqual(cfg.name, "offline-hashing")
        self.assertTrue(hasattr(embedder, "embed"))
        self.assertTrue(hasattr(router, "route"))


class TestCalibration(unittest.TestCase):
    """Derived thresholds must be valid for ANY embedder's scale.

    The first version of `derive` multiplied the noise floor, which produced
    impossible values (dedupe 1.97, review floor 1.05) for nomic-embed-text,
    whose unrelated pairs sit near 0.56 rather than near 0.05. Thresholds above
    1.0 can never fire, which would silently disable the gate.
    """

    def stats(self, same, diff):
        from mnemo.calibrate import percentile
        return {
            "same": {"mean": sum(same)/len(same), "p10": percentile(same, .10),
                     "p50": percentile(same, .50), "p90": percentile(same, .90)},
            "diff": {"mean": sum(diff)/len(diff), "p50": percentile(diff, .50),
                     "p95": percentile(diff, .95), "p99": percentile(diff, .99),
                     "max": max(diff)},
        }

    def scenarios(self):
        import random
        rng = random.Random(3)
        return {
            # hashing embedder: noise near zero
            "low-floor": self.stats(
                [rng.uniform(.30, .70) for _ in range(400)],
                [rng.uniform(.00, .25) for _ in range(400)]),
            # nomic-embed-text: everything is "somewhat similar"
            "high-floor": self.stats(
                [rng.uniform(.68, .86) for _ in range(400)],
                [rng.uniform(.45, .66) for _ in range(400)]),
            # a poorly separated embedder
            "overlapping": self.stats(
                [rng.uniform(.40, .80) for _ in range(400)],
                [rng.uniform(.35, .75) for _ in range(400)]),
        }

    def test_thresholds_stay_within_cosine_range(self):
        from mnemo.calibrate import derive
        for name, m in self.scenarios().items():
            for key, value in derive(m).items():
                self.assertGreaterEqual(value, 0.0, f"{name}/{key}={value}")
                self.assertLessEqual(value, 1.0, f"{name}/{key}={value}")

    def test_file_floor_sits_above_the_noise(self):
        from mnemo.calibrate import derive
        for name, m in self.scenarios().items():
            d = derive(m)
            self.assertGreaterEqual(d["file_floor"], m["diff"]["p50"], name)

    def test_review_floor_is_stricter_than_file_floor(self):
        from mnemo.calibrate import derive
        for name, m in self.scenarios().items():
            d = derive(m)
            self.assertGreaterEqual(d["review_floor"], d["file_floor"], name)

    def test_min_margin_is_a_small_spread_not_a_similarity(self):
        from mnemo.calibrate import derive
        for name, m in self.scenarios().items():
            self.assertLessEqual(derive(m)["min_margin"], 0.25, name)

    def test_shipped_configs_are_internally_consistent(self):
        from mnemo.config import OFFLINE, ONLINE
        for cfg in (OFFLINE, ONLINE):
            self.assertLessEqual(cfg.file_floor, cfg.review_floor, cfg.name)
            for field in ("file_floor", "review_floor", "dedupe_threshold",
                          "dedupe_centroid_threshold", "related_edge_floor",
                          "merge_threshold", "min_margin"):
                value = getattr(cfg, field)
                self.assertGreaterEqual(value, 0.0, f"{cfg.name}.{field}")
                self.assertLessEqual(value, 1.0, f"{cfg.name}.{field}")


class TestPullRetry(unittest.TestCase):
    """Model pulls must survive a dropping connection.

    Observed on a real flaky link: three attempts died at 68%, 76% and 78% of a
    3.3 GB model. Ollama keeps partial blobs and resumes, so retrying costs time
    but no bandwidth -- which makes persistence strictly better than failing and
    telling the user to run it again.
    """

    def setUp(self):
        from mnemo import runtime
        self.runtime = runtime
        self._once = runtime._pull_once
        self._binary = runtime.ollama_binary
        self._has = runtime.has_model
        runtime.ollama_binary = lambda: "fake-binary"
        runtime.has_model = lambda name: False

    def tearDown(self):
        self.runtime._pull_once = self._once
        self.runtime.ollama_binary = self._binary
        self.runtime.has_model = self._has

    def test_retries_until_it_succeeds(self):
        calls = []

        def flaky(binary, model, on_progress=None):
            calls.append(model)
            return len(calls) >= 3          # fails twice, then works

        self.runtime._pull_once = flaky
        self.runtime.time.sleep = lambda s: None
        self.assertTrue(self.runtime.pull("some:model", attempts=5))
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_the_attempt_budget(self):
        calls = []
        self.runtime._pull_once = lambda b, m, p=None: calls.append(m) or False
        self.runtime.time.sleep = lambda s: None
        self.assertFalse(self.runtime.pull("some:model", attempts=4))
        self.assertEqual(len(calls), 4)

    def test_stops_when_the_model_appears_despite_a_noisy_exit(self):
        """Ollama can exit non-zero after the manifest has landed."""
        self.runtime._pull_once = lambda b, m, p=None: False
        self.runtime.has_model = lambda name: True
        self.runtime.time.sleep = lambda s: None
        self.assertTrue(self.runtime.pull("some:model", attempts=9))

    def test_reports_each_retry(self):
        seen = []
        self.runtime._pull_once = lambda b, m, p=None: len(seen) >= 2
        self.runtime.time.sleep = lambda s: None
        self.runtime.pull("m", attempts=5, on_retry=lambda a, t: seen.append(a))
        self.assertEqual(seen, [1, 2])


class TestJournal(unittest.TestCase):
    def test_undo_without_handler_raises(self):
        conn = connect(os.path.join(tempfile.mkdtemp(), "i.db"))
        J.clear_handlers()
        J.record(conn, "x.do", {}, {"op": "x.undo"})
        with self.assertRaises(KeyError):
            J.undo(conn)

    def test_undo_marks_entry_and_stops(self):
        conn = connect(os.path.join(tempfile.mkdtemp(), "i.db"))
        J.clear_handlers()
        seen = []
        J.register("x.undo", lambda rec: seen.append(rec))
        J.record(conn, "x.do", {"n": 1}, {"op": "x.undo", "n": 1})
        self.assertIsNotNone(J.undo(conn))
        self.assertEqual(len(seen), 1)
        self.assertIsNone(J.undo(conn))


if __name__ == "__main__":
    unittest.main(verbosity=2)
