"""Letting the model use tools: search notes, search the web, read a page,
do exact maths, set a reminder, move a note.

Newer local models (Qwen3.5, Gemma 4) can call tools natively through
Ollama's `tools` field: instead of guessing, they ask for what they need, read
the result, and carry on. That is more reliable than the text markers the app
used before ("SEARCH: ..."), which remain as the fallback for models without
tool support (gemma3, the DeepSeek-R1 distills).

`run` drives the loop and yields events the chat stream can pass on:
    {"type": "token", "text": ...}            part of the final answer
    {"type": "step", "tool": ..., "label": ...} a tool is being used
    {"type": "tool", "tool": ..., "result": ToolResult}   (for the caller)
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterator, Protocol

MAX_STEPS = 4


def _fn(name: str, description: str, **params: str) -> dict:
    required = [k for k in params if not k.endswith("_")]
    props = {k.rstrip("_"): {"type": "string", "description": v} for k, v in params.items()}
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props,
                       "required": [k.rstrip("_") for k in required]}}}


SPECS = {
    "search_notes": _fn(
        "search_notes",
        "Search the user's own notes (plans, lists, reminders, saved links, people, "
        "anything they wrote down). Use it whenever the question is about the user's "
        "own things.",
        query="What to look for, in a few words."),
    "web_search": _fn(
        "web_search",
        "Search the web. Use it for recent or changing facts you can't be sure of: "
        "news, results, winners, prices, releases, weather, who holds a job now.",
        query="A short web search query."),
    "read_page": _fn(
        "read_page",
        "Read a web page (usually one found by web_search) to get the details.",
        url="The page's full URL."),
    "calculator": _fn(
        "calculator",
        "Exact arithmetic. Always use it for sums instead of working them out.",
        expression="The sum, like '1847 * 23' or '15% of 2400'."),
    "create_reminder": _fn(
        "create_reminder",
        "Set a reminder or to-do for the user.",
        text="What to be reminded about.",
        when_="When, in plain words, like 'tomorrow at 10am' or 'on the 1st'."),
    "move_note": _fn(
        "move_note",
        "Move one of the user's notes to a collection (folder).",
        note="The note's title or a few words from it.",
        collection="The collection to move it to."),
}

STEP_LABELS = {
    "search_notes": "Searching your notes for “{query}”",
    "web_search": "Searching the web for “{query}”",
    "read_page": "Reading {url}",
    "calculator": "Working out {expression}",
    "create_reminder": "Setting a reminder",
    "move_note": "Moving “{note}”",
}


@dataclass
class ToolResult:
    text: str                                   # what the model reads back
    notes: list = field(default_factory=list)   # note briefs it found
    web: list = field(default_factory=list)     # WebResults it found
    action: dict | None = None                  # something it changed (with Undo)


def label(tool: str, args: dict) -> str:
    template = STEP_LABELS.get(tool, "Using {tool}")
    try:
        text = template.format(tool=tool, **{k: str(v)[:60] for k, v in args.items()})
    except (KeyError, IndexError):
        text = template.split("{")[0].strip() or f"Using {tool}"
    return text


# --------------------------------------------------------------- the model ----
class ToolModel(Protocol):
    def turn(self, messages: list[dict], tools: list[dict]) -> Iterator[tuple[str, object]]:
        """Yields ("token", text) while answering and ("calls", [...]) at the end."""
    def close(self) -> None: ...


class ModelUnavailable(RuntimeError):
    pass


class OllamaTools:
    """One streamed chat turn with tools, from the bundled Ollama."""

    def __init__(self, model: str, host: str | None = None, timeout: int = 180,
                 think: bool = False, temperature: float = 0.3):
        from .paths import OLLAMA_HOST
        self.model = model
        self.host = (host or OLLAMA_HOST).rstrip("/")
        self.timeout = timeout
        self.think = think
        self.temperature = temperature
        self._resp = None

    def turn(self, messages, tools):
        from .runtime import think_option
        payload = {"model": self.model, "messages": messages, "stream": True,
                   "keep_alive": -1, "options": {"temperature": self.temperature},
                   **think_option(self.model, self.think)}
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            f"{self.host}/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            self._resp = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode("utf-8", "replace")[:300]) from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            raise ModelUnavailable(str(exc)) from exc
        calls = []
        try:
            for line in self._resp:
                if not line.strip():
                    continue
                data = json.loads(line)
                if data.get("error"):
                    raise RuntimeError(data["error"])
                msg = data.get("message") or {}
                if msg.get("tool_calls"):
                    calls.extend(msg["tool_calls"])
                if msg.get("content"):
                    yield "token", msg["content"]
                if data.get("done"):
                    break
        finally:
            self.close()
        if calls:
            yield "calls", calls

    def close(self):
        resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


# ---------------------------------------------------------------- the loop ----
def _args(call: dict) -> dict:
    args = (call.get("function") or {}).get("arguments") or {}
    if isinstance(args, str):                    # some models send JSON text
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"input": args}
    return args if isinstance(args, dict) else {}


def run(model: ToolModel, messages: list[dict], tools: dict[str, Callable[[dict], ToolResult]],
        max_steps: int = MAX_STEPS) -> Iterator[dict]:
    """Let the model answer, using tools for up to `max_steps` rounds.

    `messages` is extended in place with the tool calls and their results,
    so the caller can see the whole exchange afterwards.
    """
    specs = [SPECS[name] for name in tools if name in SPECS]
    for step in range(max_steps + 1):
        allowed = specs if step < max_steps else []      # last round must answer
        calls, said = [], []
        for kind, value in model.turn(messages, allowed):
            if kind == "token":
                said.append(value)
                yield {"type": "token", "text": value}
            elif kind == "calls":
                calls = list(value)
        if not calls:
            return
        messages.append({"role": "assistant", "content": "".join(said), "tool_calls": calls})
        for call in calls:
            name = (call.get("function") or {}).get("name") or ""
            args = _args(call)
            yield {"type": "step", "tool": name, "args": args, "label": label(name, args)}
            fn = tools.get(name)
            try:
                result = fn(args) if fn else ToolResult(text=f"There is no tool called {name}.")
            except Exception as exc:                     # a broken tool mustn't end the chat
                result = ToolResult(text=f"That didn't work: {type(exc).__name__}: {exc}")
            yield {"type": "tool", "tool": name, "result": result}
            messages.append({"role": "tool", "content": result.text, "tool_name": name})
