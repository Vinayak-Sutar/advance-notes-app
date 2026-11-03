"""What speaks up, and when: items, check-ins, deadlines. Made-up data, fixed clocks."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import reminders as Rem
from mnemo import server as SRV
from mnemo.engine import Engine
from tests.test_server import _free_port

MON = datetime(2026, 11, 2, 8, 0)          # Monday 8 am


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    return (MON + timedelta(days=day_offset)).replace(hour=hour, minute=minute)


class _Lists(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), online=False, enrich=False)
        self.addCleanup(self.engine.close)
        self.conn = self.engine.conn

    def make_list(self, title, body, **settings):
        made = self.engine.create_note(title=title, body=body, kind="list")
        if settings:
            self.engine.set_list(made["id"], **settings)
        return made["id"]

    def types(self, now):
        return [(r["type"], r.get("title") or r.get("text")) for r in Rem.due_now(self.conn, now)]

    def show(self, now):
        """What the page does after showing them: mark them, so they don't come back."""
        shown = Rem.due_now(self.conn, now)
        Rem.mark(self.conn, [r["key"] for r in shown])
        return shown


class Items(_Lists):
    def setUp(self):
        super().setUp()
        self.list = self.make_list("Exam prep", "- [ ] Calculus ⏰ 2026-11-02 18:00\n"
                                                "- [x] Algebra ⏰ 2026-11-02 09:00 ✅ 2026-11-01\n"
                                                "- [ ] Optics")

    def test_an_item_speaks_at_its_time_and_only_once(self):
        self.assertEqual(self.types(at(0, 17, 59)), [])
        shown = self.show(at(0, 18))
        self.assertEqual([(r["type"], r["text"], r["listTitle"]) for r in shown],
                         [("item", "Calculus", "Exam prep")])
        self.assertEqual(shown[0]["line"], 0)
        self.assertEqual(self.types(at(0, 18, 5)), [])

    def test_a_done_item_stays_quiet(self):
        self.assertNotIn(("item", "Algebra"), self.types(at(0, 23)))

    def test_snoozing_makes_it_speak_again_at_the_new_time(self):
        self.show(at(0, 18))
        self.engine.set_item(self.list, 0, "Calculus", remind="2026-11-02T18:10:00")
        self.assertEqual(self.types(at(0, 18, 5)), [])
        self.assertEqual(self.types(at(0, 18, 10)), [("item", "Calculus")])

    def test_items_in_any_note_count_not_only_lists(self):
        self.engine.create_note(title="Chores", body="- [ ] Water plants ⏰ 2026-11-02 07:30")
        self.assertIn(("item", "Water plants"), self.types(at(0, 7, 31)))


class CheckIns(_Lists):
    def setUp(self):
        super().setUp()
        self.list = self.make_list("Exam prep", "- [ ] Algebra\n- [ ] Calculus",
                                   due="2026-11-20T18:00:00", checkin="09:00")

    def test_once_a_day_at_its_time(self):
        self.assertEqual(self.types(at(0, 8, 59)), [])
        shown = self.show(at(0, 9))
        self.assertEqual([r["type"] for r in shown], ["checkin"])
        self.assertEqual((shown[0]["done"], shown[0]["total"], shown[0]["next"]), (0, 2, "Algebra"))
        self.assertEqual(shown[0]["pace"]["state"], "on_track")
        self.assertEqual(self.types(at(0, 15)), [])
        self.assertEqual(self.types(at(1, 9)), [("checkin", "Exam prep")])   # next day

    def test_opening_the_app_late_still_gets_todays(self):
        self.assertEqual(self.types(at(0, 21)), [("checkin", "Exam prep")])

    def test_paused_until_a_date(self):
        self.engine.set_list(self.list, quiet_until="2026-11-04")
        self.assertEqual(self.types(at(0, 10)), [])
        self.assertEqual(self.types(at(1, 10)), [])
        self.assertEqual(self.types(at(2, 10)), [("checkin", "Exam prep")])

    def test_turned_off_or_finished_lists_are_quiet(self):
        self.engine.set_list(self.list, checkin="")
        self.assertEqual(self.types(at(0, 10)), [])
        other = self.make_list("Done one", "- [x] Everything", checkin="09:00")
        self.assertNotIn(("checkin", "Done one"), self.types(at(0, 10)))
        self.assertIsNotNone(other)

    def test_a_list_in_a_group_speaks_through_the_group(self):
        group = self.make_list("Semester", "- [ ] Register", checkin="09:00")
        self.engine.set_list(self.list, parent=group)
        shown = Rem.due_now(self.conn, at(0, 10))
        self.assertEqual([r["title"] for r in shown], ["Semester"])
        self.assertEqual((shown[0]["done"], shown[0]["total"]), (0, 3))       # its lists count too


class Deadlines(_Lists):
    def test_missed_once_then_no_daily_nagging(self):
        self.make_list("Trip", "- [ ] Tickets", due="2026-11-02T18:00:00", checkin="09:00")
        shown = self.show(at(0, 18, 1))
        self.assertEqual([(r["type"], r["late"]) for r in shown], [("deadline", True)])
        self.assertEqual(self.types(at(1, 9)), [])                         # no check-in now
        self.assertEqual(self.types(at(5, 9)), [])

    def test_the_day_before_for_a_list_without_check_ins(self):
        made = self.make_list("Trip", "- [ ] Tickets", due="2026-11-03T18:00:00")
        self.engine.set_list(made, checkin="")
        self.assertEqual(self.types(at(0, 8, 30)), [])
        shown = self.show(at(0, 9))
        self.assertEqual([(r["type"], r["late"]) for r in shown], [("deadline", False)])
        self.assertEqual(self.types(at(0, 12)), [])

    def test_moving_the_deadline_starts_fresh(self):
        made = self.make_list("Trip", "- [ ] Tickets", due="2026-11-02T18:00:00", checkin="09:00")
        self.show(at(0, 19))
        self.engine.set_list(made, due="2026-11-09T18:00:00")
        self.assertEqual(self.types(at(1, 9)), [("checkin", "Trip")])


class Pages(_Lists):
    def test_agenda_items_and_lists(self):
        self.make_list("Exam prep", "- [ ] Calculus ⏰ 2026-11-05 18:00\n- [ ] Optics 📅 2026-11-06\n"
                                    "- [ ] Undated\n- [x] Old 📅 2026-10-01 ✅ 2026-10-01",
                       due="2026-11-20T18:00:00")
        items = Rem.agenda_items(self.conn, at(0, 10))
        self.assertEqual([(i["text"], i["remind"], i["day"]) for i in items],
                         [("Calculus", "2026-11-05T18:00:00", None), ("Optics", None, "2026-11-06")])
        lists = Rem.active_lists(self.conn, at(0, 10))
        self.assertEqual([(l["title"], l["done"], l["total"]) for l in lists], [("Exam prep", 1, 4)])

    def test_a_rebuild_doesnt_fire_old_things_again(self):
        self.make_list("Exam prep", "- [ ] Calculus ⏰ 2020-01-01 09:00")
        self.engine.rebuild()
        self.assertEqual([r for r in Rem.due_now(self.conn) if r["type"] == "item"], [])


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="mnemo-rem-")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        SRV._engine = Engine(self.folder, online=False, enrich=False)
        past = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        SRV._engine.create_note(title="Exam prep", body=f"- [ ] Calculus ⏰ {past}", kind="list")
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

    def test_due_then_reminded(self):
        due = self.call("/api/due")["reminders"]
        self.assertEqual([(r["type"], r["text"]) for r in due], [("item", "Calculus")])
        self.call("/api/reminded", {"keys": [due[0]["key"], 42]})
        self.assertEqual(self.call("/api/due")["reminders"], [])

    def test_agenda_has_items_lists_and_anytime(self):
        SRV._engine.capture("call the bank")              # an undated to-do
        data = self.call("/api/agenda")
        self.assertEqual([i["text"] for i in data["items"]], ["Calculus"])
        self.assertIn("lists", data)
        self.assertTrue(all(n["kind"] == "task" for n in data["anytime"]))


if __name__ == "__main__":
    unittest.main()
