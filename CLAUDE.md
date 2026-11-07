# wagrep

Read-only search over the WhatsApp Desktop (macOS) message store, built for
people and AI agents. Python 3.9+, standard library only.

## Run and check

```sh
./wagrep -h                     # launcher; ~/.local/bin/wagrep is a symlink to it
./wagrep index                  # sync now, read pending documents, print status
./wagrep index --rebuild        # drop and rebuild the index (~1 min)
python3 -m unittest -q          # unit tests (tests/, stdlib unittest)
-v                              # debug log, mirrored to stderr
```

- Run the tests once at the end of a change, not after each edit.
- A change is not done until `python3 -m unittest -q` passes and a real query
  (`./wagrep -C x`, `./wagrep ctx <id>`) still works.
- Point `WAGREP_CACHE` at a scratch directory to try schema or decoder changes
  without touching the real index in `~/.cache/wagrep`.

## Layout (keep `src/` at 6 modules)

| path | role |
|---|---|
| `src/wa.py` | the macOS source: snapshot, read-only access, protobuf decoding, `decode(row) -> dict` |
| `src/index.py` | index schema, accent folding, incremental sync, document reading |
| `src/content.py` | text extraction from attachments (PDF, Office, plain text) |
| `src/search.py` | filters -> SQL, dates, loose/fuzzy matching, context, summaries, chat lookup |
| `src/render.py` | compact text, JSON, CSV, budget fitting, summary text |
| `src/cli.py` | argparse, commands, logging, the agent output budget |
| `wagrep` | launcher (resolves its symlink, imports `src.cli`) |
| `tests/` | one file per module |
| `docs/icon.svg` | the icon |

Only `wa.py` knows about WhatsApp's schema. A new source (WhatsApp for
Windows, Web) produces the same message dicts and leaves the rest untouched.

## Database facts (verified on a real 330k-message store)

- `ZWAMESSAGE.ZMESSAGETYPE`: see `wa.TYPES`. 2 is video, 3 audio; 7 link with
  preview (ZTITLE = preview title); 14 deleted; 46 poll; 54 round video note;
  59 call; 13 live location; 19/20/23/32/33/42/43 business templates and
  30/41 bot menus (their text is only in the metadata protobuf).
- `ZWAMEDIAITEM.ZMETADATA` is WhatsApp's own wrapper proto: 5 quoted stanza,
  6 quoted author JID, 8 mentions, 19 quoted message, 29 disappearing timer
  (seconds), 84 forwarded-from-channel, 24 an embedded Message proto.
- `ZWAMESSAGEINFO.ZRECEIPTINFO`: 7 reactions {1: {2 jid, 3 emoji, 4 ms}},
  8 poll {2 question, 3 options, 5 votes {1 option, 4 voter}}, 10 edit
  {3 new text, 4 ms}. Only edits made from this account are recorded and the
  original text is not kept.
- Documents: the filename is `ZAUTHORNAME` (older rows: `ZTEXT`) and
  `ZMOVIEDURATION` holds the page count.
- Core Data bumps `Z_OPT` on every row update, which is what the incremental
  sync compares (`wa.signatures`).
- Not found yet: a view-once marker, and how edits by other people are stored.

## Code quality rules

- **No dead code.** No unused parameters, flags or "kept in case" helpers;
  remove what stops being called in the same change. Comments explain *why*,
  never history.
- **Read-only, always.** Never open WhatsApp's live files with SQLite; only
  `wa.snapshot()` clones them. Connections to the clone, and the `sql`
  command, go through `wa.lock_read_only`. No code path may send or modify
  anything in WhatsApp.
- **Keep the index small** (~100 MB for 330k messages). Text is stored once:
  `msgs.norm` (folded) plus zlib-compressed `docs.text`. `fts` is a
  contentless trigram index with `detail=none`, used only as a prefilter;
  `search.text_clause` confirms with `LIKE` on `norm` or `docmatch` on the
  document. Measure with `dbstat` before adding columns or indexes.
- **The index is a cache.** Schema change = edit `index.SCHEMA` and bump
  `SCHEMA_VERSION`; the next run rebuilds. No migrations.
- **Output budget.** Anything that can print many rows respects
  `budget_of(args)` and says on stderr what was left out and how to narrow.
  stdout carries results only; notes, hints and "no matches" go to stderr.
- **Logging**, not `print`, for diagnostics: `logging.getLogger("wagrep.<module>")`.
  Every swallowed failure (an unreadable document, a failed clone) logs a
  warning. User-facing errors raise `wa.SourceError` / `search.QueryError`
  and exit 2; anything else is logged with its traceback to `logs/wagrep.log`.
- **Startup is the cost.** A query takes milliseconds; imports dominate. Keep
  heavy imports out of the hot path and add no third-party dependencies.
- Plain `-` as a separator in output, never an em dash or a middle dot.
  Nothing redundant: no sender repeated as the chat name, no empty fields.

## Privacy

Message content, names and numbers are personal data. Never put real ones in
code, tests, docs or commit messages: use fictional examples (Ana, Bruno,
"Beach trip", LA3021). The index and logs stay out of git.

## Git

- Commit messages: a short imperative subject, a blank line, then what changed
  and why. **No `Co-Authored-By` or other attribution trailers.**
- Remote: `git@github.com:marcosfons/wagrep.git` (public, SSH). `main` is
  pushed; never rewrite pushed history.
- Releases: bump `VERSION` in `src/cli.py`, commit, tag `vX.Y.Z`, push the
  tag, `gh release create vX.Y.Z` with notes.
