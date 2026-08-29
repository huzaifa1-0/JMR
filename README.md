# Job Market Research Platform

A local, Windows-friendly job-market research tool. Collects publicly accessible
job postings from permitted sources, cleans and deduplicates them, extracts
structured fields, and gives you a research workspace with custom fields,
Boolean search, scoring, analytics and export.

**Search → Collect → Clean → Filter → Analyse → Research → Score → Export**

---

## Read this first: Indeed

**Indeed is not a data source in this application, and cannot be.** This is a
legal and contractual limitation, not a technical gap:

| Route | Status |
|---|---|
| Publisher / Job Search API | Deprecated 2023, closed to new integrations |
| Publisher Program | Closed to new signups since October 2022 |
| Job Sync / Indeed Apply APIs | Employer-side only — they publish jobs, they don't return searchable postings |
| Partner agreement | Selective, months-long, aimed at ATS vendors |
| Direct scraping | Prohibited by Indeed's Terms of Service |
| Third-party "Indeed APIs" | Unofficial resellers of scraped data — moves the exposure to a vendor rather than resolving it |

Scraping Indeed would require defeating its anti-bot layer, which this project
will not do. Instead, **Indeed is the UX model, not the data source**: the
search interface follows Indeed's shape, while the data comes from sources that
permit programmatic access.

### What that costs you

Stated plainly, because it should inform how you read every number:

- **Small local employers** that post only to Indeed will not appear
- **Staffing-agency volume** is heavily under-represented
- **"Easy Apply"** is Indeed-specific and will usually be empty
- **Absolute counts are a sample, not the market.** Rankings, trends and
  comparisons are sound. Total market sizing is not. The Analytics page states
  this caveat on itself rather than hiding it here.

If partner access to another provider ever becomes available, it can be added
as a single adapter without touching the rest of the application.

---

## Installation (Windows)

**Requirements:** Python 3.11 or newer, with "Add python.exe to PATH" ticked
during installation.

1. Unzip this folder anywhere, e.g. `C:\JobResearch`
2. Double-click **`run.bat`**
3. First run creates a virtual environment and installs dependencies — a few
   minutes, once. Later runs start in seconds.
4. Your browser opens at **http://127.0.0.1:8420**

macOS / Linux: `./run.sh`

To stop: press `Ctrl+C` in the terminal window.

### Where your data lives

`%LOCALAPPDATA%\JobResearch\` by default (changeable in Settings):

```
research.db      the whole corpus -- back up by copying this one file
exports/         generated CSV / XLSX / JSON
logs/app.log     rotating application log
```

---

## First run

The app works immediately with **Remotive**, which needs no credentials. For
serious US coverage, add these free keys in **Settings**:

| Source | Where | Cost | Notes |
|---|---|---|---|
| **Adzuna** | developer.adzuna.com | Free, ~1,000 calls/month | Primary breadth source for US |
| **USAJOBS** | developer.usajobs.gov | Free, unmetered | US federal roles |
| **ATS boards** | — | Free, unlimited | Greenhouse, Lever, Ashby, Workable — no signup at all |

Then: **Search & collect** → enter a job title → **Collect new data**.

---

## The one idea that makes this work

**Collection and search are separate operations.**

Searching does not touch the network. It queries the local corpus you have
already built. So your metered API budget is spent *building an asset*, never
answering individual questions — and searching is instant, unlimited and works
offline.

On top of that sits the **discovery loop**, which is what makes free tiers
compound:

```
Adzuna search (metered)
        ↓  posting includes an apply URL
boards.greenhouse.io/acmehealth/jobs/123
        ↓  board token extracted from the URL
company registered as greenhouse/acmehealth
        ↓
that employer's ENTIRE board pulled free, forever, with FULL descriptions
```

One metered call buys permanent monitoring of an employer. Over weeks the
corpus shifts from aggregator-shallow to ATS-deep, and full description text is
what the skills extraction, description filters and sponsorship detection all
depend on.

---

## Features

**Search** — Indeed-style basics (title, keywords, skills, company, city,
state, ZIP, remote), plus date posted, employment type, work arrangement,
salary range, experience level.

**Boolean logic** — real parser, not string substitution, compiled to SQLite
FTS5 with all FTS operators neutralised:

```
("medical billing" OR "revenue cycle") AND ("remote" OR "work from home")
("software engineer" OR developer) NOT internship
```

Live syntax validation as you type, with precise error messages.

**Advanced research filters** — title/description contains and does-not-contain,
required vs. excluded skills, education, years of experience, visa sponsorship,
relocation, urgency, posting age, minimum score, company size, industry, open
postings, hiring frequency, ATS platform.

**Custom fields** — create your own typed fields (text, number, boolean, date,
dropdown, multiselect, score). Each becomes a filter immediately and appears on
every job. Stored EAV with typed value columns so they stay sortable and
filterable in SQL.

**Deduplication** — three passes: exact source key, canonical fingerprint
(normalised title + company + location), then fuzzy title similarity plus
simhash on the description. Duplicates are *linked, not deleted*; the record
from the deepest source becomes primary. Appearing on several boards is itself a
signal.

**Extraction** — skills (editable gazetteer, classified required vs. preferred
by section heading), education, years of experience, salary (parsed from text
when not structured, annualised across hourly/weekly/monthly), benefits, visa
sponsorship (with negation handling), relocation, urgency.

**Research workspace** — status, priority, lead quality, tags, notes per job.

**Scoring** — configurable rules stored as JSON, not hard-coded. Toggle rules,
change weights, rescore.

**Analytics** — skills frequency, hiring companies, locations, salary bands,
experience levels, arrangement, employment type, timeline.

**Export** — CSV, XLSX, JSON, always respecting the current filters.

---

## Architecture

```
frontend/          plain ES2020, no build step, served by FastAPI
backend/app/
  api/             FastAPI routers
  collection/      JobSource adapters, quota, rate limiting, ATS discovery
    sources/       adzuna, remotive, usajobs, greenhouse, lever, ashby, workable
  processing/      normalize, extract, dedupe, scoring
  search/          Boolean parser, query builder
  models.py        SQLAlchemy schema
  data/            skills taxonomy (YAML), seed ATS companies (CSV)
```

**Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, SQLite + FTS5, vanilla JS.

No Node, no Docker, no compiled dependencies, no browser storage. The frontend
is deliberately build-free so the whole application runs with Python alone —
this is the one deliberate deviation from the original React/Vite plan.

**Adding a data source** means writing one class implementing `JobSource` and
registering it. Nothing above the collection layer changes.

---

## Compliance

Enforced structurally in the collection layer, not merely documented:

- No CAPTCHA solving
- No authentication or access-control circumvention
- No proxy, IP or identity rotation
- No headless browsers or fingerprint spoofing
- No private or authenticated endpoints
- Rate limits and monthly quotas enforced in code
- `Retry-After` honoured
- Honest, contactable User-Agent (set `JRP_USER_AGENT` to include your email)

Each source's terms should be read once before large-scale use. The job record
always retains its source and original URL, so provenance is traceable.

---

## Testing

```bash
pip install pytest
pytest -q          # 173 tests
```

- `test_units.py` — Boolean parser (including FTS5 injection attempts),
  normalisation, extraction, dedup thresholds, scoring rules
- `test_e2e.py` — full API round trip against synthetic postings: search,
  filters, dedup linking, research, custom fields, analytics, export
- `test_contract.py` — asserts every endpoint returns the exact keys the
  frontend reads, since untyped JS fails silently on a renamed key

---

## Troubleshooting

**"Python was not found"** — install from python.org and tick "Add python.exe to
PATH", then run `run.bat` again.

**Port 8420 already in use** — set `JRP_PORT=9000` in a `.env` file (copy
`.env.example`).

**No results after collecting** — check Settings that a source is enabled and
its credentials are accepted (use the Test button). Remotive alone is remote-only
and tech-leaning; add Adzuna for real US breadth.

**Adzuna quota exhausted** — the counter resets monthly. ATS sources stay
unmetered, so previously discovered employers keep updating for free.

**Descriptions look truncated** — aggregators truncate. They fill in once the
employer's ATS board is discovered; the badge in the results table marks which
records are still partial.

**Reset everything** — close the app and delete `research.db` from the data
directory. It is recreated and reseeded on next start.

---

## Not built

Honest scope: this is a working Phase 1–8 foundation, not a finished product.

- Scheduled/background collection (APScheduler is planned, collection is manual)
- Alembic migrations (schema is created directly; deleting the DB is the reset path)
- PyInstaller `.exe` packaging (Phase 10 — `run.bat` covers the stated need)
- Company size and industry enrichment (fields exist; no enrichment source wired)
- Kanban research board (the queue is a table)
