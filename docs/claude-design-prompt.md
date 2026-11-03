# Prompt for Claude Design: redesign "Notes"

Copy everything below the line into Claude Design. When it's done, save the
result as `index.html` and share it back so it can be wired into the app.

---

Redesign the interface of a desktop app called **Notes**: a local, private
note-taking and reminder app with a built-in AI. The app and its backend already
exist and work. I need a **new front end**, delivered as **one self-contained
`index.html`** that I can drop in to replace the current page.

The current design uses heavy neumorphism. Every surface is grey and either
embossed or pressed in, with double shadows everywhere. It feels heavy, muddy and
overdone, and the depth fights the content. Start fresh. Don't keep that look.

## 1. The product in one breath

There's **one input box at the top**. You type or dictate anything into it: a
thought, a to-do, a reminder, a link, a screenshot. The app works out what it is
and files it into the right collection by itself, or adds it to an existing
note. You never pick a folder. It's also a reminder app: a reminder is just a
note with a time on it. **New in this redesign: an AI chat** where you can ask
questions about your own notes and ask it to do things.

Everything runs on the user's own computer: local AI models, plain Markdown
files on disk, and nothing sent to the cloud. "Private by default" is part of the
product's personality.

## 2. Who uses it and how (drives every decision)

- The main user **dictates with voice** (Wispr Flow). Input arrives as long,
  run-on, unpunctuated text. They can't dictate symbols like `@` or `!`, so
  **plain language is the interface**: "remind me friday at 5 to renew the
  domain", "Interstellar, put this in movies", "add to the trip note that we need
  sunscreen".
- The capture box must be a **growing multi-line textarea** that is focused by
  default.
- **Keyboard-first** desktop app. Mouse is optional for everything important.
- Audience: people who save lots of small things (movies to watch, links,
  screenshots, ideas, errands) and forget them. Students, people with ADHD,
  busy people. The app should feel **friendly, calm and a little delightful**,
  never like a corporate dashboard or a developer tool.

## 3. Visual direction

**Clean, flat, friendly, colourful.** Modern 2026 consumer-app quality, in the
spirit of Things 3's calm, Linear's precision, Craft's polish and Tiimo's
colour-coding. Not Notion chrome, not neumorphism, not glassmorphism.

**Depth rules (important, because the user disliked "too much depth"):**
- Surfaces are **flat**. Separate areas with a subtle background tint and
  1px hairline borders, not shadows.
- **No inset/embossed/pressed effects at all.**
- Shadows only on things that genuinely float: menus, toasts, the chat panel
  when it overlays, dialogs. Keep them soft and small.
- Buttons are flat, either filled (one primary action per view) or quiet
  (text/ghost). Hover and active states use tint changes, not shadows.

**Colour:**
- Light theme first (warm off-white background, white content surfaces,
  near-black text), plus a proper dark theme. Default to the OS setting, with a
  toggle.
- One primary accent colour for actions and focus.
- **Each collection gets its own colour** from a set of about 8 vibrant
  swatches (coral, tangerine, sunflower, mint, teal, sky, violet, pink). Use them
  for small things: collection dots, chips, icons and the left edge of list
  items. Don't flood large areas with them. This colour-coding is what makes the
  app feel lively.
- Every text/background pair must meet WCAG AA (4.5:1). That includes muted
  text, chips and text on coloured buttons, in both themes.

**Shape and type:**
- Corner radius 10–14px for cards and inputs, pill shapes for chips.
- Generous whitespace, with comfortable reading width (about 68ch) in the note.
- System font stack (Segoe UI Variable / -apple-system / system-ui). No web
  fonts, because the app must work offline.
- Clear hierarchy: the note title is large, list rows are compact, metadata is
  small and muted.

**Motion:** quick and quiet, 120–180ms fades and slides. Nothing bounces.
Respect `prefers-reduced-motion`.

**Icons:** a small set of simple line icons drawn as inline SVG (no icon fonts,
no CDNs).

**Future-proofing:**
- Put every colour in CSS custom properties on `:root`, with
  `[data-theme="dark"]` overrides, so more themes can be added later just by
  swapping variables.
- Leave a small, empty **"buddy" slot** at the bottom of the sidebar (about
  64×64, hidden for now). A companion character will live there later. **Do not
  draw a mascot.**

## 4. Layout

Desktop app shell, designed for 1280×800 and still usable at 1024px wide:

```
┌──────────────────────────────────────────────────────────────┐
│  Capture box  (always visible, focused by default)    [Ask]  │
├────────────┬──────────────────┬──────────────────────────────┤
│ Sidebar    │ List             │ Note / detail                │
│            │                  │                    ┌────────┐│
│            │                  │                    │ Chat   ││
│            │                  │                    │ panel  ││
└────────────┴──────────────────┴────────────────────┴────────┘
```

**Sidebar,** top to bottom:
- **New note** button (Alt+N).
- **Today** (badge: due count).
- **All notes** (badge: count).
- **Inbox** (badge: count).
- **Collections**: the user's folders, each with its colour dot and count, and
  nesting up to 3 levels. A collection can expand to show *views* inside it
  (Videos, Links, Images, To-dos). These views are filters, not folders, so give
  them a distinct, lighter style.
- A collapsed **More** section holding **Map** (graph), **Tidy up** (folder
  cleanup suggestions) and **Settings**. These are advanced, so keep them out of
  the way.
- The empty buddy slot.
- Theme toggle, small.

**Status line:** drop the current developer status bar (model names, file
paths). If anything is needed, use one quiet line such as "Learning your
collections: 19 more notes" during the first-run period, and a small
"AI working…" indicator.

## 5. Screens and states to design

1. **Capture box** (the heart of the product).
   - **Live interpretation chips while typing**, for example
     `⏰ Fri 24 Oct, 5:00 PM`, `→ Movies`, `+ Add to "Trip"`, `☐ To-do`. Show
     resolved, absolute times so a misheard time is obvious.
   - Enter saves, Shift+Enter adds a newline, Esc clears.
   - Accepts paste or drop of images and links (show a small thumbnail or link
     chip while uploading).
   - Starting the text with `?` turns it into search.
   - A **Save / Ask** switch on the right side of the box. In Ask mode, Enter
     sends the text to the AI chat instead of saving it. Tab toggles the mode
     while the box is focused.

2. **After saving: a result toast** that doesn't steal focus. It reads like
   "Saved to **Movies** · linked to 2 notes" with **Undo** and **Move…**
   buttons. Variants:
   - "Reminder set · Fri 24 Oct, 5:00 PM";
   - "Added to **Trip**";
   - "Left in Inbox: not sure where it goes" (friendly, not an error);
   - "Saved image · reading the text in it…" (it updates when done).

3. **List.**
   - Compact rows showing the title, a one-line preview, a collection colour
     edge, a kind icon, and due time if any.
   - Image notes show a thumbnail; link notes show the site domain.
   - "Needs review" gets a subtle dot, not an alarm.
   - Selected and hover states use flat tints.

4. **Note view/editor.**
   - Click and type: no edit mode, no Save button, autosave with a tiny
     "Saved" fade.
   - Title on top, then the body with live Markdown (headings, bold, lists,
     checkboxes, links, code).
   - Metadata row:
     - collection chip (click to move);
     - kind;
     - due/reminder chip (click to change time, including quick picks such as
       "Later today", "Tomorrow 9 AM", "Next week");
     - **Mark done** for to-dos;
     - **File it for me** when the note is unfiled;
     - **Delete** (with undo).
   - Image notes show the image large, then "What's in the picture" and "Text in
     the image" sections.
   - Link notes show the source link and summary.
   - **Connects to**: related notes as small cards. This should feel like a real
     feature, not a footer.

5. **Today.**
   - Overdue, Today and Upcoming sections.
   - Check items off, with snooze quick picks (10 min, 1 hour, tomorrow
     morning).
   - This is the "reminder app" half, so design it with care.
   - Empty state: a calm "Nothing due today" message.

6. **In-app reminder alert.** When a reminder fires while the app is open, a
   card appears in the top-right corner with the title, time, and **Done /
   Snooze 10 min / Snooze 1 h / Open**. Several can stack.

7. **Inbox.**
   - A friendly triage queue: each item shows 2–3 suggested collections as
     one-click buttons, plus "Somewhere else…".
   - Keyboard: j/k to move between items, 1/2/3 to accept a suggestion.
   - Never make it look like an error list.

8. **Search results,** from `?query` in the capture box: hits with highlighted
   snippets and their collection, and Enter opens one.

9. **AI chat panel** (new; see §6).

10. **First run / empty states.** A new user has exactly **one note: "Welcome
    to Notes"**. The first screen must feel inviting, not empty. Show:
    - a short line under the capture box with 3 example phrases they can click
      to try;
    - a friendly empty state for each view.

11. **Map** (graph of collections and notes, on a canvas) and **Tidy up** (a
    list of suggestions such as "Merge *Films* into *Movies*?" with Accept /
    Dismiss). Simple layouts are fine; they're secondary.

12. **Settings** (simple panel):
    - Theme: Light / Dark / System.
    - How much the AI organises: Automatic / Suggest only / Manual. This is UI
      only for now.
    - Reminder defaults: what "morning" and "evening" mean.
    - Vault folder (read-only text).
    - About: "Everything stays on this computer".

## 6. The AI chat (new feature, design it fully)

A panel on the right that slides over the note area (about 380px wide).
Open it with a sparkle/chat button in the capture bar, or with **Ctrl+J**.
Esc closes it.

What it does:
- **Answers questions from the user's notes.** For example: "what movies did I
  want to watch?", "when is the domain renewal?", "summarise my trip notes".
  - Answers stream in word by word.
  - Under each answer is a row of **source chips**: the notes it used, each with
    its collection colour. Clicking a chip opens that note.
- **Does things when asked,** for example "remind me to call mom at 6", "save
  this as a note", "move the Dune note to Movies". The result shows as an
  **action card** inside the conversation (for example "⏰ Reminder set · Call
  mom · Today 6:00 PM") with **Undo**.
- **General questions** with no matching notes are answered plainly, with a
  small muted label: "Not from your notes".

Design these states:
- empty: a greeting plus 3–4 suggested questions as chips;
- thinking (before the first word arrives, which can take a few seconds because
  the AI is local);
- streaming;
- answer with sources;
- action card;
- error ("The local AI isn't running. Notes still work. Try again");
- a long conversation that scrolls.

Other requirements:
- A **New chat** button clears the conversation.
- The input is a multi-line textarea; Enter sends, Shift+Enter adds a newline.
- **Stop** button while streaming.
- Answers render basic Markdown (lists, bold, links).

## 7. Keyboard shortcuts (keep these)

| Keys | Action |
|---|---|
| Enter / Shift+Enter | Save capture / newline |
| Esc | Clear capture, close panel or menu |
| Ctrl+K | Focus the capture box from anywhere |
| Alt+N | New blank note |
| Ctrl+Z | Undo the last app action (when not typing in a note) |
| Ctrl+J | Toggle the AI chat |
| Tab (in capture) | Switch Save / Ask |
| `?text` + Enter | Search |
| j / k, 1 / 2 / 3 | Inbox triage |

Show a small keyboard-shortcuts sheet when `Ctrl+/` is pressed.

## 8. Tone of the words in the UI

Warm, short, plain English. No jargon: never "embedding", "router", "vault",
"topic", "cold start" or model names. Say "collection" (not topic/folder in
the UI), "Inbox", "Today", "File it for me", "Learning your collections".

## 9. Technical requirements (strict)

- **One file: `index.html`**, with inline CSS and JS. Vanilla JS only: no
  frameworks, no build step, no npm.
- **No external requests at runtime**: no CDN fonts, scripts or icons. The
  only network calls are to the same-origin API below.
- **Two modes in the same file:**
  - When opened directly from disk (`location.protocol === "file:"`), use
    built-in **mock data** and fake responses (including a fake streaming chat
    answer), so the design can be reviewed by double-clicking the file.
  - When served from `http://127.0.0.1:7777`, call the **real API**.
- Put every network call in **one `api` object at the top of the script**
  (for example `api.state()`, `api.notes({topic})`, `api.capture(text)`,
  `api.chat(...)`). Each method switches between mock and live, so wiring
  needs no hunting.
- The UI should refresh its counts after each change by re-calling `api.state()`.
- Accessibility:
  - visible focus rings;
  - `aria-label` on icon-only buttons;
  - `aria-live` on toasts and on the chat answer area;
  - a full keyboard path through every screen.

## 10. The real API (design to these exact shapes)

All JSON. GETs take query strings; POSTs take a JSON body.

**Shared shapes:**

```
NoteBrief = { id, title, kind,          // kind: note|task|link|video|article|image|idea|reference|snippet
              status,                   // open|done|archived
              topic,                    // collection path like "Movies" or "Work/Clients", or null = Inbox
              snippet,                  // plain-text preview, ~200 chars
              created, updated,         // ISO timestamps
              due,                      // ISO local time or null
              remind,                   // true if a reminder will fire at `due`
              done, review,             // booleans; review = filed but uncertain
              media,                    // image path or null -> show via "/media/" + basename(media)
              source }                  // URL for link notes or null

TopicNode = { id, name, path, state,    // state: "active" | "provisional" (show provisional dimmed)
              count,                    // notes inside, including sub-collections
              views: { video, link, article, task, image, pending },   // counts, may be missing
              children: [TopicNode] }
```

**Endpoints:**

```
GET  /api/state             -> { tree:[TopicNode], inbox, review, notes, topics, coldStart }
                               coldStart = notes still needed before collections form (0 = done)
GET  /api/notes?topic=&kind=&status=   -> { notes:[NoteBrief] }   (no params = all notes)
GET  /api/note?id=          -> NoteBrief + { body (markdown), related:[NoteBrief], topics:[path] }
GET  /api/inbox             -> { notes:[NoteBrief], review:[NoteBrief] }
GET  /api/suggest?id=       -> { suggestions:[{ path, similarity, count }] }   (Inbox buttons)
GET  /api/search?q=         -> { hits:[{ id, title, topic, snippet, via, score }] }
GET  /api/agenda            -> { notes:[NoteBrief] }   (everything with a due time; you group it)
GET  /api/due               -> { notes:[NoteBrief] }   (reminders that should fire now; poll every 30s)
GET  /api/graph             -> { nodes:[{ id, label, type:"topic"|"note", size, state }],
                                 links:[{ source, target, type:"parent"|"about"|"related" }] }
GET  /api/garden            -> { proposals:[{ id, kind:"merge"|"split"|..., summary, score }] }

POST /api/capture       { text }              -> { id, title, kind, due, reminder, recurrence,
                                                    appendedTo, topic, outcome:"filed"|"review"|"inbox",
                                                    related (count), steered, summary }
POST /api/upload        { data: "data:image/png;base64,...", text }
                                              -> { id, title, topic, outcome, media, ocr, description }
POST /api/note/new      { title, body, topic? }  -> { id, path, topic }
POST /api/note/update   { id, title?, body? }    -> { id, path, title }
POST /api/note/append   { id, text }
POST /api/note/schedule { id, when: ISO or null, remind: bool }   (snooze = new ISO time)
POST /api/note/complete { id, done: bool }
POST /api/note/file     { id }       -> { filed, topic, reason? }   ("File it for me")
POST /api/note/delete   { id }       -> { deleted, title }
POST /api/move          { id, topic }   (topic = collection path)
POST /api/reminded      { ids:[...] }   (mark reminders as shown)
POST /api/undo          {}           -> { undone: string|null }
```

**New: the chat endpoint** (I'll build the backend to match whatever you design,
but use this shape by default):

```
POST /api/chat   { message, history:[{ role:"user"|"assistant", content }] }
Response: a stream of newline-delimited JSON events (read with fetch + response.body.getReader()):
  {"type":"sources","notes":[{ id, title, topic }]}      // which notes it is using (may be empty)
  {"type":"token","text":"..."}                           // append to the answer
  {"type":"action","action":{ kind:"reminder"|"note"|"move", summary, noteId }}   // something it did
  {"type":"done","fromNotes":true}                        // fromNotes false -> "Not from your notes"
  {"type":"error","message":"..."}
```

## 11. Mock data (keep it small; a new user is overwhelmed by a lot)

- **Collections:** Movies (2 notes), Work (2), Ideas (1).
- **Inbox:** the "Welcome to Notes" note, plus 1 unfiled note.
- **Today:** 1 overdue reminder, 1 due today, 1 next week.
- 1 image note with OCR text, and 1 link note.
- A short sample chat that shows sources and one action card.

## 12. Deliverable

- One `index.html` covering every screen and state above, with working
  navigation, mock data and a fake streaming chat.
- At the end of your reply, list:
  - any design decisions you made that I didn't specify;
  - any API fields or endpoints you assumed beyond §10.
