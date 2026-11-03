"""Profiles: separate sets of notes on one computer.

Each profile is an ordinary vault in its own folder, so nothing in one can reach
another, and backing one up or moving it is just a folder operation:

    profiles/profiles.json    the list, first entry is the default
    profiles/<id>/            a whole vault: notes, index, Personal notes
    backups/                  dated zips, see `backup`

The names come from that list and nothing here knows them, so this works the
same for anyone. A profile is not an account: choosing one needs no password.
Who may open the app at all is decided separately, in devices.py.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

from .paths import BACKUPS_DIR, PROFILES_DIR, REPO_ROOT

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "how-to-use.md"
GUIDE_TITLE = "How to use Notes"
KEEP_BACKUPS = 7
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# Folder names Windows refuses, whatever the case.
RESERVED = {"con", "prn", "aux", "nul", *(f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10))}


@dataclass(frozen=True)
class Profile:
    id: str
    name: str

    @property
    def path(self) -> Path:
        return PROFILES_DIR / self.id


def _registry() -> Path:
    return PROFILES_DIR / "profiles.json"


def load() -> list[Profile]:
    try:
        entries = json.loads(_registry().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [Profile(e["id"], e.get("name") or e["id"]) for e in entries
            if isinstance(e, dict) and ID_RE.match(str(e.get("id", "")))]


def _save(profiles: list[Profile]) -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _registry().with_suffix(".tmp")
    tmp.write_text(json.dumps([{"id": p.id, "name": p.name} for p in profiles], indent=1),
                   encoding="utf-8")
    tmp.replace(_registry())


def slug(name: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:36] or "profile"
    return f"{out}-1" if out in RESERVED else out


def seed_guide(path: Path, online: bool | None = None) -> None:
    """Give a new profile the how-to note, so it never opens onto a blank page.

    Uses the same models the app will, or the index would be marked as needing
    a rebuild the first time the profile opens.
    """
    from .engine import Engine

    try:
        body = GUIDE.read_text(encoding="utf-8").strip()
    except OSError:
        return
    engine = Engine(path, online=online, enrich=False)
    try:
        engine.create_note(title=GUIDE_TITLE, body=body, topic_path="Getting started")
    finally:
        engine.close()


def create(name: str, online: bool | None = None) -> Profile:
    name = name.strip()
    if not name:
        raise ValueError("A profile needs a name.")
    profiles = load()
    if any(p.name.lower() == name.lower() for p in profiles):
        raise ValueError(f"There is already a profile called {name}.")
    pid, n = slug(name), 1
    while (PROFILES_DIR / pid).exists():
        n += 1
        pid = f"{slug(name)}-{n}"
    profile = Profile(pid, name)
    profile.path.mkdir(parents=True)
    seed_guide(profile.path, online)
    _save(profiles + [profile])
    return profile


def first_run(main: str = "Main", test: str | None = None,
              online: bool | None = None) -> list[Profile]:
    """Set up the starting profiles, once.

    An existing `vault/` keeps its notes: it becomes the main profile, or the
    `test` profile if one is named, after a checked zip of it is taken. A main
    profile that starts new gets the guide, plus its own copy of the old
    vault's Personal notes. Safe to run again: nothing happens once the list
    exists, and a run that was interrupted before writing it carries on.
    """
    if _registry().exists():
        return load()
    first = Profile(slug(main), main)
    second = Profile(slug(test), test) if test else None
    keeper = second or first
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)

    legacy = REPO_ROOT / "vault"
    had_notes = legacy.is_dir() and any(legacy.iterdir())
    if had_notes:
        saved = BACKUPS_DIR / f"vault-before-profiles-{date.today().isoformat()}.zip"
        _zip(legacy, saved, skip_index=False)
        with zipfile.ZipFile(saved) as archive:
            if archive.testzip() is not None:
                raise OSError(f"{saved.name} didn't check out, so the vault was left alone")
        legacy.rename(keeper.path)

    keeper.path.mkdir(parents=True, exist_ok=True)
    first.path.mkdir(parents=True, exist_ok=True)
    personal = keeper.path / "_personal"
    if second and personal.is_dir():
        shutil.copytree(personal, first.path / "_personal", dirs_exist_ok=True)
    if second or not had_notes:
        seed_guide(first.path, online)
    _save([first, second] if second else [first])
    return load()


# ---------------------------------------------------------------- backups ----
def _files(folder: Path, skip_index: bool = True) -> Iterator[Path]:
    """The index is left out of routine backups: it is rebuilt from the notes,
    and it may be open and half-written."""
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not (skip_index and path.name.startswith("index.db")):
            yield path


def _zip(folder: Path, target: Path, skip_index: bool = True, note: bytes = b"") -> None:
    """Write a zip under its real name only once it's complete."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
        archive.comment = note
        for path in _files(folder, skip_index):
            archive.write(path, path.relative_to(folder))
    tmp.replace(target)


def _fingerprint(folder: Path) -> bytes:
    """Changes whenever a file is added, edited or removed."""
    digest = hashlib.sha256()
    for path in _files(folder):
        stat = path.stat()
        digest.update(f"{path.relative_to(folder)}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest().encode()


def _same_as(archive: Path, mark: bytes) -> bool:
    try:
        with zipfile.ZipFile(archive) as latest:
            return latest.comment == mark
    except zipfile.BadZipFile:
        return False


def backup(profile: Profile, keep: int = KEEP_BACKUPS, today: date | None = None) -> Path | None:
    """One dated zip a day, and only if something changed since the last one."""
    target = BACKUPS_DIR / f"{profile.id}-{(today or date.today()).isoformat()}.zip"
    if target.exists() or not profile.path.is_dir():
        return None
    old = sorted(BACKUPS_DIR.glob(f"{profile.id}-????-??-??.zip"))
    mark = _fingerprint(profile.path)
    if old and _same_as(old[-1], mark):
        return None
    _zip(profile.path, target, note=mark)
    for stale in (old + [target])[:-keep]:
        stale.unlink(missing_ok=True)
    return target
