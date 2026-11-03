"""Which models this project uses, and how that adapts to the machine.

Three quality profiles, picked from the hardware (or chosen in Settings):

    fast      small GPU or CPU only     qwen3.5:2b, everything quick
    balanced  ~6 GB GPU and up          gemma4:e4b, the most accurate measured
    best      "Thorough"                gemma4:e4b reasoning step by step first

A machine with a much bigger GPU can point MNEMO_ROUTER_MODEL at a larger
model (gemma4:26b, say); run the benchmark on it before trusting it.

Every profile shares one embedder. The benchmark found no embedder worth the
switch: qwen3-embedding:0.6b ranked the right note first slightly more often
but takes 2.4 GB of VRAM against nomic's 0.3 GB, and embeddinggemma tied. Changing the embedder means re-embedding
every note (`python -m mnemo rebuild`), so it is a deliberate one-off upgrade,
not something a profile switch should trigger.

The constraint behind the balanced choice: model swap-in costs seconds, and
capture latency is the one thing this app cannot afford to lose, so the brain
and the embedder must fit in VRAM *together* and stay resident.

The defaults below are not picked from blog rankings; they come from
`python -m mnemo.bench`, which runs each candidate on the app's own jobs
(see docs/bench-results.md).

Environment variables still win over everything, for experiments:
MNEMO_PROFILE, MNEMO_EMBED_MODEL, MNEMO_ROUTER_MODEL, MNEMO_VISION_MODEL,
MNEMO_THINK_MODEL, MNEMO_EMBED_DIM.
"""
from __future__ import annotations

import ctypes
import json
import os
import platform
import subprocess
from dataclasses import dataclass

from .paths import VENDOR_DIR

SETTINGS_FILE = VENDOR_DIR / "ai.json"      # per machine, like the models themselves

EMBEDDER = "nomic-embed-text"
EMBEDDER_DIM = 768


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    brain: str              # chat, note AI, filing decisions
    vision: str             # reading pictures
    think: str | None       # a separate "think harder" model, if any
    note: str
    thinks: bool = False    # reason step by step on every chat / note-AI answer


# Measured on the reference laptop (RTX 3060, 6 GB), 25 Oct 2026; see
# docs/bench-results.md. gemma4:e4b was the only candidate with a perfect
# score on every job (note AI 29/29, chat 15/15, honest 8/8, tools 16/16,
# picture text 100%) and it fits a 6 GB card next to the embedder.
# qwen3.5:9b matched its accuracy but ran at 7 tokens/s here and chose tools
# worse; deepseek-r1:8b barely used tools and took 13 s to start answering.
PROFILES = {
    "fast": Profile("fast", "Fast", "qwen3.5:2b", "qwen3.5:2b", None,
                    "Quickest, smallest brain. For GPUs under 5 GB or computers without one. "
                    "A little less reliable when rewriting notes."),
    "balanced": Profile("balanced", "Balanced", "gemma4:e4b", "gemma4:e4b", None,
                        "The most accurate in our tests, and it fits a 6 GB GPU."),
    "best": Profile("best", "Thorough", "gemma4:e4b", "gemma4:e4b", None,
                    "The same brain, reasoning step by step before every answer. "
                    "Slower, more careful.", thinks=True),
}
LEGACY = Profile("legacy", "Original", "gemma3:4b", "gemma3:4b", None,
                 "The first setup, used only if no newer brain is on this computer.")


# --------------------------------------------------------------- hardware ----
def gpu_memory_gb() -> float:
    """Total memory of the first NVIDIA GPU, or 0 when there isn't one."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return round(float(out.stdout.strip().splitlines()[0]) / 1024, 1)
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return 0.0


def ram_gb() -> float:
    if platform.system() == "Windows":
        class MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        stat = MemStatus()
        stat.dwLength = ctypes.sizeof(MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return round(stat.ullTotalPhys / 1024 ** 3, 1)
        return 0.0
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3, 1)
    except (ValueError, OSError, AttributeError):
        return 0.0


def auto_profile(vram: float | None = None) -> str:
    """The profile this machine can run comfortably. "Thorough" is never
    picked automatically: it trades speed for care, which is the user's call."""
    vram = gpu_memory_gb() if vram is None else vram
    return "balanced" if vram >= 5 else "fast"


# ---------------------------------------------------------------- choice ----
def _saved() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def chosen_profile() -> str:
    """'auto' or a profile name, as saved in Settings (or MNEMO_PROFILE)."""
    name = os.environ.get("MNEMO_PROFILE") or _saved().get("profile") or "auto"
    return name if name == "auto" or name in PROFILES else "auto"


def save_profile(name: str) -> None:
    data = _saved()
    data["profile"] = name if name in PROFILES else "auto"
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def resolve(installed: set[str] | None = None) -> Profile:
    """The profile in force: the chosen one, or -- while its brain is still
    downloading -- the nearest profile whose brain is already here, and only
    then the original setup."""
    name = chosen_profile()
    wanted = auto_profile() if name == "auto" else name
    profile = PROFILES[wanted]
    if installed is None or _full(profile.brain) in installed:
        return profile
    order = {"best": ["balanced", "fast"], "balanced": ["fast", "best"],
             "fast": ["balanced", "best"]}[wanted]
    for other in order:
        if _full(PROFILES[other].brain) in installed:
            return PROFILES[other]
    return LEGACY


def _full(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


def apply(profile: Profile) -> None:
    """Make `profile` the one every new model client uses."""
    global ROUTER_MODEL, VISION_MODEL, THINK_MODEL, ACTIVE
    ACTIVE = profile
    ROUTER_MODEL = os.environ.get("MNEMO_ROUTER_MODEL", profile.brain)
    VISION_MODEL = os.environ.get("MNEMO_VISION_MODEL", profile.vision)
    THINK_MODEL = os.environ.get("MNEMO_THINK_MODEL", profile.think or "") or None


def refresh() -> Profile:
    """Re-read the choice and what's installed, and apply it."""
    from . import runtime
    installed = {_full(m) for m in runtime.installed_models()} if runtime.is_up() else None
    profile = resolve(installed)
    apply(profile)
    return profile


EMBED_MODEL = os.environ.get("MNEMO_EMBED_MODEL", EMBEDDER)
EMBED_DIM = int(os.environ.get("MNEMO_EMBED_DIM", str(EMBEDDER_DIM)))
ACTIVE: Profile = LEGACY
ROUTER_MODEL = VISION_MODEL = LEGACY.brain
THINK_MODEL: str | None = None
apply(LEGACY)        # safe default until refresh() has asked Ollama what's installed


# ------------------------------------------------------------- catalogue ----
@dataclass(frozen=True)
class ModelSpec:
    name: str
    role: str
    approx_gb: float
    note: str


CATALOGUE = [
    ModelSpec("nomic-embed-text", "embed", 0.3, "768-dim, strong retrieval, tiny."),
    ModelSpec("qwen3.5:2b", "brain", 2.4, "Fast profile. Tools, thinking and vision."),
    ModelSpec("gemma4:e4b", "brain", 4.3, "Balanced and Thorough. Tools, thinking, vision, audio."),
    ModelSpec("gemma3:4b", "brain", 3.7, "The original brain. No tool use."),
]


def resident_estimate() -> float:
    """Rough VRAM for the models kept loaded during normal use."""
    by_name = {spec.name: spec for spec in CATALOGUE}
    total = 0.0
    for name in {EMBED_MODEL, ROUTER_MODEL}:
        spec = by_name.get(name)
        total += spec.approx_gb if spec else 3.0
    return total
