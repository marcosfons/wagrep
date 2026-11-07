"""Command line: argument parsing, logging and the commands."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import logging.handlers
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

from . import index, render, search, transcribe, wa

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LAUNCHER = os.path.join(ROOT, "wagrep")
LOG_FILE = os.path.join(ROOT, "logs", "wagrep.log")
VERSION = "2.0.0"
AGENT_BUDGET = 10000
log = logging.getLogger("wagrep")

KIND_ALIASES = {"doc": "document", "docs": "document", "pdf": "document",
                "voice": "audio", "photo": "image", "img": "image",
                "ptv": "video-note", "vnote": "video-note"}
KINDS = sorted(set(wa.TYPES.values()) | {"media"})

DESCRIPTION = """\
grep your WhatsApp Desktop history (macOS). Read-only: it cannot send,
edit or delete anything.

commands (default: search):
  wagrep [terms] [filters]     search; terms are ANDed, accent/case-insensitive
  wagrep chats [name...]       find chats by name (typo-tolerant)
  wagrep ctx ID [N]            N messages before/after message #ID
  wagrep show ID               every detail of one message
  wagrep open ID|path [-R]     open the attachment (or reveal it in Finder)
  wagrep path ID...            absolute path of attachments
  wagrep stats [filters]       counts by chat/sender/type/day, no messages
  wagrep pick [filters]        interactive fuzzy finder (fzf)
  wagrep transcribe ID...      transcribe voice notes (explicit; may cost money)
  wagrep sql "SELECT ..."      read-only SQL on the index (see: wagrep guide)
  wagrep index [--rebuild]     sync now, read all documents, show status
  wagrep guide                 reference for agents: output, JSON, schema"""

EPILOG = """\
examples:
  wagrep invoice -c Ana -s 7d          what Ana said about invoices this week
  wagrep -x "Beach trip" -d today      today's conversation in that group
  wagrep -o proposal contract -s 2026-10   any of the words since October
  wagrep "LA 3021"                     also finds LA3021 / la-3021
  wagrep churasco                      no hit? retries typo-tolerant (churrasco)
  wagrep --files budget                documents named or containing "budget"
  wagrep -T audio -c Bruno -n 5        last voice notes, with durations
  wagrep ctx 381131                    the conversation around a hit
  wagrep open 381131                   open the attachment of "#381131"

Agents: output is capped (~10k chars) inside Claude Code and the rest is
summarized on stderr; narrow with -s/-u/-d/-c/-f or pass --budget 0."""


# --- setup ---------------------------------------------------------------

def setup_logging(verbose: int):
    root = logging.getLogger("wagrep")
    root.setLevel(logging.DEBUG)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    except OSError as exc:
        print(f"wagrep: cannot write {LOG_FILE}: {exc}", file=sys.stderr)
    if verbose:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter("wagrep %(levelname)s: %(message)s"))
        root.addHandler(console)


def agent_mode() -> bool:
    return bool(os.environ.get("CLAUDECODE") or os.environ.get("AI_AGENT")
                or os.environ.get("WAGREP_AGENT"))


def budget_of(args) -> int:
    if args.budget is not None:
        return args.budget
    env = os.environ.get("WAGREP_BUDGET")
    if env is not None:
        return int(env)
    return AGENT_BUDGET if agent_mode() else 0


def note(text: str):
    sys.stdout.flush()  # keep notes after the output they refer to
    print(f"wagrep: {text}", file=sys.stderr)


def open_index(args, sync=True) -> sqlite3.Connection:
    conn = index.connect()
    if sync and not args.no_sync and not os.environ.get("WAGREP_NO_SYNC"):
        index.ensure_fresh(conn, force=args.refresh)
    conn.row_factory = sqlite3.Row
    search.register(conn)
    return conn


# --- arguments -----------------------------------------------------------

def common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("sync and output")
    g.add_argument("--refresh", action="store_true",
                   help="pull WhatsApp's latest changes now (default: when "
                        "older than 15s)")
    g.add_argument("--no-sync", action="store_true",
                   help="read the index as it is, without syncing")
    g.add_argument("--budget", type=int, metavar="CHARS",
                   help="cap output size (default 10000 for agents, else "
                        "none; 0 = no cap)")
    g.add_argument("-v", "--verbose", action="count", default=0,
                   help="debug log, also mirrored to stderr")
    return p


def filter_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("terms", nargs="*", help="search terms")
    g = p.add_argument_group("filters")
    g.add_argument("-c", "--chat", metavar="NAME", action="append", default=[],
                   help="chats whose name/number contains NAME (repeatable, ORed)")
    g.add_argument("-x", "--export", metavar="NAME", action="append", default=[],
                   help="whole chats, chronological, no -n limit (repeatable)")
    g.add_argument("-f", "--from", dest="senders", metavar="NAME",
                   action="append", default=[], help="sender name contains NAME")
    g.add_argument("-s", "--since", metavar="WHEN",
                   help="on/after WHEN: 2026-08-01, '2026-08-01 09:30', 7d, 3h, today")
    g.add_argument("-u", "--until", metavar="WHEN", help="before WHEN")
    g.add_argument("-d", "--day", metavar="DATE", help="only that day")
    g.add_argument("-m", "--from-me", dest="from_me", action="store_const",
                   const=1, help="only messages you sent")
    g.add_argument("-t", "--from-them", dest="from_me", action="store_const",
                   const=0, help="only messages you received")
    g.add_argument("-T", "--type", dest="kinds", metavar="TYPE",
                   action="append", default=[],
                   help="text,image,video,audio,document,sticker,poll,link,"
                        "deleted,video-note,... (repeatable or comma list)")
    g.add_argument("--media", action="store_true", help="only attachments")
    g.add_argument("--files", action="store_true",
                   help="only documents (PDF, spreadsheets, ...)")
    g.add_argument("--edited", action="store_true", help="only edited messages")
    g.add_argument("--starred", action="store_true", help="only starred messages")
    g = p.add_argument_group("matching")
    g.add_argument("-o", "--any", action="store_true",
                   help="match ANY term instead of all")
    g.add_argument("-e", "--regex", action="store_true",
                   help="terms are regular expressions")
    g.add_argument("-z", "--fuzzy", action="store_true",
                   help="always tolerate typos (default: only when nothing matches)")
    g.add_argument("--exact", action="store_true", help="never tolerate typos")
    return p


def search_parser(common, filters) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wagrep", description=DESCRIPTION, epilog=EPILOG,
        parents=[filters, common], formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_argument_group("results")
    g.add_argument("-n", "--limit", type=int, default=50, metavar="N",
                   help="max messages (default 50, 0 = all)")
    g.add_argument("--oldest", action="store_true",
                   help="take the oldest matches instead of the newest")
    g.add_argument("-k", "--ctx", type=int, default=0, metavar="N",
                   help="also show N messages around each hit (in its chat)")
    g.add_argument("-A", "--all", action="store_true",
                   help="full text (default cuts messages at 300 chars)")
    g.add_argument("-C", "--count", action="store_true", help="print the count only")
    g.add_argument("-l", "--chats", action="store_true",
                   help="chats with matches, with counts")
    g.add_argument("--summary", action="store_true",
                   help="counts by chat/sender/type/day instead of messages")
    g.add_argument("--paths", action="store_true",
                   help="print each attachment's absolute path")
    g.add_argument("--json", action="store_true",
                   help="JSON array, one message per line")
    g.add_argument("--csv", action="store_true", help="CSV")
    g.add_argument("--extract", metavar="DIR",
                   help="copy matching attachments to DIR with readable names")
    g.add_argument("--guide", action="store_true", help=argparse.SUPPRESS)
    g.add_argument("--version", action="version", version=f"wagrep {VERSION}")
    return p


def build_query(conn, args) -> search.Query:
    q = search.Query(terms=list(args.terms), any_term=args.any, regex=args.regex,
                     fuzzy=True if args.fuzzy else False if args.exact else None,
                     senders=args.senders, from_me=args.from_me,
                     media=args.media, files=args.files, edited=args.edited,
                     starred=args.starred)
    if args.regex:
        for term in q.terms:
            try:
                re.compile(index.unaccent(term))
            except re.error as exc:
                raise search.QueryError(f"bad regex {term!r}: {exc}") from None
    for flag, patterns in (("-c", args.chat), ("-x", args.export)):
        if not patterns:
            continue
        chats, fuzzy = search.find_chats(conn, patterns)
        if not chats:
            raise search.QueryError(
                f"{flag}: no chat matches {', '.join(map(repr, patterns))}\n"
                "  List chats with: wagrep chats <part of the name>")
        if fuzzy:
            note(f"no chat named {', '.join(map(repr, patterns))}; using "
                 + ", ".join(c[1] for c in chats[:5]))
        scopes = {cid: (scope, since, archived) for cid, scope, since, archived in conn.execute(
            f"SELECT id, scope, since, archived FROM chats WHERE id IN "
            f"({','.join(str(c[0]) for c in chats)})")}
        hidden = [c for c in chats if scopes[c[0]][1] == float("inf")]
        if hidden:
            names = ", ".join(c[1] for c in hidden[:5])
            why = "archived" if all(scopes[c[0]][2] for c in hidden) else "scope: skip"
            hint = (f"not indexed ({why}): {names}\n"
                    f"  To search it: wagrep chats --keep {shlex.quote(hidden[0][1])} [--span 1y]")
            if len(hidden) == len(chats):
                raise search.QueryError(hint)
            note(hint)
        windowed = [(c[1], scopes[c[0]]) for c in chats
                    if scopes[c[0]][1] not in (None, float("inf"))]
        if windowed:
            name, (label, since, _) = windowed[0]
            q.scope_hint = (f"{name}: only messages since {render.local(since):%Y-%m-%d} "
                            f"are indexed ({label}); more: wagrep chats --keep "
                            f"{shlex.quote(name)} [--span 1y]")
        ids = [c[0] for c in chats]
        q.chat_ids = ids if q.chat_ids is None else \
            [i for i in q.chat_ids if i in set(ids)]
    default = index.load_config("scope").get("default", "all")
    if q.chat_ids is None and default != "all":
        q.window_start = time.time() - index.parse_span(default)
        q.scope_hint = (f"only the last {default} are indexed, plus chats kept in full "
                        "(wagrep chats --keep NAME [--span 1y] for older history)")
    if args.day:
        q.since = search.parse_when(args.day, "-d/--day")
        q.until = search.day_after(q.since)
    if args.since:
        q.since = search.parse_when(args.since, "-s/--since")
    if args.until:
        q.until = search.parse_when(args.until, "-u/--until")
    for value in args.kinds:
        for kind in value.split(","):
            kind = KIND_ALIASES.get(kind.strip().lower(), kind.strip().lower())
            if kind not in KINDS:
                raise search.QueryError(
                    f"-T: unknown type {kind!r}; one of: {', '.join(KINDS)}")
            q.kinds.append(kind)
    if q.fuzzy:
        search.expand_fuzzy(conn, q)
    return q


# --- search --------------------------------------------------------------

def cmd_search(args) -> int:
    if args.guide:
        return cmd_guide(args)
    conn = open_index(args)
    q = build_query(conn, args)

    if args.count:
        print(search.count(conn, q))
        return 0
    if args.chats:
        return list_matching_chats(conn, q, args)
    if args.summary:
        print(render.summary_text(search.summary(conn, q)))
        return 0

    budget = 0 if args.extract or args.csv else budget_of(args)
    limit = 0 if args.export else args.limit
    if budget and not limit:
        limit = max(50, budget // 12)  # more rows than could ever fit
    rows, total = search.run(conn, q, limit, oldest=args.oldest)
    if not rows and q.terms and q.fuzzy is None and not q.regex:
        if search.expand_fuzzy(conn, q):
            rows, total = search.run(conn, q, limit, oldest=args.oldest)
            alternatives = "; ".join(f"{t} -> {' '.join(w)}"
                                     for t, w in q.expansions.items() if len(w) > 1)
            note(f"no exact match; typo-tolerant: {alternatives}")
    reaches_back = (q.since is not None and q.window_start is not None
                    and q.since < q.window_start)
    if q.scope_hint and (not rows or (len(rows) == total and (q.chat_ids or reaches_back))):
        note(q.scope_hint)
    if not rows:
        note("no matches" + (" (in " + ", ".join(
            r[0] for r in conn.execute(
                f"SELECT name FROM chats WHERE id IN ({','.join(map(str, q.chat_ids))}) "
                "LIMIT 5")) + ")" if q.chat_ids else ""))
        return 0

    style = render.Style(sys.stdout.isatty())
    renderer = render.Renderer(conn, style, full=args.all or bool(args.export),
                               paths=args.paths,
                               terms=() if q.regex else q.terms)
    if args.extract:
        return extract([index.row_dict(r) for r in rows], args.extract)

    hits, gaps = set(), set()
    if args.ctx:
        rows, hits, gaps = with_context(conn, rows, args.ctx)
    if args.json or args.csv:
        msgs = [index.row_dict(r) for r in rows]
        if args.json and budget:
            sizes = [[json.dumps(render.to_json(m, renderer), ensure_ascii=False)]
                     for m in msgs]
            msgs = msgs[render.fit(sizes, budget):]
        (render.print_json if args.json else render.print_csv)(msgs, renderer)
    else:
        blocks, msgs = renderer.blocks(rows, hits)
        shown = (slice(0, render.fit_oldest(blocks, budget)) if args.oldest
                 else slice(render.fit(blocks, budget), None))
        renderer.text(msgs[shown], blocks[shown],
                      gaps={g - shown.start for g in gaps})
        msgs = msgs[shown]
    footer(conn, q, msgs, total, args, hits)
    return 0


def with_context(conn, rows, n):
    """Hits plus n messages around each, merged per chat; gaps mark breaks."""
    out, seen, hits, gaps = [], set(), set(), set()
    for row in rows:
        hits.add(row["id"])
    for row in rows:
        if row["id"] in seen:
            continue
        group = search.around(conn, row["id"], n, n)
        fresh = [r for r in group if r["id"] not in seen]
        if out and fresh:
            gaps.add(len(out))
        for r in fresh:
            seen.add(r["id"])
            out.append(r)
    return out, hits, gaps


def footer(conn, q, shown_msgs, total, args, hits):
    """Tell the reader what was left out and how to get at it (stderr)."""
    shown = len([m for m in shown_msgs if not hits or m["id"] in hits])
    if shown >= total:
        return
    if args.oldest:
        last = shown_msgs[-1]["ts"]
        rest = dataclasses.replace(q, since=last + 0.001)
        where = f"oldest {shown} of {total:,}; next page: -s '{render.local(last + 1):%Y-%m-%d %H:%M:%S}' --oldest"
    else:
        first = shown_msgs[0]["ts"]
        rest = dataclasses.replace(q, until=first)
        where = (f"newest {shown} of {total:,} matches; older ones: "
                 f"-u '{render.local(first):%Y-%m-%d %H:%M:%S}'")
    summary = search.summary(conn, rest)
    lines = [where]
    if summary["total"]:
        lines.append("not shown: " + render.summary_text(summary, top=8).replace("\n", "\n  "))
    lines.append("narrow with -s/-u/-d, -c, -f or more terms; "
                 "everything: --budget 0 -n 0")
    note("\n  ".join(lines))


def list_matching_chats(conn, q, args) -> int:
    sql_where, params = search.where(q)
    rows = conn.execute(
        f"SELECT c.name, c.kind, COUNT(*), MAX(m.ts) {search.from_clause(q)} "
        f"JOIN chats c ON c.id = m.chat_id WHERE {sql_where} "
        f"GROUP BY m.chat_id ORDER BY MAX(m.ts) DESC "
        f"{f'LIMIT {int(args.limit)}' if args.limit else ''}", params).fetchall()
    print_chat_rows([(name, kind, n, last) for name, kind, n, last in rows],
                    "matches")
    if q.terms:
        named, fuzzy = search.find_chats(conn, q.terms)
        if named and not fuzzy:
            note("chats whose NAME matches: " + ", ".join(c[1] for c in named[:8])
                 + "  (wagrep chats <name> lists chats by name)")
    return 0


def print_chat_rows(rows, label, scopes=None):
    """rows: (name, kind, count, last ts); scopes: scope label per row."""
    if not rows:
        note("no chats")
        return
    scope_head = f"{'scope':<6} " if scopes else ""
    print(f"{'last':<10}  {'kind':<7} {label:>8}  {scope_head}name")
    for i, (name, kind, n, last) in enumerate(rows):
        day = render.local(last).strftime("%Y-%m-%d") if last else "-"
        scope = f"{scopes[i]:<6} " if scopes else ""
        print(f"{day:<10}  {kind:<7} {n:>8,}  {scope}{name}")


def extract(msgs, dest) -> int:
    """Copy attachments out under readable names. Only reads WhatsApp's files."""
    dest = os.path.expanduser(dest)
    os.makedirs(dest, exist_ok=True)
    copied = skipped = 0
    for msg in msgs:
        src = render.abspath(msg)
        if not src or not os.path.exists(src):
            skipped += 1
            continue
        stamp = render.local(msg["ts"]).strftime("%Y%m%d-%H%M%S")
        sender = re.sub(r"[^\w.-]+", "_", msg["sender"] or "unknown")[:40]
        name = msg.get("filename") or os.path.basename(src)
        out = os.path.join(dest, f"{stamp}_{sender}_{name}")
        if not os.path.exists(out):
            shutil.copyfile(src, out)
        copied += 1
        print(out)
    note(f"{copied} file(s) -> {dest}"
         + (f"; {skipped} skipped (not downloaded on this Mac)" if skipped else ""))
    return 0


# --- other commands ------------------------------------------------------

def cmd_chats(args) -> int:
    conn = open_index(args)
    if args.keep or args.skip or args.reset:
        return set_scope(conn, args)
    counts = {cid: (n, last) for cid, n, last in conn.execute(
        "SELECT chat_id, COUNT(*), MAX(ts) FROM msgs WHERE kind != 'system' "
        "GROUP BY chat_id")}
    if args.names:
        chats, fuzzy = search.find_chats(conn, args.names)
        if fuzzy and chats:
            note("no exact name match; typo-tolerant results")
    else:
        chats = conn.execute("SELECT id, name, jid, kind, norm FROM chats").fetchall()
    scope = dict(conn.execute("SELECT id, scope FROM chats"))
    rows = sorted(((c[1], c[3], *counts.get(c[0], (0, None)), scope.get(c[0]) or "all")
                   for c in chats), key=lambda r: r[3] or 0, reverse=True)
    if args.limit:
        rows = rows[:args.limit]
    print_chat_rows([r[:4] for r in rows], "indexed", [r[4] for r in rows])
    return 0


def set_scope(conn, args) -> int:
    """Per-chat index rules ([chats] in the config), applied by a sync now."""
    if not args.names:
        raise search.QueryError("name the chat(s): wagrep chats --keep NAME")
    chats, _ = search.find_chats(conn, args.names)
    if not chats:
        raise search.QueryError(f"no chat matches {', '.join(map(repr, args.names))}")
    if len(chats) > 5:
        raise search.QueryError(
            f"{len(chats)} chats match, be more specific: "
            + ", ".join(c[1] for c in chats[:8]))
    rule = "skip" if args.skip else args.span if args.keep else None
    if rule:
        try:
            index.parse_span(rule)
        except ValueError as exc:
            raise search.QueryError(f"--span: {exc}") from None
    index.save_config("chats", {c[2]: None if rule is None else f"{rule}  # {c[1]}"
                                for c in chats})
    for c in chats:
        note(f"{c[1]}: {rule or 'back to the default scope'}")
    index.ensure_fresh(conn, force=True)
    return 0


def resolve_targets(conn, targets) -> list:
    """Message ids ("381131", "#381131") or any part of a media path, such as
    the "Media/.../866f708f-....mp4" line wagrep prints, or just the UUID."""
    rows = []
    for target in targets:
        t = target.strip().lstrip("#")
        if t.isdigit():
            row = conn.execute("SELECT * FROM msgs WHERE id = ?", (int(t),)).fetchone()
            if not row:
                raise search.QueryError(f"no message #{t}")
            rows.append(row)
            continue
        t = t.replace(wa.MEDIA_ROOT + "/", "")
        found = conn.execute(
            "SELECT * FROM msgs WHERE media_path LIKE ? ORDER BY ts DESC LIMIT 6",
            (f"%{t}%",)).fetchall()
        if not found:
            raise search.QueryError(f"no attachment path contains {t!r}")
        if len(found) > 1:
            raise search.QueryError(
                f"{t!r} matches {len(found)} attachments, e.g. "
                + ", ".join(f"#{r['id']}" for r in found[:5]) + "; be more specific")
        rows.append(found[0])
    return rows


def _existing_path(row) -> str:
    path = render.abspath(index.row_dict(row))
    if not path:
        raise search.QueryError(f"message #{row['id']} has no attachment")
    if not os.path.exists(path):
        raise search.QueryError(
            f"#{row['id']} was never downloaded to this Mac; open the chat in "
            "WhatsApp and tap the attachment to fetch it")
    return path


def cmd_open(args) -> int:
    conn = open_index(args, sync=False)
    for row in resolve_targets(conn, args.targets):
        path = _existing_path(row)
        subprocess.run(["open", "-R", path] if args.reveal else ["open", path],
                       check=True)
        log.info("opened #%s %s", row["id"], path)
    return 0


def cmd_path(args) -> int:
    conn = open_index(args, sync=False)
    for row in resolve_targets(conn, args.targets):
        print(_existing_path(row))
    return 0


def cmd_ctx(args) -> int:
    conn = open_index(args)
    before = args.before if args.before is not None else args.n
    after = args.after if args.after is not None else args.n
    rows = search.around(conn, args.id, before, after)
    renderer = render.Renderer(conn, render.Style(sys.stdout.isatty()),
                               full=args.all)
    blocks, msgs = renderer.blocks(rows, {args.id})
    renderer.text(msgs, blocks)
    return 0


def cmd_show(args) -> int:
    conn = open_index(args)
    renderer = render.Renderer(conn, render.Style(sys.stdout.isatty()), full=True)
    rows = resolve_targets(conn, args.targets)
    msgs = [index.row_dict(r) for r in rows]
    if args.json:
        render.print_json(msgs, renderer)
        return 0
    for msg in msgs:
        j = render.to_json(msg, renderer)
        chat = renderer.chats.get(msg["chat_id"], ("?", "?"))
        print(f"#{j['id']}  {j['when']}  {chat[0]} ({chat[1]})")
        print(f"from:      {j['sender']}")
        print(f"type:      {j['type']}")
        media = j.get("media")
        if media:
            print("file:      " + " ".join(filter(None, (
                media["filename"], render.human_duration(media["duration"]),
                render.human_size(media["size"]), media["mime"],
                f"{media['pages']} pages" if media["pages"] else None))))
            print(f"path:      {media['path'] or '-'}"
                  + ("" if media["exists"] else "  (not downloaded)"))
        for key in ("edited", "link_title", "phone"):
            if j.get(key):
                print(f"{key + ':':<10} {j[key]}")
        if j.get("location"):
            lat, lon = j["location"]
            print(f"location:  {lat},{lon}  https://maps.google.com/?q={lat},{lon}")
        if j.get("reply_to"):
            r = j["reply_to"]
            print(f"reply to:  {r['name']}: {' '.join((r.get('text') or '').split())[:200]}")
        if j.get("mentions"):
            print(f"mentions:  {', '.join(j['mentions'])}")
        if j.get("poll"):
            print("poll:      " + ", ".join(f"{o} ({n})" for o, n in j["poll"]))
        if j.get("reactions"):
            print("reactions: " + ", ".join(f"{e} {who}" for e, who in j["reactions"]))
        if j.get("ephemeral"):
            print(f"disappearing after {j['ephemeral'] // 86400} day(s)")
        if j.get("text"):
            print("\n" + j["text"])
        if j.get("transcript"):
            print("\ntranscript: " + j["transcript"])
        elif msg["kind"] in wa.SPEECH_KINDS:
            print(f"\nnot transcribed: wagrep transcribe {j['id']}")
        doc = renderer.doc(msg["id"])
        if doc and doc[2]:
            full = index.doc_text(doc[2])
            text = " ".join(full.split())
            print(f"\ndocument text ({len(full):,} chars): {text[:600]}"
                  + ("..." if len(text) > 600 else ""))
        if media and media["exists"]:
            print(f"\nopen: wagrep open {j['id']}")
        if len(msgs) > 1:
            print()
    return 0


def cmd_transcribe(args) -> int:
    if args.setup == "api":
        if not args.url:
            raise search.QueryError("--setup api needs --url (e.g. https://api.openai.com/v1)")
        transcribe.setup_api(args.url, args.key, args.model, args.language)
        note(f"transcription via {args.url} saved to {index.CONFIG}")
        return 0
    if args.setup == "local":
        transcribe.setup_local(index.DATA_DIR, args.model, args.language)
        note(f"local transcription ready; settings in {index.CONFIG}")
        return 0
    cfg = transcribe.load_config()
    for key in ("model", "language"):
        if getattr(args, key):
            cfg[key] = getattr(args, key)
    conn = open_index(args, sync=False)
    if not args.targets:
        return transcribe_status(conn, cfg)

    renderer = render.Renderer(conn, render.Style(sys.stdout.isatty()), full=True)
    jobs, ready = [], []
    for row in resolve_targets(conn, args.targets):
        msg = index.row_dict(row)
        if msg["kind"] not in wa.SPEECH_KINDS and not (
                msg.get("mime") or "").startswith(("audio/", "video/")):
            raise search.QueryError(f"#{msg['id']} is a {msg['kind']} message, "
                                    "not a voice note or video")
        path = _existing_path(row)
        if renderer.transcript(msg) and not args.force:
            ready.append(msg)
            continue
        jobs.append({"id": msg["id"], "path": path, "kind": msg["kind"],
                     "duration": msg.get("duration"), "msg": msg,
                     "prompt": transcribe.build_prompt(conn, msg, cfg)})
    for msg in ready:
        renderer.text([msg], [renderer.lines(msg)])
    if not jobs:
        return 0
    audio = sum(j["duration"] or 0 for j in jobs)
    note(f"transcribing {len(jobs)} voice note(s), {render.human_duration(audio)} "
         f"of audio, via {cfg.get('backend') or '?'}...")
    start, failed = time.time(), 0
    for job, result in transcribe.run(index.DATA_DIR, jobs, cfg):
        if isinstance(result, transcribe.TranscribeError):
            failed += 1
            log.warning("transcription of #%s failed: %s", job["id"], result)
            note(f"#{job['id']}: {result}")
            continue
        index.save_transcript(conn, job["id"], result, cfg["backend"], result["took"])
        log.info("transcribed #%s: %ss audio in %.1fs via %s (%s)", job["id"],
                 job["duration"], result["took"], cfg["backend"], result.get("model"))
        renderer.text([job["msg"]], [renderer.lines(job["msg"])])
    note(f"done in {time.time() - start:.1f}s"
         + (f"; {failed} failed (details above and in {LOG_FILE})" if failed else ""))
    return 2 if failed else 0


def transcribe_status(conn, cfg) -> int:
    n, audio = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(m.duration), 0) FROM tr.transcripts t "
        "JOIN msgs m ON m.id = t.id").fetchone()
    backend = cfg.get("backend")
    print(f"backend:     {backend or 'not configured (wagrep transcribe --setup api|local)'}")
    if backend == "api":
        print(f"url:         {cfg.get('url')}")
        print(f"key:         {'set' if cfg.get('key') else 'none'}")
    if backend == "local":
        ready = os.path.exists(transcribe.venv_python(index.DATA_DIR))
        print(f"virtualenv:  {'ready' if ready else 'missing, rerun --setup local'}")
    if backend:
        print(f"model:       {cfg.get('model')}   language: {cfg.get('language') or 'auto'}")
    print(f"transcripts: {n:,} ({render.human_duration(audio) or '0:00'} of audio) "
          f"in {index.TRANSCRIPTS}")
    print(f"config:      {index.CONFIG}")
    return 0


def cmd_stats(args) -> int:
    conn = open_index(args)
    q = build_query(conn, args)
    print(render.summary_text(search.summary(conn, q)))
    return 0


def cmd_index(args) -> int:
    for key in ("default", "archived"):
        value = getattr(args, key)
        if value:
            try:
                index.parse_span(value)
            except ValueError as exc:
                raise search.QueryError(f"--{key}: {exc}") from None
            index.save_config("scope", {key: value.strip().lower()})
    if args.rebuild:
        conn = index.rebuild()
    else:
        conn = index.connect()
        index.ensure_fresh(conn, force=True)
        index.read_documents(conn, None, progress=True)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size = os.path.getsize(index.INDEX)
    n, chats, first, last = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT chat_id), MIN(ts), MAX(ts) FROM msgs").fetchone()
    docs = dict(conn.execute("SELECT status, COUNT(*) FROM docs GROUP BY status"))
    synced = float(index.meta(conn, "synced_at", 0))
    full = float(index.meta(conn, "full_at", 0))
    fmt = "%Y-%m-%d %H:%M"
    print(f"index:     {index.INDEX} ({size / 1e6:.0f} MB, schema v{index.SCHEMA_VERSION})")
    print(f"messages:  {n:,} in {chats:,} chats, "
          f"{render.local(first):{fmt}} .. {render.local(last):{fmt}}")
    print(f"documents: {docs.get('ok', 0):,} read, {docs.get('error', 0)} unreadable, "
          f"{docs.get('missing', 0)} not downloaded"
          + ("" if shutil.which("pdftotext") else
             "  (install poppler for PDF text: brew install poppler)"))
    print(f"synced:    {render.local(synced):{fmt}:%S} (full check {render.local(full):{fmt}})")
    scope = index.load_config("scope")
    kept = conn.execute("SELECT COUNT(*) FROM chats WHERE since IS NULL").fetchone()[0]
    skipped = conn.execute(
        "SELECT COUNT(*) FROM chats WHERE since > 1e300").fetchone()[0]
    print(f"scope:     default {scope.get('default', 'all')}, archived "
          f"{scope.get('archived', scope.get('default', 'all'))}; {kept} chats in full, "
          f"{skipped} not indexed (wagrep chats --keep/--skip NAME)")
    print(f"config:    {index.CONFIG}")
    print(f"log:       {LOG_FILE}")
    return 0


def cmd_pick(args) -> int:
    if not shutil.which("fzf"):
        raise search.QueryError("pick needs fzf: brew install fzf")
    conn = open_index(args)
    q = build_query(conn, args)
    rows, _ = search.run(conn, q, 0)
    renderer = render.Renderer(conn, render.Style(False))
    lines = []
    for row in reversed(rows):
        msg = index.row_dict(row)
        tag = renderer.media_tag(msg)
        text = " ".join((msg.get("text") or "").split())
        body = (f"[{tag}] " if tag else "") + text
        lines.append(f"{msg['id']}\t{render.local(msg['ts']):%Y-%m-%d %H:%M}\t"
                     f"{renderer.chat_name(msg)}\t{msg['sender']}\t{body[:400]}")
    me = shlex.quote(LAUNCHER)
    proc = subprocess.run(
        ["fzf", "--delimiter=\t", "--with-nth=2..", "--no-sort",
         "--preview", f"WAGREP_NO_SYNC=1 {me} ctx {{1}} 6",
         "--preview-window=down,55%,wrap",
         "--bind", f"ctrl-o:execute-silent({me} open {{1}})",
         "--header", "enter: show in context | ctrl-o: open attachment | "
                     "'word: exact match"],
        input="\n".join(lines), capture_output=False, stdout=subprocess.PIPE,
        text=True)
    chosen = proc.stdout.strip()
    if chosen:
        args.id, args.n, args.before, args.after = int(chosen.split("\t")[0]), 15, None, None
        args.all = True
        args.no_sync = True
        return cmd_ctx(args)
    return 0


def cmd_sql(args) -> int:
    conn = wa.lock_read_only(open_index(args))
    try:
        cursor = conn.execute(args.query)
    except sqlite3.Error as exc:
        raise search.QueryError(f"sql: {exc}") from None
    budget = budget_of(args)
    out = ["\t".join(d[0] for d in cursor.description or ())]
    used = len(out[0])
    for n, row in enumerate(cursor):
        line = "\t".join("" if v is None else str(v).replace("\n", "\\n")
                         for v in row)
        used += len(line) + 1
        if budget and used > budget:
            note(f"output capped at {budget} chars after {n} rows; "
                 "add LIMIT/WHERE or pass --budget 0")
            break
        out.append(line)
    print("\n".join(out))
    return 0


GUIDE = """\
wagrep guide (for agents)
=========================
OUTPUT (text)
  -- 2026-11-05 Thu                 day separator; == NAME (kind) when one chat
  #381102 15:42 Ana: text           #ID is what ctx/show/open/path take
  *#381102 ...                      the hit, in --ctx / ctx output
  [CHAT] before the sender          when results span several chats
  [video 0:11 2.1M] [audio 0:42 81K] [document a.pdf 3p 1.2M] [poll: A 3, B 1]
  (transcript) text             what a transcribed voice note says
  "not downloaded" inside [...]     attachment not on this Mac
  > Name: quoted text               the message replied to
  (edited 15:49)                    edited (only edits from this account are known)
  👍2 ❤️ at the end                 reactions; names in: wagrep show ID
  in file: ...text...               the hit is inside the document's text
  Notes, truncation summaries and "no matches" go to stderr.

BUDGET
  Inside Claude Code (CLAUDECODE/AI_AGENT set) output is capped at 10000 chars:
  the newest matches that fit are shown and stderr summarizes the rest by
  chat/sender/type/day with the exact -u to page back. --budget N / 0 overrides.

SEARCH
  Terms: substring, ANDed, accent/case-insensitive ("marcio" = "Márcio").
  -o ORs them. Letter+digit codes ignore separators ("LA3021" = "LA-3021").
  Zero hits -> automatic typo-tolerant retry, announced on stderr (-z always,
  --exact never). Document text (PDF, Office, txt) is searched too.

JSON (--json, show --json): array, one object per line:
  id, when, chat, sender, from_me, type, text,
  media{filename,path,mime,size,duration,pages,exists},
  reply_to{kind,text,name}, mentions[], poll[[option,votes]],
  location[lat,lon], edited, reactions[[emoji,who]], starred

SQL (wagrep sql, read-only, on ~/.cache/wagrep/index.db)
  msgs(id, ts unix, chat_id, sender, from_me, kind, text, norm (folded), filename,
       media_path, mime, size, duration, edited, reply/reactions/extra JSON)
  chats(id, name, jid, kind)   docs(id=msg id, status, pages, text zlib)
  functions: doctext(docs.text) -> document text, fold(x) -> lowercase unaccented
  e.g. wagrep sql "SELECT kind, COUNT(*) FROM msgs GROUP BY 1"

SCOPE
  The user may index only part of the history (wagrep index shows it). When
  a search reaches outside it, stderr says so and gives the command, e.g.
  wagrep chats --keep NAME [--span 1y]. Run it when the user wants that
  chat's older messages; wagrep chats lists each chat's scope.

LIMITS
  Read-only snapshot of WhatsApp Desktop (macOS). Cannot send or modify.
  Deleted messages show as [deleted] (content is gone). Edits keep only the
  new text. View-once media and messages never synced to this Mac are absent.
  Voice notes are transcribed only on request.

TRANSCRIPTION (explicit; takes ~1/4 of the audio length and may cost money)
  wagrep transcribe ID...       transcribe these voice notes/videos; cached
  Transcripts then show inline as "(transcript) ..." and are searchable.
  Transcribe only the notes you need (find them with -T audio, -k, ctx).
  wagrep transcribe             shows whether a backend is configured.
"""


def cmd_guide(args) -> int:
    print(GUIDE, end="")
    return 0


# --- main ----------------------------------------------------------------

def subparsers(common, filters):
    def make(name, help_text, parents=(common,)):
        return argparse.ArgumentParser(prog=f"wagrep {name}", description=help_text,
                                       parents=list(parents))

    p = {}
    c = p["chats"] = make("chats", "find chats by name (typo-tolerant)")
    c.add_argument("names", nargs="*")
    c.add_argument("-n", "--limit", type=int, default=30, help="default 30, 0 = all")
    g = c.add_argument_group("how much of a chat to index (saved in the config)")
    g.add_argument("--keep", action="store_true",
                   help="index its history: all, or --span 1y")
    g.add_argument("--span", default="all", help="with --keep: all, 60d, 6m, 1y, ...")
    g.add_argument("--skip", action="store_true", help="never index it")
    g.add_argument("--reset", action="store_true", help="back to the default scope")
    for name, help_text in (("open", "open attachments with the default app"),
                            ("path", "print absolute paths of attachments"),
                            ("show", "every detail of messages")):
        s = p[name] = make(name, help_text)
        s.add_argument("targets", nargs="+", metavar="ID|PATH",
                       help="message id or any part of the attachment path")
    p["open"].add_argument("-R", "--reveal", action="store_true",
                           help="reveal in Finder instead")
    p["show"].add_argument("--json", action="store_true")
    x = p["ctx"] = make("ctx", "messages around one message, in its chat")
    x.add_argument("id", type=lambda v: int(v.lstrip("#")))
    x.add_argument("n", type=int, nargs="?", default=8,
                   help="messages before and after (default 8)")
    x.add_argument("-B", "--before", type=int)
    x.add_argument("-a", "--after", type=int)
    x.add_argument("-A", "--all", action="store_true", help="full message text")
    p["stats"] = make("stats", "counts by chat/sender/type/day", (filters, common))
    p["pick"] = make("pick", "interactive fuzzy finder over messages (fzf)",
                     (filters, common))
    s = p["sql"] = make("sql", "read-only SQL on the index (wagrep guide)")
    s.add_argument("query")
    t = p["transcribe"] = make(
        "transcribe", "transcribe voice notes by id; never runs on its own. "
        "With no ids: show the configuration.")
    t.add_argument("targets", nargs="*", metavar="ID|PATH")
    t.add_argument("--force", action="store_true", help="transcribe again")
    t.add_argument("--setup", choices=("api", "local"),
                   help="api: any OpenAI-compatible endpoint; local: faster-whisper here")
    t.add_argument("--url", help="API base url, e.g. https://api.openai.com/v1")
    t.add_argument("--key", help="API key")
    t.add_argument("--model", help="model name (api default whisper-1, local large-v3-turbo)")
    t.add_argument("--language", help="pt, en, ... or auto")
    i = p["index"] = make("index", "sync now, read all documents, print status")
    i.add_argument("--rebuild", action="store_true", help="drop and rebuild")
    i.add_argument("--default", metavar="SPAN",
                   help="history to index for chats without a rule: all, 60d, 6m, 1y")
    i.add_argument("--archived", metavar="SPAN",
                   help="same for archived/hidden chats: skip, all or a span")
    p["guide"] = make("guide", "reference for agents")
    return p


COMMANDS = {"chats": cmd_chats, "open": cmd_open, "path": cmd_path,
            "show": cmd_show, "ctx": cmd_ctx, "stats": cmd_stats,
            "pick": cmd_pick, "sql": cmd_sql, "index": cmd_index,
            "transcribe": cmd_transcribe,
            "guide": cmd_guide}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    common, filters = common_parser(), filter_parser()
    if argv and argv[0] in COMMANDS:
        name = argv[0]
        parser = subparsers(common, filters)[name]
        args = (parser.parse_intermixed_args(argv[1:]) if name in ("stats", "pick")
                else parser.parse_args(argv[1:]))
    else:
        name = "search"
        if argv and argv[0] == "search":
            argv = argv[1:]
        args = search_parser(common, filters).parse_intermixed_args(argv)
    setup_logging(args.verbose)
    start = time.time()
    try:
        code = (COMMANDS.get(name) or cmd_search)(args)
        log.info("%s %s -> exit %d in %.3fs", name, shlex.join(argv), code,
                 time.time() - start)
        return code
    except (wa.SourceError, search.QueryError, transcribe.TranscribeError) as exc:
        log.warning("%s %s -> %s", name, shlex.join(argv), exc)
        print(f"wagrep: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        # Output cut short by `head` and friends: not an error.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - last line of defence, logged in full
        log.exception("%s %s failed", name, shlex.join(argv))
        print(f"wagrep: unexpected error: {exc}\n  details: {LOG_FILE}",
              file=sys.stderr)
        return 2
