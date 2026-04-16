#!/usr/bin/env python3
"""
bibscan.py — Scan a BibTeX file for unescaped or problematic characters.

Checks every field value for characters that are special in LaTeX / BibTeX
and will cause compilation errors or silent corruption:

    %   comment character         → \\%
    &   alignment character       → {\\&}
    #   parameter character       → \\#
    _   subscript (outside math)  → \\_
    ^   superscript (outside math)→ \\^{}
    ~   non-breaking space (ok in some contexts, flagged for review)
    $   math shift (unbalanced)
    { } unbalanced braces
    "   straight quote in value   → use {} delimiters or LaTeX quotes
    Bare URLs without \\url{}
    Non-ASCII / Unicode characters that may not survive encoding changes

Usage:
    python bibscan.py [options] file.bib [file2.bib ...]

Options:
    -v, --verbose       Print every field checked, not just problems
    -d, --debug         Show regex match details
    -o, --output FILE   Write a report to FILE instead of stdout
    --strict            Exit with code 1 if any issues found
    --no-colour         Disable ANSI colour output
    --fields FIELDS     Comma-separated fields to scan (default: all)
    --skip-fields FIELDS  Comma-separated fields to skip (e.g. abstract,url)
    --only-errors       Show only ERROR-level issues, suppress warnings
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import bibtexparser
from bibtexparser.bparser import BibTexParser

# ── ANSI colours ──────────────────────────────────────────────────────────────
_USE_COLOUR = True

def _c(text, *codes):
    if not _USE_COLOUR:
        return str(text)
    return "".join(codes) + str(text) + "\033[0m"

BOLD   = "\033[1m"
RED    = "\033[31m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
CYAN   = "\033[36m"
GREY   = "\033[90m"
BLUE   = "\033[34m"


# ── Issue dataclass ────────────────────────────────────────────────────────────
@dataclass
class Hit:
    severity: str        # 'error' | 'warning' | 'info'
    rule_id:  str
    entry_id: str
    entry_type: str
    field_name: str
    position:   int      # character offset in field value
    context:    str      # snippet around the problem
    message:    str
    suggestion: str      # LaTeX fix, if known


# ── Rules ─────────────────────────────────────────────────────────────────────
@dataclass
class CharRule:
    rule_id:    str
    severity:   str
    pattern:    re.Pattern
    message:    str
    suggestion: str
    skip_math:  bool = False   # if True, ignore matches inside $...$


# Characters that, if preceded by a backslash, are already escaped
_BACKSLASH_ESCAPED = re.compile(r'\\.')

def _mask_escaped(val: str) -> str:
    """Replace already-escaped sequences with harmless placeholders."""
    return _BACKSLASH_ESCAPED.sub(lambda m: "X" * len(m.group()), val)

def _mask_math(val: str) -> str:
    """Replace content inside $...$ with placeholders (single $ only)."""
    return re.sub(r'\$[^$]*\$', lambda m: "X" * len(m.group()), val)

def _mask_braced_urls(val: str) -> str:
    """Replace {http...} with placeholders so bare-URL rule doesn't double-fire."""
    return re.sub(r'\{https?://[^}]*\}', lambda m: "X" * len(m.group()), val)

def _context_snippet(val: str, pos: int, width: int = 40) -> str:
    """Return a short snippet of val centred on pos, with a caret marker."""
    start = max(0, pos - width // 2)
    end   = min(len(val), pos + width // 2)
    snippet = val[start:end].replace("\n", "↵")
    caret_pos = pos - start
    return snippet, caret_pos


RULES: list[CharRule] = [

    CharRule(
        rule_id    = "unescaped_percent",
        severity   = "error",
        pattern    = re.compile(r'(?<!\\)%'),
        message    = "Unescaped '%' — BibTeX treats everything after it as a comment",
        suggestion = r"\%",
    ),

    CharRule(
        rule_id    = "unescaped_ampersand",
        severity   = "error",
        pattern    = re.compile(r'(?<!\\)&'),
        message    = "Unescaped '&' — LaTeX alignment character, causes 'Misplaced &' error",
        suggestion = r"{\&}",
    ),

    CharRule(
        rule_id    = "unescaped_hash",
        severity   = "error",
        pattern    = re.compile(r'(?<!\\)#'),
        message    = "Unescaped '#' — LaTeX parameter character, causes errors in most contexts",
        suggestion = r"\#",
    ),

    CharRule(
        rule_id    = "unescaped_underscore",
        severity   = "error",
        pattern    = re.compile(r'(?<!\\)_'),
        message    = "Unescaped '_' — LaTeX subscript operator; causes error outside math mode",
        suggestion = r"\_",
        skip_math  = True,
    ),

    CharRule(
        rule_id    = "unescaped_caret",
        severity   = "error",
        pattern    = re.compile(r'(?<!\\)\^'),
        message    = "Unescaped '^' — LaTeX superscript operator; causes error outside math mode",
        suggestion = r"\^{}",
        skip_math  = True,
    ),

    CharRule(
        rule_id    = "double_backslash_percent",
        severity   = "warning",
        pattern    = re.compile(r'\\\\%'),
        message    = r"Found '\\%' — should be '\%' (one backslash, not two)",
        suggestion = r"\%",
    ),

    CharRule(
        rule_id    = "unbalanced_dollar",
        severity   = "warning",
        # matches an odd number of $: heuristic only
        pattern    = re.compile(r'(?<!\$)\$(?!\$)'),
        message    = "Single '$' — check for unbalanced math mode delimiters",
        suggestion = "Ensure math content is wrapped in $...$",
    ),

    CharRule(
        rule_id    = "straight_double_quote",
        severity   = "warning",
        pattern    = re.compile(r'"'),
        message    = 'Straight double-quote (\") in field value — prefer {braces} or LaTeX quotes ``…\'\'',
        suggestion = "Use {braces} around the value, or ``text'' for typographic quotes",
    ),

    CharRule(
        rule_id    = "tilde_in_text",
        severity   = "info",
        pattern    = re.compile(r'(?<!\\)~'),
        message    = "Literal '~' — in BibTeX values this is a non-breaking space; intentional?",
        suggestion = "If a non-breaking space is intended, this is fine. Otherwise remove it.",
    ),

    CharRule(
        rule_id    = "bare_url",
        severity   = "warning",
        pattern    = re.compile(r'(?<!\{)https?://\S+'),
        message    = r"Bare URL not wrapped in \url{} or a url/howpublished field",
        suggestion = r"\url{https://...}  or use the 'url' field",
    ),

    CharRule(
        rule_id    = "non_ascii",
        severity   = "info",
        pattern    = re.compile(r'[^\x00-\x7F]'),
        message    = "Non-ASCII character — safe only with UTF-8 input encoding declared; "
                     "use LaTeX encoding commands for maximum portability (e.g. \\'{e} for é)",
        suggestion = "Use \\'{e}, \\\"{u}, etc. for accented chars if portability is needed",
    ),
]


# Brace-balance check (not a regex rule — handled separately)
def check_brace_balance(val: str, entry_id: str, entry_type: str,
                        field_name: str) -> list[Hit]:
    hits = []
    depth = 0
    for i, ch in enumerate(val):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth < 0:
                snippet, cp = _context_snippet(val, i)
                hits.append(Hit(
                    severity   = "error",
                    rule_id    = "unbalanced_brace_close",
                    entry_id   = entry_id,
                    entry_type = entry_type,
                    field_name = field_name,
                    position   = i,
                    context    = snippet,
                    message    = "Unexpected closing '}' — no matching opening brace",
                    suggestion = "Remove the extra '}' or add a matching '{'",
                ))
                depth = 0  # reset so we keep finding more
    if depth > 0:
        hits.append(Hit(
            severity   = "error",
            rule_id    = "unbalanced_brace_open",
            entry_id   = entry_id,
            entry_type = entry_type,
            field_name = field_name,
            position   = len(val) - 1,
            context    = val[-40:].replace("\n", "↵"),
            message    = f"Unclosed '{{' — {depth} opening brace(s) never closed",
            suggestion = f"Add {depth} closing brace(s) '}}' at the end of the value",
        ))
    return hits


# ── Scanner ────────────────────────────────────────────────────────────────────
SKIP_KEYS = {"ID", "ENTRYTYPE"}

# Fields where bare URLs are expected — don't fire bare_url there
URL_FIELDS = {"url", "howpublished", "eprint"}


def scan_entry(entry: dict, rules: list[CharRule],
               only_fields: Optional[set],
               skip_fields: set,
               verbose: bool, debug: bool) -> list[Hit]:
    hits: list[Hit] = []
    eid   = entry.get("ID", "<no key>")
    etype = entry.get("ENTRYTYPE", "?")

    for fname, fval in entry.items():
        if fname in SKIP_KEYS:
            continue
        if only_fields and fname not in only_fields:
            continue
        if fname in skip_fields:
            continue
        if not isinstance(fval, str):
            continue

        if verbose:
            print(_c(f"    checking field '{fname}' ({len(fval)} chars) …", GREY))

        # brace balance
        hits.extend(check_brace_balance(fval, eid, etype, fname))

        # character rules
        for rule in rules:
            # build a working copy with already-escaped seqs masked out
            working = _mask_escaped(fval)

            if rule.skip_math:
                working = _mask_math(working)

            if rule.rule_id == "bare_url" and fname in URL_FIELDS:
                continue  # urls are expected there

            if rule.rule_id == "bare_url":
                working = _mask_braced_urls(working)

            # straight_double_quote: the field delimiters are stripped by
            # bibtexparser, so any remaining " in the value is real
            if rule.rule_id == "straight_double_quote":
                working = fval  # use raw, not masked

            for m in rule.pattern.finditer(working):
                pos = m.start()
                snippet, _ = _context_snippet(fval, pos)

                if debug:
                    print(_c(
                        f"      [{rule.rule_id}] match at pos {pos}: "
                        f"'{fval[pos:pos+8].encode()}' context: '{snippet}'", GREY))

                hits.append(Hit(
                    severity   = rule.severity,
                    rule_id    = rule.rule_id,
                    entry_id   = eid,
                    entry_type = etype,
                    field_name = fname,
                    position   = pos,
                    context    = snippet,
                    message    = rule.message,
                    suggestion = rule.suggestion,
                ))

    return hits


# ── Reporting ──────────────────────────────────────────────────────────────────
_SEV_LABEL = {
    "error":   lambda: _c("[ERROR]", RED, BOLD),
    "warning": lambda: _c("[WARN] ", YELLOW, BOLD),
    "info":    lambda: _c("[INFO] ", BLUE, BOLD),
}

def _sev_order(h: Hit) -> int:
    return {"error": 0, "warning": 1, "info": 2}.get(h.severity, 9)


def print_report(all_hits: list[Hit], bib_path: Path,
                 entry_count: int, out=sys.stdout):

    def w(*args, **kwargs):
        print(*args, file=out, **kwargs)

    # group by entry
    by_entry: dict[str, list[Hit]] = {}
    for h in sorted(all_hits, key=_sev_order):
        by_entry.setdefault(h.entry_id, []).append(h)

    for eid, hits in by_entry.items():
        etype = hits[0].entry_type
        w(_c(f"\n@{etype}{{{eid}}}", BOLD))
        for h in hits:
            label = _SEV_LABEL.get(h.severity, lambda: "[?]")()
            w(f"  {label} field {_c(h.field_name, CYAN)}: {h.message}")
            w(f"         {_c('context:', GREY)}   …{h.context}…")
            w(f"         {_c('fix:', GREY)}       {h.suggestion}")

    # summary
    errors   = sum(1 for h in all_hits if h.severity == "error")
    warnings = sum(1 for h in all_hits if h.severity == "warning")
    infos    = sum(1 for h in all_hits if h.severity == "info")

    w()
    w(_c("─" * 60, GREY))
    w(f"  File             : {bib_path}")
    w(f"  Entries scanned  : {entry_count}")
    w(f"  Entries with hits: {len(by_entry)}")
    w(f"  Errors           : {_c(errors,   RED,    BOLD) if errors   else _c(errors,   GREEN)}")
    w(f"  Warnings         : {_c(warnings, YELLOW, BOLD) if warnings else _c(warnings, GREEN)}")
    w(f"  Info             : {infos}")
    w(_c("─" * 60, GREY))

    return errors, warnings


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        prog="bibscan",
        description="Scan BibTeX files for unescaped or problematic characters.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("bib_files", nargs="+", metavar="FILE",
                    help="One or more .bib files to scan")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Show every field as it is checked")
    ap.add_argument("-d", "--debug", action="store_true",
                    help="Show regex match details")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="Write report to FILE instead of stdout")
    ap.add_argument("--strict", action="store_true",
                    help="Exit with code 1 if any errors or warnings found")
    ap.add_argument("--no-colour", action="store_true",
                    help="Disable ANSI colour output")
    ap.add_argument("--fields", metavar="FIELDS",
                    help="Only scan these fields (comma-separated)")
    ap.add_argument("--skip-fields", metavar="FIELDS", default="",
                    help="Skip these fields (comma-separated, e.g. abstract,url)")
    ap.add_argument("--only-errors", action="store_true",
                    help="Show only ERROR-level issues")
    ap.add_argument("--list-rules", action="store_true",
                    help="Print all rules and exit")

    args = ap.parse_args()

    global _USE_COLOUR
    if args.no_colour:
        _USE_COLOUR = False

    if args.list_rules:
        print(_c("Available rules:", BOLD))
        for r in RULES:
            sev = _SEV_LABEL[r.severity]()
            print(f"  {sev} {_c(r.rule_id, CYAN):45s} {r.message[:60]}")
        print(f"  {_c('[ERROR]', RED, BOLD)} {'brace_balance':45s} Detects unmatched {{ or }}")
        sys.exit(0)

    only_fields = (
        {f.strip().lower() for f in args.fields.split(",")}
        if args.fields else None
    )
    skip_fields = (
        {f.strip().lower() for f in args.skip_fields.split(",") if f.strip()}
    )

    active_rules = [
        r for r in RULES
        if not (args.only_errors and r.severity != "error")
    ]

    out_stream = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout

    grand_errors = 0
    grand_warnings = 0

    for bib_file in args.bib_files:
        bib_path = Path(bib_file)
        if not bib_path.exists():
            print(_c(f"[ERROR] File not found: '{bib_path}'", RED, BOLD))
            sys.exit(1)

        print(_c(f"\nbibscan", BOLD, CYAN) +
              _c(f" — scanning '{bib_path}'\n", BOLD), file=out_stream)

        parser = BibTexParser(common_strings=True)
        parser.ignore_nonstandard_types = False
        parser.homogenise_fields = False
        try:
            raw = bib_path.read_text(encoding="utf-8", errors="replace")
            db  = bibtexparser.loads(raw, parser=parser)
        except Exception as e:
            print(_c(f"[ERROR] Cannot parse '{bib_path}': {e}", RED, BOLD), file=out_stream)
            sys.exit(1)

        all_hits: list[Hit] = []

        for entry in db.entries:
            eid = entry.get("ID", "<no key>")
            if args.verbose:
                print(_c(f"\n  @{entry.get('ENTRYTYPE','?')}{{{eid}}}", BOLD),
                      file=out_stream)

            hits = scan_entry(entry, active_rules, only_fields,
                              skip_fields, args.verbose, args.debug)
            all_hits.extend(hits)

        if not all_hits:
            print(_c("  ✓ No issues found.", GREEN), file=out_stream)
        else:
            errors, warnings = print_report(all_hits, bib_path,
                                            len(db.entries), out=out_stream)
            grand_errors   += errors
            grand_warnings += warnings

    if args.output:
        out_stream.close()
        print(_c(f"\n  ✔ Report written to '{args.output}'", GREEN, BOLD))

    if args.strict and (grand_errors > 0 or grand_warnings > 0):
        print(_c("\n[STRICT] Issues found — exiting with code 1.", RED, BOLD))
        sys.exit(1)


if __name__ == "__main__":
    main()
