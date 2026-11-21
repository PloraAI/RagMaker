# HtmlToRAG

**An application for converting HTML and TXT files to RAG-compatible format.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)

Developed and published by **Plora AI**.

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
  - [cleaner_html.py](#cleaner_htmlpy)
  - [cleaner_txt.py](#cleaner_txtpy)
  - [tokenhelper.py](#tokenhelperpy)
- [Output Format](#output-format)
- [How It Works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Overview

Raw HTML pages and text/log files are full of content that hurts Retrieval-Augmented
Generation (RAG): navigation menus, cookie banners, scripts, photo captions, decorative
separators, timestamps, debug logs, progress bars and endlessly repeated lines.
Feeding them straight into an embedding model wastes tokens and pollutes retrieval results.

HtmlToRAG removes that noise, keeps the meaningful content (including tables), preserves
the heading hierarchy and splits everything into clean, heading-aware chunks that are ready
to be embedded and stored in a vector database.

## Features

**HTML cleaning (`cleaner_html.py`)**
- Removes `script`, `style`, `nav`, `footer`, `aside`, hidden elements, cookie banners, ads and similar boilerplate
- Removes image-only `<figure>` blocks together with their captions and photo credits
- Detects the main content area (`<main>`, `<article>` or the densest block)
- Reads tables row by row and merges headers into each row (`Currency: USD | Buy: 32.15 | Sell: 32.40`)
- Removes exact and near-duplicate blocks
- Automatic fallback modes when a page is hard to parse (form-wrapped pages, `div`-only layouts)

**TXT / log cleaning (`cleaner_txt.py`)**
- Strips ANSI colour codes, control characters, zero-width characters, emoji and decorative lines (`=====`, `-----`, box drawing)
- Removes log boilerplate: timestamps, thread ids, logger names, `DEBUG` / `TRACE` lines
- Collapses repeated log messages into a single line with a counter (`[x37]`)
- Removes progress bars, stack-trace frames, page numbers and repeated running headers
- Optionally joins hard-wrapped lines into paragraphs
- Can write a report of every removed line together with the reason

**Token counting (`tokenhelper.py`)**
- Counts tokens with [tiktoken](https://github.com/openai/tiktoken) so you can size chunks and estimate embedding cost

**Common**
- Heading-aware chunking (the heading path is prepended to every chunk)
- Output as clean text/markdown and as JSON Lines (`.jsonl`)

## Project Structure

```
HtmlToRAG/
├── cleaner_html.py      # HTML  -> clean markdown + RAG chunks
├── cleaner_txt.py       # TXT / LOG -> clean text + RAG chunks
├── tokenhelper.py       # Token counting utility
├── requirements.txt     # Python dependencies
├── LICENSE              # MIT License
└── README.md
```

## Requirements

- Python **3.9** or newer
- Python packages:

| Package          | Used by                         | Purpose                                  |
| ---------------- | ------------------------------- | ---------------------------------------- |
| `beautifulsoup4` | `cleaner_html.py`               | HTML parsing and DOM cleanup             |
| `lxml`           | `cleaner_html.py`               | Fast and forgiving HTML parser           |
| `tiktoken`       | `tokenhelper.py`                | Token counting                           |

> `cleaner_txt.py` uses only the Python standard library.

## Installation

**1. Clone the repository**

```bash
git clone https://github.com/PloraAI/RagMaker
cd RagMaker
```

**2. (Recommended) Create a virtual environment**

```bash
python -m venv .venv

# Linux / macOS
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

**3. Install the dependencies**

```bash
pip install -r requirements.txt
```

Or install the packages manually:

```bash
pip install beautifulsoup4 lxml tiktoken
```

`requirements.txt`:

```text
beautifulsoup4>=4.12
lxml>=5.0
tiktoken>=0.7
```

> **Note:** `tiktoken` downloads its encoding files the first time it is used, so the first
> run of `tokenhelper.py` needs an internet connection.

## Usage

### cleaner_html.py

```bash
python cleaner_html.py page.html
python cleaner_html.py page.html -o output --max-chars 1500 --overlap 150
```

| Option          | Default      | Description                                                                 |
| --------------- | ------------ | --------------------------------------------------------------------------- |
| `html_file`     | –            | Path to the HTML file                                                       |
| `-o, --out-dir` | `rag_output` | Output directory                                                            |
| `--max-chars`   | `1200`       | Maximum characters per chunk                                                |
| `--overlap`     | `150`        | Overlap between chunks when a long paragraph has to be split                |
| `--keep-hidden` | off          | Keep hidden (`display:none` / `aria-hidden`) elements, useful for tabbed pages |
| `--debug`       | off          | Print step-by-step diagnostics                                              |

**Output:** `clean.md` and `chunks.jsonl` inside the output directory.

### cleaner_txt.py

```bash
python cleaner_txt.py app.log
python cleaner_txt.py notes.txt report.txt -o output
python cleaner_txt.py ./logs --ext .log,.txt --report --unwrap
```

| Option                | Default                | Description                                                         |
| --------------------- | ---------------------- | ------------------------------------------------------------------- |
| `inputs`              | –                      | One or more files or directories                                    |
| `-o, --out-dir`       | `txt_output`           | Output directory                                                    |
| `--ext`               | `.txt,.log,.text`      | Extensions to pick up when an input is a directory                  |
| `--max-chars`         | `1200`                 | Maximum characters per chunk                                        |
| `--overlap`           | `150`                  | Overlap for very long lines                                         |
| `--no-chunks`         | off                    | Do not write the `.chunks.jsonl` file                               |
| `--drop-levels`       | `DEBUG,TRACE,VERBOSE`  | Log levels to delete entirely (`none` keeps all)                    |
| `--keep-timestamps`   | off                    | Keep the timestamp of log lines                                     |
| `--keep-traces`       | off                    | Keep stack-trace frames                                             |
| `--keep-emoji`        | off                    | Keep emoji and symbol characters                                    |
| `--keep-log-variants` | off                    | Only merge identical log lines (do not ignore numbers / ids)        |
| `--no-counts`         | off                    | Do not add `[xN]` to collapsed log lines                            |
| `--unwrap`            | off                    | Join hard-wrapped lines into paragraphs                             |
| `--report`            | off                    | Write every removed line with the reason to `<name>.removed.txt`    |

**Output (per input file):** `<name>.clean.txt`, `<name>.chunks.jsonl` and, with `--report`,
`<name>.removed.txt`.

### tokenhelper.py

Counts the tokens of a text file or of the generated output, so you can check chunk sizes
and estimate embedding costs.

```bash
python tokenhelper.py output/clean.md
```

Run `python tokenhelper.py --help` to see all available options.

## Output Format

Each line of `chunks.jsonl` is one self-contained JSON object:

```json
{
  "id": "page-0001",
  "source": "page.html",
  "heading_path": "Python Guide > Installation",
  "text": "Python Guide > Installation\n\nYou can download Python from the official website.\n- Use the installer on Windows\n- Use your package manager on Linux",
  "char_count": 171
}
```

| Field          | Description                                                        |
| -------------- | ------------------------------------------------------------------ |
| `id`           | Unique chunk id (`<source>-<index>`)                               |
| `source`       | Name of the original file                                          |
| `heading_path` | Heading hierarchy the chunk belongs to (`H1 > H2 > H3`)            |
| `text`         | Chunk text, prefixed with its heading path to improve embeddings   |
| `char_count`   | Number of characters in `text`                                     |

## How It Works

```
 HTML / TXT file
       │
       ▼
 1. Decode (encoding detection: UTF-8, UTF-16, Windows-1254, ...)
       │
       ▼
 2. Strip noise (scripts, menus, ANSI codes, log prefixes, decorations, ...)
       │
       ▼
 3. Extract meaningful blocks (paragraphs, lists, table rows, log messages)
       │
       ▼
 4. Deduplicate (exact + near-duplicate detection)
       │
       ▼
 5. Detect headings and build heading-aware chunks
       │
       ▼
 clean text / markdown  +  chunks.jsonl  →  embedding model  →  vector database
```

## Troubleshooting

- **The output is empty or incomplete (HTML).** The page is probably rendered with
  JavaScript, so the content is not in the saved file. Open the page in a browser, wait until
  it is fully loaded, then use *DevTools → Elements → right-click `<html>` → Copy outerHTML*
  and save that as your input. Run with `--debug` to see what each step finds.
- **Data in tabs is missing (HTML).** Hidden tab panels are removed by default. Use `--keep-hidden`.
- **A line was removed that should have stayed (TXT).** Run with `--report` and check
  `<name>.removed.txt` to see which rule removed it, then adjust the related option
  (`--drop-levels`, `--keep-traces`, `--keep-log-variants`, ...).
- **Garbled characters.** The scripts try UTF-8, UTF-16, Windows-1254, ISO-8859-9 and
  Latin-1 in that order. If your file uses another encoding, convert it to UTF-8 first.

## License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE.md) file for details.

---

<p align="center">Developed and published by <b>Plora AI</b></p>