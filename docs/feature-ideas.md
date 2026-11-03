# Feature ideas backlog

These came out of the redesign session (24 Oct 2026). They're parked for later;
nothing here is built yet unless it's marked as done. The agreed order of work
is in [roadmap.md](roadmap.md).

Every idea builds on what already makes the app different:
- you just say things;
- it runs on your own computer;
- it stays calm;
- it suits people who forget things easily.

## What to build next (priority order)

1. **Global quick-capture hotkey and native notifications.** These make capture
   and reminders work outside the app window, which is the core promise.
2. **Repeating reminders, and several items from one dictation.** People who
   dictate will hit these limits in the first week.
3. **Daily digest and rediscovery.** This solves the "I save things and forget
   them" problem the app exists for.
4. **App lock and backups.** For a private app, trust matters more than
   features.
5. **Custom collection icons and colours, plus density.** Cheap customisation
   wins that people see straight away.

## Capture: getting things in

- **Global quick-capture hotkey.** A small floating box over any app, so you can
  save without switching windows.
- **Built-in voice capture.** A push-to-talk key that uses a local speech
  model, so you don't need Wispr Flow.
- **Share and clipboard capture.** Save from the browser or the right-click
  menu, or turn on an optional "watch clipboard" mode.
- **Screenshot hotkey.** Take a screenshot straight into Notes, which then
  reads the text and files it.
- **Link enrichment.** Fetch the page title, a preview and a summary
  automatically, and pull in a video's length and thumbnail. (Partly done: the
  title and text are already fetched.)
- **Several items from one dictation.** "Buy milk, call Riya, and remind me to
  pay rent on the 1st" becomes three items.

## Reminders: the "remember" half

- **Repeating reminders in plain words:** "every Monday at 9", "first of every
  month", "every 3 days".
- **Location-style triggers without GPS:** "when I open my laptop tomorrow",
  "next time I'm in Notes".
- **Nagging mode for important items.** It keeps nudging every 10 minutes until
  you tick the item off.
- **Morning brief.** A gentle card each day showing what's due, what's overdue,
  and one forgotten save.
- **System notifications.** Native OS notifications, even when the app is
  closed.
- **Smart snooze.** It learns when you usually deal with things, for example
  "you tend to finish work items around 7 PM".

## Organising without effort

- **Smart views.** Saved searches such as "movies I haven't watched", "links
  older than a month" or "to-dos with no date".
- **Status for media lists.** Watched, read or bought, so a movie list becomes a
  real watchlist.
- **Pin and favourite** notes to the top of a collection.
- **Bulk actions.** Select several notes, then move, tag, delete or merge them.
- **Archive instead of delete,** so the app stays tidy without losing anything.
- **Duplicate detection.** "You saved Interstellar twice. Merge them?"

## AI chat and smarts

- **"Do it for me" actions.** "Turn this note into a checklist", "split this
  into separate notes", "make a shopping list from my recipe notes".
- **Daily or weekly digest.** "Here's what you saved this week" and "3 things
  you forgot about".
- **Rediscovery.** Each day, a random old save comes back with the question
  "still want this?"
- **Ask about a single note.** A button on any note that summarises it, expands
  it or pulls out the to-dos.
- **Personal memory.** It learns facts you tell it ("Sam is my brother", "the
  trip is in Goa") and uses them in answers.
- **Model choice.** A setting to switch between smaller, faster local models and
  larger, smarter ones.

## ADHD-friendly

- **Focus mode.** Shows only today's items and one note, and hides everything
  else.
- **Gentle overdue handling.** No red wall of overdue items. Instead, offer
  "Reschedule all to tomorrow?" in one click.
- **Break big tasks down.** "This feels big. Want me to break it into steps?"
- **Done list and streaks.** A "Done today" celebration, with confetti you can
  turn off.
- **Time-blindness help.** Show "in 2 hours" next to clock times, plus an
  optional visual countdown.
- **Body doubling.** A quiet "working on this" timer (Pomodoro-style) attached
  to a to-do.

## Customisation beyond palettes and fonts

- **Theme editor.** Pick your own background and accent with a live contrast
  check, then save and share theme files.
- **Collection look.** Custom colours, icons and emoji per collection.
- **Sidebar items.** Choose which ones show (Today, Inbox, Map, and so on), and
  reorder them.
- **Density.** Compact, comfortable or spacious lists.
- **Custom keyboard shortcuts.**
- **Named looks.** Save your current setup as a named look.
- **Buddy character.** Choose a companion, its mood and how chatty it is. That's
  what the empty slot in the sidebar is for.
- **Sounds.** A soft tick, a reminder chime or silence, with a volume setting.
- **Time and calendar.** Week start, and 12- or 24-hour time.
- **Language** for the interface and for dates.
- **Background.** A wallpaper or subtle texture behind the panels: paper, grain
  or gradient.

## Privacy and data

- **App lock.** A PIN or the computer's own sign-in (Windows Hello, Touch ID),
  plus a separate lock for individual notes.
- **Encrypted notes folder,** as an option.
- **Backups.** Automatic local snapshots, and one-click export to a zip.
- **Sync without the cloud.** Between your own devices over local Wi-Fi, or
  through a folder you control (Syncthing, a USB drive).
- **Import** from Apple Notes, Google Keep, Notion, Obsidian and plain text.
- **"What the AI saw" log.** A clear list of which notes were used for each
  answer.

## Integrations (all optional)

- **Calendar.** Show reminders in your calendar, and read events so "remind me
  before my dentist appointment" works.
- **Obsidian compatibility.** Your notes folder also works as an Obsidian vault
  (it's already Markdown).
- **Phone companion.** A tiny capture-only phone app that sends to your computer
  over local Wi-Fi.
- **Browser extension.** "Save to Notes" on any page.

## Delight

- **Collection covers.** Cover images picked automatically from the notes
  inside.
- **Year in review.** "You saved 412 things and finished 180 to-dos."
- **Seasonal touches.** Seasonal palettes and small celebrations for
  milestones.
