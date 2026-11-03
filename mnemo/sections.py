"""The machine-written parts of image and link notes.

An image note's file holds three things: what the user typed, what the vision
model saw, and the text it read off the picture. A link note holds the user's
comment followed by the page it fetched. All of it stays in the one Markdown
body, because that body is what gets searched and embedded -- the picture and
the page are otherwise invisible to the rest of the system.

The note view shows those parts separately, and only the user's own words are
editable. These helpers split a stored body into its parts and join an edit
back without losing the machine-written ones.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DESC_MARK = "**What's in the picture**"
OCR_MARK = "**Text in image**"

# A capture with no vision model available stores this placeholder instead.
_PLACEHOLDER = re.compile(r"^\(image: [^)\n]*\)$")

# The fetched part of a link note starts with "[site] page title". The site
# is empty or at least two characters, so a task line like "[x] done" or
# "[ ] milk" is never mistaken for it.
_LINK_HEADER = re.compile(r"^\[(?:[^\]\n]{2,})?\] \S")

SUMMARY_CHARS = 400


@dataclass
class ImageParts:
    user: str
    description: str
    ocr: str


@dataclass
class LinkParts:
    user: str
    fetched: str          # "[site] title" plus the page text, kept verbatim
    summary: str | None


def _split_at(body: str, mark: str) -> tuple[str, str] | None:
    """Split around a line that is exactly `mark`, or None if absent."""
    m = re.search(rf"(?m)^{re.escape(mark)}[ \t]*$", body)
    if m is None:
        return None
    return body[:m.start()].strip(), body[m.end():].strip()


def split_image(body: str) -> ImageParts:
    body = body or ""
    ocr = ""
    pre = body.strip()
    found = _split_at(pre, OCR_MARK)
    if found:
        pre, ocr = found
    found = _split_at(pre, DESC_MARK)
    if found:
        user, description = found
    else:
        # Notes saved before the description had its own heading: everything
        # above the transcript was written by the vision model.
        user, description = "", pre
    if _PLACEHOLDER.match(description):
        description = ""
    return ImageParts(user=user, description=description, ocr=ocr)


def join_image(user: str, description: str, ocr: str) -> str:
    parts = [(user or "").strip()]
    if (description or "").strip():
        parts.append(f"{DESC_MARK}\n\n{description.strip()}")
    if (ocr or "").strip():
        parts.append(f"{OCR_MARK}\n\n{ocr.strip()}")
    return "\n\n".join(p for p in parts if p)


def split_link(body: str) -> LinkParts:
    blocks = re.split(r"\n\s*\n", (body or "").strip())
    for i, block in enumerate(blocks):
        if _LINK_HEADER.match(block.strip()):
            user = "\n\n".join(blocks[:i]).strip()
            fetched = "\n\n".join(blocks[i:]).strip()
            after = [b.strip() for b in blocks[i + 1:] if b.strip()]
            return LinkParts(user=user, fetched=fetched,
                             summary=_shorten(after[0]) if after else None)
    return LinkParts(user=(body or "").strip(), fetched="", summary=None)


def join_link(user: str, fetched: str) -> str:
    return "\n\n".join(p for p in ((user or "").strip(), (fetched or "").strip()) if p)


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= SUMMARY_CHARS:
        return text
    cut = text[:SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"
