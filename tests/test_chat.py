"""The chat panel's brain: instructions act, questions stream from the model."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import notes as N
from mnemo import topics as T
from mnemo.chat import Chat, ModelUnavailable, _SourcesFilter
from mnemo.engine import Engine


class FakeModel:
    def __init__(self, pieces=(), fail=None):
        self.pieces, self.fail = list(pieces), fail
        self.calls, self.closed = [], False

    def stream(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise self.fail
        yield from self.pieces

    def close(self):
        self.closed = True


def run(engine, message, model, history=None):
    return list(Chat(engine, model=model).events(message, history or []))


def text_of(events):
    return "".join(e["text"] for e in events if e["type"] == "token")


def of_type(events, kind):
    return [e for e in events if e["type"] == kind]


class Instructions(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)

    def tearDown(self):
        self.engine.close()

    def test_reminder_is_scheduled_without_the_model(self):
        model = FakeModel()
        events = run(self.engine, "remind me to call mom tomorrow at 6 pm", model)
        self.assertEqual(model.calls, [])
        action = of_type(events, "action")[0]["action"]
        self.assertEqual(action["kind"], "reminder")
        row = N.get(self.engine.conn, action["noteId"])
        self.assertTrue(row["due_at"].endswith("18:00:00"))
        self.assertIn("remind you", text_of(events))
        self.assertTrue(of_type(events, "done")[0]["fromNotes"])

    def test_set_a_reminder_phrasing(self):
        events = run(self.engine, "set a reminder to water the plants tomorrow at 9 am", FakeModel())
        action = of_type(events, "action")[0]["action"]
        self.assertEqual(action["kind"], "reminder")
        self.assertIn("water the plants", action["summary"].lower())

    def test_reminder_can_be_undone(self):
        events = run(self.engine, "remind me to pay rent tomorrow at 10 am", FakeModel())
        note_id = of_type(events, "action")[0]["action"]["noteId"]
        self.engine.undo()
        self.assertIsNone(N.get(self.engine.conn, note_id))

    def test_remind_me_what_is_a_question(self):
        model = FakeModel(["Nothing yet.\nSources: none"])
        events = run(self.engine, "remind me what I saved yesterday?", model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(of_type(events, "action"), [])

    def test_save_as_note(self):
        events = run(self.engine, "save this as a note: buy a new kettle", FakeModel())
        action = of_type(events, "action")[0]["action"]
        self.assertEqual(action["kind"], "note")
        row = N.get(self.engine.conn, action["noteId"])
        self.assertIn("kettle", (row["title"] + row["body"]).lower())

    def test_move_to_a_collection(self):
        self.engine.capture('@"Movies" Dune part two trailer')
        work = self.engine.capture('@"Work" send the invoice to Brightside')
        events = run(self.engine, "move the invoice note to movies", FakeModel())
        action = of_type(events, "action")[0]["action"]
        self.assertEqual(action["kind"], "move")
        self.assertEqual(action["noteId"], work.note.id)
        topic = N.primary_topic(self.engine.conn, work.note.id)
        self.assertEqual(T.path_of(self.engine.conn, topic), "Movies")

    def test_move_of_unknown_note_says_so(self):
        events = run(self.engine, "move the zebra spreadsheet note to work", FakeModel())
        self.assertEqual(of_type(events, "action"), [])
        self.assertIn("couldn't find", text_of(events))


class Questions(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)
        self.dune = self.engine.capture('@"Movies" Dune part two trailer, rewatch part one first')
        self.engine.capture('@"Work" send the invoice to Brightside')

    def tearDown(self):
        self.engine.close()

    def test_answer_streams_and_the_sources_line_is_hidden(self):
        model = FakeModel(["You wanted to see ", "**Dune**.\nSour", "ces: 1"])
        events = run(self.engine, "what did I note about the dune trailer?", model)
        self.assertEqual(text_of(events), "You wanted to see **Dune**.\n")
        prompt = model.calls[0][-1]["content"]
        first = prompt.split("[1] ", 1)[1].split(" (", 1)[0]
        sources = of_type(events, "sources")[0]["notes"]
        self.assertEqual([s["title"] for s in sources], [first])
        self.assertTrue(of_type(events, "done")[0]["fromNotes"])
        self.assertTrue(model.closed)

    def test_sources_none_means_not_from_notes(self):
        model = FakeModel(["Paris is the capital of France.\n", "Sources: none"])
        events = run(self.engine, "what is the capital of france?", model)
        self.assertEqual(of_type(events, "sources")[0]["notes"], [])
        self.assertFalse(of_type(events, "done")[0]["fromNotes"])

    def test_unrelated_questions_get_no_notes(self):
        model = FakeModel(["Paris."])
        events = run(self.engine, "what is the capital of france?", model)
        self.assertIn("(no notes matched)", model.calls[0][-1]["content"])
        self.assertFalse(of_type(events, "done")[0]["fromNotes"])

    def test_without_a_sources_line_the_titles_it_names_are_the_sources(self):
        title = N.get(self.engine.conn, self.dune.note.id)["title"]
        model = FakeModel([f"You saved **{title}**."])
        events = run(self.engine, "what did I note about the dune trailer?", model)
        sources = of_type(events, "sources")[0]["notes"]
        self.assertEqual([s["id"] for s in sources], [self.dune.note.id])

    def test_a_note_named_in_the_question_is_always_considered(self):
        model = FakeModel(["Ok.\nSources: none"])
        title = N.get(self.engine.conn, self.dune.note.id)["title"]
        run(self.engine, f"summarise {title.lower()} please", model)
        self.assertIn(title, model.calls[0][-1]["content"])

    def test_history_is_passed_to_the_model(self):
        model = FakeModel(["Yes.\nSources: none"])
        run(self.engine, "and the second one?", model,
            history=[{"role": "user", "content": "first question"},
                     {"role": "assistant", "content": "first answer"}])
        roles = [m["role"] for m in model.calls[0]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])

    def test_without_the_model_it_still_points_at_notes(self):
        events = run(self.engine, "what about dune?", FakeModel(fail=ModelUnavailable("down")))
        self.assertIn("isn't running", text_of(events))
        self.assertTrue(of_type(events, "sources")[0]["notes"])

    def test_without_the_model_or_any_notes_it_reports_an_error(self):
        empty = Engine(tempfile.mkdtemp(), enrich=False)
        try:
            events = run(empty, "anything there?", FakeModel(fail=ModelUnavailable("down")))
            self.assertEqual(events[-1]["type"], "error")
        finally:
            empty.close()


class ToolCalling(unittest.TestCase):
    """The chat with a model that calls tools itself."""

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
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)
        self.trip = self.engine.capture('@"Travel" Goa trip: hotel for 4 nights, buy sunscreen')

    def tearDown(self):
        self.engine.close()

    def run_chat(self, model, message, web=None):
        from mnemo.websearch import WebConfig
        return list(Chat(self.engine, tool_model=model).events(
            message, [], web=web or WebConfig(mode="off")))

    def test_calculator_step_then_answer(self):
        model = self.Scripted([("calls", [self.call("calculator", expression="1847 * 23")])],
                              [("token", "That's 42,481.\nSources: none")])
        events = self.run_chat(model, "what is 1847 times 23?")
        step = [e for e in events if e["type"] == "step"][0]
        self.assertEqual(step["label"], "Working out 1847 * 23")
        self.assertEqual(text_of(events), "That's 42,481.\n")
        self.assertNotIn("web_search", model.seen[0])          # web is off

    def test_search_notes_adds_numbered_sources(self):
        model = self.Scripted([("calls", [self.call("search_notes", query="goa trip")])],
                              [("token", "Buy sunscreen.\nSources: 1")])
        events = self.run_chat(model, "anything else for the trip to the beach?")
        sources = of_type(events, "sources")[0]["notes"]
        self.assertEqual([s["id"] for s in sources], [self.trip.note.id])
        self.assertTrue(of_type(events, "done")[0]["fromNotes"])

    def test_reminder_tool_makes_an_undoable_action(self):
        model = self.Scripted([("calls", [self.call("create_reminder", text="call the bank",
                                                     when="tomorrow at 10am")])],
                              [("token", "Done.\nSources: none")])
        events = self.run_chat(model, "can you make sure I don't forget the bank tomorrow?")
        action = of_type(events, "action")[0]["action"]
        self.assertEqual(action["kind"], "reminder")
        self.assertTrue(N.get(self.engine.conn, action["noteId"])["due_at"].endswith("10:00:00"))

    def test_web_tool_results_become_web_sources(self):
        from mnemo.websearch import WebConfig, WebResult
        page = WebResult("2026 Valorant Champions", "https://en.wikipedia.org/wiki/2026_Valorant_Champions",
                         text="LOUD won.")
        model = self.Scripted([("calls", [self.call("web_search", query="vct leader")])],
                              [("token", "LOUD.\nSources: W1")])
        chat = Chat(self.engine, tool_model=model, searcher=lambda q, cfg: [page])
        events = list(chat.events("tell me about the vct", [], web=WebConfig()))
        self.assertEqual(of_type(events, "searching")[0]["query"], "vct leader")
        self.assertEqual(of_type(events, "web")[0]["results"][0]["site"], "en.wikipedia.org")
        self.assertTrue(of_type(events, "done")[0]["fromWeb"])


class SourcesFilter(unittest.TestCase):
    def feed_all(self, pieces):
        f = _SourcesFilter()
        out = "".join(f.feed(p) for p in pieces) + f.finish()
        return out, f.cited

    def test_lines_that_only_start_like_sources_pass_through(self):
        out, cited = self.feed_all(["So the ", "answer is yes"])
        self.assertEqual(out, "So the answer is yes")
        self.assertIsNone(cited)

    def test_bold_sources_line_is_parsed(self):
        out, cited = self.feed_all(["Done.\n", "**Sources:** 2, 3"])
        self.assertEqual(out, "Done.\n")
        self.assertEqual(cited, [2, 3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
