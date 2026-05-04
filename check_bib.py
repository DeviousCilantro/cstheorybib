#!/usr/bin/env python3
"""Quality checker for the csTheoryBib .bib files.

Parses every .bib file under bib/ and reports problems grouped by severity:

  ERROR   — entry is definitely broken or will cause citation failures
  WARNING — entry is likely wrong or inconsistent
  INFO    — minor style issues worth knowing about

Exit code is 0 if no ERRORs were found, 1 otherwise.

Usage::

    python3 check_bib.py [--only <venue-id>] [--errors-only] [--summary]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

BIB_DIR = Path(__file__).parent / "bib"

REQUIRED_FIELDS = {"author", "title", "journal", "year"}

# Full canonical journal names expected in this repo.
CANONICAL_JOURNALS = {
    "Combinatorica",
    "Electronic Colloquium on Computational Complexity",
    "Information Processing Letters",
    "Journal of Symbolic Logic",
    "Journal of the {ACM}",
    "Theoretical Computer Science",
}

# DBLP abbreviated forms that should have been expanded.
ABBREVIATED_JOURNALS = re.compile(
    r"^(Comb\.|J\. ACM|Theor\. Comput\.|Inf\. Process\. Lett\.|"
    r"J\. Symb\. Log\.|Electron\. Colloquium Comput\. Complex\.)$"
)

DISAMBIG_RE = re.compile(r"\b0\d{3,}\b")

CURRENT_YEAR = 2026


class Severity(Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass
class Issue:
    """A single quality issue attached to one bib entry.

    Attributes:
        severity: Classification of how serious the problem is.
        code: Short mnemonic tag (e.g. ``"MISSING_AUTHOR"``).
        message: Human-readable description.
        entry_key: BibTeX citation key of the offending entry.
        file_name: Name of the .bib file (without directory).
        line_no: Approximate line number of the entry in the file.
    """

    severity: Severity
    code: str
    message: str
    entry_key: str
    file_name: str
    line_no: int


@dataclass
class BibEntry:
    """Minimal parsed representation of a single BibTeX entry.

    Attributes:
        key: Citation key.
        entry_type: BibTeX type in lowercase (e.g. ``"article"``).
        fields: Lower-cased field name -> raw value mapping.
        raw: Full raw text of the entry.
        line_no: Line number in the source file where the entry begins.
    """

    key: str
    entry_type: str
    fields: Dict[str, str]
    raw: str
    line_no: int


def _parse_value(s: str, idx: int) -> Tuple[str, int]:
    """Parse one BibTeX field value starting at *idx* in *s*.

    Args:
        s: Body text of an entry (everything after the key + comma).
        idx: Starting position.

    Returns:
        ``(value_text, new_idx)`` pair.
    """
    if idx >= len(s):
        return "", idx
    ch = s[idx]
    if ch == "{":
        depth, idx = 1, idx + 1
        start = idx
        while idx < len(s):
            c = s[idx]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start:idx], idx + 1
            idx += 1
        return s[start:], idx
    if ch == '"':
        idx += 1
        start = idx
        escaped = False
        while idx < len(s):
            c = s[idx]
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                return s[start:idx], idx + 1
            idx += 1
        return s[start:], idx
    start = idx
    while idx < len(s) and s[idx] not in ",\r\n":
        idx += 1
    return s[start:idx].strip(), idx


def _parse_entry(raw: str, line_no: int) -> Optional[BibEntry]:
    """Parse *raw* as a single BibTeX entry.

    Args:
        raw: Raw entry text starting with ``@``.
        line_no: Line number in the source file.

    Returns:
        A :class:`BibEntry`, or None if *raw* is malformed.
    """
    m = re.match(r"@([A-Za-z]+)\s*\{\s*([^,\s]+)\s*,", raw, re.S)
    if not m:
        return None
    entry_type = m.group(1).lower()
    key = m.group(2).strip()
    body = raw[m.end() : raw.rfind("}")]
    fields: Dict[str, str] = {}
    idx = 0
    while idx < len(body):
        while idx < len(body) and body[idx] in " \t\r\n,":
            idx += 1
        ns = idx
        while idx < len(body) and re.match(r"[A-Za-z0-9_:-]", body[idx]):
            idx += 1
        if idx == ns:
            break
        name = body[ns:idx].lower()
        while idx < len(body) and body[idx].isspace():
            idx += 1
        if idx >= len(body) or body[idx] != "=":
            break
        idx += 1
        while idx < len(body) and body[idx].isspace():
            idx += 1
        value, idx = _parse_value(body, idx)
        fields[name] = value.strip()
        while idx < len(body) and body[idx] not in ",":
            idx += 1
        if idx < len(body):
            idx += 1
    return BibEntry(
        key=key, entry_type=entry_type, fields=fields, raw=raw, line_no=line_no
    )


def parse_bib_file(path: Path) -> Tuple[List[BibEntry], List[Issue]]:
    """Parse all entries from *path*.

    Args:
        path: Path to a .bib file.

    Returns:
        ``(entries, parse_issues)`` — entries successfully parsed, plus any
        issues encountered during parsing itself.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    # Build a line-number index: character offset -> line number
    offset_to_line: List[int] = []
    for lineno, line in enumerate(lines, 1):
        offset_to_line.extend([lineno] * (len(line) + 1))

    entries: List[BibEntry] = []
    parse_issues: List[Issue] = []

    i = 0
    n = len(text)
    while i < n:
        at = text.find("@", i)
        if at < 0:
            break
        brace = text.find("{", at)
        if brace < 0:
            break
        depth = 0
        j = brace
        in_quote = False
        escaped = False
        while j < n:
            c = text[j]
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_quote = not in_quote
            elif not in_quote:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        raw = text[at : j + 1]
                        line_no = offset_to_line[at] if at < len(offset_to_line) else 0
                        if re.match(r"@\w+\s*\{", raw):
                            entry = _parse_entry(raw, line_no)
                            if entry is None:
                                parse_issues.append(
                                    Issue(
                                        severity=Severity.ERROR,
                                        code="PARSE_FAILURE",
                                        message="Could not parse entry",
                                        entry_key="(unknown)",
                                        file_name=path.name,
                                        line_no=line_no,
                                    )
                                )
                            else:
                                entries.append(entry)
                        i = j + 1
                        break
            j += 1
        else:
            break

    return entries, parse_issues


def _check_entry(entry: BibEntry, file_name: str) -> List[Issue]:
    """Run all quality checks on a single entry.

    Args:
        entry: The parsed entry to inspect.
        file_name: Name of the source file (for issue reporting).

    Returns:
        List of :class:`Issue` objects found.
    """
    issues: List[Issue] = []

    def add(severity: Severity, code: str, message: str) -> None:
        issues.append(
            Issue(
                severity=severity,
                code=code,
                message=message,
                entry_key=entry.key,
                file_name=file_name,
                line_no=entry.line_no,
            )
        )

    f = entry.fields

    # --- required fields ---
    for fld in REQUIRED_FIELDS:
        if fld not in f or not f[fld].strip():
            add(Severity.ERROR, "MISSING_FIELD", f"Missing required field: {fld}")

    # --- author checks ---
    author = f.get("author", "")
    if author:
        if DISAMBIG_RE.search(author):
            add(
                Severity.ERROR,
                "DISAMBIG_NUMBER",
                f"DBLP disambiguation number not removed from author: {author[:80]}",
            )
        if author.strip().lower() in {"others", "{others}"}:
            add(Severity.WARNING, "ANON_AUTHOR", "Author field is 'others'")
        # check for HTML entities that weren't unescaped
        if "&amp;" in author or "&#" in author:
            add(
                Severity.WARNING, "HTML_ENTITY", "Unescaped HTML entity in author field"
            )

    # --- title checks ---
    title = f.get("title", "")
    if title:
        if "&amp;" in title or "&#" in title:
            add(Severity.WARNING, "HTML_ENTITY", "Unescaped HTML entity in title field")

    # --- journal checks ---
    journal = f.get("journal", "")
    if journal:
        if ABBREVIATED_JOURNALS.match(journal):
            add(
                Severity.ERROR,
                "ABBREVIATED_JOURNAL",
                f"Journal name is still abbreviated: {journal!r}",
            )
        elif journal not in CANONICAL_JOURNALS:
            add(
                Severity.WARNING,
                "UNKNOWN_JOURNAL",
                f"Journal name not in canonical list: {journal!r}",
            )

    # --- year checks ---
    year_raw = f.get("year", "")
    if year_raw:
        m = re.fullmatch(r"\d{4}", year_raw)
        if not m:
            add(
                Severity.ERROR,
                "BAD_YEAR",
                f"Year is not a 4-digit number: {year_raw!r}",
            )
        else:
            year = int(year_raw)
            if year < 1930:
                add(Severity.WARNING, "OLD_YEAR", f"Suspiciously old year: {year}")
            elif year > CURRENT_YEAR:
                add(Severity.WARNING, "FUTURE_YEAR", f"Year is in the future: {year}")

    # --- url checks ---
    url = f.get("url", "")
    if url:
        if not url.startswith("http://") and not url.startswith("https://"):
            add(
                Severity.WARNING,
                "BAD_URL",
                f"URL does not start with http(s)://: {url[:80]}",
            )
        if " " in url:
            add(Severity.ERROR, "URL_SPACE", "URL contains a space")

    # --- doi checks ---
    doi = f.get("doi", "")
    if doi:
        # DOI should not be a full URL
        if doi.startswith("http"):
            add(
                Severity.WARNING,
                "DOI_IS_URL",
                f"DOI field contains a URL instead of just the DOI token: {doi[:80]}",
            )
        # URL and DOI should agree when URL is a doi.org link
        if url and "doi.org/" in url:
            url_doi = url.split("doi.org/", 1)[1]
            if url_doi.lower() != doi.lower():
                add(
                    Severity.WARNING,
                    "DOI_URL_MISMATCH",
                    f"DOI field {doi!r} does not match doi.org URL {url!r}",
                )

    # --- residual DBLP noise ---
    for noise_field in ("biburl", "bibsource", "timestamp"):
        if noise_field in f:
            add(
                Severity.ERROR,
                "DBLP_NOISE_FIELD",
                f"DBLP noise field should have been removed: {noise_field}",
            )

    # --- empty fields ---
    for name, value in f.items():
        if not value.strip():
            add(Severity.WARNING, "EMPTY_FIELD", f"Field {name!r} is present but empty")

    return issues


def check_file(path: Path) -> List[Issue]:
    """Parse and check a single .bib file.

    Args:
        path: Path to the .bib file.

    Returns:
        All issues found, including parse errors and per-entry checks.
    """
    entries, parse_issues = parse_bib_file(path)
    issues = list(parse_issues)

    # duplicate key check within the file
    seen: Dict[str, int] = {}
    for entry in entries:
        if entry.key in seen:
            issues.append(
                Issue(
                    severity=Severity.ERROR,
                    code="DUPLICATE_KEY",
                    message=f"Key {entry.key!r} appears more than once (first at line {seen[entry.key]})",
                    entry_key=entry.key,
                    file_name=path.name,
                    line_no=entry.line_no,
                )
            )
        else:
            seen[entry.key] = entry.line_no

    for entry in entries:
        issues.extend(_check_entry(entry, path.name))

    return issues


_SEV_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
_SEV_COLOUR = {
    Severity.ERROR: "\033[31m",  # red
    Severity.WARNING: "\033[33m",  # yellow
    Severity.INFO: "\033[36m",  # cyan
}
_RESET = "\033[0m"


def _colourise(text: str, severity: Severity, use_colour: bool) -> str:
    if not use_colour:
        return text
    return _SEV_COLOUR[severity] + text + _RESET


def report(
    all_issues: List[Issue],
    *,
    errors_only: bool = False,
    use_colour: bool = True,
    summary_only: bool = False,
) -> None:
    """Print a structured quality report to stdout.

    Args:
        all_issues: All issues from all files.
        errors_only: When True, suppress WARNING and INFO lines.
        use_colour: When True, emit ANSI colour codes.
        summary_only: When True, print only the per-file summary table.
    """
    if errors_only:
        visible = [i for i in all_issues if i.severity == Severity.ERROR]
    else:
        visible = all_issues

    visible.sort(key=lambda i: (i.file_name, _SEV_ORDER[i.severity], i.line_no, i.code))

    counts: Dict[str, Dict[Severity, int]] = {}
    for issue in all_issues:
        counts.setdefault(issue.file_name, {s: 0 for s in Severity})
        counts[issue.file_name][issue.severity] += 1

    if not summary_only:
        current_file = ""
        for issue in visible:
            if issue.file_name != current_file:
                current_file = issue.file_name
                print(f"\n{'─' * 72}")
                print(f"  {current_file}")
                print(f"{'─' * 72}")
            sev_str = _colourise(
                f"[{issue.severity.value:<7}]", issue.severity, use_colour
            )
            print(
                f"  {sev_str}  line {issue.line_no:>5}  {issue.entry_key:<30}  "
                f"{issue.code}: {issue.message}"
            )

    # Summary table
    print(f"\n{'═' * 72}")
    print(f"  {'FILE':<30}  {'ERRORS':>7}  {'WARNINGS':>8}  {'INFO':>5}  {'TOTAL':>6}")
    print(f"{'─' * 72}")
    grand = {s: 0 for s in Severity}
    for fname in sorted(counts):
        c = counts[fname]
        total = sum(c.values())
        err_s = _colourise(
            f"{c[Severity.ERROR]:>7}",
            Severity.ERROR,
            use_colour and c[Severity.ERROR] > 0,
        )
        warn_s = _colourise(
            f"{c[Severity.WARNING]:>8}",
            Severity.WARNING,
            use_colour and c[Severity.WARNING] > 0,
        )
        print(f"  {fname:<30}  {err_s}  {warn_s}  {c[Severity.INFO]:>5}  {total:>6}")
        for s in Severity:
            grand[s] += c[s]
    print(f"{'─' * 72}")
    grand_total = sum(grand.values())
    err_s = _colourise(
        f"{grand[Severity.ERROR]:>7}",
        Severity.ERROR,
        use_colour and grand[Severity.ERROR] > 0,
    )
    warn_s = _colourise(
        f"{grand[Severity.WARNING]:>8}",
        Severity.WARNING,
        use_colour and grand[Severity.WARNING] > 0,
    )
    print(
        f"  {'TOTAL':<30}  {err_s}  {warn_s}  {grand[Severity.INFO]:>5}  {grand_total:>6}"
    )
    print(f"{'═' * 72}\n")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Build and parse the CLI argument spec.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]`` when None.

    Returns:
        Parsed namespace.
    """
    p = argparse.ArgumentParser(
        description="Quality-check .bib files and report issues."
    )
    p.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="VENUE",
        help="Check only this filename stem (repeatable); e.g. --only jacm",
    )
    p.add_argument(
        "--errors-only",
        action="store_true",
        help="Print only ERROR-level issues",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print only the summary table, not individual issue lines",
    )
    p.add_argument(
        "--no-colour",
        action="store_true",
        help="Disable ANSI colour output",
    )
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: run quality checks and report results.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]`` when None.

    Returns:
        0 if no errors were found, 1 otherwise.
    """
    args = parse_args(argv)

    bib_files = sorted(BIB_DIR.glob("*.bib"))
    if not bib_files:
        print(f"No .bib files found in {BIB_DIR}", file=sys.stderr)
        return 1

    if args.only:
        selected = {s.lower().removesuffix(".bib") for s in args.only}
        bib_files = [f for f in bib_files if f.stem.lower() in selected]
        if not bib_files:
            print(f"--only matched no files: {sorted(selected)}", file=sys.stderr)
            return 1

    all_issues: List[Issue] = []
    for path in bib_files:
        issues = check_file(path)
        all_issues.extend(issues)

    use_colour = not args.no_colour and sys.stdout.isatty()
    report(
        all_issues,
        errors_only=args.errors_only,
        use_colour=use_colour,
        summary_only=args.summary,
    )

    has_errors = any(i.severity == Severity.ERROR for i in all_issues)
    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
