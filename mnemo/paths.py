"""Every path this project uses, resolved relative to the repository.

The whole product lives inside the repo directory and nothing is written to
the user's home, AppData, or any absolute location. That is what makes a
checkout portable: copy the folder to another machine or another user account,
run the setup script, and it works unchanged.

Anything here can still be overridden by environment variable, so a user who
*wants* their vault on a different drive can have it.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path

# mnemo/paths.py -> mnemo/ -> <repo root>
REPO_ROOT = Path(os.environ.get("MNEMO_HOME") or Path(__file__).resolve().parent.parent)

VENDOR_DIR = REPO_ROOT / "vendor"
OLLAMA_DIR = VENDOR_DIR / "ollama"          # extracted portable runtime
MODELS_DIR = VENDOR_DIR / "models"          # OLLAMA_MODELS lives here, not ~/.ollama
CACHE_DIR = VENDOR_DIR / "cache"            # downloads in progress

PROFILES_DIR = REPO_ROOT / "profiles"       # one vault per profile, see profiles.py
BACKUPS_DIR = REPO_ROOT / "backups"


def _first_profile() -> Path | None:
    try:
        entries = json.loads((PROFILES_DIR / "profiles.json").read_text(encoding="utf-8"))
        return PROFILES_DIR / entries[0]["id"]
    except (OSError, ValueError, LookupError, TypeError):
        return None


DEFAULT_VAULT = Path(os.environ.get("MNEMO_VAULT") or _first_profile() or (REPO_ROOT / "vault"))
OLLAMA_HOST = os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"

IS_WINDOWS = platform.system() == "Windows"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""


def ensure_dirs() -> None:
    for directory in (VENDOR_DIR, MODELS_DIR, CACHE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def ollama_binary() -> Path | None:
    """The bundled Ollama executable, or None if setup has not run.

    Only the copy inside the repo counts. A system-wide Ollama on PATH is
    deliberately ignored: relying on it would make behaviour depend on which
    machine the code happens to be running on, and its models would live in the
    user's home directory rather than here.
    """
    candidates = [
        OLLAMA_DIR / f"ollama{EXE_SUFFIX}",
        OLLAMA_DIR / "bin" / f"ollama{EXE_SUFFIX}",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # The zip layout has moved between releases; fall back to a shallow search.
    if OLLAMA_DIR.is_dir():
        for found in OLLAMA_DIR.rglob(f"ollama{EXE_SUFFIX}"):
            if found.is_file():
                return found
    return None


def system_ollama() -> str | None:
    """Only used to warn that a system install exists and is being bypassed."""
    return shutil.which("ollama")


def ollama_env() -> dict[str, str]:
    """Environment for the Ollama server so its models land in the repo."""
    env = dict(os.environ)
    env["OLLAMA_MODELS"] = str(MODELS_DIR)
    env["OLLAMA_HOST"] = OLLAMA_HOST.replace("http://", "").replace("https://", "")
    # Keep both models resident: a swap costs seconds and would wreck capture
    # latency, which is the one thing this app cannot afford to lose.
    env.setdefault("OLLAMA_KEEP_ALIVE", "-1")
    env.setdefault("OLLAMA_MAX_LOADED_MODELS", "2")
    return env


def describe() -> dict[str, str]:
    binary = ollama_binary()
    return {
        "repo": str(REPO_ROOT),
        "vault": str(DEFAULT_VAULT),
        "vendor": str(VENDOR_DIR),
        "models": str(MODELS_DIR),
        "ollama": str(binary) if binary else "(not installed - run: python -m mnemo.setup)",
        "host": OLLAMA_HOST,
    }
