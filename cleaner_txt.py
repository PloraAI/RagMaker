#!/usr/bin/env python3
"""
txt_rag_cleaner.py
------------------
Cleans plain-text files (.txt, .log, ...) for AI / RAG pipelines by removing
noise and keeping the useful content:

  * ANSI colour codes, control characters, zero-width characters
  * decorative lines and symbols (=====, -----, *****, box drawing, emoji, ...)
  * log boilerplate: timestamps, thread ids, logger names, DEBUG/TRACE lines
  * progress bars (including carriage-return "overwrite" updates)
  * stack-trace frames (the exception message itself is kept)
  * page-number lines and repeated running headers / footers
  * repeated lines (exact and near-duplicates; repeated log messages are
    collapsed into one line with a "[xN]" counter)

Headings are detected (markdown '#', underlined titles, "=== Title ===" banners,
ALL-CAPS lines) and used to build heading-aware chunks.

Install:
    Nothing to install (standard library only).

Usage:
    python txt_rag_cleaner.py app.log
    python txt_rag_cleaner.py notes.txt report.txt -o output
    python txt_rag_cleaner.py ./logs --ext .log,.txt --report --unwrap

Outputs (written to the directory given with -o), per input file:
    <name>.clean.txt     -> cleaned text
    <name>.chunks.jsonl  -> one chunk per line: {id, heading_path, text, ...}
    <name>.removed.txt   -> every removed line with the reason (with --report)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter, deque
from pathlib import Path

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

NEAR_DUP_THRESHOLD = 0.85      # shingle Jaccard similarity threshold
MIN_DEDUP_CHARS = 20           # shorter plain-text lines are never deduplicated
NEAR_DUP_WINDOW = 2000         # compare against this many recent kept lines

# --- low-level cleanup -----------------------------------------------------
ANSI_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ZERO_WIDTH_RE = re.compile("[\u200b-\u200d\u2060\ufeff\u00ad\ufe0f]")

# --- decoration ------------------------------------------------------------
BOX_VERTICAL = "│┃║╎╏┆┇┊┋"
LEAD_BULLET_RE = re.compile(r"^\s*[•●○◦▪▫■□★☆►▶➤➔→✔✓✗✘‣⁃]\s+")
SYMBOL_RUN_RE = re.compile(r"(?:(?<=\s)|^)([^\w\s.])\1{2,}(?=\s|$)")   # ===  ---  ***  ###
DOT_RUN_RE = re.compile(r"\.{5,}|(?:\. ){4,}\.?|[·•]{3,}")             # dotted leaders
KEEP_SYMBOLS = set("©®™°℃℉№§¶")
BANNER_RE = re.compile(r"^\s*([^\w\s])\1{2,}\s*(.+?)\s*([^\w\s])\3{2,}\s*$")   # === Title ===
SETEXT_UNDERLINE_RE = re.compile(r"^\s*([=\-~])\1{2,}\s*$")
MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")

# --- log parsing -----------------------------------------------------------
_TS_STRONG = [
    r"\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s?(?:Z|[+-]\d{2}:?\d{2}|UTC|GMT))?",
    r"\d{2}[-/.]\d{2}[-/.]\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?",
    r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}",                      # syslog
    r"\d{1,2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}\s[+-]\d{4}",          # apache
]
_TS_BARE = r"\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
TS_STRONG_RE = re.compile(r"^\s*[\[(<]?\s*(?P<ts>" + "|".join(_TS_STRONG) + r")\s*[\])>]?[\s,:|\-]*")
TS_BARE_RE = re.compile(r"^\s*[\[(<]?\s*(?P<ts>" + _TS_BARE + r")\s*[\])>]?[\s,:|\-]*")

LEVELS = r"TRACE|DEBUG|VERBOSE|INFO|NOTICE|WARN(?:ING)?|ERROR|ERR|CRIT(?:ICAL)?|FATAL|SEVERE"
LEVEL_BRACKET_RE = re.compile(rf"^[\[(<]\s*(?P<lvl>{LEVELS})\s*[\])>]\s*[:\-|]?\s*", re.I)
LEVEL_BARE_RE = re.compile(rf"^(?P<lvl>{LEVELS})\b\s*[:\-|]?\s*")               # uppercase only
LEVEL_BARE_ANY_RE = re.compile(rf"^(?P<lvl>{LEVELS})\b\s*[:\-|]?\s*", re.I)    # when a timestamp precedes

LEVEL_MAP = {"TRACE": "TRACE", "DEBUG": "DEBUG", "VERBOSE": "VERBOSE", "INFO": "INFO",
             "NOTICE": "INFO", "WARN": "WARNING", "WARNING": "WARNING", "ERROR": "ERROR",
             "ERR": "ERROR", "CRIT": "CRITICAL", "CRITICAL": "CRITICAL",
             "FATAL": "CRITICAL", "SEVERE": "CRITICAL"}
SHOW_LEVEL = {"WARNING", "ERROR", "CRITICAL"}      # level tag kept in the output

THREAD_RE = re.compile(
    r"^(?:\[(?:main|[\w.\-]*(?:thread|pool|worker|exec|nio|task)[\w.\-]*|\d+|pid[: ]?\d+|[0-9a-f]{6,})\]\s*)+",
    re.I)
LOGGER_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\s*[:\-]\s+")   # com.foo.Bar -
PROC_RE = re.compile(r"^(?:\S+\s+)?[\w./-]+\[\d+\]:\s*")                         # host sshd[123]:

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
HEX_RE = re.compile(r"\b(?:0x[0-9a-f]+|[0-9a-f]{8,})\b", re.I)
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")

# --- other noise -----------------------------------------------------------
PROGRESS_PCT_RE = re.compile(r"\d{1,3}(?:\.\d+)?\s?%")
PROGRESS_BAR_RE = re.compile(
    r"[\[|][#=>\-.█▉▊▋▌▍▎▏░▒▓ ]{8,}[\]|]|\b(?:it/s|s/it|[kKMG]i?B/s|ETA|eta)\b"
    r"|\b(?:download|upload|progress|loading|extracting)\w*", re.I)
PAGE_NUMBER_RES = [
    re.compile(r"^\s*page\s+\d+(?:\s*(?:of|/)\s*\d+)?\s*$", re.I),
    re.compile(r"^\s*[-–—]\s*\d{1,4}\s*[-–—]\s*$"),
]
PY_FRAME_RE = re.compile(r'^\s+File ".*", line \d+(?:, in .*)?$')
TRACE_RES = [
    PY_FRAME_RE,
    re.compile(r"^\s+at\s+\S+.*\(.*\)\s*$"),                       # Java / JS frames
    re.compile(r"^\s+at\s+.*:\d+:\d+\)?\s*$"),
    re.compile(r"^\s*\.\.\.\s*\d+\s+(?:more|common frames omitted)\s*$"),
    re.compile(r"^\s*Traceback \(most recent call last\):\s*$"),
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "utf-8", "windows-1254", "iso-8859-9", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def has_alnum(text: str) -> bool:
    return any(c.isalnum() for c in text)


def has_digit(text: str) -> bool:
    return any(c.isdigit() for c in text)


def norm(text: str) -> str:
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def shingles(text: str, k: int = 3) -> set[str]:
    words = norm(text).split()
    if len(words) <= k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def template_key(msg: str) -> str:
    """Replace variable parts (ids, IPs, numbers) so similar log lines collapse."""
    t = msg.lower()
    t = UUID_RE.sub("<id>", t)
    t = IP_RE.sub("<ip>", t)
    t = HEX_RE.sub("<hex>", t)
    t = NUM_RE.sub("#", t)
    return re.sub(r"\s+", " ", t).strip()


def sanitize(line: str, stats: Counter) -> str:
    if "\x1b" in line:
        stats["ansi_codes_stripped"] += 1
        line = ANSI_RE.sub("", line)
    line = CTRL_RE.sub("", line)
    line = ZERO_WIDTH_RE.sub("", line)
    return line.expandtabs(4).rstrip()


def strip_decor(line: str, keep_emoji: bool) -> str:
    """Remove decorative symbols, bullets glyphs, box drawing, emoji and leader dots."""
    line = LEAD_BULLET_RE.sub("- ", line)
    out = []
    for ch in line:
        o = ord(ch)
        if ch in BOX_VERTICAL:
            out.append("|")
        elif 0x2500 <= o <= 0x259F:
            out.append(" ")
        elif not keep_emoji and ch not in KEEP_SYMBOLS and (
                unicodedata.category(ch) == "So" or 0x1F3FB <= o <= 0x1F3FF):
            out.append(" ")
        else:
            out.append(ch)
    line = "".join(out)
    line = SYMBOL_RUN_RE.sub(" ", line)
    line = DOT_RUN_RE.sub(" ", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line.strip(" |").strip()


def parse_log(line: str):
    """Return {ts, level, msg} if the line looks like a log line, else None."""
    rest = line
    ts = None
    ts_strong = False

    m = TS_STRONG_RE.match(rest)
    if m:
        ts, ts_strong, rest = m.group("ts"), True, rest[m.end():]
    else:
        m = TS_BARE_RE.match(rest)
        if m:
            ts, rest = m.group("ts"), rest[m.end():]

    if ts:                                   # "<ts> [main] DEBUG ..." -> thread tag comes first
        rest = THREAD_RE.sub("", rest)

    level = None
    for rx in ((LEVEL_BRACKET_RE, LEVEL_BARE_ANY_RE) if ts else (LEVEL_BRACKET_RE, LEVEL_BARE_RE)):
        lm = rx.match(rest)
        if lm:
            level = LEVEL_MAP[lm.group("lvl").upper()]
            rest = rest[lm.end():]
            break

    # A bare time alone ("12:30:00 Lunch") is not enough to call it a log line
    if not (ts_strong or level):
        return None

    rest = THREAD_RE.sub("", rest)
    rest = PROC_RE.sub("", rest, count=1) if (ts_strong and level is None) else rest
    rest = LOGGER_RE.sub("", rest)
    return {"ts": ts, "level": level, "msg": rest.strip()}


# --------------------------------------------------------------------------
# Core cleaning
# --------------------------------------------------------------------------

def clean_text(text: str, opts: argparse.Namespace):
    """Return (items, stats, removed). items: list of dicts with kind in
    {'blank', 'heading', 'line'}."""
    stats: Counter = Counter()
    removed: list[tuple[str, str]] = []
    drop_levels = {LEVEL_MAP.get(x.strip().upper(), x.strip().upper())
                   for x in opts.drop_levels.split(",") if x.strip() and x.strip().lower() != "none"}

    def drop(reason: str, line: str) -> None:
        stats[reason] += 1
        removed.append((reason, line))

    text = text.replace("\r\n", "\n")
    raw_lines: list[str] = []
    for ln in text.split("\n"):
        if "\r" in ln:                                    # progress-bar style overwrites
            segs = [s for s in ln.split("\r") if s.strip()]
            stats["carriage_return_overwrites"] += max(len(segs) - 1, 0)
            ln = segs[-1] if segs else ""
        raw_lines.append(ln)

    lines = [sanitize(ln, stats) for ln in raw_lines]
    stats["lines_in"] = len(lines)

    items: list[dict] = []
    log_seen: dict[tuple[str, str], dict] = {}
    exact_seen: set[str] = set()
    kept_shingles: deque[set[str]] = deque(maxlen=NEAR_DUP_WINDOW)
    in_py_frame = False

    def add_heading(level: int, title: str, caps: bool = False) -> None:
        title = strip_decor(title, opts.keep_emoji).lstrip("-• ").strip()
        if title and has_alnum(title):
            items.append({"kind": "heading", "level": level, "text": title, "caps": caps})

    i = 0
    while i < len(lines):
        s = lines[i]
        i += 1

        if not s.strip():
            items.append({"kind": "blank"})
            in_py_frame = False
            continue

        # ---- stack traces ------------------------------------------------
        if not opts.keep_traces:
            if any(rx.match(s) for rx in TRACE_RES):
                drop("stack_trace", s)
                in_py_frame = bool(PY_FRAME_RE.match(s))
                continue
            if in_py_frame and s[:1] in (" ", "\t"):       # source line under a Python frame
                drop("stack_trace", s)
                in_py_frame = False
                continue
        in_py_frame = False

        # ---- progress bars / page numbers --------------------------------
        if PROGRESS_PCT_RE.search(s) and PROGRESS_BAR_RE.search(s):
            drop("progress_bar", s)
            continue
        if any(rx.match(s) for rx in PAGE_NUMBER_RES):
            drop("page_number", s)
            continue

        # ---- headings (checked before decorations are stripped) ----------
        banner = BANNER_RE.match(s)
        if banner and has_alnum(banner.group(2)):
            add_heading(1, banner.group(2))
            continue
        md = MD_HEADING_RE.match(s)
        if md and has_alnum(md.group(2)):
            add_heading(len(md.group(1)), md.group(2))
            continue
        if (i < len(lines) and SETEXT_UNDERLINE_RE.match(lines[i])
                and has_alnum(s) and len(s.strip()) <= 100 and not parse_log(s)):
            add_heading(1 if lines[i].strip()[0] == "=" else 2, s.strip())
            i += 1                                          # consume the underline
            continue

        # ---- log lines ----------------------------------------------------
        log = parse_log(s)
        if log:
            if log["level"] in drop_levels:
                drop(f"log_level_{log['level'].lower()}", s)
                continue
            msg = strip_decor(log["msg"], opts.keep_emoji)
            if not msg or not has_alnum(msg):
                drop("empty_log_message", s)
                continue
            if not opts.keep_traces and any(rx.match(msg) for rx in TRACE_RES):
                drop("stack_trace", s)
                continue
            key_text = msg.lower() if opts.keep_log_variants else template_key(msg)
            key = (log["level"] or "", key_text)
            if key in log_seen:
                log_seen[key]["count"] += 1
                drop("duplicate_log_message", s)
                continue
            shown = f"[{log['level']}] {msg}" if log["level"] in SHOW_LEVEL else msg
            if opts.keep_timestamps and log["ts"]:
                shown = f"{log['ts']} {shown}"
            item = {"kind": "line", "text": shown, "is_log": True, "count": 1}
            log_seen[key] = item
            items.append(item)
            continue

        # ---- plain text ----------------------------------------------------
        t = strip_decor(s, opts.keep_emoji)
        if not t or not has_alnum(t):
            drop("decorative_line", s)
            continue

        letters = [c for c in t if c.isalpha()]
        if (t.isupper() and 3 <= len(t) <= 70 and len(t.split()) <= 8 and len(letters) >= 3
                and not t.endswith((".", ",", ";")) and sum(c.isdigit() for c in t) <= len(t) / 3):
            add_heading(2, t, caps=True)
            continue

        if len(t) >= MIN_DEDUP_CHARS:
            key = hashlib.md5(norm(t).encode()).hexdigest()
            if key in exact_seen:
                drop("duplicate_line", s)
                continue
            exact_seen.add(key)
            sh = shingles(t)
            if (len(sh) >= 6 and not has_digit(t)
                    and any(jaccard(sh, k) >= NEAR_DUP_THRESHOLD for k in kept_shingles)):
                drop("near_duplicate_line", s)
                continue
            kept_shingles.append(sh)
        items.append({"kind": "line", "text": t, "is_log": False, "count": 1})

    # ---- repeated running headers (ALL-CAPS titles appearing 3+ times) -------
    caps_count = Counter(norm(it["text"]) for it in items if it.get("caps"))
    seen_caps: set[str] = set()
    kept: list[dict] = []
    for it in items:
        if it.get("caps") and caps_count[norm(it["text"])] >= 3:
            k = norm(it["text"])
            if k in seen_caps:
                drop("repeated_header", it["text"])
                continue
            seen_caps.add(k)
        kept.append(it)
    items = kept

    # ---- repeat counters on collapsed log lines ----------------------------
    if not opts.no_counts:
        for it in items:
            if it.get("count", 1) > 1:
                it["text"] += f"  [x{it['count']}]"

    if opts.unwrap:
        items = unwrap_paragraphs(items)

    # ---- drop headings with no content below them --------------------------
    cleaned: list[dict] = []
    for idx, it in enumerate(items):
        if it["kind"] == "heading":
            nxt = next((x for x in items[idx + 1:] if x["kind"] != "blank"), None)
            if nxt is None or (nxt["kind"] == "heading" and nxt["level"] <= it["level"]):
                stats["empty_headings"] += 1
                continue
        cleaned.append(it)
    items = cleaned
    stats["lines_out"] = sum(1 for it in items if it["kind"] != "blank")
    return items, stats, removed


BULLET_START_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def unwrap_paragraphs(items: list[dict]) -> list[dict]:
    """Join hard-wrapped prose lines (e.g. fixed 80-column text) into paragraphs."""
    out: list[dict] = []
    for it in items:
        prev = out[-1] if out else None
        if (prev and prev["kind"] == "line" and it["kind"] == "line"
                and not prev.get("is_log") and not it.get("is_log")
                and len(prev["text"]) >= 40
                and not prev["text"].rstrip().endswith((".", "!", "?", ":", ";", '"', "”", ")"))
                and not BULLET_START_RE.match(it["text"])
                and not prev["text"].isupper()):
            if re.search(r"[A-Za-z]-$", prev["text"]) and it["text"][:1].islower():
                prev["text"] = prev["text"][:-1] + it["text"]       # de-hyphenate
            else:
                prev["text"] += " " + it["text"]
            continue
        out.append(dict(it))
    return out


# --------------------------------------------------------------------------
# Output: text + chunks
# --------------------------------------------------------------------------

def to_text(items: list[dict]) -> str:
    lines: list[str] = []
    for it in items:
        if it["kind"] == "blank":
            lines.append("")
        elif it["kind"] == "heading":
            lines += ["", f"{'#' * it['level']} {it['text']}", ""]
        else:
            lines.append(it["text"])
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    parts, cur = [], ""
    for s in sentences:
        while len(s) > max_chars:
            parts.append((cur + " " + s[:max_chars]).strip() if cur else s[:max_chars])
            cur, s = "", s[max_chars - overlap:]
        if len(cur) + len(s) + 1 > max_chars and cur:
            parts.append(cur.strip())
            cur = cur[-overlap:] + " " + s if overlap else s
        else:
            cur = f"{cur} {s}".strip()
    if cur.strip():
        parts.append(cur.strip())
    return parts


def build_chunks(items: list[dict], max_chars: int, overlap: int, source: str) -> list[dict]:
    chunks: list[dict] = []
    path: list[tuple[int, str]] = []
    buf: list[str] = []
    size = 0
    gap = False

    def flush():
        nonlocal size
        if not buf:
            return
        body = "\n".join(buf).strip()
        heading_path = " > ".join(t for _, t in path)
        for part in split_long(body, max_chars, overlap):
            text = f"{heading_path}\n\n{part}" if heading_path else part
            chunks.append({
                "id": f"{Path(source).name}-{len(chunks):04d}",
                "source": source,
                "heading_path": heading_path,
                "text": text,
                "char_count": len(text),
            })
        buf.clear()
        size = 0

    for it in items:
        if it["kind"] == "blank":
            gap = True
        elif it["kind"] == "heading":
            flush()
            while path and path[-1][0] >= it["level"]:
                path.pop()
            path.append((it["level"], it["text"]))
            gap = False
        else:
            if size + len(it["text"]) > max_chars and buf:
                flush()
            if gap and buf:
                buf.append("")
            buf.append(it["text"])
            size += len(it["text"]) + 1
            gap = False
    flush()
    return chunks


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def collect_files(inputs: list[Path], exts: set[str]) -> list[Path]:
    files: list[Path] = []
    for p in inputs:
        if p.is_dir():
            files += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in exts)
        elif p.is_file():
            files.append(p)
        else:
            print(f"Not found: {p}", file=sys.stderr)
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="Clean plain-text / log files for RAG")
    ap.add_argument("inputs", nargs="+", type=Path, help="files or directories")
    ap.add_argument("-o", "--out-dir", type=Path, default=Path("txt_output"))
    ap.add_argument("--ext", default=".txt,.log,.text",
                    help="extensions to pick up when an input is a directory")
    ap.add_argument("--max-chars", type=int, default=1200, help="max characters per chunk")
    ap.add_argument("--overlap", type=int, default=150, help="chunk overlap for very long lines")
    ap.add_argument("--no-chunks", action="store_true", help="do not write the chunks.jsonl file")
    ap.add_argument("--drop-levels", default="DEBUG,TRACE,VERBOSE",
                    help="comma-separated log levels to delete entirely, or 'none'")
    ap.add_argument("--keep-timestamps", action="store_true", help="keep the timestamp of log lines")
    ap.add_argument("--keep-traces", action="store_true", help="keep stack-trace frames")
    ap.add_argument("--keep-emoji", action="store_true", help="keep emoji / symbol characters")
    ap.add_argument("--keep-log-variants", action="store_true",
                    help="only merge log lines that are identical (do not ignore numbers / ids)")
    ap.add_argument("--no-counts", action="store_true", help="do not add [xN] to collapsed log lines")
    ap.add_argument("--unwrap", action="store_true", help="join hard-wrapped lines into paragraphs")
    ap.add_argument("--report", action="store_true", help="write removed lines with reasons")
    args = ap.parse_args()

    exts = {e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
            for e in args.ext.split(",") if e.strip()}
    files = collect_files(args.inputs, exts)
    if not files:
        print("No input files found.", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    total_in = total_out = 0

    for path in files:
        text = read_text(path)
        items, stats, removed = clean_text(text, args)
        clean = to_text(items)

        (args.out_dir / f"clean.txt").write_text(clean, encoding="utf-8")
        n_chunks = 0
        if not args.no_chunks:
            chunks = build_chunks(items, args.max_chars, args.overlap, path.name)
            n_chunks = len(chunks)
            with (args.out_dir / f"chunks.jsonl").open("w", encoding="utf-8") as f:
                for c in chunks:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")
        #if args.report:
        #    with (args.out_dir / f"{path.name}.removed.txt").open("w", encoding="utf-8") as f:
        #        for reason, line in removed:
        #            f.write(f"{reason}\t{line}\n")

        size_in, size_out = len(text), len(clean)
        total_in += size_in
        total_out += size_out
        pct = 100 * (1 - size_out / size_in) if size_in else 0
        print(f"\n{path}")
        print(f"  lines: {stats['lines_in']} -> {stats['lines_out']}   "
              f"chars: {size_in} -> {size_out} (-{pct:.0f}%)   chunks: {n_chunks}")
        noise = {k: v for k, v in stats.items() if k not in ("lines_in", "lines_out")}
        for reason, n in sorted(noise.items(), key=lambda kv: -kv[1]):
            print(f"    {reason:<28} {n}")

    if len(files) > 1 and total_in:
        print(f"\nTotal: {total_in} -> {total_out} chars (-{100 * (1 - total_out / total_in):.0f}%)")
    print(f"Output directory: {args.out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())