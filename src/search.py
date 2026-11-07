"""Turning filters into index queries: text match, fuzzy expansion, context,
summaries and chat lookup. Everything here reads the index only."""

from __future__ import annotations

import datetime as dt
import functools
import json
import re
from dataclasses import dataclass, field

from .index import WORD, accent_pattern, doc_text, fold, unaccent
from .wa import MEDIA_KINDS
SEPARATORS = r"[\s.\-/_]?"


class QueryError(Exception):
    """Bad user input (unparseable date, unknown chat); message is user-facing."""


# --- dates ---------------------------------------------------------------

_RELATIVE = re.compile(
    r"^-?(\d+)\s*(m|min|mins|minutes?|h|hours?|d|days?|w|weeks?|mo|months?|y|years?)$")
_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400,
          "y": 365 * 86400}


def parse_when(value: str, flag: str) -> float:
    """A local date/time expression -> Unix time.

    Accepts 2026-08-01, 2026-08-01 09:30, 2026-08-01T09:30:15, 2026-08,
    01/08/2026, 01/08, 09:30 (today), today, yesterday, now, and relative
    spans like 7d, -7d, 3h, 2w, "-7 days".
    """
    text = value.strip().lower()
    now = dt.datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if text in ("now",):
        return now.timestamp()
    if text in ("today", "hoje"):
        return today.timestamp()
    if text in ("yesterday", "ontem"):
        return (today - dt.timedelta(days=1)).timestamp()
    match = _RELATIVE.match(text)
    if match:
        unit = match.group(2)
        key = "mo" if unit.startswith("mo") else unit[0]
        return now.timestamp() - int(match.group(1)) * _UNITS[key]
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
        parts = [int(p) for p in text.split(":")] + [0]
        return today.replace(hour=parts[0], minute=parts[1],
                             second=parts[2]).timestamp()
    br = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?(?:[ t](.+))?", text)
    if br:
        day, month, year, rest = br.groups()
        text = f"{year or now.year}-{int(month):02d}-{int(day):02d}"
        if rest:
            text += " " + rest
    if re.fullmatch(r"\d{4}-\d{2}", text):
        text += "-01"
    try:
        return dt.datetime.fromisoformat(text.replace("t", " ")).timestamp()
    except ValueError:
        raise QueryError(
            f"{flag}: cannot parse date {value!r}\n"
            "  Use 2026-08-01, '2026-08-01 09:30', 09:30, today, yesterday, "
            "or a span like 7d / 3h / 2w.") from None


def day_after(ts: float) -> float:
    day = dt.datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0)
    return (day + dt.timedelta(days=1)).timestamp()


# --- query ---------------------------------------------------------------

@dataclass
class Query:
    terms: list[str] = field(default_factory=list)
    any_term: bool = False
    regex: bool = False
    fuzzy: bool | None = None          # None = only when nothing matches
    chat_ids: list[int] | None = None
    senders: list[str] = field(default_factory=list)
    since: float | None = None
    until: float | None = None
    from_me: int | None = None
    kinds: list[str] = field(default_factory=list)
    media: bool = False
    files: bool = False
    edited: bool = False
    starred: bool = False
    expansions: dict = field(default_factory=dict)  # fuzzy term -> words
    scope_hint: str | None = None   # shown when results may reach unindexed history
    window_start: float | None = None  # where the default index window begins

    @property
    def has_text(self):
        return bool(self.terms)


def _fts_string(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _loose_pattern(term: str) -> tuple[str, str | None] | None:
    """Identifiers like booking codes and part numbers ("LA 3021", "la-3021",
    "LA3021") are written with or without separators. Returns a regex that
    allows any of them, plus the longest letter/digit run to prefilter on."""
    squashed = re.sub(r"[\s.\-/_]", "", term)
    if not (len(squashed) >= 4 and squashed.isalnum()
            and re.search(r"\d", squashed) and re.search(r"[a-z]", squashed)
            and len(term) - len(squashed) <= 3):
        return None
    pattern = (r"(?<![a-z0-9])" + SEPARATORS.join(re.escape(c) for c in squashed)
               + r"(?![a-z0-9])")
    runs = re.findall(r"[a-z]+|\d+", squashed)
    longest = max(runs, key=len)
    return pattern, longest if len(longest) >= 3 else None


def _prefilter(literal: str) -> str | None:
    """FTS query requiring every trigram of `literal` (None if it has none)."""
    grams = sorted(_grams(literal))
    return " AND ".join(map(_fts_string, grams)) if grams else None


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def text_clause(q: Query) -> tuple[str, list]:
    """SQL for the query's terms. Each term must appear in the message's
    folded text (`m.norm`), its transcript or its document; a trigram prefilter on `fts`
    narrows the rows first whenever every needed term has trigrams."""
    if not q.terms:
        return "", []
    clauses, params, filters = [], [], []
    for term in q.terms:
        t = fold(term).strip()
        if q.regex or _loose_pattern(t):
            if q.regex:
                pattern, run = unaccent(term), None
            else:
                pattern, run = _loose_pattern(t)
            body = "(regexp(?, m.norm) OR regexp(?, t.norm))"
            body_params = [pattern, pattern]
            test = ["r", pattern]
            filters.append(_prefilter(run) if run else None)
        else:
            words = q.expansions.get(term) or [t]
            body = "(" + " OR ".join(["m.norm LIKE ? ESCAPE '\\'",
                                      "t.norm LIKE ? ESCAPE '\\'"] * len(words)) + ")"
            body_params = [_like(w) for w in words for _ in (0, 1)]
            test = ["s", words]
            parts = [_prefilter(w) for w in words]
            filters.append(None if None in parts
                           else " OR ".join(f"({p})" for p in parts))
        clauses.append(f"({body} OR (d.text IS NOT NULL AND docmatch(?, d.text)))")
        params += body_params + [json.dumps(test)]
    joiner = " OR " if q.any_term else " AND "
    sql = "(" + joiner.join(clauses) + ")"
    if q.any_term:
        prefilter = " OR ".join(f"({f})" for f in filters) if all(filters) else None
    else:
        prefilter = " AND ".join(f"({f})" for f in filters if f) or None
    if prefilter:
        return (f"(m.id IN (SELECT rowid FROM fts WHERE fts MATCH ?) AND {sql})",
                [prefilter] + params)
    return sql, params


@functools.lru_cache(maxsize=256)
def _compiled(pattern: str):
    return re.compile(pattern, re.IGNORECASE)


def regexp(pattern, text) -> int:
    """SQL function regexp(pattern, text), case-insensitive."""
    return int(text is not None and _compiled(pattern).search(text) is not None)


@functools.lru_cache(maxsize=64)
def _words_pattern(words: tuple):
    return re.compile("|".join(map(accent_pattern, words)), re.IGNORECASE)


def docmatch(test: str, blob) -> int:
    """SQL function: does one term (["s", words] or ["r", regex]) occur in a
    compressed document's text? Words are matched accent-insensitively on
    the original text, which is much faster than folding whole documents."""
    kind, value = json.loads(test)
    text = doc_text(blob)
    if kind == "r":
        return int(_compiled(value).search(fold(text)) is not None)
    return int(_words_pattern(tuple(value)).search(text) is not None)


def register(conn):
    """SQL functions the queries (and `wagrep sql`) rely on."""
    conn.create_function("regexp", 2, regexp, deterministic=True)
    conn.create_function("docmatch", 2, docmatch, deterministic=True)
    conn.create_function("fold", 1, fold, deterministic=True)
    conn.create_function("doctext", 1, doc_text, deterministic=True)


def where(q: Query) -> tuple[str, list]:
    clauses, params = [], []
    if q.chat_ids is not None:
        clauses.append(f"m.chat_id IN ({','.join(map(str, q.chat_ids))})")
    else:
        clauses.append("m.chat_id NOT IN (SELECT id FROM chats WHERE hidden = 1)")
    if q.since is not None:
        clauses.append("m.ts >= ?")
        params.append(q.since)
    if q.until is not None:
        clauses.append("m.ts < ?")
        params.append(q.until)
    if q.from_me is not None:
        clauses.append("m.from_me = ?")
        params.append(q.from_me)
    if q.senders:
        clauses.append("(" + " OR ".join("fold(m.sender) LIKE ?" for _ in q.senders) + ")")
        params += [f"%{fold(s)}%" for s in q.senders]
    if q.kinds:
        clauses.append(f"m.kind IN ({','.join('?' * len(q.kinds))})")
        params += q.kinds
    elif q.files:
        clauses.append("(m.kind = 'document' OR m.mime LIKE 'application/%' "
                       "OR m.mime LIKE 'text/%')")
    elif q.media:
        clauses.append(f"(m.media_path IS NOT NULL OR m.kind IN "
                       f"({','.join(repr(k) for k in sorted(MEDIA_KINDS))}))")
    else:
        clauses.append("m.kind != 'system'")
    if q.edited:
        clauses.append("m.edited IS NOT NULL")
    if q.starred:
        clauses.append("m.starred = 1")
    text_sql, text_params = text_clause(q)
    if text_sql:
        clauses.append(text_sql)
        params += text_params
    return " AND ".join(clauses) or "1", params


def from_clause(q: Query) -> str:
    join = (" LEFT JOIN docs d ON d.id = m.id"
            " LEFT JOIN tr.transcripts t ON t.id = m.id") if q.has_text else ""
    return f"FROM msgs m{join}"


def run(conn, q: Query, limit: int, oldest=False) -> tuple[list, int]:
    """Matching rows (newest `limit`, or oldest with oldest=True), plus the
    total match count."""
    sql_where, params = where(q)
    order = "ASC" if oldest else "DESC"
    sql = (f"SELECT m.* {from_clause(q)} WHERE {sql_where} "
           f"ORDER BY m.ts {order}, m.id {order}")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    total = len(rows)
    if limit and total == limit:
        total = count(conn, q)
    if not oldest:
        rows.reverse()
    return rows, total


def count(conn, q: Query) -> int:
    sql_where, params = where(q)
    return conn.execute(
        f"SELECT COUNT(*) {from_clause(q)} WHERE {sql_where}", params).fetchone()[0]


def summary(conn, q: Query) -> dict:
    """Counts by chat, sender, kind and day for everything the query matches."""
    sql_where, params = where(q)
    base = f"{from_clause(q)} LEFT JOIN chats c ON c.id = m.chat_id WHERE {sql_where}"

    def grouped(expr, limit=12):
        return conn.execute(
            f"SELECT {expr} AS g, COUNT(*) {base} GROUP BY g "
            f"ORDER BY COUNT(*) DESC LIMIT {limit}", params).fetchall()

    total, first, last = conn.execute(
        f"SELECT COUNT(*), MIN(m.ts), MAX(m.ts) {base}", params).fetchone()
    days = conn.execute(
        f"SELECT date(m.ts, 'unixepoch', 'localtime') AS d, COUNT(*) {base} "
        f"GROUP BY d ORDER BY d", params).fetchall()
    return {"total": total, "first": first, "last": last,
            "chats": grouped("c.name"), "senders": grouped("m.sender"),
            "kinds": grouped("m.kind"), "days": days}


def around(conn, msg_id: int, before: int, after: int) -> list:
    """Messages around one message in its own chat, oldest first."""
    row = conn.execute("SELECT chat_id, ts FROM msgs WHERE id = ?",
                       (msg_id,)).fetchone()
    if not row:
        raise QueryError(f"no message #{msg_id}")
    chat_id, ts = row
    older = conn.execute(
        "SELECT * FROM msgs WHERE chat_id = ? AND kind != 'system' "
        "AND (ts < ? OR (ts = ? AND id < ?)) ORDER BY ts DESC, id DESC LIMIT ?",
        (chat_id, ts, ts, msg_id, before)).fetchall()
    newer = conn.execute(
        "SELECT * FROM msgs WHERE chat_id = ? AND (kind != 'system' OR id = ?) "
        "AND (ts > ? OR (ts = ? AND id >= ?)) ORDER BY ts, id LIMIT ?",
        (chat_id, msg_id, ts, ts, msg_id, after + 1)).fetchall()
    return older[::-1] + newer


# --- fuzzy ---------------------------------------------------------------

def distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance, giving up (returning limit + 1) past `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            cur.append(cost)
            best = min(best, cost)
        if best > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def max_typos(word: str) -> int:
    return 0 if len(word) < 4 else 1 if len(word) < 8 else 2


def _grams(word):
    return {word[i:i + 3] for i in range(len(word) - 2)}


@functools.lru_cache(maxsize=None)
def _vocab_by_length(conn):
    by_len = {}
    for word, n in conn.execute("SELECT word, n FROM vocab"):
        by_len.setdefault(len(word), []).append((word, n))
    return by_len


def similar_words(conn, word: str, limit: int = 8) -> list[str]:
    """Indexed words within a few typos of `word`, most frequent first."""
    typos = max_typos(word)
    if not typos:
        return [word]
    grams = _grams(word)
    need = max(1, len(grams) - 3 * typos)
    by_len = _vocab_by_length(conn)
    found = []
    for length in range(len(word) - typos, len(word) + typos + 1):
        for cand, n in by_len.get(length, ()):
            if len(grams & _grams(cand)) >= need:
                d = distance(word, cand, typos)
                if d <= typos:
                    found.append((d, -n, cand))
    found.sort()
    words = [w for _, _, w in found[:limit]]
    return words if word in words else [word] + words[:limit - 1]


def expand_fuzzy(conn, q: Query) -> bool:
    """Fill q.expansions with typo-tolerant alternatives for each term.
    Returns False when no term gained an alternative."""
    gained = False
    for term in q.terms:
        words = WORD.findall(fold(term))
        # Only plain words: digits and codes are rarely typos of each other.
        if len(words) != 1 or words[0] != fold(term).strip() or not words[0].isalpha():
            continue
        alternatives = similar_words(conn, words[0])
        q.expansions[term] = alternatives
        gained |= len(alternatives) > 1
    return gained


# --- chats ---------------------------------------------------------------

def find_chats(conn, patterns: list[str]) -> tuple[list, bool]:
    """Chats whose name or number contains any pattern (accent- and
    case-insensitive). A pattern with no such chat falls back to typo-tolerant
    matching on name words; the flag says whether any pattern needed that."""
    chats = conn.execute("SELECT id, name, jid, kind, norm FROM chats").fetchall()
    hits, fuzzy = {}, False
    for p in patterns:
        fp = fold(p).strip()
        found = [c for c in chats if _contains(c, fp)]
        if not found:
            words = WORD.findall(fp)
            found = [c for c in chats if words and all(
                any(distance(w, cw, max(1, max_typos(w))) <= max(1, max_typos(w))
                    for cw in WORD.findall(c[4])) for w in words)]
            fuzzy |= bool(found)
        for c in found:
            hits[c[0]] = c
    return list(hits.values()), fuzzy


def _contains(chat, pattern: str) -> bool:
    if pattern in chat[4] or (chat[2] and pattern in chat[2]):
        return True
    # Names are full of punctuation and emoji: "schools" finds SCHOOL'S.
    squashed = re.sub(r"[\W_]", "", pattern)
    if len(squashed) >= 3 and squashed in re.sub(r"[\W_]", "", chat[4]):
        return True
    if pattern.isdigit() and len(pattern) >= 4:
        digits = re.sub(r"\D", "", chat[1] + (chat[2] or "").split("@")[0])
        return pattern in digits
    return False
