# How Notes works: architecture

Last updated 25 Oct 2026.

## The big picture

Notes is a website that runs only on your own computer. A small Python
program (the **server**) shows the page in your browser at
`http://127.0.0.1:7777`. It saves notes as plain files on your disk, and it
talks to an AI that also runs on your computer (**Ollama**). The internet is
used only for web search, and you can turn that off.

```
 Browser page (mnemo/web/app.html)
   │  everything you see: capture box, lists, editor, chat, note AI, settings
   │  talks to the server with small JSON messages (/api/...)
   ▼
 Local server (mnemo/server.py) ── only reachable from this computer
   │
   ├── Engine (mnemo/engine.py) ─ saving, filing, editing, undo, reminders
   │     ├── intent.py   understands "remind me friday at 5", "put this in movies"
   │     ├── router.py + gate.py + topics.py   decide which collection a note goes in
   │     ├── enrich.py   reads links (page title/text) and pictures (description + text)
   │     ├── gardener.py "Tidy up" suggestions (merge / fold collections)
   │     └── journal.py  remembers how to undo every change
   │
   ├── Chat (mnemo/chat.py) ─ "Ask your notes" panel (Ctrl+J)
   ├── Note AI (mnemo/noteai.py) ─ the AI box inside each note (Alt+A)
   ├── Web search (mnemo/websearch.py) ─ only when needed, can be turned off
   └── Calculator (mnemo/calc.py) ─ exact maths so the AI never gets sums wrong
          │
          ▼
 Storage on disk                              Local AI (vendor/, inside this folder)
   vault/              your notes (Markdown)    Ollama runtime   1.9 GB
   vault/.mnemo/       index + pictures         nomic-embed-text  (finds similar notes)
                                                gemma3:4b        (writes, reads images, chats)
```

Nothing runs in the cloud. The whole project (code, AI models and notes) lives
in `D:\temp\mnemo`. Copy that folder to another computer, run the setup once,
and it works there too.

## How your data is stored today

| What | Where | Format | Can you open it yourself? |
|---|---|---|---|
| Every note | `vault\<Collection>\<title>.md` (unfiled ones in `vault\Inbox\`) | Plain Markdown text, with a small header of facts (id, title, kind, due time, collection, link source) | Yes, in Notepad, VS Code or Obsidian. **This is the real copy of your notes.** |
| Collections | The folders inside `vault\` | Normal Windows folders | Yes. A brand-new collection gets its folder after it has 3 notes. |
| Pictures | `vault\.mnemo\media\<code>.png` | The original image, named by a fingerprint of its content (so the same picture is only stored once) | Technically yes, but the folder is hidden and the names are codes. The improved design below fixes this. |
| What's in a picture | Inside its note's Markdown: "What's in the picture" and "Text in image" sections | Text | Yes |
| Search index, similarity data, links between notes, undo history, Tidy up suggestions | `vault\.mnemo\index.db` | SQLite database | It's a *cache*: if it's deleted, `python -m mnemo rebuild` recreates it from the Markdown files. Only the undo history would be lost. |
| Your settings (look, fonts, panel sizes, web search choice and key) | Your browser's local storage | JSON | Kept by the browser, per browser |
| AI programs and models | `vendor\ollama\`, `vendor\models\` | Ollama files | Not something you'd open |

Two choices behind this design:
1. **Plain files are the source of truth.** If this app disappeared tomorrow,
   your notes would still be readable files in normal folders.
2. **The database is disposable.** It only exists to make search and filing
   fast, and it can always be rebuilt from the files.

## What happens when you save something

1. **Understand it** (`intent.py`): pull out times ("friday at 5"), a collection
   you named ("put this in movies"), "add to the X note", links, to-dos. These
   are simple rules, so this takes under a millisecond.
2. **Enrich it** (`enrich.py`): a link gets its page title and text; a picture
   gets a description and the text read from it, both by the local AI.
3. **Save it first** (`vault.py`): the Markdown file is written before any
   decision is made, so a note can never be lost.
4. **Find similar things** (`vecs.py`, `search.py`): the note becomes a list of
   numbers (an "embedding") and is compared with every collection.
5. **Decide where it goes** (`router.py`, `gate.py`): file it when the match is
   clear, otherwise leave it in Inbox. New collections are created carefully,
   with six checks so you don't end up with "Movie", "Movies" and "Films".
6. **Link and schedule** it: related notes are connected, and the reminder time
   is set.
7. **Record how to undo it** (`journal.py`), so Ctrl+Z can reverse it.

## The two AIs

**Ask your notes** (`chat.py`, Ctrl+J)
- *Instructions* ("remind me…", "save…", "add to the X note…", "move the X note
  to Y") are carried out directly through the engine. No AI is needed for
  them, and each one can be undone.
- *Questions*: the relevant notes are picked (search, plus Today or Inbox when
  you ask about those). The AI answers word by word and names the notes it used.

**AI in a note** (`noteai.py`, Alt+A)
- The AI chooses one of: *answer*, *add text*, *rewrite the note*, or *search
  the web first*.
- If a rewrite drops lines you didn't ask to remove, it is retried once.
  Repeated lines are filtered out, and sums are done exactly by `calc.py`.

**Honesty and web search** (new)
- Both AIs are told that their own knowledge stops in early 2024 and that "I
  don't know" beats guessing.
- A question about recent or changing things (a recent year, "latest", "who
  won", prices, news) is looked up *before* the AI answers. The AI can also ask
  for a search itself.
- Answers that used the web list their pages as links.
- **Where search goes:**
  - Default: Wikipedia and DuckDuckGo's instant answers (free, no account).
  - Optional: Brave Search with your own key, or your own private SearXNG server.
- **Privacy:** only the search words leave your computer, never your notes.
  You can switch search off in Settings → General → Web search.

## The AI models: profiles, tools and the benchmark (Oct 2026)

**Profiles.** `models.py` has three quality profiles, and the app picks one
from your hardware. You can change it in Settings → AI.

| Profile | For | Brain (chat, note AI, filing, pictures) |
|---|---|---|
| Fast | GPU under 5 GB, or no GPU | `qwen3.5:2b` |
| Balanced | about 6 GB GPU and up (this laptop) | `gemma4:e4b` |
| Thorough | anyone who wants more care over speed | `gemma4:e4b`, reasoning step by step first |

The brains were picked by `python -m mnemo.bench` (see `docs/bench-results.md`).
`gemma4:e4b` was the only candidate with a perfect score on every job. A
machine with a much bigger GPU can point `MNEMO_ROUTER_MODEL` at a larger model.

- **The embedder** (`nomic-embed-text`) is shared by every profile. Changing it
  means re-embedding every note, so it's a deliberate one-off upgrade.
- **While a profile's brain is still downloading,** the original `gemma3:4b`
  keeps working.
- **What's saved where:** the choice is stored per computer in `vendor\ai.json`.
  Settings → AI shows the models on disk, their size, what's in use and how
  much is on the GPU, and can remove unused ones.

**Tools** (`agent.py`). Brains that support tool calling (Qwen 3.5, Gemma 4)
use real tools instead of the old text markers:
- `search_notes`
- `web_search`
- `read_page`
- `calculator`
- `create_reminder`
- `move_note`

The chat shows each step ("Working out 1847 * 23", "Searching the web for…").
Notes and pages a tool finds become numbered sources, the same as notes
found up front. These quick, model-free paths still run first:
- plain instructions;
- the pre-search for questions about recent facts;
- exact maths;
- the lost-lines retry.

Brains without tools (`gemma3:4b`, DeepSeek-R1 distills) use the old flow
unchanged.

**Think harder.** A switch in the chat turns on the model's own
step-by-step reasoning (Ollama's `think`). For quick jobs it stays off,
because reasoning out loud multiplies the time an answer takes.

**The benchmark** (`python -m mnemo.bench`, results in
`docs/bench-results.md`). Each candidate model runs Notes' own jobs, which
have known right answers (`tests/bench/`):
- note-AI decisions, with no lost lines;
- answers from a sample set of notes;
- honest "I don't know";
- tool choice;
- copying text from pictures;
- speed and GPU fit.

The profile defaults come from those measurements, not from blog rankings.

**DeepSeek.** DeepSeek's current models (V4, V4.1) are open, but they range
from 284 billion to 1.6 trillion parameters, so they need data-centre GPUs.
You chose local only, so they aren't used. The small DeepSeek-R1 8B distill
was tested as a "think harder" option.

## Personal notes (`/personal-notes`)

A separate section for chats exported from WhatsApp, where someone messaged
themselves to save things. They stay apart from notes: hundreds of fragments
would bury real notes and trigger automatic collections.

**The app only displays this; nothing on the page changes the data.** For now
the content is prepared and updated by hand, with Claude reading it, not by the
engine:

1. `python tools/personal_parse.py <folder>` splits the exports, copies them
   into a profile's `_personal/sources/`, hides private details and cleans links.
   It uses the first profile unless given `--profile=<id>`.
2. `python tools/personal_previews.py` fetches each public link's preview once
   and keeps it, including the thumbnail. Private-type links are never fetched.
3. Claude reads `data/` and writes summaries, key points, lists and one-line
   descriptions of links.

| In `profiles/<id>/_personal/` | What it is |
|---|---|
| `sources/` | Untouched copies of the exports and their files |
| `data/<source>.json` | Messages and moments, with private text replaced by a label |
| `data/<source>.notes.json` | Summary, key points, themes, moment titles, notes on links |
| `data/links.json`, `data/lists.json` | One entry per link; books, movies and other lists |
| `private/private.json` | Hidden text. Only the Show button reads it, and it's never read for processing |
| `layout.json` | How the link tiles are arranged. It's the one thing the page writes, and it holds no content |

`profiles/` is ignored by git, so none of this is committed. This file layout is
the contract: when the engine takes over the processing it writes the same
files, and `mnemo/personal_view.py` and the page don't change.

## Profiles

A profile is a separate set of notes on one computer. Each one is a whole vault
in its own folder, so nothing in one can reach another:

    profiles/profiles.json    the list; the first entry is the default
    profiles/<id>/            notes, index, pictures, Personal notes
    backups/                  dated zips

The server keeps one engine and one lock per profile. Each request picks its
profile by the `notes_profile` cookie, so each device remembers its own choice.
The page also names the profile it loaded in an `X-Notes-Profile` header, so a
tab left open keeps working on that profile if another tab switches. Saved
settings in the browser are stored per profile too. Undo handlers belong to each
engine for the same reason: a shared set would apply one profile's undo to the
other's files.

Names come from `profiles.json`, nothing in the code knows them. On a new copy
the first start makes one profile, Main, holding the how-to note from
`docs/how-to-use.md`. An existing `vault/` becomes that profile, after a checked
zip of it is taken. `--vault` or `MNEMO_VAULT` opens a single vault instead, with
no profiles.

Starting the server makes one zip a day of the first profile, skips it if
nothing changed, and keeps the last 7. The index is left out, since it's rebuilt
from the notes.

## To-do lists

A to-do list is a note with `kind: list`. Its settings sit in the note's header
(`due`, `checkin`, `quiet_until`, `repeat`, `parent`), and each item carries its
own small marks at the end of its line, Obsidian Tasks style: `📅` a date, `⏰` a
reminder, `🔁` a repeat, `✅` the day it was ticked. `mnemo/checklist.py` reads and
writes them; the page mirrors those rules, and so do the capture box's chips.

- **Saying it:** `intent.py` understands "exam prep list: … by 15 nov", "add … to
  the … list [time]" and "don't remind me about … until …". Chat runs the same
  phrases directly, and a tool-calling model gets `create_list`, `add_to_list`
  and `pause_reminders`.
- **The page:** a To-do lists view (rows or tiles), and inside a list a box with
  the deadline, progress and pace, section counts, item chips with a bell, and
  done items folded away. Pace wording is gentle on purpose.
- **The AI box in a note** puts back any marks a rewrite drops.
- **Reminders** (`mnemo/reminders.py`): `due_now()` decides what speaks up,
  each with a key in `reminder_log` so it fires once: single reminders, items'
  ⏰, one check-in per list per day at its time, "tomorrow's the deadline" for
  lists without check-ins, and one "past the deadline", after which the daily
  nudges stop. Paused lists stay quiet; a list in a group speaks through the
  group. The page polls `/api/due` and shows a card for each kind; `/api/agenda`
  feeds Today (what needs you today, and each list's pace) and the Reminders
  page (by day, plus undated to-dos). A rebuild marks what's already past.
- **Not yet:** alerts appear only while a page is open (roadmap step 6), and
  groups and repeating are step 5.

## Links (`mnemo/links.py`)

The Links page is a view over the notes, worked out each time, so there's
nothing to keep in step: every link note's address, plus every web address
written in any note (only the part you wrote, never a link note's fetched page
text). Links are cleaned of tracking codes and keyed so the same video or post
shared two ways is one tile, which lists every note it's in.

Kept beside the notes, in the profile's `.mnemo/links/`: `previews.json` (each
link's title, caption and creator, fetched once), `thumbs/`, and `layout.json`
(your tile order; links saved since come first). The page asks
`/api/links/fetch` for a few missing previews at a time; that runs outside the
database lock. Only the link's own site is contacted, never a local address,
and links that may need an account (Drive, Docs, invites) are never fetched.
The same code serves the Personal notes tools, so there's one implementation.

## Phones over Wi-Fi

`start-notes.bat` runs `python -m mnemo serve --lan`. Plain `serve` stays on
this computer only.

- **Pairing.** Each start makes a new code like `K7M2-QX9P`, shown only in the
  laptop's window as a QR code. Opening `/pair/<code>` gives that device a long
  random key in an HttpOnly cookie, and `profiles/devices.json` keeps only its
  hash. The code can be typed too. Ten wrong codes pause pairing for five
  minutes.
- **The lock.** A request from anywhere but this computer gets the "connect
  this device" page, or a 401, until its key matches. This runs before anything
  reads notes. Host and Origin must be `localhost` or the address the request
  arrived on, so a hostile site can't pass by resolving its name to this laptop.
- **Laptop only.** Wi-Fi here isn't encrypted, so private text in Personal
  notes, AI model changes, rebuilding, adding profiles and managing phones are
  refused from phones (`LAPTOP_ONLY` in server.py). Attachments and link
  addresses in Personal notes do go to a paired phone.
- **Address.** The QR uses the address the laptop would use to reach the
  internet, which skips VMware's virtual adapters. `python -m mnemo connect`
  shows it again for a Notes that's running; `--address` overrides it, for
  example for the laptop's Mobile Hotspot.
- **Home Screen.** iPhone Home Screen apps don't share Safari's cookies, so the
  manifest each paired device gets starts at `/open/<its key>`, which sets the
  cookie in the app's own storage.
- **One copy.** On Windows the server no longer sets SO_REUSEADDR, which had let
  a second copy share the port while the old one kept answering.

The phone layout is in `app.html`, inside one media rule (phone width, or a
touch screen up to 1000 px), so the laptop layout doesn't change. The list and
a note are separate screens. A note, the menu, Add, chat, link details and
search are layers, each with its own history entry, so Back closes the top one.
Touch: pull to refresh, swipe down on a panel's top to close it, and in a Home Screen app (which has no browser back gesture) swipe from the left edge to go back. A launch screen covers the first paint, and a saved note shows in the list at once as "Saving…". Without a secure page there's no service worker, so no offline use and no
notifications; Copy falls back to the old copy command.

## Module map

| File | Job |
|---|---|
| `web/app.html` | The whole page: layout, editor, chat, settings, the 17 built-in fonts |
| `server.py` | Serves the page and the `/api/...` endpoints; this computer and paired devices only |
| `links.py` | Links as tiles: finding, cleaning and previewing links |
| `devices.py`, `qr.py` | Pairing phones over Wi-Fi; the QR code, drawn with no library |
| `engine.py` | Saving, filing, editing, moving, deleting, undo, Tidy up actions |
| `profiles.py` | The list of profiles, first-time setup, daily backups |
| `intent.py` | Understanding plain-language input |
| `vault.py` | Reading and writing the Markdown files |
| `db.py`, `schema.sql` | The SQLite index |
| `notes.py`, `topics.py` | Note and collection records |
| `vecs.py`, `search.py` | Embeddings, similarity, keyword + meaning search |
| `router.py`, `gate.py`, `llm.py` | Deciding where a note goes; guarding against messy collections |
| `gardener.py` | Tidy up suggestions |
| `journal.py` | Undo history |
| `enrich.py` | Reading links and pictures |
| `sections.py` | Separating your words from the AI's in picture and link notes |
| `chat.py` | Ask your notes |
| `noteai.py` | The AI inside a note |
| `websearch.py` | Web search providers, deciding when to search |
| `calc.py` | Exact arithmetic |
| `agent.py` | Tool definitions and the step-by-step tool loop |
| `models.py` | Quality profiles, hardware detection, which model does which job |
| `aisettings.py` | Settings → AI: profile switching, background downloads, removing models |
| `bench.py`, `tests/bench/` | Measuring candidate models on Notes' own jobs |
| `runtime.py`, `setup.py`, `models.py`, `doctor.py`, `paths.py` | The bundled Ollama: download, start, check, and where everything lives |
| `cli.py` | Command-line tools (`serve`, `rebuild`, `doctor`, …) |
| `tests/` | 218 automatic checks |

---

## Proposal: a media library you can see in Windows

> Not built yet. This is the design for what you asked: images and videos go
> through the app, the app decides where they're stored, you can browse them
> in File Explorer, and every file carries its own description.

### What changes for you
- Every picture, video, PDF or voice memo you give the app is stored **in a
  normal, visible folder with a readable name**, next to the notes it belongs
  with:
  ```
  vault\
    Movies\
      Dune trailer.md                ← the note: description, transcript, your comments
      files\
        2026-10-25 dune-trailer.mp4  ← the real file, renamed so you can find it
        2026-10-25 dune-trailer.jpg  ← thumbnail
    Work\
      Whiteboard planning.md
      files\
        2026-10-21 whiteboard-planning.png
    Drop here\                       ← copy anything in here from Explorer…
  ```
- **"Drop here" folder:** drag files into it from File Explorer, or save
  straight into it from any app. Notes picks each file up, works out what it
  is, renames it, files it into the right collection, and writes its note.
- **"Show in folder"** on every file note opens File Explorer with that file
  selected.
- Move a note to another collection and its files move with it.
- Paste the same file twice and it's stored once (the content fingerprint we
  already use).

### The context every file gets (its note)

| Type | What the app works out | How (all on this computer) |
|---|---|---|
| Picture | What's in it, the text in it, when it was taken | Vision AI (already built), OCR (already built), photo date from the file |
| Video | A short summary, what's said (full transcript), what's shown at key moments, length, a thumbnail | **ffmpeg** to pull out frames and audio, **Whisper** (local speech-to-text) for the transcript, vision AI for the frames |
| Voice memo / audio | Transcript and summary | Whisper |
| PDF | The text, a summary, the page count | A PDF text reader |

All of it is written into the file's note, so search, chat and the note AI can
use it. For example, you could ask "what did the speaker say about pricing in
that video?"

### Under the hood
- **Bundling, as with Ollama:** `ffmpeg` and a Whisper model are downloaded
  into `vendor\`, so the folder stays portable. That's roughly 150 MB plus the
  Whisper model (about 150–500 MB, depending on size).
- **Links that work elsewhere:** notes refer to their files with relative
  links (`![](files/…)`), so images also show in Obsidian or VS Code.
- **Index:** a `files` table in the index stores type, size, length, size in
  pixels and fingerprint. Like the rest of the index, it can be rebuilt from
  the notes.
- **Thumbnails** go in `vault\.mnemo\thumbs\`, a cache that can be regenerated
  at any time.
- **Optional separate media drive:** big videos can live elsewhere (for
  example `E:\NotesMedia`) while notes stay in `vault\`.
- **Moving existing pictures:** pictures already in `.mnemo\media\` are moved
  into their collection's `files\` folder and given readable names.

### Suggested order to build
1. Pictures in visible folders with readable names, "Show in folder", and
   moving existing pictures.
2. The "Drop here" folder.
3. Videos and audio (ffmpeg + Whisper).
4. PDFs.
