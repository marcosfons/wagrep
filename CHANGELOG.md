# Changelog

All notable changes to wagrep. Versions follow [Semantic Versioning](https://semver.org);
`wagrep --version` prints the current one.

## [2.0.0] - 2026-11-07

First public release: a rewrite of the original single-file `wagrep` script.

### Added
- Local index (SQLite FTS5 trigram, accent-folded) with incremental sync driven
  by Core Data change counters; typical searches take a few milliseconds.
- Output budget for AI agents: inside Claude Code and similar tools output is
  capped and the rest is summarized by chat, sender, type and day on stderr.
- Typo-tolerant retry when nothing matches, separator-insensitive codes
  (`LA 3021` = `LA3021`), `-o` any-term, `-e` regex, `-k` context, `-f` sender.
- Search inside PDFs, Office files and plain-text attachments.
- Decoding of edits, reactions, polls, deleted messages, business templates,
  locations, round video notes, real document filenames and page counts.
- `#id` on every message, used by `ctx`, `show`, `open`, `path` and `transcribe`.
- `wagrep transcribe`: explicit, on-demand voice note transcription through any
  OpenAI-compatible API or a local faster-whisper (`--setup api|local`).
- Index scope preferences: index recent history by default, chosen chats in
  full (`wagrep index --default`, `wagrep chats --keep/--skip`).
- `wagrep pick` (fzf), `wagrep stats`, `wagrep sql`, `wagrep guide`.

### Kept
- The original flags (`-c -x -s -u -n -m -t -A -C -l --media --files --json
  --csv --extract`) behave as before.

[2.0.0]: https://github.com/marcosfons/wagrep/releases/tag/v2.0.0
