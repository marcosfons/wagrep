# WhatsApp Desktop (Windows) / Web as a second source

Research notes toward a second `wa.py`-style source. Not implemented yet.
On macOS the store is a plain SQLite database (`wa.py`), which is why wagrep
can read it directly. Windows and Web are different: per-message metadata is
readable offline, but message bodies are encrypted and - as of this research -
the key cannot be derived from a static offline copy alone.

## The Windows app changed under us (late 2025)

Earlier notes here talked about "WhatsApp Desktop" as one thing. It is not.
There have been three Windows clients, and the current one is new enough that
most of the reverse-engineering writeups predate it:

| Era | Engine | Message store | Key protection |
|---|---|---|---|
| ~2016-2022 | Electron (wrapper around Web) | IndexedDB in `%APPDATA%\WhatsApp` | same as Web (below) |
| ~2022-2025 | native UWP / WinUI | encrypted SQLite under `LocalState` | device-bound key, DPAPI, multiply-wrapped |
| 2025 onward | WebView2 (Chromium) wrapper | see below - likely still a device-bound SQLite, *not* the browser Web scheme | DPAPI-NG + device ID |

Meta switched WhatsApp Desktop from UWP to WebView2 around **9 December 2025**
(date per the ZAPiXDESK README; press coverage put the native app's removal at
~November 2025). So "Windows Desktop" and "the browser Web client" are **not**
the same target, and the UWP branch is now legacy but still present on
un-updated machines.

This matters because the 2024 ScienceDirect paper (and WhatsAppKeyBOF) describe
the **UWP** path - device-bound "identifier value", multiply-encrypted database
key, DPAPI. That is a different key hierarchy from the browser Web client, and
the earlier draft of this doc filed them under one heading.

## Two distinct encryption schemes

### A. Browser Web client (web.whatsapp.com in Chrome/Edge/etc.)

This is the cleanest target to study and the only one reproducible on a Mac.

- **Metadata - plaintext.** IndexedDB database `model-storage`, object stores
  `message`, `chat`, `contact`, `group-metadata`. Chat, sender, timestamp,
  type are readable without any key.
- **Text body - encrypted.** Each `message` record carries
  `msgRowOpaqueData {_keyId, iv, _data}` (`iv` 16 bytes, `_data` the AES-CBC
  ciphertext). Decrypted `_data` is a protobuf whose field 1 is the body.
- **Key.** A **non-extractable** `CryptoKey` lives in a separate IndexedDB
  database `wawc_db_enc`, store `keys` (record `id` 1). It is the HKDF *input*,
  not the message key: WhatsApp calls `deriveKey` (HKDF-SHA256) ->
  `AES-CBC, length 128`, non-extractable, usages `[encrypt, decrypt]`.
- **The salt is the blocker.** mazzo.li (2021) states the HKDF **salt is not
  stored locally** - it arrives in a welcome message over the WebSocket at
  login and is "hard to recover" afterwards. The localStorage item
  `WebEncKeySalt` / `WAWebEncKeySalt` is the HKDF **info**, not the salt.
  (Sources disagree here: a 2026 Baileys comment treats that localStorage item
  as the salt; willhackett tried it as the salt and failed. The disagreement
  itself is the finding - the derivation inputs are not settled in public.)

Consequence: **a static offline copy of the browser profile is not enough to
decrypt text** with what is public today. You would need the salt, which is
only observed during a live, logged-in session. The ScienceDirect authors
recovered it "by analyzing that exchange" - i.e. live, not offline.

### B. Desktop app (UWP, and apparently the new WebView2 wrapper too)

The forensic tool ZAPiXDESK handles both UWP and WebView2 Desktop and, notably,
for WebView2 it targets a **device-bound encrypted SQLite** (`session.db`,
`nativeSettings`, WAL files) protected by DPAPI-NG plus the machine's
`OfflineDeviceUniqueID` (TPM / registry) - **not** the browser's
`model-storage`/`wawc_db_enc` IndexedDB scheme. So the WebView2 switch did not
simply turn Desktop into the browser Web client; the native wrapper keeps its
own device-tied store. (Treat this as reported-not-verified; it comes from one
tool's README.)

This whole branch is **out of scope for wagrep**: the keys are bound to the
specific Windows machine and, for the live technique, to a running app. It
conflicts with the read-only/offline rule for the same reasons WhatsAppKeyBOF
did, and it is Windows-only.

## "Non-extractable" does not mean "encrypted on disk"

Worth recording because it is a common misread. `extractable: false` only
blocks `crypto.subtle.exportKey` at the JavaScript layer. The Web Crypto spec's
own security considerations explicitly allow implementations to **store key
material in plaintext in device storage**, and warn that anyone with access to
that storage may recover it. Chromium's IndexedDB serialization carries the
`extractable` flag alongside the key, not as on-disk encryption.

So the `wawc_db_enc` master key may well be readable from a copied profile. It
is still not sufficient on its own - without the server salt (scheme A) the
derived AES-CBC key cannot be reconstructed offline. The protection that
actually matters here is the salt's absence from disk, not the key's
non-extractability.

## What this means for wagrep

- **Metadata-only Web source: feasible offline, today.** Chats, senders,
  timestamps, types, media kinds - all plaintext in `model-storage`. No key,
  no salt, no live session. This is a real, in-scope deliverable.
- **Full text on Web: not feasible offline** with current public knowledge,
  because the HKDF salt is not on disk. It would require capturing the salt
  during a live login - which crosses wagrep's offline line - or new research
  showing the salt is cached somewhere. Leave this gated behind that finding.
- **Desktop app (UWP / WebView2 SQLite): out of scope.** Device-bound,
  live-or-DPAPI, Windows-only. Noted for completeness, not for implementation.
- **Value check.** A linked device only syncs part of the history, so a Web
  source mostly helps *other people* run wagrep on Windows/Web; it adds little
  to an existing macOS index that already holds the full store. Metadata-only
  value is correspondingly limited. Weigh this before investing.

## Cost of a stdlib-only reader

wagrep is stdlib-only, so a Web source cannot take `ccl_chromium_reader` or a
V8-deserialize library as a dependency. What it would have to reimplement:

- **Storage backend - now a moving target.** Chromium is migrating IndexedDB
  from LevelDB to SQLite (feature `IdbSqliteOnDiskRollout`; staged as of
  2025-2026: in-memory first, then *new* on-disk stores to SQLite, existing
  LevelDB left in place, no migration yet). A reader may meet **either**
  backend. The SQLite backend is far easier for us (`sqlite3` is stdlib); the
  LevelDB backend needs a log/`.ldb` parser plus **Snappy** decompression and
  the external `.blob` sidecar files. Check which backend the profile uses
  before building anything.
- **V8 / Blink structured-clone deserialization** of the stored values -
  required regardless of backend. This is the largest piece. The
  Blink-specific `CryptoKey` tag is part of it.
- **AES-CBC + HKDF in pure Python** - the stdlib ships no ciphers. HKDF is
  `hmac` only; AES would be a small pure-Python implementation (fine for short
  text bodies, but a first full index would be slow). Only needed if/when the
  salt problem is solved - i.e. not for the metadata-only source.

## wagrep refactor needed first (independent of the research)

CLAUDE.md says a new source "produces the same message dicts and leaves the
rest untouched." That is not true of the current code - `index.py` reaches into
WhatsApp's schema directly:

- `src/index.py:275` / `:277` pass Core Data SQL (`"m.Z_PK > ?"`,
  `"m.Z_PK IN (...)"`) into `wa.rows`.
- Incremental sync assumes integer PKs that only grow (`Z_PK`) and a
  per-row version bumped on update (`Z_OPT`, via `wa.signatures`). IndexedDB
  ids are strings and there is no `Z_OPT`; a LevelDB sequence number or the
  SQLite backend's rowids would have to stand in.
- `wa.rows`, `wa.signatures`, `wa.authors_by_stanza` all take a
  `sqlite3.Connection`.

A source-neutral interface - roughly `snapshot() -> handle`,
`changes(handle, since) -> ids`, `messages(handle, ids) -> dicts` - is worth
doing **before** any Web code exists, and is fully testable against today's
macOS source. This is the one piece of work here that is unambiguously useful
regardless of how the Web research turns out.

## Existing readers

- [`whatsapp-desktop-forensic`](https://github.com/SecurityRonin/whatsapp-desktop-forensic)
  / [crate](https://crates.io/crates/whatsapp-desktop-core) - Rust, parses
  IndexedDB into typed records, surfaces encrypted bodies as
  `MessageBody::Encrypted{key_id, iv, ciphertext}` verbatim, decrypts only with
  a key you supply. Does not derive the key. Good reference for the parse layer.
- [cclgroupltd/ccl_chromium_reader](https://github.com/cclgroupltd/ccl_chromium_reader)
  - maintained general-purpose Python Chromium IndexedDB/LevelDB reader with a
  V8 deserializer. Can't be a dependency, but is the reference for what a
  stdlib port must cover.
- [kraftdenker/ZAPiXDESK](https://github.com/kraftdenker/ZAPiXDESK) - GPL-3.0,
  Alberto Magno. Handles both UWP and the new WebView2 Desktop. **Live /
  on-host** forensics (needs the machine's device ID, DPAPI-NG); out of scope,
  but the clearest public source on the post-Dec-2025 Desktop layout. Read at
  README level only.
- [MrOctopus/pyWhatsUpp](https://github.com/mroctopus/pywhatsupp),
  [furkanpaligu/BrowSwEx](https://github.com/furkanpaligu/BrowSwEx) - older
  (2023) IndexedDB parsers, likely stale against current `_scheme` values.

## Sources read

- mazzo.li, ["Backing up WhatsApp data through the multi-device web client"](https://mazzo.li/posts/whatsapp-backup.html)
  (2021) - **read in full.** `model-storage` / `wawc_db_enc`+`keys` layout,
  `msgRowOpaqueData` fields, HKDF-SHA256 -> AES-CBC-128, salt from the WebSocket
  welcome (not on disk), `WebEncKeySalt` = HKDF info, non-extractable CryptoKey,
  plaintext protobuf schema, media-key derivation (`"WhatsApp Image Keys"` etc.,
  zero salt, 112-byte output). The single most useful source for scheme A.
- ScienceDirect, ["Analyzing the Web and UWP versions of WhatsApp for digital forensics"](https://www.sciencedirect.com/science/article/abs/pii/S2666281724001884)
  (Kim/Hur/Kang/Kim, 2024; [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4972794),
  doi 10.1016/j.fsidi.2024.301861) - **abstract + secondary summaries only; full
  method still not read** (paywalled, SSRN 403). Web: HKDF + AES-128-CBC, salt
  recovered from the server exchange. UWP: database key protected by a device
  "identifier value" the authors reproduce without the API, stored
  multiply-encrypted.
- Baileys issue [#2672](https://github.com/WhiskeySockets/Baileys/issues/2672)
  - community comment (2026) on the non-extractable master key in `wawc_db_enc`;
  disagrees with mazzo.li on salt location. Unverified, treat as a data point.
- willhackett, ["How to Not Decrypt WhatsApp Web (But Still Win)"](https://willhackett.com/en-gb/whatsapp-and-tonic/)
  - a failed direct-decrypt attempt (confirms the salt/info confusion is a real
  trap); full technical text not retrieved (site redirect). Abstract level.
- Chromium blink-dev, ["IndexedDB: SQLite backend"](https://groups.google.com/a/chromium.org/g/blink-dev/c/jS0khnC5IWA)
  + WebView2 feedback [#5754](https://github.com/MicrosoftEdge/WebView2Feedback/discussions/5754)
  - the LevelDB->SQLite migration staging.
- Press on the UWP->WebView2 switch: [alternativeto](https://alternativeto.net/news/2025/7/whatsapp-drops-its-uwp-native-app-in-favor-of-a-chromium-web-wrapper-on-windows-11/),
  [Gizmochina](https://www.gizmochina.com/2025/11/13/whatsapp-windows11-webview/).
- READMEs (not source) of `whatsapp-desktop-forensic`, `ZAPiXDESK`,
  `WhatsAppKeyBOF`.

## Where to search next

- **Reproduce scheme A locally first.** Link web.whatsapp.com in a throwaway
  Chrome profile on this Mac, copy its `IndexedDB/` folder, inspect the copy
  offline. One experiment answers: LevelDB vs SQLite backend, current
  `_scheme` values, whether the `CryptoKey` bytes are on disk, and - the
  decisive one - whether any salt is cached locally. (Linking adds a device to
  the account; the user's call.)
- **Full ScienceDirect method section** - exact HKDF inputs and precisely where
  the salt is obtained/cached. Determines whether offline text is possible at
  all. Try an institutional/library copy.
- **Is the WebView2 Desktop store really the device-bound SQLite** (ZAPiXDESK),
  or does it fall back to `model-storage` IndexedDB? Confirm against a real
  post-Dec-2025 install before assuming either.
- Scope `ccl_chromium_reader`'s V8-deserialize to size a stdlib port.
- Terms: `wawc_db_enc`, `WAWebEncKeySalt`, `IdbSqliteOnDiskRollout`,
  `whatsapp model-storage _scheme`, `whatsapp web HKDF welcome salt`.
