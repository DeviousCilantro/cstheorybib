---
title: "Bibliography for Theoretical Computer Science and Related Areas"
aliases: "Bibliography for Theoretical Computer Science and Related Areas"
linter-yaml-title-alias: "Bibliography for Theoretical Computer Science and Related Areas"
date created: Sunday, May 10th 2026, 2:37:09 pm
date modified: 2026-07-21
---

<!-- @format -->

# Bibliography for Theoretical Computer Science and Related Areas

DBLP-derived BibTeX records for selected theory venues. `gen.py` selects the Rust generator by default and falls back to the Python implementation when Cargo is unavailable.

The Rust engine paginates DBLP JSON results, assigns stable keys, manages the cache, and writes the outputs. `gen_python.py` provides the compatible fallback path.

## Generated Files

```text
bib/{all,combinatorica,ecc,ipl,jacm,jsl,tcs}.bib
meta/keymap.tsv
meta/manifest.json
```

| File | Label | DBLP Stream | Venue |
| --- | ---: | --- | --- |
| `bib/combinatorica.bib` | `COMB` | `journals/combinatorica` | Combinatorica |
| `bib/ecc.bib` | `ECC` | `journals/eccc` | Electronic Colloquium on Computational Complexity |
| `bib/ipl.bib` | `IPL` | `journals/ipl` | Information Processing Letters |
| `bib/jacm.bib` | `JACM` | `journals/jacm` | Journal of the ACM |
| `bib/jsl.bib` | `JSL` | `journals/jsyml` | Journal of Symbolic Logic |
| `bib/tcs.bib` | `TCS` | `journals/tcs` | Theoretical Computer Science |

`ECC` currently denotes DBLP's ECCC stream. Change `venues.json` if another venue is intended.

## Key Convention

Keys have the form `venue-label:author-labelYY` with a lowercase collision suffix when needed. One author contributes the full surname; two or three authors contribute the first three letters of each surname; larger teams contribute surname initials for up to six authors.

`meta/keymap.tsv` preserves assigned keys across updates, preventing a newly colliding record from renaming an existing citation.

## Requirements

- Python 3.9 or newer is required for orchestration and fallback generation.
- A Rust toolchain with Cargo enables the default engine.

The first Rust run builds `target/release/theorybib-dblp`; later runs reuse it.

## Generate

```bash
python3 gen.py --contact "your.email@example.edu"
python3 gen.py --engine rust --contact "your.email@example.edu"
python3 gen.py --engine python --contact "your.email@example.edu"
python3 gen.py --only jsl --contact "your.email@example.edu"
```

DBLP asks automated clients to identify a contact address. For unattended operation, use the delay, jitter, cooldown, and retry options documented by `python3 gen.py --help`.

## Cache Policy

```bash
python3 gen.py --cache-policy refresh  # replace cached pages
python3 gen.py --cache-policy reuse    # fetch only missing pages
python3 gen.py --cache-policy offline  # prohibit network access
```

The untracked cache lives under `.cache/dblp/`. Offline generation is deterministic only for the pages already present.

## Cron

Set `REPO` and `CONTACT` in `scripts/update-and-push.sh`, then adapt `cron.d/theorybib` to the local account and absolute paths. The script uses `flock`, regenerates data, commits only changed `bib/` or `meta/` files, and pushes; inspect it before enabling unattended writes.

## Notes

JSON pagination is the supported bulk path because its result counts can be validated consistently. `gen_python.py --individual-fallback --only VENUE` can reproduce DBLP-style records one at a time, but it is unsuitable for large streams.
