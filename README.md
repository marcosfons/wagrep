<p align="center">
  <img src="docs/icon.svg" width="128" height="128" alt="wagrep icon">
</p>

<h1 align="center">wagrep</h1>

<p align="center">
  Search your WhatsApp history from the terminal, fast enough and compact enough<br>
  that your AI agent can dig through it for you.
</p>

---

A lot of life happens in WhatsApp: the booking code a friend sent, the
decision made in a voice-note-heavy group, the PDF someone shared three weeks
ago. None of it is reachable from your editor or your agent.

wagrep reads the WhatsApp Desktop database already on your Mac and turns it
into something you (or Claude, Codex, any agent with a shell) can query:

```text
$ wagrep "LA 3021" -s 30d
== Beach trip (group)
-- 2026-10-28 Wed
#381102 14:05 Ana: did anyone book the LA3021 flight yet?
  > Bruno: I'll book it tonight
#381110 14:21 Bruno [audio 0:42 81.2K]
#381131 15:02 Carla [document booking.pdf 2p 157.3K]
  in file: ...Flight LA-3021 Departure 08:40 Seat 14C...
```

## Why it works well with agents

- **Answers in milliseconds.** Searches hit a local full-text index
  (SQLite FTS5), not a table scan. Typical queries take 5-10 ms; the index
  follows WhatsApp incrementally in the background of each call.
- **Never floods the context.** Inside an agent session the output is capped
  at ~10k characters. Instead of a million lines you get the newest matches
  that fit, plus a summary of the rest by chat, sender, type and day, with the
  exact flag to page back.
- **Forgiving search.** Accent- and case-insensitive (`marcio` finds
  `Márcio`). Codes like `LA3021` match with or without separators. A search
  with no hits retries tolerating typos (`churasco` finds `churrasco`) and
  says so.
- **Searches inside files.** Text in PDFs, spreadsheets, Word/PowerPoint
  documents and plain-text files people sent is indexed with the messages.
- **Shows what the app shows.** Who replied to what, reactions, edits, polls
  with vote counts, deleted messages, business messages, locations, and every
  attachment's type, length, size and page count.
- **One id for everything.** Each message prints as `#id`;
  `wagrep ctx/show/open/path <id>` takes it from there.
- **Read-only by construction.** WhatsApp's database is never opened, only
  cloned, and the clone is opened with writes denied at the SQLite level.
  wagrep cannot send, edit or delete anything, and nothing leaves your Mac.

## Install

Needs macOS with WhatsApp Desktop signed in, and Python 3.9+ (no packages).

```sh
git clone git@github.com:marcosfons/wagrep.git ~/prog/wagrep
ln -s ~/prog/wagrep/wagrep ~/.local/bin/wagrep
wagrep index          # first run: builds the index (about a minute)
```

Give your terminal **Full Disk Access** (System Settings > Privacy & Security),
since WhatsApp's data lives in a protected container. Optional extras:
`brew install poppler` (text inside PDFs) and `brew install fzf` (`wagrep pick`).

## Use

| | |
|---|---|
| `wagrep invoice -c Ana -s 7d` | messages with "invoice" in Ana's chat this week |
| `wagrep -x "Beach trip" -d today` | today's conversation, in order |
| `wagrep -o proposal contract` | messages with either word |
| `wagrep --files budget` | documents named or containing "budget" |
| `wagrep -T audio -c Bruno -n 5` | last voice notes, with durations |
| `wagrep chats beach` | find a chat by name |
| `wagrep ctx 381131` | the conversation around a message |
| `wagrep open 381131` | open its attachment (`-R` reveals it in Finder) |
| `wagrep stats -c beach -s 2026-10` | who talked, when, about how much |
| `wagrep pick` | browse everything interactively with fzf |

`wagrep -h` lists every flag; `wagrep guide` is the reference written for agents.

## Teach your agent

Add this to your `CLAUDE.md` (or `AGENTS.md`). It costs a few dozen tokens:

```markdown
- **`wagrep`**: my WhatsApp history, read-only. Use it when context may live in
  chats (people, decisions, codes, files sent). `wagrep chats NAME`, then
  `wagrep TERMS -c CHAT -s 7d`; `#ID`s feed `ctx`/`show`/`open`/`path`.
  Follow the stderr hints to narrow. Details: `wagrep guide`.
```

## How it works

```
WhatsApp's ChatStorage.sqlite --clone--> ~/.cache/wagrep snapshot --decode--> index.db (FTS5)
```

Each run compares WhatsApp's file signature with the last sync. If it changed,
only the rows whose Core Data change counter moved are decoded again (reply
threads, reactions, edits and polls live in protobuf blobs that wagrep parses
itself). Everything stays in `~/.cache/wagrep`: about 100 MB for 330k messages
and 900 documents, because text is stored once and the search index only keeps
which 3-letter chunks each message contains. Logs go to `logs/wagrep.log`.

## Not yet

- **WhatsApp for Windows and WhatsApp Web.** They keep messages in other
  stores (WhatsApp Web in encrypted IndexedDB), so they need their own reader.
  Only `src/wa.py` knows about the macOS database; another source fills the
  same index.
- **Voice note transcription**, to search what was said, not only written.
- **View-once media** is not identified in the database yet.

## License

[MIT](LICENSE). Not affiliated with WhatsApp or Meta.
