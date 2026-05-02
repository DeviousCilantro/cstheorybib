#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import errno
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

try:
    import fcntl  # Unix-only; cron/server use-case is Unix-like.
except ImportError:  # pragma: no cover
    fcntl = None

DBLP_API = "https://dblp.org/search/publ/api"
DBLP_REC = "https://dblp.org/rec"

DEFAULT_VENUES = [
    {
        "id": "jsl",
        "label": "JSL",
        "stream": "journals/jsyml",
        "journal": "Journal of Symbolic Logic",
        "out": "jsl.bib",
    },
    {
        "id": "tcs",
        "label": "TCS",
        "stream": "journals/tcs",
        "journal": "Theoretical Computer Science",
        "out": "tcs.bib",
    },
    {
        "id": "combinatorica",
        "label": "COMB",
        "stream": "journals/combinatorica",
        "journal": "Combinatorica",
        "out": "combinatorica.bib",
    },
    {
        "id": "ipl",
        "label": "IPL",
        "stream": "journals/ipl",
        "journal": "Information Processing Letters",
        "out": "ipl.bib",
    },
    {
        "id": "jacm",
        "label": "JACM",
        "stream": "journals/jacm",
        "journal": "Journal of the {ACM}",
        "out": "jacm.bib",
    },
    {
        "id": "ecc",
        "label": "ECC",
        "stream": "journals/eccc",
        "journal": "Electronic Colloquium on Computational Complexity",
        "out": "ecc.bib",
    },
]

FIELD_ORDER = [
    "author",
    "title",
    "journal",
    "volume",
    "number",
    "pages",
    "year",
    "url",
    "doi",
    "timestamp",
    "biburl",
    "bibsource",
]


@dataclass(frozen=True)
class Venue:
    id: str
    label: str
    stream: str
    journal: str
    out: str


@dataclass
class BibEntry:
    venue: Venue
    dblp_key: str                 # e.g. journals/tcs/FooB24, without DBLP:
    original_bib_key: str         # e.g. DBLP:journals/tcs/FooB24
    entry_type: str               # article, inproceedings, ...
    raw_bib: str                  # official DBLP BibTeX entry
    fields: Dict[str, str]
    authors: List[str] = field(default_factory=list)
    year: str = ""
    author_label: str = ""
    base_key: str = ""
    custom_key: str = ""


class LockFile:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        if fcntl is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise SystemExit(f"another generator instance holds {self.path}")
                raise
        os.write(self.fd, f"pid={os.getpid()} started={_dt.datetime.now().isoformat()}\n".encode())
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fd is not None:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)


class Fetcher:
    def __init__(
        self,
        *,
        delay: float,
        jitter: float,
        cooldown: float,
        max_retries: int,
        contact: str,
        timeout: float,
    ) -> None:
        self.delay = delay
        self.jitter = jitter
        self.cooldown = cooldown
        self.max_retries = max_retries
        self.timeout = timeout
        self.last_request = 0.0
        contact_part = contact.strip() or "contact=unset"
        self.user_agent = f"theorybib-generator/0.2 ({contact_part}; polite DBLP API client)"

    def get(self, url: str, params: Mapping[str, str]) -> str:
        query = urllib.parse.urlencode(params)
        full_url = url + ("?" + query if query else "")
        req = urllib.request.Request(full_url, headers={"User-Agent": self.user_agent})
        last_error: Optional[BaseException] = None

        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    charset = resp.headers.get_content_charset() or "utf-8"
                    return raw.decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                last_error = exc
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                status = exc.code
                if status == 429:
                    delay = self._retry_after(exc) or (self.cooldown * (attempt + 1))
                    print(
                        f"HTTP 429 from DBLP; cooling down for {delay:.0f}s "
                        f"before retry {attempt + 1}/{self.max_retries}.",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                if status in (408, 500, 502, 503, 504) and attempt < self.max_retries:
                    delay = min(300.0, 10.0 * (2 ** attempt))
                    print(
                        f"HTTP {status}; retrying after {delay:.0f}s. "
                        f"sample={one_line(body, 120)!r}",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"HTTP {status} for {full_url}: {one_line(body, 500)}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    delay = min(300.0, 20.0 * (2 ** attempt))
                    print(
                        f"network error {exc}; retrying after {delay:.0f}s "
                        f"({attempt + 1}/{self.max_retries}).",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                break

        raise RuntimeError(f"request failed after retries: {full_url}: {last_error}")

    def _retry_after(self, exc: urllib.error.HTTPError) -> Optional[float]:
        raw = exc.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        if raw.isdigit():
            return float(raw)
        return None

    def _throttle(self) -> None:
        target = self.delay + (random.random() * self.jitter if self.jitter > 0 else 0.0)
        elapsed = time.monotonic() - self.last_request
        if elapsed < target:
            time.sleep(target - elapsed)
        self.last_request = time.monotonic()


class Cache:
    def __init__(self, root: Path, policy: str):
        self.root = root
        self.policy = policy

    def path(self, kind: str, venue: Venue, offset: int) -> Path:
        return self.root / kind / venue.id / f"{offset:07d}.{kind}"

    def get_or_fetch(
        self,
        *,
        kind: str,
        venue: Venue,
        offset: int,
        fetch: callable,
    ) -> str:
        path = self.path(kind, venue, offset)
        if self.policy in {"reuse", "offline"} and path.exists():
            return path.read_text(encoding="utf-8")
        if self.policy == "offline":
            raise RuntimeError(f"missing cache file in offline mode: {path}")
        text = fetch()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return text


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    bib_dir = root / args.bib_dir
    meta_dir = root / args.meta_dir
    cache = Cache(root / args.cache_dir, args.cache_policy)

    if args.write_default_config:
        write_default_config(root / args.config)
        return 0

    venues = load_venues(root / args.config)
    if args.only:
        selected = {x.lower() for x in args.only}
        venues = [v for v in venues if v.id.lower() in selected or v.label.lower() in selected]
        if not venues:
            raise SystemExit(f"--only selected no venues: {sorted(selected)}")

    fetcher = Fetcher(
        delay=args.delay,
        jitter=args.jitter,
        cooldown=args.cooldown,
        max_retries=args.max_retries,
        contact=args.contact,
        timeout=args.timeout,
    )

    bib_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    with LockFile(meta_dir / "generate.lock"):
        all_entries: List[BibEntry] = []
        for venue in venues:
            entries = fetch_venue(
                venue=venue,
                fetcher=fetcher,
                cache=cache,
                page_size=min(max(args.page_size, 1), 1000),
                bib_format=args.bib_format,
                individual_fallback=args.individual_fallback,
                json_only=args.json_only,
            )
            all_entries.extend(entries)

        keymap_path = meta_dir / "keymap.tsv"
        existing_keymap = read_keymap(keymap_path)
        assign_custom_keys(all_entries, existing_keymap)

        if args.dry_run:
            print(f"dry run: fetched {len(all_entries)} entries across {len(venues)} venues")
            return 0

        write_outputs(
            entries=all_entries,
            venues=venues,
            bib_dir=bib_dir,
            meta_dir=meta_dir,
            keymap_path=keymap_path,
            generator_args=args,
        )

        if args.git_commit or args.git_push:
            git_commit_and_maybe_push(root, args.git_push)

    return 0


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate CryptoBib-style BibTeX files for theory venues from DBLP."
    )
    p.add_argument("--root", default=".", help="repository root; default: current directory")
    p.add_argument("--bib-dir", default="bib", help="output directory relative to root")
    p.add_argument("--meta-dir", default="meta", help="metadata directory relative to root")
    p.add_argument("--cache-dir", default=".cache/dblp", help="cache directory relative to root")
    p.add_argument("--config", default="venues.json", help="venue config JSON relative to root")
    p.add_argument("--write-default-config", action="store_true", help="write default venues.json and exit")
    p.add_argument("--only", action="append", default=[], help="only generate this venue id/label; repeatable")
    p.add_argument("--contact", default=os.environ.get("THEORYBIB_CONTACT", ""), help="contact string for User-Agent")
    p.add_argument("--delay", type=float, default=8.0, help="minimum seconds between DBLP requests")
    p.add_argument("--jitter", type=float, default=2.0, help="extra random seconds added to each delay")
    p.add_argument("--cooldown", type=float, default=300.0, help="seconds to wait after a 429 without Retry-After")
    p.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout in seconds")
    p.add_argument("--max-retries", type=int, default=12, help="maximum retries per HTTP request")
    p.add_argument("--page-size", type=int, default=1000, help="DBLP h parameter; capped at 1000")
    p.add_argument("--bib-format", default="bib1", choices=["bib0", "bib1", "bib2"], help="DBLP BibTeX format")
    p.add_argument(
        "--cache-policy",
        default="refresh",
        choices=["refresh", "reuse", "offline"],
        help="refresh=fetch network and overwrite cache; reuse=use existing cache if present; offline=cache only",
    )
    p.add_argument("--individual-fallback", action="store_true", help="fall back to /rec/<key>.bib if a bulk page fails validation")
    p.add_argument(
        "--json-only",
        action="store_true",
        help="do not fetch DBLP BibTeX; synthesize BibTeX from DBLP JSON metadata. Fewer requests, less DBLP-exact formatting.",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--git-commit", action="store_true", help="commit changed bib/meta files after generation")
    p.add_argument("--git-push", action="store_true", help="commit and push changed bib/meta files after generation")
    return p.parse_args(argv)


def write_default_config(path: Path) -> None:
    if path.exists():
        raise SystemExit(f"refusing to overwrite existing {path}")
    path.write_text(json.dumps(DEFAULT_VENUES, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")


def load_venues(path: Path) -> List[Venue]:
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = DEFAULT_VENUES
    venues: List[Venue] = []
    seen = set()
    for item in raw:
        stream = normalize_stream(str(item["stream"]))
        venue = Venue(
            id=str(item["id"]).strip(),
            label=str(item["label"]).strip(),
            stream=stream,
            journal=str(item.get("journal", item["label"])).strip(),
            out=str(item.get("out", f"{item['id']}.bib")).strip(),
        )
        if not venue.id or not venue.label or not venue.stream:
            raise ValueError(f"bad venue config item: {item!r}")
        if venue.id in seen:
            raise ValueError(f"duplicate venue id: {venue.id}")
        seen.add(venue.id)
        venues.append(venue)
    return venues


def normalize_stream(stream: str) -> str:
    stream = stream.strip().rstrip(":")
    if stream.startswith("streams/"):
        stream = stream[len("streams/") :]
    return stream


def fetch_venue(
    *,
    venue: Venue,
    fetcher: Fetcher,
    cache: Cache,
    page_size: int,
    bib_format: str,
    individual_fallback: bool,
    json_only: bool,
) -> List[BibEntry]:
    query = f"streamid:{venue.stream}:"
    entries: List[BibEntry] = []
    seen: set[str] = set()
    offset = 0
    total: Optional[int] = None

    print(f"\n== {venue.label} [{venue.stream}] ==", file=sys.stderr)

    while True:
        json_text = cache.get_or_fetch(
            kind="json",
            venue=venue,
            offset=offset,
            fetch=lambda: fetcher.get(
                DBLP_API,
                {
                    "q": query,
                    "format": "json",
                    "h": str(page_size),
                    "f": str(offset),
                    "c": "0",
                },
            ),
        )
        page = parse_json_page(json_text)
        if total is None:
            total = page["total"]
            print(f"DBLP reports {total} records", file=sys.stderr)
        sent = page["sent"]
        first = page["first"]
        keys = page["keys"]
        infos = page["infos"]
        print(f"[{venue.id}] page first={first} sent={sent} progress={first + sent}/{page['total']}", file=sys.stderr)

        if sent == 0 or not keys:
            break

        if json_only:
            page_entries = [entry_from_json_info(venue, info) for info in infos]
        else:
            try:
                bib_text = cache.get_or_fetch(
                    kind="bib",
                    venue=venue,
                    offset=first,
                    fetch=lambda: fetcher.get(
                        DBLP_API,
                        {
                            "q": query,
                            "format": bib_format,
                            "h": str(page_size),
                            "f": str(first),
                            "c": "0",
                            "rd": "1a",
                        },
                    ),
                )
                page_entries = parse_official_bib_page(venue, bib_text)
                validate_page_keys(keys, page_entries)
            except Exception as exc:
                print(f"bulk BibTeX page failed for {venue.id}@{first}: {exc}", file=sys.stderr)
                if not individual_fallback:
                    print("using DBLP JSON metadata for this page instead", file=sys.stderr)
                    page_entries = [entry_from_json_info(venue, info) for info in infos]
                else:
                    print("falling back to individual /rec/<key>.bib requests", file=sys.stderr)
                    page_entries = []
                    for idx, key in enumerate(keys, 1):
                        text = fetcher.get(f"{DBLP_REC}/{key}.bib", {})
                        parsed = parse_official_bib_page(venue, text)
                        if len(parsed) != 1:
                            raise RuntimeError(f"expected one BibTeX entry for {key}, got {len(parsed)}")
                        page_entries.append(parsed[0])
                        if idx % 100 == 0:
                            print(f"  individual fallback {idx}/{len(keys)}", file=sys.stderr)

        for entry in page_entries:
            if entry.dblp_key in seen:
                print(f"warning: duplicate DBLP key in {venue.id}: {entry.dblp_key}", file=sys.stderr)
                continue
            seen.add(entry.dblp_key)
            derive_key_material(entry)
            entries.append(entry)

        offset = first + sent
        if offset >= page["total"]:
            break

    if total is not None and len(entries) != total:
        print(f"warning: collected {len(entries)} entries but DBLP reported {total} for {venue.id}", file=sys.stderr)

    print(f"collected {len(entries)} entries for {venue.id}", file=sys.stderr)
    return entries


def parse_json_page(text: str) -> Dict[str, object]:
    data = json.loads(text)
    hits = data.get("result", {}).get("hits", {})
    total = int(hits.get("@total", 0))
    sent = int(hits.get("@sent", 0))
    first = int(hits.get("@first", 0))
    raw_hit = hits.get("hit", [])
    if isinstance(raw_hit, dict):
        hit_list = [raw_hit]
    elif isinstance(raw_hit, list):
        hit_list = raw_hit
    else:
        hit_list = []
    infos = []
    keys = []
    for hit in hit_list:
        info = hit.get("info", {})
        key = info.get("key")
        if key:
            keys.append(str(key))
            infos.append(info)
    return {"total": total, "sent": sent, "first": first, "keys": keys, "infos": infos}


def parse_official_bib_page(venue: Venue, bib_text: str) -> List[BibEntry]:
    entries = []
    for raw in split_bib_entries(bib_text):
        parsed = parse_bib_entry(raw)
        if parsed is None:
            continue
        entry_type, bib_key, fields = parsed
        dblp_key = bib_key[len("DBLP:") :] if bib_key.startswith("DBLP:") else bib_key
        entry = BibEntry(
            venue=venue,
            dblp_key=dblp_key,
            original_bib_key=bib_key,
            entry_type=entry_type,
            raw_bib=raw.strip(),
            fields=fields,
        )
        entries.append(entry)
    return entries


def validate_page_keys(expected_keys: Sequence[str], entries: Sequence[BibEntry]) -> None:
    expected = set(expected_keys)
    actual = {e.dblp_key for e in entries}
    if expected != actual:
        missing = sorted(expected - actual)[:10]
        extra = sorted(actual - expected)[:10]
        raise RuntimeError(f"BibTeX page key mismatch; missing={missing}, extra={extra}")


def entry_from_json_info(venue: Venue, info: Mapping[str, object]) -> BibEntry:
    dblp_key = str(info["key"])
    fields: Dict[str, str] = {}
    authors = authors_from_json(info)
    if authors:
        fields["author"] = " and ".join(authors)
    if info.get("title"):
        fields["title"] = str(info["title"]).rstrip(".")
    fields["journal"] = venue.journal
    for src, dst in [("volume", "volume"), ("number", "number"), ("pages", "pages"), ("year", "year"), ("doi", "doi")]:
        if info.get(src):
            fields[dst] = str(info[src])
    ee = info.get("ee")
    if isinstance(ee, list):
        ee = ee[0] if ee else None
    if ee:
        fields["url"] = str(ee)
    elif info.get("url"):
        fields["url"] = "https://dblp.org/" + str(info["url"]).lstrip("/")
    fields["biburl"] = f"https://dblp.org/rec/{dblp_key}.bib"
    fields["bibsource"] = "dblp computer science bibliography, https://dblp.org"
    raw = format_bib_entry("article", f"DBLP:{dblp_key}", fields)
    return BibEntry(
        venue=venue,
        dblp_key=dblp_key,
        original_bib_key=f"DBLP:{dblp_key}",
        entry_type="article",
        raw_bib=raw,
        fields=fields,
    )


def authors_from_json(info: Mapping[str, object]) -> List[str]:
    authors_obj = info.get("authors")
    if not isinstance(authors_obj, Mapping):
        return []
    author_obj = authors_obj.get("author")
    if isinstance(author_obj, list):
        return [author_text(x) for x in author_obj if author_text(x)]
    text = author_text(author_obj)
    return [text] if text else []


def author_text(obj: object) -> str:
    if isinstance(obj, str):
        return html.unescape(obj).strip()
    if isinstance(obj, Mapping):
        text = obj.get("text")
        if text:
            return html.unescape(str(text)).strip()
    return ""


def split_bib_entries(text: str) -> List[str]:
    entries: List[str] = []
    n = len(text)
    i = 0
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
            ch = text[j]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quote = not in_quote
            elif not in_quote:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        entries.append(text[at : j + 1].strip())
                        i = j + 1
                        break
            j += 1
        else:
            break
    return [e for e in entries if re.match(r"@\w+\s*\{", e)]


def parse_bib_entry(raw: str) -> Optional[Tuple[str, str, Dict[str, str]]]:
    m = re.match(r"@([A-Za-z]+)\s*\{\s*([^,]+)\s*,", raw, flags=re.S)
    if not m:
        return None
    entry_type = m.group(1)
    key = m.group(2).strip()
    pos = m.end()
    end = raw.rfind("}")
    if end <= pos:
        return entry_type, key, {}
    body = raw[pos:end]
    fields: Dict[str, str] = {}
    idx = 0
    while idx < len(body):
        while idx < len(body) and body[idx] in " \t\r\n,":
            idx += 1
        name_start = idx
        while idx < len(body) and re.match(r"[A-Za-z0-9_:-]", body[idx]):
            idx += 1
        if idx == name_start:
            break
        name = body[name_start:idx].lower()
        while idx < len(body) and body[idx].isspace():
            idx += 1
        if idx >= len(body) or body[idx] != "=":
            break
        idx += 1
        while idx < len(body) and body[idx].isspace():
            idx += 1
        value, idx = parse_bib_value(body, idx)
        fields[name] = value.strip()
        while idx < len(body) and body[idx] not in ",":
            # Handles concatenated macros conservatively by keeping the raw suffix.
            idx += 1
        if idx < len(body) and body[idx] == ",":
            idx += 1
    return entry_type, key, fields


def parse_bib_value(s: str, idx: int) -> Tuple[str, int]:
    if idx >= len(s):
        return "", idx
    ch = s[idx]
    if ch == "{":
        depth = 1
        idx += 1
        start = idx
        escaped = False
        while idx < len(s):
            c = s[idx]
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == "{":
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


def derive_key_material(entry: BibEntry) -> None:
    author_field = entry.fields.get("author", "")
    authors = split_authors(author_field)
    entry.authors = authors
    entry.year = normalize_year(entry.fields.get("year", ""))
    entry.author_label = author_label(authors)
    yy = entry.year[-2:] if re.fullmatch(r"\d{4}", entry.year) else "00"
    entry.base_key = f"{entry.venue.label}:{entry.author_label}{yy}"


def split_authors(author_field: str) -> List[str]:
    if not author_field:
        return []
    parts: List[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(author_field):
        ch = author_field[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0 and author_field.startswith(" and ", i):
            parts.append(author_field[start:i].strip())
            i += 5
            start = i
            continue
        i += 1
    parts.append(author_field[start:].strip())
    return [p for p in parts if p]


def author_label(authors: Sequence[str]) -> str:
    surnames = [sanitize_name(last_name(a)) for a in authors if a and a.lower() != "others"]
    surnames = [s for s in surnames if s]
    if not surnames:
        return "Anon"
    if len(surnames) == 1:
        return camel(surnames[0])
    if len(surnames) <= 3:
        return "".join(camel(s[:3]) for s in surnames)
    return "".join(s[0].upper() for s in surnames[:6] if s)


def last_name(author: str) -> str:
    clean = latexish_to_ascii(author)
    clean = re.sub(r"\s+\d{4}$", "", clean).strip()  # DBLP disambiguation suffix
    clean = clean.replace("~", " ")
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        return "Anon"
    if "," in clean:
        return clean.split(",", 1)[0].strip()
    tokens = clean.split()
    if not tokens:
        return "Anon"
    return tokens[-1]


def latexish_to_ascii(s: str) -> str:
    # Remove common LaTeX accent wrappers while preserving the base letters.
    s = html.unescape(s)
    replacements = {
        r"\ss": "ss",
        r"\ae": "ae",
        r"\AE": "AE",
        r"\oe": "oe",
        r"\OE": "OE",
        r"\o": "o",
        r"\O": "O",
        r"\aa": "a",
        r"\AA": "A",
        r"\l": "l",
        r"\L": "L",
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    # \c{c}, \"{o}, \'e, etc.
    s = re.sub(r"\\[A-Za-z]+\s*\{([^{}]*)\}", r"\1", s)
    s = re.sub(r"\\['`\"^~=\.uvHctbdkr]\s*\{?([A-Za-z])\}?", r"\1", s)
    s = re.sub(r"\\[A-Za-z]+", "", s)
    s = s.replace("{", "").replace("}", "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s


def sanitize_name(s: str) -> str:
    s = latexish_to_ascii(s)
    s = re.sub(r"[^A-Za-z0-9]+", "", s)
    return s or "Anon"


def camel(s: str) -> str:
    if not s:
        return ""
    return s[0].upper() + s[1:]


def normalize_year(year: str) -> str:
    m = re.search(r"(\d{4})", year or "")
    return m.group(1) if m else ""


def read_keymap(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    result: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2 or parts[0] == "dblp_key":
            continue
        dblp_key, custom_key = parts[0], parts[1]
        if dblp_key and custom_key:
            result[dblp_key] = custom_key
    return result


def assign_custom_keys(entries: Sequence[BibEntry], old: Mapping[str, str]) -> None:
    used: Dict[str, str] = {}
    unmapped: List[BibEntry] = []

    # Keep old keys whenever possible. This is essential for unattended updates:
    # adding a new colliding record must not rewrite citations in old papers.
    for entry in sorted(entries, key=lambda e: e.dblp_key):
        old_key = old.get(entry.dblp_key)
        if old_key and old_key not in used:
            entry.custom_key = old_key
            used[old_key] = entry.dblp_key
        else:
            unmapped.append(entry)

    groups: Dict[str, List[BibEntry]] = {}
    for entry in unmapped:
        groups.setdefault(entry.base_key, []).append(entry)

    for base in sorted(groups):
        group = sorted(groups[base], key=lambda e: (e.year, e.author_label, e.dblp_key))
        clean_collision = len(group) > 1 and base not in used and not any(
            k.startswith(base) for k in used
        )
        for idx, entry in enumerate(group):
            if len(group) == 1 and base not in used:
                key = base
            elif clean_collision:
                key = base + suffix(idx)
            else:
                key = first_free_key(base, used)
            entry.custom_key = key
            used[key] = entry.dblp_key


def suffix(i: int) -> str:
    # 0 -> a, 25 -> z, 26 -> aa, etc.
    letters = []
    i += 1
    while i > 0:
        i -= 1
        letters.append(chr(ord("a") + (i % 26)))
        i //= 26
    return "".join(reversed(letters))


def first_free_key(base: str, used: Mapping[str, str]) -> str:
    if base not in used:
        return base
    i = 0
    while True:
        key = base + suffix(i)
        if key not in used:
            return key
        i += 1


def rewrite_bib_key(raw: str, new_key: str) -> str:
    return re.sub(r"^(@[A-Za-z]+\s*\{\s*)[^,]+", r"\1" + new_key, raw.strip(), count=1)


def format_bib_entry(entry_type: str, key: str, fields: Mapping[str, str]) -> str:
    lines = [f"@{entry_type}{{{key},"]
    written = set()
    for name in FIELD_ORDER:
        if name in fields and fields[name] not in (None, ""):
            lines.append(f"  {name:<10} = {{{fields[name]}}},")
            written.add(name)
    for name in sorted(fields):
        if name not in written and fields[name] not in (None, ""):
            lines.append(f"  {name:<10} = {{{fields[name]}}},")
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


def write_outputs(
    *,
    entries: Sequence[BibEntry],
    venues: Sequence[Venue],
    bib_dir: Path,
    meta_dir: Path,
    keymap_path: Path,
    generator_args: argparse.Namespace,
) -> None:
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    tmp = Path(tempfile.mkdtemp(prefix="theorybib-", dir=str(bib_dir.parent)))
    try:
        tmp_bib = tmp / "bib"
        tmp_meta = tmp / "meta"
        tmp_bib.mkdir()
        tmp_meta.mkdir()

        by_venue: Dict[str, List[BibEntry]] = {v.id: [] for v in venues}
        for entry in entries:
            by_venue.setdefault(entry.venue.id, []).append(entry)

        for venue in venues:
            out_path = tmp_bib / venue.out
            venue_entries = sorted(by_venue.get(venue.id, []), key=lambda e: e.custom_key)
            out_path.write_text(bib_file_text(venue, venue_entries, now), encoding="utf-8")
            print(f"wrote {len(venue_entries)} entries -> {bib_dir / venue.out}", file=sys.stderr)

        all_entries = sorted(entries, key=lambda e: e.custom_key)
        (tmp_bib / "all.bib").write_text(bib_file_text(None, all_entries, now), encoding="utf-8")

        write_keymap(tmp_meta / "keymap.tsv", all_entries)
        write_manifest(tmp_meta / "manifest.json", venues, all_entries, generator_args, now)

        # Atomic-ish replacement per file. Preserve unrelated files in bib/ and meta/.
        bib_dir.mkdir(parents=True, exist_ok=True)
        meta_dir.mkdir(parents=True, exist_ok=True)
        for path in tmp_bib.iterdir():
            shutil.move(str(path), str(bib_dir / path.name))
        for path in tmp_meta.iterdir():
            shutil.move(str(path), str(meta_dir / path.name))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def bib_file_text(venue: Optional[Venue], entries: Sequence[BibEntry], now: str) -> str:
    if venue is None:
        title = "combined theory bibliography"
        label = "ALL"
    else:
        title = f"{venue.journal} bibliography"
        label = venue.label
    header = f"""% File generated by gen.py -- DO NOT EDIT MANUALLY.
% Generated at: {now}
% Source: DBLP publication search API and DBLP BibTeX export.
% Bibliography: {title}
%
% Labeling convention, following the CryptoBib style:
%   venue-label ':' author-label two-digit-year [collision-letter]
%   one author: full last name, e.g. {label}:Shamir79
%   two/three authors: first three letters of each last name, e.g. {label}:AliBob98
%   four or more authors: first letters of up to six last names, e.g. {label}:ABCDEG98
% Existing keys are preserved via meta/keymap.tsv.

"""
    body = []
    for entry in entries:
        body.append(rewrite_bib_key(entry.raw_bib, entry.custom_key))
    return header + "\n\n".join(body).rstrip() + "\n"


def write_keymap(path: Path, entries: Sequence[BibEntry]) -> None:
    lines = ["dblp_key\tcustom_key\tvenue\tyear\tauthor_label\tbase_key\toriginal_bib_key"]
    for e in sorted(entries, key=lambda x: (x.venue.id, x.custom_key, x.dblp_key)):
        lines.append(
            "\t".join(
                [
                    e.dblp_key,
                    e.custom_key,
                    e.venue.id,
                    e.year,
                    e.author_label,
                    e.base_key,
                    e.original_bib_key,
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest(
    path: Path,
    venues: Sequence[Venue],
    entries: Sequence[BibEntry],
    args: argparse.Namespace,
    now: str,
) -> None:
    counts = {v.id: 0 for v in venues}
    for e in entries:
        counts[e.venue.id] = counts.get(e.venue.id, 0) + 1
    manifest = {
        "generated_at": now,
        "source": "dblp",
        "json_only": bool(args.json_only),
        "counts": counts,
        "venues": [v.__dict__ for v in venues],
    }
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git_commit_and_maybe_push(root: Path, push: bool) -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain", "bib", "meta"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if status.returncode != 0:
        print(status.stderr, file=sys.stderr)
        raise SystemExit(status.returncode)
    if not status.stdout.strip():
        print("no bib/meta changes to commit", file=sys.stderr)
        return
    subprocess.run(["git", "add", "bib", "meta"], cwd=root, check=True)
    date = _dt.date.today().isoformat()
    subprocess.run(["git", "commit", "-m", f"Update DBLP bibliography {date}"], cwd=root, check=True)
    if push:
        subprocess.run(["git", "push"], cwd=root, check=True)


def one_line(s: str, limit: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
