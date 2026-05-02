# theorybib

CryptoBib-style BibTeX files for selected theory venues, generated from DBLP.

This repository uses a hybrid layout:

* `gen.py` is the Python orchestration wrapper. It is the entry point for humans and cron.
* `rust/theorybib-dblp/` is the Rust engine. It does the DBLP JSON pagination, key generation, cache handling, and output writing.
* `gen_python.py` is a pure-Python fallback. The wrapper runs it in JSON-only mode if Rust/Cargo is unavailable.

The Rust engine is the default because it avoids the fragile paginated bulk-BibTeX export path. Large streams such as TCS can make DBLP's BibTeX export endpoint disagree with the JSON page, whereas the DBLP publication search API explicitly supports JSON pagination via `h` and `f`.

## Generated files

```text
bib/jsl.bib
bib/tcs.bib
bib/combinatorica.bib
bib/ipl.bib
bib/jacm.bib
bib/ecc.bib
bib/all.bib
meta/keymap.tsv
meta/manifest.json
```

The default venue set is:

| File | Label | DBLP stream | Venue |
|---|---:|---|---|
| `bib/jsl.bib` | `JSL` | `journals/jsyml` | Journal of Symbolic Logic |
| `bib/tcs.bib` | `TCS` | `journals/tcs` | Theoretical Computer Science |
| `bib/combinatorica.bib` | `COMB` | `journals/combinatorica` | Combinatorica |
| `bib/ipl.bib` | `IPL` | `journals/ipl` | Information Processing Letters |
| `bib/jacm.bib` | `JACM` | `journals/jacm` | Journal of the ACM |
| `bib/ecc.bib` | `ECC` | `journals/eccc` | Electronic Colloquium on Computational Complexity |

`ECC` is currently mapped to DBLP's ECCC stream. Edit `venues.json` if you mean another stream.

## Key convention

Keys follow the CryptoBib convention:

```text
venue-label ':' author-label two-digit-year [collision-letter]
```

Author labels are generated as follows.

* One author: full last name. Example: `JSL:Shamir79`.
* Two or three authors: first three letters of each author's last name. Example: `JSL:AliBob98` or `TCS:AbdMinNam05`.
* Four or more authors: first letter of each author's last name, up to six authors. Example: `JSL:ABCDEG98`.
* Collisions: append `a`, `b`, `c`, ... as necessary.

`meta/keymap.tsv` preserves existing citation keys across future updates. This is essential for unattended runs: when DBLP adds a newly colliding record, old citations are not renamed.

## Requirements

For the default fast path:

```text
Rust toolchain with Cargo
Python 3.9+
```

For the fallback path:

```text
Python 3.9+
```

The first Rust run will compile the engine. Subsequent runs reuse `target/release/theorybib-dblp`.

## Generate

From the repository root:

```bash
python3 gen.py --contact "your.email@example.edu"
```

The wrapper chooses Rust automatically when Cargo is available. To force the Rust engine:

```bash
python3 gen.py --engine rust --contact "your.email@example.edu"
```

To force the Python fallback:

```bash
python3 gen.py --engine python --contact "your.email@example.edu"
```

For a local test on one venue:

```bash
python3 gen.py --only jsl --contact "your.email@example.edu"
```

For a conservative server run:

```bash
python3 gen.py \
  --engine rust \
  --contact "your.email@example.edu" \
  --delay 10 \
  --jitter 3 \
  --cooldown 900 \
  --max-retries 12
```

## The TCS bulk-BibTeX mismatch

If the old Python bulk-BibTeX path fails with a message like this:

```text
RuntimeError: BibTeX page key mismatch; missing=[...], extra=[]
```

run either of these:

```bash
python3 gen.py --engine rust --contact "your.email@example.edu"
```

or, without Rust:

```bash
python3 gen_python.py --json-only --contact "your.email@example.edu"
```

The immediate cleanup command is:

```bash
rm -rf .cache/dblp/bib/tcs
```

The new wrapper does not use the failing bulk-BibTeX path by default.

## Cache policy

```bash
# Fetch fresh DBLP pages and overwrite cache.
python3 gen.py --cache-policy refresh

# Reuse cached pages when present; fetch missing pages.
python3 gen.py --cache-policy reuse

# Use only cache; useful for deterministic offline regeneration.
python3 gen.py --cache-policy offline
```

The cache lives under `.cache/dblp/` and is not intended to be committed.

## Cron

Edit `scripts/update-and-push.sh` and set:

```bash
REPO="/absolute/path/to/theorybib"
CONTACT="your.email@example.edu"
```

Then install a cron entry similar to `cron.d/theorybib`:

```cron
17 3 1 */4 * USER /absolute/path/to/theorybib/scripts/update-and-push.sh >> /absolute/path/to/theorybib/update.log 2>&1
```

That runs at 03:17 on the first day of every fourth month. The update script uses `flock`, regenerates the bibliography, commits only if `bib/` or `meta/` changed, and pushes.

## Notes

The Rust engine generates UTF-8 BibTeX from DBLP JSON metadata. This is usually preferable for unattended updates because it is compact, paginated, and validated by DBLP's JSON result counts. If exact DBLP BibTeX formatting is required for a particular venue, use `gen_python.py --individual-fallback --only VENUE`, but that mode performs one request per record and is not appropriate for large streams such as TCS.
