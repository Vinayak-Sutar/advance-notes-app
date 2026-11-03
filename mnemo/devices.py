"""Who may open Notes over Wi-Fi.

This laptop always may. Anything else has to be paired first, or anyone on the
same network could read your notes:

* Each start makes a new code, shown only in the laptop's window, as a QR code
  and as text. Opening it marks that device as yours with a long random key.
* The key is kept in a cookie the page's scripts can't read. Only its hash is
  stored here, so this file is no use to anyone who copies it.
* Wrong codes are limited: too many in a few minutes and pairing pauses.

Removing a device takes effect on its next request.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from datetime import date
from pathlib import Path

from . import qr

COOKIE = "notes_device"
ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"     # none that look alike: 0/O, 1/I/L, U/V
TRIES = 10
PAUSE_SECONDS = 300
KINDS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android phone"),
         ("Windows", "Windows computer"), ("Macintosh", "Mac"), ("Linux", "Linux computer"))


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_code() -> str:
    letters = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{letters[:4]}-{letters[4:]}"


def tidy_code(typed: str) -> str:
    """So "k7m2 qx9p" counts: phones capitalise and space things their own way."""
    letters = re.sub(r"[^A-Z0-9]", "", typed.upper())
    return f"{letters[:4]}-{letters[4:]}"


def device_name(user_agent: str) -> str:
    return next((name for word, name in KINDS if word in user_agent), "Device")


class Devices:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.code = new_code()
        self._lock = threading.Lock()
        self._misses: list[float] = []
        self._devices = self._load()

    def _load(self) -> list[dict]:
        try:
            entries = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [e for e in entries if isinstance(e, dict) and e.get("hash")]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._devices, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _paused(self) -> bool:
        now = time.monotonic()
        self._misses = [t for t in self._misses if now - t < PAUSE_SECONDS]
        return len(self._misses) >= TRIES

    def paused(self) -> bool:
        with self._lock:
            return self._paused()

    def pair(self, code: str, user_agent: str = "") -> str | None:
        """A key for a new device if the code is right, otherwise None."""
        with self._lock:
            if self._paused():
                return None
            if not secrets.compare_digest(tidy_code(code), self.code):
                self._misses.append(time.monotonic())
                return None
            key = secrets.token_urlsafe(32)
            today = date.today().isoformat()
            self._devices.append({"id": secrets.token_hex(4), "name": device_name(user_agent),
                                  "hash": _hash(key), "added": today, "seen": today})
            self._save()
            return key

    def known(self, key: str | None) -> dict | None:
        if not key:
            return None
        digest = _hash(key)
        with self._lock:
            found = next((d for d in self._devices if secrets.compare_digest(d["hash"], digest)),
                         None)
            if found:
                today = date.today().isoformat()
                if found["seen"] != today:
                    found["seen"] = today
                    self._save()
            return found

    def listing(self) -> list[dict]:
        with self._lock:
            return [{k: d.get(k) for k in ("id", "name", "added", "seen")} for d in self._devices]

    def remove(self, device_id: str) -> bool:
        with self._lock:
            kept = [d for d in self._devices if d.get("id") != device_id]
            removed = len(kept) != len(self._devices)
            self._devices = kept
            if removed:
                self._save()
            return removed


# ------------------------------------------------------------- the address ----
def _usable(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_private and not ip.is_loopback and not ip.is_link_local


def lan_address() -> str | None:
    """This laptop's address on the network, or None without one.

    Asks which address would be used to reach the internet, which skips the
    virtual adapters that VMware and the like add. Nothing is actually sent.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))     # reserved for documentation, never used
            address = probe.getsockname()[0]
        if _usable(address):
            return address
    except OSError:
        pass
    try:
        return next(filter(_usable, socket.gethostbyname_ex(socket.gethostname())[2]), None)
    except OSError:
        return None


def pair_url(address: str, port: int, code: str) -> str:
    return f"http://{address}:{port}/pair/{code}"


def banner(port: int, address: str | None, code: str) -> str:
    lines = ["", "  Notes is running", "", f"  On this laptop   http://127.0.0.1:{port}"]
    if address:
        url = pair_url(address, port, code)
        lines += ["  On your phone    scan this, or type the address",
                  f"                   {url}", "", qr.terminal(qr.encode(url)), ""]
    else:
        lines += ["  On your phone    this laptop isn't on Wi-Fi yet.",
                  "                   Connect it, then click the shortcut again.", ""]
    return "\n".join(lines)


def show(text: str) -> None:
    if os.name == "nt":
        os.system("")                   # turns on colours in the Windows console
    if hasattr(sys.stdout, "reconfigure"):
        # A console takes Unicode anyway; this keeps the blocks when the output
        # goes to a file or a pipe, where Windows would use an old code page.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text, flush=True)
