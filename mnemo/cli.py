"""Command line interface.

    python -m mnemo add "@gaming rtx 4070 undervolt settings #pc"
    python -m mnemo tree
    python -m mnemo find "chunking"
    python -m mnemo inbox
    python -m mnemo garden
    python -m mnemo undo

The vault is the first profile in profiles/profiles.json, or wherever
MNEMO_VAULT or --vault points.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import urlopen

from . import gardener as Gd
from . import notes as N
from . import profiles as P
from . import search as S
from . import topics as T
from .engine import Engine
from .paths import DEFAULT_VAULT as _DEFAULT_VAULT
from .paths import describe

DEFAULT_VAULT = str(_DEFAULT_VAULT)

KIND_ICON = {"video": "[v]", "link": "[l]", "article": "[a]", "task": "[ ]",
             "image": "[i]", "idea": "[*]", "reference": "[r]", "note": "[n]",
             "snippet": "[s]"}


def _engine(args) -> Engine:
    online = None if getattr(args, "online", None) is None else args.online
    engine = Engine(args.vault, online=online)
    if engine.embedder_changed:
        print(f"  WARNING: this vault was indexed with {engine.embedder_changed!r}",
              file=sys.stderr)
        print(f"           now using {engine.embedder.name!r}. Vectors from two "
              f"models are not comparable.", file=sys.stderr)
        print(f"           run:  python -m mnemo rebuild", file=sys.stderr)
    return engine


# ------------------------------------------------------------- commands ----
def cmd_add(engine: Engine, args) -> int:
    text = " ".join(args.text).strip()
    if not text:
        print("nothing to add", file=sys.stderr)
        return 1
    result = engine.capture(text)
    print(f"  {result.summary()}")
    print(f"  {result.path}")
    if result.bootstrapped:
        print(f"  built the initial taxonomy from your first "
              f"{engine.cfg.cold_start_notes} notes "
              f"({len(result.bootstrapped)} topics)")
    if result.promoted:
        names = [T.get(engine.conn, t)["name"] for t in result.promoted]
        print(f"  {', '.join(names)} earned a folder")
    return 0


def cmd_tree(engine: Engine, args) -> int:
    def walk(parent_id, indent=0):
        for row in T.children(engine.conn, parent_id):
            marker = "" if row["state"] == "active" else "  (provisional)"
            print(f'{"  " * indent}{row["name"]}  {row["note_count"]}{marker}')
            counts = S.view_counts(engine.conn, row["id"])
            views = [f"{k} {v}" for k, v in sorted(counts.items())
                     if k in ("video", "link", "article", "task", "pending")]
            if views and args.views:
                print(f'{"  " * (indent + 1)}. {"  ".join(views)}')
            walk(row["id"], indent + 1)

    walk(None)
    unfiled = len(N.inbox(engine.conn))
    if unfiled:
        print(f"\nInbox  {unfiled}")
    return 0


def cmd_find(engine: Engine, args) -> int:
    query = " ".join(args.query)
    hits = S.search(engine.conn, query, engine._embed_one(query), limit=args.limit)
    if not hits:
        print("  no matches")
        return 0
    for hit in hits:
        print(f'  {hit.title[:60]:62} {hit.topic_path or "Inbox"}')
        print(f'      {hit.snippet[:90]}  ({hit.via})')
    return 0


def cmd_inbox(engine: Engine, args) -> int:
    rows = N.inbox(engine.conn)
    review = N.needing_review(engine.conn)
    if not rows and not review:
        print("  inbox is empty")
        return 0
    for row in rows:
        print(f'  {KIND_ICON.get(row["kind"], "[n]")} {row["title"][:70]}')
    if review:
        print(f"\n  {len(review)} filed but flagged for review:")
        for row in review[:15]:
            topic = T.path_of(engine.conn, N.primary_topic(engine.conn, row["id"]))
            print(f'    {row["title"][:52]:54} -> {topic}')
    return 0


def cmd_garden(engine: Engine, args) -> int:
    proposals = Gd.scan(engine.conn, engine.cfg)
    if not proposals:
        print("  nothing to tidy")
        return 0
    for proposal in proposals:
        print(f"  [{proposal.kind}] {proposal.summary}")
    print(f"\n  {len(proposals)} proposal(s). Nothing applied — these are "
          f"suggestions, review them before acting.")
    return 0


def cmd_move(engine: Engine, args) -> int:
    path = engine.move_note(args.note_id, args.topic)
    print(f"  -> {path}")
    return 0


def cmd_undo(engine: Engine, args) -> int:
    record = engine.undo()
    print(f'  undid: {record["op"]}' if record else "  nothing to undo")
    return 0


def cmd_rebuild(engine: Engine, args) -> int:
    stats = engine.rebuild()
    print(f'  reindexed {stats["notes"]} notes into {stats["topics"]} topics '
          f"from the Markdown files alone")
    return 0


def cmd_status(engine: Engine, args) -> int:
    conn = engine.conn
    total = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
    active = [r for r in T.all_active(conn) if r["state"] == "active"]
    provisional = [r for r in T.all_active(conn) if r["state"] == "provisional"]
    for key, value in describe().items():
        print(f"  {key:12} {value}")
    print(f"  embedder     {engine.embedder.name}")
    print(f"  router       {engine.provider.name}")
    print(f"  thresholds   {engine.cfg.name}")
    print(f"  notes        {total}")
    print(f"  topics       {len(active)} active, {len(provisional)} provisional")
    print(f"  inbox        {len(N.inbox(conn))}")
    print(f"  review queue {len(N.needing_review(conn))}")
    if total < engine.cfg.cold_start_notes and not active:
        remaining = engine.cfg.cold_start_notes - total
        print(f"\n  cold start: {remaining} more note(s) before the initial "
              f"taxonomy is clustered")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mnemo", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault", default=DEFAULT_VAULT,
                        help=f"default: {DEFAULT_VAULT}")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--online", dest="online", action="store_true", default=None,
                       help="require the local models (fail if unavailable)")
    group.add_argument("--offline", dest="online", action="store_false",
                       help="force the dependency-free stack")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="capture a note (the one input channel)")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("tree", help="show the topic hierarchy")
    p.add_argument("--views", action="store_true", help="include per-kind counts")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("find", help="hybrid keyword + semantic search")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("inbox", help="unfiled notes and the review queue")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("garden", help="propose merges, splits and reparents")
    p.set_defaults(func=cmd_garden)

    p = sub.add_parser("move", help="refile a note (teaches the router)")
    p.add_argument("note_id")
    p.add_argument("topic")
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("undo", help="undo the last organisational change")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("rebuild", help="rebuild the index from the .md files")
    p.set_defaults(func=cmd_rebuild)

    p = sub.add_parser("status", help="vault and configuration summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("serve", help="run the web GUI")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--lan", action="store_true",
                   help="also open on this Wi-Fi, for devices you pair with a QR code")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("connect", help="show the QR code of the Notes that's running")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--address", help="use this address, e.g. the laptop's Mobile Hotspot")
    p.set_defaults(func=cmd_connect)

    p = sub.add_parser("profiles", help="list profiles, or add one")
    p.add_argument("--add", metavar="NAME")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("setup", help="download the runtime and models into vendor/")
    p.add_argument("setup_args", nargs="*")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("doctor", help="verify the local model stack")
    p.set_defaults(func=cmd_doctor)
    return parser


def _single_vault(args) -> bool:
    """The user pointed at one vault, so profiles are out of the picture."""
    return bool(os.environ.get("MNEMO_VAULT")) or args.vault != DEFAULT_VAULT


def cmd_serve(engine: None, args) -> int:
    # The server owns its Engines, one per profile, and setting up profiles for
    # the first time moves the vault, so no Engine is opened before this runs.
    online = getattr(args, "online", None)
    from .server import serve
    serve(args.vault, port=args.port, host=args.host,
          open_browser=not args.no_browser, online=online, verbose=args.verbose,
          profiles=None if _single_vault(args) else P.first_run(online=online), lan=args.lan)
    return 0


def cmd_connect(engine: None, args) -> int:
    from . import devices as D
    try:
        with urlopen(f"http://127.0.0.1:{args.port}/api/connect", timeout=5) as reply:
            info = json.load(reply)
    except OSError:
        print("  Notes isn't running. Double-click the Notes shortcut first.", file=sys.stderr)
        return 1
    if not info.get("lan"):
        print("  Notes is only open on this laptop. Restart it with:  python -m mnemo serve --lan")
        return 1
    D.show(D.banner(args.port, args.address or info.get("address"), info["code"]))
    return 0


def cmd_profiles(engine: None, args) -> int:
    online = getattr(args, "online", None)
    P.first_run(online=online)
    if args.add:
        P.create(args.add, online=online)
    for profile in P.load():
        print(f"  {profile.name:20} {profile.path}")
    return 0


def cmd_setup(engine: Engine, args) -> int:
    from .setup import main as setup_main
    return setup_main(args.setup_args)


def cmd_doctor(engine: Engine, args) -> int:
    from .doctor import main as doctor_main
    return doctor_main()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.func in (cmd_serve, cmd_connect, cmd_profiles):
        return args.func(None, args)
    engine = _engine(args)
    try:
        return args.func(engine, args)
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
