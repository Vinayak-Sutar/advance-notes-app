"""Due times, reminders and "done" live in the note's file, not only the index.

They used to be kept in SQLite alone, so a rebuild lost them and any later
edit quietly set a finished to-do back to open. These pin the fix.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import notes as N
from mnemo.db import local_now_iso
from mnemo.engine import Engine
from mnemo.vault import Note, loads, render


class Case(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)

    def tearDown(self):
        self.engine.close()

    def row(self, note_id):
        return N.get(self.engine.conn, note_id)

    def on_disk(self, note_id) -> Note:
        return self.engine.vault.read(self.row(note_id)["path"])


class Header(unittest.TestCase):
    def test_new_fields_round_trip(self):
        n = Note(title="Exam prep", body="- [ ] algebra", kind="list",
                 due="2026-11-15T18:00:00", remind="2026-11-14T09:00:00",
                 repeat="weekly", checkin="09:00", quiet_until="2026-11-10",
                 parent="01ABC", order=1729000000.5)
        back = loads(render(n))
        for key in ("due", "remind", "repeat", "checkin", "quiet_until", "parent"):
            self.assertEqual(getattr(back, key), getattr(n, key), key)
        self.assertEqual(back.order, 1729000000.5)
        self.assertEqual(back.kind, "list")

    def test_plain_note_header_is_unchanged(self):
        text = render(Note(title="hi", body="there"))
        for key in ("due:", "remind:", "repeat:", "checkin:", "quiet_until:",
                    "parent:", "order:"):
            self.assertNotIn(key, text)


class Scheduling(Case):
    def test_schedule_and_done_are_written_to_the_file(self):
        r = self.engine.create_note(title="Call mom", body="about Sunday")
        self.engine.schedule(r["id"], "2026-11-01T17:00:00")
        self.engine.complete(r["id"], True)
        note = self.on_disk(r["id"])
        self.assertEqual(note.due, "2026-11-01T17:00:00")
        self.assertEqual(note.remind, "2026-11-01T17:00:00")
        self.assertEqual(note.status, "done")
        self.assertEqual(note.kind, "task")

    def test_done_survives_a_later_edit(self):
        r = self.engine.create_note(title="Call mom", body="about Sunday")
        self.engine.schedule(r["id"], "2026-11-01T17:00:00")
        self.engine.complete(r["id"], True)
        self.engine.update_note(r["id"], body="about Sunday lunch")
        row = self.row(r["id"])
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["due_at"], "2026-11-01T17:00:00")

    def test_snooze_moves_only_the_reminder(self):
        r = self.engine.create_note(title="Pay rent", body="")
        self.engine.schedule(r["id"], "2026-11-01T09:00:00")
        self.engine.schedule(r["id"], "2026-11-01T09:10:00", snooze=True)
        row = self.row(r["id"])
        self.assertEqual(row["due_at"], "2026-11-01T09:00:00")
        self.assertEqual(row["remind_at"], "2026-11-01T09:10:00")

    def test_a_moved_reminder_fires_again(self):
        r = self.engine.create_note(title="Pay rent", body="")
        self.engine.schedule(r["id"], "2026-11-01T09:00:00")
        N.mark_reminded(self.engine.conn, r["id"])
        self.engine.schedule(r["id"], "2026-11-01T09:10:00", snooze=True)
        self.assertEqual(self.row(r["id"])["reminded"], 0)

    def test_undo_puts_the_schedule_and_done_back(self):
        r = self.engine.create_note(title="Pay rent", body="")
        self.engine.schedule(r["id"], "2026-11-01T09:00:00")
        self.engine.complete(r["id"], True)
        self.engine.undo()
        self.assertEqual(self.row(r["id"])["status"], "open")
        self.engine.undo()
        self.assertIsNone(self.row(r["id"])["due_at"])
        self.assertIsNone(self.on_disk(r["id"]).due)

    def test_capture_writes_the_time_into_the_file(self):
        res = self.engine.capture("remind me tomorrow at 5pm to call mom")
        note = self.on_disk(res.note.id)
        self.assertTrue(note.due and note.due.endswith("17:00:00"))
        self.assertEqual(note.remind, note.due)

    def test_rebuild_keeps_times_and_done_and_does_not_refire_old_ones(self):
        r = self.engine.create_note(title="Old", body="")
        self.engine.schedule(r["id"], "2020-01-01T09:00:00")
        s = self.engine.create_note(title="Finished", body="")
        self.engine.complete(s["id"], True)
        self.engine.rebuild()
        self.assertEqual(self.row(r["id"])["due_at"], "2020-01-01T09:00:00")
        self.assertEqual(self.row(r["id"])["reminded"], 1)
        self.assertEqual(self.row(s["id"])["status"], "done")


class OlderVaults(Case):
    def test_times_kept_only_in_the_index_are_copied_into_files(self):
        r = self.engine.create_note(title="Legacy", body="")
        conn = self.engine.conn
        conn.execute("UPDATE notes SET due_at = '2026-12-01T09:00:00',"
                     " remind_at = '2026-12-01T09:00:00', status = 'done',"
                     " kind = 'task' WHERE id = ?", (r["id"],))
        conn.execute("DELETE FROM meta WHERE key = 'times_in_files'")
        root = self.engine.vault.root
        self.engine.close()
        self.engine = Engine(root, enrich=False)
        note = self.on_disk(r["id"])
        self.assertEqual(note.due, "2026-12-01T09:00:00")
        self.assertEqual(note.status, "done")
        self.assertEqual(note.kind, "task")


class LightSave(Case):
    def test_ticks_and_reordering_do_not_re_embed(self):
        r = self.engine.create_note(title="Groceries", body="")
        self.engine.update_note(r["id"], body="- [ ] milk\n- [ ] eggs")
        calls = []
        real = self.engine._embed_one
        self.engine._embed_one = lambda text: calls.append(text) or real(text)
        self.engine.update_note(r["id"], body="- [x] milk ✅ 2026-10-27\n- [ ] eggs")
        self.engine.update_note(r["id"], body="- [ ] eggs\n- [x] milk ✅ 2026-10-27")
        self.assertEqual(calls, [])
        self.engine.update_note(r["id"], body="- [ ] eggs\n- [x] milk\n- [ ] bread")
        self.assertEqual(len(calls), 1)


class DueUsesLocalTime(Case):
    def test_a_reminder_a_minute_ago_is_due_now(self):
        r = self.engine.create_note(title="Stretch", body="")
        just_now = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
        later = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
        self.engine.schedule(r["id"], just_now)
        s = self.engine.create_note(title="Later", body="")
        self.engine.schedule(s["id"], later)
        due = [row["id"] for row in N.due_soon(self.engine.conn, local_now_iso())]
        self.assertIn(r["id"], due)
        self.assertNotIn(s["id"], due)


if __name__ == "__main__":
    unittest.main(verbosity=2)
