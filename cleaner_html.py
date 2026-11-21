#!/usr/bin/env python3
"""
html_rag_cleaner.py
-------------------
Cleans an HTML file by removing repeated / unnecessary parts (menus, footers,
ads, cookie banners, script/style, duplicated paragraphs, etc.) and produces
text chunks that preserve the heading hierarchy, ready for AI / RAG pipelines.

Install:
    pip install beautifulsoup4 lxml

Usage:
    python html_rag_cleaner.py page.html
    python html_rag_cleaner.py page.html -o output --max-chars 1200 --overlap 150

Outputs (written to the directory given with -o):
    clean.md      -> cleaned, markdown-like plain text
    chunks.jsonl  -> one chunk per line: {id, heading_path, text, ...}
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

# Tags that are removed entirely
REMOVE_TAGS = [
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "object", "embed", "button", "input", "select", "textarea",
    "nav", "footer", "aside", "dialog", "audio", "video", "picture",
]

# Patterns that mark an element as "boilerplate" when found in class / id
BOILERPLATE_RE = re.compile(
    r"(^|[-_\s])(nav|navbar|menu|sidebar|footer|header|breadcrumb|cookie|consent|"
    r"gdpr|banner|advert|ads?|promo|popup|modal|newsletter|subscribe|share|social|"
    r"related|recommend|comment|pagination|toolbar|skip|sponsor|caption|photo-credit|image-credit|img-credit|photo-source|image-source)([-_\s]|$)",
    re.I,
)

# Content blocks other than headings
BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre",
              "blockquote", "td", "th", "dd", "dt", "figcaption"]

HEADING_LEVEL = {f"h{i}": i for i in range(1, 7)}

MIN_BLOCK_CHARS = 20          # non-heading blocks shorter than this are dropped
NEAR_DUP_THRESHOLD = 0.85     # shingle Jaccard similarity threshold


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def read_html(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "windows-1254", "iso-8859-9", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def norm(text: str) -> str:
    """Normalize for comparison (lowercase, single spaces, no punctuation)."""
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def clean_ws(text: str) -> str:
    text = text.replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


def shingles(text: str, k: int = 3) -> set[str]:
    words = norm(text).split()
    if len(words) <= k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_hidden(tag: Tag) -> bool:
    if tag.attrs is None:
        return False
    if tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
        return True
    style = (tag.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def looks_like_boilerplate(tag: Tag) -> bool:
    if tag.attrs is None:
        return False
    attrs = " ".join(
        [" ".join(tag.get("class", [])), tag.get("id", ""), tag.get("role", "")]
    )
    # <body>/<html>/<main>/<article> are never treated as boilerplate
    if tag.name in ("body", "html", "main", "article"):
        return False
    if tag.get("role") in ("navigation", "banner", "contentinfo", "complementary"):
        return True
    return bool(BOILERPLATE_RE.search(attrs))


# --------------------------------------------------------------------------
# 1) DOM cleanup
# --------------------------------------------------------------------------

def strip_dom(soup: BeautifulSoup, aggressive: bool = True,
              keep_hidden: bool = False) -> None:
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()

    # For sites that keep values in <input value="..."> (rate boxes, etc.), turn the value into text
    for inp in soup.find_all("input"):
        v = (inp.get("value") or "").strip()
        if v and len(v) < 100 and (inp.get("type") or "text").lower() in ("text", "number", "tel", "search"):
            inp.replace_with(NavigableString(f" {v} "))

    for t in soup(REMOVE_TAGS):
        t.decompose()

    # Image-only <figure> blocks: drop them together with captions / photo credits.
    # Figures holding a table, code or text are kept (their figcaption is often the title).
    for fig in soup.find_all("figure"):
        if fig.find(["table", "pre", "blockquote", "ul", "ol", "p"]) is None:
            fig.decompose()

    # <form> wrappers (ASP.NET etc.) carry the content: don't delete, just unwrap
    for f in soup.find_all("form"):
        f.unwrap()

    if not aggressive:
        return

    total = len(soup.get_text(" ", strip=True)) or 1
    to_remove = []
    for t in soup.find_all(True):
        if is_hidden(t):
            if not keep_hidden:
                to_remove.append(t)
        elif looks_like_boilerplate(t):
            # Keep wrappers that hold a large share of the page
            if len(t.get_text(" ", strip=True)) / total > 0.3:
                continue
            to_remove.append(t)
    for t in to_remove:
        if t.parent is not None:      # skip if an ancestor was already removed
            t.decompose()


def find_main(soup: BeautifulSoup) -> Tag:
    """<main>/<article> or the block with the highest text density. If the chosen
    root holds less than half of the body text, <body> is used to avoid data loss."""
    body = soup.body or soup
    body_len = len(body.get_text(" ", strip=True)) or 1

    best = None
    for sel in ("main", "article", "[role=main]"):
        found = soup.select_one(sel)
        if found and len(found.get_text(strip=True)) > 200:
            best = found
            break

    if best is None:
        best_score = 0
        for tag in soup.find_all(["div", "section", "td"]):
            text_len = len(tag.get_text(" ", strip=True))
            link_len = sum(len(a.get_text(" ", strip=True)) for a in tag.find_all("a"))
            score = text_len - link_len
            if score > best_score:
                best, best_score = tag, score

    if best is None or len(best.get_text(" ", strip=True)) < 0.5 * body_len:
        return body
    return best


# --------------------------------------------------------------------------
# 2) Block extraction + deduplication
# --------------------------------------------------------------------------

STRUCT = list(HEADING_LEVEL) + ["table", "p", "ul", "ol", "li", "pre",
                                 "blockquote", "dl", "dd", "dt", "tr"]


def has_digit(text: str) -> bool:
    return any(c.isdigit() for c in text)


def cell_text(tag: Tag) -> str:
    return clean_ws(tag.get_text(" ", strip=True))


def link_density(tag: Tag) -> float:
    total = len(tag.get_text(" ", strip=True)) or 1
    links = sum(len(a.get_text(" ", strip=True)) for a in tag.find_all("a"))
    return links / total


def row_text(tag: Tag) -> str:
    """Turn short elements whose text lives entirely in child tags into an
    'a | b | c' row (e.g. <div><span>USD</span><span>32.15</span></div>).
    Elements with plain text of their own (paragraphs) return normal text."""
    full = cell_text(tag)
    has_loose = any(isinstance(c, NavigableString) and not isinstance(c, Comment)
                    and len(clean_ws(str(c))) > 3 for c in tag.children)
    if has_loose or len(full) > 200:
        return full
    parts = [cell_text(c) for c in tag.children if isinstance(c, Tag)]
    parts = [p for p in parts if p]
    return " | ".join(parts) if len(parts) >= 2 else full


def is_data_table(table: Tag) -> bool:
    """A table with no headings/paragraphs/lists/nested tables inside is a data
    table; otherwise it is used for page layout and its content is walked normally."""
    return table.find(list(HEADING_LEVEL) + ["table", "p", "ul", "ol", "pre", "blockquote"]) is None


def table_blocks(table: Tag) -> list[dict]:
    """Make each row a single block; if there is a header row, format each row
    as 'Header: value' so every row is meaningful on its own (important for RAG)."""
    parsed = []
    for tr in table.find_all("tr"):
        cells = [cell_text(c) for c in tr.find_all(["th", "td"])]
        if any(cells):
            parsed.append((tr, cells))
    if not parsed:
        return []

    header = None
    first_tr, first_cells = parsed[0]
    if first_tr.find_parent("thead") or (first_tr.find("th") and not first_tr.find("td")):
        header, parsed = first_cells, parsed[1:]

    out: list[dict] = []
    cap = table.find("caption")
    if cap and cell_text(cap):
        out.append({"type": "text", "level": 0, "tag": "p", "text": cell_text(cap)})
    for _, cells in parsed:
        if header and len(header) == len(cells):
            parts = [f"{h}: {v}" if h and v else v for h, v in zip(header, cells)]
        else:
            parts = cells
        text = " | ".join(p for p in parts if p)
        if text:
            out.append({"type": "text", "level": 0, "tag": "tr", "text": text})
    return out


def _add_text(tag: Tag, out: list[dict]) -> None:
    text = row_text(tag)
    if not text:
        return
    digit = has_digit(text)
    if len(text) < MIN_BLOCK_CHARS and not digit:
        return                                   # short blocks containing numbers (rates) are kept
    if link_density(tag) > 0.6 and not digit:
        return                                   # piles of links, like menus
    kind = "li" if tag.name == "li" else ("tr" if " | " in text else "p")
    out.append({"type": "text", "level": 0, "tag": kind, "text": text})


def walk(node: Tag, out: list[dict]) -> None:
    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            t = clean_ws(str(ch))
            if len(t) >= MIN_BLOCK_CHARS or (len(t) >= 2 and has_digit(t)):
                out.append({"type": "text", "level": 0, "tag": "p", "text": t})
            continue
        if not isinstance(ch, Tag):
            continue
        n = ch.name
        if n in HEADING_LEVEL:
            t = cell_text(ch)
            if t:
                out.append({"type": "heading", "level": HEADING_LEVEL[n], "tag": n, "text": t})
        elif n == "pre":
            t = ch.get_text("\n").strip("\n")
            if t:
                out.append({"type": "code", "level": 0, "tag": "pre", "text": t})
        elif n == "table":
            if is_data_table(ch):
                out.extend(table_blocks(ch))
            else:
                walk(ch, out)
        elif n in ("br", "hr", "img"):
            continue
        elif n in ("p", "li", "dd", "dt", "blockquote", "figcaption"):
            if ch.find(STRUCT):
                walk(ch, out)
            else:
                _add_text(ch, out)
        else:                                      # div, span, section, tr, td, ul ...
            if not cell_text(ch):
                continue
            if ch.find(STRUCT) is None and len(cell_text(ch)) <= 400:
                _add_text(ch, out)
            else:
                walk(ch, out)


def extract_blocks(root: Tag) -> list[dict]:
    """Return (type, level, tag, text) blocks in document order."""
    blocks: list[dict] = []
    walk(root, blocks)
    return blocks


def extract_text_lines(root: Tag) -> list[dict]:
    """Fallback mode: for pages without block tags (div/span heavy), take all
    text nodes in order and treat anything inside h1-h6 as a heading."""
    blocks: list[dict] = []
    last_heading = None
    for s in root.find_all(string=True):
        text = clean_ws(str(s))
        if not text:
            continue
        h = s.find_parent(list(HEADING_LEVEL))
        if h is not None:
            if h is last_heading and blocks and blocks[-1]["type"] == "heading":
                blocks[-1]["text"] += " " + text
            else:
                blocks.append({"type": "heading", "level": HEADING_LEVEL[h.name],
                               "tag": h.name, "text": text})
            last_heading = h
            continue
        last_heading = None
        if len(text) >= MIN_BLOCK_CHARS:
            blocks.append({"type": "text", "level": 0, "tag": "p", "text": text})
    return blocks


def deduplicate(blocks: list[dict]) -> list[dict]:
    """Remove exact and near-duplicate blocks. Headings are kept."""
    seen_exact: set[str] = set()
    kept_shingles: list[set[str]] = []
    out: list[dict] = []

    for b in blocks:
        if b["type"] == "heading":
            out.append(b)
            continue

        key = hashlib.md5(norm(b["text"]).encode()).hexdigest()
        if key in seen_exact:
            continue
        seen_exact.add(key)

        sh = shingles(b["text"])
        # Skip the near-duplicate check for table rows and blocks with numbers
        if (b["tag"] != "tr" and not has_digit(b["text"]) and len(sh) >= 4
                and any(jaccard(sh, k) >= NEAR_DUP_THRESHOLD for k in kept_shingles)):
            continue
        kept_shingles.append(sh)
        out.append(b)

    # Drop headings that have no content under them
    cleaned: list[dict] = []
    for i, b in enumerate(out):
        if b["type"] == "heading":
            nxt = next((x for x in out[i + 1:]), None)
            if nxt is None or (nxt["type"] == "heading" and nxt["level"] <= b["level"]):
                continue
        cleaned.append(b)
    return cleaned


# --------------------------------------------------------------------------
# 3) Markdown + chunk generation
# --------------------------------------------------------------------------

def to_markdown(blocks: list[dict]) -> str:
    lines: list[str] = []
    for b in blocks:
        if b["type"] == "heading":
            lines.append(f"\n{'#' * b['level']} {b['text']}\n")
        elif b["type"] == "code":
            lines.append(f"```\n{b['text']}\n```")
        elif b["tag"] in ("li", "tr"):
            lines.append(f"- {b['text']}")
        else:
            lines.append(b["text"] + "\n")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    parts, cur = [], ""
    for s in sentences:
        while len(s) > max_chars:                      # very long sentence
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


def build_chunks(blocks: list[dict], max_chars: int, overlap: int,
                 source: str) -> list[dict]:
    chunks: list[dict] = []
    path: list[tuple[int, str]] = []       # (level, title)
    buf: list[str] = []

    def flush():
        if not buf:
            return
        body = "\n".join(buf).strip()
        heading_path = " > ".join(t for _, t in path)
        for part in split_long(body, max_chars, overlap):
            # Prepend the heading path to the text: improves embedding quality
            text = f"{heading_path}\n\n{part}" if heading_path else part
            chunks.append({
                "id": f"{Path(source).stem}-{len(chunks):04d}",
                "source": source,
                "heading_path": heading_path,
                "text": text,
                "char_count": len(text),
            })
        buf.clear()

    for b in blocks:
        if b["type"] == "heading":
            flush()
            while path and path[-1][0] >= b["level"]:
                path.pop()
            path.append((b["level"], b["text"]))
        else:
            line = f"- {b['text']}" if b["tag"] in ("li", "tr") else b["text"]
            if sum(len(x) for x in buf) + len(line) > max_chars and buf:
                flush()
            buf.append(line)
    flush()
    return chunks


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------

def parse(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def process(html: str, source: str, max_chars: int, overlap: int,
            debug: bool = False, keep_hidden: bool = False):
    def log(msg):
        if debug:
            print(f"[debug] {msg}", file=sys.stderr)

    title = ""
    blocks: list[dict] = []
    mode = ""

    # Try in order: aggressive -> soft -> soft + plain-text fallback
    attempts = [("aggressive", True, False), ("soft", False, False),
                ("text-fallback", False, True)]
    for mode, aggressive, text_fallback in attempts:
        soup = parse(html)
        if not title and soup.title:
            title = clean_ws(soup.title.get_text())
        log(f"[{mode}] raw text length: {len(soup.get_text(' ', strip=True))}")
        strip_dom(soup, aggressive, keep_hidden)
        root = find_main(soup)
        log(f"[{mode}] main root: <{root.name}> text: {len(root.get_text(' ', strip=True))}")
        blocks = extract_text_lines(root) if text_fallback else extract_blocks(root)
        if not blocks and root is not soup.body and soup.body is not None:
            root = soup.body
            blocks = extract_text_lines(root) if text_fallback else extract_blocks(root)
        log(f"[{mode}] block count: {len(blocks)}")
        if len(blocks) >= 3:
            break

    before = len(blocks)
    blocks = deduplicate(blocks)
    after = len(blocks)

    if not blocks:
        print("WARNING: No text could be extracted. The page may be loading its "
              "content with JavaScript (SPA). Instead of 'Save page as', try HTML "
              "copied via DevTools > Elements > 'Copy outerHTML'.",
              file=sys.stderr)

    # If there is no h1, add the page title as the first heading
    if title and blocks and not any(b["type"] == "heading" and b["level"] == 1 for b in blocks):
        blocks.insert(0, {"type": "heading", "level": 1, "tag": "h1", "text": title})

    markdown = to_markdown(blocks)
    chunks = build_chunks(blocks, max_chars, overlap, source)
    stats = {"mode": mode, "blocks_before": before, "blocks_after": after,
             "removed_duplicates": before - after, "chunks": len(chunks)}
    return markdown, chunks, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Clean HTML and chunk it for RAG")
    ap.add_argument("html_file", type=Path)
    ap.add_argument("-o", "--out-dir", type=Path, default=Path("rag_output"))
    ap.add_argument("--max-chars", type=int, default=1200, help="max characters per chunk")
    ap.add_argument("--overlap", type=int, default=150, help="chunk overlap for long paragraphs")
    ap.add_argument("--keep-hidden", action="store_true",
                    help="do not remove hidden (display:none / aria-hidden) elements; "
                         "useful for tabbed pages")
    ap.add_argument("--debug", action="store_true", help="print step-by-step diagnostics")
    args = ap.parse_args()

    if not args.html_file.exists():
        print(f"File not found: {args.html_file}", file=sys.stderr)
        return 1

    html = read_html(args.html_file)
    markdown, chunks, stats = process(html, args.html_file.name,
                                      args.max_chars, args.overlap, args.debug, args.keep_hidden)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "clean.md").write_text(markdown, encoding="utf-8")
    with (args.out_dir / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print(f"Mode used          : {stats['mode']}")
    print(f"Blocks (before/after): {stats['blocks_before']} -> {stats['blocks_after']}")
    print(f"Duplicates removed : {stats['removed_duplicates']}")
    print(f"Chunks produced    : {stats['chunks']}")
    print(f"Output directory   : {args.out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())