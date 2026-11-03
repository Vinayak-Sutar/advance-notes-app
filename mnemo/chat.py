"""Ask your notes: the brain behind the chat panel.

A message is either an instruction or a question.

Instructions ("remind me...", "save...", "add to the X note...", "move the X
note to Y") go through the same engine calls the rest of the app uses, so they
file, schedule and undo exactly like a capture. No model is needed for them,
which also means they keep working when the local AI is down.

Questions are answered by the local model, grounded in a handful of notes
picked by search (plus the agenda or the Inbox when the question is about
those). The model ends its answer with a "Sources:" line naming the notes it
actually used; that line is stripped from the stream and becomes the source
chips under the answer.

`Chat.events` yields the wire events of the /api/chat stream:
    {"type": "sources", "notes": [{id, title, topic}]}
    {"type": "token",   "text": "..."}
    {"type": "action",  "action": {kind, summary, noteId}}
    {"type": "done",    "fromNotes": bool}
    {"type": "error",   "message": "..."}
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime
from typing import Iterator, Protocol

from . import intent as I
from . import notes as N
from . import search as S
from . import topics as T
from . import websearch as W
from .vecs import cosine

MAX_NOTES = 6           # notes chosen by search
MAX_CONTEXT = 10        # including agenda / Inbox / recent picks
EXCERPT_CHARS = 700
HISTORY_TURNS = 6

SYSTEM_PROMPT = """You are the assistant inside Notes, a private notes and reminders app. \
Everything runs on the user's own computer.
Today is {today}.

Answer using the user's notes below when they are relevant. Be brief and warm: \
plain words, short paragraphs, '-' bullet lists. Put note titles in **bold**. \
Write dates like "Fri 24 Oct, 5:00 PM".
If the notes don't answer the question, say so in one short sentence, then \
answer from general knowledge if you can.
Never invent notes, dates or reminders that aren't listed.

Your own knowledge stops in early 2024. Never guess about anything after that, or \
anything you aren't sure of: events, results, winners, prices, releases, who holds \
a job now. Saying "I don't know" is always better than a wrong answer.
{web_rule}
If web results are given below, answer from them, name the page you used, and \
don't search again. If they don't contain the answer, say so.

You can't change notes yourself. If the user wants something done, tell them to \
start their message with "remind me", "save" or "move", or to say something like \
"exam prep list: algebra, calculus by 15 nov" or "add optics to the exam prep list".
Finish with one last line on its own: "Sources:" followed by what you used: note \
numbers and web results, like "Sources: 1, W2", or "Sources: none"."""

WEB_ON = """If you need the web to answer, reply with exactly one line and nothing \
else: "SEARCH: <short web search query>". You'll then be given web results."""
WEB_OFF = """Web search is turned off. If you don't know something, say so plainly \
and mention that turning on web search in Settings would let you look it up."""
WEB_TOOLS = """You have tools. Use search_notes to look further through the user's \
notes, calculator for any arithmetic, create_reminder to set a reminder, move_note \
to move a note, and create_list, add_to_list and pause_reminders for to-do lists. \
Use web_search (then read_page for details) when you need the web, \
instead of guessing. Answer only once you have what you need."""
TOOLS_NO_WEB = """You have tools. Use search_notes to look further through the user's \
notes, calculator for any arithmetic, create_reminder to set a reminder, move_note \
to move a note, and create_list, add_to_list and pause_reminders for to-do lists. \
Web search is turned off: if you don't know something, say so and \
mention that turning on web search in Settings would let you look it up."""


# ------------------------------------------------------------ the model ----
class ChatModel(Protocol):
    def stream(self, messages: list[dict]) -> Iterator[str]: ...
    def close(self) -> None: ...


class ModelUnavailable(RuntimeError):
    """The local model can't be reached at all (not an error mid-answer)."""


class OllamaChat:
    """Streams a chat completion from the bundled Ollama, token by token."""

    def __init__(self, model: str | None = None, host: str | None = None,
                 timeout: int = 180, think: bool = False):
        from .models import ROUTER_MODEL
        from .paths import OLLAMA_HOST
        self.model = model or ROUTER_MODEL
        self.host = (host or OLLAMA_HOST).rstrip("/")
        self.timeout = timeout
        self.think = think
        self._resp = None

    def stream(self, messages: list[dict]) -> Iterator[str]:
        from .runtime import think_option
        payload = {
            "model": self.model, "messages": messages, "stream": True,
            "keep_alive": -1,
            # No num_ctx here: a different context size makes Ollama reload
            # the model, which costs seconds and evicts the router's copy.
            "options": {"temperature": 0.3},
            **think_option(self.model, self.think),
        }
        request = urllib.request.Request(
            f"{self.host}/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            self._resp = urllib.request.urlopen(request, timeout=self.timeout)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise ModelUnavailable(str(exc)) from exc
        try:
            for line in self._resp:
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(data["error"])
                text = (data.get("message") or {}).get("content") or ""
                if text:
                    yield text
                if data.get("done"):
                    break
        finally:
            self.close()

    def close(self) -> None:
        # Closing the connection is what tells Ollama to stop generating.
        resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


# -------------------------------------------------------------- helpers ----
def fmt_when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso.replace("Z", ""))
    except ValueError:
        return iso
    hour = d.hour % 12 or 12
    return (f"{d.strftime('%a')} {d.day} {d.strftime('%b')}, "
            f"{hour}:{d.minute:02d} {'PM' if d.hour >= 12 else 'AM'}")


def _today() -> str:
    d = datetime.now()
    hour = d.hour % 12 or 12
    return (f"{d.strftime('%A')} {d.day} {d.strftime('%B')} {d.year}, "
            f"{hour}:{d.minute:02d} {'PM' if d.hour >= 12 else 'AM'}")


def _day(iso: str) -> str:
    """'2026-11-10' -> 'Tue 10 Nov'."""
    try:
        d = datetime.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _list_name(spoken) -> str:
    """'the Exam prep list' -> 'Exam prep'."""
    name = re.sub(r"^\s*(?:the|my)\s+", "", str(spoken or ""), flags=re.I)
    return re.sub(r"\s+(?:to-?do\s+)?list\s*$", "", name, flags=re.I).strip(" .,:")


def _cap(s: str) -> str:
    s = s.strip()
    return s[:1].upper() + s[1:] if s else s


def _words(text: str, size: int = 3) -> Iterator[str]:
    """Split a ready-made reply into small pieces so it streams like the model."""
    parts = re.findall(r"\S+\s*", text)
    for i in range(0, len(parts), size):
        yield "".join(parts[i:i + size])


# Instructions. Each is anchored at the start: a question that merely
# mentions a reminder ("when is my dentist reminder?") is still a question.
_REMIND = re.compile(
    r"^\s*(?:please\s+)?(?:remind me\b|(?:set|add|make|create)\s+(?:a\s+|an\s+)?reminder\b)",
    re.I)
# "Remind me what I saved yesterday?" is a question, not a reminder.
_REMIND_QUESTION = re.compile(
    r"^\s*(?:please\s+)?remind me\s+(?:what|which|who|where|when|how|why|if|whether|about what)", re.I)
_SET_REMINDER = re.compile(
    r"^\s*(?:please\s+)?(?:set|add|make|create)\s+(?:a\s+|an\s+)?reminder\s+(?:to\s+|for\s+|that\s+)?",
    re.I)
_APPEND = re.compile(r"^\s*(?:please\s+)?add\s+(?:this\s+|it\s+)?to\s+(?:the\s+|my\s+)?\S.*?\s(?:note|list)\b", re.I)
_SAVE = re.compile(
    r"^\s*(?:please\s+)?(?:save|note down|jot down|write down|remember)"
    r"(?:\s+(?:this|that))?(?:\s+as\s+a\s+note)?\s*[:,\-]?\s+(?P<text>\S.*)$", re.I | re.S)
_MOVE = re.compile(
    r"^\s*(?:please\s+)?move\s+(?:the\s+|my\s+)?(?P<what>.+?)(?:\s+note)?\s+"
    r"(?:to|into|in)\s+(?:the\s+|my\s+)?(?P<dest>.+?)(?:\s+(?:collection|folder))?\s*[.!?]*\s*$",
    re.I)

_ABOUT_DUE = re.compile(
    r"\b(due|reminders?|agenda|to-?dos?|tasks?|overdue|schedule|"
    r"what do i have|coming up|today|tomorrow|this week|next week)\b", re.I)
_ABOUT_INBOX = re.compile(r"\binbox\b", re.I)
_ABOUT_RECENT = re.compile(
    r"\b(recent(?:ly)?|lately|latest|last few|just saved|"
    r"saved (?:today|this week|yesterday))\b", re.I)


# --------------------------------------------------------- source filter ----
class _SourcesFilter:
    """Pass streamed text through, holding back a final "Sources: 1, 3" line.

    The line can arrive split across tokens, so whichever line is still open
    is held while it could yet turn out to be the sources line.
    """
    KEY = "sources:"

    def __init__(self):
        self.line = ""        # the current, unfinished line
        self.passing = False  # current line already ruled out and streaming
        self.cited: list[int] | None = None       # note numbers
        self.cited_web: list[int] = []            # W numbers

    def feed(self, text: str) -> str:
        out = []
        for ch in text:
            if ch == "\n":
                if not self.passing:
                    out.append(self.line)
                out.append("\n")
                self.line, self.passing = "", False
                continue
            if self.passing:
                out.append(ch)
                continue
            self.line += ch
            probe = self.line.lstrip(" *_").lower()
            if not (self.KEY.startswith(probe) or probe.startswith(self.KEY)):
                out.append(self.line)
                self.passing = True
        return "".join(out)

    def finish(self) -> str:
        probe = self.line.lstrip(" *_").lower()
        if not self.passing and probe.startswith(self.KEY):
            rest = probe[len(self.KEY):]
            self.cited = [int(n) for n in re.findall(r"(?<![\dw])(\d+)", rest)]
            self.cited_web = [int(n) for n in re.findall(r"w(\d+)", rest)]
            return ""
        return "" if self.passing else self.line


class _SearchRequest:
    """Hold the answer's first line until it's clearly not "SEARCH: <query>".

    That line is how the model asks for the web. It must never reach the
    screen, and when it appears the answer so far is thrown away.
    """
    KEY = "search:"

    def __init__(self):
        self.head = ""
        self.decided = False
        self.query: str | None = None

    def feed(self, text: str) -> str:
        if self.decided:
            return text
        self.head += text
        probe = self.head.lstrip(" *_`\n").lower()
        if probe.startswith(self.KEY):
            if "\n" in self.head.lstrip("\n"):
                self._take()
            return ""
        if self.KEY.startswith(probe):      # could still turn into "SEARCH:"
            return ""
        self.decided = True
        out, self.head = self.head, ""
        return out

    def finish(self) -> str:
        if self.decided:
            return ""
        if self.head.lstrip(" *_`\n").lower().startswith(self.KEY):
            self._take()
            return ""
        self.decided = True
        return self.head

    def _take(self):
        line = self.head.lstrip(" *_`\n").split("\n", 1)[0]
        self.query = line[len(self.KEY):].strip(" *_`\"'") or None
        self.decided = True


# ------------------------------------------------------------------ chat ----
class Chat:
    def __init__(self, engine, lock=None, model: ChatModel | None = None,
                 json_model=None, searcher=None, tool_model=None, think: bool = False):
        self.engine = engine
        self.lock = lock or threading.Lock()
        self.model = model
        self.json_model = json_model          # writes web search queries
        self.searcher = searcher or W.search  # (query, WebConfig) -> [WebResult]
        self.tool_model = tool_model          # a model that can call tools (agent.ToolModel)
        self.think = think                    # "think harder"

    def _tools_model(self):
        """A tool-calling model if the brain supports tools, else None."""
        if self.tool_model is not None:
            return self.tool_model
        if self.model is not None:            # an injected plain model: old flow
            return None
        from . import agent as AG
        from . import models, runtime
        brain = models.THINK_MODEL if self.think and models.THINK_MODEL else models.ROUTER_MODEL
        if "tools" in runtime.caps(brain):
            return AG.OllamaTools(brain, think=self.think)
        return None

    @property
    def conn(self):
        return self.engine.conn

    def events(self, message: str, history: list[dict] | None = None,
               organise: str = "auto", web: W.WebConfig | None = None) -> Iterator[dict]:
        message = (message or "").strip()
        if not message:
            yield {"type": "error", "message": "Ask me something first."}
            return
        web = web or W.WebConfig(mode="off")
        history = history or []
        try:
            with self.lock:
                reply = self._instruction(message, auto_file=organise == "auto")
                if reply is None:
                    # Everything that reads the database happens here, under
                    # the lock; the web and the model are slow and run outside it.
                    picked = self._retrieve(message)
                    notes = self._notes_block(picked)
                    briefs = [self._brief(r["id"]) for r in picked]
        except Exception as exc:                    # never leave the UI hanging
            yield {"type": "error", "message": f"Something went wrong: {exc}"}
            return

        if reply is not None:
            sources, actions, text = reply
            yield {"type": "sources", "notes": sources}
            for piece in _words(text):
                yield {"type": "token", "text": piece}
            for action in actions:
                yield {"type": "action", "action": action}
            yield {"type": "done", "fromNotes": True}
            return

        yield from self._question(message, picked, notes, briefs, history, web)

    # ----------------------------------------------------------------- web --
    def _question(self, message: str, picked: list, notes: str, briefs: list[dict],
                  history: list[dict], web: W.WebConfig) -> Iterator[dict]:
        """Answer, looking things up first when the question is about things
        the model can't know, or when the model asks for it mid-answer."""
        tools = self._tools_model()
        if tools is not None:
            yield from self._agent(tools, message, picked, notes, briefs, history, web)
            return
        if web.enabled and W.needs_web(message):
            query = self._write_query(message, history)
            yield {"type": "searching", "query": query}
            found, failed = self._search(query, web)
            yield from self._answer(picked, briefs, self._prompt(
                message, notes, history, web, found, failed, query),
                found, allow_search=False, query=query)
            return
        asked = yield from self._answer(picked, briefs, self._prompt(
            message, notes, history, web,
            must_say_unknown=not web.enabled and W.needs_web(message)),
            [], allow_search=web.enabled)
        if asked:                                   # the model said "SEARCH: ..."
            yield {"type": "searching", "query": asked}
            found, failed = self._search(asked, web)
            yield from self._answer(picked, briefs, self._prompt(
                message, notes, history, web, found, failed, asked),
                found, allow_search=False, query=asked)

    def _write_query(self, message: str, history: list[dict]) -> str:
        if self.json_model is None:
            from .noteai import OllamaJson
            self.json_model = OllamaJson()
        try:
            return W.write_query(self.json_model, message, history)
        except Exception:
            return message

    def _agent(self, tools_model, message: str, picked: list, notes: str, briefs: list[dict],
               history: list[dict], web: W.WebConfig) -> Iterator[dict]:
        """Answer with a model that calls tools itself.

        The notes search already found are given up front (most questions need
        nothing else). Notes and pages the tools find are added to the same
        numbered lists, so "Sources: 2, W1" means the same thing either way.
        The quick paths stay in front: instructions never reach this, and a
        question that plainly needs the web is looked up before the model
        gets a chance to guess.
        """
        from . import agent as AG
        from . import calc
        from .enrich import fetch_url

        picked, briefs, found, actions = list(picked), list(briefs), [], []
        query = None
        if web.enabled and W.needs_web(message):
            query = self._write_query(message, history)
            yield {"type": "searching", "query": query}
            found, failed = self._search(query, web)
        else:
            failed = None
        prompt = self._prompt(message, notes, history, web, found, failed, query,
                              must_say_unknown=not web.enabled and W.needs_web(message))
        prompt[0]["content"] = prompt[0]["content"].replace(
            WEB_ON, "").replace(WEB_OFF, "") + "\n\n" + (WEB_TOOLS if web.enabled else TOOLS_NO_WEB)

        def note_block(rows):
            out = []
            for row in rows:
                if row["id"] not in {r["id"] for r in picked}:
                    picked.append(row)
                    briefs.append(self._brief(row["id"]))
                i = next(n for n, r in enumerate(picked, 1) if r["id"] == row["id"])
                body = " ".join((row["body"] or "").split())[:EXCERPT_CHARS]
                due = f" (due {fmt_when(row['due_at'])})" if row["due_at"] else ""
                out.append(f"[{i}] {row['title']}{due}\n{body}")
            return "\n\n".join(out) or "No notes matched."

        def search_notes(args):
            with self.lock:
                return AG.ToolResult(text=note_block(self._retrieve(str(args.get("query") or message))))

        def web_search(args):
            q = str(args.get("query") or message)
            results, why = self._search(q, web)
            start = len(found)
            found.extend(r for r in results if r.url not in {f.url for f in found})
            new = found[start:]
            if not new:
                return AG.ToolResult(text=f"Nothing useful came back{f' ({why})' if why else ''}.")
            return AG.ToolResult(text="\n\n".join(
                f"[W{start + i}] {r.title} ({r.site}) {r.url}\n{r.text or r.snippet}"
                for i, r in enumerate(new, 1)), web=new)

        def read_page(args):
            url = str(args.get("url") or "")
            if not url.startswith(("http://", "https://")):
                return AG.ToolResult(text="That isn't a web address.")
            page = fetch_url(url)
            if not page.ok:
                return AG.ToolResult(text=f"Couldn't read that page ({page.error}).")
            text = " ".join(page.text.split())[:3000]
            for r in found:
                if r.url == url:
                    r.text = text[:W.TEXT_CHARS]
            return AG.ToolResult(text=f"{page.title}\n{text}")

        def calculator(args):
            expr = str(args.get("expression") or "")
            worked = calc.facts(expr)
            if worked:
                return AG.ToolResult(text="\n".join(worked))
            value = calc.evaluate(expr)
            return AG.ToolResult(text=f"{expr} = {value}" if value is not None
                                 else "I can only work out plain arithmetic.")

        def create_reminder(args):
            what, when = str(args.get("text") or "").strip(), str(args.get("when") or "").strip()
            if not what:
                return AG.ToolResult(text="What should the reminder say?")
            with self.lock:
                res = self.engine.capture(f"remind me {when} to {what}".replace("  ", " "))
            if res.due_at:
                action = {"kind": "reminder", "summary": f"{res.note.title} · {fmt_when(res.due_at)}",
                          "noteId": res.note.id}
                return AG.ToolResult(text=f"Reminder set for {fmt_when(res.due_at)}.", action=action)
            action = {"kind": "note", "summary": f"{res.note.title} · {res.topic_path or 'Inbox'}",
                      "noteId": res.note.id}
            return AG.ToolResult(text="Saved as a to-do, but no time was understood.", action=action)

        def move_note(args):
            with self.lock:
                matches = self.engine.resolve_note(str(args.get("note") or ""))
                if not matches:
                    return AG.ToolResult(text="No note matched that.")
                dest = self._collection_path(str(args.get("collection") or "Inbox"))
                self.engine.move_note(matches[0]["id"], dest)
            action = {"kind": "move", "summary": f"{matches[0]['title']} → {dest}",
                      "noteId": matches[0]["id"]}
            return AG.ToolResult(text=f"Moved {matches[0]['title']} to {dest}.", action=action)

        def create_list(args):
            title = _list_name(args.get("title")) or "To-do"
            items = [i.strip(" .") for i in re.split(r"[,;\n]", str(args.get("items") or ""))
                     if i.strip(" .")]
            if not items:
                return AG.ToolResult(text="Which items should the list have?")
            due = str(args.get("due") or "").strip()
            with self.lock:
                res = self.engine.capture(f"{title} list: {', '.join(items)}"
                                          + (f" by {due}" if due else ""))
            _, actions, reply = self._list_reply(res)
            return AG.ToolResult(text=reply.replace("**", ""), action=actions[0])

        def add_to_list(args):
            name, item = _list_name(args.get("list")), str(args.get("item") or "").strip()
            if not name or not item:
                return AG.ToolResult(text="Which list, and what should go on it?")
            when, _ = I.parse_when(str(args.get("when") or ""))
            remind = when.strftime("%Y-%m-%dT%H:%M:%S") if when else None
            with self.lock:
                target = self.engine.add_to_named_list(name, item, remind=remind)
                title = N.get(self.conn, target)["title"]
            extra = f", with a reminder {fmt_when(remind)}" if remind else ""
            return AG.ToolResult(text=f"Added {item} to {title}{extra}.", action={
                "kind": "note", "summary": f"Added to {title}", "noteId": target})

        def pause_reminders(args):
            name = _list_name(args.get("list"))
            until, _ = I.parse_when(str(args.get("until") or ""))
            if until is None:
                return AG.ToolResult(text="Until when? Give a date, like 'monday'.")
            with self.lock:
                target = self.engine.pause_list(name, until.date().isoformat())
                if target is None:
                    return AG.ToolResult(text=f"There's no to-do list called {name}.")
                title = N.get(self.conn, target)["title"]
            day = _day(until.date().isoformat())
            return AG.ToolResult(text=f"Paused {title} until {day}.", action={
                "kind": "note", "summary": f"{title} · paused until {day}", "noteId": target})

        tools = {"search_notes": search_notes, "calculator": calculator,
                 "create_reminder": create_reminder, "move_note": move_note,
                 "create_list": create_list, "add_to_list": add_to_list,
                 "pause_reminders": pause_reminders}
        if web.enabled:
            tools.update(web_search=web_search, read_page=read_page)

        flt, answer = _SourcesFilter(), []
        try:
            for ev in AG.run(tools_model, prompt, tools):
                if ev["type"] == "token":
                    shown = flt.feed(ev["text"])
                    if shown:
                        answer.append(shown)
                        yield {"type": "token", "text": shown}
                elif ev["type"] == "step":
                    if ev["tool"] == "web_search":
                        query = str(ev["args"].get("query") or message)
                        yield {"type": "searching", "query": query}
                    else:
                        yield {"type": "step", "tool": ev["tool"], "label": ev["label"]}
                elif ev["type"] == "tool" and ev["result"].action:
                    actions.append(ev["result"].action)
                    yield {"type": "action", "action": ev["result"].action}
            tail = flt.finish()
            if tail:
                answer.append(tail)
                yield {"type": "token", "text": tail}
        except AG.ModelUnavailable:
            yield from self._offline(picked, briefs, found)
            return
        except Exception as exc:
            if answer:
                yield {"type": "done", "fromNotes": bool(picked), "fromWeb": bool(found)}
            else:
                yield {"type": "error", "message": str(exc)}
            return
        finally:
            tools_model.close()

        said = "".join(answer).lower()
        if flt.cited is None:
            used = [i for i, r in enumerate(picked)
                    if len((r["title"] or "").strip()) >= 3 and r["title"].strip().lower() in said]
            web_used = list(range(min(len(found), 3)))
        else:
            used = [i - 1 for i in flt.cited if 0 < i <= len(picked)]
            web_used = [i - 1 for i in flt.cited_web if 0 < i <= len(found)]
            if found and not web_used and not used and "sources: none" not in said:
                web_used = list(range(min(len(found), 3)))
        yield {"type": "sources", "notes": [briefs[i] for i in used]}
        if found or query is not None:
            yield {"type": "web", "query": query or "",
                   "results": [found[i].brief() for i in web_used]}
        yield {"type": "done", "fromNotes": bool(used) or bool(actions), "fromWeb": bool(web_used)}

    def _search(self, query: str, web: W.WebConfig):
        try:
            return self.searcher(query, web), None
        except W.SearchError as exc:
            return [], str(exc)
        except Exception as exc:
            return [], f"{type(exc).__name__}"

    # -------------------------------------------------------- instructions --
    def _instruction(self, message: str, auto_file: bool):
        """Carry out a request. Returns (sources, actions, reply) or None."""
        e = self.engine
        is_question = message.rstrip().endswith("?")
        if not is_question:
            parsed = I.parse(message)
            if parsed.pause_target and e.find_list(parsed.pause_target) is None:
                return ([], [], f"I couldn't find a to-do list called “{parsed.pause_target}”.")
            if {"phrase:list", "phrase:add-to-list", "phrase:pause"} & set(parsed.matched):
                return self._list_reply(e.capture(message, auto_file=auto_file))
        if _REMIND.match(message) and not is_question and not _REMIND_QUESTION.match(message):
            text = _SET_REMINDER.sub("remind me to ", message, count=1)
            res = e.capture(text, auto_file=auto_file)
            title = res.note.title
            if res.due_at:
                when = fmt_when(res.due_at)
                return ([], [{"kind": "reminder", "summary": f"{title} · {when}",
                              "noteId": res.note.id}],
                        f"Done. I'll remind you **{when}**.")
            return ([], [{"kind": "note", "summary": f"{title} · {res.topic_path or 'Inbox'}",
                          "noteId": res.note.id}],
                    f"I saved **{title}** as a to-do, but I couldn't tell when. "
                    "Open it to pick a time.")

        if _APPEND.match(message):
            res = e.capture(message, auto_file=auto_file)
            if res.appended_to:
                return ([self._brief(res.appended_to)],
                        [{"kind": "note", "summary": f"Added to {res.note.title}",
                          "noteId": res.appended_to}],
                        f"Added it to **{res.note.title}**.")
            return ([], [{"kind": "note", "summary": f"{res.note.title} · {res.topic_path or 'Inbox'}",
                          "noteId": res.note.id}],
                    "I couldn't find that note, so I saved this as a new one.")

        m = _MOVE.match(message)
        if m:
            matches = e.resolve_note(m["what"])
            if not matches:
                return ([], [], f"I couldn't find a note called “{m['what']}”. "
                                "Try the exact title?")
            target = matches[0]
            dest = self._collection_path(m["dest"])
            e.move_note(target["id"], dest)
            return ([self._brief(target["id"])],
                    [{"kind": "move", "summary": f"{target['title']} → {dest}",
                      "noteId": target["id"]}],
                    f"Moved **{target['title']}** to **{dest}**.")

        m = _SAVE.match(message)
        if m and not is_question:
            res = e.capture(m["text"].strip(), auto_file=auto_file)
            where = res.topic_path or "Inbox"
            return ([], [{"kind": "note", "summary": f"{res.note.title} · {where}",
                          "noteId": res.note.id}],
                    f"Saved it as a note in **{where}**.")
        return None

    def _list_reply(self, res):
        """(sources, actions, reply) for a capture that made or changed a to-do list."""
        note = res.note
        if res.paused_until:
            when = _day(res.paused_until)
            return ([self._brief(note.id)],
                    [{"kind": "note", "summary": f"{note.title} · paused until {when}",
                      "noteId": note.id}],
                    f"I won't check in about **{note.title}** until **{when}**.")
        if res.appended_to:
            extra = f", with a reminder **{fmt_when(res.due_at)}**" if res.due_at else ""
            return ([self._brief(res.appended_to)],
                    [{"kind": "note", "summary": f"Added to {note.title}",
                      "noteId": res.appended_to}],
                    f"Added it to **{note.title}**{extra}.")
        count = res.list_items
        due = f", due **{fmt_when(res.due_at)}**" if res.due_at else ""
        return ([], [{"kind": "note", "summary": f"{note.title} · {count} items",
                      "noteId": note.id}],
                f"Made the to-do list **{note.title}** with {count} "
                f"item{'' if count == 1 else 's'}{due}.")

    def _collection_path(self, spoken: str) -> str:
        spoken = spoken.strip().strip("\"'“”").rstrip(".!?")
        low = spoken.lower()
        for row in T.all_active(self.conn):
            path = T.path_of(self.conn, row["id"]) or ""
            if low in (path.lower(), row["name"].lower()):
                return path
        return " ".join(_cap(w) for w in spoken.split()) or "Inbox"

    def _brief(self, note_id: str) -> dict:
        row = N.get(self.conn, note_id)
        return {"id": note_id, "title": row["title"] if row else "",
                "topic": T.path_of(self.conn, N.primary_topic(self.conn, note_id))}

    # ------------------------------------------------------------ question --
    def _retrieve(self, message: str) -> list:
        """The notes worth showing the model, best first, without repeats."""
        conn, picked, seen = self.conn, [], set()

        def add(rows):
            for row in rows:
                if row["id"] not in seen and len(picked) < MAX_CONTEXT:
                    seen.add(row["id"])
                    picked.append(row)

        if _ABOUT_DUE.search(message):
            add([r for r in N.agenda(conn) if r["status"] != "done"][:10])
        if _ABOUT_INBOX.search(message):
            add(N.inbox(conn)[:8])
        if _ABOUT_RECENT.search(message):
            add(conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT 6").fetchall())

        # A note named in the question is relevant whatever the vectors say.
        low = message.lower()
        add(r for r in conn.execute("SELECT * FROM notes")
            if len((r["title"] or "").strip()) >= 4
            and (r["title"] or "").strip().lower() in low)

        # Search always returns its top hits, even for "what's the capital of
        # Japan?". Only notes that are actually close to the question go to
        # the model: above the measured floor, and near the best match.
        vec = self.engine._embed_one(message)
        floor = getattr(self.engine.cfg, "chat_floor", 0.45)
        scored = []
        for hit in S.search(conn, message, vec, limit=MAX_NOTES * 2):
            note_vec = N.vector(conn, hit.note_id)
            scored.append((cosine(vec, note_vec) if note_vec else 0.0, hit.note_id))
        best = max((sim for sim, _ in scored), default=0.0)
        close = [note_id for sim, note_id in sorted(scored, reverse=True)
                 if sim >= max(floor, best - 0.15)][:MAX_NOTES]
        add(r for r in (N.get(conn, i) for i in close) if r is not None)
        return picked

    def _notes_block(self, picked: list) -> str:
        """The picked notes as the model reads them. Needs the lock (DB reads)."""
        lines = []
        for i, row in enumerate(picked, 1):
            topic = T.path_of(self.conn, N.primary_topic(self.conn, row["id"])) or "Inbox"
            meta = [f"collection: {topic}", f"kind: {row['kind']}"]
            if row["due_at"]:
                meta.append(f"due: {fmt_when(row['due_at'])}"
                            + (" (reminder on)" if row["remind_at"] else ""))
            if row["status"] == "done":
                meta.append("done")
            meta.append(f"saved: {fmt_when(row['created_at'])}")
            body = " ".join((row["body"] or "").split())[:EXCERPT_CHARS]
            lines.append(f"[{i}] {row['title']} ({'; '.join(meta)})\n{body}")
        return "\n\n".join(lines) if lines else "(no notes matched)"

    def _prompt(self, message: str, notes: str, history: list[dict], web: W.WebConfig,
                found: list | None = None, failed: str | None = None,
                query: str | None = None, must_say_unknown: bool = False) -> list[dict]:
        # Once results are in, the model doesn't get to ask for more.
        searched = query is not None
        rule = WEB_OFF if not web.enabled else ("" if searched else WEB_ON)
        system = SYSTEM_PROMPT.format(today=_today(), web_rule=rule)

        parts = [f"My notes:\n\n{notes}"]
        if found:
            parts.append(f'Web results for "{query}":\n\n{W.as_context(found)}')
        elif searched:
            why = f" ({failed})" if failed else ""
            parts.append(f'I searched the web for "{query}" but found nothing useful{why}. '
                         "Say you couldn't find it. Don't guess.")
        if must_say_unknown:
            parts.append("This question is about recent or changing facts, and web "
                         "search is off. Don't answer from memory.")
        parts.append(f"Question: {message}")

        messages = [{"role": "system", "content": system}]
        for turn in history[-HISTORY_TURNS:]:
            role = turn.get("role")
            content = str(turn.get("content") or "")[:1500]
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": "\n\n".join(parts)})
        return messages

    def _answer(self, picked: list, briefs: list[dict], prompt: list[dict],
                found: list, allow_search: bool, query: str | None = None):
        """Stream one answer. Returns a search query if the model asked for
        the web instead of answering (only when `allow_search`)."""
        model = self.model or OllamaChat(think=self.think)
        ask = _SearchRequest() if allow_search else None
        flt = _SourcesFilter()
        answer = []

        def out(text):
            if ask is not None:
                text = ask.feed(text)
            return flt.feed(text) if text else ""

        try:
            for piece in model.stream(prompt):
                shown = out(piece)
                if ask is not None and ask.query:
                    break                                   # it wants the web
                if shown:
                    answer.append(shown)
                    yield {"type": "token", "text": shown}
            if ask is not None and not ask.query:
                rest = ask.finish()
                if rest:
                    shown = flt.feed(rest)
                    if shown:
                        answer.append(shown)
                        yield {"type": "token", "text": shown}
            if ask is not None and ask.query:
                return ask.query
            tail = flt.finish()
            if tail:
                answer.append(tail)
                yield {"type": "token", "text": tail}
        except ModelUnavailable:
            yield from self._offline(picked, briefs, found)
            return None
        except Exception as exc:
            if answer:       # half an answer is still worth keeping
                yield {"type": "done", "fromNotes": bool(picked), "fromWeb": bool(found)}
            else:
                yield {"type": "error", "message": str(exc)}
            return None
        finally:
            model.close()

        said = "".join(answer).lower()
        if flt.cited is None:
            # Small models sometimes skip the sources line. The prompt asks for
            # note titles in bold, so the notes it names are the ones it used.
            used = [i for i, r in enumerate(picked)
                    if len((r["title"] or "").strip()) >= 3
                    and r["title"].strip().lower() in said]
            web_used = list(range(min(len(found), 3)))
        else:
            used = [i - 1 for i in flt.cited if 0 < i <= len(picked)]
            web_used = [i - 1 for i in flt.cited_web if 0 < i <= len(found)]
            if found and not web_used and not used and "sources: none" not in said:
                web_used = list(range(min(len(found), 3)))
        yield {"type": "sources", "notes": [briefs[i] for i in used]}
        if query is not None:
            yield {"type": "web", "query": query,
                   "results": [found[i].brief() for i in web_used]}
        yield {"type": "done", "fromNotes": bool(used), "fromWeb": bool(web_used)}
        return None

    def _offline(self, picked: list, briefs: list[dict], found: list) -> Iterator[dict]:
        """No model: still point at the notes (and pages) search found."""
        if not picked and not found:
            yield {"type": "error", "message": "The local AI isn't running."}
            return
        lines = "\n".join(f"- **{r['title']}**" for r in picked[:MAX_NOTES])
        lines += "".join(f"\n- [{r.title}]({r.url})" for r in found[:3])
        text = ("The AI on this computer isn't running right now, so I can't "
                f"write an answer. These look related:\n\n{lines.strip()}")
        for piece in _words(text):
            yield {"type": "token", "text": piece}
        yield {"type": "sources", "notes": briefs[:MAX_NOTES]}
        yield {"type": "done", "fromNotes": bool(picked), "fromWeb": bool(found)}
