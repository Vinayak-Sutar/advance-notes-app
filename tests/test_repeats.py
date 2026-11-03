"""Things that repeat, and lists in groups. Made-up data, fixed dates."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import checklist as CL
from mnemo import notes as N
from mnemo import reminders as Rem
from mnemo import server as SRV
from mnemo.engine import Engine
from mnemo.intent import parse
from tests.test_server import _free_port

WED = datetime(2026, 11, 4, 10, 0)          # a Wednesday morning


class Phrases(unittest.TestCase):
    def test_repeats_said_in_words(self):
        for raw, title, rec, due in [
            ("water the plants every monday 9am", "water the plants", "weekly", datetime(2026, 11, 9, 9)),
            ("remind me to pay rent every month on the 1st", "pay rent", "monthly", datetime(2026, 12, 1, 9)),
            ("gym every day at 7am", "gym", "daily", datetime(2026, 11, 5, 7)),
            ("review notes every other week at 5pm", "review notes", "every 2 weeks", datetime(2026, 11, 4, 17)),
            ("remind me to stretch every 3 days at 8pm", "stretch", "every 3 days", datetime(2026, 11, 4, 20)),
        ]:
            i = parse(raw, WED)
            self.assertEqual((i.text, i.recurrence, i.due_at), (title, rec, due), raw)

    def test_weekdays_start_on_a_weekday(self):
        i = parse("call mom every weekday at 6pm", datetime(2026, 11, 7, 10))     # a Saturday
        self.assertEqual((i.recurrence, i.due_at), ("weekdays", datetime(2026, 11, 9, 18)))

    def test_ordinary_writing_keeps_its_words(self):
        for raw in ["I talk to her every day", "the weekly meeting notes are long", "monthly budget ideas"]:
            i = parse(raw, WED)
            self.assertEqual((i.text, i.recurrence), (raw, None), raw)


class Rules(unittest.TestCase):
    def test_reading_a_repeat(self):
        self.assertEqual(Rem.parse_rule("weekly"), ("week", 1))
        self.assertEqual(Rem.parse_rule("every 2 weeks"), ("week", 2))
        self.assertEqual(Rem.parse_rule("Every Monday"), ("week", 1))
        self.assertEqual(Rem.parse_rule("weekdays"), ("weekday", 1))
        self.assertIsNone(Rem.parse_rule("now and then"))

    def test_the_next_time_round(self):
        nine = datetime(2026, 11, 4, 9)
        self.assertEqual(Rem.next_after(nine, ("day", 1), nine), datetime(2026, 11, 5, 9))
        self.assertEqual(Rem.next_after(nine, ("week", 1), datetime(2026, 11, 20)), datetime(2026, 11, 25, 9))
        fri = datetime(2026, 11, 6, 9)
        self.assertEqual(Rem.next_after(fri, ("weekday", 1), fri), datetime(2026, 11, 9, 9))
        jan31 = datetime(2026, 1, 31, 9)
        self.assertEqual(Rem.next_after(jan31, ("month", 1), jan31), datetime(2026, 2, 28, 9))
        self.assertEqual(Rem.next_after(jan31, ("month", 1), datetime(2026, 3, 1)), datetime(2026, 3, 31, 9))

    def test_when_a_round_begins(self):
        self.assertEqual(Rem.period_start(("day", 1), WED), datetime(2026, 11, 4))
        self.assertEqual(Rem.period_start(("week", 1), WED), datetime(2026, 11, 2))       # Monday
        self.assertEqual(Rem.period_start(("month", 1), WED), datetime(2026, 11, 1))
        sat = datetime(2026, 11, 7, 10)
        self.assertEqual(Rem.period_start(("weekday", 1), sat), datetime(2026, 11, 6))   # Friday's round


class Items(unittest.TestCase):
    def roll(self, line, day):
        return Rem.roll_items(line, day).split("\n")[0]

    def test_a_ticked_daily_item_comes_back_tomorrow_with_its_reminder(self):
        line = "- [x] Water plants ⏰ 2026-11-04 09:00 🔁 daily ✅ 2026-11-04"
        self.assertEqual(self.roll(line, date(2026, 11, 4)), line)                       # still today
        self.assertEqual(self.roll(line, date(2026, 11, 5)), "- [ ] Water plants ⏰ 2026-11-05 09:00 🔁 daily")

    def test_ticked_early_counts_for_the_coming_turn(self):
        line = "- [x] Bins 📅 2026-11-09 🔁 weekly ✅ 2026-11-04"
        self.assertEqual(self.roll(line, date(2026, 11, 9)), line)
        self.assertEqual(self.roll(line, date(2026, 11, 16)), "- [ ] Bins 📅 2026-11-16 🔁 weekly")

    def test_an_undated_item_comes_back_a_round_after_it_was_done(self):
        line = "- [x] Clean desk 🔁 every week ✅ 2026-11-04"
        self.assertEqual(self.roll(line, date(2026, 11, 10)), line)
        self.assertEqual(self.roll(line, date(2026, 11, 11)), "- [ ] Clean desk 🔁 every week")

    def test_unticked_or_one_off_items_are_left_alone(self):
        for line in ["- [ ] Water plants 🔁 daily", "- [x] Once ✅ 2026-11-01", "plain text 🔁 daily"]:
            self.assertEqual(self.roll(line, date(2026, 12, 1)), line)


class InTheEngine(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), online=False, enrich=False)
        self.addCleanup(self.engine.close)

    def note(self, note_id):
        return self.engine.vault.read(N.get(self.engine.conn, note_id)["path"])

    def test_a_repeating_reminder_keeps_its_repeat_and_moves_on_when_ticked(self):
        res = self.engine.capture("remind me to water the plants every monday 9am")
        self.assertEqual(self.note(res.note.id).repeat, "weekly")
        moved = self.engine.complete(res.note.id, True)
        note = self.note(res.note.id)
        self.assertEqual(note.status, "open")
        self.assertEqual(moved["next"], note.remind)
        self.assertEqual(datetime.fromisoformat(note.due) - datetime.fromisoformat(res.due_at), timedelta(days=7))
        self.engine.undo()
        self.assertEqual(self.note(res.note.id).due, res.due_at)

    def test_a_repeating_dated_to_do_moves_on_without_starting_to_ring(self):
        res = self.engine.capture("gym every day at 7am")              # no "remind me": a dated to-do
        self.engine.complete(res.note.id, True)
        note = self.note(res.note.id)
        self.assertEqual((note.status, note.remind), ("open", None))
        self.assertGreater(note.due, res.due_at)

    def test_a_one_off_reminder_is_simply_done(self):
        res = self.engine.capture("remind me to call the bank tomorrow at 10am")
        self.assertEqual(self.engine.complete(res.note.id, True), {})
        self.assertEqual(self.note(res.note.id).status, "done")

    def test_adding_a_repeating_item_to_a_list(self):
        self.engine.capture("chores list: dishes, laundry")
        self.engine.capture("add water plants to the chores list every week")
        row = N.find_by_title(self.engine.conn, "chores")[0]
        self.assertEqual(CL.parse(self.note(row["id"]).body).items[-1].repeat, "weekly")

    def test_a_repeating_list_unticks_itself_each_round(self):
        made = self.engine.create_note(title="Weekly reset", body="- [x] Inbox zero\n- [ ] Plan week", kind="list")
        self.engine.set_list(made["id"], repeat="weekly")
        self.engine.advance_repeats(datetime.now())
        self.assertEqual(CL.progress(self.note(made["id"]).body), (1, 2))          # this round kept
        next_round = datetime.now() + timedelta(days=7)
        self.assertEqual(self.engine.advance_repeats(next_round), 1)
        self.assertEqual(CL.progress(self.note(made["id"]).body), (0, 2))
        self.assertEqual(self.engine.advance_repeats(next_round), 0)               # once per round

    def test_items_come_round_through_the_engine_too(self):
        made = self.engine.create_note(
            title="Chores", body="- [x] Water plants 🔁 daily ✅ 2026-11-04", kind="list")
        self.engine.advance_repeats(datetime(2026, 11, 5, 8))
        self.assertEqual(CL.parse(self.note(made["id"]).body).items[0].done, False)

    def test_a_rebuild_doesnt_reset_a_list_mid_round(self):
        made = self.engine.create_note(title="Weekly reset", body="- [x] Inbox zero", kind="list")
        self.engine.set_list(made["id"], repeat="weekly")
        self.engine.rebuild()
        self.assertEqual(self.engine.advance_repeats(datetime.now()), 0)


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="mnemo-rep-")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        SRV._engine = Engine(self.folder, online=False, enrich=False)
        self.port = _free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), SRV.Handler)
        self.httpd.verbose = False
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        SRV._engine.close()

    def call(self, path, payload=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if payload is None else json.dumps(payload).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_ticking_a_repeating_reminder_says_when_next(self):
        res = self.call("/api/capture", {"text": "gym every day at 7am"})
        got = self.call("/api/note/complete", {"id": res["id"], "done": True})
        self.assertTrue(got["next"].endswith("07:00:00"))
        self.assertEqual(self.call(f"/api/note?id={res['id']}")["repeat"], "daily")

    def test_a_group_shows_its_lists(self):
        group = self.call("/api/note/new", {"title": "Semester", "kind": "list"})
        child = self.call("/api/note/new", {"title": "Maths", "kind": "list", "body": "- [ ] Algebra"})
        self.call("/api/list/settings", {"id": child["id"], "parent": group["id"]})
        note = self.call(f"/api/note?id={group['id']}")
        self.assertEqual([c["title"] for c in note["childLists"]], ["Maths"])
        self.assertEqual((note["list"]["done"], note["list"]["total"]), (0, 1))

    def test_due_reports_what_came_round(self):
        SRV._engine.create_note(title="Chores", body="- [x] Water 🔁 daily ✅ 2020-01-01", kind="list")
        self.assertEqual(self.call("/api/due")["rolled"], 1)
        self.assertEqual(self.call("/api/due")["rolled"], 0)


if __name__ == "__main__":
    unittest.main()
