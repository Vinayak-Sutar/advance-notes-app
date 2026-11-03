"""To-do lists said in words: the capture box, chat, the AI box in a note, the API.

Made-up data only.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import checklist as CL
from mnemo import notes as N
from mnemo import noteai as NA
from mnemo import server as SRV
from mnemo.chat import Chat
from mnemo.engine import Engine
from mnemo.intent import parse
from tests.test_chat import FakeModel, of_type, text_of
from tests.test_server import _free_port

NOW = datetime(2026, 11, 3, 10, 0)      # a Tuesday morning


class Phrases(unittest.TestCase):
    def test_a_named_list_with_a_deadline(self):
        i = parse("exam prep list: algebra, calculus and probability by 15 nov", NOW)
        self.assertEqual(i.kind, "list")
        self.assertEqual(i.list_title, "Exam prep")
        self.assertEqual(i.list_items, ["Algebra", "Calculus", "Probability"])
        self.assertEqual(i.due_at, datetime(2026, 11, 15, 18, 0))    # by that evening
        self.assertEqual(i.text, "- [ ] Algebra\n- [ ] Calculus\n- [ ] Probability")

    def test_a_deadline_with_a_time_keeps_it(self):
        i = parse("trip list: tickets, hotel by friday 10am", NOW)
        self.assertEqual(i.due_at, datetime(2026, 11, 6, 10, 0))

    def test_other_ways_of_saying_it(self):
        for raw, title in [
            ("make a list for the trip: tickets, hotel, sunscreen", "Trip"),
            ("shopping list: milk; eggs; bread", "Shopping"),
            ("create a to-do list called garden - weeding, seeds", "Garden"),
        ]:
            i = parse(raw, NOW)
            self.assertEqual((i.kind, i.list_title), ("list", title), raw)
            self.assertGreaterEqual(len(i.list_items), 2, raw)

    def test_typed_bullets_become_a_list_titled_by_the_first_line(self):
        i = parse("Packing\n- passport\n- charger\n- [ ] snacks", NOW)
        self.assertEqual((i.kind, i.list_title), ("list", "Packing"))
        self.assertEqual(i.list_items, ["Passport", "Charger", "Snacks"])

    def test_adding_to_a_list_keeps_the_time(self):
        i = parse("add optics to the exam prep list thursday 6pm", NOW)
        self.assertEqual(i.append_to, "exam prep")
        self.assertTrue(i.append_item)
        self.assertEqual(i.due_at, datetime(2026, 11, 5, 18, 0))
        self.assertIn("optics", i.text.lower())

    def test_pausing_a_list(self):
        i = parse("don't remind me about exam prep until monday", NOW)
        self.assertEqual(i.pause_target, "exam prep")
        self.assertEqual(i.pause_until.isoformat(), "2026-11-09")

    def test_ordinary_sentences_are_not_lists(self):
        for raw in ["I made a list of ideas yesterday",
                    "the shopping list is on the fridge",
                    "remind me to check the guest list tomorrow at 5pm",
                    "add milk to the shopping note"]:
            self.assertNotEqual(parse(raw, NOW).kind, "list", raw)
            self.assertFalse(parse(raw, NOW).append_item, raw)


class InTheEngine(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), online=False, enrich=False)
        self.addCleanup(self.engine.close)

    def made(self, raw):
        res = self.engine.capture(raw)
        row = N.get(self.engine.conn, res.note.id)
        return res, row, self.engine.vault.read(row["path"])

    def test_a_list_capture_makes_a_list_note(self):
        res, row, note = self.made("exam prep list: algebra, calculus by 15 nov")
        self.assertEqual((row["kind"], row["title"]), ("list", "Exam prep"))
        self.assertEqual(res.list_items, 2)
        self.assertEqual(CL.progress(note.body), (0, 2))
        self.assertTrue(note.due.startswith("2026-11-15") or note.due.endswith("18:00:00"))
        self.assertEqual(note.checkin, self.engine.checkin_default)     # a deadline checks in

    def test_adding_to_a_list_writes_a_checkbox_with_its_reminder(self):
        res, _, _ = self.made("exam prep list: algebra, calculus")
        added = self.engine.capture("add optics to the exam prep list thursday 6pm")
        self.assertEqual(added.appended_to, res.note.id)
        body = self.engine.vault.read(N.get(self.engine.conn, res.note.id)["path"]).body
        line = body.splitlines()[-1]
        self.assertTrue(line.startswith("- [ ] Optics ⏰ "), line)
        self.assertTrue(line.endswith(" 18:00"), line)

    def test_adding_to_a_list_that_doesnt_exist_makes_it(self):
        target = self.engine.add_to_named_list("garden", "weeding")
        row = N.get(self.engine.conn, target)
        self.assertEqual((row["kind"], row["title"]), ("list", "Garden"))
        self.assertEqual(CL.parse(self.engine.vault.read(row["path"]).body).items[0].text, "Weeding")

    def test_pausing_sets_quiet_until_on_the_list(self):
        res, _, _ = self.made("exam prep list: algebra, calculus")
        paused = self.engine.capture("don't remind me about exam prep until monday")
        self.assertEqual(paused.note.id, res.note.id)
        note = self.engine.vault.read(N.get(self.engine.conn, res.note.id)["path"])
        self.assertEqual(note.quiet_until, paused.paused_until)
        self.assertIsNone(self.engine.pause_list("no such thing", "2026-12-01"))


class InChat(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), online=False, enrich=False)
        self.addCleanup(self.engine.close)

    def chat(self, message):
        model = FakeModel(["should not be asked"])
        events = list(Chat(self.engine, model=model).events(message, []))
        self.assertEqual(model.calls, [], message)              # done without the model
        return events

    def test_making_a_list_in_chat(self):
        events = self.chat("exam prep list: algebra, calculus by 15 nov")
        self.assertIn("Made the to-do list **Exam prep** with 2 items", text_of(events))
        action = of_type(events, "action")[0]["action"]
        self.assertEqual(N.get(self.engine.conn, action["noteId"])["kind"], "list")

    def test_adding_and_pausing_in_chat(self):
        self.chat("exam prep list: algebra, calculus")
        self.assertIn("Added it to **Exam prep**", text_of(self.chat("add optics to the exam prep list")))
        self.assertIn("I won't check in about **Exam prep** until",
                      text_of(self.chat("don't remind me about exam prep until monday")))

    def test_pausing_a_list_that_doesnt_exist_changes_nothing(self):
        before = self.engine.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        events = self.chat("don't remind me about chess club until monday")
        self.assertIn("couldn't find a to-do list", text_of(events))
        self.assertEqual(before, self.engine.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"])

    def test_a_question_about_a_list_is_still_a_question(self):
        model = FakeModel(["You have two items.\nSources: none"])
        list(Chat(self.engine, model=model).events("what's on my exam prep list: anything left?", []))
        self.assertEqual(len(model.calls), 1)


class ChatTools(unittest.TestCase):
    """A model that calls the to-do list tools itself."""

    class Scripted:
        def __init__(self, *turns):
            self.turns, self.seen = list(turns), []

        def turn(self, messages, tools):
            self.seen.append([t["function"]["name"] for t in tools])
            yield from self.turns.pop(0)

        def close(self):
            pass

    @staticmethod
    def call(name, **args):
        return {"function": {"name": name, "arguments": args}}

    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), online=False, enrich=False)
        self.addCleanup(self.engine.close)

    def run_chat(self, *turns):
        from mnemo.websearch import WebConfig
        model = self.Scripted(*turns, [("token", "Done.\nSources: none")])
        events = list(Chat(self.engine, tool_model=model).events(
            "help me plan my exam", [], web=WebConfig(mode="off")))
        return model, events

    def lists(self):
        return {r["title"]: r for r in N.lists(self.engine.conn)}

    def test_the_tools_are_offered(self):
        model, _ = self.run_chat()
        self.assertTrue({"create_list", "add_to_list", "pause_reminders"} <= set(model.seen[0]))

    def test_create_add_and_pause(self):
        _, events = self.run_chat(
            [("calls", [self.call("create_list", title="Exam prep list",
                                  items="algebra, calculus", due="15 nov")])],
            [("calls", [self.call("add_to_list", list="the exam prep list", item="optics",
                                  when="tomorrow 6pm")])],
            [("calls", [self.call("pause_reminders", list="exam prep", until="monday")])])
        made = self.lists()["Exam prep"]
        note = self.engine.vault.read(made["path"])
        self.assertEqual([it.text for it in CL.parse(note.body).items],
                         ["Algebra", "Calculus", "Optics"])
        self.assertIsNotNone(CL.parse(note.body).items[-1].remind)
        self.assertIsNotNone(note.quiet_until)
        self.assertEqual(len(of_type(events, "action")), 3)

    def test_pausing_an_unknown_list_says_so(self):
        _, events = self.run_chat(
            [("calls", [self.call("pause_reminders", list="chess", until="monday")])])
        self.assertEqual(of_type(events, "action"), [])


class NoteAiKeepsMarks(unittest.TestCase):
    BODY = "## Maths\n- [ ] Probability ⏰ 2026-11-05 18:00\n- [x] Calculus ✅ 2026-10-27"

    def test_marks_dropped_by_a_rewrite_come_back(self):
        rewritten = "## Maths\n- [ ] Probability theory\n- [ ] Probability\n- [x] Calculus"
        kept = NA._keep_marks(self.BODY, rewritten)
        self.assertIn("- [ ] Probability ⏰ 2026-11-05 18:00", kept)
        self.assertIn("- [x] Calculus ✅ 2026-10-27", kept)
        self.assertIn("- [ ] Probability theory\n", kept)                # a new line stays bare

    def test_a_line_kept_without_its_marks_is_not_counted_as_lost(self):
        self.assertEqual(NA._dropped(self.BODY, "## Maths\n- [ ] Probability\n- [x] Calculus"), [])

    def test_a_replace_through_respond_keeps_them(self):
        class Model:
            def complete(self, messages, schema):
                return json.dumps({"action": "replace", "reply": "Sorted.",
                                   "text": "## Maths\n- [x] Calculus\n- [ ] Probability",
                                   "position": "end"})
        out = NA.respond("Exam prep", self.BODY, "sort the done ones first", model=Model())
        self.assertIn("⏰ 2026-11-05 18:00", out["text"])
        self.assertIn("✅ 2026-10-27", out["text"])


class OverHttp(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="mnemo-lists-")
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

    def test_new_list_from_the_button(self):
        made = self.call("/api/note/new", {"title": "Groceries", "kind": "list"})
        self.assertEqual(N.get(SRV._engine.conn, made["id"])["kind"], "list")
        odd = self.call("/api/note/new", {"title": "Odd", "kind": "anything"})
        self.assertEqual(N.get(SRV._engine.conn, odd["id"])["kind"], "note")

    def test_saving_a_list_says_how_many_items(self):
        res = self.call("/api/capture", {"text": "trip list: tickets, hotel, sunscreen"})
        self.assertEqual((res["kind"], res["listItems"]), ("list", 3))
        self.call("/api/capture", {"text": "don't remind me about trip until friday"})
        lists = self.call("/api/lists")["lists"]
        self.assertEqual(lists[0]["title"], "Trip")
        self.assertEqual((lists[0]["list"]["done"], lists[0]["list"]["total"]), (0, 3))
        self.assertIsNotNone(lists[0]["list"]["quietUntil"])


if __name__ == "__main__":
    unittest.main()
