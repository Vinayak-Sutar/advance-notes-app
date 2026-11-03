"""The engine: one capture call, everything else falls out of it.

Ordering matters here. The note is written to disk and indexed BEFORE routing
is allowed to fail, so a capture can never be lost because the model was down,
slow, or wrong. Routing then moves an already-safe note into place.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import gardener as Gd
from . import gate as G
from . import journal as J
from . import notes as N
from . import router as R
from . import sections as Sec
from . import topics as T
from . import checklist as CL
from .checklist import semantic_sig
from .config import Config, for_embedder
from .db import connect, local_now_iso, now_iso, slugify, transaction, ulid
from .llm import OfflineRouter, Router, name_from_texts
from .vault import Note, Vault
from .vecs import Embedder, HashingEmbedder, top_k

# Two forms, because "where does the topic name end" is genuinely ambiguous in
# free text:  @gaming  or  @Gaming/Elden-Ring  (single token), and
# @"Gaming/Elden Ring"  when the path contains spaces. A bare token that names
# an existing topic is also extended greedily -- see Engine._resolve_steer.
STEER_RE = re.compile(r'(?:^|\s)@(?:"([^"]+)"|([A-Za-z0-9][\w/-]*))')
TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9][\w-]*)")
TASK_RE = re.compile(r"(?:^|\s)!(task|todo)\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://\S+")
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")
# Header fields a schedule / done / list-setting change may touch, all undone together.
RESTATE_FIELDS = ("kind", "status", "due", "remind", "repeat", "checkin",
                  "quiet_until", "parent", "order")


@dataclass
class CaptureResult:
    note: Note
    path: str
    outcome: str                       # filed | review | inbox
    topic_path: str | None = None
    confidence: float = 0.0
    related: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    steered: bool = False
    appended_to: str | None = None
    append_candidates: list = field(default_factory=list)
    due_at: str | None = None
    reminder: bool = False
    recurrence: str | None = None
    matched: list = field(default_factory=list)
    ocr: str = ""
    description: str = ""
    enrich_error: str = ""
    bootstrapped: list[str] = field(default_factory=list)
    journal_seq: int | None = None
    routing: R.RoutingOutcome | None = None
    paused_until: str | None = None    # "don't remind me about X until..." was applied
    list_items: int = 0                # a new to-do list was made with this many items

    def summary(self) -> str:
        """The one line the capture toast shows."""
        where = self.topic_path or "Inbox"
        bits = [f"-> {where}"]
        if self.steered:
            bits.append("(you chose)")
        if self.related:
            bits.append(f"linked to {len(self.related)}")
        if self.outcome == R.REVIEW:
            bits.append("needs review")
        return "  ".join(bits)


def parse_input(raw: str) -> dict:
    """Read a capture into directives.

    Delegates to `intent.parse`, which understands spoken phrasing as well as
    the typed `@topic` / `#tag` / `!task` symbols. Voice is the primary input
    here and cannot produce those symbols, so plain language has to work.
    """
    from . import intent as I

    parsed = I.parse(raw)
    return {
        "text": parsed.text,
        "topic": parsed.topic,
        "append_to": parsed.append_to,
        "tags": parsed.tags,
        "kind": parsed.kind,
        "url": parsed.url,
        "due_at": parsed.due_at,
        "remind": parsed.remind,
        "recurrence": parsed.recurrence,
        "matched": parsed.matched,
        "list_title": parsed.list_title,
        "list_items": parsed.list_items,
        "append_item": parsed.append_item,
        "pause_target": parsed.pause_target,
        "pause_until": parsed.pause_until,
    }


def _legacy_parse_input(raw: str) -> dict:
    """Pull steering directives out of the single input channel.

    '@gaming this build is broken #pc !task' -> topic 'gaming', tag 'pc',
    kind 'task', text 'this build is broken'.
    """
    steer = STEER_RE.search(raw)
    topic = ((steer.group(1) or steer.group(2)).strip() if steer else None)
    tags = TAG_RE.findall(raw)
    is_task = bool(TASK_RE.search(raw))

    text = STEER_RE.sub(" ", raw)
    text = TAG_RE.sub(" ", text)
    text = TASK_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()

    urls = URL_RE.findall(raw)
    if is_task:
        kind = "task"
    elif urls:
        kind = "video" if any(h in urls[0] for h in VIDEO_HOSTS) else "link"
    else:
        kind = "note"

    return {"text": text, "topic": topic, "tags": tags, "kind": kind,
            "url": urls[0] if urls else None}


def build_stack(online: bool | None = None):
    """Pick the embedder/router pair to use.

    Tries the bundled local models and silently falls back to the offline
    stack if they are not installed or the server will not start. Nothing in
    the app requires a model to be present -- the offline path is a real
    fallback, not a stub.
    """
    from .config import OFFLINE, ONLINE

    if online is not False:
        try:
            from . import models, runtime
            from .llm import OllamaRouter
            from .vecs import OllamaEmbedder

            if runtime.start(wait=30) and runtime.has_model(models.EMBED_MODEL):
                models.refresh()        # the quality profile picks the brain
                embedder = OllamaEmbedder()
                router = (OllamaRouter() if runtime.has_model(models.ROUTER_MODEL)
                          else OfflineRouter(file_floor=ONLINE.file_floor,
                                             min_margin=ONLINE.min_margin))
                return embedder, router, ONLINE
        except Exception:
            pass
        if online is True:
            raise RuntimeError(
                "local models unavailable; run: python -m mnemo.setup")

    embedder = HashingEmbedder()
    return embedder, OfflineRouter(file_floor=OFFLINE.file_floor,
                                   min_margin=OFFLINE.min_margin), OFFLINE


class Engine:
    def __init__(self, vault_root: str | Path, embedder: Embedder | None = None,
                 provider: Router | None = None, cfg: Config | None = None,
                 online: bool | None = False, enrich: bool = True):
        self.vault = Vault(vault_root)
        self.db_path = self.vault.root / ".mnemo" / "index.db"
        self.conn: sqlite3.Connection = connect(self.db_path)

        if embedder is None and provider is None and cfg is None:
            embedder, provider, cfg = build_stack(online)

        self.embedder = embedder or HashingEmbedder()
        self.cfg = cfg or for_embedder(self.embedder)
        self.provider = provider or OfflineRouter(
            file_floor=self.cfg.file_floor, min_margin=self.cfg.min_margin
        )
        # Fetching a pasted link is what makes it route as well as a typed
        # note; disable for tests and for fully air-gapped use.
        self.enrich = enrich
        self.embedder_changed = self._check_embedder()
        self._register_undo()
        self._times_into_files()

    def _times_into_files(self) -> None:
        """One-off: older versions kept due times, reminders and "done" only in
        the index. Copy them into the files, which are the truth from now on."""
        if self.conn.execute(
                "SELECT 1 FROM meta WHERE key = 'times_in_files'").fetchone():
            return
        rows = self.conn.execute(
            "SELECT * FROM notes WHERE path IS NOT NULL AND (due_at IS NOT NULL"
            " OR remind_at IS NOT NULL OR status <> 'open' OR kind = 'task')").fetchall()
        with transaction(self.conn):
            for row in rows:
                try:
                    note = self.vault.read(row["path"])
                except (OSError, ValueError):
                    continue
                if note is None:
                    continue
                wanted = {"due": row["due_at"], "remind": row["remind_at"],
                          "status": row["status"], "kind": row["kind"]}
                if all(getattr(note, k) == v for k, v in wanted.items()):
                    continue
                note.due = note.due or row["due_at"]
                note.remind = note.remind or row["remind_at"]
                note.status, note.kind = row["status"], row["kind"]
                self.vault.write(note, row["path"])
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('times_in_files', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value")

    def _check_embedder(self) -> str | None:
        """Detect that the vault was indexed with a different embedder.

        Vectors from two different models share no geometry, so silently mixing
        them would corrupt every similarity comparison -- routing, dedupe,
        related links, the lot. Record which embedder owns the index and report
        a mismatch so `rebuild()` can re-embed everything.
        """
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'embedder'").fetchone()
        previous = row["value"] if row else None
        if previous and previous != self.embedder.name:
            has_vectors = self.conn.execute(
                "SELECT 1 FROM note_vec LIMIT 1").fetchone() is not None
            if has_vectors:
                return previous
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('embedder', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (self.embedder.name,))
        return None

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------- enriching --
    def _enrich(self, parsed: dict, text: str) -> tuple[str, str, str, dict]:
        """Expand a link or image into text worth routing on.

        Returns (routing_text, title, kind, extra). A bare URL carries almost no
        signal, so without this a pasted link routes on the characters in its
        domain. Failures are non-fatal: we fall back to the raw text.
        """
        if not self.enrich:
            return text, "", parsed["kind"], {}

        url = parsed.get("url")
        if not url:
            return text, "", parsed["kind"], {}

        from . import enrich as E

        found = E.fetch_url(url)
        if not found.ok:
            return text, "", parsed["kind"], {"enrich_error": found.error}

        # The user's own words still lead; fetched content is context after it.
        commentary = text.replace(url, "").strip(" -–—:")
        body = "\n\n".join(part for part in (
            commentary,
            f"[{found.site}] {found.title}" if found.title else "",
            found.text,
        ) if part)
        return (body, found.title or "", found.kind,
                {"site": found.site, "author": found.author})

    # ------------------------------------------------------------ capture --
    def capture_image(self, image_path: str | Path, note_text: str = "",
                      auto_file: bool = True) -> CaptureResult:
        """Capture a pasted or dropped image.

        The file is copied into the vault (content-addressed, so the same
        screenshot pasted twice is stored once) and a vision model describes it.
        That description is what gets embedded and routed -- the image itself is
        opaque to every other part of the system.
        """
        import hashlib
        import shutil

        from . import enrich as E

        source = Path(image_path)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        media_rel = f".mnemo/media/{digest}{source.suffix.lower()}"
        media_abs = self.vault.abs_path(media_rel)
        media_abs.parent.mkdir(parents=True, exist_ok=True)
        if not media_abs.exists():
            shutil.copy2(source, media_abs)

        read = E.read_image(media_abs) if self.enrich else E.Enrichment(kind="image")
        described = read.ok

        # Each machine-written part gets its own heading so the note view can
        # show them apart from what the user typed (see sections.py).
        body = ""
        if described:
            body = Sec.join_image(note_text, read.meta.get("description", ""),
                                  read.meta.get("transcript", ""))
        if not body or body == note_text.strip():
            body = "\n\n".join(p for p in (note_text.strip(),
                                           f"(image: {source.name})") if p)

        title = (note_text.strip().splitlines()[0] if note_text.strip()
                 else (read.title if described else source.stem))

        result = self.capture(body, title=title[:70] or source.stem, kind="image",
                              media=media_rel,
                              note_error=None if described else read.error,
                              intent_text=note_text, auto_file=auto_file)
        result.ocr = read.meta.get("transcript", "") if described else ""
        result.description = read.meta.get("description", "") if described else ""
        result.enrich_error = read.error
        return result

    def capture(self, raw: str, title: str | None = None, kind: str | None = None,
                media: str | None = None, note_error: str | None = None,
                intent_text: str | None = None, auto_file: bool = True) -> CaptureResult:
        # `intent_text` separates instruction from content. For an image, the
        # user's own words are the instruction and the OCR transcript is
        # content -- scanning the transcript for directives finds times inside
        # IP addresses and topics inside UI labels.
        parsed = parse_input(intent_text if intent_text is not None else raw)
        text = raw.strip() if intent_text is not None else (parsed["text"] or raw.strip())

        if intent_text is None and not media:
            handled = self._list_instruction(parsed, text)
            if handled is not None:
                return handled
        if parsed.get("list_title") and intent_text is None:
            # "exam prep list: algebra, calculus by 15 nov" is a to-do list:
            # filed like any note, but its kind and title are what you said.
            title, kind = title or parsed["list_title"], "list"

        enriched_text, fetched_title, detected_kind, extra = self._enrich(parsed, text)
        text = enriched_text
        note = Note(
            title=(title or fetched_title or self._derive_title(text)),
            body=text,
            kind=kind or detected_kind,
            status="open",
            tags=parsed["tags"],
            source=parsed["url"],
            media=media,
        )

        due = parsed.get("due_at")
        due_iso = due.strftime("%Y-%m-%dT%H:%M:%S") if due else None
        if due_iso and note.kind == "list":
            # A list's date is its deadline; it checks in daily rather than
            # ringing once.
            note.due, note.checkin = due_iso, self.checkin_default
        elif due_iso:
            # The time lives in the file, so it survives edits and a rebuild.
            note.due = due_iso
            note.remind = due_iso if parsed.get("remind", True) else None
            if note.kind == "note":
                note.kind = "task"

        vec = self.embedder.embed_one(text) if hasattr(self.embedder, "embed_one") \
            else self.embedder.embed([text])[0]

        # "add this to the X note" is a different action from filing a new one,
        # and the old symbol syntax had no way to express it at all.
        if parsed.get("append_to") and not media:
            matches = self.resolve_note(parsed["append_to"], vec)
            if matches:
                self.append_to_note(matches[0]["id"], text)
                row = N.get(self.conn, matches[0]["id"])
                return CaptureResult(
                    note=self.vault.read(row["path"]), path=row["path"],
                    outcome=R.FILED, topic_path=matches[0]["topic"],
                    confidence=1.0, appended_to=matches[0]["id"],
                    append_candidates=matches,
                    matched=parsed.get("matched") or [],
                )

        with transaction(self.conn):
            # 1. Land it somewhere safe FIRST. Routing cannot lose a note.
            path = self.vault.path_for(note, None)
            self.vault.write(note, path)
            N.upsert(self.conn, note, path)
            N.set_vector(self.conn, note.id, vec, self.embedder.name)

            # 2. If this note is the one that tips the vault over the
            #    bootstrap threshold, build the taxonomy BEFORE routing --
            #    otherwise routing invents a topic from this single note and
            #    the clustering never gets a chance to run.
            bootstrapped = self._maybe_bootstrap()
            seeded_topic = N.primary_topic(self.conn, note.id) if bootstrapped else None

            # 3. Decide where it belongs.
            steered = False
            if seeded_topic:
                outcome = R.RoutingOutcome(
                    outcome=R.REVIEW, topic_id=seeded_topic,
                    topic_path=T.path_of(self.conn, seeded_topic), confidence=0.5,
                )
            elif parsed["topic"]:
                # Explicit steering outranks everything, including cold start.
                # If the user names a topic, that is not a guess to be deferred.
                steer_path, text = self._resolve_steer(parsed["topic"], text)
                note.body = text
                topic_id = T.ensure_path(self.conn, steer_path, state="active")
                outcome = R.RoutingOutcome(
                    outcome=R.FILED, topic_id=topic_id,
                    topic_path=T.path_of(self.conn, topic_id), confidence=1.0,
                )
                steered = True
            elif self._in_cold_start() or not auto_file:
                # Nothing to retrieve against yet. Naming a topic from one
                # note's incidental wording produces junk like "Covering
                # Deduction", so hold notes in Inbox until there is enough
                # data to cluster properly. A user who turned automatic
                # filing off gets the same: everything waits in Inbox.
                outcome = R.RoutingOutcome(outcome=R.TO_INBOX)
            else:
                outcome = R.route(
                    self.conn, self.provider, self.cfg, vec, text,
                    title=note.title, kind_hint=note.kind,
                    embed_name=self._embed_one, exclude_note=note.id,
                    allow_new_topics=not self._warming_up(),
                )
                if outcome.decision:
                    # An explicitly passed kind (an image upload says "image")
                    # is a fact, not a guess -- the model does not get to
                    # relabel a screenshot as a task because it contains a
                    # button that says "Retry deployment".
                    if kind is None:
                        note.kind = outcome.decision.kind or note.kind
                        if note.due and note.kind == "note":
                            note.kind = "task"     # something with a time is a to-do
                    note.entities = outcome.decision.entities

            # 4. Place it.
            if outcome.topic_id and not seeded_topic:
                N.assign(self.conn, note.id, outcome.topic_id, is_primary=True,
                         confidence=outcome.confidence,
                         assigned_by="user" if steered else "llm")
                T.fold_into_centroid(self.conn, outcome.topic_id, vec)
                T.recount(self.conn, outcome.topic_id)
                note.topics = [T.path_of(self.conn, outcome.topic_id) or ""]

            if seeded_topic:
                # The bootstrap already wrote and moved this file; `path` is
                # stale and moving again would chase a file that is gone.
                row = N.get(self.conn, note.id)
                new_path = row["path"] if row and row["path"] else path
            else:
                new_path = self._place(note, outcome.topic_id, path)
                # Re-write the file: step 1 wrote it before routing, so the
                # frontmatter still has no topic. Without this the .md files
                # carry no filing information and `rebuild()` cannot restore
                # the taxonomy -- which is the whole basis for treating
                # index.db as a disposable cache.
                self.vault.write(note, new_path)
            N.upsert(self.conn, note, new_path,
                     needs_review=(outcome.outcome == R.REVIEW))
            R.log(self.conn, note.id, outcome)

            # 5. Auto-link semantically close notes.
            related = self._link_related(note.id, vec)

            # 6. A provisional topic may have just earned its folder.
            promoted = self._promote_and_relocate()

            seq = J.record(
                self.conn, "note.capture",
                {"note_id": note.id, "path": new_path, "topic_id": outcome.topic_id},
                {"op": "note.remove", "note_id": note.id, "path": new_path},
            )

        if seeded_topic:
            row = N.get(self.conn, note.id)
            new_path = row["path"] if row and row["path"] else new_path

        return CaptureResult(
            note=note, path=new_path, outcome=outcome.outcome,
            topic_path=outcome.topic_path, confidence=outcome.confidence,
            related=related, promoted=promoted, steered=steered,
            bootstrapped=bootstrapped, journal_seq=seq, routing=outcome,
            due_at=due_iso, reminder=bool(parsed.get("remind")),
            recurrence=parsed.get("recurrence"), matched=parsed.get("matched") or [],
            list_items=len(parsed.get("list_items") or []) if note.kind == "list" else 0,
        )

    # ------------------------------------------------------------ to-do lists --
    checkin_default = "09:00"     # the page sends the person's own morning time

    def _list_instruction(self, parsed: dict, text: str) -> CaptureResult | None:
        """Captures that change a to-do list instead of making a note:
        "add optics to the exam prep list [thursday 6pm]" and
        "don't remind me about exam prep until monday"."""
        if parsed.get("pause_target"):
            until = parsed["pause_until"].isoformat()
            target = self.pause_list(parsed["pause_target"], until)
            if target is None:
                return None
            row = N.get(self.conn, target)
            return CaptureResult(note=self.vault.read(row["path"]), path=row["path"],
                                 outcome=R.FILED,
                                 topic_path=T.path_of(self.conn, N.primary_topic(self.conn, target)),
                                 confidence=1.0, paused_until=until,
                                 matched=parsed.get("matched") or [])
        if parsed.get("append_item") and parsed.get("append_to"):
            due = parsed.get("due_at")
            remind = due.strftime("%Y-%m-%dT%H:%M:%S") if due else None
            target = self.add_to_named_list(parsed["append_to"], text, remind=remind)
            row = N.get(self.conn, target)
            return CaptureResult(note=self.vault.read(row["path"]), path=row["path"],
                                 outcome=R.FILED, confidence=1.0, appended_to=target,
                                 topic_path=T.path_of(self.conn, N.primary_topic(self.conn, target)),
                                 due_at=remind, reminder=bool(remind),
                                 matched=parsed.get("matched") or [])
        return None

    def add_to_named_list(self, name: str, item: str, remind: str | None = None) -> str:
        """Add an item to the list `name` names, making that list if there's
        none yet. Returns the list's id."""
        target = self.find_list(name)
        if target is None:
            from .intent import list_title
            target = self.create_note(title=list_title(name), body="", kind="list")["id"]
        item = item.strip()
        self.add_to_list(target, item[:1].upper() + item[1:], remind=remind)
        return target

    def pause_list(self, name: str, until: str) -> str | None:
        """No check-ins from the list `name` names until `until` (a date)."""
        target = self.find_list(name)
        if target is not None:
            self.set_list(target, quiet_until=until)
        return target

    def find_list(self, phrase: str) -> str | None:
        """The to-do list "exam prep" names: a list whose title has those words,
        else any note whose title does. Never a guess by meaning alone -- adding
        to the wrong note is worse than starting a new list."""
        rows = N.find_by_title(self.conn, phrase)
        for row in rows:
            if row["kind"] == "list":
                return row["id"]
        return rows[0]["id"] if rows else None

    def set_list(self, note_id: str, **fields) -> Note:
        """Change a list's header: due, checkin, quiet_until, repeat, parent,
        kind. One undoable step; None clears a field."""
        unknown = set(fields) - {"due", "checkin", "quiet_until", "repeat", "parent", "kind", "remind"}
        if unknown:
            raise ValueError(f"not a list setting: {', '.join(sorted(unknown))}")
        if fields.get("parent"):
            self._check_parent(note_id, fields["parent"])

        def change(note: Note) -> None:
            for key, value in fields.items():
                setattr(note, key, value if value not in ("",) else None)
            if note.kind != "list" and fields.get("kind") is None and (
                    fields.get("checkin") or fields.get("parent")):
                note.kind = "list"
            if "due" in fields and note.kind == "list" and fields["due"] and not note.checkin \
                    and "checkin" not in fields:
                note.checkin = self.checkin_default      # a deadline checks in daily
            note.kind = note.kind or "note"
        return self._rewrite(note_id, change, "list.settings")

    def _check_parent(self, note_id: str, parent: str) -> None:
        """A list can sit in a group, but never inside itself."""
        seen, cur = {note_id}, parent
        while cur:
            if cur in seen:
                raise ValueError("a list can't be inside itself")
            seen.add(cur)
            if N.get(self.conn, cur) is None:
                raise KeyError(cur)
            cur = N.meta(self.conn, cur)["parent"]

    def _body_change(self, note_id: str, edit) -> dict:
        row = N.get(self.conn, note_id)
        if row is None:
            raise KeyError(note_id)
        return self.update_note(note_id, body=edit(row["body"] or ""))

    def add_to_list(self, note_id: str, text: str, remind: str | None = None,
                    section: str | None = None) -> dict:
        return self._body_change(note_id, lambda body: CL.add_item(
            body, text, section=section, remind=remind))

    def set_item(self, note_id: str, line: int | None, text: str, remind: str | None = None,
                 due: str | None = None, repeat: str | None = None) -> dict:
        return self._body_change(note_id, lambda body: CL.set_when(
            body, line, text, remind=remind, due=due, repeat=repeat))

    def _resolve_steer(self, topic: str, text: str) -> tuple[str, str]:
        """Extend a bare `@token` with following words when doing so names a
        topic that actually exists.

        '@Gaming/Elden Ring malenia tips' resolves to Gaming/Elden Ring once
        that topic exists, without the user needing quotes. A path that does
        not resolve is left alone, so new topics still need @"quoted form".
        """
        if T.by_path(self.conn, topic) is not None:
            return topic, text
        words = text.split()
        for take in range(1, min(4, len(words)) + 1):
            candidate = f"{topic} {' '.join(words[:take])}"
            if T.by_path(self.conn, candidate) is not None:
                return candidate, " ".join(words[take:])
        return topic, text

    # -------------------------------------------------------- cold start --
    def _warming_up(self) -> bool:
        """Too few notes for a machine-chosen topic name to mean anything.

        Distinct from `_in_cold_start`: steering can create topics early, which
        ends cold start, but a five-note vault still has no business inventing
        topics from one note's wording.
        """
        count = self.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        return count < self.cfg.cold_start_notes

    def _in_cold_start(self) -> bool:
        """True while the vault has no taxonomy and too little data to build one."""
        if T.all_active(self.conn):
            return False
        count = self.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        return count < self.cfg.cold_start_notes

    def _maybe_bootstrap(self) -> list[str]:
        """Derive the starting taxonomy from the first batch of real notes.

        Clustering 20 notes finds structure that no single note reveals, and
        naming a cluster from the terms its members SHARE produces a real topic
        name rather than one note's incidental wording.
        """
        if T.all_active(self.conn):
            return []
        rows = N.inbox(self.conn)
        if len(rows) < self.cfg.cold_start_notes:
            return []

        ids, vectors, texts = [], [], []
        for row in rows:
            vec = N.vector(self.conn, row["id"])
            if not vec:
                continue
            ids.append(row["id"])
            vectors.append(vec)
            texts.append(f'{row["title"]} {row["body"]}')

        if len(vectors) < self.cfg.cold_start_notes:
            return []

        k, assign, _ = Gd.choose_k(vectors, k_min=2,
                                   k_max=min(self.cfg.max_children, len(vectors) // 2),
                                   min_cluster=self.cfg.min_mass)

        created: list[str] = []
        for cluster in range(k):
            members = [i for i, a in enumerate(assign) if a == cluster]
            if len(members) < self.cfg.min_mass:
                continue
            name = name_from_texts([texts[i] for i in members])
            topic_id = T.create(self.conn, name, None, state="active",
                                name_vec=self._embed_one(name))
            created.append(topic_id)
            for i in members:
                N.assign(self.conn, ids[i], topic_id, is_primary=True,
                         confidence=0.5, assigned_by="gardener")
                T.fold_into_centroid(self.conn, topic_id, vectors[i])
            T.recount(self.conn, topic_id)

            # Move the clustered notes out of Inbox into their new folder.
            for i in members:
                row = N.get(self.conn, ids[i])
                if not row or not row["path"]:
                    continue
                note = self.vault.read(row["path"])
                note.topics = [T.path_of(self.conn, topic_id) or ""]
                dest = self._place(note, topic_id, row["path"])
                self.vault.write(note, dest)
                self.conn.execute("UPDATE notes SET path = ?, needs_review = 1"
                                  " WHERE id = ?", (dest, ids[i]))

        if created:
            J.record(self.conn, "taxonomy.bootstrap",
                     {"topics": created, "notes": len(ids)},
                     {"op": "noop"})
        return created

    def bootstrap_now(self) -> list[str]:
        """Force the cold-start clustering early, from the CLI or the UI."""
        with transaction(self.conn):
            return self._maybe_bootstrap()

    # ------------------------------------------------------------ placing --
    def _place(self, note: Note, topic_id: str | None, current_path: str) -> str:
        """Move a note to wherever its topic currently projects to.

        Uses `gate.effective_dir`, so a note assigned to a *provisional* topic
        stays in the nearest active ancestor's folder instead of creating a
        directory the topic has not earned yet.
        """
        target_dir = G.effective_dir(self.conn, topic_id) if topic_id else None
        desired = self.vault.path_for(note, target_dir)
        if desired != current_path:
            self.vault.move(current_path, desired)
        return desired

    def _promote_and_relocate(self) -> list[str]:
        """Graduate provisional topics that hit min_mass, then physically move
        their notes into the folder they just earned."""
        promoted = G.promote_ready(self.conn, self.cfg)
        for topic_id in promoted:
            for row in N.in_topic(self.conn, topic_id):
                if not row["path"]:
                    continue
                note = self.vault.read(row["path"])
                new_path = self._place(note, topic_id, row["path"])
                if new_path != row["path"]:
                    self.conn.execute("UPDATE notes SET path = ? WHERE id = ?",
                                      (new_path, row["id"]))
        return promoted

    def _link_related(self, note_id: str, vec) -> list[str]:
        linked = []
        for other_id, sim in top_k(vec, N.all_vectors(self.conn, exclude=note_id),
                                   k=5, floor=self.cfg.related_edge_floor):
            if sim >= self.cfg.related_edge_floor:
                N.link(self.conn, note_id, other_id, "related", round(sim, 4))
                linked.append(other_id)
        return linked

    # -------------------------------------------------------------- edits --
    def update_note(self, note_id: str, body: str | None = None,
                    title: str | None = None) -> dict:
        """Save an edit. Re-embeds when the meaning changed, because every
        similarity decision downstream depends on the embedding matching what
        the note says. A tick, a date on an item or lines dragged into a new
        order say the same thing, so those skip the embedder and leave the
        collection's average alone."""
        row = N.get(self.conn, note_id)
        if row is None:
            raise KeyError(note_id)
        old_path = row["path"]
        note = self.vault.read(old_path) if old_path else None
        if note is None:
            raise KeyError(f"no file for {note_id}")

        previous = {"title": note.title, "body": note.body}
        if body is not None:
            note.body = body
        if title is not None:
            note.title = title.strip() or note.title
        note.updated = now_iso()
        same_meaning = (row["content_hash"] == semantic_sig(note.title, note.body)
                        and N.has_vector(self.conn, note_id))

        with transaction(self.conn):
            vec = None if same_meaning else self._embed_one(f"{note.title}\n{note.body}")
            # The filename follows the title, so a retitle moves the file.
            topic_id = N.primary_topic(self.conn, note_id)
            target_dir = G.effective_dir(self.conn, topic_id) if topic_id else None
            new_path = self.vault.path_for(note, target_dir)
            if new_path != old_path:
                self.vault.move(old_path, new_path)
            self.vault.write(note, new_path)
            N.upsert(self.conn, note, new_path,
                     needs_review=bool(row["needs_review"]))
            if vec is not None:
                N.set_vector(self.conn, note_id, vec, self.embedder.name)
                if topic_id:
                    T.fold_into_centroid(self.conn, topic_id, vec)
            J.record(self.conn, "note.edit",
                     {"note_id": note_id, "path": new_path},
                     {"op": "note.restore", "note_id": note_id,
                      "path": old_path, **previous})
        return {"id": note_id, "path": new_path, "title": note.title}

    def _rewrite(self, note_id: str, change, op: str) -> Note:
        """Change a note's header fields through its file, as one undoable step.

        `change(note)` edits the Note in place. The inverse remembers the
        fields it can touch, so undo puts them all back in one go."""
        row = N.get(self.conn, note_id)
        if row is None or not row["path"]:
            raise KeyError(note_id)
        note = self.vault.read(row["path"])
        before = {k: getattr(note, k) for k in RESTATE_FIELDS}
        change(note)
        with transaction(self.conn):
            self.vault.write(note, row["path"])
            N.upsert(self.conn, note, row["path"], needs_review=bool(row["needs_review"]))
            J.record(self.conn, op, {"note_id": note_id},
                     {"op": "note.restate", "note_id": note_id, "fields": before})
        return note

    def append_to_note(self, note_id: str, text: str) -> dict:
        """Add a line to an existing note.

        'add to the elden ring note that mimic tear helps' is a distinct
        intent from creating a new note, and it is the one the old symbol
        syntax had no way to express at all.
        """
        row = N.get(self.conn, note_id)
        if row is None:
            raise KeyError(note_id)
        note = self.vault.read(row["path"])
        addition = text.strip()
        body = f"{note.body.rstrip()}\n\n{addition}" if note.body.strip() else addition
        return self.update_note(note_id, body=body)

    def resolve_note(self, phrase: str, vec=None) -> list[dict]:
        """Candidate notes for 'add this to X', by title then by meaning."""
        found = {r["id"]: r for r in N.find_by_title(self.conn, phrase)}
        if vec is None:
            vec = self._embed_one(phrase)
        for note_id, sim in top_k(vec, N.all_vectors(self.conn), k=5):
            if sim > self.cfg.related_edge_floor and note_id not in found:
                row = N.get(self.conn, note_id)
                if row is not None:
                    found[note_id] = row
        return [{"id": r["id"], "title": r["title"],
                 "topic": T.path_of(self.conn, N.primary_topic(self.conn, r["id"])),
                 "snippet": (r["body"] or "")[:120]}
                for r in list(found.values())[:5]]

    # --------------------------------------------------------- scheduling --
    def schedule(self, note_id: str, when_iso: str | None, remind: bool = True,
                 snooze: bool = False) -> None:
        """Set (or clear) when something is due. A snooze moves only the
        reminder: the thing is still due when it was due."""
        def change(note: Note) -> None:
            if snooze and when_iso:
                note.remind = when_iso
                return
            note.due = when_iso
            note.remind = when_iso if (when_iso and remind) else None
            if when_iso and note.kind == "note":
                note.kind = "task"
            note.updated = now_iso()
        self._rewrite(note_id, change, "note.schedule")

    def set_order(self, note_id: str, before: str | None = None,
                  after: str | None = None, view: list[str] | None = None) -> float:
        """Put a note between two others: `before` is the note that should sit
        just above it, `after` the one just below (either may be missing at
        the ends). Positions are seconds-like numbers, higher first, so a note
        never dragged sits at the moment it was made. `view` is the whole new
        order on screen, used to space things out again if two neighbours ever
        end up too close to fit another note between."""
        if N.get(self.conn, note_id) is None:
            raise KeyError(note_id)
        up = N.position(self.conn, before) if before else None
        down = N.position(self.conn, after) if after else None
        if up is not None and down is not None:
            if up - down < 1e-3 and view:
                self._respace(view)
                return N.position(self.conn, note_id)
            pos = (up + down) / 2
        elif down is not None:
            # On top: only just above, so anything made from now on still
            # lands above it.
            pos = down + 1e-3
        elif up is not None:
            pos = up - 60.0
        else:
            return N.position(self.conn, note_id)

        def change(note: Note) -> None:
            note.order = pos
        self._rewrite(note_id, change, "note.order")
        return pos

    def _respace(self, view: list[str]) -> None:
        """Give every note in `view` a fresh, evenly spaced position, keeping
        the top one where it is. One undo step."""
        ids = [i for i in view if N.get(self.conn, i) is not None]
        if not ids:
            return
        top = max(N.position(self.conn, i) for i in ids)
        steps = []
        with transaction(self.conn):
            for k, note_id in enumerate(ids):
                row = N.get(self.conn, note_id)
                note = self.vault.read(row["path"])
                steps.append({"op": "note.restate", "note_id": note_id,
                              "fields": {"order": note.order}})
                note.order = top - k
                self.vault.write(note, row["path"])
                N.upsert(self.conn, note, row["path"],
                         needs_review=bool(row["needs_review"]))
            J.record(self.conn, "note.respace", {"ids": ids},
                     {"op": "batch", "steps": steps})

    def complete(self, note_id: str, done: bool = True) -> None:
        def change(note: Note) -> None:
            note.status = "done" if done else "open"
            note.updated = now_iso()
        self._rewrite(note_id, change, "note.complete")

    # ---------------------------------------------------------- blank notes --
    def create_note(self, title: str = "Untitled", body: str = "",
                    topic_path: str | None = None, kind: str = "note",
                    **header) -> dict:
        """A normal, empty note -- the 'New note' button (or 'New list', with
        kind="list" and header fields such as due, checkin or parent).

        The capture box interprets what you type; this does not. With a folder
        chosen, the note simply lives there. Without one it starts unfiled and
        `auto_file` places it once there is something to route on -- a blank
        page has nothing to match against, so filing it now would be a guess.
        """
        note = Note(title=title.strip() or "Untitled", body=body, kind=kind or "note")
        for key, value in header.items():
            if key in RESTATE_FIELDS and value not in (None, ""):
                setattr(note, key, value)
        if note.parent:
            self._check_parent(note.id, note.parent)
        with transaction(self.conn):
            topic_id = (T.ensure_path(self.conn, topic_path, state="active")
                        if topic_path else None)
            if topic_id:
                note.topics = [T.path_of(self.conn, topic_id) or ""]
            target_dir = G.effective_dir(self.conn, topic_id) if topic_id else None
            path = self.vault.path_for(note, target_dir)
            self.vault.write(note, path)
            N.upsert(self.conn, note, path)
            if topic_id:
                N.assign(self.conn, note.id, topic_id, is_primary=True,
                         confidence=1.0, assigned_by="user")
                T.recount(self.conn, topic_id)
            J.record(self.conn, "note.create",
                     {"note_id": note.id, "path": path, "topic_id": topic_id},
                     {"op": "note.remove", "note_id": note.id, "path": path})
        return {"id": note.id, "path": path,
                "topic": T.path_of(self.conn, topic_id) if topic_id else None}

    def auto_file(self, note_id: str) -> dict:
        """Route an existing, unfiled note by what it now says.

        Same decision path as the capture box -- retrieval first, the model only
        when retrieval is unsure, Inbox when nothing is confident -- so a note
        typed by hand files exactly as well as one dictated.
        """
        row = N.get(self.conn, note_id)
        if row is None:
            raise KeyError(note_id)
        if N.primary_topic(self.conn, note_id):
            return {"filed": False, "reason": "already filed",
                    "topic": T.path_of(self.conn, N.primary_topic(self.conn, note_id))}

        note = self.vault.read(row["path"])
        text = f"{note.title}\n{note.body}".strip()
        if len(text.split()) < 3:
            return {"filed": False, "reason": "too short to file", "topic": None}

        vec = self._embed_one(text)
        with transaction(self.conn):
            N.set_vector(self.conn, note_id, vec, self.embedder.name)
            outcome = R.route(
                self.conn, self.provider, self.cfg, vec, text,
                title=note.title, kind_hint=note.kind,
                embed_name=self._embed_one, exclude_note=note_id,
                allow_new_topics=not self._warming_up(),
            )
            R.log(self.conn, note_id, outcome)
            if not outcome.topic_id:
                return {"filed": False, "reason": "not confident", "topic": None,
                        "outcome": outcome.outcome}

            N.assign(self.conn, note_id, outcome.topic_id, is_primary=True,
                     confidence=outcome.confidence, assigned_by="llm")
            T.fold_into_centroid(self.conn, outcome.topic_id, vec)
            T.recount(self.conn, outcome.topic_id)
            note.topics = [T.path_of(self.conn, outcome.topic_id) or ""]
            new_path = self._place(note, outcome.topic_id, row["path"])
            self.vault.write(note, new_path)
            N.upsert(self.conn, note, new_path,
                     needs_review=(outcome.outcome == R.REVIEW))
            related = self._link_related(note_id, vec)
            promoted = self._promote_and_relocate()
            J.record(self.conn, "note.file",
                     {"note_id": note_id, "path": new_path,
                      "topic_id": outcome.topic_id},
                     {"op": "note.relocate", "note_id": note_id,
                      "path": row["path"], "topic_id": None})
        return {"filed": True, "topic": outcome.topic_path, "path": new_path,
                "outcome": outcome.outcome, "related": len(related),
                "promoted": [T.get(self.conn, t)["name"] for t in promoted]}

    def delete_note(self, note_id: str) -> dict:
        """Delete a note, keeping enough in the journal to bring it back.

        The whole Markdown file is stored as the inverse, so `undo` restores
        the note exactly -- a delete button without that is one misclick away
        from losing writing for good.
        """
        from .vault import render

        row = N.get(self.conn, note_id)
        if row is None:
            raise KeyError(note_id)
        note = self.vault.read(row["path"]) if row["path"] else None
        topic_id = N.primary_topic(self.conn, note_id)
        with transaction(self.conn):
            if row["path"]:
                self.vault.delete(row["path"])
            for tid in N.topics_of(self.conn, note_id):
                N.unassign(self.conn, note_id, tid)
                T.recount(self.conn, tid)
            N.delete(self.conn, note_id)
            J.record(self.conn, "note.delete",
                     {"note_id": note_id, "path": row["path"]},
                     {"op": "note.undelete", "note_id": note_id,
                      "path": row["path"], "topic_id": topic_id,
                      "markdown": render(note) if note else "",
                      "due_at": row["due_at"], "remind_at": row["remind_at"]})
        return {"deleted": note_id, "title": row["title"]}

    def discard_if_blank(self, note_id: str) -> bool:
        """Drop a note made with "New note" and left without a word in it.

        Unlike a delete this leaves no trace: the creation is struck from the
        journal too, so Ctrl+Z doesn't spend two presses resurrecting and
        re-deleting an empty page. Anything with content is left alone.
        """
        row = N.get(self.conn, note_id)
        if row is None:
            return False
        # A new list starts with one empty "- [ ]" waiting to be typed into.
        words = re.sub(r"(?m)^\s*[-*+]\s+\[[ xX]\]\s*$", "", row["body"] or "").strip()
        if (row["title"] or "").strip() not in ("", "Untitled") or words:
            return False
        with transaction(self.conn):
            if row["path"]:
                self.vault.delete(row["path"])
            for tid in N.topics_of(self.conn, note_id):
                N.unassign(self.conn, note_id, tid)
                T.recount(self.conn, tid)
            N.delete(self.conn, note_id)
            self.conn.execute(
                "UPDATE journal SET undone = 1 WHERE undone = 0"
                " AND json_extract(payload, '$.note_id') = ?", (note_id,))
        return True

    # ---------------------------------------------------------- correction --
    def move_note(self, note_id: str, topic_path: str) -> str:
        """User drags a note elsewhere. Explicit intent, so it bypasses the gate
        and is recorded as a correction the router can learn from."""
        row = N.get(self.conn, note_id)
        if row is None:
            raise KeyError(note_id)
        old_topic = N.primary_topic(self.conn, note_id)
        old_path = row["path"]

        with transaction(self.conn):
            topic_id = T.ensure_path(self.conn, topic_path, state="active")
            if old_topic:
                N.unassign(self.conn, note_id, old_topic)
                T.recount(self.conn, old_topic)
            N.assign(self.conn, note_id, topic_id, is_primary=True,
                     confidence=1.0, assigned_by="user")
            T.fold_into_centroid(self.conn, topic_id, N.vector(self.conn, note_id))
            T.recount(self.conn, topic_id)

            note = self.vault.read(old_path)
            note.topics = [T.path_of(self.conn, topic_id) or ""]
            new_path = self._place(note, topic_id, old_path)
            self.vault.write(note, new_path)
            N.upsert(self.conn, note, new_path, needs_review=False)
            R.record_correction(self.conn, note_id, topic_id)

            J.record(
                self.conn, "note.move",
                {"note_id": note_id, "from": old_path, "to": new_path,
                 "topic_id": topic_id},
                {"op": "note.relocate", "note_id": note_id, "path": old_path,
                 "topic_id": old_topic},
            )
        return new_path

    # ----------------------------------------------------------- tidy up --
    def apply_proposal(self, proposal_id: str) -> dict:
        """Carry out a gardener suggestion the user accepted.

        Only merges and dissolves for now: both come down to moving every note
        out of one collection, into another (merge) or up to its parent or the
        Inbox (dissolve). The whole thing is one journal entry, so a single
        undo puts every note back and revives the collection.
        """
        row = self.conn.execute(
            "SELECT * FROM proposals WHERE id = ? AND state = 'pending'",
            (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        payload = json.loads(row["payload"])
        if row["kind"] == Gd.MERGE:
            source, target = payload["drop"], payload["keep"]
        elif row["kind"] == Gd.DISSOLVE:
            source = payload["topic"]
            parent = T.get(self.conn, source)
            target = parent["parent_id"] if parent else None
        else:
            raise ValueError(f"{row['kind']} suggestions can't be applied yet")
        source_row = T.get(self.conn, source)
        if source_row is None or source_row["state"] == "dissolved":
            raise KeyError(source)
        if T.children(self.conn, source):
            raise ValueError("collections with sub-collections can't be merged yet")

        steps: list[dict] = []
        with transaction(self.conn):
            target_path = T.path_of(self.conn, target) if target else None
            for note_row in N.in_topic(self.conn, source):
                old_path = note_row["path"]
                N.unassign(self.conn, note_row["id"], source)
                if target:
                    N.assign(self.conn, note_row["id"], target, is_primary=True,
                             confidence=1.0, assigned_by="user")
                    T.fold_into_centroid(self.conn, target,
                                         N.vector(self.conn, note_row["id"]))
                note = self.vault.read(old_path)
                note.topics = [target_path] if target_path else []
                new_path = self._place(note, target, old_path)
                self.vault.write(note, new_path)
                N.upsert(self.conn, note, new_path, needs_review=False)
                steps.append({"op": "note.relocate", "note_id": note_row["id"],
                              "path": old_path, "topic_id": source})
            if target:
                T.recount(self.conn, target)
                # Saying the old name later should still land in the new home.
                T.add_alias(self.conn, source_row["name"], target)
            T.recount(self.conn, source)
            self.conn.execute("UPDATE topics SET state = 'dissolved' WHERE id = ?",
                              (source,))
            self.conn.execute("UPDATE proposals SET state = 'applied' WHERE id = ?",
                              (proposal_id,))
            steps.insert(0, {"op": "topic.restate", "topic_id": source,
                             "state": source_row["state"],
                             "proposal_id": proposal_id,
                             "alias": source_row["name"] if target else None})
            J.record(self.conn, "garden.apply",
                     {"proposal_id": proposal_id, "kind": row["kind"],
                      "source": source, "target": target},
                     {"op": "batch", "steps": steps})
        return {"ok": True, "moved": len(steps) - 1,
                "from": source_row["name"], "to": target_path}

    # --------------------------------------------------------------- undo --
    def _register_undo(self) -> None:
        # Kept on this engine, not shared: each profile is its own engine, and
        # an undo must only ever touch its own vault.
        self._undo_handlers = {
            "note.remove": self._undo_remove,
            "note.relocate": self._undo_relocate,
            # `update_note` has always journaled a `note.restore` inverse, but no
            # handler existed -- so Ctrl+Z after editing raised KeyError.
            "note.restore": self._undo_edit,
            "note.undelete": self._undo_delete,
            "batch": self._undo_batch,
            "topic.restate": self._undo_restate,
            "note.restate": self._undo_note_restate,
        }

    def _undo_note_restate(self, rec: dict) -> None:
        row = N.get(self.conn, rec["note_id"])
        if row is None or not row["path"]:
            return
        note = self.vault.read(row["path"])
        for key, value in (rec.get("fields") or {}).items():
            if key in RESTATE_FIELDS:
                setattr(note, key, value)
        self.vault.write(note, row["path"])
        N.upsert(self.conn, note, row["path"], needs_review=bool(row["needs_review"]))

    def _undo_batch(self, rec: dict) -> None:
        # Steps were recorded in the order they happened; undo runs backwards.
        for step in reversed(rec.get("steps") or []):
            J.run(step, self._undo_handlers)

    def _undo_restate(self, rec: dict) -> None:
        self.conn.execute("UPDATE topics SET state = ? WHERE id = ?",
                          (rec["state"], rec["topic_id"]))
        if rec.get("proposal_id"):
            self.conn.execute("UPDATE proposals SET state = 'pending' WHERE id = ?",
                              (rec["proposal_id"],))
        if rec.get("alias"):
            self.conn.execute("DELETE FROM topic_aliases WHERE alias = ?",
                              (slugify(rec["alias"]),))

    def _undo_edit(self, rec: dict) -> None:
        row = N.get(self.conn, rec["note_id"])
        if row is None or not row["path"]:
            return
        note = self.vault.read(row["path"])
        note.title = rec.get("title", note.title)
        note.body = rec.get("body", note.body)
        topic_id = N.primary_topic(self.conn, rec["note_id"])
        target_dir = G.effective_dir(self.conn, topic_id) if topic_id else None
        new_path = self.vault.path_for(note, target_dir)
        if new_path != row["path"]:
            self.vault.move(row["path"], new_path)
        self.vault.write(note, new_path)
        N.upsert(self.conn, note, new_path, needs_review=bool(row["needs_review"]))
        N.set_vector(self.conn, rec["note_id"],
                     self._embed_one(f"{note.title}\n{note.body}"), self.embedder.name)

    def _undo_delete(self, rec: dict) -> None:
        from .vault import loads

        if not rec.get("markdown") or not rec.get("path"):
            return
        note = loads(rec["markdown"])
        # Deletes recorded before times lived in the file kept them on the side.
        note.due = note.due or rec.get("due_at")
        note.remind = note.remind or rec.get("remind_at")
        self.vault.write(note, rec["path"])
        N.upsert(self.conn, note, rec["path"])
        topic_id = rec.get("topic_id")
        if topic_id and T.get(self.conn, topic_id) is not None:
            N.assign(self.conn, note.id, topic_id, is_primary=True,
                     assigned_by="user")
            T.recount(self.conn, topic_id)
        N.set_vector(self.conn, note.id,
                     self._embed_one(f"{note.title}\n{note.body}"), self.embedder.name)

    def _undo_remove(self, rec: dict) -> None:
        note_id = rec["note_id"]
        row = N.get(self.conn, note_id)
        if row and row["path"]:
            self.vault.delete(row["path"])
        for topic_id in N.topics_of(self.conn, note_id):
            N.unassign(self.conn, note_id, topic_id)
            T.recount(self.conn, topic_id)
        N.delete(self.conn, note_id)

    def _undo_relocate(self, rec: dict) -> None:
        note_id, target_path = rec["note_id"], rec["path"]
        row = N.get(self.conn, note_id)
        if row is None:
            return
        if row["path"] and row["path"] != target_path:
            self.vault.move(row["path"], target_path)
        for topic_id in N.topics_of(self.conn, note_id):
            N.unassign(self.conn, note_id, topic_id)
            T.recount(self.conn, topic_id)
        if rec.get("topic_id"):
            N.assign(self.conn, note_id, rec["topic_id"], is_primary=True,
                     assigned_by="user")
            T.recount(self.conn, rec["topic_id"])
        self.conn.execute("UPDATE notes SET path = ? WHERE id = ?",
                          (target_path, note_id))
        # The file's frontmatter must agree, or `rebuild` would silently
        # re-file the note into the topic this undo just took it out of.
        note = self.vault.read(target_path)
        note.topics = ([T.path_of(self.conn, rec["topic_id"]) or ""]
                       if rec.get("topic_id") else [])
        self.vault.write(note, target_path)

    def undo(self, seq: int | None = None) -> dict | None:
        with transaction(self.conn):
            return J.undo(self.conn, seq, self._undo_handlers)

    # ------------------------------------------------------------ rebuild --
    def rebuild(self) -> dict:
        """Reconstruct the entire index from the Markdown files alone.

        This is what makes index.db disposable: delete it, run this, get
        everything back. Also the recovery path if the DB is ever corrupted.
        """
        stats = {"notes": 0, "topics": 0}
        with transaction(self.conn):
            for table in ("note_topics", "note_vec", "notes", "topic_vec",
                          "topic_aliases", "topics", "edges"):
                self.conn.execute(f"DELETE FROM {table}")

            for path, note in self.vault.scan():
                vec = self._embed_one(f"{note.title}\n{note.body}")
                N.upsert(self.conn, note, path)
                N.set_vector(self.conn, note.id, vec, self.embedder.name)
                stats["notes"] += 1
                if note.topics:
                    topic_id = T.ensure_path(self.conn, note.topics[0], state="active")
                    N.assign(self.conn, note.id, topic_id, is_primary=True,
                             assigned_by="user")
                    T.fold_into_centroid(self.conn, topic_id, vec)
                    for secondary in note.topics[1:]:
                        sid = T.ensure_path(self.conn, secondary, state="active")
                        N.assign(self.conn, note.id, sid, is_primary=False,
                                 assigned_by="user")
            T.recount_all(self.conn)
            # Reminders already in the past were shown before the rebuild;
            # don't fire them all again.
            self.conn.execute("UPDATE notes SET reminded = 1 WHERE remind_at <= ?",
                              (local_now_iso(),))
            from . import reminders as Rem
            Rem.mark_past(self.conn)          # items and check-ins too
            for row in T.all_active(self.conn):
                if row["name_vec"] is None:
                    T.set_name_vec(self.conn, row["id"], self._embed_one(row["name"]))
            stats["topics"] = len(T.all_active(self.conn))
            # Re-embedding with the current model makes this vault its own again.
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES('embedder', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self.embedder.name,))
        self.embedder_changed = None
        stats["embedder"] = self.embedder.name
        return stats

    # ------------------------------------------------------------ helpers --
    def _embed_one(self, text: str):
        if hasattr(self.embedder, "embed_one"):
            return self.embedder.embed_one(text)
        return self.embedder.embed([text])[0]

    @staticmethod
    def _derive_title(text: str) -> str:
        first = text.strip().splitlines()[0] if text.strip() else "untitled"
        first = re.sub(r"^#+\s*", "", first).strip()
        if len(first) > 70:
            first = first[:67].rsplit(" ", 1)[0] + "..."
        return first or "untitled"
