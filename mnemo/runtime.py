"""Managing the bundled Ollama server process.

The server is started from the copy inside the repo, with OLLAMA_MODELS pointed
at vendor/models, so a checkout is self-contained and leaves nothing behind in
the user's home directory.

Everything degrades: if the server is missing or unreachable, callers fall back
to the offline embedder and router rather than failing. Capture must never
depend on a model being alive.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request

from .paths import OLLAMA_HOST, MODELS_DIR, ollama_binary, ollama_env

_server: subprocess.Popen | None = None


def api(path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    """Call the Ollama HTTP API. Stdlib only, no requests dependency."""
    url = f"{OLLAMA_HOST.rstrip('/')}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body) if body else {}


def api_delete(path: str, payload: dict, timeout: int = 60) -> bool:
    """A DELETE call (Ollama removes models this way). True on success."""
    request = urllib.request.Request(
        f"{OLLAMA_HOST.rstrip('/')}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="DELETE")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return 200 <= response.status < 300


def is_up(timeout: int = 2) -> bool:
    try:
        api("/api/version", timeout=timeout)
        return True
    except Exception:
        return False


def version() -> str | None:
    try:
        return api("/api/version", timeout=3).get("version")
    except Exception:
        return None


def installed_models() -> list[str]:
    try:
        return [m["name"] for m in api("/api/tags", timeout=10).get("models", [])]
    except Exception:
        return []


def _full(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


def has_model(name: str) -> bool:
    """Whether exactly this model (and size tag) is installed.

    Bare names mean ':latest', as Ollama treats them. A different size of the
    same family doesn't count: having qwen3.5:4b says nothing about :9b.
    """
    return _full(name) in {_full(m) for m in installed_models()}


def show(name: str) -> dict:
    """Ollama's description of a model: capabilities, details, parameters."""
    try:
        return api("/api/show", {"model": name}, timeout=30)
    except Exception:
        return {}


def capabilities(name: str) -> set[str]:
    """What a model can do, as Ollama reports it: completion, tools,
    vision, thinking, embedding, audio... Empty when unknown."""
    return set(show(name).get("capabilities") or [])


_CAPS: dict[str, set[str]] = {}


def caps(name: str) -> set[str]:
    """`capabilities`, remembered per model (they don't change once pulled)."""
    if name not in _CAPS:
        found = capabilities(name)
        if not found:
            return found            # Ollama not up yet: ask again next time
        _CAPS[name] = found
    return _CAPS[name]


def think_option(model: str, think: bool = False) -> dict:
    """The `think` request field, for models that have a thinking mode.

    Newer models (Qwen3.5, Gemma 4, DeepSeek-R1) reason out loud by default,
    which multiplies latency. Quick jobs turn it off; "think harder" turns it
    on. Models without the mode get no field at all.
    """
    return {"think": think} if "thinking" in caps(model) else {}


def loaded() -> list[dict]:
    """Models currently in memory, with how much of each sits in VRAM."""
    try:
        return api("/api/ps", timeout=10).get("models", [])
    except Exception:
        return []


def remove(name: str) -> bool:
    """Delete a model's weights from vendor/models (frees disk)."""
    try:
        return api_delete("/api/delete", {"model": name})
    except Exception:
        return False


def start(wait: float = 60.0) -> bool:
    """Start the bundled server if it is not already answering.

    Returns True when the API is reachable. Never raises -- callers fall back
    to offline mode.
    """
    global _server
    if is_up():
        return True

    binary = ollama_binary()
    if binary is None:
        return False

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    creation = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):        # Windows: no console flash
        creation = subprocess.CREATE_NO_WINDOW
    try:
        _server = subprocess.Popen(
            [str(binary), "serve"],
            env=ollama_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
        )
    except OSError:
        return False

    deadline = time.time() + wait
    while time.time() < deadline:
        if is_up():
            return True
        if _server.poll() is not None:                  # died on startup
            return False
        time.sleep(0.4)
    return False


def stop() -> None:
    """Stop only a server this process started; leave a pre-existing one alone."""
    global _server
    if _server is not None and _server.poll() is None:
        _server.terminate()
        try:
            _server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _server.kill()
    _server = None


def pull(model: str, on_progress=None, attempts: int = 40,
         on_retry=None) -> bool:
    """Download a model into vendor/models, retrying until it completes.

    Model weights are multi-gigabyte and CDN connections drop. Ollama keeps
    partial blobs on disk and resumes from them, so a retry costs time but no
    bandwidth -- which makes persistent retrying strictly better than asking
    someone to run the command again. Observed on a flaky link: three manual
    attempts died at 68%, 76% and 78%, each resuming where the last stopped.
    """
    binary = ollama_binary()
    if binary is None:
        return False

    for attempt in range(1, attempts + 1):
        if _pull_once(binary, model, on_progress):
            return True
        if has_model(model):            # manifest landed despite a noisy exit
            return True
        if attempt < attempts:
            if on_retry:
                on_retry(attempt, attempts)
            time.sleep(min(30, 2 * attempt))
    return False


def _pull_once(binary, model: str, on_progress=None) -> bool:
    # encoding/errors are load-bearing: Ollama's progress bar emits bytes that
    # Windows' default cp1252 codec cannot decode, which otherwise kills the
    # pull with a UnicodeDecodeError on any non-UTF-8 console codepage.
    process = subprocess.Popen(
        [str(binary), "pull", model],
        env=ollama_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    for line in process.stdout or []:
        if on_progress:
            on_progress(line.rstrip())
    return process.wait() == 0


def warm(models: list[str]) -> None:
    """Load models into VRAM now so the first real capture is not slow."""
    for model in models:
        try:
            api("/api/generate",
                {"model": model, "prompt": "", "keep_alive": -1, "stream": False},
                timeout=180)
        except Exception:
            pass
