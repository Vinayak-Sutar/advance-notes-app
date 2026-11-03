"""The Wi-Fi lock. Runs a real server and pretends the caller is on the network.

Made-up data only. The QR code is checked here for its structure, and by an
independent reader when one is installed (it isn't a dependency of the app).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemo import devices as D
from mnemo import qr
from mnemo import server as SRV
from mnemo.engine import Engine
from tests.test_server import _free_port


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Pairing(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="mnemo-dev-"))
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.devices = D.Devices(self.folder / "devices.json")

    def test_right_code_gives_a_key_and_only_its_hash_is_stored(self):
        key = self.devices.pair(self.devices.code, "Mozilla (iPhone; CPU)")
        self.assertTrue(self.devices.known(key))
        stored = (self.folder / "devices.json").read_text()
        self.assertNotIn(key, stored)
        self.assertEqual(self.devices.listing()[0]["name"], "iPhone")
        self.assertNotIn("hash", self.devices.listing()[0])

    def test_paired_devices_survive_a_restart_with_a_new_code(self):
        key = self.devices.pair(self.devices.code)
        again = D.Devices(self.folder / "devices.json")
        self.assertTrue(again.known(key))
        self.assertNotEqual(again.code, self.devices.code)

    def test_typed_codes_are_forgiven_their_case_and_spacing(self):
        typed = self.devices.code.replace("-", " ").lower()
        self.assertTrue(self.devices.pair(typed))

    def test_wrong_codes_and_strange_ones(self):
        for code in ("", "AAAA-AAAA", "x" * 500, "नमस्ते", "\x00"):
            self.assertIsNone(self.devices.pair(code))
        self.assertIsNone(self.devices.known("made-up"))
        self.assertIsNone(self.devices.known(None))

    def test_too_many_wrong_codes_pause_even_the_right_one(self):
        for _ in range(D.TRIES):
            self.devices.pair("AAAA-AAAA")
        self.assertTrue(self.devices.paused())
        self.assertIsNone(self.devices.pair(self.devices.code))
        self.devices._misses = [time.monotonic() - D.PAUSE_SECONDS - 1] * D.TRIES
        self.assertTrue(self.devices.pair(self.devices.code))

    def test_removed_device_loses_access(self):
        key = self.devices.pair(self.devices.code)
        self.assertTrue(self.devices.remove(self.devices.listing()[0]["id"]))
        self.assertIsNone(self.devices.known(key))
        self.assertFalse(self.devices.remove("nothing"))

    def test_code_has_no_letters_that_look_alike(self):
        for _ in range(50):
            self.assertRegex(D.new_code(), r"^[2-9A-HJKMNP-TV-Z]{4}-[2-9A-HJKMNP-TV-Z]{4}$")


class Address(unittest.TestCase):
    def test_laptop_addresses_are_told_apart(self):
        self.assertTrue(D._usable("192.168.0.20"))
        self.assertFalse(D._usable("127.0.0.1"))
        self.assertFalse(D._usable("169.254.10.1"))
        self.assertFalse(D._usable("8.8.8.8"))

    def test_banner_shows_the_address_and_a_code(self):
        text = D.banner(7777, "192.168.0.20", "AAAA-BBBB")
        self.assertIn("http://192.168.0.20:7777/pair/AAAA-BBBB", text)
        self.assertIn("http://127.0.0.1:7777", text)
        self.assertIn("▀", text)

    def test_banner_without_wifi_says_so(self):
        text = D.banner(7777, None, "AAAA-BBBB")
        self.assertIn("isn't on Wi-Fi", text)
        self.assertNotIn("AAAA-BBBB", text)


class OverTheNetwork(unittest.TestCase):
    """`_is_local` is swapped to say False, as it would for a phone."""

    ROUTES = ["/", "/personal-notes", "/static/app.html", "/media/pic.png",
              "/personal-file/_thumbs/a.jpg", "/api/state", "/api/notes", "/api/note?id=1",
              "/api/search?q=secret", "/api/personal", "/api/personal/messages?source=a",
              "/api/personal/reveal?id=1", "/api/profiles", "/api/connect", "/api/graph",
              "/api/ai"]

    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="mnemo-lan-"))
        SRV._engine = Engine(cls.folder / "vault", enrich=False)
        SRV._engine.create_note(title="A secret note", body="only for me", topic_path="T")
        SRV._devices = D.Devices(cls.folder / "devices.json")
        (cls.folder / "vault" / "_personal" / "private").mkdir(parents=True)
        (cls.folder / "vault" / "_personal" / "private" / "private.json").write_text(
            '{"m1": "hidden words"}', encoding="utf-8")
        cls.port = _free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), SRV.Handler)
        cls.httpd.verbose = False
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        SRV._engine.close()
        SRV._devices = None
        shutil.rmtree(cls.folder, ignore_errors=True)

    def setUp(self):
        patcher = mock.patch.object(SRV, "_is_local", lambda address: False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, path, payload=None, key=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if payload is None else json.dumps(payload).encode())
        if key:
            req.add_header("Cookie", f"{D.COOKIE}={key}")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=10) as reply:
                return reply.status, reply.read(), reply.headers
        except urllib.error.HTTPError as err:
            return err.code, err.read(), err.headers

    def paired(self):
        code = SRV._devices.code
        status, _, headers = self.call(f"/pair/{code}")
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        return cookie.split(";")[0].split("=", 1)[1]

    def test_nothing_reaches_a_device_that_isnt_paired(self):
        for route in self.ROUTES:
            status, body, _ = self.call(route)
            self.assertEqual(status, 401, route)
            self.assertNotIn(b"secret note", body, route)
            self.assertNotIn(b"hidden words", body, route)

    def test_no_change_can_be_made_without_pairing(self):
        before = SRV._engine.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        for route in ("/api/note/new", "/api/capture", "/api/chat", "/api/undo", "/api/rebuild",
                      "/api/profile", "/api/personal/layout", "/api/ai"):
            status, _, _ = self.call(route, {"title": "x", "text": "x", "message": "x"})
            self.assertEqual(status, 401, route)
        after = SRV._engine.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        self.assertEqual(before, after)

    def test_a_made_up_key_is_refused(self):
        self.assertEqual(self.call("/api/notes", key="made-up")[0], 401)

    def test_the_page_tells_them_how_to_connect_and_shows_no_code(self):
        status, body, _ = self.call("/")
        self.assertEqual(status, 401)
        self.assertIn(b"Connect this device", body)
        self.assertNotIn(SRV._devices.code.encode(), body)

    def test_scanning_the_code_lets_a_phone_in(self):
        key = self.paired()
        self.assertEqual(self.call("/api/notes", key=key)[0], 200)
        self.assertIn(b"A secret note", self.call("/api/notes", key=key)[1])
        self.assertEqual(self.call("/", key=key)[0], 200)
        state = json.loads(self.call("/api/state", key=key)[1])
        self.assertFalse(state["laptop"])

    def test_a_phone_can_save_notes(self):
        key = self.paired()
        status, _, _ = self.call("/api/note/new", {"title": "From my phone"}, key=key)
        self.assertEqual(status, 200)

    def test_typed_code_works_and_a_wrong_one_says_so(self):
        status, _, headers = self.call(f"/pair?code={SRV._devices.code}")
        self.assertEqual((status, headers["Location"]), (303, "/"))
        status, _, headers = self.call("/pair/AAAA-AAAA")
        self.assertEqual((status, headers["Location"]), (303, "/?pair=wrong"))
        self.assertIsNone(headers["Set-Cookie"])
        SRV._devices._misses.clear()

    def test_private_text_and_setup_changes_stay_on_the_laptop(self):
        key = self.paired()
        status, body, _ = self.call("/api/personal/reveal?id=m1", key=key)
        self.assertEqual(status, 403)
        self.assertNotIn(b"hidden words", body)
        self.assertEqual(self.call("/api/connect", key=key)[0], 403)
        for route in ("/api/ai", "/api/ai/remove", "/api/rebuild", "/api/profile/new",
                      "/api/device/remove"):
            self.assertEqual(self.call(route, {"name": "x"}, key=key)[0], 403, route)

    def test_the_laptop_can_do_all_of_it_without_pairing(self):
        with mock.patch.object(SRV, "_is_local", lambda address: True):
            status, body, _ = self.call("/api/personal/reveal?id=m1")
            self.assertEqual((status, json.loads(body)["text"]), (200, "hidden words"))
            info = json.loads(self.call("/api/connect")[1])
            self.assertIn(info["code"], info["url"] or info["code"])
            self.assertEqual(json.loads(self.call("/api/state")[1])["laptop"], True)

    def test_removing_a_phone_from_the_laptop_locks_it_out(self):
        key = self.paired()
        device = SRV._devices.listing()[-1]["id"]
        with mock.patch.object(SRV, "_is_local", lambda address: True):
            self.assertEqual(self.call("/api/device/remove", {"id": device})[0], 200)
        self.assertEqual(self.call("/api/notes", key=key)[0], 401)

    def test_a_phone_keeps_its_key_while_it_uses_the_app(self):
        key = self.paired()
        headers = self.call("/", key=key)[2]
        self.assertIn(key, headers["Set-Cookie"])

    def test_home_screen_icon_brings_its_key_along(self):
        key = self.paired()
        manifest = json.loads(self.call("/manifest.webmanifest", key=key)[1])
        self.assertEqual(manifest["start_url"], f"/open/{key}")
        status, _, headers = self.call(manifest["start_url"])
        self.assertEqual((status, headers["Location"]), (303, "/"))
        self.assertIn(key, headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])

    def test_a_made_up_home_screen_link_gives_nothing(self):
        status, _, headers = self.call("/open/made-up")
        self.assertEqual(status, 303)
        self.assertIsNone(headers["Set-Cookie"])

    def test_the_manifest_is_only_for_connected_devices(self):
        self.assertEqual(self.call("/manifest.webmanifest")[0], 401)

    def test_icons_need_no_pairing_and_nothing_else_comes_with_them(self):
        self.assertEqual(self.call("/static/apple-touch-icon.png")[0], 200)
        self.assertEqual(self.call("/static/icon-192.png")[0], 200)
        self.assertEqual(self.call("/static/app.html")[0], 401)

    def test_foreign_host_and_origin_are_refused_even_when_paired(self):
        key = self.paired()
        for name, value in (("Host", "evil.example.com"), ("Origin", "http://evil.example.com")):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/notes",
                                         headers={name: value, "Cookie": f"{D.COOKIE}={key}"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 403)

    def test_without_lan_mode_nothing_is_paired(self):
        with mock.patch.object(SRV, "_devices", None):
            self.assertEqual(self.call("/api/notes")[0], 401)
            self.assertEqual(self.call("/pair/AAAA-BBBB")[0], 401)


class SecondCopy(unittest.TestCase):
    def test_a_second_server_cant_share_the_port_on_windows(self):
        if os.name != "nt":
            self.skipTest("only Windows lets two servers share a port")
        port = _free_port()
        first = SRV._Server(("127.0.0.1", port), SRV.Handler)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            SRV._Server(("127.0.0.1", port), SRV.Handler)


class DroppedConnections(unittest.TestCase):
    def test_a_phone_hanging_up_prints_nothing(self):
        import io
        import contextlib
        server = SRV._Server(("127.0.0.1", 0), SRV.Handler)
        self.addCleanup(server.server_close)
        noise = io.StringIO()
        with contextlib.redirect_stderr(noise):
            try:
                raise ConnectionResetError(10054, "closed by the remote host")
            except ConnectionResetError:
                server.handle_error(None, ("192.168.0.20", 5000))
        self.assertEqual(noise.getvalue(), "")


class QrCode(unittest.TestCase):
    URL = "http://192.168.0.213:7777/pair/K7M2-QX9P"

    def test_size_follows_the_length(self):
        self.assertEqual(len(qr.encode("hi")), 21)
        self.assertEqual(len(qr.encode(self.URL)), 29)              # version 3
        self.assertEqual(len(qr.encode("x" * 213)), 57)             # version 10, the limit
        with self.assertRaises(ValueError):
            qr.encode("x" * 214)

    def test_the_three_corners_and_timing_are_right(self):
        grid = qr.encode(self.URL)
        size = len(grid)
        finder = ["1111111", "1000001", "1011101", "1011101", "1011101", "1000001", "1111111"]
        for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
            drawn = ["".join("1" if grid[top + r][left + c] else "0" for c in range(7))
                     for r in range(7)]
            self.assertEqual(drawn, finder)
        self.assertEqual([grid[6][c] for c in range(8, size - 8)],
                         [i % 2 == 0 for i in range(8, size - 8)])
        self.assertTrue(grid[size - 8][8])                          # the always-dark module

    def test_error_correction_matches_the_published_example(self):
        # ISO/IEC 18004 annex I: "01234567" as version 1-M.
        data = [16, 32, 12, 86, 97, 128, 236, 17, 236, 17, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qr._correction(data, 10),
                         [165, 36, 212, 193, 237, 54, 199, 135, 44, 85])

    def test_drawings(self):
        grid = qr.encode(self.URL)
        self.assertEqual(qr.svg(grid).count("<svg"), 1)
        self.assertEqual(len(qr.terminal(grid).split("\n")), (len(grid) + 8 + 1) // 2)

    def test_an_independent_reader_agrees(self):
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("no independent QR reader installed")
        for text in (self.URL, "hi", "http://192.168.100.200:7777/pair/ABCD-EFGH",
                     "x" * 100, "y" * 200):
            grid = qr.encode(text)
            scale = 8
            pixels = np.full(((len(grid) + 8) * scale,) * 2, 255, np.uint8)
            for r, row in enumerate(grid):
                for c, dark in enumerate(row):
                    if dark:
                        pixels[(r + 4) * scale:(r + 5) * scale,
                               (c + 4) * scale:(c + 5) * scale] = 0
            found, _, _ = cv2.QRCodeDetector().detectAndDecode(pixels)
            self.assertEqual(found, text)


if __name__ == "__main__":
    unittest.main()
