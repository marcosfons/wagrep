"""The local index: a denormalized, searchable copy of every message.

Searches read only this file (~/.cache/wagrep/index.db), never the WhatsApp
snapshot. `ensure_fresh` brings it up to date when WhatsApp's database has
changed: only rows whose Core Data change counter moved are decoded again.

Size matters here, so text is stored once. The full-text table is a
contentless trigram index without positions (on SQLite older than 3.43 it
also holds a copy of the text): it only narrows the candidates
(every trigram of a term must be present), and the real match is confirmed
against `msgs.norm` (folded message text) or the compressed document text.

The index is a cache. Changing SCHEMA means bumping SCHEMA_VERSION, which
rebuilds it from scratch on the next run instead of migrating.
"""

from __future__ import annotations

import collections
import configparser
import fcntl
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import zipfile
import zlib

from . import content, wa

log = logging.getLogger("wagrep.index")

INDEX = os.path.join(wa.CACHE, "index.db")
# Transcripts cost time or money, so they live outside the rebuildable cache.
DATA_DIR = os.environ.get("WAGREP_DATA") or os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "wagrep")
TRANSCRIPTS = os.path.join(DATA_DIR, "transcripts.db")
TRANSCRIPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tr.transcripts(
  id INTEGER PRIMARY KEY,           -- msgs.id of the voice note
  text TEXT, norm TEXT, language TEXT, confidence REAL,
  backend TEXT, model TEXT, took REAL, created REAL);
"""
SCHEMA_VERSION = 4
# Re-check this many of the newest rows on every sync (edits, reactions and
# downloads mostly touch recent messages); everything else every FULL_EVERY.
RECENT_ROWS = 20000
FULL_EVERY = 6 * 3600
# Seconds a sync stays good before WhatsApp's changes are pulled again.
TTL = float(os.environ.get("WAGREP_TTL", "15"))

# Contentless FTS with deletes needs SQLite 3.43. Older ones (some Python
# builds) keep a copy of the text in the FTS table: bigger, same results.
FTS_OPTIONS = ("tokenize='trigram', content='', contentless_delete=1, detail=none"
               if sqlite3.sqlite_version_info >= (3, 43)
               else "tokenize='trigram', detail=none")
SCHEMA = f"""
CREATE TABLE meta(k TEXT PRIMARY KEY, v);
CREATE TABLE chats(id INTEGER PRIMARY KEY, name TEXT, jid TEXT, kind TEXT,
                   archived INT, norm TEXT,
                   scope TEXT,       -- rule label: all, skip, hide, 60d, 1y, ...
                   since REAL,       -- oldest indexed time; NULL = everything
                   hidden INT);      -- left out of searches unless named (-c/-x)
CREATE TABLE msgs(
  id INTEGER PRIMARY KEY,           -- ZWAMESSAGE.Z_PK
  sig INTEGER,                      -- Core Data change counters
  ts REAL, chat_id INTEGER, sender TEXT, from_me INT, kind TEXT, text TEXT,
  norm TEXT,                        -- folded searchable text (see fts_body)
  filename TEXT, starred INT, media_path TEXT, mime TEXT, size INT,
  duration INT, lat REAL, lon REAL, edited REAL,
  reply TEXT, reactions TEXT, extra TEXT);
CREATE INDEX msgs_chat_ts ON msgs(chat_id, ts);
CREATE INDEX msgs_ts ON msgs(ts);
CREATE VIRTUAL TABLE fts USING fts5(text, {FTS_OPTIONS});
CREATE TABLE docs(id INTEGER PRIMARY KEY, status TEXT, pages INT, chars INT,
                  text BLOB);       -- zlib-compressed UTF-8
CREATE TABLE vocab(word TEXT PRIMARY KEY, n INT) WITHOUT ROWID;
"""
COLUMNS = ("id", "sig", "ts", "chat_id", "sender", "from_me", "kind", "text",
           "norm", "filename", "starred", "media_path", "mime", "size", "duration",
           "lat", "lon", "edited", "reply", "reactions", "extra")
JSON_COLUMNS = ("reply", "reactions", "extra")
INSERT = (f"INSERT OR REPLACE INTO msgs ({','.join(COLUMNS)}) "
          f"VALUES ({','.join('?' * len(COLUMNS))})")


# --- preferences -------------------------------------------------------
# ~/.config/wagrep/config.ini holds user preferences: [whisper] for
# transcription, [scope] and [chats] for how much history gets indexed.

CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME")
                      or os.path.expanduser("~/.config"), "wagrep", "config.ini")
_SPAN = re.compile(r"^(\d+)\s*(d|w|m|mo|y)$")
_SPAN_SECONDS = {"d": 86400, "w": 7 * 86400, "m": 30 * 86400, "mo": 30 * 86400,
                 "y": 365 * 86400}


def _parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.optionxform = str  # chat JIDs are keys; keep them verbatim
    parser.read(CONFIG)
    return parser


def load_config(section: str) -> dict:
    parser = _parser()
    return dict(parser[section]) if parser.has_section(section) else {}


def save_config(section: str, values: dict):
    """Set (or, with None, remove) keys in one section; the file stays 0600."""
    parser = _parser()
    if not parser.has_section(section):
        parser.add_section(section)
    for key, value in values.items():
        if value is None:
            parser.remove_option(section, key)
        else:
            parser.set(section, key, str(value))
    os.makedirs(os.path.dirname(CONFIG), mode=0o700, exist_ok=True)
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        parser.write(fh)


def parse_span(text: str) -> float | str:
    """"all", "skip", "hide", or a span like 60d / 2w / 6m / 1y in seconds."""
    value = text.strip().lower()
    if value in ("all", "skip", "hide"):
        return value
    match = _SPAN.match(value)
    if not match:
        raise ValueError(f"{text!r} is not all, skip, hide or a span like 60d, 6m, 1y")
    return int(match.group(1)) * _SPAN_SECONDS[match.group(2)]


def chat_scopes(chats: list[dict], now: float) -> dict[int, tuple]:
    """chat id -> (rule label, oldest time to index, hidden).

    The time is None for everything and inf for nothing. A rule for the chat
    in [chats] wins; then [scope] archived for archived chats, [scope] muted
    for muted ones, and [scope] default for the rest (default: all). "hide"
    indexes a chat like the default but leaves it out of searches that do
    not name it."""
    scope = load_config("scope")
    rules = load_config("chats")
    default = scope.get("default", "all")
    if parse_span(default) == "hide":
        raise ValueError("[scope] default cannot be hide")
    out = {}
    for chat in chats:
        label = (rules.get(chat["jid"] or "")
                 or (chat["archived"] and scope.get("archived"))
                 or (chat.get("muted") and scope.get("muted"))
                 or default).strip().lower()
        span = parse_span(label)
        hidden = span == "hide"
        if hidden:
            span = parse_span(default)
        since = None if span == "all" else float("inf") if span == "skip" else now - span
        out[chat["id"]] = (label, since, int(hidden))
    return out


# --- text ----------------------------------------------------------------

def _fold_table():
    table = {}
    for cp in range(0xC0, 0x2B0):
        ch = chr(cp)
        base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                       if not unicodedata.combining(c))
        if base != ch:
            table[cp] = base
    return table


_FOLD = _fold_table()
WORD = re.compile(r"\w{3,30}")
_VARIANTS = collections.defaultdict(set)
for _cp, _base in _FOLD.items():
    if len(_base) == 1:
        _VARIANTS[_base.lower()].add(chr(_cp).lower())


def accent_pattern(word: str) -> str:
    """Regex matching `word` (already folded) in unfolded text with any
    accents, for use with re.IGNORECASE: "acao" matches "AÇÃO"."""
    return "".join(
        "[" + re.escape(c + "".join(sorted(_VARIANTS[c]))) + "]" if c in _VARIANTS
        else re.escape(c) for c in word)


def unaccent(text: str) -> str:
    return text.translate(_FOLD)


def fold(text: str | None) -> str:
    """Lowercase and strip accents, so "Márcio" and "marcio" index alike."""
    return text.translate(_FOLD).lower() if text else ""


def fts_body(msg: dict) -> str:
    """Searchable text of a message: body, filename and structured extras."""
    parts = [msg.get("text"), msg.get("filename")]
    extra = msg.get("extra") or {}
    parts.append(extra.get("link_title"))
    parts.append(extra.get("phone"))
    parts.extend(name for name, _ in extra.get("poll") or ())
    return fold("\n".join(p for p in parts if p))


def doc_text(blob) -> str | None:
    return zlib.decompress(blob).decode("utf-8") if blob else None


# --- connection ----------------------------------------------------------

def connect() -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < (3, 34):
        raise wa.SourceError(
            f"SQLite {sqlite3.sqlite_version} is too old (needs 3.34+ for "
            "trigram search); use a newer Python, e.g. brew install python")
    os.makedirs(wa.CACHE, mode=0o700, exist_ok=True)
    new = not os.path.exists(INDEX)
    conn = sqlite3.connect(INDEX, timeout=10)
    if new:
        os.chmod(INDEX, 0o600)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    version = None
    try:
        version = conn.execute(
            "SELECT v FROM meta WHERE k = 'schema'").fetchone()
    except sqlite3.OperationalError:
        pass
    if not version or int(version[0]) != SCHEMA_VERSION:
        conn.close()
        _remove_index()
        conn = sqlite3.connect(INDEX, timeout=10)
        os.chmod(INDEX, 0o600)
        conn.execute("PRAGMA journal_mode = WAL")
        log.info("creating index schema v%s at %s", SCHEMA_VERSION, INDEX)
        with conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    conn.execute("ATTACH DATABASE ? AS tr", (TRANSCRIPTS,))
    conn.executescript(TRANSCRIPTS_SCHEMA)
    return conn


def _remove_index():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(INDEX + suffix)
        except FileNotFoundError:
            pass


def meta(conn, key, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def row_dict(row: sqlite3.Row) -> dict:
    msg = dict(row)
    for col in JSON_COLUMNS:
        if msg.get(col):
            msg[col] = json.loads(msg[col])
    return msg


# --- sync ----------------------------------------------------------------

def ensure_fresh(conn, force=False, quiet=False) -> bool:
    """Sync if WhatsApp changed since the last sync. True when it synced."""
    sig = wa.source_signature()
    synced_at = float(meta(conn, "synced_at", 0))
    if not force:
        if meta(conn, "src_sig") == sig:
            return False
        if synced_at and time.time() - synced_at < TTL:
            log.debug("source changed but last sync is %.1fs old; skipping",
                      time.time() - synced_at)
            return False
    with open(os.path.join(wa.CACHE, "sync.lock"), "w") as lock:
        # A concurrent wagrep may be syncing (fzf previews, parallel calls).
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not force and meta(conn, "src_sig") == sig:
            return False
        sync(conn, sig, progress=not quiet and not synced_at)
    return True


def sync(conn, sig, full=False, progress=False, doc_budget=2.0):
    t0 = time.time()
    first = not meta(conn, "synced_at")
    if progress:
        print("wagrep: building the index (first run only, ~1 min)...",
              file=sys.stderr)
    src = wa.snapshot()
    t_snap = time.time() - t0
    try:
        names = wa.Names(src)
        chats = wa.chats(src)
        max_pk = conn.execute("SELECT COALESCE(MAX(id), 0) FROM msgs").fetchone()[0]
        last_full = float(meta(conn, "full_at", 0))
        full = full or first or time.time() - last_full > FULL_EVERY
        after = 0 if full else max(0, max_pk - RECENT_ROWS)

        scopes = chat_scopes(chats, time.time())
        rules = json.dumps([load_config("scope"), load_config("chats")], sort_keys=True)
        if meta(conn, "scope") != rules:
            # Scope changed: reconcile everything (backfill or prune).
            full, after = True, 0
        src_sigs = {pk: sig for pk, (sig, chat, ts) in wa.signatures(src, after).items()
                    if ts >= (scopes.get(chat, (None, None, 0))[1] or 0)}
        idx_sigs = dict(conn.execute(
            "SELECT id, sig FROM msgs WHERE id > ?", (after,)))
        changed = [pk for pk, s in src_sigs.items() if idx_sigs.get(pk) != s]
        deleted = [pk for pk in idx_sigs if pk not in src_sigs]

        with conn:
            conn.execute("DELETE FROM chats")
            conn.executemany(
                "INSERT INTO chats VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(c["id"], c["name"], c["jid"], c["kind"], c["archived"],
                  fold(c["name"]), *scopes[c["id"]]) for c in chats])
            for chunk in _chunks(deleted, 500):
                holes = ",".join("?" * len(chunk))
                for table in ("msgs", "fts", "docs"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE rowid IN ({holes})", chunk)
            words = _store(conn, src, names, changed, src_sigs, idx_sigs,
                           after, progress)
            conn.executemany(
                "INSERT INTO vocab VALUES (?, ?) "
                "ON CONFLICT(word) DO UPDATE SET n = n + excluded.n",
                words.items())
            set_meta(conn, "src_sig", sig)
            set_meta(conn, "synced_at", time.time())
            set_meta(conn, "scope", rules)
            if full:
                set_meta(conn, "full_at", time.time())
    finally:
        src.close()
        # The clone is only needed while decoding; keeping it would show up
        # as a second 300 MB database in the cache directory.
        wa.discard_snapshot()
    pending = read_documents(conn, None if first else doc_budget, progress)
    if first:
        conn.execute("INSERT INTO fts(fts) VALUES ('optimize')")
        conn.commit()
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    if free * 4096 > 20e6 or free > pages / 4:
        # Pruning (a narrower scope, old messages leaving the window) frees
        # pages SQLite would otherwise keep; give the disk space back.
        conn.execute("VACUUM")
        log.info("vacuumed %d free pages", free)
    log.info("sync: %s, %d changed, %d deleted, snapshot %.2fs, total %.2fs%s",
             "full" if full else f"after #{after}", len(changed), len(deleted),
             t_snap, time.time() - t0,
             f", {pending} documents left to read" if pending else "")


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _store(conn, src, names, changed, src_sigs, idx_sigs, after, progress):
    """Decode changed rows into msgs + fts. Returns vocabulary of new rows."""
    words = collections.Counter()
    if not changed:
        return words
    wanted = set(changed)
    if len(changed) > 5000:
        batches = [wa.rows(src, "m.Z_PK > ?", (after,))]
    else:
        batches = (wa.rows(src, f"m.Z_PK IN ({','.join('?' * len(c))})", c)
                   for c in _chunks(changed, 500))
    done = 0
    for cursor in batches:
        msgs = []
        for row in cursor:
            if row[0] not in wanted:
                continue
            msgs.append(wa.decode(row, names))
            done += 1
            if len(msgs) >= 20000:
                _write(conn, src, names, msgs, src_sigs, idx_sigs, words)
                msgs = []
                if progress:
                    print(f"  {done:,} messages", file=sys.stderr)
        _write(conn, src, names, msgs, src_sigs, idx_sigs, words)
    return words


def _write(conn, src, names, msgs, src_sigs, idx_sigs, words):
    if not msgs:
        return
    _name_reply_authors(src, names, msgs)
    ids = [m["id"] for m in msgs]
    docs, spoken = {}, {}
    for chunk in _chunks(ids, 500):
        holes = ",".join("?" * len(chunk))
        if idx_sigs:
            docs.update(conn.execute(
                f"SELECT id, text FROM docs WHERE status = 'ok' AND id IN ({holes})",
                chunk))
        spoken.update(conn.execute(
            f"SELECT id, norm FROM tr.transcripts WHERE id IN ({holes})", chunk))
    records, texts = [], []
    for msg in msgs:
        msg["sig"] = src_sigs[msg["id"]]
        body = msg["norm"] = fts_body(msg)
        if msg["id"] not in idx_sigs:
            words.update(WORD.findall(body))
        if msg["id"] in docs:
            body += "\n" + fold(doc_text(docs[msg["id"]]))
        if msg["id"] in spoken:
            body += "\n" + spoken[msg["id"]]
        records.append(tuple(
            json.dumps(msg[c], ensure_ascii=False, separators=(",", ":"))
            if c in JSON_COLUMNS and msg.get(c) else msg.get(c)
            for c in COLUMNS))
        texts.append((msg["id"], body))
    conn.executemany(INSERT, records)
    if idx_sigs:
        conn.executemany("DELETE FROM fts WHERE rowid = ?", [(i,) for i in ids])
        # A download that finished or failed changes the row: retry it.
        conn.executemany("DELETE FROM docs WHERE id = ? AND status != 'ok'",
                         [(i,) for i in ids])
    conn.executemany("INSERT INTO fts (rowid, text) VALUES (?, ?)", texts)


def _name_reply_authors(src, names, msgs):
    """Settle each reply's author name now, so the index needs no stanza ids.

    The quoted author's JID is right ~99% of the time but often missing from
    the push-name table; the quoted message's own sender is better then.
    """
    unresolved = {}
    for msg in msgs:
        reply = msg.get("reply")
        if reply and not reply.get("name") and reply.get("stanza"):
            unresolved.setdefault(reply["stanza"], []).append(reply)
    found = wa.authors_by_stanza(src, names, list(unresolved))
    for msg in msgs:
        reply = msg.get("reply")
        if not reply:
            continue
        jid = reply.pop("jid", None)
        stanza = reply.pop("stanza", None)
        if not reply.get("name"):
            reply["name"] = found.get(stanza) or (jid or "?").split("@")[0]


def read_documents(conn, budget: float | None, progress=False) -> int:
    """Index the text of documents not read yet, for at most `budget` seconds
    (None = all). Returns how many are still pending."""
    pending = conn.execute(
        "SELECT id, media_path FROM msgs WHERE media_path IS NOT NULL "
        "AND (kind = 'document' OR mime LIKE 'application/%' "
        "     OR mime LIKE 'text/%') "
        "AND id NOT IN (SELECT id FROM docs) ORDER BY id DESC").fetchall()
    todo = [(pk, rel) for pk, rel in pending
            if content.readable(os.path.splitext(rel)[1].lower())]
    if not todo:
        return 0
    if progress:
        print(f"wagrep: reading {len(todo)} documents...", file=sys.stderr)
    start = time.time()
    done = 0
    for pk, rel in todo:
        if budget is not None and time.time() - start > budget:
            break
        path = os.path.join(wa.MEDIA_ROOT, rel)
        text = pages = None
        if not os.path.exists(path):
            status = "missing"
        else:
            try:
                text, pages = content.extract(path)
                text = text.replace("\x00", "")
                status = "ok"
            except (OSError, subprocess.SubprocessError, zipfile.BadZipFile,
                    ValueError) as exc:
                log.warning("cannot read document #%s %s: %s", pk, rel, exc)
                status = "error"
        with conn:
            conn.execute("INSERT OR REPLACE INTO docs VALUES (?, ?, ?, ?, ?)",
                         (pk, status, pages, len(text or ""),
                          zlib.compress(text.encode("utf-8")) if text else None))
            if text:
                body = conn.execute("SELECT norm FROM msgs WHERE id = ?",
                                    (pk,)).fetchone()[0] or ""
                conn.execute("DELETE FROM fts WHERE rowid = ?", (pk,))
                conn.execute("INSERT INTO fts (rowid, text) VALUES (?, ?)",
                             (pk, body + "\n" + fold(text)))
        done += 1
        if progress and done % 100 == 0:
            print(f"  {done}/{len(todo)} documents", file=sys.stderr)
    return len(todo) - done


def save_transcript(conn, msg_id: int, result: dict, backend: str, took: float):
    """Store a transcript and make it searchable right away."""
    norm = fold(result["text"])
    with conn:
        conn.execute("INSERT OR REPLACE INTO tr.transcripts VALUES (?,?,?,?,?,?,?,?,?)",
                     (msg_id, result["text"], norm, result.get("language"),
                      result.get("confidence"), backend, result.get("model"),
                      took, time.time()))
        body = conn.execute("SELECT norm FROM msgs WHERE id = ?",
                            (msg_id,)).fetchone()[0] or ""
        conn.execute("DELETE FROM fts WHERE rowid = ?", (msg_id,))
        conn.execute("INSERT INTO fts (rowid, text) VALUES (?, ?)",
                     (msg_id, body + "\n" + norm))


def rebuild():
    """Drop the index and build it again from a fresh snapshot."""
    _remove_index()
    conn = connect()
    sync(conn, wa.source_signature(), full=True, progress=True)
    return conn
