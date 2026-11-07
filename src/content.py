"""Text inside attachments, so documents are searchable like messages.

PDFs go through `pdftotext` (poppler) when installed, Word/RTF through macOS
`textutil`, Office Open XML and OpenDocument through their zipped XML, and
plain-text formats are read directly. Files are only read, never modified.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import zipfile

MAX_CHARS = 300_000
PLAIN = {".txt", ".csv", ".tsv", ".md", ".json", ".xml", ".html", ".htm",
         ".log", ".sql", ".ipynb", ".yaml", ".yml", ".ini", ".conf", ".kml", ".gpx",
         ".py", ".js", ".ts", ".sh", ".c", ".h", ".java", ".go", ".rs"}
TEXTUTIL = {".doc", ".rtf", ".rtfd", ".webarchive", ".wordml"}
ZIPPED = {
    ".docx": (r"word/(document|header\d*|footer\d*)\.xml",),
    ".xlsx": (r"xl/sharedStrings\.xml", r"xl/worksheets/sheet\d+\.xml"),
    ".pptx": (r"ppt/slides/slide\d+\.xml",),
    ".odt": (r"content\.xml",), ".ods": (r"content\.xml",),
    ".odp": (r"content\.xml",),
}
_TAG = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"</(w:p|a:p|text:p|row|si)>|<w:br/>|<w:tab/>")


def readable(ext: str) -> bool:
    return ext in PLAIN or ext in TEXTUTIL or ext in ZIPPED or (
        ext == ".pdf" and shutil.which("pdftotext") is not None)


def extract(path: str) -> tuple[str, int | None]:
    """(text, pages) of one file. Raises on unreadable files."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        out = subprocess.run(
            ["pdftotext", "-q", "-enc", "UTF-8", path, "-"],
            capture_output=True, timeout=60, check=True).stdout
        text = out.decode("utf-8", "replace")
        return text[:MAX_CHARS], text.count("\f") or None
    if ext in TEXTUTIL:
        out = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", path],
            capture_output=True, timeout=60, check=True).stdout
        return out.decode("utf-8", "replace")[:MAX_CHARS], None
    if ext in ZIPPED:
        return _zipped(path, ZIPPED[ext], ext)
    with open(path, "rb") as fh:
        raw = fh.read(MAX_CHARS * 2)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text[:MAX_CHARS], None


def _zipped(path, patterns, ext):
    parts, pages = [], 0
    with zipfile.ZipFile(path) as zf:
        names = sorted(n for n in zf.namelist()
                       if any(re.fullmatch(p, n) for p in patterns))
        for name in names:
            xml = zf.read(name).decode("utf-8", "replace")
            parts.append(html.unescape(_TAG.sub(" ", _BREAK.sub("\n", xml))))
            pages += ext == ".pptx"
            if sum(map(len, parts)) > MAX_CHARS:
                break
    text = re.sub(r"[ \t]+", " ", "\n".join(parts))
    return text[:MAX_CHARS], pages or None
