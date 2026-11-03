"""Personal notes: the page's reader, and the helper that prepares its data.

Made-up messages only. Real exports hold private details and must never be
copied into a test.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mnemo.personal_view import Personal
from mnemo.vault import Vault

spec = importlib.util.spec_from_file_location("personal_parse", ROOT / "tools" / "personal_parse.py")
parse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parse)


class PrivateDetector(unittest.TestCase):
    def test_flags_each_kind(self):
        for text in ("Pass Hunter42", "Bank a/c no : 000111222333", "IFSC ABCD0123456",
                     "WIFI:T:WPA;P:notreal;S:Cafe;H:false;", "Sample@123", "9876543210",
                     "+91 98765 43210", "someone@example.com", "room no. 4, ground floor",
                     "4111 1111 1111 1111", "Join: https://chat.whatsapp.com/AbCdEf",
                     "https://meet.google.com/abc-defg-hij", "Abc12345xyz", "Rex\n18 june 2001"):
            self.assertIsNotNone(parse.private_label(text), text)

    def test_leaves_ordinary_saves_alone(self):
        for text in ("Atomic 15.29", "Rdr 2 2k", "Bh3 165", "mtp_mid_report.pdf",
                     "See this Instagram post by @some_name_here: https://www.instagram.com/reel/Abc/",
                     "Underdog.io", "https://youtu.be/abc?si=xyz", "<Media omitted>\nreport_2025_final.pdf",
                     "DOC-20260707-WA0000. (file attached)"):
            self.assertIsNone(parse.private_label(text), text)


class Links(unittest.TestCase):
    def test_the_same_post_matches_however_it_was_shared(self):
        a = parse.link_info(parse.clean_url("https://www.instagram.com/reel/Abc_1/?igsh=xxx"))
        b = parse.link_info(parse.clean_url("https://instagram.com/reel/Abc_1/?utm_source=ig"))
        self.assertEqual(a["key"], b["key"])
        self.assertEqual(a["kind"], "reel")
        self.assertEqual(a["url"], "https://www.instagram.com/reel/Abc_1/")
        c = parse.link_info(parse.clean_url("https://youtu.be/XyZ?si=1"))
        d = parse.link_info(parse.clean_url("https://www.youtube.com/watch?v=XyZ"))
        self.assertEqual(c["key"], d["key"])

    def test_private_type_links_are_marked(self):
        self.assertTrue(parse.link_info("https://docs.google.com/document/d/1/edit")["private"])
        self.assertTrue(parse.link_info("https://forms.gle/abc")["private"])
        self.assertFalse(parse.link_info("https://www.youtube.com/watch?v=1")["private"])

    def test_tracking_codes_are_removed_but_real_query_stays(self):
        self.assertEqual(parse.clean_url("https://example.com/a?id=5&utm_source=x"),
                         "https://example.com/a?id=5")


EXPORT = """01/02/24, 9:00 am - Messages and calls are end-to-end encrypted. Only people in this chat can read.
01/02/24, 9:00 am - You created this group
01/02/24, 9:01 am - Sam: First line
second line
01/02/24, 9:03 am - Sam: Pass Hunter42
01/02/24, 9:04 am - Sam: <Media omitted>
report.pdf
01/02/24, 9:30 pm - Sam: See this Instagram post by @a_b: https://www.instagram.com/reel/Abc/?igsh=1
01/02/24, 9:31 pm - Sam: Tiny idea <This message was edited>
02/02/24, 10:00 am - Sam: You deleted this message
02/02/24, 10:05 am - Sam: https://www.instagram.com/reel/Abc/?utm_source=x
02/02/24, 10:06 am - Sam: book.pdf (file attached)
"""


class ParseAndServe(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        inbox = self.tmp / "inbox" / "WhatsApp Chat with Sample"
        inbox.mkdir(parents=True)
        (inbox / "WhatsApp Chat with Sample.txt").write_text(EXPORT, encoding="utf-8")
        (inbox / "book.pdf").write_bytes(b"%PDF-1.4 sample")
        (inbox / "NoExtension").write_bytes(b"%PDF-1.4 sample")
        (inbox / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
        self.old = parse.OUT, sys.argv
        parse.OUT = self.tmp / "vault" / "_personal"
        sys.argv = ["x", str(self.tmp / "inbox")]
        self.run_parse()
        self.view = Personal(self.tmp / "vault")

    def tearDown(self):
        parse.OUT, sys.argv = self.old

    def run_parse(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            parse.main()

    def messages(self):
        return self.view.messages("sample")["messages"]

    def test_every_message_is_read_and_system_lines_kept_apart(self):
        data = json.loads((parse.OUT / "data" / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(data["counts"]["messages"], 8)
        self.assertEqual(data["system"], ["You created this group"])
        self.assertEqual(self.messages()[0]["text"], "First line\nsecond line")

    def test_private_text_stays_out_of_data(self):
        data_text = "".join(p.read_text(encoding="utf-8") for p in (parse.OUT / "data").glob("*.json"))
        self.assertNotIn("Hunter42", data_text)
        overview = json.dumps(self.view.overview()) + json.dumps(self.view.messages("sample"))
        self.assertNotIn("Hunter42", overview)
        hidden = next(m for m in self.messages() if m.get("private"))
        self.assertEqual(hidden["text"], "")
        self.assertEqual(self.view.reveal(hidden["id"]), "Pass Hunter42")
        self.assertIsNone(self.view.reveal("not-a-message"))

    def test_kinds_and_moments(self):
        msgs = self.messages()
        missing = next(m for m in msgs if m["kind"] == "missing_file")
        self.assertEqual(missing["file"]["name"], "report.pdf")
        self.assertFalse(missing["file"]["present"])
        attached = next(m for m in msgs if m["kind"] == "file")
        self.assertTrue(attached["file"]["present"])
        self.assertTrue(next(m for m in msgs if m.get("text") == "Tiny idea")["edited"])
        moments = self.view.messages("sample")["moments"]
        self.assertEqual([len(x["msgs"]) for x in moments], [3, 2, 3])

    def test_a_link_saved_twice_is_one_link(self):
        links = self.view.overview()["links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(len(links[0]["saves"]), 2)
        self.assertEqual(links[0]["handle"], "a_b")

    def test_previews_survive_reprocessing(self):
        path = parse.OUT / "data" / "links.json"
        links = json.loads(path.read_text(encoding="utf-8"))
        next(iter(links.values())).update(status="ok", caption="kept")
        path.write_text(json.dumps(links), encoding="utf-8")
        self.run_parse()
        self.assertEqual(self.view.overview()["links"][0]["caption"], "kept")

    def test_the_page_cannot_change_anything(self):
        self.assertFalse([name for name in dir(self.view) if name in ("mark", "post", "write", "save")])

    def test_tile_arrangement_is_kept_apart_from_the_data(self):
        before = {p.name: p.read_bytes() for p in (parse.OUT / "data").glob("*.json")}
        link = self.view.overview()["links"][0]
        self.view.set_layout("sample", [link["id"], "not-a-link", link["id"]])
        self.assertEqual(self.view.overview()["layout"], {"sample": [link["id"]]})
        self.assertEqual(before, {p.name: p.read_bytes() for p in (parse.OUT / "data").glob("*.json")})

    def test_arrangement_survives_reprocessing_and_can_be_reset(self):
        link = self.view.overview()["links"][0]
        self.view.set_layout("all", [link["id"]])
        self.run_parse()
        self.assertEqual(self.view.layout(), {"all": [link["id"]]})
        self.view.set_layout("all", [])
        self.assertEqual(self.view.layout(), {})

    def test_arrangement_refuses_unknown_views(self):
        with self.assertRaises(ValueError):
            self.view.set_layout("../private", [])

    def test_files_cannot_escape_their_folder(self):
        for name in ("../../private/private.json", "..\\..\\private\\private.json", "../exports/x.txt"):
            self.assertIsNone(self.view.file("sample", name), name)
        self.assertIsNone(self.view.file("nope", "book.pdf"))
        self.assertIsNone(self.view.file("../private", "private.json"))

    def test_only_safe_types_open_in_the_browser(self):
        body, ctype, inline = self.view.file("sample", "NoExtension")
        self.assertEqual((ctype, inline), ("application/pdf", True))
        body, ctype, inline = self.view.file("sample", "page.html")
        self.assertEqual((ctype, inline), ("application/octet-stream", False))

    def test_rebuild_ignores_this_folder(self):
        (parse.OUT / "sources" / "sample" / "files" / "readme.md").write_text("# not a note", encoding="utf-8")
        (self.tmp / "vault" / "real.md").write_text("---\ntitle: Real\n---\n\nbody\n", encoding="utf-8")
        found = [path for path, _ in Vault(self.tmp / "vault").scan()]
        self.assertEqual(found, ["real.md"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
