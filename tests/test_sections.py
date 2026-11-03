"""Splitting image and link notes into what the user wrote and what the app added."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import sections as Sec


class ImageSections(unittest.TestCase):
    def test_round_trip(self):
        body = Sec.join_image("my caption", "A whiteboard with sticky notes.",
                              "NOW\nFix sync")
        parts = Sec.split_image(body)
        self.assertEqual(parts.user, "my caption")
        self.assertEqual(parts.description, "A whiteboard with sticky notes.")
        self.assertEqual(parts.ocr, "NOW\nFix sync")

    def test_multi_paragraph_description_stays_whole(self):
        desc = "A settings screen.\n\nIt shows a dark theme toggle."
        parts = Sec.split_image(Sec.join_image("", desc, ""))
        self.assertEqual(parts.description, desc)
        self.assertEqual(parts.user, "")

    def test_legacy_body_without_description_heading(self):
        legacy = "A terminal window.\n\n**Text in image**\n\n$ ping 10.0.4.17"
        parts = Sec.split_image(legacy)
        self.assertEqual(parts.user, "")
        self.assertEqual(parts.description, "A terminal window.")
        self.assertEqual(parts.ocr, "$ ping 10.0.4.17")

    def test_placeholder_is_not_a_description(self):
        parts = Sec.split_image("(image: shot.png)")
        self.assertEqual(parts.description, "")

    def test_editing_the_caption_keeps_the_rest(self):
        stored = Sec.join_image("old", "A cat.", "MEOW")
        parts = Sec.split_image(stored)
        again = Sec.split_image(Sec.join_image("new words", parts.description, parts.ocr))
        self.assertEqual((again.user, again.description, again.ocr),
                         ("new words", "A cat.", "MEOW"))


class LinkSections(unittest.TestCase):
    BODY = ("watch this before the trip\n\n"
            "[YouTube] Packing light in 10 minutes\n\n"
            "A quick guide to fitting a week into a backpack.\n\n"
            "Lots of page text follows here.")

    def test_split(self):
        parts = Sec.split_link(self.BODY)
        self.assertEqual(parts.user, "watch this before the trip")
        self.assertTrue(parts.fetched.startswith("[YouTube] Packing light"))
        self.assertEqual(parts.summary, "A quick guide to fitting a week into a backpack.")

    def test_join_restores_body(self):
        parts = Sec.split_link(self.BODY)
        self.assertEqual(Sec.join_link(parts.user, parts.fetched), self.BODY)

    def test_task_lines_are_not_mistaken_for_the_page_header(self):
        body = "[x] done\n\n[ ] milk"
        parts = Sec.split_link(body)
        self.assertEqual(parts.user, body)
        self.assertEqual(parts.fetched, "")

    def test_long_summary_is_shortened(self):
        body = "[Site] Title\n\n" + "word " * 200
        summary = Sec.split_link(body).summary
        self.assertLessEqual(len(summary), Sec.SUMMARY_CHARS + 1)
        self.assertTrue(summary.endswith("…"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
