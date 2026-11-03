"""What a to-do list should say about its deadline, and when it should speak up.

`pace()` turns "5 of 12 done, due 15 Nov" into something you can act on today:
how many a day keep you on track, and -- gently -- whether you're behind.
The wording lives in the page; this returns the facts.
"""
from __future__ import annotations

import math
from datetime import date, datetime


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
