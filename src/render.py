"""Output: compact text, JSON, CSV and summaries.

Text output is built for both people and agents: one header line per message
(`#id HH:MM sender: text`), day separators instead of repeating dates, and
media described inline (`[video 0:11 2.1M]`) instead of long sandbox paths.
The `#id` is what `wagrep open/show/ctx/path` take.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys

from . import wa
from .index import doc_text, fold, row_dict

SNIPPET = 110


class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _wrap(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def dim(self, t):
        return self._wrap("2", t)

    def bold(self, t):
        return self._wrap("1", t)

    def cyan(self, t):
        return self._wrap("36", t)

    def yellow(self, t):
        return self._wrap("33", t)

    def green(self, t):
        return self._wrap("32", t)


def human_size(size) -> str:
    if not size:
        return ""
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return ""


def human_duration(seconds) -> str:
    if not seconds:
        return ""
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def abspath(msg) -> str | None:
    return os.path.join(wa.MEDIA_ROOT, msg["media_path"]) if msg.get("media_path") else None


def local(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts)


class Renderer:
    def __init__(self, conn, style: Style, full=False, paths=False, terms=()):
        self.conn = conn
        self.style = style
        self.full = full
        self.paths = paths
        self.terms = [fold(t) for t in terms]
        self.chats = {cid: (name, kind) for cid, name, kind in
                      conn.execute("SELECT id, name, kind FROM chats")}
        self._docs = {}

    # --- pieces ----------------------------------------------------------

    def chat_name(self, msg):
        return self.chats.get(msg["chat_id"], ("?", "?"))[0]

    def doc(self, msg_id):
        if msg_id not in self._docs:
            self._docs[msg_id] = self.conn.execute(
                "SELECT status, pages, text FROM docs WHERE id = ?",
                (msg_id,)).fetchone()
        return self._docs[msg_id]

    def pages(self, msg):
        if msg["kind"] != "document":
            return None
        doc = self.doc(msg["id"])
        return (doc and doc[1]) or (msg.get("extra") or {}).get("pages")

    def media_tag(self, msg) -> str | None:
        kind = msg["kind"]
        extra = msg.get("extra") or {}
        if kind == "deleted":
            return "deleted"
        if kind == "poll":
            options = ", ".join(f"{name} {n}" for name, n in extra.get("poll", ()))
            return f"poll: {options}" if options else "poll"
        if kind == "contact":
            return " ".join(x for x in ("contact", extra.get("phone")) if x)
        if kind in ("location", "live-location"):
            if msg.get("lat") is not None:
                return f"{kind} {msg['lat']:.5f},{msg['lon']:.5f}"
            return kind
        if kind == "call":
            return "call"
        if not (msg.get("media_path") or kind in wa.MEDIA_KINDS):
            return None
        parts = [kind]
        if msg.get("filename"):
            parts.append(msg["filename"])
        pages = self.pages(msg)
        if pages:
            parts.append(f"{pages}p")
        parts.append(human_duration(msg.get("duration")))
        parts.append(human_size(msg.get("size")))
        path = abspath(msg)
        if not path or not os.path.exists(path):
            parts.append("not downloaded")
        return " ".join(p for p in parts if p)

    def snippet(self, msg) -> str | None:
        """Where a search term hit inside a document's text, not its message."""
        if not self.terms or msg["kind"] != "document":
            return None
        doc = self.doc(msg["id"])
        if not doc or not doc[2]:
            return None
        body = fold(" ".join(filter(None, (msg.get("text"), msg.get("filename")))))
        text = doc_text(doc[2])
        folded = fold(text)
        for term in self.terms:
            if term in body:
                continue
            pos = folded.find(term)
            if pos >= 0:
                start = max(0, pos - SNIPPET // 2)
                piece = " ".join(text[start:start + SNIPPET].split())
                return f"in file: ...{piece}..."
        return None

    # --- message ---------------------------------------------------------

    def lines(self, msg, show_chat=False, hit=False) -> list[str]:
        st = self.style
        when = local(msg["ts"]).strftime("%H:%M")
        head = [st.dim(("*" if hit else "") + f"#{msg['id']} {when}")]
        sender = msg["sender"] or "?"
        if show_chat and sender != self.chat_name(msg):
            head.append(st.cyan(f"[{self.chat_name(msg)}]"))
        if msg.get("edited"):
            sender += f" (edited {local(msg['edited']).strftime('%H:%M')})"
        tag = self.media_tag(msg)
        label = st.bold(sender) + (" " + st.green(f"[{tag}]") if tag else "")
        text = msg.get("text") or ""
        if text and not self.full and len(text) > 300:
            text = text[:300] + "..."
        text_lines = text.split("\n") if text else []
        first = " ".join(head) + " " + label + (": " + text_lines[0] if text_lines else "")
        out = [first]
        reply = msg.get("reply")
        if reply:
            quoted = " ".join((reply.get("text") or "").split())
            if len(quoted) > 90:
                quoted = quoted[:90] + "..."
            name = reply.get("name") or "?"
            kind = "" if reply.get("kind") in (None, "text") else f" [{reply['kind']}]"
            out.append("  " + st.yellow(f"> {name}{kind}: {quoted}" if quoted
                                        else f"> {name}{kind}"))
        out += ["  " + line for line in text_lines[1:]]
        snippet = self.snippet(msg)
        if snippet:
            out.append("  " + st.yellow(snippet))
        if self.paths and msg.get("media_path"):
            out.append("  " + st.dim(abspath(msg)))
        if msg.get("reactions"):
            counts = {}
            for emoji, _ in msg["reactions"]:
                counts[emoji] = counts.get(emoji, 0) + 1
            out[-1] += " " + st.dim(" ".join(
                e + (str(n) if n > 1 else "") for e, n in counts.items()))
        return out

    def blocks(self, rows, hits=()) -> list[list[str]]:
        msgs = [row_dict(r) for r in rows]
        show_chat = len({m["chat_id"] for m in msgs}) > 1
        return [self.lines(m, show_chat, m["id"] in hits) for m in msgs], msgs

    def text(self, msgs, blocks, out=sys.stdout, gaps=()):
        """Print blocks with a header per chat (single-chat output) and a
        separator per day. `gaps` are indexes where a `--` break goes."""
        st = self.style
        chats = {m["chat_id"] for m in msgs}
        if len(chats) == 1 and msgs:
            name, kind = self.chats.get(msgs[0]["chat_id"], ("?", "?"))
            print(st.cyan(f"== {name} ({kind})"), file=out)
        day = None
        for i, (msg, block) in enumerate(zip(msgs, blocks)):
            if i in gaps:
                print("--", file=out)
                day = None
            d = local(msg["ts"]).strftime("%Y-%m-%d %a")
            if d != day:
                print(st.dim(f"-- {d}"), file=out)
                day = d
            print("\n".join(block), file=out)


# --- structured ----------------------------------------------------------

def to_json(msg, renderer: Renderer) -> dict:
    out = {
        "id": msg["id"],
        "when": local(msg["ts"]).isoformat(sep=" ", timespec="seconds"),
        "chat": renderer.chat_name(msg),
        "sender": msg["sender"],
        "from_me": bool(msg["from_me"]),
        "type": msg["kind"],
        "text": msg.get("text"),
    }
    if msg.get("media_path") or msg["kind"] in wa.MEDIA_KINDS:
        path = abspath(msg)
        out["media"] = {
            "filename": msg.get("filename"), "path": path,
            "mime": msg.get("mime"), "size": msg.get("size"),
            "duration": msg.get("duration"),
            "pages": renderer.pages(msg),
            "exists": bool(path and os.path.exists(path)),
        }
    if msg.get("reply"):
        out["reply_to"] = msg["reply"]
    extra = msg.get("extra") or {}
    for key in ("mentions", "poll", "link_title", "phone", "ephemeral"):
        if extra.get(key):
            out[key] = extra[key]
    if msg.get("lat") is not None:
        out["location"] = [msg["lat"], msg["lon"]]
    if msg.get("edited"):
        out["edited"] = local(msg["edited"]).isoformat(sep=" ", timespec="seconds")
    if msg.get("reactions"):
        out["reactions"] = msg["reactions"]
    if msg.get("starred"):
        out["starred"] = True
    return out


def print_json(msgs, renderer, out=sys.stdout):
    """A JSON array with one message per line: loads with json.load and
    still greps and tails line by line."""
    out.write("[\n")
    for i, msg in enumerate(msgs):
        out.write(json.dumps(to_json(msg, renderer), ensure_ascii=False,
                             separators=(",", ":")))
        out.write(",\n" if i < len(msgs) - 1 else "\n")
    out.write("]\n")


def print_csv(msgs, renderer, out=sys.stdout):
    writer = csv.writer(out)
    writer.writerow(["id", "when", "chat", "sender", "from_me", "type", "text",
                     "reply_to_name", "reply_to_text", "media", "filename",
                     "mime", "size", "duration"])
    for msg in msgs:
        j = to_json(msg, renderer)
        reply = j.get("reply_to") or {}
        media = j.get("media") or {}
        writer.writerow([
            j["id"], j["when"], j["chat"], j["sender"], int(j["from_me"]),
            j["type"], j["text"] or "", reply.get("name") or "",
            reply.get("text") or "", media.get("path") or "",
            media.get("filename") or "", media.get("mime") or "",
            media.get("size") or "", media.get("duration") or ""])


# --- budget and summaries ------------------------------------------------

def fit(blocks, budget: int) -> int:
    """Index of the first block such that the newest blocks fit in `budget`
    characters (always at least one)."""
    if not budget:
        return 0
    used = 0
    for i in range(len(blocks) - 1, -1, -1):
        used += sum(len(line) + 1 for line in blocks[i])
        if used > budget:
            return min(i + 1, len(blocks) - 1)
    return 0


def fit_oldest(blocks, budget: int) -> int:
    """Number of oldest blocks that fit in `budget` characters (at least one)."""
    if not budget:
        return len(blocks)
    used = 0
    for i, block in enumerate(blocks):
        used += sum(len(line) + 1 for line in block)
        if used > budget:
            return max(1, i)
    return len(blocks)


def _days(days) -> str:
    if len(days) <= 40:
        year = None
        parts = []
        for d, n in days:
            parts.append(f"{d if d[:4] != year else d[5:]} {n}")
            year = d[:4]
        return ", ".join(parts)
    months = {}
    for d, n in days:
        months[d[:7]] = months.get(d[:7], 0) + n
    return ", ".join(f"{m} {n}" for m, n in months.items()) + \
        "  (by month; narrow with -s/-u for days)"


def summary_text(s: dict, top: int = 12) -> str:
    if not s["total"]:
        return "0 messages"
    first = local(s["first"]).strftime("%Y-%m-%d %H:%M")
    last = local(s["last"]).strftime("%Y-%m-%d %H:%M")

    def row(label, items):
        return f"{label:<8} " + ", ".join(f"{k or '?'} {n}" for k, n in items[:top])

    return "\n".join([
        f"{s['total']:,} messages, {first} .. {last}",
        row("chats:", s["chats"]),
        row("senders:", s["senders"]),
        row("types:", s["kinds"]),
        f"{'days:':<8} {_days(s['days'])}",
    ])
