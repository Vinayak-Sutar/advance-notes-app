"""One-command bootstrap for a fresh checkout.

    python -m mnemo.setup              # runtime + default models
    python -m mnemo.setup --check      # report what is present, download nothing
    python -m mnemo.setup --models-only

Downloads the portable Ollama runtime and the models into `vendor/` inside this
repository. Nothing is written to the user's home directory, AppData, or any
absolute path, so the checkout stays self-contained and portable.
"""
from __future__ import annotations

import argparse
import platform
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from . import runtime
from .models import EMBED_MODEL, PROFILES, auto_profile, chosen_profile, save_profile
from .paths import (CACHE_DIR, MODELS_DIR, OLLAMA_DIR, REPO_ROOT, VENDOR_DIR,
                    describe, ensure_dirs, ollama_binary, system_ollama)

OLLAMA_VERSION = "v0.33.2"
RELEASES = "https://github.com/ollama/ollama/releases/download"


def asset_name() -> str:
    """The release asset for this platform.

    The Windows amd64 build is the large one because it bundles the CUDA
    libraries; that is what makes the GPU usable, so it is worth the size.
    """
    system = platform.system()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if system == "Windows":
        return "ollama-windows-arm64.zip" if arm else "ollama-windows-amd64.zip"
    if system == "Linux":
        return "ollama-linux-arm64.tgz" if arm else "ollama-linux-amd64.tgz"
    if system == "Darwin":
        return "ollama-darwin.tgz"
    raise RuntimeError(f"unsupported platform: {system} {machine}")


def download(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    print(f"  downloading {url.rsplit('/', 1)[-1]} ...")

    def hook(block: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        done = min(block * block_size, total)
        pct = done * 100 / total
        sys.stdout.write(f"\r    {done / 1e6:8.1f} / {total / 1e6:.1f} MB  {pct:5.1f}%")
        sys.stdout.flush()

    urllib.request.urlretrieve(url, partial, reporthook=hook)
    sys.stdout.write("\n")
    partial.replace(target)
    return target


def extract(archive: Path, destination: Path) -> None:
    print(f"  extracting into {destination.relative_to(REPO_ROOT)} ...")
    destination.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(destination)
    else:
        with tarfile.open(archive) as tf:
            tf.extractall(destination)
    if platform.system() != "Windows":
        found = ollama_binary()
        if found:
            found.chmod(0o755)


def install_runtime(force: bool = False) -> bool:
    if ollama_binary() and not force:
        print(f"  runtime already present: "
              f"{ollama_binary().relative_to(REPO_ROOT)}")
        return True
    name = asset_name()
    archive = CACHE_DIR / name
    if not archive.exists():
        download(f"{RELEASES}/{OLLAMA_VERSION}/{name}", archive)
    extract(archive, OLLAMA_DIR)
    binary = ollama_binary()
    if binary is None:
        print("  ERROR: no ollama executable found after extraction", file=sys.stderr)
        return False
    print(f"  installed: {binary.relative_to(REPO_ROOT)}")
    return True


def install_models(models: list[str]) -> bool:
    if not runtime.start():
        print("  ERROR: could not start the bundled Ollama server", file=sys.stderr)
        return False
    print(f"  server up (ollama {runtime.version()}), "
          f"models -> {MODELS_DIR.relative_to(REPO_ROOT)}")
    ok = True
    for model in models:
        if runtime.has_model(model):
            print(f"  {model}: already present")
            continue
        print(f"  pulling {model} ...")

        def progress(line: str, _last=[""]) -> None:
            # Ollama draws a braille spinner and box-drawing progress bar. On a
            # cp1252 console (the Windows default) printing those raises
            # UnicodeEncodeError, so reduce to ASCII before writing.
            clean = "".join(c for c in line if 32 <= ord(c) < 127).strip()
            if clean and clean != _last[0]:
                sys.stdout.write(f"\r    {clean[:78]:<78}")
                sys.stdout.flush()
                _last[0] = clean
        def retried(attempt: int, total: int) -> None:
            sys.stdout.write("\r" + " " * 80 + "\r")
            print(f"    connection dropped — resuming (attempt {attempt + 1}/{total})")

        if runtime.pull(model, progress, on_retry=retried):
            sys.stdout.write("\r" + " " * 80 + "\r")
            print(f"  {model}: ready")
        else:
            sys.stdout.write("\n")
            print(f"  {model}: FAILED after repeated retries", file=sys.stderr)
            ok = False
    return ok


def report() -> None:
    print("paths")
    for key, value in describe().items():
        print(f"  {key:10} {value}")
    other = system_ollama()
    if other:
        print(f"\n  note: a system-wide Ollama exists at {other}.")
        print("        It is ignored on purpose -- this project uses only the")
        print("        copy in vendor/ so models stay inside the repo.")
    print("\nruntime")
    if runtime.is_up():
        print(f"  server     running (ollama {runtime.version()})")
        for model in runtime.installed_models():
            print(f"    model    {model}")
    else:
        print("  server     not running")
        binary = ollama_binary()
        print(f"  binary     {'present' if binary else 'MISSING - run python -m mnemo.setup'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mnemo.setup", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report state, download nothing")
    parser.add_argument("--models-only", action="store_true")
    parser.add_argument("--runtime-only", action="store_true")
    parser.add_argument("--force", action="store_true", help="re-extract the runtime")
    parser.add_argument("--vision", action="store_true",
                        help="also pull a separate vision model if the profile has one")
    parser.add_argument("--profile", choices=["auto", *PROFILES],
                        help="quality profile to set up (default: what this computer suits)")
    args = parser.parse_args(argv)

    ensure_dirs()
    if args.check:
        report()
        return 0

    print(f"setting up in {REPO_ROOT}\n")
    if not args.models_only:
        if not install_runtime(force=args.force):
            return 1
    if not args.runtime_only:
        # The brain follows the quality profile, so a laptop with a small GPU
        # and a desktop with a big one each get a model that suits them.
        if args.profile:
            save_profile(args.profile)
        chosen = chosen_profile()
        profile = PROFILES[auto_profile() if chosen == "auto" else chosen]
        print(f"  profile    {profile.label} ({'picked for this computer' if chosen == 'auto' else 'chosen'})"
              f" -> {profile.brain}")
        wanted = [EMBED_MODEL, profile.brain]
        if args.vision and profile.vision not in wanted:
            wanted.append(profile.vision)
        if not install_models(wanted):
            return 1

    print("\ndone.")
    print("  python -m mnemo status")
    print("  python -m mnemo doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
