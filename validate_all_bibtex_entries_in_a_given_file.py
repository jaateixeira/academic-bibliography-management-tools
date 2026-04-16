#!/usr/bin/env python3
"""
bibcheck.py — Interactive BibTeX validator and fixer.

Usage:
    python bibcheck.py [OPTIONS] file.bib [file2.bib ...]

Options:
    -v, --verbose       Show all checks, not just problems
    -d, --debug         Show internal parsing details
    -o, --output FILE   Write corrected BibTeX to FILE
    --strict            Exit with error code 1 if any problems found
    --no-interactive    Apply no corrections (report only)
"""

import argparse
import copy
import logging
import re
import sys
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import convert_to_unicode

# ── Severity ──────────────────────────────────────────────────────────────────

class Severity(Enum):
    ERROR   = auto()
    WARNING = auto()
    INFO    = auto()

SEVERITY_LABEL = {
    Severity.ERROR:   "ERROR  ",
    Severity.WARNING: "WARNING",
    Severity.INFO:    "INFO   ",
}
SEVERITY_COLOR = {
    Severity.ERROR:   "\033[91m",
    Severity.WARNING: "\033[93m",
    Severity.INFO:    "\033[94m",
}
RESET = "\033[0m"
BOLD  = "\033[1m"

def colorize(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else text

# ── Issue ─────────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    severity:    Severity
    rule_id:     str
    entry_key:   str
    field_name:  str
    description: str
    suggestion:  str
    old_value:   Optional[str] = None
    new_value:   Optional[str] = None   # None = no auto-fix

# ── Rules ─────────────────────────────────────────────────────────────────────

def check_unescaped_percent(entry):
    issues = []
    for fname, fval in entry.items():
        if fname in ("ENTRYTYPE", "ID") or not isinstance(fval, str):
            continue
        if re.search(r'(?<!\\)%', fval):
            fixed = re.sub(r'(?<!\\)%', r'\\%', fval)
            issues.append(Issue(
                severity=Severity.ERROR, rule_id="unescaped_percent",
                entry_key=entry["ID"], field_name=fname,
                description=f"Unescaped '%' in '{fname}' — acts as BibTeX comment, truncates the value.",
                suggestion="Replace '%' with '\\%'",
                old_value=fval, new_value=fixed,
            ))
    return issues


def check_unescaped_ampersand(entry):
    issues = []
    for fname, fval in entry.items():
        if fname in ("ENTRYTYPE", "ID") or not isinstance(fval, str):
            continue
        if re.search(r'(?<!\\)(?<!\{)&', fval):
            fixed = re.sub(r'(?<!\\)(?<!\{)&', r'{\\&}', fval)
            issues.append(Issue(
                severity=Severity.ERROR, rule_id="unescaped_ampersand",
                entry_key=entry["ID"], field_name=fname,
                description=f"Unescaped '&' in '{fname}' — causes runaway argument errors.",
                suggestion="Replace '&' with '{\\&}'",
                old_value=fval, new_value=fixed,
            ))
    return issues


def check_double_backslash_percent(entry):
    issues = []
    for fname, fval in entry.items():
        if fname in ("ENTRYTYPE", "ID") or not isinstance(fval, str):
            continue
        if '\\\\%' in fval:
            fixed = fval.replace('\\\\%', '\\%')
            issues.append(Issue(
                severity=Severity.WARNING, rule_id="double_backslash_percent",
                entry_key=entry["ID"], field_name=fname,
                description=f"'\\\\\\\\%' in '{fname}' produces literal backslash+%; probably meant '\\%'.",
                suggestion="Replace '\\\\\\\\%' with '\\%'",
                old_value=fval, new_value=fixed,
            ))
    return issues


def check_month_format(entry):
    months = ["jan","feb","mar","apr","may","jun",
              "jul","aug","sep","oct","nov","dec"]
    fval = entry.get("month", "")
    if not isinstance(fval, str) or not fval:
        return []
    if fval.lower() in months and fval != fval.lower():
        return [Issue(
            severity=Severity.WARNING, rule_id="month_capitalized",
            entry_key=entry["ID"], field_name="month",
            description=f"month = {fval!r} should be a lowercase BibTeX string constant (no quotes).",
            suggestion=f"Use: month = {fval.lower()}  (no quotes, lowercase)",
            old_value=fval, new_value=fval.lower(),
        )]
    return []


def check_missing_required_fields(entry):
    REQUIRED = {
        "article":       ["author", "title", "journal", "year"],
        "book":          ["author", "title", "publisher", "year"],
        "inproceedings": ["author", "title", "booktitle", "year"],
        "incollection":  ["author", "title", "booktitle", "publisher", "year"],
        "phdthesis":     ["author", "title", "school", "year"],
        "mastersthesis": ["author", "title", "school", "year"],
        "techreport":    ["author", "title", "institution", "year"],
        "misc":          ["author", "title", "year"],
    }
    etype = entry.get("ENTRYTYPE", "").lower()
    issues = []
    for req in REQUIRED.get(etype, []):
        if not str(entry.get(req, "")).strip():
            issues.append(Issue(
                severity=Severity.WARNING, rule_id="missing_required_field",
                entry_key=entry["ID"], field_name=req,
                description=f"Required field '{req}' missing for @{etype}.",
                suggestion=f"Add a '{req}' field.",
            ))
    return issues


def check_unbalanced_braces(entry):
    issues = []
    for fname, fval in entry.items():
        if fname in ("ENTRYTYPE", "ID") or not isinstance(fval, str):
            continue
        depth = 0
        for ch in fval:
            if ch == '{':  depth += 1
            elif ch == '}': depth -= 1
            if depth < 0: break
        if depth != 0:
            issues.append(Issue(
                severity=Severity.ERROR, rule_id="unbalanced_braces",
                entry_key=entry["ID"], field_name=fname,
                description=f"Unbalanced braces in '{fname}' (depth ends at {depth:+d}).",
                suggestion="Manually balance all '{' and '}' characters.",
                old_value=fval, new_value=None,
            ))
    return issues


def check_url_in_note(entry):
    note = entry.get("note", "")
    if isinstance(note, str) and note and re.search(r'https?://', note) and not entry.get("url"):
        return [Issue(
            severity=Severity.INFO, rule_id="url_in_note",
            entry_key=entry["ID"], field_name="note",
            description="URL found inside 'note' field — consider a dedicated 'url' field.",
            suggestion="Move URL to: url = {https://...}",
            old_value=note, new_value=None,
        )]
    return []


def check_title_case_protection(entry):
    title = entry.get("title", "")
    if not isinstance(title, str) or not title:
        return []
    words = title.split()
    unprotected = []
    for w in words[1:]:
        clean = w.strip('.,;:!?()')
        if clean and clean[0].isupper() and '{' not in clean and not clean.isupper():
            unprotected.append(clean)
    if unprotected:
        return [Issue(
            severity=Severity.INFO, rule_id="title_case_unprotected",
            entry_key=entry["ID"], field_name="title",
            description=f"Title has unprotected uppercase words {unprotected[:4]} — some styles will lowercase them.",
            suggestion="Wrap proper nouns/acronyms in braces: {Python}, {LaTeX}.",
            old_value=title, new_value=None,
        )]
    return []


def check_doi_url_duplicate(entry):
    doi = entry.get("doi", "")
    url = entry.get("url", "")
    if doi and url and "doi.org" in url:
        bare = doi.replace("https://doi.org/","").replace("http://doi.org/","")
        if bare in url:
            return [Issue(
                severity=Severity.INFO, rule_id="doi_url_duplicate",
                entry_key=entry["ID"], field_name="url",
                description="'url' is redundant — it is just a doi.org wrapper and 'doi' already exists.",
                suggestion="Remove the 'url' field; biblatex renders doi links automatically.",
                old_value=url, new_value=None,
            )]
    return []


ALL_RULES = [
    check_unescaped_percent,
    check_unescaped_ampersand,
    check_double_backslash_percent,
    check_month_format,
    check_missing_required_fields,
    check_unbalanced_braces,
    check_url_in_note,
    check_title_case_protection,
    check_doi_url_duplicate,
]

# ── Validator ─────────────────────────────────────────────────────────────────

def validate_entries(entries, verbose, debug):
    all_issues = []
    for entry in entries:
        key   = entry.get("ID", "???")
        etype = entry.get("ENTRYTYPE", "???")
        if debug:
            logging.debug(f"Checking @{etype}{{{key}}} ({len(entry)-2} fields)")
        entry_issues = []
        for rule in ALL_RULES:
            found = rule(entry)
            if debug and found:
                logging.debug(f"  {rule.__name__}: {[i.rule_id for i in found]}")
            entry_issues.extend(found)
        all_issues.extend(entry_issues)
        if verbose and not entry_issues:
            print(colorize(f"  ✓ @{etype}{{{key}}} — OK", "\033[92m"))
    return all_issues

# ── Correction helpers ────────────────────────────────────────────────────────

def apply_fix(entries, issue):
    if issue.new_value is None:
        return
    for entry in entries:
        if entry["ID"] == issue.entry_key:
            entry[issue.field_name] = issue.new_value
            return


def apply_fix_all_similar(entries, issues, rule_id, field_name):
    count = 0
    for iss in issues:
        if iss.rule_id == rule_id and iss.field_name == field_name and iss.new_value is not None:
            apply_fix(entries, iss)
            count += 1
    return count

# ── Interactive session ───────────────────────────────────────────────────────

def interactive_session(issues, entries, verbose, debug):
    already_all  = set()   # (rule_id, field_name) -> auto-fix all
    already_skip = set()   # (rule_id, field_name) -> skip all
    fixed_count  = 0
    skipped_count = 0

    for iss in issues:
        sig = (iss.rule_id, iss.field_name)

        if sig in already_all:
            apply_fix(entries, iss)
            fixed_count += 1
            continue
        if sig in already_skip:
            skipped_count += 1
            continue

        sev_label = colorize(f"[{SEVERITY_LABEL[iss.severity]}]",
                             SEVERITY_COLOR[iss.severity])
        print(f"\n{sev_label} {BOLD}{iss.entry_key}{RESET}  field: {iss.field_name}")
        print(f"  Problem    : {iss.description}")
        print(f"  Suggestion : {iss.suggestion}")
        if iss.old_value is not None:
            ex = iss.old_value[:120] + ("…" if len(iss.old_value) > 120 else "")
            print(f"  Old value  : {ex!r}")
        if iss.new_value is not None:
            ex = iss.new_value[:120] + ("…" if len(iss.new_value) > 120 else "")
            print(f"  New value  : {ex!r}")

        if iss.new_value is None:
            print(colorize("  (No automatic fix — manual edit required)", "\033[90m"))
            input("  Press Enter to continue… ")
            skipped_count += 1
            continue

        similar_count = sum(
            1 for i in issues
            if i.rule_id == iss.rule_id and i.field_name == iss.field_name and i.new_value is not None
        )
        prompt = f"  Fix? [y]es / [n]o / [a]ll similar ({similar_count}) / [s]kip all similar : "

        while True:
            choice = input(prompt).strip().lower()
            if choice in ("y", "yes", ""):
                apply_fix(entries, iss)
                fixed_count += 1
                break
            elif choice in ("n", "no"):
                skipped_count += 1
                break
            elif choice in ("a", "all"):
                n = apply_fix_all_similar(entries, issues, iss.rule_id, iss.field_name)
                print(colorize(f"  ✓ Fixed {n} entr{'y' if n==1 else 'ies'}.", "\033[92m"))
                fixed_count += n
                already_all.add(sig)
                break
            elif choice in ("s", "skip"):
                already_skip.add(sig)
                skipped_count += 1
                break
            else:
                print("  Please type  y / n / a / s")

    print(f"\n{colorize('Correction summary:', BOLD)} "
          f"{colorize(str(fixed_count)+' fixed', SEVERITY_COLOR[Severity.INFO])}, "
          f"{skipped_count} skipped.")

# ── Writer ────────────────────────────────────────────────────────────────────

def write_bib(entries, strings, path):
    db = bibtexparser.bibdatabase.BibDatabase()
    db.entries = entries
    db.strings = strings
    writer = bibtexparser.bwriter.BibTexWriter()
    writer.indent = "  "
    with open(path, "w", encoding="utf-8") as f:
        f.write(writer.write(db))
    print(colorize(f"\n✓ Corrected BibTeX written to: {path}", "\033[92m"))

# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Validate and interactively fix BibTeX files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("bibfiles", nargs="+", metavar="FILE.bib")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Show all entries, not just problems")
    p.add_argument("-d", "--debug", action="store_true",
                   help="Show internal rule-firing details")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="Write corrected BibTeX to FILE")
    p.add_argument("--strict", action="store_true",
                   help="Exit code 1 if any problems found (CI-friendly)")
    p.add_argument("--no-interactive", action="store_true",
                   help="Report only — do not prompt for corrections")
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(format="[DEBUG] %(message)s",
                        level=logging.DEBUG if args.debug else logging.WARNING)

    all_issues     = []
    combined_entries = []
    combined_strings = {}

    for bibfile in args.bibfiles:
        print(colorize(f"\n{'─'*62}", "\033[90m"))
        print(colorize(f"  {bibfile}", BOLD))
        print(colorize(f"{'─'*62}", "\033[90m"))

        try:
            with open(bibfile, encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            print(colorize(f"ERROR: File not found: {bibfile}", "\033[91m"), file=sys.stderr)
            sys.exit(1)
        except UnicodeDecodeError:
            print(colorize(f"ERROR: {bibfile} is not UTF-8. Save it as UTF-8 first.",
                           "\033[91m"), file=sys.stderr)
            sys.exit(1)

        parser = BibTexParser(common_strings=True)
        parser.customization = convert_to_unicode
        parser.ignore_nonstandard_types = False

        try:
            db = bibtexparser.loads(raw, parser)
        except Exception as e:
            print(colorize(f"Parse error in {bibfile}: {e}", "\033[91m"), file=sys.stderr)
            if args.strict:
                sys.exit(1)
            continue

        if args.debug:
            logging.debug(f"Parsed {len(db.entries)} entries, {len(db.strings)} strings")

        issues = validate_entries(db.entries, args.verbose, args.debug)
        all_issues.extend(issues)
        combined_entries.extend(copy.deepcopy(db.entries))
        combined_strings.update(db.strings)

        errors   = sum(1 for i in issues if i.severity == Severity.ERROR)
        warnings = sum(1 for i in issues if i.severity == Severity.WARNING)
        infos    = sum(1 for i in issues if i.severity == Severity.INFO)
        print(f"\n  {len(db.entries)} entries — "
              f"{colorize(f'{errors} error(s)', SEVERITY_COLOR[Severity.ERROR])}  "
              f"{colorize(f'{warnings} warning(s)', SEVERITY_COLOR[Severity.WARNING])}  "
              f"{colorize(f'{infos} note(s)', SEVERITY_COLOR[Severity.INFO])}")

    # Strict mode
    if args.strict and all_issues:
        errors = sum(1 for i in all_issues if i.severity == Severity.ERROR)
        print(colorize(
            f"\n[STRICT] {len(all_issues)} issue(s) found ({errors} error(s)). Exiting 1.",
            "\033[91m"), file=sys.stderr)
        sys.exit(1)

    if not all_issues:
        print(colorize("\n✓ No issues found.", "\033[92m"))
        return

    # Interactive or report-only
    if not args.no_interactive and sys.stdin.isatty():
        print(colorize(f"\n{'═'*62}", BOLD))
        print(colorize("  Interactive correction", BOLD))
        print(colorize(f"{'═'*62}", BOLD))
        interactive_session(all_issues, combined_entries, args.verbose, args.debug)
    else:
        print(colorize(f"\n{'═'*62}", BOLD))
        print(colorize("  Issues (report-only mode)", BOLD))
        print(colorize(f"{'═'*62}", BOLD))
        for iss in all_issues:
            sev = colorize(f"[{SEVERITY_LABEL[iss.severity]}]",
                           SEVERITY_COLOR[iss.severity])
            print(f"  {sev}  {iss.entry_key} / {iss.field_name}")
            print(f"           {iss.description}")

    if args.output:
        write_bib(combined_entries, combined_strings, args.output)


if __name__ == "__main__":
    main()
