"""Understanding dictated input.

The capture box is driven by voice most of the time, which rules out the symbol
syntax the CLI uses: you cannot dictate `@gaming` or `!task` -- saying "at
gaming" just produces those words. So intent has to be read out of ordinary
speech instead:

    "put this in my gaming notes: the build is broken"   -> topic Gaming
    "add to the elden ring note that mimic tear helps"   -> append to a note
    "remind me friday at 5 to renew the domain"          -> reminder
    "i need to buy milk tomorrow"                        -> task, due tomorrow

Everything here is deliberately rule-based rather than model-driven: it runs in
under a millisecond, it is deterministic, and it is trivial to see why it did
what it did. The LLM is a fallback for the cases these rules miss, not the first
resort.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
WEEKDAYS.update({name.lower(): i for i, name in enumerate(calendar.day_abbr)})

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "sixty": 60, "a": 1, "an": 1, "half": 0,
}

# Rough times of day, so "tomorrow morning" resolves to something concrete.
DAYPARTS = {"morning": 9, "noon": 12, "midday": 12, "afternoon": 14,
            "evening": 18, "tonight": 20, "night": 20, "midnight": 0}

DEFAULT_HOUR = 9        # a bare date with no time means "morning"


@dataclass
class Intent:
    text: str                              # what remains after directives
    topic: str | None = None               # explicit destination folder
    append_to: str | None = None           # name of an existing note to add to
    kind: str = "note"
    tags: list[str] = field(default_factory=list)
    due_at: datetime | None = None
    remind: bool = False
    recurrence: str | None = None
    url: str | None = None
    matched: list[str] = field(default_factory=list)   # which rules fired
    # "exam prep list: algebra, calculus by 15 nov" -> a new to-do list
    list_title: str | None = None
    list_items: list[str] = field(default_factory=list)
    # "add optics to the exam prep list" -> one more item on an existing list
    append_item: bool = False
    # "don't remind me about exam prep until monday"
    pause_target: str | None = None
    pause_until: date | None = None

    @property
    def is_task(self) -> bool:
        return self.kind == "task"


# --------------------------------------------------------------- times ----
def _at_time(text: str, base: datetime,
             allow_dayparts: bool = True) -> tuple[int, int] | None:
    """Pull an explicit clock time: '5pm', 'at 5', '17:30'.

    Every pattern is fenced so it cannot match inside a longer number. Without
    that, OCR'd text finds times everywhere: the IP address 10.0.4.17 parses as
    4:17, and a version string like 2.30 parses as half past two. Image
    transcripts are full of both, so a note about a failed deploy would quietly
    acquire a 4:17 AM reminder.
    """
    # Not preceded by a digit or dot, not followed by one either.
    m = re.search(r"(?<![\d.:])(?:at\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?(?![\d.:])", text)
    if not m:
        # A dot separator is only a clock if am/pm makes that explicit.
        m = re.search(r"(?<![\d.:])(?:at\s+)?(\d{1,2})\.(\d{2})\s*(am|pm)(?![\d.:])", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            return None
        if m.group(3) == "pm" and hour < 12:
            hour += 12
        if m.group(3) == "am" and hour == 12:
            hour = 0
        return hour, minute

    m = re.search(r"(?<![\d.:])(\d{1,2})\s*(am|pm)(?![\d.:])", text)
    if m:
        hour = int(m.group(1))
        if hour > 12:
            return None
        if m.group(2) == "pm" and hour < 12:
            hour += 12
        if m.group(2) == "am" and hour == 12:
            hour = 0
        return hour, 0

    # "at 5" with no am/pm -- assume the next sensible occurrence, favouring
    # daytime, because "remind me at 5" almost never means 5am.
    m = re.search(r"\bat\s+(\d{1,2})(?![\d.:])\b(?!\s*(?:st|nd|rd|th))", text)
    if m:
        hour = int(m.group(1))
        if hour > 23:
            return None
        if hour <= 7:
            hour += 12
        return hour, 0
    if allow_dayparts:
        for word, hour in DAYPARTS.items():
            if re.search(rf"(?<!last )\b{word}\b", text):
                return hour, 0
    return None


def parse_when(text: str, now: datetime | None = None,
               allow_dayparts: bool = True) -> tuple[datetime | None, str]:
    """Find a time expression and return (when, text_without_it).

    Returns the absolute resolved datetime -- the UI always shows this back, so
    a misparse is obvious immediately rather than silently wrong.
    """
    now = now or datetime.now()
    lowered = text.lower()

    # "the deploy that broke last night" and "we shipped it 3 days ago" are
    # descriptions of the past, not requests for a future reminder.
    if re.search(r"\b(last night|yesterday|\bago\b|earlier today|last week)\b",
                 lowered):
        return None, text

    when: datetime | None = None
    consumed: list[tuple[int, int]] = []

    def take(match: re.Match) -> None:
        consumed.append((match.start(), match.end()))

    clock = _at_time(lowered, now, allow_dayparts)
    hour, minute = clock if clock else (DEFAULT_HOUR, 0)

    # in N minutes/hours/days/weeks
    m = re.search(r"\bin\s+(\d+|[a-z]+)\s+(minute|min|hour|day|week|month)s?\b", lowered)
    if m:
        raw = m.group(1)
        count = int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw, 0)
        if count:
            unit = m.group(2)
            delta = {"minute": timedelta(minutes=count), "min": timedelta(minutes=count),
                     "hour": timedelta(hours=count), "day": timedelta(days=count),
                     "week": timedelta(weeks=count),
                     "month": timedelta(days=30 * count)}[unit]
            when = now + delta
            take(m)

    if when is None:
        # "tonight's build notes" is a title, not a request for 8pm.
        m = re.search(r"\b(today|tonight|tomorrow|day after tomorrow)\b(?!['’]s)",
                      lowered)
        if m:
            word = m.group(1)
            offset = {"today": 0, "tonight": 0, "tomorrow": 1,
                      "day after tomorrow": 2}[word]
            day = now.date() + timedelta(days=offset)
            when = datetime.combine(day, datetime.min.time()).replace(
                hour=20 if word == "tonight" and not clock else hour, minute=minute)
            take(m)

    if when is None:
        m = re.search(r"\b(?:next\s+|this\s+|on\s+)?(" + "|".join(WEEKDAYS) + r")\b",
                      lowered)
        if m:
            target = WEEKDAYS[m.group(1)]
            ahead = (target - now.weekday()) % 7
            if ahead == 0 or "next" in lowered[max(0, m.start() - 6):m.start()]:
                ahead = ahead or 7
            day = now.date() + timedelta(days=ahead)
            when = datetime.combine(day, datetime.min.time()).replace(
                hour=hour, minute=minute)
            take(m)

    if when is None:
        # "3 October", "October 3", "3rd of October"
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" +
                      "|".join(MONTHS) + r")\b", lowered) or \
            re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b",
                      lowered)
        if m:
            groups = m.groups()
            if groups[0].isdigit():
                day_num, month = int(groups[0]), MONTHS[groups[1]]
            else:
                month, day_num = MONTHS[groups[0]], int(groups[1])
            year = now.year + (1 if (month, day_num) <
                               (now.month, now.day) else 0)
            try:
                when = datetime(year, month, day_num, hour, minute)
                take(m)
            except ValueError:
                when = None

    if when is None and clock:
        # A bare time with no date: the next time it comes round.
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        when = candidate
        m = re.search(r"\b(?:at\s+)?\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?\b", lowered)
        if m:
            take(m)

    if when is None:
        return None, text

    when = when.replace(second=0, microsecond=0)
    # Strip the phrase we consumed, plus the clock phrase if it was separate.
    cleaned = text
    for start, end in sorted(consumed, reverse=True):
        cleaned = cleaned[:start] + cleaned[end:]
    if clock:
        cleaned = re.sub(r"\b(?:at\s+)?\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)\b", " ",
                         cleaned, flags=re.I)
        cleaned = re.sub(r"\bat\s+\d{1,2}\b", " ", cleaned, flags=re.I)
        for word in DAYPARTS:
            cleaned = re.sub(rf"\b{word}\b", " ", cleaned, flags=re.I)
    return when, re.sub(r"\s{2,}", " ", cleaned).strip(" ,.:;-")


# ------------------------------------------------------------- phrases ----
TOPIC_PATTERNS = [
    r"\b(?:put|file|save|add|stick|throw)\s+(?:this|it|that)\s+(?:in|into|under|to)\s+"
    r"(?:my\s+)?(?P<topic>[\w /-]+?)(?:\s+(?:notes?|folder|list))?\s*[:,]?\s",
    r"^(?:in|under)\s+(?:my\s+)?(?P<topic>[\w /-]+?)\s*[:,]\s",
    r"\bgoes?\s+(?:in|into|under)\s+(?:my\s+)?(?P<topic>[\w /-]+?)(?:\s+notes?)?\s*[:,]?\s",
]

APPEND_PATTERNS = [
    r"\badd\s+(?:this\s+)?to\s+(?:the\s+|my\s+)?(?P<note>[\w /-]+?)\s+note\b\s*[:,]?\s*(?:that\s+)?",
    r"\bappend\s+(?:this\s+)?to\s+(?:the\s+|my\s+)?(?P<note>[\w /-]+?)\b\s*[:,]?\s*(?:that\s+)?",
    r"\badd\s+(?:this\s+)?to\s+(?:the\s+|my\s+)?(?P<note>[\w /-]+?)\s*[:,]\s*",
]

REMIND_PATTERNS = [
    r"\bremind\s+me\s+(?:to\s+)?",
    r"\bset\s+a?\s*reminder\s+(?:to\s+|for\s+)?",
    r"\bdon'?t\s+let\s+me\s+forget\s+(?:to\s+)?",
]

TASK_PATTERNS = [
    r"\bi\s+need\s+to\b", r"\bi\s+have\s+to\b", r"\bi\s+should\b",
    r"\bneed\s+to\s+remember\s+to\b", r"\btodo\b", r"\bto-do\b",
    r"\bmake\s+sure\s+(?:i|to)\b",
]

# Most specific first. A label of None means "every N <unit>s", read from the
# match. "every monday" loses only its "every": the day itself is the date.
RECUR_PATTERNS = [
    (r"\bevery\s+(?:other|second)\s+day\b", "every 2 days"),
    (r"\bevery\s+(?:other|second)\s+week\b|\bfortnightly\b", "every 2 weeks"),
    (r"\bevery\s+(?:other|second)\s+month\b", "every 2 months"),
    (r"\bevery\s+(\d{1,2})\s+(day|week|month|year)s?\b", None),
    (r"\bevery\s+week\s*days?\b|\b(?:on\s+)?weekdays\b", "weekdays"),
    (r"\bevery\s*day\b|\bdaily\b", "daily"),
    (r"\bevery\s+week\b|\bweekly\b", "weekly"),
    (r"\bevery\s+month\b|\bmonthly\b", "monthly"),
    (r"\bevery\s+year\b|\byearly\b|\bannually\b", "yearly"),
    (r"\bevery\s+(?=(?:" + "|".join(WEEKDAYS) + r")\b)", "weekly"),
]
# "on the 1st", "the 15th": a day of the month, for things that repeat monthly.
DAY_OF_MONTH = re.compile(r"\b(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)?\b|\bon\s+(\d{1,2})(?:st|nd|rd|th)\b",
                          re.IGNORECASE)

URL_RE = re.compile(r"https?://\S+")
SYMBOL_TOPIC = re.compile(r'(?:^|\s)@(?:"([^"]+)"|([A-Za-z0-9][\w/-]*))')
SYMBOL_TAG = re.compile(r"(?:^|\s)#([A-Za-z0-9][\w-]*)")
SYMBOL_TASK = re.compile(r"(?:^|\s)!(task|todo|remind)\b", re.IGNORECASE)

VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")


# ------------------------------------------------------------- lists ----
_LIST_WORD = r"(?:to-?do\s+|todo\s+|check\s*|study\s+|shopping\s+|packing\s+)?list"
LIST_NAMED = re.compile(
    r"^\s*(?:(?:make|create|start|new|add)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:new\s+)?)?"
    + _LIST_WORD + r"\s+(?:for|of|called|named)\s+(?P<title>[^:\n]{1,60}?)\s*[:\n–-]\s*(?P<items>.+)$",
    re.IGNORECASE | re.DOTALL)
LIST_TRAILING = re.compile(
    r"^\s*(?P<title>[^:\n]{1,60}?)\s+" + _LIST_WORD + r"\s*[:\n]\s*(?P<items>.+)$",
    re.IGNORECASE | re.DOTALL)
BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)]|\[[ xX]?\])\s*(?:\[[ xX]?\]\s*)?")
DEADLINE = re.compile(r"\s*,?\s*\b(?:by|due|before|deadline)\s+(?P<when>[^,;]+?)\s*$", re.IGNORECASE)
ADD_TO_LIST = re.compile(
    r"^\s*(?:please\s+)?(?:add|put)\s+(?P<item>.+?)\s+(?:to|on|onto|in|into)\s+(?:the\s+|my\s+)?"
    r"(?P<list>[\w' /-]{1,50}?)\s+" + _LIST_WORD + r"\b\s*(?P<rest>.*)$",
    re.IGNORECASE)
PAUSE = re.compile(
    r"^\s*(?:please\s+)?(?:(?:don'?t|do\s+not|stop)\s+(?:remind(?:ing)?|checking\s+in\s+(?:with|on)|nag(?:ging)?)"
    r"(?:\s+me)?|pause|mute|snooze)\s+(?:about\s+|on\s+|for\s+|reminders?\s+(?:for|about)\s+)?"
    r"(?:the\s+|my\s+)?(?P<target>.+?)\s+(?:until|till|til|before|for\s+now\s+until)\s+(?P<when>.+?)\s*[.!]?$",
    re.IGNORECASE)
LIST_DEADLINE_HOUR = 18     # "by 15 nov" means by that evening


def list_title(raw: str) -> str:
    """'the trip' -> 'Trip'; 'exam prep' -> 'Exam prep'."""
    t = re.sub(r"^(?:the|my|a|an|our)\s+", "", raw.strip(" ,.:"), flags=re.I).strip()
    return t[:1].upper() + t[1:] if t else "To-do list"


def _split_items(raw: str) -> list[str]:
    lines = [l for l in raw.split("\n") if l.strip()]
    if len(lines) > 1:
        parts = lines
    else:
        parts = re.split(r"\s*[,;]\s*|\s+and\s+(?=[^,;]+$)", raw)
    out = []
    for p in parts:
        p = BULLET.sub("", p).strip(" .")
        if p:
            out.append(p[:1].upper() + p[1:] if p[:1].islower() and len(p) > 1 else p)
    return out


def _deadline(items: str, now: datetime | None) -> tuple[str, datetime | None]:
    m = DEADLINE.search(items)
    if not m:
        return items, None
    when, rest = parse_when(m.group("when"), now)
    if when is None or rest.strip(" ,."):
        return items, None
    if not re.search(r"\d{1,2}\s*(?:am|pm)|\d{1,2}:\d{2}|\bat\s+\d|noon|midnight|morning|"
                     r"evening|tonight|afternoon", m.group("when"), re.I):
        when = when.replace(hour=LIST_DEADLINE_HOUR, minute=0)
    return items[:m.start()], when


def _parse_list(text: str, now: datetime | None) -> tuple[str, list[str], datetime | None] | None:
    """A whole capture that is a to-do list, or None."""
    lines = [l for l in text.split("\n") if l.strip()]
    # Several lines, most of them bullets: the first plain line is the title.
    if len(lines) >= 2 and sum(bool(BULLET.match(l)) for l in lines) >= max(2, len(lines) - 1):
        head = lines[0] if not BULLET.match(lines[0]) else ""
        body = "\n".join(lines[1:] if head else lines)
        head = re.sub(r"\s*" + _LIST_WORD + r"\s*$", "", head.rstrip(" :"), flags=re.I).strip()
        head, due = _deadline(head, now) if head else (head, None)
        items = _split_items(body)
        return list_title(head), items, due
    for pattern in (LIST_NAMED, LIST_TRAILING):
        m = pattern.match(text)
        if m:
            items, due = _deadline(m.group("items").strip(), now)
            parts = _split_items(items)
            if parts:
                return list_title(m.group("title")), parts, due
    return None


def _strip(text: str, pattern: str) -> tuple[str, bool]:
    new = re.sub(pattern, " ", text, count=1, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", new).strip(), new != text


def parse(raw: str, now: datetime | None = None) -> Intent:
    """Read a dictated or typed capture into a structured intent."""
    text = raw.strip()
    intent = Intent(text=text)

    # -- whole-sentence instructions about to-do lists --
    m = PAUSE.match(text)
    if m:
        when, rest = parse_when(m.group("when"), now)
        if when is not None and not rest.strip(" ,."):
            intent.pause_target = m.group("target").strip(" ,.")
            intent.pause_until = when.date()
            intent.matched.append("phrase:pause")
            return intent
    made = _parse_list(text, now)
    if made:
        intent.list_title, intent.list_items, intent.due_at = made
        intent.kind = "list"
        intent.text = "\n".join(f"- [ ] {item}" for item in intent.list_items)
        intent.matched.append("phrase:list")
        return intent
    m = ADD_TO_LIST.match(text)
    if m:
        intent.append_to = m.group("list").strip()
        intent.append_item = True
        text = (m.group("item") + " " + m.group("rest")).strip()
        intent.matched.append("phrase:add-to-list")

    # -- symbols first: if someone typed them, they meant them --
    m = SYMBOL_TOPIC.search(text)
    if m:
        intent.topic = (m.group(1) or m.group(2)).strip()
        text = SYMBOL_TOPIC.sub(" ", text, count=1)
        intent.matched.append("symbol:topic")
    intent.tags = SYMBOL_TAG.findall(text)
    if intent.tags:
        text = SYMBOL_TAG.sub(" ", text)
    m = SYMBOL_TASK.search(text)
    if m:
        intent.kind = "task"
        if m.group(1).lower() == "remind":
            intent.remind = True
        text = SYMBOL_TASK.sub(" ", text)
        intent.matched.append("symbol:task")

    # -- spoken destination --
    if intent.topic is None and intent.append_to is None:
        for pattern in APPEND_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                intent.append_to = m.group("note").strip()
                text = (text[:m.start()] + " " + text[m.end():]).strip()
                intent.matched.append("phrase:append")
                break

    if intent.topic is None and intent.append_to is None:
        for pattern in TOPIC_PATTERNS:
            m = re.search(pattern, text + " ", re.IGNORECASE)
            if m:
                candidate = m.group("topic").strip(" ,.:")
                # Guard against swallowing the note itself.
                if 0 < len(candidate) <= 40:
                    intent.topic = candidate
                    text = (text[:m.start()] + " " + text[m.end():]).strip()
                    intent.matched.append("phrase:topic")
                break

    # -- reminders and tasks --
    for pattern in REMIND_PATTERNS:
        text, hit = _strip(text, pattern)
        if hit:
            intent.remind = True
            intent.kind = "task"
            intent.matched.append("phrase:remind")
            break

    if intent.kind != "task":
        for pattern in TASK_PATTERNS:
            text, hit = _strip(text, pattern)
            if hit:
                intent.kind = "task"
                intent.matched.append("phrase:task")
                break

    # -- time --
    # A bare "morning" or "tonight" inside ordinary prose is not a schedule
    # request. Vague times only count when something else already signalled
    # intent -- a reminder phrase, a task phrase, or an explicit date word.
    wants_time = intent.remind or intent.kind == "task" or intent.append_item
    explicit_date = bool(re.search(
        r"\b(?:(?:today|tonight|tomorrow)(?!['’]s)|next week|in \d+|in (?:a|an|two|three|"
        r"four|five|ten)\s|\d{1,2}\s*(?:am|pm)|\d{1,2}:\d{2}|"
        + "|".join(WEEKDAYS) + r"|" + "|".join(MONTHS) + r")\b", text, re.I))

    # A repeat means something only for a reminder, a to-do or a timed thing;
    # in ordinary writing ("I talk to her every day") the words stay put.
    if wants_time or explicit_date:
        for pattern, label in RECUR_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                if label is None:
                    n, unit = int(m.group(1)), m.group(2).lower()
                    label = ({"day": "daily", "week": "weekly", "month": "monthly",
                              "year": "yearly"}[unit] if n == 1 else f"every {n} {unit}s")
                intent.recurrence = label
                intent.matched.append(f"recur:{label}")
                # The repeat words aren't part of what the thing is called.
                text = re.sub(r"\s{2,}", " ", text[:m.start()] + " " + text[m.end():]).strip()
                break

    if wants_time or explicit_date:
        when, text = parse_when(text, now, allow_dayparts=wants_time)
        if when:
            intent.due_at = when
            intent.matched.append("time")
            if intent.kind == "note" and not intent.append_item:
                # An explicit future time on a note still means "surface it then".
                intent.kind = "task"

    if intent.recurrence and "month" in intent.recurrence:
        m = DAY_OF_MONTH.search(text)
        if m and 1 <= int(m.group(1) or m.group(2)) <= 31:
            base = now or datetime.now()
            hour, minute = ((intent.due_at.hour, intent.due_at.minute) if intent.due_at else (9, 0))
            intent.due_at = _next_day_of_month(int(m.group(1) or m.group(2)), base, hour, minute)
            text = (text[:m.start()] + " " + text[m.end():]).strip()
            if "time" not in intent.matched:
                intent.matched.append("time")
    if intent.recurrence == "weekdays" and intent.due_at and intent.due_at.weekday() >= 5:
        intent.due_at += timedelta(days=7 - intent.due_at.weekday())      # on to Monday

    # -- links --
    urls = URL_RE.findall(text)
    if urls:
        intent.url = urls[0]
        if intent.kind == "note":
            intent.kind = "video" if any(h in urls[0] for h in VIDEO_HOSTS) else "link"

    # Tidy the leftovers directives leave behind: "remind me to X" -> "X",
    # and the dangling "every" after a weekday was consumed as the date.
    text = re.sub(r"^(?:to|that|about)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bevery\s*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*every\s+(?=\w)", "", text, flags=re.IGNORECASE)
    intent.text = re.sub(r"\s{2,}", " ", text).strip(" ,.:;-") or raw.strip()
    return intent


def _next_day_of_month(day: int, now: datetime, hour: int, minute: int) -> datetime:
    """The next date with that day of the month, this month if it's still ahead.
    Short months use their last day."""
    year, month = now.year, now.month
    for _ in range(2):
        last = calendar.monthrange(year, month)[1]
        when = datetime(year, month, min(day, last), hour, minute)
        if when > now:
            return when
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return when


def describe_when(when: datetime | None, now: datetime | None = None) -> str:
    """Human-readable absolute time, always shown back so a misparse is obvious.

    Built without %-I / %-d: those are glibc extensions and raise ValueError on
    Windows, where this app mostly runs.
    """
    if when is None:
        return ""
    now = now or datetime.now()
    delta = (when.date() - now.date()).days

    hour = when.hour % 12 or 12
    clock = f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"

    if delta == 0:
        return f"Today, {clock}"
    if delta == 1:
        return f"Tomorrow, {clock}"
    if delta == -1:
        return f"Yesterday, {clock}"
    if 0 < delta < 7:
        return f"{when.strftime('%A')}, {clock}"
    if -7 < delta < 0:
        return f"Last {when.strftime('%A')}, {clock}"
    stamp = f"{when.strftime('%a')} {when.day} {when.strftime('%b')}"
    if when.year != now.year:
        stamp += f" {when.year}"
    return f"{stamp}, {clock}"
