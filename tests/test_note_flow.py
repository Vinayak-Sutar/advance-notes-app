"""Writing a note by hand: New note, edit, file, delete, and undo for each.

The capture box interprets text; these paths do not. They exist so the app also
works as an ordinary notes app, and they lean heavily on undo -- which is why
the undo-after-edit crash is pinned here.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import notes as N
from mnemo import topics as T
from mnemo.engine import Engine
from mnemo.vault import loads


class NoteFlowCase(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)

    def tearDown(self):
        self.engine.close()

    def file_text(self, note_id):
        row = N.get(self.engine.conn, note_id)
        return self.engine.vault.abs_path(row["path"]).read_text(encoding="utf-8")


class TestCreate(NoteFlowCase):
    def test_blank_note_starts_unfiled_in_inbox(self):
        r = self.engine.create_note()
        self.assertIsNone(r["topic"])
        self.assertTrue(r["path"].startswith("Inbox/"))
        self.assertIsNone(N.primary_topic(self.engine.conn, r["id"]))
        self.assertTrue(self.engine.vault.abs_path(r["path"]).exists())

    def test_note_created_inside_a_folder_lives_there(self):
        r = self.engine.create_note(topic_path="Gaming/Elden Ring")
        self.assertEqual(r["topic"], "Gaming/Elden Ring")
        self.assertTrue(r["path"].startswith("Gaming/Elden Ring/"))
        # The frontmatter carries the folder, so a rebuild keeps it there.
        self.assertIn("Gaming/Elden Ring", loads(self.file_text(r["id"])).topics)

    def test_two_blank_notes_do_not_collide(self):
        a, b = self.engine.create_note(), self.engine.create_note()
        self.assertNotEqual(a["path"], b["path"])


class TestEditAndUndo(NoteFlowCase):
    def test_retitling_moves_the_file(self):
        r = self.engine.create_note()
        out = self.engine.update_note(r["id"], title="Grocery list", body="milk")
        self.assertTrue(out["path"].endswith("grocery-list.md"))
        self.assertFalse(self.engine.vault.abs_path(r["path"]).exists())

    def test_undo_after_an_edit_restores_the_text(self):
        """Every edit journaled a `note.restore` inverse with no handler, so
        Ctrl+Z after editing raised KeyError instead of undoing."""
        r = self.engine.create_note(topic_path="Cooking")
        self.engine.update_note(r["id"], title="Pasta", body="first draft")
        self.engine.update_note(r["id"], body="second draft")
        self.engine.undo()
        self.assertEqual(N.get(self.engine.conn, r["id"])["body"], "first draft")
        self.assertIn("first draft", self.file_text(r["id"]))

    def test_undoing_a_retitle_moves_the_file_back(self):
        r = self.engine.create_note(topic_path="Cooking")
        self.engine.update_note(r["id"], title="Pasta", body="x")
        path_before = N.get(self.engine.conn, r["id"])["path"]
        self.engine.update_note(r["id"], title="Risotto")
        self.engine.undo()
        row = N.get(self.engine.conn, r["id"])
        self.assertEqual(row["title"], "Pasta")
        self.assertEqual(row["path"], path_before)


class TestAutoFile(NoteFlowCase):
    def setUp(self):
        super().setUp()
        for i in range(4):
            self.engine.capture(
                f'@"Cooking" sourdough starter levain hydration bake loaf note {i}')
        for i in range(4):
            self.engine.capture(
                f'@"Gaming" elden ring malenia boss bleed build note {i}')

    def test_written_note_is_filed_by_content(self):
        r = self.engine.create_note()
        self.engine.update_note(
            r["id"], title="Levain timing",
            body="sourdough starter levain hydration and bake timing for the loaf")
        out = self.engine.auto_file(r["id"])
        self.assertTrue(out["filed"], out)
        self.assertEqual(out["topic"], "Cooking")
        self.assertTrue(N.get(self.engine.conn, r["id"])["path"].startswith("Cooking/"))

    def test_too_little_text_is_not_filed(self):
        r = self.engine.create_note()
        out = self.engine.auto_file(r["id"])
        self.assertFalse(out["filed"])
        self.assertEqual(out["reason"], "too short to file")

    def test_already_filed_notes_are_left_alone(self):
        r = self.engine.create_note(topic_path="Gaming")
        self.engine.update_note(r["id"], body="sourdough levain hydration loaf bake")
        out = self.engine.auto_file(r["id"])
        self.assertFalse(out["filed"])
        self.assertEqual(out["topic"], "Gaming")

    def test_undoing_a_file_returns_it_to_inbox_on_disk_too(self):
        """The file's frontmatter must be cleared as well, or `rebuild` would
        quietly re-file the note the undo just unfiled."""
        r = self.engine.create_note()
        self.engine.update_note(
            r["id"], title="Crumb notes",
            body="sourdough levain hydration open crumb bake loaf")
        self.assertTrue(self.engine.auto_file(r["id"])["filed"])
        self.engine.undo()
        self.assertIsNone(N.primary_topic(self.engine.conn, r["id"]))
        self.assertTrue(N.get(self.engine.conn, r["id"])["path"].startswith("Inbox/"))
        self.assertEqual(loads(self.file_text(r["id"])).topics, [])


class TestDelete(NoteFlowCase):
    def test_delete_removes_file_and_row(self):
        r = self.engine.create_note(topic_path="Music")
        self.engine.update_note(r["id"], title="Riff", body="drop D")
        path = N.get(self.engine.conn, r["id"])["path"]
        self.engine.delete_note(r["id"])
        self.assertIsNone(N.get(self.engine.conn, r["id"]))
        self.assertFalse(self.engine.vault.abs_path(path).exists())

    def test_undo_brings_a_deleted_note_back_exactly(self):
        r = self.engine.create_note(topic_path="Music")
        self.engine.update_note(r["id"], title="Riff", body="drop D, palm muted")
        self.engine.schedule(r["id"], "2026-10-01T09:00:00")
        before = N.get(self.engine.conn, r["id"])
        self.engine.delete_note(r["id"])
        self.engine.undo()
        after = N.get(self.engine.conn, r["id"])
        self.assertIsNotNone(after)
        self.assertEqual(after["title"], "Riff")
        self.assertEqual(after["body"], "drop D, palm muted")
        self.assertEqual(after["path"], before["path"])
        self.assertEqual(after["due_at"], "2026-10-01T09:00:00")
        self.assertEqual(T.path_of(self.engine.conn,
                                   N.primary_topic(self.engine.conn, r["id"])), "Music")

    def test_delete_unknown_note_raises(self):
        with self.assertRaises(KeyError):
            self.engine.delete_note("NOPE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
