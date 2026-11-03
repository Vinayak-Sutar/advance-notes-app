"""Web API tests, including the guards that protect a localhost server.

Runs against a real server on an ephemeral port, so it exercises the actual
HTTP path rather than calling handlers directly.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import server as SRV
from mnemo.engine import Engine


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LiveServer(unittest.TestCase):
    """A real server on an ephemeral port over a small seeded vault."""

    @classmethod
    def setUpClass(cls):
        cls.vault = tempfile.mkdtemp(prefix="mnemo-srv-")
        SRV._engine = Engine(cls.vault, enrich=False)
        for text in ['@"Technology/AI" rag chunking overlap beats naive splitting',
                     '@"Technology/AI" vector store hybrid retrieval notes',
                     '@"Gaming/Elden Ring" malenia waterfowl timing',
                     'a drifting thought with no obvious home zzz']:
            SRV._engine.capture(text)

        cls.port = _free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), SRV.Handler)
        cls.httpd.verbose = False
        cls.httpd.daemon_threads = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        SRV._engine.close()

    # -- helpers --
    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers=headers or {})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())


class ServerCase(_LiveServer):
    # -- security --
    def test_rejects_foreign_host_header(self):
        """A loopback bind alone does not stop DNS rebinding; the Host check does."""
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/state", {"Host": "evil.example.com"})
        self.assertEqual(ctx.exception.code, 403)

    def test_rejects_foreign_origin(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/state", {"Origin": "https://evil.example.com"})
        self.assertEqual(ctx.exception.code, 403)

    def test_allows_localhost_origin(self):
        state = self.get("/api/state", {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertIn("notes", state)

    def test_static_path_traversal_blocked(self):
        for attempt in ("/static/../server.py", "/static/..%2fserver.py"):
            try:
                self.get(attempt)
                self.fail(f"served {attempt}")
            except urllib.error.HTTPError as exc:
                self.assertIn(exc.code, (400, 403, 404))

    # -- api --
    def test_state_shape(self):
        state = self.get("/api/state")
        for key in ("tree", "notes", "topics", "inbox", "embedder", "router", "vault"):
            self.assertIn(key, state)
        self.assertGreaterEqual(state["notes"], 4)

    def test_tree_counts_roll_up_to_parents(self):
        state = self.get("/api/state")
        tech = next(t for t in state["tree"] if t["name"] == "Technology")
        self.assertEqual(tech["own"], 0)
        self.assertGreaterEqual(tech["count"], 2)   # via Technology/AI

    def test_notes_filtered_by_topic(self):
        r = self.get("/api/notes?topic=Technology/AI")
        self.assertEqual(len(r["notes"]), 2)
        for n in r["notes"]:
            self.assertEqual(n["topic"], "Technology/AI")

    def test_search_returns_hits(self):
        r = self.get("/api/search?q=chunking")
        self.assertTrue(r["hits"])

    def test_search_with_hostile_query_does_not_500(self):
        for q in ['"', "AND", "a*", "NEAR(", "%27", ")("]:
            self.get("/api/search?q=" + urllib.parse.quote(q))

    def test_capture_and_undo_round_trip(self):
        before = self.get("/api/state")["notes"]
        made = self.post("/api/capture", {"text": '@"Gaming" a note to undo'})
        self.assertEqual(made["topic"], "Gaming")
        self.assertEqual(self.get("/api/state")["notes"], before + 1)
        self.post("/api/undo", {})
        self.assertEqual(self.get("/api/state")["notes"], before)

    def test_capture_rejects_empty(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/capture", {"text": "   "})
        self.assertEqual(ctx.exception.code, 400)

    def test_suggest_ranks_the_right_topic_first(self):
        r = self.get("/api/notes?topic=Technology/AI")
        note_id = r["notes"][0]["id"]
        s = self.get("/api/suggest?id=" + note_id)
        self.assertTrue(s["suggestions"])
        self.assertEqual(s["suggestions"][0]["path"], "Technology/AI")

    def test_note_detail_includes_body_and_topics(self):
        note_id = self.get("/api/notes?topic=Technology/AI")["notes"][0]["id"]
        n = self.get("/api/note?id=" + note_id)
        self.assertTrue(n["body"])
        self.assertIn("Technology/AI", n["topics"])

    def test_missing_note_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/note?id=NOPE")
        self.assertEqual(ctx.exception.code, 404)

    def test_graph_is_consistent(self):
        g = self.get("/api/graph")
        ids = {n["id"] for n in g["nodes"]}
        for link in g["links"]:
            self.assertIn(link["source"], ids)
            self.assertIn(link["target"], ids)

    def test_garden_returns_proposals_list(self):
        self.assertIsInstance(self.get("/api/garden")["proposals"], list)

    # -- image upload --
    def _png(self) -> bytes:
        """Smallest valid PNG: a 1x1 transparent pixel."""
        import base64
        return base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

    def _upload(self, blob: bytes, text: str = ""):
        import base64
        return self.post("/api/upload",
                         {"data": base64.b64encode(blob).decode(), "text": text})

    def test_upload_rejects_non_image_bytes(self):
        """The sniffed magic decides the extension, so a lie must not get through."""
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._upload(b"<script>alert(1)</script>")
        self.assertEqual(ctx.exception.code, 415)

    def test_upload_rejects_bad_base64(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/upload", {"data": "!!!not base64!!!"})
        self.assertEqual(ctx.exception.code, 400)

    def test_upload_rejects_empty(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/upload", {"data": ""})
        self.assertEqual(ctx.exception.code, 400)

    def test_upload_rejects_oversized(self):
        from mnemo.server import MAX_UPLOAD
        blob = self._png() + b"\x00" * (MAX_UPLOAD + 1)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._upload(blob)
        self.assertEqual(ctx.exception.code, 413)

    def test_upload_stores_image_and_makes_a_note(self):
        # Engine has enrich=False here, so no model is called: this covers the
        # storage and note-creation path, not the vision path.
        r = self._upload(self._png(), "a pasted screenshot")
        self.assertTrue(r["media"])
        self.assertIn("a pasted screenshot", r["title"])
        note = self.get("/api/note?id=" + r["id"])
        self.assertEqual(note["kind"], "image")

    def test_identical_images_are_stored_once(self):
        a = self._upload(self._png(), "first")
        b = self._upload(self._png(), "second")
        self.assertEqual(a["media"], b["media"])   # content-addressed

    def test_media_is_served_and_traversal_blocked(self):
        r = self._upload(self._png(), "served image")
        name = r["media"].split("/")[-1]
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/media/{name}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.headers.get_content_type(), "image/png")
            self.assertTrue(resp.read())
        for attempt in ("/media/../index.db", "/media/..%2f..%2fserver.py"):
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{attempt}", timeout=10)
                self.fail(f"served {attempt}")
            except urllib.error.HTTPError as exc:
                self.assertIn(exc.code, (400, 403, 404))

    def test_sniffer_accepts_real_formats_only(self):
        from mnemo.server import _sniff
        self.assertEqual(_sniff(self._png()), "png")
        self.assertEqual(_sniff(bytes.fromhex("ffd8ffe0")), "jpeg")
        self.assertEqual(_sniff(b"RIFF0000WEBP"), "webp")
        self.assertEqual(_sniff(b"%PDF-1.7"), "")
        self.assertEqual(_sniff(b""), "")

    # -- writing notes by hand --
    def test_new_note_in_a_folder(self):
        r = self.post("/api/note/new", {"topic": "Gaming/Elden Ring"})
        self.assertEqual(r["topic"], "Gaming/Elden Ring")
        n = self.get("/api/note?id=" + r["id"])
        self.assertEqual(n["title"], "Untitled")

    def test_new_note_without_a_folder_is_unfiled(self):
        r = self.post("/api/note/new", {})
        self.assertIsNone(r["topic"])
        self.assertTrue(r["path"].startswith("Inbox/"))

    def test_file_endpoint_on_blank_note_declines(self):
        r = self.post("/api/note/new", {})
        out = self.post("/api/note/file", {"id": r["id"]})
        self.assertFalse(out["filed"])

    def test_delete_then_undo_round_trip(self):
        r = self.post("/api/note/new", {"topic": "Gaming"})
        self.post("/api/note/update", {"id": r["id"], "title": "Keep me",
                                       "body": "important"})
        self.post("/api/note/delete", {"id": r["id"]})
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/api/note?id=" + r["id"])
        self.post("/api/undo", {})
        n = self.get("/api/note?id=" + r["id"])
        self.assertEqual(n["body"], "important")

    def test_note_endpoints_reject_unknown_ids(self):
        for route in ("/api/note/file", "/api/note/delete"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post(route, {"id": "NOPE"})
            self.assertEqual(ctx.exception.code, 404, route)

    def test_index_html_is_served(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode()
        self.assertIn("<title>Notes</title>", body)


class FakeChatModel:
    def __init__(self, pieces):
        self.pieces = pieces

    def stream(self, messages):
        yield from self.pieces

    def close(self):
        pass


class NewDesignEndpoints(_LiveServer):
    """The endpoints the redesigned page relies on."""

    def stream(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            self.assertEqual(r.headers["Content-Type"], "application/x-ndjson")
            return [json.loads(line) for line in r.read().decode().splitlines() if line]

    def test_chat_streams_newline_delimited_events(self):
        SRV._chat_model = lambda: FakeChatModel(["Chunking ", "overlap helps.\n", "Sources: 1"])
        try:
            events = self.stream("/api/chat", {"message": "what did I note about chunking?",
                                               "history": []})
        finally:
            SRV._chat_model = None
        tokens = "".join(e["text"] for e in events if e["type"] == "token")
        self.assertEqual(tokens, "Chunking overlap helps.\n")
        self.assertEqual(events[-1], {"type": "done", "fromNotes": True, "fromWeb": False})
        self.assertEqual(len([e for e in events if e["type"] == "sources"][0]["notes"]), 1)
        # The server is still usable afterwards: chunked framing was correct.
        self.assertIn("notes", self.get("/api/state"))

    def test_chat_reminder_acts_without_a_model(self):
        events = self.stream("/api/chat", {"message": "remind me to stretch tomorrow at 7 am"})
        action = [e for e in events if e["type"] == "action"][0]["action"]
        self.assertEqual(action["kind"], "reminder")
        self.assertTrue(self.get(f"/api/note?id={action['noteId']}")["due"])

    def test_image_note_parts_and_editing_keeps_the_ocr(self):
        from mnemo import sections as Sec
        body = Sec.join_image("", "A whiteboard.", "NOW\nFix sync")
        res = SRV._engine.capture(body, title="Whiteboard", kind="image",
                                  media=".mnemo/media/abc.png", intent_text="")
        note = self.get(f"/api/note?id={res.note.id}")
        self.assertEqual((note["body"], note["description"], note["ocr"]),
                         ("", "A whiteboard.", "NOW\nFix sync"))
        self.post("/api/note/update", {"id": res.note.id, "title": "Whiteboard",
                                       "body": "planning on monday"})
        note = self.get(f"/api/note?id={res.note.id}")
        self.assertEqual(note["body"], "planning on monday")
        self.assertEqual(note["ocr"], "NOW\nFix sync")

    def test_blank_note_is_discarded(self):
        created = self.post("/api/note/new", {"title": "", "body": ""})
        self.assertTrue(self.post("/api/note/discard", {"id": created["id"]})["discarded"])
        with self.assertRaises(urllib.error.HTTPError):
            self.get(f"/api/note?id={created['id']}")

    def test_stale_tidy_suggestion_is_a_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/garden/apply", {"id": "NOPE"})
        self.assertEqual(ctx.exception.code, 404)
        self.assertTrue(self.post("/api/garden/dismiss", {"id": "NOPE"})["ok"])

    def test_note_ai_endpoint(self):
        class Fake:
            def complete(self, messages, schema):
                return json.dumps({"action": "add", "reply": "Added colours.",
                                   "text": "- red\n- blue", "position": "end"})
        SRV._note_ai_model = Fake
        try:
            note = self.post("/api/note/new", {"title": "Colours", "body": "my list"})
            out = self.post("/api/note/ai", {"id": note["id"], "request": "write colours here"})
        finally:
            SRV._note_ai_model = None
        self.assertEqual((out["action"], out["text"]), ("add", "- red\n- blue"))

    def test_search_hits_carry_the_kind(self):
        hits = self.get("/api/search?q=malenia")["hits"]
        self.assertTrue(hits and all("kind" in h for h in hits))

    def test_map_nodes_carry_paths(self):
        graph = self.get("/api/graph")
        topics = [n for n in graph["nodes"] if n["type"] == "topic"]
        self.assertIn("Technology/AI", {n["path"] for n in topics})


class SnippetCase(unittest.TestCase):
    def test_markdown_marks_are_stripped_from_previews(self):
        body = "Intro line.\n\n## Just type\n- **Today** shows `due` items\n1. first"
        self.assertEqual(SRV._snippet(body),
                         "Intro line. Just type Today shows due items first")

    def test_inline_symbols_inside_text_survive(self):
        self.assertEqual(SRV._snippet("C# and 3-4 people, a - b"),
                         "C# and 3-4 people, a - b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
