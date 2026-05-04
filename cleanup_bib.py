#!/usr/bin/env python3
"""Clean up DBLP-generated .bib files in the bib/ directory.

Performs the following transformations on every .bib file under bib/:
  - Removes DBLP disambiguation numbers from author/editor names
    (e.g. "John Smith 0001" -> "John Smith").
  - Removes ``biburl`` and ``bibsource`` fields entirely.
  - Expands abbreviated DBLP journal names to their full canonical forms.
  - Strips trailing whitespace from all lines.
  - Collapses runs of 3+ consecutive blank lines down to 2.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BIB_DIR = Path(__file__).parent / "bib"

# Matches a DBLP numeric disambiguator appended to an author name:
#   "Foo Bar 0042" or "Foo Bar 0042 and ..." — the number is always 4+ digits.
DISAMBIG_RE = re.compile(r"\s+\b0\d{3,}\b")

# Fields to drop entirely (matched case-insensitively against the field key).
FIELDS_TO_DROP = {"biburl", "bibsource"}

# Maps every DBLP abbreviated journal name to its full canonical form.
JOURNAL_EXPANSIONS: dict[str, str] = {
    "Comb.": "Combinatorica",
    "Electron. Colloquium Comput. Complex.": "Electronic Colloquium on Computational Complexity",
    "Inf. Process. Lett.": "Information Processing Letters",
    "J. ACM": "Journal of the {ACM}",
    "J. Symb. Log.": "Journal of Symbolic Logic",
    "Theor. Comput. Sci.": "Theoretical Computer Science",
}

# Pre-compiled regex that matches any abbreviated journal name inside braces.
_ABBREV_PATTERN = re.compile(
    r"(\s*journal\s*=\s*\{)("
    + "|".join(re.escape(k) for k in JOURNAL_EXPANSIONS)
    + r")(\})",
    re.IGNORECASE,
)


def _expand_journal(line: str) -> str:
    """Replace an abbreviated journal value on *line* with its full name.

    Args:
        line: A single line from a .bib file.

    Returns:
        The line with the journal abbreviation expanded, or unchanged if no
        known abbreviation is present.
    """

    def _replace(m: re.Match[str]) -> str:  # type: ignore[type-arg]
        abbrev = m.group(2)
        for key, full in JOURNAL_EXPANSIONS.items():
            if abbrev.lower() == key.lower():
                return m.group(1) + full + m.group(3)
        return m.group(0)

    return _ABBREV_PATTERN.sub(_replace, line)


def _drop_field(lines: list[str], start: int) -> int:
    """Remove a multi-line bib field beginning at *start* from *lines* in-place.

    Args:
        lines: The full list of file lines, modified in-place.
        start: Index of the line containing the field key.

    Returns:
        The index of the first line that follows the removed field.
    """
    i = start + 1
    # A field value continues until we reach a line that closes the brace
    # or starts a new field / entry terminator.
    while i < len(lines):
        if re.match(r"\s*(}|\w+\s*=)", lines[i]):
            break
        i += 1
    del lines[start:i]
    return start


def clean_file(path: Path) -> bool:
    """Clean a single .bib file in-place.

    Args:
        path: Path to the .bib file to clean.

    Returns:
        True if the file was modified, False if it was already clean.
    """
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    i = 0
    while i < len(lines):
        line = lines[i]

        # Strip trailing whitespace while preserving the line terminator.
        stripped = line.rstrip()
        if stripped != line.rstrip("\n"):
            lines[i] = stripped + "\n" if line.endswith("\n") else stripped
            line = lines[i]

        field_match = re.match(r"\s*(\w+)\s*=", line)
        if field_match:
            field_name = field_match.group(1).lower()
            if field_name in FIELDS_TO_DROP:
                i = _drop_field(lines, i)
                continue
            if field_name == "journal":
                lines[i] = _expand_journal(lines[i])
                line = lines[i]

        # Remove DBLP disambiguators from author/editor fields.
        # Values may span multiple lines joined by "and".
        if field_match and field_match.group(1).lower() in {"author", "editor"}:
            j = i
            brace_depth = line.count("{") - line.count("}")
            while brace_depth > 0 and j + 1 < len(lines):
                j += 1
                brace_depth += lines[j].count("{") - lines[j].count("}")
            for k in range(i, j + 1):
                lines[k] = DISAMBIG_RE.sub("", lines[k])

        i += 1

    # Collapse runs of 3+ blank lines to at most 2.
    result_lines: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 2:
                result_lines.append(line)
        else:
            blank_run = 0
            result_lines.append(line)

    result = "".join(result_lines)
    if result == original:
        return False
    path.write_text(result, encoding="utf-8")
    return True


def main() -> None:
    """Entry point: clean every .bib file under BIB_DIR and report results."""
    bib_files = sorted(BIB_DIR.glob("*.bib"))
    if not bib_files:
        print(f"No .bib files found in {BIB_DIR}", file=sys.stderr)
        sys.exit(1)

    changed = 0
    for path in bib_files:
        if clean_file(path):
            print(f"  cleaned: {path.name}")
            changed += 1
        else:
            print(f"unchanged: {path.name}")

    print(f"\n{changed}/{len(bib_files)} file(s) modified.")


if __name__ == "__main__":
    main()
