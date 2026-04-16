#!/usr/bin/env python3
"""
bibprune.py — Extract only the BibTeX entries cited in a LaTeX document.

Resolves \\input / \\include chains recursively, collects every citation key
used by any cite-family command, then writes a trimmed .bib containing only
those entries.

Usage:
    python bibprune.py --large-bib-file big.bib --tex-file main.tex --output small.bib

Aliases:
    --large-bib-file / --bib
    --output         / --small-bib-file / -o
    --tex-file       / --tex
"""

import argparse
import re
import sys
from pathlib import Path

import bibtexparser
from bibtexparser.bwriter import BibTexWriter
from bibtexparser.bparser import BibTexParser

# ── ANSI colours ──────────────────────────────────────────────────────────────
RESET = "\033[0m"
BOLD  = "\033[1m"
RED   = "\033[31m"
YELLOW= "\033[33m"
GREEN = "\033[32m"
CYAN  = "\033[36m"
GREY  = "\033[90m"

def c(text, *codes):
    return "".join(codes) + str(text) + RESET


# ── Citation-command regex ─────────────────────────────────────────────────────
# Matches \cite, \citep, \citet, \citealt, \citealp, \citeauthor,
# \citeyear, \citeyearpar, \citenum, \nocite, \footcite, \parencite,
# \textcite, \autocite, \smartcite, \supercite — and any \cite* variant.
# Each command may hold one or more comma-separated keys, e.g. \cite{a,b,c}.
_CITE_RE = re.compile(
    r'\\(?:no)?cite[a-zA-Z*]*'   # command name
    r'(?:\[[^\]]*\])*'            # optional [...] modifiers
    r'\{([^}]+)\}',               # {key,key,...}
    re.UNICODE,
)

# \input{file} or \include{file} — with or without .tex extension
_INPUT_RE = re.compile(
    r'\\(?:input|include)\s*\{([^}]+)\}',
    re.UNICODE,
)

# Strip LaTeX comments from a line
_COMMENT_RE = re.compile(r'(?<!\\)%.*$')


def strip_comments(line: str) -> str:
    return _COMMENT_RE.sub("", line)


# ── TeX crawler ───────────────────────────────────────────────────────────────
def collect_tex_files(root: Path, verbose: bool, debug: bool,
                      _visited: set | None = None) -> list[Path]:
    """Recursively follow \\input / \\include and return all .tex files."""
    if _visited is None:
        _visited = set()

    resolved = root.resolve()
    if resolved in _visited:
        if debug:
            print(c(f"  [debug] already visited {root}, skipping", GREY))
        return []
    _visited.add(resolved)

    files = [root]
    if verbose:
        print(c(f"  → reading {root}", GREY))

    try:
        text = root.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(c(f"  [warn] cannot read '{root}': {e}", YELLOW))
        return files

    for m in _INPUT_RE.finditer(text):
        raw = m.group(1).strip()
        # add .tex if missing
        candidate = root.parent / raw
        if not candidate.suffix:
            candidate = candidate.with_suffix(".tex")
        if candidate.exists():
            if debug:
                print(c(f"  [debug] found \\input -> {candidate}", GREY))
            files.extend(collect_tex_files(candidate, verbose, debug, _visited))
        else:
            print(c(f"  [warn] \\input target not found: {candidate}", YELLOW))

    return files


def collect_citation_keys(tex_files: list[Path], verbose: bool, debug: bool) -> set[str]:
    """Extract all citation keys from a list of .tex files."""
    keys: set[str] = set()

    for tex in tex_files:
        try:
            text = tex.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(c(f"  [warn] cannot read '{tex}': {e}", YELLOW))
            continue

        # strip comments line by line
        clean = "\n".join(strip_comments(line) for line in text.splitlines())

        found_here = 0
        for m in _CITE_RE.finditer(clean):
            for raw_key in m.group(1).split(","):
                key = raw_key.strip()
                if key:
                    keys.add(key)
                    found_here += 1

        if debug:
            print(c(f"  [debug] {tex.name}: {found_here} cite references found", GREY))

    return keys


# ── BibTeX loader ──────────────────────────────────────────────────────────────
def load_bib(bib_path: Path, verbose: bool, debug: bool):
    if verbose:
        print(c(f"  Loading '{bib_path}' …", GREY))

    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    parser.homogenise_fields = False

    try:
        raw = bib_path.read_text(encoding="utf-8", errors="replace")
        db  = bibtexparser.loads(raw, parser=parser)
    except Exception as e:
        print(c(f"[ERROR] Could not parse '{bib_path}': {e}", RED, BOLD))
        sys.exit(1)

    if debug:
        print(c(f"  [debug] parsed {len(db.entries)} entries from '{bib_path}'", GREY))

    return db


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="bibprune",
        description="Extract only the BibTeX entries cited in a LaTeX document.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--large-bib-file", "--bib",
        required=True,
        metavar="FILE",
        dest="bib_file",
        help="Input (large) .bib file",
    )
    parser.add_argument(
        "--tex-file", "--tex",
        required=True,
        metavar="FILE",
        dest="tex_file",
        help="Root .tex file (\\input chains are followed automatically)",
    )
    parser.add_argument(
        "--output", "--small-bib-file", "-o",
        required=True,
        metavar="FILE",
        dest="output",
        help="Output (pruned) .bib file",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show which files are read and how many keys are found",
    )
    parser.add_argument(
        "-d", "--debug",
        action="store_true",
        help="Show internal parsing details",
    )
    parser.add_argument(
        "--missing-warn",
        action="store_true",
        default=True,
        help="Warn about cited keys not found in the .bib (default: on)",
    )
    parser.add_argument(
        "--no-missing-warn",
        action="store_false",
        dest="missing_warn",
        help="Suppress warnings about missing keys",
    )

    args = parser.parse_args()

    bib_path = Path(args.bib_file)
    tex_path = Path(args.tex_file)
    out_path = Path(args.output)

    for p, label in [(bib_path, "--large-bib-file"), (tex_path, "--tex-file")]:
        if not p.exists():
            print(c(f"[ERROR] {label} not found: '{p}'", RED, BOLD))
            sys.exit(1)

    print(c("\nbibprune", BOLD, CYAN) + c(f" — pruning '{bib_path.name}' for '{tex_path.name}'\n", BOLD))

    # 1. crawl .tex files
    if args.verbose:
        print(c("Step 1: Collecting .tex files …", BOLD))
    tex_files = collect_tex_files(tex_path, args.verbose, args.debug)
    if args.verbose:
        print(c(f"  {len(tex_files)} .tex file(s) found\n", GREEN))

    # 2. collect citation keys
    if args.verbose:
        print(c("Step 2: Extracting citation keys …", BOLD))
    cited_keys = collect_citation_keys(tex_files, args.verbose, args.debug)
    if args.verbose:
        print(c(f"  {len(cited_keys)} unique citation key(s) found\n", GREEN))
    if args.debug:
        for k in sorted(cited_keys):
            print(c(f"    {k}", GREY))

    if not cited_keys:
        print(c("[WARN] No citation keys found in the .tex file(s). Output will be empty.", YELLOW))

    # 3. load bib
    if args.verbose:
        print(c("Step 3: Loading .bib file …", BOLD))
    db = load_bib(bib_path, args.verbose, args.debug)

    # build lookup: key → entry
    bib_index = {e["ID"]: e for e in db.entries}

    # 4. filter
    if args.verbose:
        print(c("\nStep 4: Matching cited keys against .bib entries …", BOLD))

    kept    = []
    missing = []

    for key in sorted(cited_keys):
        if key in bib_index:
            kept.append(bib_index[key])
            if args.debug:
                print(c(f"  ✓ {key}", GREEN))
        else:
            missing.append(key)
            if args.debug:
                print(c(f"  ✗ {key} (not in .bib)", RED))

    # 5. warn about missing
    if args.missing_warn and missing:
        print()
        print(c(f"[WARN] {len(missing)} cited key(s) not found in '{bib_path.name}':", YELLOW, BOLD))
        for k in sorted(missing):
            print(c(f"  • {k}", YELLOW))

    # 6. write output
    out_db         = bibtexparser.bibdatabase.BibDatabase()
    out_db.entries = kept

    writer         = BibTexWriter()
    writer.indent  = "  "
    writer.order_entries_by = ("author", "year", "title")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(bibtexparser.dumps(out_db, writer), encoding="utf-8")

    # 7. summary
    original_count = len(db.entries)
    kept_count     = len(kept)
    dropped_count  = original_count - kept_count
    pct_reduction  = 100 * dropped_count / original_count if original_count else 0

    print()
    print(c("─" * 55, GREY))
    print(f"  .tex files scanned  : {len(tex_files)}")
    print(f"  Citation keys found : {len(cited_keys)}")
    print(f"  Entries in source   : {original_count}")
    print(f"  Entries kept        : {c(kept_count, GREEN, BOLD)}")
    print(f"  Entries dropped     : {c(dropped_count, GREY)}")
    print(f"  Size reduction      : {c(f'{pct_reduction:.1f}%', CYAN, BOLD)}")
    if missing:
        print(f"  Keys not in .bib    : {c(len(missing), YELLOW, BOLD)}")
    print(c("─" * 55, GREY))
    print(c(f"\n  ✔ Written to '{out_path}'\n", GREEN, BOLD))


if __name__ == "__main__":
    main()
