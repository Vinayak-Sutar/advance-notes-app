"""Reading intent out of dictated text.

Voice is the primary input, so `@topic` / `!task` symbols are unusable and
plain speech has to carry the meaning. These tests pin the phrasings that
matter and, just as importantly, the text that must NOT be read as a command.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo.intent import describe_when, parse

NOW = datetime(2026, 9, 3, 14, 30)      # a Thursday afternoon


class TestSpokenDestination(unittest.TestCase):
    def test_topic_phrases(self):
        for raw, topic in [
            ("put this in my gaming notes the build is broken", "gaming"),
            ("file this under technology rag chunking notes", "technology"),
            ("save this in music barre chords are hard", "music"),
        ]:
            self.assertEqual(parse(raw, NOW).topic, topic, raw)

    def test_append_to_an_existing_note(self):
        i = parse("add to the elden ring note that mimic tear helps", NOW)
        self.assertEqual(i.append_to, "elden ring")
        self.assertIn("mimic tear helps", i.text)
        self.assertIsNone(i.topic)

    def test_symbols_still_work_when_typed(self):
        i = parse('@"Technology/AI" !task try reranking', NOW)
        self.assertEqual(i.topic, "Technology/AI")
        self.assertEqual(i.kind, "task")

    def test_plain_note_gets_no_directives(self):
        i = parse("malenia waterfowl timing is brutal", NOW)
        self.assertIsNone(i.topic)
        self.assertIsNone(i.append_to)
        self.assertIsNone(i.due_at)
        self.assertEqual(i.kind, "note")


class TestReminders(unittest.TestCase):
    def test_reminder_phrases_resolve_to_absolute_times(self):
        cases = {
            "remind me friday at 5 to renew the domain": (4, 17),   # Fri, 5pm
            "remind me tomorrow at 9 to call the bank": (4, 9),
            "remind me in 3 hours to check the deploy": (3, 17),
            "remind me tonight to water the plants": (3, 20),
        }
        for raw, (day, hour) in cases.items():
            i = parse(raw, NOW)
            self.assertIsNotNone(i.due_at, raw)
            self.assertEqual(i.due_at.day, day, raw)
            self.assertEqual(i.due_at.hour, hour, raw)
            self.assertTrue(i.remind, raw)
            self.assertEqual(i.kind, "task", raw)

    def test_task_phrases_without_the_word_remind(self):
        i = parse("i need to buy milk tomorrow", NOW)
        self.assertEqual(i.kind, "task")
        self.assertEqual(i.due_at.day, 4)
        self.assertIn("buy milk", i.text)

    def test_directive_words_are_stripped_from_the_note(self):
        i = parse("remind me friday at 5 to renew the domain", NOW)
        self.assertNotIn("remind", i.text.lower())
        self.assertNotIn("friday", i.text.lower())
        self.assertEqual(i.text, "renew the domain")

    def test_recurrence_detected(self):
        i = parse("remind me every monday to water the plants", NOW)
        self.assertEqual(i.recurrence, "weekly")
        self.assertNotIn("every", i.text.lower())


class TestTimesInsideNumbersAreNotTimes(unittest.TestCase):
    """OCR transcripts are full of IPs, ports and versions.

    An earlier regex matched `4.17` inside the IP address 10.0.4.17 and gave a
    screenshot of a failed deploy a 4:17 AM reminder. Numbers embedded in
    longer numbers must never parse as clock times.
    """

    def test_ip_addresses_and_versions_are_not_times(self):
        for raw in [
            "could not connect to server at 10.0.4.17",
            "psycopg2 error on port 5432",
            "upgraded to version 2.30 last week",
            "the file is 3.45 MB",
            "build 4821 duration 3m 42s",
            "error code 12.30 returned",
        ]:
            self.assertIsNone(parse(raw, NOW).due_at, raw)

    def test_real_clock_times_still_parse(self):
        for raw, hour, minute in [
            ("remind me at 5 to call", 17, 0),
            ("remind me friday at 5pm", 17, 0),
            ("meeting at 17:30 tomorrow", 17, 30),
            ("remind me at 9.30am", 9, 30),
        ]:
            i = parse(raw, NOW)
            self.assertIsNotNone(i.due_at, raw)
            self.assertEqual((i.due_at.hour, i.due_at.minute), (hour, minute), raw)

    def test_impossible_clock_values_rejected(self):
        for raw in ["at 99:99 something", "value 45:99 logged"]:
            self.assertIsNone(parse(raw, NOW).due_at, raw)


class TestProseIsNotASchedule(unittest.TestCase):
    """Ordinary description must not silently create reminders.

    "the deploy that broke last night" acquired an 8 PM reminder because the
    word "night" mapped to a time of day. A vague daypart only counts when
    something else already signalled temporal intent.
    """

    def test_past_tense_never_schedules(self):
        for raw in ["the deploy that broke last night",
                    "we shipped this 3 days ago",
                    "yesterday's outage postmortem",
                    "the meeting last week went long"]:
            self.assertIsNone(parse(raw, NOW).due_at, raw)

    def test_bare_dayparts_in_prose_do_not_schedule(self):
        for raw in ["good morning thoughts about the api design",
                    "the evening session on retrieval was useful",
                    "afternoon slump productivity ideas"]:
            self.assertIsNone(parse(raw, NOW).due_at, raw)

    def test_possessive_dates_are_titles_not_requests(self):
        for raw in ["tonight's build notes", "today's standup summary",
                    "tomorrow's agenda draft"]:
            self.assertIsNone(parse(raw, NOW).due_at, raw)

    def test_dayparts_still_work_with_a_reminder_phrase(self):
        for raw, hour in [("remind me tonight to water the plants", 20),
                          ("remind me tomorrow morning to call", 9),
                          ("remind me this evening to stretch", 18)]:
            i = parse(raw, NOW)
            self.assertIsNotNone(i.due_at, raw)
            self.assertEqual(i.due_at.hour, hour, raw)

    def test_a_plain_note_stays_a_note(self):
        i = parse("css grid areas beat nested flexbox for layout", NOW)
        self.assertEqual(i.kind, "note")
        self.assertIsNone(i.due_at)


class TestDescribeWhen(unittest.TestCase):
    def test_relative_labels(self):
        self.assertTrue(describe_when(datetime(2026, 9, 3, 17, 0), NOW)
                        .startswith("Today"))
        self.assertTrue(describe_when(datetime(2026, 9, 4, 9, 0), NOW)
                        .startswith("Tomorrow"))
        self.assertIn("Monday", describe_when(datetime(2026, 9, 7, 9, 0), NOW))

    def test_twelve_hour_clock(self):
        self.assertIn("5:00 PM", describe_when(datetime(2026, 9, 3, 17, 0), NOW))
        self.assertIn("12:00 AM", describe_when(datetime(2026, 9, 3, 0, 0), NOW))
        self.assertIn("12:00 PM", describe_when(datetime(2026, 9, 3, 12, 0), NOW))

    def test_no_platform_specific_strftime(self):
        """%-I and %-d are glibc-only and raise on Windows."""
        for month in range(1, 13):
            describe_when(datetime(2027, month, 5, 14, 5), NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
