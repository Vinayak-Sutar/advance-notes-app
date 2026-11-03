"""The benchmark's judging rules, the tool loop, and the model helpers."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import agent as A
from mnemo import bench as B
from mnemo import runtime as R

CHAT = json.loads((B.DATA / "chat.json").read_text(encoding="utf-8"))


class Scoring(unittest.TestCase):
    def test_note_ai_action_and_content(self):
        case = {"request": "make this a checklist", "body": "Groceries\nmilk\neggs",
                "expect": ["replace"], "text_has": ["[ ]"], "keep_lines": True}
        good = {"action": "replace", "text": "Groceries\n- [ ] milk\n- [ ] eggs"}
        lossy = {"action": "replace", "text": "- [ ] milk\n- [ ] eggs"}
        self.assertEqual(B.score_noteai(case, good), (True, ""))
        ok, why = B.score_noteai(case, lossy)
        self.assertFalse(ok)
        self.assertIn("lost lines", why)
        self.assertFalse(B.score_noteai(case, {"action": "answer", "reply": "ok"})[0])

    def test_note_ai_order_and_removal(self):
        order = {"request": "sort", "body": "- b\n- a", "expect": ["replace"],
                 "text_order": ["apple", "banana"]}
        self.assertTrue(B.score_noteai(order, {"action": "replace", "text": "- apple\n- banana"})[0])
        self.assertFalse(B.score_noteai(order, {"action": "replace", "text": "- banana\n- apple"})[0])
        gone = {"request": "remove bright", "body": "x", "expect": ["replace"], "text_not": ["bright"]}
        self.assertFalse(B.score_noteai(gone, {"action": "replace", "text": "warm, bright"})[0])

    def test_chat_honesty(self):
        no = {"q": "passport?", "no_answer": True}
        self.assertTrue(B.score_chat(no, "I don't see your passport number in your notes.", False)[0])
        self.assertTrue(B.score_chat(no, "That isn't in your notes.", False)[0])
        self.assertFalse(B.score_chat(no, "Your passport number is K1234567.", False)[0])
        yes = {"q": "groceries?", "all": ["milk", "eggs"]}
        self.assertTrue(B.score_chat(yes, "Milk and eggs.", True)[0])
        self.assertFalse(B.score_chat(yes, "Just milk.", True)[0])

    def test_tool_choice(self):
        calc = {"tool": ["calculator"], "args_any": ["1847"]}
        call = [{"function": {"name": "calculator", "arguments": {"expression": "1847*23"}}}]
        self.assertTrue(B.score_tool(calc, call)[0])
        self.assertFalse(B.score_tool(calc, [])[0])
        chit = {"tool": [None]}
        self.assertTrue(B.score_tool(chit, [])[0])
        self.assertFalse(B.score_tool(chit, call)[0])

    def test_similarity(self):
        self.assertEqual(B.similarity("Hello  World", "hello world"), 1.0)
        self.assertLess(B.similarity("abc", "xyz"), 0.2)

    def test_bench_data_is_consistent(self):
        """Every answerable chat question names a real note that holds its answer."""
        notes = {n["title"]: n for n in CHAT["notes"]}
        for case in CHAT["questions"]:
            if case.get("no_answer"):
                self.assertNotIn("note", case)
                continue
            note = notes[case["note"]]
            where = (note["title"] + " " + note["body"] + " " + str(note.get("due"))).lower()
            words = case.get("all") or case.get("any")
            self.assertTrue(any(w in where for w in words), case["q"])


class FakeToolModel:
    """Replays scripted turns: each is a list of ("token", text) / ("calls", [...])."""
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), []

    def turn(self, messages, tools):
        self.seen.append((list(messages), [t["function"]["name"] for t in tools]))
        yield from self.turns.pop(0)

    def close(self):
        pass


def call(name, **args):
    return {"function": {"name": name, "arguments": args}}


class AgentLoop(unittest.TestCase):
    def test_tool_then_answer(self):
        model = FakeToolModel([("calls", [call("calculator", expression="1847*23")])],
                              [("token", "It's 42,481.")])
        events = list(A.run(model, [{"role": "user", "content": "1847*23?"}],
                            {"calculator": lambda a: A.ToolResult(text="42481")}))
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["step", "tool", "token"])
        self.assertEqual(events[0]["label"], "Working out 1847*23")
        msgs = model.seen[1][0]
        self.assertEqual(msgs[-1], {"role": "tool", "content": "42481", "tool_name": "calculator"})

    def test_last_round_must_answer(self):
        loop = [("calls", [call("search_notes", query="x")])]
        model = FakeToolModel(loop, loop, [("token", "done")])
        list(A.run(model, [], {"search_notes": lambda a: A.ToolResult(text="")}, max_steps=2))
        self.assertEqual(model.seen[-1][1], [])          # no tools offered on the final turn

    def test_unknown_tool_and_broken_tool_dont_crash(self):
        def boom(args):
            raise ValueError("nope")
        model = FakeToolModel([("calls", [call("teleport"), call("calculator", expression="1/0")])],
                              [("token", "sorry")])
        events = list(A.run(model, [], {"calculator": boom}))
        results = [e["result"].text for e in events if e["type"] == "tool"]
        self.assertIn("no tool called teleport", results[0])
        self.assertIn("didn't work", results[1])

    def test_string_arguments_are_parsed(self):
        self.assertEqual(A._args({"function": {"arguments": '{"query": "goa"}'}}), {"query": "goa"})


class RuntimeHelpers(unittest.TestCase):
    def test_has_model_wants_the_exact_size(self):
        with mock.patch.object(R, "installed_models", return_value=["qwen3.5:4b", "nomic-embed-text:latest"]):
            self.assertTrue(R.has_model("qwen3.5:4b"))
            self.assertFalse(R.has_model("qwen3.5:9b"))
            self.assertTrue(R.has_model("nomic-embed-text"))

    def test_think_option_only_for_thinking_models(self):
        with mock.patch.object(R, "capabilities", side_effect=[{"completion", "thinking"}, {"completion"}]):
            R._CAPS.clear()
            self.assertEqual(R.think_option("a"), {"think": False})
            self.assertEqual(R.think_option("b", True), {})
        R._CAPS.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
