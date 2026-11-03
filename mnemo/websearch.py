"""Looking things up on the web, for questions the local model can't know.

The model's own knowledge stops at its training date and it will happily
invent the rest ("Fnatic won Valorant Champions 2026"). So the AI is told to
search instead of guessing, and its answer is built from what comes back.

Privacy: only the search words leave the computer -- never note contents.
The user can turn search off, and every answer that used the web says so and
links its sources.

Providers, all through official APIs:

    builtin   Wikipedia search + page intros, and DuckDuckGo instant answers.
              No account, no key. Strong on facts and events with a wiki page;
              weak on breaking news.
    brave     Brave Search API with the user's own key: full web results.
    searxng   The user's own SearXNG instance: full results, nothing logged
              by a third party.

Scraping search result pages is deliberately not offered: those sites block
it, and getting around their bot checks isn't something this app should do.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

USER_AGENT = "NotesApp/0.1 (personal notes app running on the user's own computer)"
TIMEOUT = 12
MAX_RESULTS = 5
TEXT_CHARS = 1400


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str = ""
    text: str = ""                     # longer readable text when we have it

    @property
    def site(self) -> str:
        host = urllib.parse.urlparse(self.url).hostname or ""
        return host[4:] if host.startswith("www.") else host

    def brief(self) -> dict:
        return {"title": self.title, "url": self.url, "site": self.site}


@dataclass
class WebConfig:
    mode: str = "auto"                 # auto | off
    provider: str = "builtin"          # builtin | brave | searxng
    key: str = ""
    url: str = ""

    @classmethod
    def from_payload(cls, data) -> "WebConfig":
        if not isinstance(data, dict):
            return cls()
        return cls(mode=str(data.get("mode") or "auto"),
                   provider=str(data.get("provider") or "builtin"),
                   key=str(data.get("key") or "").strip(),
                   url=str(data.get("url") or "").strip())

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


class SearchError(RuntimeError):
    pass


# ----------------------------------------------------------- http helper ----
def _get_json(url: str, headers: dict | None = None) -> dict:
    from .enrich import _tls_context
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json",
                                               **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_tls_context()) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise SearchError(f"{exc.code} from {urllib.parse.urlparse(url).hostname}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise SearchError(f"couldn't reach {urllib.parse.urlparse(url).hostname}") from exc
    except json.JSONDecodeError as exc:
        raise SearchError("the search service sent something unreadable") from exc


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return " ".join(text.replace("&quot;", '"').replace("&amp;", "&").split())


# ------------------------------------------------------------- providers ----
def wikipedia(query: str, limit: int = 3) -> list[WebResult]:
    api = "https://en.wikipedia.org/w/api.php?"
    found = _get_json(api + urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": limit, "format": "json", "utf8": 1}))
    hits = (found.get("query") or {}).get("search") or []
    if not hits:
        return []
    titles = [h["title"] for h in hits]
    pages = _get_json(api + urllib.parse.urlencode({
        "action": "query", "prop": "extracts", "exintro": 1, "explaintext": 1,
        "redirects": 1, "titles": "|".join(titles), "format": "json", "utf8": 1}))
    intro = {p.get("title"): p.get("extract") or ""
             for p in ((pages.get("query") or {}).get("pages") or {}).values()}
    out = []
    for h in hits:
        title = h["title"]
        out.append(WebResult(
            title=title,
            url="https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            snippet=_clean(h.get("snippet", "")),
            text=" ".join((intro.get(title) or "").split())[:TEXT_CHARS]))
    return out


def ddg_instant(query: str) -> list[WebResult]:
    """DuckDuckGo's official Instant Answer API: one summary, when it has one."""
    data = _get_json("https://api.duckduckgo.com/?" + urllib.parse.urlencode({
        "q": query, "format": "json", "no_html": 1, "skip_disambig": 1}))
    text = data.get("Answer") or data.get("AbstractText") or ""
    url = data.get("AbstractURL") or ""
    if not text or not url:
        return []
    return [WebResult(title=data.get("Heading") or query, url=url,
                      snippet=text[:300], text=text[:TEXT_CHARS])]


def brave(query: str, key: str) -> list[WebResult]:
    if not key:
        raise SearchError("add your Brave Search API key in Settings")
    data = _get_json("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": MAX_RESULTS}), headers={"X-Subscription-Token": key})
    return [WebResult(title=_clean(r.get("title", "")), url=r.get("url", ""),
                      snippet=_clean(r.get("description", "")))
            for r in ((data.get("web") or {}).get("results") or []) if r.get("url")]


def searxng(query: str, base: str) -> list[WebResult]:
    if not base:
        raise SearchError("add your SearXNG address in Settings")
    data = _get_json(base.rstrip("/") + "/search?" + urllib.parse.urlencode(
        {"q": query, "format": "json"}))
    return [WebResult(title=_clean(r.get("title", "")), url=r.get("url", ""),
                      snippet=_clean(r.get("content", "")))
            for r in (data.get("results") or []) if r.get("url")]


def _read_pages(results: list[WebResult], count: int = 2) -> None:
    """Fill in readable text for the top results that only have a snippet."""
    from .enrich import fetch_url

    todo = [r for r in results[:count] if not r.text]

    def read(r: WebResult):
        page = fetch_url(r.url)
        if page.ok and page.text:
            r.text = " ".join(page.text.split())[:TEXT_CHARS]

    if todo:
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            list(pool.map(read, todo))


def search(query: str, cfg: WebConfig) -> list[WebResult]:
    """The best few results for `query` from the configured provider."""
    query = " ".join((query or "").split())[:200]
    if not query:
        return []
    if cfg.provider == "brave":
        results = brave(query, cfg.key)
        _read_pages(results)
    elif cfg.provider == "searxng":
        results = searxng(query, cfg.url)
        _read_pages(results)
    else:
        results, errors = [], []
        for provider in (ddg_instant, wikipedia):
            try:
                results += provider(query)
            except SearchError as exc:
                errors.append(str(exc))
        if not results and errors:
            raise SearchError(errors[0])
    seen, unique = set(), []
    for r in results:
        if r.url not in seen:
            seen.add(r.url)
            unique.append(r)
    return unique[:MAX_RESULTS]


def as_context(results: list[WebResult]) -> str:
    """Web results as the model sees them: [W1] title (site) then text."""
    return "\n\n".join(
        f"[W{i}] {r.title} ({r.site})\n{r.text or r.snippet}"
        for i, r in enumerate(results, 1))


# ------------------------------------------------------ writing a query ----
QUERY_SYSTEM = """You turn a question into a short web search query, the way \
someone would type it into Wikipedia or a search engine. Today is {today}.
Expand nicknames, abbreviations and short years: "valorant champs 26" becomes \
"Valorant Champions 2026", "wc final" becomes "World Cup final".
Use the conversation for context, so "and who came second?" keeps the topic.
Reply in JSON: {{"query": "..."}}"""


def write_query(json_model, question: str, history=()) -> str:
    """A good search query for `question`, written by the model."""
    today = datetime.now().strftime("%d %B %Y")
    messages = [{"role": "system", "content": QUERY_SYSTEM.format(today=today)}]
    for turn in list(history)[-4:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": str(turn["content"])[:600]})
    messages.append({"role": "user", "content": question})
    schema = {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"]}
    raw = json_model.complete(messages, schema)
    try:
        query = str(json.loads(raw).get("query") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        query = ""
    return " ".join(query.split())[:200] or question


# ------------------------------------------------- when a search is a must ----
_LIVE = re.compile(
    r"\b(latest|newest|current(?:ly)?|today'?s?|tonight|right now|this (?:week|month|year)|"
    r"recent(?:ly)?|news|breaking|live score|score|who won|winners?|won the|champions?|"
    r"results? of|standings|price of|cost of|stock|weather|forecast|release date|"
    r"released|launch(?:ed)?|announced|ceo of|president of|prime minister|"
    r"search (?:the )?(?:web|internet)|look (?:it )?up|google|online|on the internet)\b", re.I)


def needs_web(text: str, now: datetime | None = None) -> bool:
    """Whether a question is about things a 2024-trained model can't know.

    A recent year ("2025", "2026") or words about live / changing facts mean
    the model must not answer from memory.
    """
    now = now or datetime.now()
    years = [int(y) for y in re.findall(r"\b(20\d\d)\b", text or "")]
    if any(y >= now.year - 1 for y in years):
        return True
    m = re.search(r"\b(?:champs|worlds|masters)\s*'?(\d\d)\b", text or "", re.I)
    if m and 2000 + int(m.group(1)) >= now.year - 1:
        return True
    return bool(_LIVE.search(text or ""))
