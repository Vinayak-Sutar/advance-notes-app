"""Profiles must stay apart: notes, files, undo and settings. Made-up data only."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import journal as J
from mnemo import profiles as P
from mnemo import server as SRV
from mnemo.engine import Engine
from tests.test_server import _free_port


class _Folders(unittest.TestCase):
    """Points profiles at a throwaway copy of the repo layout."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mnemo-prof-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        saved = P.PROFILES_DIR, P.BACKUPS_DIR, P.REPO_ROOT
        P.PROFILES_DIR, P.BACKUPS_DIR, P.REPO_ROOT = (
            self.root / "profiles", self.root / "backups", self.root)
        self.addCleanup(lambda: (setattr(P, "PROFILES_DIR", saved[0]),
                                 setattr(P, "BACKUPS_DIR", saved[1]),
                                 setattr(P, "REPO_ROOT", saved[2])))

    def old_vault(self):
        engine = Engine(self.root / "vault", online=False, enrich=False)
        engine.create_note(title="Old note", body="kept", topic_path="Things")
        engine.close()
        layout = self.root / "vault" / "_personal" / "layout.json"
        layout.parent.mkdir(parents=True)
        layout.write_text('{"all": ["a", "b"]}', encoding="utf-8")


class FirstRun(_Folders):
    def titles(self, profile):
        engine = Engine(profile.path, online=False, enrich=False)
        try:
            return {r["title"] for r in engine.conn.execute("SELECT title FROM notes")}
        finally:
            engine.close()

    def test_new_user_gets_one_profile_with_the_guide(self):
        profiles = P.first_run(online=False)
        self.assertEqual([p.name for p in profiles], ["Main"])
        self.assertEqual(self.titles(profiles[0]), {P.GUIDE_TITLE})

    def test_old_vault_becomes_the_main_profile_for_anyone_else(self):
        self.old_vault()
        profiles = P.first_run(online=False)
        self.assertEqual(self.titles(profiles[0]), {"Old note"})
        self.assertFalse((self.root / "vault").exists())

    def test_named_test_profile_takes_the_old_vault(self):
        self.old_vault()
        main, test = P.first_run("Someone", "Trying", online=False)
        self.assertEqual((main.id, test.id), ("someone", "trying"))
        self.assertEqual(self.titles(test), {"Old note"})
        self.assertEqual(self.titles(main), {P.GUIDE_TITLE})
        # Each has its own copy of Personal notes, order included.
        for profile in (main, test):
            self.assertIn('"a"', (profile.path / "_personal" / "layout.json").read_text())
        (test.path / "_personal" / "layout.json").write_text("{}")
        self.assertIn('"a"', (main.path / "_personal" / "layout.json").read_text())

    def test_the_old_vault_is_zipped_first(self):
        self.old_vault()
        P.first_run("Someone", "Trying", online=False)
        saved = next((self.root / "backups").glob("vault-before-profiles-*.zip"))
        with zipfile.ZipFile(saved) as archive:
            names = archive.namelist()
        self.assertTrue(any(n.endswith("layout.json") for n in names))
        self.assertTrue(any(n.endswith(".md") for n in names))

    def test_running_twice_changes_nothing(self):
        self.old_vault()
        first = P.first_run("Someone", "Trying", online=False)
        self.assertEqual(P.first_run("Other", online=False), first)
        self.assertEqual(self.titles(first[1]), {"Old note"})

    def test_new_profile_gets_the_guide_and_a_safe_folder(self):
        P.first_run(online=False)
        self.assertEqual(self.titles(P.create("Work", online=False)), {P.GUIDE_TITLE})
        self.assertEqual(P.create("Con", online=False).id, "con-1")       # Windows refuses "con"
        self.assertEqual(P.create("काम", online=False).id, "profile")      # no latin letters
        self.assertEqual(P.create("नोट", online=False).id, "profile-2")
        with self.assertRaises(ValueError):
            P.create("work", online=False)
        with self.assertRaises(ValueError):
            P.create("   ", online=False)
        self.assertEqual(len(P.load()), 5)


class Backups(_Folders):
    def setUp(self):
        super().setUp()
        self.profile = P.Profile("me", "Me")
        self.profile.path.mkdir(parents=True)
        self.note = self.profile.path / "a.md"
        self.note.write_text("one")
        (self.profile.path / ".mnemo").mkdir()
        (self.profile.path / ".mnemo" / "index.db").write_text("cache")

    def test_one_zip_a_day_without_the_index(self):
        made = P.backup(self.profile, today=date(2026, 1, 1))
        self.assertEqual(made.name, "me-2026-01-01.zip")
        with zipfile.ZipFile(made) as archive:
            self.assertEqual(archive.namelist(), ["a.md"])
        self.assertIsNone(P.backup(self.profile, today=date(2026, 1, 1)))

    def test_skips_a_day_when_nothing_changed(self):
        P.backup(self.profile, today=date(2026, 1, 1))
        self.assertIsNone(P.backup(self.profile, today=date(2026, 1, 2)))

    def test_a_removed_note_counts_as_a_change(self):
        (self.profile.path / "b.md").write_text("two")
        P.backup(self.profile, today=date(2026, 1, 1))
        (self.profile.path / "b.md").unlink()
        self.assertIsNotNone(P.backup(self.profile, today=date(2026, 1, 2)))

    def test_keeps_only_the_last_few(self):
        for day in range(1, 11):
            self.note.write_text(f"version {day}")
            P.backup(self.profile, keep=3, today=date(2026, 1, day))
        names = sorted(p.name for p in P.BACKUPS_DIR.glob("*.zip"))
        self.assertEqual(names, [f"me-2026-01-{d:02}.zip" for d in (8, 9, 10)])

    def test_another_profiles_backups_are_left_alone(self):
        (P.BACKUPS_DIR).mkdir()
        other = P.BACKUPS_DIR / "you-2026-01-01.zip"
        other.write_bytes(b"x")
        for day in range(1, 4):
            self.note.write_text(f"version {day}")
            P.backup(self.profile, keep=1, today=date(2026, 1, day))
        self.assertTrue(other.exists())


class Undo(_Folders):
    def test_undo_stays_inside_its_own_profile(self):
        one = Engine(self.root / "one", online=False, enrich=False)
        two = Engine(self.root / "two", online=False, enrich=False)
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        note = one.create_note(title="Mine", body="first", topic_path="T")
        one.update_note(note["id"], body="second")
        two.create_note(title="Other", body="x", topic_path="T")
        # `two` opened last. If handlers were shared, this would run on its vault.
        record = one.undo()
        self.assertEqual(record["op"], "note.restore")
        row = one.conn.execute("SELECT body FROM notes WHERE id = ?", (note["id"],)).fetchone()
        self.assertEqual(row["body"].strip(), "first")
        self.assertIsNotNone(J.entries(two.conn))


class TwoProfilesOverHttp(_Folders):
    def setUp(self):
        super().setUp()
        self.profiles = [P.create("First", online=False), P.create("Second", online=False)]
        SRV._rooms.clear()
        for profile in self.profiles:
            SRV._rooms[profile.id] = SRV.Room(Engine(profile.path, online=False, enrich=False),
                                              profile=profile)
        self.engines = [room.engine for room in SRV._rooms.values()]
        self.engines[0].create_note(title="Only in first", body="alpha", topic_path="T")
        self.engines[1].create_note(title="Only in second", body="beta", topic_path="T")
        (self.profiles[0].path / ".mnemo" / "media").mkdir(parents=True, exist_ok=True)
        (self.profiles[0].path / ".mnemo" / "media" / "pic.png").write_bytes(b"first picture")

        self.port = _free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), SRV.Handler)
        self.httpd.verbose = False
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        SRV._rooms.clear()
        for engine in self.engines:
            engine.close()

    def call(self, path, payload=None, profile=None, tab=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if payload is None else json.dumps(payload).encode())
        if profile:
            req.add_header("Cookie", f"{SRV.PROFILE_COOKIE}={profile}")
        if tab:
            req.add_header("X-Notes-Profile", tab)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r, r.read()

    def titles(self, profile=None):
        data = json.loads(self.call("/api/notes", profile=profile)[1])
        return {n["title"] for n in data["notes"]}

    def test_each_browser_sees_only_its_profile(self):
        self.assertIn("Only in first", self.titles("first"))
        self.assertNotIn("Only in second", self.titles("first"))
        self.assertIn("Only in second", self.titles("second"))
        self.assertNotIn("Only in first", self.titles("second"))

    def test_no_choice_or_an_unknown_one_means_the_first(self):
        self.assertIn("Only in first", self.titles())
        self.assertIn("Only in first", self.titles("../second"))
        self.assertIn("Only in first", self.titles("nobody"))

    def test_a_new_note_lands_in_the_chosen_profile(self):
        self.call("/api/note/new", {"title": "Saved here"}, profile="second")
        self.assertIn("Saved here", self.titles("second"))
        self.assertNotIn("Saved here", self.titles("first"))

    def test_files_stay_with_their_profile(self):
        self.assertEqual(self.call("/media/pic.png", profile="first")[1], b"first picture")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/media/pic.png", profile="second")
        self.assertEqual(ctx.exception.code, 404)

    def test_undo_acts_on_the_chosen_profile(self):
        first, second = self.engines
        note = json.loads(self.call("/api/note/new", {"title": "Move me", "topic": "Here"},
                                    profile="second")[1])
        self.call("/api/move", {"id": note["id"], "topic": "Elsewhere"}, profile="second")
        before = first.conn.execute("SELECT COUNT(*) c FROM journal WHERE undone = 0").fetchone()["c"]
        reply = json.loads(self.call("/api/undo", {}, profile="second")[1])
        self.assertEqual(reply["undone"], "note.relocate")
        path = second.conn.execute("SELECT path FROM notes WHERE id = ?", (note["id"],)).fetchone()["path"]
        self.assertIn("Here", path)
        after = first.conn.execute("SELECT COUNT(*) c FROM journal WHERE undone = 0").fetchone()["c"]
        self.assertEqual(before, after)

    def test_a_tab_stays_on_the_profile_it_loaded(self):
        """Another tab moved the cookie on; this one must not start saving there."""
        self.call("/api/note/new", {"title": "From the old tab"}, profile="second", tab="first")
        self.assertIn("From the old tab", self.titles("first"))
        self.assertNotIn("From the old tab", self.titles("second"))

    def test_choosing_a_profile_sets_it_for_this_browser_only(self):
        reply, _ = self.call("/api/profile", {"id": "second"})
        self.assertIn(f"{SRV.PROFILE_COOKIE}=second", reply.headers["Set-Cookie"])
        self.assertNotIn("HttpOnly", reply.headers["Set-Cookie"])     # the page reads it
        self.assertIn("SameSite=Lax", reply.headers["Set-Cookie"])

    def test_an_unknown_profile_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/api/profile", {"id": "nobody"})
        self.assertEqual(ctx.exception.code, 404)

    def test_the_page_gives_a_new_browser_the_default(self):
        reply, _ = self.call("/")
        self.assertIn(f"{SRV.PROFILE_COOKIE}=first", reply.headers["Set-Cookie"])
        reply, _ = self.call("/", profile="second")
        self.assertIsNone(reply.headers["Set-Cookie"])

    def test_profiles_are_listed(self):
        data = json.loads(self.call("/api/profiles", profile="second")[1])
        self.assertEqual([p["name"] for p in data["profiles"]], ["First", "Second"])
        self.assertEqual(data["current"], "second")


if __name__ == "__main__":
    unittest.main()
