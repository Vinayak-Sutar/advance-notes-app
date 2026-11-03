# Design brief: Mnemo — a note + reminder app with one input

I want you to design the UI for a desktop app called **Mnemo**. Give me a
working, clickable HTML/CSS/JS prototype I can open in a browser, with realistic
sample content in it. Design quality matters — this should feel like a
considered product, not a bootstrapped admin panel.

---

## 1. What the product is

**One input box. Everything goes in there, and it files itself.**

You paste or dictate a thought, a link, an image, a task, or a reminder into a
single capture field. The app works out what it is and where it belongs — which
folder, or which *existing note* it should be appended to — and puts it there.
You never pick a folder from a dropdown.

It is equal parts **note-taking app** and **reminder app**. Those aren't two
separate modes; a reminder is just a note that also has a time on it.

Three things must be effortless:

1. **Getting something in** — one field, no decisions required
2. **Reading and finding things** — browsing must be pleasant, not a file tree chore
3. **Editing** — click a note, type, done. No edit mode, no Save button.

---

## 2. How it is actually used — this drives everything

**The user dictates almost everything with voice-to-text (Wispr Flow).** This is
the single most important design constraint. It means:

- **Input arrives as long, run-on, unpunctuated text.** Not neat short phrases.
  A single capture might be three sentences of rambling that contains a note, a
  task, and a date all mixed together.
- **Symbol syntax is nearly unusable.** You cannot dictate `@gaming` or `!task`
  — saying "at gaming" produces the words "at gaming". So **natural language is
  the primary interface**: "put this in my gaming notes", "remind me Friday at
  five", "add this to the Elden Ring note". Symbols (`@topic`, `#tag`, `!task`)
  should still work as a typed power-user shortcut, but they are secondary and
  must never be the only way to do something.
- **The capture field must be a growing multi-line textarea**, not a
  single-line input. Dictation produces paragraphs.
- **Hands stay off the mouse during capture.** Keyboard/voice only for the whole
  capture loop, including confirming or correcting where it went.

Because the app is *interpreting* messy natural language, it must **show its
interpretation and make it trivially correctable**. That is the core interaction
to get right — see §4.

---

## 3. The domain model the UI must express

The backend already exists. Design to these real concepts:

**Notes** live in **topics** (folders), nested up to 3 levels
(`Technology/AI`, `Gaming/Elden Ring`). A note has:
- a **kind**: note, link, video, article, image, task, idea, reference, snippet
- a **status**: open, done, archived
- optional **due/remind time**
- **tags**, **entities** (people/products/things mentioned), and **related
  notes** (automatically discovered — "this connects to 3 things you already
  wrote")

**Key idea — type views are not folders.** "Gaming ▸ Videos" is a live filter
(everything in Gaming where kind = video), not a directory. So one note can
appear under its topic *and* under every relevant type view without being
duplicated. The sidebar should show these as sub-items under each topic:
`▶ Videos 8`, `🔗 Links 12`, `☐ Pending 3`. Make it visually clear these are
views, not folders — they behave differently (you can't drag a note "into" one).

**Provisional topics.** A newly created topic is *provisional* until enough
notes accumulate — it exists but has no real folder yet. Show these differently
(dimmed/italic) so the user can see the taxonomy forming.

**Inbox.** When the app isn't confident, the note goes to Inbox rather than
being guessed at. Inbox is a first-class triage queue, **not an error state** —
design it as a pleasant, fast, keyboard-drivable place to make quick decisions,
with 2–3 suggested destinations as one-click buttons per note.

**Review flag.** Some notes get filed but flagged as uncertain. They need a
subtle marker in lists — noticeable, not alarming.

**Garden.** A background process proposes structural cleanups: "these two
folders have become the same thing — merge?", "this folder has grown into two
distinct clusters — split?". These are *suggestions* requiring one click to
accept or dismiss. Show a before/after preview of what a merge or split would
actually do.

---

## 4. The capture interaction — the heart of the product

This is what to spend the most design effort on.

**The field accepts:** typed or dictated text, a pasted URL, a pasted or dragged
image, dragged files. One field, all of it.

**It must handle these intents, expressed in plain speech:**

| What the user says | What should happen |
|---|---|
| "malenia waterfowl timing is brutal" | new note, auto-filed into Gaming/Elden Ring |
| "put this in my work notes: the deploy is broken" | new note, user chose the topic |
| "add to the elden ring note that mimic tear helps" | **appended to an existing note** |
| "remind me Friday at 5 to renew the domain" | task + real notification scheduled |
| "I need to buy milk tomorrow" | task with a due date |
| *pastes a URL* | link note, page title and text fetched automatically |
| *pastes a screenshot* | image note, described so it's findable later |

**Appending to an existing note is a first-class case** and currently missing —
"add this to X" must work as smoothly as creating a new note. When the app
thinks you mean an existing note, show *which* note, with its current content
visible, before or immediately after committing.

**Show the interpretation.** After Enter, the note is saved instantly and the
field clears — capture must never block. But the app then shows what it decided,
in a way that is glanceable and correctable:

```
✓ Saved → Gaming / Elden Ring          [note]     Undo   Change…
  "mimic tear helps against Malenia"
  linked to 3 related notes
```

For a reminder:
```
✓ Reminder set → Friday 3 Oct, 5:00 PM             Undo   Change…
  "renew the domain"
```

**Correction must be keyboard-only and instant.** The user is dictating; making
them grab a mouse to fix a wrong folder breaks the whole loop. Design a way to
say or type a correction immediately after capture ("no, put that in work")
without navigating anywhere.

**Ambiguity handling.** If the app can't tell whether something is a note or a
task, or which of two notes to append to, it should ask *inline in the capture
area* with numbered options selectable by keystroke — never a modal dialog that
steals focus.

---

## 5. Screens to design

1. **Capture** — always present at the top, always focused by default. The
   product's identity. Growing textarea, drop target, interpretation feedback.

2. **Library** — the main browsing view. Sidebar topic tree with type views,
   note list, and the note itself. Reading and scanning must feel good: sensible
   typography, comfortable line length, real hierarchy. Show image thumbnails
   and link previews in the list.

3. **The note view / editor** — click anywhere in a note and type. Live markdown
   preview (headings, **bold**, lists, `code`, checkboxes, links render as you
   write). Autosaves, no Save button. Also shows: which topic it's in (and lets
   you change it), tags, entities, and "connects to" — the automatically
   discovered related notes, which should feel like a genuine feature, not a
   footer.

4. **Today / Reminders** — everything due or scheduled. Overdue, today,
   upcoming. Check items off. Snooze. This is the "reminder app" half and
   deserves real design attention, not a filtered list.

5. **Inbox** — triage queue, keyboard-drivable (`j`/`k` to move, `1`/`2`/`3` to
   accept a suggestion, `e` to edit).

6. **Graph** — a visual map of notes, topics and how they connect. Nice to have,
   should feel exploratory rather than decorative.

7. **Garden** — the structural proposals queue with before/after previews.

8. **Settings** — vault location, which local AI models are in use, confidence
   thresholds, whether the gardener applies changes automatically.

---

## 6. Reminders

Reminders fire as **real OS notifications even when the app is closed**. The
notification itself needs design: title, note body, and actions — *Snooze 1h*,
*Done*, *Open*.

Natural-language times must work, since they'll be dictated: "friday at 5",
"tomorrow morning", "in 3 hours", "next monday", "every tuesday". Always show
the resolved absolute time back to the user so a misparse is obvious
immediately: *"Friday 3 Oct, 5:00 PM"*, not *"friday at 5"*.

---

## 7. Visual direction

- **Dark-first**, with a proper light theme too. Near-black background, one
  accent colour used sparingly.
- **Calm and text-forward.** This app is mostly words. Typography does the work:
  clear hierarchy, generous whitespace, comfortable measure. Resist decoration.
- **Fast and quiet.** Subtle, quick transitions (~150ms). Nothing bounces or
  slides in dramatically. It should feel instant.
- **Dense where it helps, airy where it matters** — lists can be compact; the
  note you're reading should breathe.
- Reference points in spirit: Linear's precision, Things' calm, Obsidian's
  text-first respect for content. Not: Notion's chrome, a corporate dashboard.

---

## 8. Technical constraints — these are real

- **A single self-contained HTML file.** Inline CSS and JS, no build step, no
  npm. It is served by a small local Python server.
- **No external network requests at runtime.** No CDN fonts, no CDN libraries.
  The app must work fully offline — use system font stacks, write any needed JS
  by hand. This is non-negotiable; the whole product runs locally.
- **Desktop, keyboard-first.** Single layout, no responsive/mobile work needed.
- Talks to a local JSON API. Assume endpoints like: `GET /api/state` (topic tree
  + counts), `GET /api/notes?topic=&kind=&status=`, `GET /api/note?id=`,
  `POST /api/capture {text}`, `POST /api/move {id, topic}`, `POST /api/undo`,
  `GET /api/search?q=`, `GET /api/suggest?id=`, `GET /api/graph`,
  `GET /api/garden`. Invent additional endpoints where you need them and list
  what you assumed.
- Use realistic mock data so the design can be judged honestly — populated
  folders, long notes and short ones, overdue reminders, a few Inbox items.

---

## 9. What good looks like

- Someone dictates a rambling sentence and the note is filed correctly, with
  what happened visible in one glance and fixable in one keystroke.
- Browsing 500 notes is pleasant, and finding one takes seconds.
- Editing has no friction — no mode, no Save, no lost work.
- The reminder half feels like a real reminder app, not a checkbox column.
- Nothing the AI does is mysterious or irreversible. Where it made a judgement
  call, that's visible and undoable.

## 10. Anti-goals

- No folder pickers, tag pickers, or "choose a category" dropdowns in the
  capture path.
- No modal dialogs interrupting capture.
- No relying on symbols the user cannot dictate.
- Don't make Inbox feel like a failure or an error queue.
- Don't hide the AI's decisions behind a spinner and hope they're right.

---

## Deliverable

A single working HTML file I can open, covering the screens in §5 with realistic
mock data and working navigation between them. Include the capture interaction
in a functioning state (typing something shows a plausible interpretation
result). Where you made a design decision I didn't specify, note it briefly at
the end.
