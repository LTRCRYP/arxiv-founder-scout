# arxiv-founder-scout

Screens newly published arXiv papers, scores them against an editable thesis, and cross-references
their authors against founders and executives of recently started companies. Produces a weekly report
with three lists: top papers, author ↔ founder matches, and strong papers whose authors have no known startup.

Python 3.12, three runtime dependencies (`requests`, `anthropic`, `python-dotenv`), SQLite storage.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # then fill in the keys below
.venv/bin/python -m scout init
```

| Variable | Needed for | How to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | `screen` (LLM criteria) | console.anthropic.com |
| `OPENALEX_API_KEY` | `enrich` (institutions, ORCID) | free: openalex.org/signup → Settings → API key. Without it you share a $0.10/day budget with your whole IP. |
| `SEMANTIC_SCHOLAR_API_KEY` | optional, dedicated 1 req/s | semanticscholar.org/product/api |
| `SEC_USER_AGENT` | `founders` (Form D download) | no signup; SEC requires `"Your Name you@example.com"` on every request |

## Pipeline

```
scout ingest   → arXiv API, by category and submitted-date window, idempotent upserts
scout enrich   → Semantic Scholar + OpenAlex, per paper (h-index, citations, affiliations, ORCID, homepage)
scout screen   → deterministic criteria + one Claude call per paper (JSON schema output, one-line reasons)
scout founders → pluggable sources: SEC Form D quarterly data sets, CSV imports (Crunchbase/PitchBook), [yc_oss, off]
scout match    → author ↔ person confidence from combined signals, with evidence
scout report   → reports/<year>-W<week>.md (and/or .html)
scout run      → all of the above
```

Typical daily cron: `scout ingest && scout enrich && scout screen`; weekly: `scout founders && scout match && scout report`.

Useful flags: `--since/--until` (ISO dates, UTC), `--limit N`, `screen --no-llm`, `screen --rescore`,
`match --rematch`, `match --review <id> accepted|rejected`, `report --format html|both`, `founders --source csv_import`.

## Configuration

Everything lives in `config.toml`: categories, the "new startup" window (months), scoring weights, the topic
thesis the LLM is shown, keyword lists for deterministic signals, enabled founder sources with per-source filters,
CSV column mappings, matching weights and tier thresholds, report sizes.

## How matching works

A name alone never produces a confirmed match. Each author/person pair gets a confidence in [0, 1] from
additive signals, each recorded as evidence in the report:

| Signal | Default weight |
|---|---|
| exact / fuzzy / initials-only name match | 0.45 / 0.35 / 0.20 |
| author homepage on the company's domain | +0.30 |
| ORCID employment record names the company | +0.30 |
| author affiliation string names the company | +0.25 |
| a co-author is also listed at the same company | +0.20 |
| company location appears in the author's affiliations | +0.15 |
| paper text overlaps the company description (scaled) | up to +0.15 |
| company is in a tech industry group (Form D has no descriptions) | +0.05 |
| > 20 Semantic Scholar authors share the exact name | −0.15 |
| match relies on initials only | −0.10 |

Tiers: confirmed ≥ 0.85, probable ≥ 0.60, otherwise weak. Weak matches appear only in a collapsed
"needs review" list and are never counted as a founder for list 3. Use `scout match --review` to accept or reject.

## Data sources and terms (verified 2026-11-09)

- **arXiv API**: ≤ 1 request / 3 s, single connection, HTTPS only; metadata is CC0. Papers appear only after
  announcement, so ingest looks back 4 days by default and overlaps 1 day between runs.
- **Semantic Scholar**: public endpoints without a key (shared pool), optional free key for a dedicated 1 req/s.
- **OpenAlex**: free key gives a $1/day budget (≈ 1,000 work lookups) and 30 req/s.
- **SEC Form D data sets**: quarterly TSV zips; ≤ 10 req/s with a declared User-Agent including contact email.
  US companies that raised money only; lags up to a quarter. Tables used: ISSUERS, OFFERING, RELATEDPERSONS, FORMDSUBMISSION.
- **ORCID public API**: anonymous reads allowed (12 req/s, 25k/day/IP).
- **Y Combinator**: ycombinator.com's terms prohibit scraping, so this tool never requests it. A third-party
  mirror adapter (`yc_oss`, company-level only) exists but is disabled by default.
- **LinkedIn**: not used.

Only public professional information about people is stored (name, role, company, public affiliations, ORCID).

## Tests

```bash
.venv/bin/python -m pytest -q
```

Covers name normalisation, ingest dedup and version handling, founder import filtering/dedup, matching tiers
and evidence, screening composition (with a fake LLM client), and report rendering, on a small fixture set.
