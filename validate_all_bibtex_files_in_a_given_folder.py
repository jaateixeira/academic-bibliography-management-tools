#!/usr/bin/env python3
"""
validate.py - Validate all .bib files in the repository as proper BibTeX entries.
Usage: python validate.py [directory]
"""

import sys
import os
import glob
import argparse
from pathlib import Path

try:
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
    from bibtexparser.customization import homogenize_latex_encoding
except ImportError:
    print("Error: bibtexparser not installed. Run: pip install bibtexparser")
    sys.exit(1)


def validate_bibtex_file(filepath):
    """
    Validate a single BibTeX file.
    Returns (is_valid, error_message, warnings)
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Parse with strict mode
        parser = BibTexParser(common_strings=True)
        parser.ignore_nonstandard_types = False
        parser.homogenize_fields = True
        
        # Try to parse the file
        bib_database = bibtexparser.loads(content, parser=parser)
        
        # Check if we got any entries
        if len(bib_database.entries) == 0 and content.strip():
            # File has content but no entries found - might be a problem
            return False, "File contains no valid BibTeX entries", None
        
        # Check for common issues
        warnings = []
        for entry in bib_database.entries:
            # Check required fields for common entry types
            required_fields = {
                'article': ['author', 'title', 'journal', 'year'],
                'book': ['author', 'title', 'publisher', 'year'],
                'inproceedings': ['author', 'title', 'booktitle', 'year'],
                'phdthesis': ['author', 'title', 'school', 'year'],
                'mastersthesis': ['author', 'title', 'school', 'year'],
            }
            
            entry_type = entry.get('ENTRYTYPE', '')
            if entry_type in required_fields:
                missing = [f for f in required_fields[entry_type] if f not in entry]
                if missing:
                    warnings.append(f"Entry '{entry.get('ID', 'unknown')}' missing {missing}")
        
        return True, None, warnings if warnings else None
        
    except Exception as e:
        return False, str(e), None


def find_bib_files(directory="."):
    """Find all .bib files in the given directory recursively."""
    bib_files = []
    for root, dirs, files in os.walk(directory):
        # Skip .git and other hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for file in files:
            if file.endswith('.bib'):
                bib_files.append(os.path.join(root, file))
    return bib_files


def main():
    parser = argparse.ArgumentParser(
        description="Validate all BibTeX files in the repository"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan for .bib files (default: current directory)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed warnings for each file"
    )
    
    args = parser.parse_args()
    
    # Find all .bib files
    bib_files = find_bib_files(args.directory)
    
    if not bib_files:
        print(f"No .bib files found in {args.directory}")
        return 0
    
    print(f"Found {len(bib_files)} BibTeX file(s) to validate")
    print("-" * 60)
    
    # Validate each file
    failed_files = []
    all_warnings = []
    
    for bib_file in bib_files:
        rel_path = os.path.relpath(bib_file, args.directory)
        is_valid, error, warnings = validate_bibtex_file(bib_file)
        
        if is_valid:
            if warnings:
                print(f"✓ {rel_path} (with warnings)")
                if args.verbose:
                    for warning in warnings:
                        print(f"  ⚠ {warning}")
                all_warnings.extend([(rel_path, w) for w in warnings])
            else:
                print(f"✓ {rel_path}")
        else:
            print(f"✗ {rel_path}")
            print(f"  Error: {error}")
            failed_files.append((rel_path, error))
    
    print("-" * 60)
    
    # Summary
    if failed_files:
        print(f"\n❌ Validation failed: {len(failed_files)} file(s) with errors")
        if not args.verbose:
            print("Run with --verbose to see warnings")
        return 1
    elif all_warnings:
        print(f"\n⚠️  Validation passed with {len(all_warnings)} warning(s)")
        return 0
    else:
        print("\n✅ All BibTeX files are valid!")
        return 0


if __name__ == "__main__":
    sys.exit(main())