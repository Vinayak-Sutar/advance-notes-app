"""To-do items inside a note's Markdown, and the small tokens they carry.

A to-do list is ordinary Markdown, so the file stays readable in Notepad,
Obsidian or anything else:

    ## Maths 📅 2026-11-10
    - [ ] Probability ⏰ 2026-11-05 18:00
    - [ ] Optics 🔁 every week
    - [x] Calculus ✅ 2026-10-27

The tokens follow the Obsidian Tasks style: 📅 a date it's due, ⏰ when to
remind, 🔁 how it repeats, ✅ the day it was ticked. Headings are sections.

Everything here is text in, text out -- no database -- so it is easy to test
and safe to call from anywhere.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

CHECK_RE = re.compile(r"^(\s*)([-*+])\s+\[([ xX])\](?:\s+(.*))?$")
HEAD_RE = re.compile(r"^(#{1,3})\s+(.*)$")
FENCE_RE = re.compile(r"^\s*```")
# A token runs from its symbol to the next symbol or the end of the line. The
# optional U+FE0F is the "show as emoji" selector some keyboards add.
TOKEN_RE = re.compile("\\s*([📅⏰✅🔁])\ufe0f?\\s*([^📅⏰✅🔁]*)")
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2}))?")


@dataclass
class Item:
    line: int                    # index into body.split("\n")
    indent: str
    text: str                    # without the tokens
    done: bool
    due: str | None = None       # "2026-11-05"
    remind: str | None = None    # "2026-11-05T18:00:00"
    repeat: str | None = None    # "every week"
    done_on: str | None = None   # "2026-10-27"
    section: int | None = None   # index into Parsed.sections


@dataclass
class Section:
    line: int
    level: int
    title: str
    due: str | None = None
    items: list[int] = field(default_factory=list)   # indexes into Parsed.items

    def progress(self, items: list[Item]) -> tuple[int, int]:
        mine = [items[i] for i in self.items]
        return sum(1 for it in mine if it.done), len(mine)


@dataclass
class Parsed:
    sections: list[Section]
    items: list[Item]

    def progress(self) -> tuple[int, int]:
        return sum(1 for it in self.items if it.done), len(self.items)


# ----------------------------------------------------------------- tokens ----
def split_tokens(content: str) -> tuple[str, dict]:
    """'Probability ⏰ 2026-11-05 18:00' -> ('Probability', {'remind': ...})."""
    first = TOKEN_RE.search(content)
    text = content[:first.start()] if first else content
    found: dict = {}
    for m in TOKEN_RE.finditer(content):
        symbol, value = m.group(1), m.group(2).strip()
        if symbol == "🔁":
            if value:
                found["repeat"] = value
            continue
        d = _DATE_RE.match(value)
        if not d:
            continue
        if symbol == "⏰":
            hour, minute = (int(d.group(2)), int(d.group(3))) if d.group(2) else (9, 0)
            found["remind"] = f"{d.group(1)}T{hour:02d}:{minute:02d}:00"
        elif symbol == "📅":
            found["due"] = d.group(1)
        else:
            found["done_on"] = d.group(1)
    return text.strip(), found


def strip_tokens(content: str) -> str:
    return split_tokens(content)[0]


def tokens_text(due: str | None = None, remind: str | None = None,
                repeat: str | None = None, done_on: str | None = None) -> str:
    out = []
    if due:
        out.append(f"📅 {due[:10]}")
    if remind:
        out.append(f"⏰ {remind[:10]} {remind[11:16]}")
    if repeat:
        out.append(f"🔁 {repeat}")
    if done_on:
        out.append(f"✅ {done_on[:10]}")
    return (" " + " ".join(out)) if out else ""


def format_item(item: Item) -> str:
    return (f"{item.indent}- [{'x' if item.done else ' '}] {item.text}"
            + tokens_text(item.due, item.remind, item.repeat, item.done_on)).rstrip()


# ------------------------------------------------------------------ parse ----
def parse(body: str) -> Parsed:
    sections: list[Section] = []
    items: list[Item] = []
    in_code = False
    for i, line in enumerate((body or "").split("\n")):
        if FENCE_RE.match(line):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = CHECK_RE.match(line)
        if m:
            text, tok = split_tokens(m.group(4) or "")
            items.append(Item(line=i, indent=m.group(1), text=text,
                              done=m.group(3) != " ",
                              section=len(sections) - 1 if sections else None, **tok))
            if sections:
                sections[-1].items.append(len(items) - 1)
            continue
        h = HEAD_RE.match(line)
        if h:
            title, tok = split_tokens(h.group(2))
            sections.append(Section(line=i, level=len(h.group(1)), title=title,
                                    due=tok.get("due")))
    return Parsed(sections, items)


def progress(body: str) -> tuple[int, int]:
    return parse(body).progress()


# ------------------------------------------------------------------ edits ----
def find(body: str, line: int | None, text: str) -> Item:
    """The item at `line`, if it still says `text`; otherwise the one item that
    says `text`. A tick from a page that's a moment out of date must never
    land on the wrong line."""
    items = parse(body).items
    want = strip_tokens(text or "").strip()
    for it in items:
        if it.line == line and (not want or it.text == want):
            return it
    same = [it for it in items if it.text == want]
    if len(same) == 1:
        return same[0]
    raise LookupError(f"no to-do item says {text!r}")


def _replace(body: str, item: Item) -> str:
    lines = body.split("\n")
    lines[item.line] = format_item(item)
    return "\n".join(lines)


def toggle(body: str, line: int | None, text: str, done: bool | None = None,
           today: date | None = None) -> str:
    it = find(body, line, text)
    it.done = (not it.done) if done is None else bool(done)
    it.done_on = (today or date.today()).isoformat() if it.done else None
    return _replace(body, it)


def set_when(body: str, line: int | None, text: str, *, remind: str | None = None,
             due: str | None = None, repeat: str | None = None) -> str:
    """Replace an item's date, reminder and repeat (None clears each one)."""
    it = find(body, line, text)
    it.remind, it.due, it.repeat = remind, due, repeat
    return _replace(body, it)


def reset(body: str) -> str:
    """Untick everything: a repeating list starting its next round."""
    lines = body.split("\n")
    for it in parse(body).items:
        if it.done:
            it.done, it.done_on = False, None
            lines[it.line] = format_item(it)
    return "\n".join(lines)


def add_item(body: str, text: str, section: str | None = None,
             remind: str | None = None, due: str | None = None,
             repeat: str | None = None) -> str:
    """Add '- [ ] text' after the last item of `section` (or of the list)."""
    new = format_item(Item(line=-1, indent="", text=text.strip(), done=False,
                           remind=remind, due=due, repeat=repeat))
    lines = (body or "").rstrip("\n").split("\n") if (body or "").strip() else []
    parsed = parse("\n".join(lines))
    target = None
    if section:
        key = section.strip().lower()
        target = next((s for s in parsed.sections if s.title.lower() == key), None)
    if target is not None:
        last = parsed.items[target.items[-1]].line if target.items else target.line
        lines.insert(last + 1, new)
    elif parsed.items and not section:
        lines.insert(parsed.items[-1].line + 1, new)
    else:
        lines.append(new)
    return "\n".join(lines)


# -------------------------------------------------------------- meaning ----
def semantic_sig(title: str, body: str) -> str:
    """A fingerprint of what a note *says*, ignoring line order, ticks and
    dates. Two bodies with the same fingerprint mean the same thing, so a tick
    or a drag doesn't need the note re-embedded."""
    norm = []
    for line in (body or "").split("\n"):
        m = CHECK_RE.match(line)
        if m:
            line = m.group(1) + "- " + strip_tokens(m.group(4) or "")
        else:
            h = HEAD_RE.match(line)
            if h:
                line = h.group(1) + " " + strip_tokens(h.group(2))
        line = " ".join(line.split())
        if line:
            norm.append(line)
    raw = (title or "").strip() + "\n" + "\n".join(sorted(norm))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
