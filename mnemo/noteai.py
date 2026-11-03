"""The AI box inside a note: answer a question, or change the note.

The model reads the note and the request and picks one of three actions:

    answer   reply only; the note is left alone ("what is 23 x 17?")
    add      new Markdown to insert ("write the names of colours here")
    replace  the whole note rewritten ("make this a checklist")

It answers in JSON constrained by a schema, so the choice is always one of
those three. Nothing is written here: the page applies the change to what it
is showing and saves it like any edit, which keeps Undo exact even when the
user has typed since.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Protocol

from . import calc
from . import websearch as W

MAX_BODY = 7000          # longer notes are trimmed in the prompt...
HISTORY_TURNS = 6        # ...and can then only be answered about or added to

SYSTEM = """You are the AI inside a note in a notes app. The user is looking at the note \
and typed a request in the box under it. Today is {today}.

Decide what to do and reply in JSON:
- "answer": the user asked a question or wants to be told something, and the note \
should NOT change. Put the answer in "reply". Leave "text" empty.
- "add": the user wants new content written INTO the note ("write the names of colours \
here", "add a packing list", "put the steps below"). Put ONLY the new content in "text", \
as Markdown. Put a short confirmation in "reply", like "Added 7 colours."
- "replace": the user wants the note's existing text changed (rewrite, fix spelling, \
turn into a checklist, translate, shorten, sort, remove something). Put the COMPLETE new \
note in "text", keeping everything they didn't ask to change. Put a short confirmation \
in "reply".

A question is "answer" even when asked inside a note, unless the user says to write, \
put, add, list or note it down here.

Your own knowledge stops in early 2024. Never guess about anything after that, or \
anything you aren't sure of: events, results, winners, prices, releases, who holds a \
job now. "I don't know" is always better than a wrong answer.
{web_rule}
"position" matters only for "add": "cursor" when they say here / below this / at this \
point, "start" for the top or beginning, otherwise "end".
Write lists with "- ", checklists with "- [ ] ", headings with "## ". Never wrap "text" \
in code fences. Keep "reply" short, warm and plain. If "Calculator" lines are given, \
they are exact: use those numbers.

Examples:
Request: what is 15% of 80? -> {{"action": "answer", "reply": "15% of 80 is 12.", "text": "", "position": "end"}}
Request: write the days of the week here -> {{"action": "add", "reply": "Added the seven days of the week.", "text": "- Monday\\n- Tuesday\\n- Wednesday\\n- Thursday\\n- Friday\\n- Saturday\\n- Sunday", "position": "cursor"}}
Request: make this a checklist -> {{"action": "replace", "reply": "Turned it into a checklist.", "text": "- [ ] Buy milk\\n- [ ] Call Riya", "position": "end"}}"""


WEB_ON = """- "search": you need the web first (recent events, results, prices, facts you \
aren't sure of). Put a short web search query in "text"; leave "reply" empty. You'll \
then get the results and decide again."""
WEB_GIVEN = """Web results are given below: use them, name the page you used in "reply", \
and don't search again. If they don't contain the answer, say so."""
WEB_OFF = """Web search is turned off. If you don't know, say so and mention that turning \
on web search in Settings would let you look it up."""


def _schema(can_replace: bool, can_search: bool = False) -> dict:
    actions = ["answer", "add", "replace"] if can_replace else ["answer", "add"]
    if can_search:
        actions = actions + ["search"]
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": actions},
            "reply": {"type": "string"},
            "text": {"type": "string"},
            "position": {"type": "string", "enum": ["end", "cursor", "start"]},
        },
        "required": ["action", "reply", "text", "position"],
    }


class ModelUnavailable(RuntimeError):
    pass


class JsonModel(Protocol):
    def complete(self, messages: list[dict], schema: dict) -> str: ...


class OllamaJson:
    """One non-streaming completion constrained to a JSON schema."""

    def __init__(self, model: str | None = None, host: str | None = None,
                 timeout: int = 180, think: bool = False):
        from .models import ROUTER_MODEL
        from .paths import OLLAMA_HOST
        self.model = model or ROUTER_MODEL
        self.host = (host or OLLAMA_HOST).rstrip("/")
        self.timeout = timeout
        self.think = think

    def complete(self, messages: list[dict], schema: dict) -> str:
        from .runtime import think_option
        payload = {"model": self.model, "messages": messages, "format": schema,
                   "stream": False, "keep_alive": -1,
                   "options": {"temperature": 0.2},
                   **think_option(self.model, self.think)}
        request = urllib.request.Request(
            f"{self.host}/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise ModelUnavailable(str(exc)) from exc
        if body.get("error"):
            raise RuntimeError(body["error"])
        return body["message"]["content"]


def _today() -> str:
    d = datetime.now()
    return f"{d.strftime('%A')} {d.day} {d.strftime('%B')} {d.year}"


# Requests that mean "take something out", where lines going missing is the point.
_REMOVAL = re.compile(
    r"\b(remove|delete|drop|shorten|shorter|summari[sz]e|condense|trim|cut|clear|"
    r"translate|rewrite|replace|only keep|keep only|merge|dedupe|tidy)\b", re.I)
_MARKER = re.compile(r"^\s*(?:#{1,6}\s+|[-*+]\s+\[[ xX]\]\s+|[-*+]\s+|\d+[.)]\s+|>\s?)")


def _norm(line: str) -> str:
    """A line without its list / heading / checkbox markers, for comparing."""
    return " ".join(_MARKER.sub("", line).split()).lower()


def _dropped(old: str, new: str) -> list[str]:
    """Lines of the note that are missing from a rewrite of it."""
    kept = {_norm(line) for line in new.splitlines()}
    return [line.strip() for line in old.splitlines()
            if len(_norm(line)) >= 2 and _norm(line) not in kept]


def _without_repeats(added: str, body: str) -> str:
    """Drop lines the model copied from the note into text meant to be added."""
    existing = {_norm(line) for line in body.splitlines() if len(_norm(line)) >= 2}
    lines = [line for line in added.splitlines()
             if not (len(_norm(line)) >= 2 and _norm(line) in existing)]
    return "\n".join(lines).strip("\n")


def _unfence(text: str) -> str:
    """Drop a ``` fence the model wrapped the whole thing in anyway."""
    m = re.fullmatch(r"\s*```[\w-]*\n(.*?)\n?```\s*", text, re.S)
    return m.group(1) if m else text


def build_messages(title: str, body: str, request: str, history: list[dict],
                   cursor_line: str | None, web_on: bool = False,
                   found: list | None = None, query: str | None = None,
                   failed: str | None = None) -> tuple[list[dict], bool]:
    can_replace = len(body) <= MAX_BODY
    shown = body if can_replace else body[:MAX_BODY] + "\n[… the rest of the note is not shown]"
    parts = [f"Note title: {title or 'Untitled'}",
             f"Note (Markdown):\n<<<\n{shown}\n>>>" if body.strip() else "The note is empty."]
    if cursor_line is not None:
        parts.append(f'The cursor is on this line: "{cursor_line}"')
    worked = calc.facts(request)
    if worked:
        parts.append("Calculator:\n" + "\n".join(worked))
    if found:
        parts.append(f'Web results for "{query}":\n\n{W.as_context(found)}')
    elif query is not None:
        why = f" ({failed})" if failed else ""
        parts.append(f'I searched the web for "{query}" but found nothing useful{why}. '
                     "Say you couldn't find it. Don't guess.")
    elif not web_on and W.needs_web(request):
        parts.append("This request is about recent or changing facts and web search is "
                     "off, so you can't know the answer. Don't answer from memory: say you "
                     "don't know and that turning on web search would let you look it up.")
    parts.append(f"Request: {request}")

    rule = WEB_OFF if not web_on else (WEB_GIVEN if query is not None else WEB_ON)
    messages = [{"role": "system", "content": SYSTEM.format(today=_today(), web_rule=rule)}]
    for turn in history[-HISTORY_TURNS:]:
        role, content = turn.get("role"), str(turn.get("content") or "")[:1200]
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": "\n\n".join(parts)})
    return messages, can_replace


def respond(title: str, body: str, request: str, history: list[dict] | None = None,
            cursor_line: str | None = None, model: JsonModel | None = None,
            web: W.WebConfig | None = None, searcher=None) -> dict:
    """Decide and produce {action, reply, text, position, web?} for one request."""
    request = (request or "").strip()
    if not request:
        return {"action": "answer", "reply": "Ask me something first.", "text": "",
                "position": "end"}
    web = web or W.WebConfig(mode="off")
    searcher = searcher or W.search
    if model is None:
        from . import models
        model = OllamaJson(think=models.ACTIVE.thinks)    # "Thorough" reasons first
    history = history or []
    found, query, failed = [], None, None

    def look_up(q):
        try:
            return searcher(q, web), None
        except W.SearchError as exc:
            return [], str(exc)
        except Exception as exc:
            return [], type(exc).__name__

    # A request about recent or changing facts is looked up before the model
    # answers at all: left to itself, a small model guesses.
    if web.enabled and W.needs_web(request):
        try:
            query = W.write_query(model, request, history)
        except Exception:
            query = request
        found, failed = look_up(query)

    def ask():
        msgs, can_rep = build_messages(title, body or "", request, history, cursor_line,
                                       web.enabled, found, query, failed)
        return msgs, can_rep, model.complete(
            msgs, _schema(can_rep, can_search=web.enabled and query is None))

    messages, can_replace, raw = ask()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"action": "answer", "reply": raw.strip() or "Sorry, I got muddled. Try again?",
                "text": "", "position": "end"}

    if data.get("action") == "search" and web.enabled and query is None:
        query = " ".join(str(data.get("text") or "").split())[:200] or request
        found, failed = look_up(query)
        messages, can_replace, raw = ask()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"action": "answer", "reply": raw.strip()}

    # Small models rewriting a note tend to drop lines they weren't asked to
    # touch (a title above a list, say). One retry, told exactly what went
    # missing, fixes that nearly every time; losing someone's writing is the
    # one mistake this feature must not make.
    if data.get("action") == "replace" and not _REMOVAL.search(request):
        missing = _dropped(body or "", str(data.get("text") or ""))
        if missing:
            retry = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                    "Your new note left out these lines, which I didn't ask you to remove:\n"
                    + "\n".join(f"- {line}" for line in missing[:20])
                    + "\nGive the complete note again with them kept."}]
            try:
                again = json.loads(model.complete(retry, _schema(can_replace)))
                if again.get("action") == "replace" and len(
                        _dropped(body or "", str(again.get("text") or ""))) < len(missing):
                    # Its second reply tends to describe the retry ("kept the
                    # title"); the first one describes the change the user asked for.
                    data = {**again, "reply": data.get("reply") or again.get("reply")}
            except (json.JSONDecodeError, RuntimeError):
                pass

    action = data.get("action") if data.get("action") in ("answer", "add", "replace") else "answer"
    if action == "replace" and not can_replace:
        action = "add"
    text = _unfence(str(data.get("text") or "")).strip("\n")
    reply = str(data.get("reply") or "").strip()
    position = data.get("position") if data.get("position") in ("end", "cursor", "start") else "end"

    if action == "add":
        text = _without_repeats(text, body or "")
    if action != "answer" and not text.strip():
        action = "answer"            # nothing to write, so it can only be an answer
    if action == "answer":
        reply = reply or text.strip() or "I'm not sure."
        text = ""
    elif not reply:
        reply = "Updated your note." if action == "replace" else "Added it to your note."
    out = {"action": action, "reply": reply, "text": text, "position": position}
    if query is not None:
        out["web"] = {"query": query, "results": [r.brief() for r in found[:3]],
                      "error": failed}
    return out
