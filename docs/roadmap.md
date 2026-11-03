# Roadmap

What gets built next, in order. Wider ideas live in
[feature-ideas.md](feature-ideas.md); this file is the agreed sequence.

## Done so far

- Notes, filing, search, chat and the AI box inside a note, all on this computer.
- Draggable tiles and lines; to-do lists that save (Phases 1 and 2, most of 3's
  server side).
- Personal notes: saved WhatsApp chats as summaries, lists and link tiles.
- Profiles (one for real use, one for trying things), daily backups.
- The phone: Wi-Fi access locked to paired devices, a phone layout, swipes.

## Next, in order

1. **A phone address that doesn't change.** Use the laptop's name
   (`<computer-name>.local`) instead of its number, so a phone stays connected
   when the laptop rejoins the Wi-Fi. Test on iPhone and Android; fall back to
   reserving the address in the router.
2. **Finish to-do lists.** Done 2026-11-03: the To-do lists page, the list
   layout, capture chips, and lists in chat.
3. **Links, as tiles.** See below.
4. **Reminders and check-ins.** The reminders engine, daily check-ins ("unless
   I ask you not to until a date"), and the Today and Reminders pages.
5. **Groups and repeating.** Lists inside lists; things that repeat.
6. **Reminders with the page closed.** On the laptop first, and fix the server's
   occasional exit with code 4. On a phone this needs the secure link below.

## Links, as tiles

A **Links** section for everyday notes, like the link tiles in Personal notes:
every link shows as a tile with its picture, title and site. Tap to open it,
use ⋯ for details, drag to arrange, filter by site, type or collection, and
search.

Links get there in two ways:

- **Added as a tile.** Paste a link in the box, or use ＋ on the Links page, and
  it becomes a tile, with an optional line about why you saved it.
- **Found in your notes.** Any link written inside a note becomes a tile
  connected to that note. The tile says which notes it's in and opens them; the
  note shows its links as small tiles at the bottom. A link in several notes is
  one tile listing all of them.

How it works:

- Notes stay the truth. Links are read out of the notes' files, and the tiles
  are an index plus saved previews that can be rebuilt any time.
- Previews are fetched once and kept in the profile's folder. Only the link's
  own site is contacted, never with any note text, and links that may need your
  account (Drive, Docs, invites) are never fetched. These are the rules
  Personal notes already follow.
- Personal notes and this page share one tile design.

To decide when we build it:

- Should a pasted link still also make its own note, as it does today, or only
  a tile?
- Should Personal notes' links show here too, behind a "From WhatsApp" filter?
- Where it sits on the phone: inside the Notes tab, or in the menu.

## Later

- **A secure link** (for example Tailscale): installs as a real app on Android
  and iPhone, opens instantly, reminders on the phone, and use away from home.
- **Better search:** passage-level matching for long notes, a reranker,
  follow-up questions that use the conversation.
- **Voice and video:** speech to text on this computer, and the media library
  in [architecture.md](architecture.md).
