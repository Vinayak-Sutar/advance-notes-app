"""Personal notes: show what's in a profile's _personal folder on the page.

The content is prepared and updated outside the app, by
tools/personal_parse.py and tools/personal_previews.py plus Claude reading the
result, and written to data/. There is no parsing and no AI here, and the page
can't change any of it. When the engine takes this over it writes the same
files, and nothing here changes.

    data/<source>.json        messages and moments (private text already removed)
    data/<source>.notes.json  summary, key points, themes, titles, notes on links
    data/links.json           one entry per link, with its preview
    data/lists.json           books, movies and other lists
    private/private.json      hidden text; only `reveal` ever reads it
    layout.json               how you arranged the link tiles; processing leaves it alone
"""
from __future__ import annotations

import json
from pathlib import Path

# Opened in the browser. Anything else downloads, so a saved .html or .svg can
# never run as if it were part of this app.
INLINE_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif",
                ".mp4": "video/mp4", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
                ".txt": "text/plain; charset=utf-8"}


class Personal:
    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).resolve() / "_personal"
        self.data = self.root / "data"

    def _read(self, path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def sources(self) -> list[str]:
        return sorted(p.stem for p in self.data.glob("*.json")
                      if not p.name.endswith(".notes.json") and p.stem not in ("links", "lists"))

    def _source(self, source: str) -> dict | None:
        if source not in self.sources():
            return None
        return self._read(self.data / f"{source}.json", None)

    def overview(self) -> dict:
        """Everything except the messages themselves."""
        sources, notes_by = [], {}
        for key in self.sources():
            data = self._source(key)
            if not data:
                continue
            notes = self._read(self.data / f"{key}.notes.json", {})
            notes_by[key] = notes
            sources.append({
                "source": key, "name": data.get("name", key), "from": data.get("from"),
                "start": data.get("start"), "end": data.get("end"),
                "counts": data.get("counts", {}), "months": data.get("months", {}),
                "about": notes.get("about"), "summary": notes.get("summary"),
                "points": notes.get("points", []), "themes": notes.get("themes", []),
            })
        return {"sources": sources, "links": self._links(notes_by),
                "lists": self._read(self.data / "lists.json", {}), "layout": self.layout()}

    def _links(self, notes_by: dict) -> list[dict]:
        links = self._read(self.data / "links.json", {})
        out = []
        for lid, link in links.items():
            your_notes, starred = [], False
            for notes in notes_by.values():
                your_notes += notes.get("linkNotes", {}).get(lid, [])
                starred = starred or lid in notes.get("linkStars", [])
            saves = sorted(link.get("saves", []), key=lambda s: s.get("ts", ""))
            out.append({**link, "saves": saves, "notes": your_notes, "star": starred,
                        "done": bool(link.get("done"))})
        out.sort(key=lambda l: (l["saves"][-1]["ts"] if l["saves"] else ""), reverse=True)
        return out

    def messages(self, source: str) -> dict | None:
        data = self._source(source)
        if data is None:
            return None
        notes = self._read(self.data / f"{source}.notes.json", {})
        return {"source": source, "name": data.get("name", source),
                "messages": data.get("messages", []), "moments": data.get("moments", []),
                "momentTitles": notes.get("momentTitles", {})}

    def layout(self) -> dict:
        return self._read(self.root / "layout.json", {})

    def set_layout(self, key: str, order: list) -> dict:
        """Remember how you arranged the link tiles of one view: "all" or a
        source. This is the page's only write: it holds tile positions, never
        any of the content, and an empty order goes back to newest first."""
        if key != "all" and key not in self.sources():
            raise ValueError("no such view")
        known = self._read(self.data / "links.json", {})
        clean = list(dict.fromkeys(i for i in order if i in known))
        layout = self.layout()
        if clean:
            layout[key] = clean
        else:
            layout.pop(key, None)
        tmp = self.root / "layout.json.tmp"
        tmp.write_text(json.dumps(layout, indent=0), encoding="utf-8")
        tmp.replace(self.root / "layout.json")
        return {"ok": True}

    def reveal(self, message_id: str) -> str | None:
        """The one way private text leaves its file: you pressed Show."""
        return self._read(self.root / "private" / "private.json", {}).get(message_id)

    def file(self, source: str, name: str) -> tuple[bytes, str, bool] | None:
        """(bytes, type, open in the browser?) for an attachment or a thumbnail.
        Only names directly inside the allowed folder can be reached."""
        if source == "_thumbs":
            folder = self.root / "thumbs"
        elif source in self.sources():
            folder = self.root / "sources" / source / "files"
        else:
            return None
        folder = folder.resolve()
        target = (folder / name).resolve()
        if target.parent != folder or not target.is_file():
            return None
        body = target.read_bytes()
        ctype = INLINE_TYPES.get(target.suffix.lower())
        if body[:5] == b"%PDF-":                # WhatsApp drops some PDFs' extension
            ctype = "application/pdf"
        return body, ctype or "application/octet-stream", ctype is not None
