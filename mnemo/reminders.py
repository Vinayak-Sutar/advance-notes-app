"""What a to-do list should say about its deadline, and when anything should speak up.

`pace()` turns "5 of 12 done, due 15 Nov" into something you can act on today:
how many a day keep you on track, and -- gently -- whether you're behind.
`due_now()` decides what speaks up now: single reminders, to-do items, a
list's daily check-in and its deadline. The wording lives in the page; this
returns the facts.
"""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime

from . import checklist as CL
from . import notes as N


def _day(value) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")[:19]).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def pace(done: int, total: int, due=None, start=None, now: datetime | None = None) -> dict:
    """{"state": empty | done | open | on_track | behind | late, ...}.

    `daysLeft` counts today, so a list due today has one day left. Being
    "behind" means fewer done than an even spread from `start` would have by
    now -- with a one-item allowance, so a single slow day doesn't nag.
    """
    left = max(0, total - done)
    if total == 0:
        return {"state": "empty", "left": 0}
    if left == 0:
        return {"state": "done", "left": 0}
    due_day = _day(due)
    if due_day is None:
        return {"state": "open", "left": left}
    today = (now or datetime.now()).date()
    days_left = (due_day - today).days + 1
    if days_left <= 0:
        return {"state": "late", "left": left, "daysLeft": days_left - 1}
    per_day = math.ceil(left / days_left)
    start_day = _day(start) or today
    span = max(1, (due_day - start_day).days + 1)
    elapsed = max(0, (today - start_day).days)
    expected = total * min(1.0, elapsed / span)
    state = "behind" if done + 1 < expected else "on_track"
    return {"state": state, "left": left, "daysLeft": days_left, "perDay": per_day}


# ------------------------------------------------------- what speaks up ----
# Each thing that can speak up has a key, kept in reminder_log once it has been
# shown, so it fires exactly once:
#     task:<note>:<time>              a single reminder (also marked on the note)
#     item:<note>:<time>:<text>       a to-do item's ⏰
#     checkin:<list>:<day>            a list's daily check-in
#     deadline-soon:<list>:<due>      the day before a list's deadline
#     deadline-missed:<list>:<due>    once, when it has passed
DEFAULT_TIME = "09:00"


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%S")


def _clock(day: date, hhmm: str | None) -> datetime | None:
    try:
        hour, minute = (int(x) for x in str(hhmm or "").split(":")[:2])
        return datetime(day.year, day.month, day.day, hour, minute)
    except (ValueError, TypeError):
        return None


def _short(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _quiet(row, today: date) -> bool:
    """Paused with "don't remind me about … until …"."""
    until = _day(row["quiet_until"]) if row["quiet_until"] else None
    return until is not None and today < until


def fired(conn) -> set[str]:
    return {r["key"] for r in conn.execute("SELECT key FROM reminder_log")}


def mark(conn, keys) -> None:
    """These have been shown. A single reminder is marked on its note too."""
    stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    for key in keys:
        key = str(key)[:300]
        conn.execute("INSERT OR IGNORE INTO reminder_log(key, fired_at) VALUES(?, ?)", (key, stamp))
        if key.startswith("task:"):
            N.mark_reminded(conn, key.split(":")[1])


def list_facts(conn, row, now: datetime) -> dict:
    """What a list's check-in, Today row and alert say about it. A group
    counts the items of the lists inside it too."""
    parsed = CL.parse(row["body"] or "")
    done, total = parsed.progress()
    for kid in conn.execute(
            "SELECT n.body FROM note_meta m JOIN notes n ON n.id = m.note_id WHERE m.parent = ?",
            (row["id"],)):
        d, t = CL.progress(kid["body"] or "")
        done, total = done + d, total + t
    nxt = next((it.text for it in parsed.items if not it.done), None)
    return {"noteId": row["id"], "title": row["title"], "done": done, "total": total,
            "due": row["due_at"], "next": nxt, "checkin": row["checkin"],
            "quietUntil": row["quiet_until"],
            "pace": pace(done, total, row["due_at"], row["created_at"], now)}


def due_now(conn, now: datetime | None = None) -> list[dict]:
    """Everything that should speak up now and hasn't yet."""
    now = now or datetime.now()
    today, stamp, seen, out = now.date(), _stamp(now), fired(conn), []
    lists = {row["id"]: row for row in N.lists(conn)}

    for row in N.due_soon(conn, stamp):
        out.append({"type": "task", "key": f"task:{row['id']}:{row['remind_at']}",
                    "noteId": row["id"], "title": row["title"], "when": row["remind_at"]})

    for row in conn.execute("SELECT * FROM notes WHERE body LIKE '%⏰%' AND status <> 'archived'"):
        if row["id"] in lists and _quiet(lists[row["id"]], today):
            continue
        for it in CL.parse(row["body"] or "").items:
            if it.done or not it.remind or it.remind > stamp:
                continue
            key = f"item:{row['id']}:{it.remind}:{_short(it.text)}"
            if key not in seen:
                out.append({"type": "item", "key": key, "noteId": row["id"],
                            "listTitle": row["title"], "text": it.text, "line": it.line,
                            "when": it.remind, "day": it.due, "repeat": it.repeat})

    for row in lists.values():
        # A list inside a group speaks through its group.
        if row["status"] == "archived" or _quiet(row, today) or row["parent"] in lists:
            continue
        facts = list_facts(conn, row, now)
        if facts["total"] == 0 or facts["done"] == facts["total"]:
            continue                                    # nothing to do, nothing to say
        due = datetime.fromisoformat(row["due_at"][:19]) if row["due_at"] else None
        if due is not None and now > due:
            key = f"deadline-missed:{row['id']}:{row['due_at']}"
            if key not in seen:
                out.append({"type": "deadline", "key": key, "late": True, **facts})
            continue                                    # no daily nagging once it's late
        if row["checkin"]:
            at = _clock(today, row["checkin"])
            key = f"checkin:{row['id']}:{today.isoformat()}"
            if at is not None and now >= at and key not in seen:
                out.append({"type": "checkin", "key": key, **facts})
        elif due is not None and (due.date() - today).days == 1:
            key = f"deadline-soon:{row['id']}:{row['due_at']}"
            if now >= _clock(today, DEFAULT_TIME) and key not in seen:
                out.append({"type": "deadline", "key": key, "late": False, **facts})
    return out


def agenda_items(conn, now: datetime | None = None) -> list[dict]:
    """To-do items with a date or a reminder, for Today and Reminders. Done ones
    only if they were ticked today."""
    today = (now or datetime.now()).date().isoformat()
    out = []
    for row in conn.execute(
            "SELECT id, title, body FROM notes WHERE (body LIKE '%⏰%' OR body LIKE '%📅%')"
            " AND status <> 'archived'"):
        for it in CL.parse(row["body"] or "").items:
            if not (it.remind or it.due) or (it.done and it.done_on != today):
                continue
            out.append({"noteId": row["id"], "listTitle": row["title"], "line": it.line,
                        "text": it.text, "done": it.done, "remind": it.remind, "day": it.due,
                        "repeat": it.repeat})
    return out


def active_lists(conn, now: datetime | None = None) -> list[dict]:
    """Open lists with a deadline or a check-in, for Today: how each is going."""
    now = now or datetime.now()
    out = []
    lists = {row["id"]: row for row in N.lists(conn)}
    for row in lists.values():
        if row["status"] == "archived" or row["parent"] in lists or _quiet(row, now.date()):
            continue
        if not (row["due_at"] or row["checkin"]):
            continue
        facts = list_facts(conn, row, now)
        if facts["total"] and facts["done"] < facts["total"]:
            out.append(facts)
    return sorted(out, key=lambda f: f["due"] or "9999")


def mark_past(conn, now: datetime | None = None) -> int:
    """After a rebuild the log is empty: don't fire everything already past again."""
    keys = [r["key"] for r in due_now(conn, now)]
    mark(conn, keys)
    return len(keys)
