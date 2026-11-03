"""Append-only operation log with stored inverses.

Every mutation that changes how the vault is organised writes a journal row
carrying both what it did and how to undo it. That is what makes "the LLM files
freely" safe: nothing it does is one-way, including a gardener merge from last
week.

Handlers are registered by the engine (which owns the vault and the DB), so this
module stays pure storage and has no import cycle with engine/vault.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Callable

from .db import now_iso

Handler = Callable[[dict], None]
_HANDLERS: dict[str, Handler] = {}


def register(op: str, handler: Handler) -> None:
    _HANDLERS[op] = handler


def clear_handlers() -> None:
    _HANDLERS.clear()


def record(conn: sqlite3.Connection, op: str, payload: dict, inverse: dict) -> int:
    """Log an operation. `inverse` must itself be a valid {op, ...} record."""
    cur = conn.execute(
        "INSERT INTO journal(ts, op, payload, inverse) VALUES(?,?,?,?)",
        (now_iso(), op, json.dumps(payload), json.dumps(inverse)),
    )
    return cur.lastrowid


def entries(conn: sqlite3.Connection, limit: int = 50, include_undone: bool = False
            ) -> list[sqlite3.Row]:
    where = "" if include_undone else "WHERE undone = 0"
    return conn.execute(
        f"SELECT * FROM journal {where} ORDER BY seq DESC LIMIT ?", (limit,)
    ).fetchall()


def undo(conn: sqlite3.Connection, seq: int | None = None,
         handlers: dict[str, Handler] | None = None) -> dict | None:
    """Undo one operation -- the most recent by default, or any specific `seq`.

    Returns the inverse record that was applied, or None if there was nothing
    to undo. `handlers` are the ones belonging to this vault: with two profiles
    open, a shared set would apply one profile's undo to the other's files.
    """
    if seq is None:
        row = conn.execute(
            "SELECT * FROM journal WHERE undone = 0 ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM journal WHERE seq = ? AND undone = 0", (seq,)
        ).fetchone()
    if row is None:
        return None

    inverse = json.loads(row["inverse"])
    run(inverse, handlers)
    conn.execute("UPDATE journal SET undone = 1 WHERE seq = ?", (row["seq"],))
    return inverse


def run(inverse: dict, handlers: dict[str, Handler] | None = None) -> None:
    """Apply one inverse record through its handler."""
    op = inverse.get("op")
    handler = (_HANDLERS if handlers is None else handlers).get(op)
    if handler is None:
        raise KeyError(f"no undo handler registered for op {op!r}")
    handler(inverse)
