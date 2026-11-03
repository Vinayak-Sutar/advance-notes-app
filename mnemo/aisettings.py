"""Settings → AI: which quality profile runs, what's downloaded, and downloads.

Kept apart from server.py because downloads run in a background thread and
outlive any one request; the page polls `state()` for progress.
"""
from __future__ import annotations

import re
import threading

from . import models as M
from . import runtime as R

_pull = {"model": None, "percent": 0, "status": "idle", "error": None}
_pull_lock = threading.Lock()


def _gb(n: int | float) -> float:
    return round((n or 0) / 1e9, 1)


def in_use() -> set[str]:
    """Models the app needs right now; these can't be removed."""
    p = M.ACTIVE
    keep = {M.EMBED_MODEL, M.ROUTER_MODEL, M.VISION_MODEL, p.brain, p.vision}
    if M.THINK_MODEL:
        keep.add(M.THINK_MODEL)
    return {m if ":" in m else f"{m}:latest" for m in keep if m}


def state() -> dict:
    up = R.is_up()
    tags = R.api("/api/tags", timeout=10).get("models", []) if up else []
    sizes = {t["name"]: t.get("size", 0) for t in tags}
    loaded = {m["name"]: m for m in (R.loaded() if up else [])}
    keep = in_use()
    profiles = []
    for p in M.PROFILES.values():
        name = p.brain if ":" in p.brain else f"{p.brain}:latest"
        spec = next((s for s in M.CATALOGUE if s.name == p.brain), None)
        profiles.append({"name": p.name, "label": p.label, "note": p.note, "brain": p.brain,
                         "installed": name in sizes,
                         "size_gb": _gb(sizes[name]) if name in sizes else (spec.approx_gb if spec else None)})
    with _pull_lock:
        pull = dict(_pull)
    return {
        "running": up, "ollama": R.version() if up else None,
        "chosen": M.chosen_profile(), "active": M.ACTIVE.name, "auto": M.auto_profile(),
        "hardware": {"gpu_gb": M.gpu_memory_gb(), "ram_gb": M.ram_gb()},
        "roles": {"brain": M.ROUTER_MODEL, "vision": M.VISION_MODEL, "embedder": M.EMBED_MODEL,
                  "think": M.THINK_MODEL},
        "profiles": profiles,
        "models": sorted(({"name": n, "size_gb": _gb(s), "in_use": n in keep,
                           "loaded": n in loaded,
                           "on_gpu_pct": round(100 * loaded[n].get("size_vram", 0) / loaded[n]["size"])
                           if n in loaded and loaded[n].get("size") else None}
                          for n, s in sizes.items()), key=lambda m: (not m["in_use"], m["name"])),
        "total_gb": _gb(sum(sizes.values())),
        "pull": pull,
    }


def choose(profile: str, engines=()) -> dict:
    """Switch profile. Downloads its brain first if needed (in the background)."""
    M.save_profile(profile)
    wanted = M.PROFILES[M.auto_profile() if M.chosen_profile() == "auto" else M.chosen_profile()]
    if R.is_up() and not R.has_model(wanted.brain):
        start_pull(wanted.brain, engines)
    _apply(engines)
    return state()


def _apply(engines) -> None:
    M.refresh()
    for engine in engines:
        provider = getattr(engine, "provider", None)
        if provider is not None and hasattr(provider, "model") and provider.name.startswith("ollama:"):
            provider.model = M.ROUTER_MODEL      # filing follows the new brain
            provider.name = f"ollama:{M.ROUTER_MODEL}"


def start_pull(model: str, engines=()) -> bool:
    with _pull_lock:
        if _pull["status"] == "pulling":
            return False
        _pull.update(model=model, percent=0, status="pulling", error=None)

    def progress(line: str):
        m = re.findall(r"(\d{1,3})%", line)
        if m:
            with _pull_lock:
                _pull["percent"] = max(_pull["percent"], int(m[-1]))

    def work():
        ok = R.pull(model, on_progress=progress, attempts=20)
        with _pull_lock:
            _pull.update(status="done" if ok else "failed", percent=100 if ok else _pull["percent"],
                         error=None if ok else "The download didn't finish. Check the internet and try again.")
        if ok:
            _apply(engines)

    threading.Thread(target=work, daemon=True, name=f"pull-{model}").start()
    return True


def remove(model: str) -> dict:
    full = model if ":" in model else f"{model}:latest"
    if full in in_use():
        return {"ok": False, "error": "That model is in use right now."}
    return {"ok": R.remove(full), "state": state()}
