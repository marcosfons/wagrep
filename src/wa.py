"""WhatsApp Desktop for macOS as a data source.

Reads ChatStorage.sqlite (Core Data) through a private snapshot and decodes
each message row into a plain dict that `index.py` stores. Nothing here knows
about searching or output, so a future source (WhatsApp for Windows, a Web
export) only has to produce the same dicts.

Safety contract, enforced here:
  * WhatsApp's live database is never opened by SQLite. It is byte-copied
    (APFS clonefile) to the cache directory; only the copy is opened.
  * The copy is opened with `query_only` and an authorizer that denies every
    action except reads, so no code path can write to it.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import shutil
import sqlite3
import subprocess

log = logging.getLogger("wagrep.wa")

WA_DIR = os.path.expanduser(
    "~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared")
SRC = os.path.join(WA_DIR, "ChatStorage.sqlite")
MEDIA_ROOT = os.path.join(WA_DIR, "Message")
CACHE = os.environ.get("WAGREP_CACHE") or os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "wagrep")
SNAP = os.path.join(CACHE, "ChatStorage.sqlite")
EPOCH = 978307200  # Core Data epoch (2001-01-01) -> Unix epoch
SUFFIXES = ("", "-wal", "-shm")

# ZWAMESSAGE.ZMESSAGETYPE. Media types were verified by bucketing on-disk file
# extensions per type; the rest by sampling rows and their metadata protobuf.
# 2 is video and 3 is audio, not the other way round.
TYPES = {
    0: "text", 1: "image", 2: "video", 3: "audio", 4: "contact",
    5: "location", 6: "system", 7: "link", 8: "document", 10: "system",
    11: "gif", 12: "system", 13: "live-location", 14: "deleted",
    15: "sticker", 19: "template", 20: "template", 23: "template",
    25: "reply-button", 26: "image", 28: "system", 30: "interactive",
    31: "reply-button", 32: "template", 33: "template", 34: "reply-button",
    41: "interactive", 42: "template", 43: "template", 46: "poll",
    54: "video-note", 55: "system", 59: "call", 66: "system",
    68: "status-mention",
}
MEDIA_KINDS = frozenset(
    ("image", "video", "audio", "document", "gif", "sticker", "video-note"))
SPEECH_KINDS = frozenset(("audio", "video", "video-note"))
# Business templates and bot menus keep their visible text inside the metadata
# protobuf instead of ZTEXT, so it has to be harvested from there.
PROTO_TEXT_KINDS = frozenset(("template", "interactive", "reply-button"))

# Inside a quoted message: WhatsApp Message proto field -> (kind, text field).
# These are the wire proto numbers, unrelated to ZMESSAGETYPE above.
QUOTED_KINDS = {
    1: ("text", None), 2: ("sender-key", None), 3: ("image", 3),
    4: ("contact", 1), 5: ("location", None), 6: ("text", 1),
    7: ("document", 5), 8: ("audio", None), 9: ("video", 3),
    14: ("sticker", None),
}

# Bidi isolates and marks WhatsApp wraps around phone-number names, plus the
# non-breaking characters inside them, which make names impossible to type.
_NAME_JUNK = str.maketrans({
    "‪": None, "‬": None, "‎": None, "‏": None,
    " ": " ", "‑": "-",
})
# ZWAMESSAGE.ZPUSHNAME is base64-like protobuf junk on most group messages:
# one long token mixing case and digits, which real names essentially never are.
_JUNK_PUSHNAME = re.compile(
    r"^(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9+/]{16,}={0,2}$")


class SourceError(Exception):
    """The WhatsApp database is missing or unreadable; message is user-facing."""


# --- read-only access ----------------------------------------------------

# Numeric fallbacks: Apple's python3 (3.9) lacks the SQLITE_* constants.
_OK = getattr(sqlite3, "SQLITE_OK", 0)
_DENY = getattr(sqlite3, "SQLITE_DENY", 1)
_READ_ACTIONS = frozenset((
    getattr(sqlite3, "SQLITE_SELECT", 21),
    getattr(sqlite3, "SQLITE_READ", 20),
    getattr(sqlite3, "SQLITE_FUNCTION", 31),
))


def deny_writes(action, *_):
    """SQLite authorizer: allow reads, refuse everything else."""
    return _OK if action in _READ_ACTIONS else _DENY


def lock_read_only(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute("PRAGMA query_only = ON")
    conn.set_authorizer(deny_writes)
    return conn


def source_signature() -> str:
    """Size and mtime of the live DB and its WAL.

    WhatsApp runs in WAL mode: new messages land in the -wal file and the main
    file's mtime does not move for hours, so both must be part of the check.
    """
    parts = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(SRC + suffix)
            parts.append(f"{st.st_size}:{st.st_mtime_ns}")
        except FileNotFoundError:
            parts.append("-")
        except PermissionError as exc:
            raise SourceError(
                f"cannot read {SRC} (permission denied)\n"
                "  Grant your terminal Full Disk Access in System Settings > "
                "Privacy & Security.") from exc
    if parts[0] == "-":
        raise SourceError(
            f"no WhatsApp database at {SRC}\n"
            "  Install WhatsApp Desktop and sign in, then retry.")
    return "|".join(parts)


def snapshot() -> sqlite3.Connection:
    """Clone the live DB into the cache and open the clone read-only."""
    if not os.access(SRC, os.R_OK):
        raise SourceError(
            f"cannot read {SRC}\n  Grant your terminal Full Disk Access in "
            "System Settings > Privacy & Security.")
    os.makedirs(CACHE, mode=0o700, exist_ok=True)
    discard_snapshot()
    for suffix in SUFFIXES:
        src = SRC + suffix
        if not os.path.exists(src):
            continue
        # cp -c is an APFS clone: instant and free of extra disk space.
        if subprocess.call(["cp", "-c", src, SNAP + suffix],
                           stderr=subprocess.DEVNULL) != 0:
            log.warning("clonefile failed for %s, falling back to a copy", src)
            shutil.copyfile(src, SNAP + suffix)
    # Opened read-write on purpose: this is our private clone, and a read-only
    # handle cannot replay a WAL that needs recovery. Writes are then locked.
    return lock_read_only(sqlite3.connect(SNAP))


def discard_snapshot():
    for suffix in SUFFIXES:
        try:
            os.remove(SNAP + suffix)
        except FileNotFoundError:
            pass


# --- protobuf ------------------------------------------------------------
# Just enough wire-format parsing to walk WhatsApp's metadata blobs.

def _varint(buf, i):
    result = shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, i


def pb(buf) -> dict:
    """Decode one protobuf message into {field_number: [values]}."""
    out = collections.defaultdict(list)
    if not isinstance(buf, (bytes, bytearray)):
        return out
    i, end = 0, len(buf)
    while i < end:
        try:
            key, i = _varint(buf, i)
            field, wire = key >> 3, key & 7
            if wire == 2:
                length, i = _varint(buf, i)
                out[field].append(buf[i:i + length])
                i += length
            elif wire == 0:
                value, i = _varint(buf, i)
                out[field].append(value)
            elif wire == 5:
                i += 4
            elif wire == 1:
                i += 8
            else:
                break
        except IndexError:
            break
    return out


def utf8(value) -> str | None:
    if not isinstance(value, (bytes, bytearray)):
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _first(fields, n):
    values = fields.get(n)
    return values[0] if values else None


def harvest_text(buf, depth=0, out=None) -> list[str]:
    """Human text nested anywhere in a protobuf (template bodies, buttons).

    A length-delimited field is taken as text when it decodes as UTF-8, has a
    space or is a short label, and is not a URL, media path or hash; otherwise
    it is walked as a nested message.
    """
    out = [] if out is None else out
    if depth > 5:
        return out
    for values in pb(buf).values():
        for value in values:
            if not isinstance(value, (bytes, bytearray)) or len(value) < 2:
                continue
            text = utf8(value)
            if text and not all(c.isprintable() or c in "\r\n\t" for c in text):
                text = None
            if text and " " in text and not text.startswith(("http", "/v/t")):
                if text not in out:
                    out.append(text)
            else:
                harvest_text(value, depth + 1, out)
    return out


def context_info(blob) -> dict:
    """Reply, mentions and disappearing-timer data from ZWAMEDIAITEM.ZMETADATA."""
    if not blob:
        return {}
    fields = pb(blob)
    ctx = {}
    if 8 in fields:
        ctx["mentions"] = [j for j in map(utf8, fields[8]) if j]
    if 29 in fields and isinstance(fields[29][0], int) and fields[29][0]:
        ctx["ephemeral"] = fields[29][0]
    if 19 in fields:
        reply = {"stanza": utf8(_first(fields, 5)),
                 # Verified as the quoted author on 99% of 12k replies where
                 # the quoted message was also local; mismatches were the
                 # same person under another name form.
                 "jid": utf8(_first(fields, 6))}
        quoted = pb(fields[19][0])
        for field, (kind, text_sub) in QUOTED_KINDS.items():
            if field not in quoted:
                continue
            reply["kind"] = kind
            value = quoted[field][0]
            if field == 1:
                reply["text"] = utf8(value)
            elif text_sub is not None:
                reply["text"] = utf8(_first(pb(value), text_sub))
            break
        ctx["reply"] = reply
    ctx["fields"] = fields
    return ctx


def receipt_info(blob) -> dict:
    """Edit, reactions and poll data from ZWAMESSAGEINFO.ZRECEIPTINFO.

    Field 10 is the edit record (new text + edit time in ms; WhatsApp keeps
    no earlier version), field 7 the reactions, field 8 the poll.
    Only edits made from this account are recorded here.
    """
    if not blob:
        return {}
    fields = pb(blob)
    out = {}
    if 10 in fields:
        ms = _first(pb(fields[10][0]), 4)
        if isinstance(ms, int):
            out["edited"] = ms / 1000
    reactions = []
    for wrapper in fields.get(7, ()):
        for raw in pb(wrapper).get(1, ()):
            r = pb(raw)
            emoji = utf8(_first(r, 3))
            if emoji:
                reactions.append((emoji, utf8(_first(r, 2))))
    if reactions:
        out["reactions"] = reactions
    if 8 in fields:
        poll = pb(fields[8][0])
        options = [utf8(_first(pb(o), 1)) or "?" for o in poll.get(3, ())]
        votes = {}
        for raw in poll.get(5, ()):
            v = pb(raw)
            idx, voter = _first(v, 1), utf8(_first(v, 4))
            if isinstance(idx, int):
                votes[(idx, voter)] = 1
        tally = collections.Counter(idx for idx, _ in votes)
        out["poll"] = {
            "question": utf8(_first(poll, 2)),
            "options": [[name, tally.get(i, 0)] for i, name in enumerate(options)],
        }
    return out


# --- names ---------------------------------------------------------------

def clean_name(name: str | None) -> str | None:
    if not name:
        return None
    return name.translate(_NAME_JUNK).strip() or None


class Names:
    """JID -> display name, from ZWAPROFILEPUSHNAME."""

    def __init__(self, conn: sqlite3.Connection):
        self.push = {}
        self.by_number = {}
        for jid, name in conn.execute(
                "SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME"):
            name = clean_name(name)
            if jid and name:
                self.push[jid] = name
                self.by_number.setdefault(jid.split("@")[0], name)

    def known(self, jid):
        if not jid:
            return None
        return self.push.get(jid) or self.by_number.get(jid.split("@")[0])

    def jid(self, jid):
        """Display name, falling back to the bare number."""
        if not jid:
            return None
        return self.known(jid) or jid.split("@")[0]

    def resolve_mentions(self, text):
        """Rewrite inline @<phone|lid> mentions into @<name>."""
        if not text or "@" not in text:
            return text
        return re.sub(r"@(\d{8,})",
                      lambda m: "@" + (self.by_number.get(m.group(1)) or m.group(1)),
                      text)


# --- rows ----------------------------------------------------------------

def chat_kind(jid: str | None, session_type) -> str:
    jid = jid or ""
    if jid.endswith("@g.us"):
        return "group"
    if jid.endswith("@newsletter"):
        return "channel"
    if jid.endswith("@broadcast"):
        return "broadcast"
    return "dm" if session_type == 0 else "group"


def chats(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT Z_PK, ZPARTNERNAME, ZCONTACTJID, ZSESSIONTYPE, ZARCHIVED, "
        "ZHIDDEN, ZREMOVED FROM ZWACHATSESSION")
    # Hidden (locked) and removed chats are treated like archived ones.
    return [{"id": pk, "name": clean_name(name) or jid or "(unknown)", "jid": jid,
             "kind": chat_kind(jid, stype),
             "archived": int(bool(archived or hidden or removed))}
            for pk, name, jid, stype, archived, hidden, removed in rows]


SIGNATURES = f"""
SELECT m.Z_PK, m.Z_OPT, COALESCE(mi.Z_OPT, 0), COALESCE(i.Z_OPT, 0),
       m.ZCHATSESSION, m.ZMESSAGEDATE + {EPOCH}
  FROM ZWAMESSAGE m
  LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
  LEFT JOIN ZWAMESSAGEINFO i ON i.Z_PK = m.ZMESSAGEINFO
 WHERE m.Z_PK > ?
"""


def signatures(conn, after_pk: int) -> dict[int, tuple]:
    """Z_PK -> (change counter, chat id, unix time). Core Data bumps Z_OPT on
    every row update, so an edit, a reaction or a finished download changes
    the counter; chat and time let the index apply its scope rules."""
    return {pk: (m + (mi << 20) + (i << 40), chat, ts)
            for pk, m, mi, i, chat, ts in conn.execute(SIGNATURES, (after_pk,))}


ROWS = f"""
SELECT m.Z_PK, m.ZMESSAGEDATE + {EPOCH}, m.ZCHATSESSION, cs.ZSESSIONTYPE,
       cs.ZPARTNERNAME, m.ZISFROMME, m.ZMESSAGETYPE, m.ZTEXT, m.ZPUSHNAME,
       m.ZSTARRED, gm.ZMEMBERJID, gm.ZCONTACTNAME,
       mi.ZMEDIALOCALPATH, mi.ZTITLE, mi.ZAUTHORNAME, mi.ZVCARDNAME, mi.ZVCARDSTRING,
       mi.ZFILESIZE, mi.ZMOVIEDURATION, mi.ZLATITUDE, mi.ZLONGITUDE,
       mi.ZMETADATA, i.ZRECEIPTINFO
  FROM ZWAMESSAGE m
  LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
  LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
  LEFT JOIN ZWAMEDIAITEM mi ON mi.Z_PK = m.ZMEDIAITEM
  LEFT JOIN ZWAMESSAGEINFO i ON i.Z_PK = m.ZMESSAGEINFO
"""


def rows(conn, where: str, params=()):
    return conn.execute(f"{ROWS} WHERE {where}", params)


def authors_by_stanza(conn, names: Names, stanzas: list) -> dict:
    """Sender name of each locally stored message, by stanza id."""
    found = {}
    for i in range(0, len(stanzas), 400):
        chunk = stanzas[i:i + 400]
        for stanza, from_me, member_jid, contact_name, partner in conn.execute(
                f"""SELECT m.ZSTANZAID, m.ZISFROMME, gm.ZMEMBERJID,
                           gm.ZCONTACTNAME, cs.ZPARTNERNAME
                      FROM ZWAMESSAGE m
                      LEFT JOIN ZWAGROUPMEMBER gm ON gm.Z_PK = m.ZGROUPMEMBER
                      LEFT JOIN ZWACHATSESSION cs ON cs.Z_PK = m.ZCHATSESSION
                     WHERE m.ZSTANZAID IN ({','.join('?' * len(chunk))})""", chunk):
            name = ("me" if from_me else clean_name(contact_name)
                    or names.known(member_jid) or clean_name(partner))
            if name:
                found[stanza] = name
    return found


def _vcard_name(vcard: str | None) -> str | None:
    match = re.search(r"^FN:(.+)$", vcard or "", re.M)
    return match.group(1).strip() if match else None


def _vcard_phone(vcard: str | None) -> str | None:
    match = re.search(r"^TEL[^:]*:(.+)$", vcard or "", re.M)
    return match.group(1).strip() if match else None


def filename_for(text, media_path):
    """The sender's own filename for a document, with its extension restored.

    On disk files are named by UUID; WhatsApp keeps the human name in the
    database and often drops the extension ("Result_5" for "Result_5.xlsx").
    """
    stored = os.path.basename(media_path or "")
    ext = os.path.splitext(stored)[1]
    name = (text or "").strip().replace("/", "_").replace("\n", " ")
    if not name or len(name) > 160:
        return stored or None
    if ext and not name.lower().endswith(ext.lower()):
        name += ext
    return name


def decode(row, names: Names) -> dict:
    """One ZWAMESSAGE row -> the message dict stored in the index."""
    (pk, ts, chat_id, session_type, partner, from_me, mtype, text, pushname,
     starred, member_jid, contact_name, media_path, title, author, vcard_name,
     vcard, size, duration, lat, lon, metadata, receipt) = row
    kind = TYPES.get(mtype, "system" if not text else "text")
    partner = clean_name(partner)

    if from_me:
        sender = "me"
    elif session_type == 0:
        sender = partner or "?"
    else:
        sender = (clean_name(contact_name) or names.known(member_jid)
                  or (pushname if pushname and not _JUNK_PUSHNAME.match(pushname)
                      else None)
                  or names.jid(member_jid) or "?")

    ctx = context_info(metadata)
    rec = receipt_info(receipt)
    msg = {"id": pk, "ts": ts, "chat_id": chat_id, "sender": sender,
           "from_me": int(bool(from_me)), "kind": kind,
           "starred": int(bool(starred))}

    body = None
    extra = {}
    if kind == "document":
        # ZAUTHORNAME holds the sender's filename; older rows only have it in
        # ZTEXT, where newer rows put the caption instead.
        msg["filename"] = filename_for(author or text, media_path)
        caption = text if author and text and text.strip() not in (
            author.strip(), os.path.splitext(author.strip())[0]) else None
        body = caption or (title if title and title != text else None)
    elif kind == "link":
        body = text
        if title:
            extra["link_title"] = title
    elif kind == "deleted":
        body = None
    elif kind == "contact":
        body = clean_name(vcard_name) or _vcard_name(vcard)
        phone = _vcard_phone(vcard)
        if phone:
            extra["phone"] = phone
    elif kind in ("location", "live-location"):
        body = text or (title if title and " " in title else None)
        if lat or lon:
            msg["lat"], msg["lon"] = lat, lon
    elif kind == "poll":
        poll = rec.get("poll")
        if poll:
            body = poll["question"]
            extra["poll"] = poll["options"]
    else:
        body = text or title

    if kind in PROTO_TEXT_KINDS and metadata:
        harvested = harvest_text(metadata)
        if harvested:
            body = "\n".join(dict.fromkeys(([body] if body else []) + harvested))
    msg["text"] = names.resolve_mentions(body) if body else None

    if kind in MEDIA_KINDS or media_path:
        msg["media_path"] = media_path
        msg["mime"] = vcard if vcard and "/" in vcard and len(vcard) < 120 else None
        msg["size"] = size or None
        if kind == "document":
            # For documents ZMOVIEDURATION holds the page count.
            if duration:
                extra["pages"] = duration
        else:
            msg["duration"] = duration or None

    if ctx.get("mentions"):
        extra["mentions"] = [names.jid(j) for j in ctx["mentions"]]
    if ctx.get("ephemeral"):
        extra["ephemeral"] = ctx["ephemeral"]
    if ctx.get("reply"):
        reply = ctx["reply"]
        reply["name"] = names.known(reply.get("jid"))
        if reply.get("text"):
            reply["text"] = names.resolve_mentions(reply["text"])
        msg["reply"] = reply
    if rec.get("edited"):
        msg["edited"] = rec["edited"]
    if rec.get("reactions"):
        msg["reactions"] = [[e, "me" if j is None else names.jid(j)]
                            for e, j in rec["reactions"]]
    if extra:
        msg["extra"] = extra
    return msg
