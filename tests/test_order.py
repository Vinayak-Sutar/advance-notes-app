"""Your own order of notes: dragging tiles, and what keeps it stable."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import notes as N
from mnemo.engine import Engine


class Order(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)
        self.ids = []
        for k, title in enumerate(("one", "two", "three", "four")):
            note_id = self.engine.create_note(title=title, body=title)["id"]
            self.ids.append(note_id)
            # Creation times have one-second resolution; space them out in
            # the file itself (the truth a rebuild reads) instead of sleeping.
            row = N.get(self.engine.conn, note_id)
            note = self.engine.vault.read(row["path"])
            note.created = f"2026-10-01T10:00:0{k}Z"
            self.engine.vault.write(note, row["path"])
            N.upsert(self.engine.conn, note, row["path"])

    def tearDown(self):
        self.engine.close()

    def order(self):
        rows = self.engine.conn.execute(
            f"SELECT n.title FROM notes n ORDER BY {N.ORDER}").fetchall()
        return [r["title"] for r in rows]

    def test_newest_first_until_you_drag(self):
        self.assertEqual(self.order(), ["four", "three", "two", "one"])

    def test_drop_between_two(self):
        one, two, three, four = self.ids
        self.engine.set_order(one, before=four, after=three)
        self.assertEqual(self.order(), ["four", "one", "three", "two"])

    def test_drop_on_top_and_at_the_bottom(self):
        one, two, three, four = self.ids
        self.engine.set_order(one, after=four)
        self.assertEqual(self.order()[0], "one")
        self.engine.set_order(four, before=two)
        self.assertEqual(self.order()[-1], "four")

    def test_a_new_note_still_lands_on_top(self):
        one, two, three, four = self.ids
        self.engine.set_order(one, after=four)
        fresh = self.engine.create_note(title="fresh", body="new")["id"]
        self.assertEqual(self.order()[0], "fresh")
        self.assertTrue(fresh)

    def test_editing_does_not_move_it(self):
        one, two, three, four = self.ids
        self.engine.set_order(one, before=four, after=three)
        before = self.order()
        self.engine.update_note(three, body="three, edited")
        self.engine.complete(two, True)
        self.assertEqual(self.order(), before)

    def test_order_is_in_the_file_and_survives_a_rebuild(self):
        one, two, three, four = self.ids
        self.engine.set_order(one, before=four, after=three)
        row = N.get(self.engine.conn, one)
        self.assertIn("order:", self.engine.vault.abs_path(row["path"]).read_text(encoding="utf-8"))
        before = self.order()
        self.engine.rebuild()
        self.assertEqual(self.order(), before)

    def test_undo_puts_it_back(self):
        one, two, three, four = self.ids
        self.engine.set_order(one, after=four)
        self.engine.undo()
        self.assertEqual(self.order(), ["four", "three", "two", "one"])

    def test_squeezed_neighbours_get_spaced_out(self):
        one, two, three, four = self.ids
        # Drag back and forth into the same gap until it is too narrow.
        for _ in range(40):
            self.engine.set_order(one, before=four, after=three,
                                  view=[four, one, three, two])
        self.assertEqual(self.order(), ["four", "one", "three", "two"])
        gaps = [N.position(self.engine.conn, a) - N.position(self.engine.conn, b)
                for a, b in ((four, one), (one, three))]
        self.assertTrue(all(g > 1e-3 for g in gaps), gaps)


if __name__ == "__main__":
    unittest.main(verbosity=2)
