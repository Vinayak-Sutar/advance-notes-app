"""The AI box inside a note, and the exact calculator behind its maths."""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import calc
from mnemo import noteai as A

NOTE = "Paint ideas for the bedroom\n\n- warm\n- calm"


class FakeJson:
    """Replies with canned JSON, one per call, and records what it was sent."""

    def __init__(self, *replies):
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.calls = []

    def complete(self, messages, schema):
        self.calls.append((messages, schema))
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


def reply(action, text="", position="end", said="ok"):
    return {"action": action, "reply": said, "text": text, "position": position}


class Calculator(unittest.TestCase):
    def test_sums_in_words(self):
        found = calc.facts("what is 23 x 17 and 15% of 80, then sqrt of 144 and (2+3)^2?")
        self.assertIn("23 x 17 = 391", found)
        self.assertIn("15% of 80 = 12", found)
        self.assertIn("square root of 144 = 12", found)
        self.assertIn("(2+3)^2 = 25", found)

    def test_thousands_and_decimals(self):
        self.assertIn("1,250 * 4 = 5,000", calc.facts("1,250 * 4"))
        self.assertIn("10 / 4 = 2.5", calc.facts("10 / 4"))

    def test_dates_and_plain_numbers_are_not_sums(self):
        self.assertEqual(calc.facts("meeting on 2026-10-25 with 3 people"), [])

    def test_nothing_dangerous_is_evaluated(self):
        self.assertIsNone(calc.evaluate("__import__('os')"))
        self.assertIsNone(calc.evaluate("9 ** 9999"))
        self.assertIsNone(calc.evaluate("1 / 0"))


class NoteAI(unittest.TestCase):
    def test_question_is_answered_and_maths_is_handed_over_exactly(self):
        model = FakeJson(reply("answer", said="391"))
        out = A.respond("Bedroom", NOTE, "what is 23 x 17?", model=model)
        self.assertEqual(out["action"], "answer")
        self.assertEqual(out["text"], "")
        prompt = model.calls[0][0][-1]["content"]
        self.assertIn("Calculator:\n23 x 17 = 391", prompt)
        self.assertIn(NOTE, prompt)

    def test_add_keeps_only_new_lines(self):
        model = FakeJson(reply("add", "# Budget\n- warm\n- calm", "start"))
        out = A.respond("Bedroom", NOTE, "add a heading called Budget at the top", model=model)
        self.assertEqual((out["action"], out["text"], out["position"]), ("add", "# Budget", "start"))

    def test_rewrite_that_loses_lines_is_retried(self):
        lossy = reply("replace", "- [ ] warm\n- [ ] calm")
        whole = reply("replace", "Paint ideas for the bedroom\n\n- [ ] warm\n- [ ] calm")
        model = FakeJson(lossy, whole)
        out = A.respond("Bedroom", NOTE, "make this a checklist", model=model)
        self.assertEqual(len(model.calls), 2)
        self.assertIn("Paint ideas for the bedroom", model.calls[1][0][-1]["content"])
        self.assertTrue(out["text"].startswith("Paint ideas for the bedroom"))

    def test_asked_removals_are_not_second_guessed(self):
        model = FakeJson(reply("replace", "- warm\n- calm"))
        out = A.respond("Bedroom", NOTE, "remove the title line", model=model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(out["text"], "- warm\n- calm")

    def test_an_edit_with_no_text_becomes_an_answer(self):
        out = A.respond("Bedroom", NOTE, "write something",
                        model=FakeJson(reply("add", "", said="Not sure what to write.")))
        self.assertEqual(out["action"], "answer")
        self.assertEqual(out["reply"], "Not sure what to write.")

    def test_code_fences_are_removed(self):
        out = A.respond("x", "", "write a list",
                        model=FakeJson(reply("add", "```markdown\n- a\n- b\n```")))
        self.assertEqual(out["text"], "- a\n- b")

    def test_long_notes_cannot_be_rewritten_wholesale(self):
        model = FakeJson(reply("add", "- more"))
        A.respond("Long", "word " * 3000, "add more", model=model)
        schema = model.calls[0][1]
        self.assertEqual(schema["properties"]["action"]["enum"], ["answer", "add"])

    def test_cursor_line_is_shared(self):
        model = FakeJson(reply("add", "- red", "cursor"))
        A.respond("Bedroom", NOTE, "write colours here", cursor_line="- calm", model=model)
        self.assertIn('cursor is on this line: "- calm"', model.calls[0][0][-1]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
