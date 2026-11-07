# Security and privacy

wagrep reads private conversations, so its guarantees matter more than its
features.

## What wagrep does and does not do

- **Read-only.** WhatsApp's database is never opened by SQLite; it is cloned
  (APFS clonefile) and the clone is opened with an authorizer that denies every
  write. wagrep cannot send, edit, delete or mark messages as read.
- **Local.** The index (`~/.cache/wagrep`), transcripts
  (`~/.local/share/wagrep`) and settings (`~/.config/wagrep`, mode 600) never
  leave your Mac.
- **One exception, opt-in:** `wagrep transcribe` uploads the voice notes you
  name to the transcription API you configured. With `--setup local` nothing is
  uploaded.
- `logs/wagrep.log` records the commands you ran, search terms included. It is
  git-ignored; delete it whenever you like.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting
(**Security > Report a vulnerability** on this repository) rather than a public
issue. Never attach real messages, databases, logs or transcripts to a report.
