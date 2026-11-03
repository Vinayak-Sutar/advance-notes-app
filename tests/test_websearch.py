"""Web search: when it's needed, how answers use it, and that it stays optional."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import noteai as A
from mnemo import websearch as W
from mnemo.chat import Chat, _SearchRequest
from mnemo.engine import Engine

NOW = datetime(2026, 10, 25)
LOUD = W.WebResult(title="2026 Valorant Champions",
                   url="https://en.wikipedia.org/wiki/2026_Valorant_Champions",
                   text="In the final, LOUD defeated Team Vitality 3-1.")


class FakeSearch:
    def __init__(self, results=(), fail=None):
        self.results, self.fail, self.queries = list(results), fail, []

    def __call__(self, query, cfg):
        self.queries.append(query)
        if self.fail:
            raise self.fail
        return self.results


class FakeJson:
    def __init__(self, *replies):
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.calls = []

    def complete(self, messages, schema):
        self.calls.append((messages, schema))
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


class FakeStream:
    """Streams one canned answer per call."""
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def stream(self, messages):
        self.calls.append(messages)
        yield from self.answers[min(len(self.calls), len(self.answers)) - 1]

    def close(self):
        pass


def events(engine, message, model, search, web=None, query="Valorant Champions 2026"):
    chat = Chat(engine, model=model, json_model=FakeJson({"query": query}), searcher=search)
    return list(chat.events(message, [], web=web or W.WebConfig()))


class NeedsWeb(unittest.TestCase):
    def test_recent_years_and_live_facts(self):
        for q in ("who won valorant champs 26", "Valorant Champions 2026 winner",
                  "what's the latest iPhone", "price of bitcoin", "who is the ceo of openai"):
            self.assertTrue(W.needs_web(q, NOW), q)

    def test_ordinary_questions_do_not(self):
        for q in ("what is 23 x 17", "summarise this note", "capital of australia",
                  "what did I save about dune"):
            self.assertFalse(W.needs_web(q, NOW), q)

    def test_query_writer_falls_back_to_the_question(self):
        self.assertEqual(W.write_query(FakeJson("not json"), "who won"), "who won")
        self.assertEqual(W.write_query(FakeJson({"query": "Valorant Champions 2026"}),
                                       "champs 26"), "Valorant Champions 2026")


class ChatWithWeb(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(tempfile.mkdtemp(), enrich=False)

    def tearDown(self):
        self.engine.close()

    def test_recent_question_is_looked_up_before_answering(self):
        search = FakeSearch([LOUD])
        model = FakeStream(["LOUD won it.\nSources: W1"])
        out = events(self.engine, "who won valorant champs 26", model, search)
        self.assertEqual(search.queries, ["Valorant Champions 2026"])
        self.assertEqual(out[0], {"type": "searching", "query": "Valorant Champions 2026"})
        self.assertIn("[W1] 2026 Valorant Champions", model.calls[0][-1]["content"])
        web = [e for e in out if e["type"] == "web"][0]
        self.assertEqual(web["results"][0]["site"], "en.wikipedia.org")
        self.assertEqual(out[-1], {"type": "done", "fromNotes": False, "fromWeb": True})

    def test_model_can_ask_for_the_web_and_that_line_is_never_shown(self):
        search = FakeSearch([LOUD])
        model = FakeStream(["SEAR", "CH: who leads the VCT\n"], ["Here you go.\nSources: W1"])
        out = events(self.engine, "tell me about the vct", model, search)
        tokens = "".join(e["text"] for e in out if e["type"] == "token")
        self.assertNotIn("SEARCH", tokens)
        self.assertEqual(tokens, "Here you go.\n")
        self.assertEqual(search.queries, ["who leads the VCT"])

    def test_web_off_means_no_search_and_an_honest_prompt(self):
        search = FakeSearch([LOUD])
        model = FakeStream(["I don't know.\nSources: none"])
        events(self.engine, "who won valorant champs 26", model, search,
               web=W.WebConfig(mode="off"))
        self.assertEqual(search.queries, [])
        prompt = model.calls[0]
        self.assertIn("Web search is turned off", prompt[0]["content"])
        self.assertIn("Don't answer from memory", prompt[-1]["content"])

    def test_failed_search_is_admitted_not_guessed(self):
        search = FakeSearch(fail=W.SearchError("couldn't reach en.wikipedia.org"))
        model = FakeStream(["I couldn't find that.\nSources: none"])
        out = events(self.engine, "who won valorant champs 26", model, search)
        self.assertIn("found nothing useful (couldn't reach", model.calls[0][-1]["content"])
        self.assertFalse(out[-1]["fromWeb"])


class SearchRequestFilter(unittest.TestCase):
    def test_ordinary_answer_passes_through(self):
        f = _SearchRequest()
        out = f.feed("Se") + f.feed("veral things") + f.finish()
        self.assertEqual(out, "Several things")
        self.assertIsNone(f.query)

    def test_search_line_is_captured(self):
        f = _SearchRequest()
        self.assertEqual(f.feed("**SEARCH:** valorant champions 2026") + f.finish(), "")
        self.assertEqual(f.query, "valorant champions 2026")


class NoteAIWithWeb(unittest.TestCase):
    def test_recent_request_is_looked_up_first(self):
        search = FakeSearch([LOUD])
        model = FakeJson({"query": "Valorant Champions 2026"},
                         {"action": "add", "reply": "Added.", "text": "- LOUD won", "position": "cursor"})
        out = A.respond("Esports", "notes", "add the 2026 champions winner here",
                        model=model, web=W.WebConfig(), searcher=search)
        self.assertEqual(out["text"], "- LOUD won")
        self.assertEqual(out["web"]["query"], "Valorant Champions 2026")
        self.assertIn("[W1]", model.calls[1][0][-1]["content"])
        self.assertNotIn("search", model.calls[1][1]["properties"]["action"]["enum"])

    def test_model_chosen_search(self):
        search = FakeSearch([LOUD])
        model = FakeJson({"action": "search", "reply": "", "text": "VCT leader", "position": "end"},
                         {"action": "answer", "reply": "LOUD.", "text": "", "position": "end"})
        out = A.respond("x", "", "tell me about the vct", model=model,
                        web=W.WebConfig(), searcher=search)
        self.assertEqual(search.queries, ["VCT leader"])
        self.assertEqual(out["reply"], "LOUD.")

    def test_web_off_has_no_search_action(self):
        model = FakeJson({"action": "answer", "reply": "I don't know.", "text": "", "position": "end"})
        out = A.respond("x", "", "who won valorant champs 26", model=model,
                        web=W.WebConfig(mode="off"), searcher=FakeSearch())
        self.assertNotIn("search", model.calls[0][1]["properties"]["action"]["enum"])
        self.assertNotIn("web", out)


class Providers(unittest.TestCase):
    def test_brave_and_searxng_need_their_settings(self):
        with self.assertRaises(W.SearchError):
            W.search("x", W.WebConfig(provider="brave"))
        with self.assertRaises(W.SearchError):
            W.search("x", W.WebConfig(provider="searxng"))

    def test_config_from_the_page(self):
        cfg = W.WebConfig.from_payload({"mode": "off", "provider": "brave", "key": " k "})
        self.assertEqual((cfg.enabled, cfg.provider, cfg.key), (False, "brave", "k"))
        self.assertTrue(W.WebConfig.from_payload(None).enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
