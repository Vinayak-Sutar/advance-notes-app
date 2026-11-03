"""To-do items in Markdown: parsing, the date tokens, safe ticking, progress."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import checklist as C

BODY = """Plan for the exams.

## Maths 📅 2026-11-10
- [ ] Algebra
- [x] Calculus ✅ 2026-10-27
- [ ] Probability ⏰ 2026-11-05 18:00
## Physics
- [ ] Optics 🔁 every week
  - [ ] Lenses

```
- [ ] not an item, it's code
```"""


class Parse(unittest.TestCase):
    def test_sections_items_and_tokens(self):
        p = C.parse(BODY)
        self.assertEqual([s.title for s in p.sections], ["Maths", "Physics"])
        self.assertEqual(p.sections[0].due, "2026-11-10")
        texts = [it.text for it in p.items]
        self.assertEqual(texts, ["Algebra", "Calculus", "Probability", "Optics", "Lenses"])
        calc, prob, optics = p.items[1], p.items[2], p.items[3]
        self.assertTrue(calc.done)
        self.assertEqual(calc.done_on, "2026-10-27")
        self.assertEqual(prob.remind, "2026-11-05T18:00:00")
        self.assertEqual(optics.repeat, "every week")
        self.assertEqual(p.progress(), (1, 5))
        self.assertEqual(p.sections[0].progress(p.items), (1, 3))
        self.assertEqual(p.sections[1].progress(p.items), (0, 2))

    def test_reminder_without_a_time_is_nine(self):
        _, tok = C.split_tokens("Revise ⏰ 2026-11-05")
        self.assertEqual(tok["remind"], "2026-11-05T09:00:00")

    def test_emoji_variation_selector_is_accepted(self):
        _, tok = C.split_tokens("Revise ⏰️ 2026-11-05 07:30")
        self.assertEqual(tok["remind"], "2026-11-05T07:30:00")


class Edits(unittest.TestCase):
    def test_toggle_ticks_and_records_the_day(self):
        out = C.toggle(BODY, 3, "Algebra", today=date(2026, 10, 28))
        self.assertIn("- [x] Algebra ✅ 2026-10-28", out)
        back = C.toggle(out, 3, "Algebra")
        self.assertIn("- [ ] Algebra\n", back)

    def test_toggle_keeps_the_other_tokens(self):
        out = C.toggle(BODY, 5, "Probability", today=date(2026, 10, 28))
        self.assertIn("- [x] Probability ⏰ 2026-11-05 18:00 ✅ 2026-10-28", out)

    def test_a_stale_line_number_finds_the_item_by_its_text(self):
        shifted = "New first line\n" + BODY
        out = C.toggle(shifted, 3, "Algebra", today=date(2026, 10, 28)).split("\n")
        self.assertEqual(out[3], "## Maths 📅 2026-11-10")     # line 3 is now the heading
        self.assertEqual(out[4], "- [x] Algebra ✅ 2026-10-28")

    def test_unknown_item_is_refused(self):
        with self.assertRaises(LookupError):
            C.toggle(BODY, 3, "Chemistry")

    def test_set_when_and_clear(self):
        out = C.set_when(BODY, 3, "Algebra", remind="2026-11-01T18:00:00", repeat="daily")
        self.assertIn("- [ ] Algebra ⏰ 2026-11-01 18:00 🔁 daily", out)
        back = C.set_when(out, 3, "Algebra")
        self.assertIn("- [ ] Algebra\n", back)

    def test_reset_unticks_everything(self):
        out = C.reset(BODY)
        self.assertEqual(C.progress(out), (0, 5))
        self.assertNotIn("✅", out)

    def test_add_item_to_a_section_and_to_the_end(self):
        out = C.add_item(BODY, "Statistics", section="maths")
        lines = out.split("\n")
        self.assertEqual(lines[lines.index("- [ ] Probability ⏰ 2026-11-05 18:00") + 1],
                         "- [ ] Statistics")
        out = C.add_item("", "milk")
        self.assertEqual(out, "- [ ] milk")
        out = C.add_item("- [ ] milk\n\nsome words", "eggs")
        self.assertEqual(out.split("\n")[:2], ["- [ ] milk", "- [ ] eggs"])


class Meaning(unittest.TestCase):
    def test_ticks_dates_and_order_keep_the_meaning(self):
        a = C.semantic_sig("Exam", "- [ ] a\n- [ ] b")
        self.assertEqual(a, C.semantic_sig("Exam", "- [x] b ✅ 2026-10-28\n- [ ] a ⏰ 2026-11-01 09:00"))
        self.assertNotEqual(a, C.semantic_sig("Exam", "- [ ] a\n- [ ] c"))
        self.assertNotEqual(a, C.semantic_sig("Exams", "- [ ] a\n- [ ] b"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
