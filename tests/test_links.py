"""Links as tiles: worked out from the notes, previews fetched once. Made-up data only."""
from __future__ import annotations

import json
import shutil
import sys
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import links as L
from mnemo import sections as Sec
from mnemo import server as SRV
from mnemo.engine import Engine
from tests.test_server import _free_port


class Addresses(unittest.TestCase):
    def test_links_are_found_cleaned_and_kept_once(self):
        text = ("watch https://youtu.be/abc123?si=track. and [the docs](https://example.com/guide?utm_source=x)"
                " again https://youtu.be/abc123?si=other, plus www.not-a-link.com and ftp://nope")
        self.assertEqual(L.find(text), ["https://youtu.be/abc123", "https://example.com/guide"])

    def test_the_same_video_shared_two_ways_is_one_link(self):
        a = L.link_info("https://youtu.be/abc123")
        b = L.link_info("https://www.youtube.com/watch?v=abc123&t=30")
        self.assertEqual(a["key"], b["key"])
        self.assertEqual((a["site"], a["kind"]), ("YouTube", "video"))

    def test_account_links_are_marked_private(self):
        self.assertTrue(L.link_info("https://docs.google.com/document/d/1/edit")["private"])
        self.assertFalse(L.link_info("https://example.com/page")["private"])
        self.assertEqual(L.preview("https://drive.google.com/file/d/x")["status"], "private")


class _Vault(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="mnemo-links-")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.engine = Engine(self.folder, online=False, enrich=False)
        self.addCleanup(self.engine.close)
        self.links = L.Links(self.folder)

    def note(self, title, body):
        return self.engine.create_note(title=title, body=body)["id"]


class Tiles(_Vault):
    def test_a_link_in_two_notes_is_one_tile_listing_both(self):
        saved = self.engine.capture("https://youtu.be/abc123 a talk on chunking")
        mention = self.note("Study plan", "Rewatch https://www.youtube.com/watch?v=abc123 before Friday")
        tiles = self.links.tiles(self.engine.conn)
        self.assertEqual(len(tiles), 1)
        tile = tiles[0]
        self.assertEqual((tile["key"], tile["site"]), ("yt:abc123", "YouTube"))
        self.assertEqual({(n["id"], n["how"]) for n in tile["notes"]},
                         {(saved.note.id, "saved"), (mention, "in")})
        self.assertEqual(tile["title"], saved.note.title)        # until a preview comes

    def test_links_in_a_link_notes_fetched_page_text_dont_count(self):
        res = self.engine.capture("https://example.com/article my words")
        self.engine.update_note(res.note.id, body=Sec.join_link(
            "my words", "[example.com] An article\n\nSee https://elsewhere.example.org/x"))
        self.assertEqual([t["url"] for t in self.links.tiles(self.engine.conn)],
                         ["https://example.com/article"])

    def test_private_links_show_but_are_never_fetched(self):
        self.note("Shared", "Our sheet https://docs.google.com/spreadsheets/d/1/edit")
        tiles = self.links.tiles(self.engine.conn)
        self.assertTrue(tiles[0]["private"])
        self.assertEqual(self.links.missing(tiles), [])

    def test_archived_notes_and_notes_without_links_are_left_out(self):
        self.note("Plain", "nothing to see")
        gone = self.note("Old", "https://example.com/old")
        self.engine.complete(gone, True)
        self.engine.conn.execute("UPDATE notes SET status = 'archived' WHERE id = ?", (gone,))
        self.assertEqual(self.links.tiles(self.engine.conn), [])

    def test_your_order_holds_and_new_links_come_first(self):
        for i in range(3):
            self.note(f"N{i}", f"https://example.com/{i}")
        keys = [t["key"] for t in self.links.tiles(self.engine.conn)]
        self.links.set_layout(list(reversed(keys)) + ["made-up"], set(keys))
        self.assertEqual([t["key"] for t in self.links.tiles(self.engine.conn)], list(reversed(keys)))
        self.note("New", "https://example.com/new")
        self.assertEqual(self.links.tiles(self.engine.conn)[0]["key"], "example.com/new")
        self.assertNotIn("made-up", self.links.layout())

    def test_one_notes_links(self):
        a = self.note("A", "https://example.com/a and https://example.com/b")
        self.note("B", "https://example.com/c")
        self.assertEqual([t["url"] for t in self.links.tiles(self.engine.conn, a)],
                         ["https://example.com/a", "https://example.com/b"])


class Previews(_Vault):
    def test_fetched_once_and_kept(self):
        self.note("A", "https://example.com/a")
        calls = []

        def getter(url):
            calls.append(url)
            return {"status": "ok", "title": "A page", "caption": "About it"}
        tile = self.links.missing(self.links.tiles(self.engine.conn))[0]
        self.links.fetch(tile, getter)
        tiles = self.links.tiles(self.engine.conn)
        self.assertEqual((tiles[0]["title"], tiles[0]["caption"], tiles[0]["status"]),
                         ("A page", "About it", "ok"))
        self.assertEqual(self.links.missing(tiles), [])
        self.assertEqual(calls, ["https://example.com/a"])

    def test_refused_waits_a_day_but_offline_tries_again(self):
        self.note("A", "https://example.com/a")
        self.note("B", "https://example.com/b")
        tiles = self.links.tiles(self.engine.conn)
        by = {t["url"]: t for t in tiles}
        self.links.fetch(by["https://example.com/a"], lambda url: {"status": "refused"})
        self.links.fetch(by["https://example.com/b"], lambda url: {"status": "offline"})
        later = self.links.tiles(self.engine.conn)
        self.assertEqual([t["url"] for t in self.links.missing(later)], ["https://example.com/b"])
        tomorrow = datetime.now() + timedelta(days=1)
        self.assertEqual(len(self.links.missing(later, tomorrow)), 2)

    def test_pictures_are_only_served_from_their_folder(self):
        self.links.thumbs.mkdir(parents=True)
        (self.links.thumbs / "abc.jpg").write_bytes(b"jpeg")
        self.assertEqual(self.links.thumb("abc.jpg"), (b"jpeg", "image/jpeg"))
        for name in ("../previews.json", "..\\x.jpg", "missing.jpg", "abc.exe"):
            self.assertIsNone(self.links.thumb(name), name)


class OverHttp(_Vault):
    def setUp(self):
        super().setUp()
        SRV._engine = self.engine
        SRV._link_preview = lambda url: {"status": "ok", "title": "Fetched " + url[-1]}
        self.port = _free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), SRV.Handler)
        self.httpd.verbose = False
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        SRV._link_preview = None

    def call(self, path, payload=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if payload is None else json.dumps(payload).encode())
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_tiles_previews_and_order(self):
        for name in "abcd":
            self.note(name, f"see https://example.com/{name}")
        tiles = self.call("/api/links")["links"]
        self.assertEqual(len(tiles), 4)
        got = self.call("/api/links/fetch", {"max": 3})
        self.assertEqual((len(got["done"]), got["left"]), (3, 1))
        self.call("/api/links/fetch", {})
        tiles = self.call("/api/links")["links"]
        self.assertTrue(all(t["title"].startswith("Fetched") for t in tiles))
        keys = [t["key"] for t in tiles]
        self.call("/api/links/layout", {"order": keys[::-1]})
        self.assertEqual([t["key"] for t in self.call("/api/links")["links"]], keys[::-1])

    def test_a_note_lists_the_links_written_in_it(self):
        made = self.note("Plan", "read https://example.com/x")
        self.assertEqual([t["url"] for t in self.call(f"/api/note?id={made}")["links"]],
                         ["https://example.com/x"])
        saved = self.engine.capture("https://example.com/y")
        self.assertEqual(self.call(f"/api/note?id={saved.note.id}")["links"], [])

    def test_missing_picture_is_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.call("/link-thumb/..%2Fpreviews.json")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
