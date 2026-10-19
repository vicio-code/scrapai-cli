# QA suite — independent data-quality & spider-drift detection

Three small, **read-only**, dependency-free checks that run against the scrapai
database to answer, at scale (495 target sites), questions a build-time 5-item
spot-check cannot:

| Script | Question it answers | When it fires |
|--------|--------------------|---------------|
| `dq_check.py` | *Is the data we already stored actually good?* | per-row, immediately |
| `drift_check.py` | *Did a working spider silently get worse than its own past?* | needs a baseline, catches decay |
| `config_check.py` | *Can this spider config even keep working, or is it doomed to decay?* | static, immediately |

They are **independent** of scrapai's own build-time verdict on purpose: each
opens the DB read-only via `sqlite3` and re-derives its own judgement, so it is a
genuine second opinion, not a re-run of the system that built the spiders. None
of them import framework code or write to the DB.

## Why these three

They map to the three ways job data goes wrong:

1. **It's bad now** — empty/junk fields, a street address where a city should be,
   a workplace flag (`Remote`/`Hybrid`) stored as a location, a description that
   is a cookie banner, a title that is a UI label. `dq_check` catches these with
   per-field + cross-field heuristics and per-spider length-outlier stats, then
   triages every spider `CRITICAL / REVIEW / WATCH / OK`.
2. **It goes bad later** — a site redesign, a selector silently returning empty.
   `drift_check` fingerprints each spider (item count, per-required-field fill
   rate, median description length) into a baseline, then diffs a later run and
   classifies the change: `EXTRACTION_DRIFT / PARTIAL_DRIFT / VOLUME_DROP /
   NO_DATA / NEW / GONE`. It reads the **DB** (spider `active` flag + transport
   settings) to tell a transport block from legitimately-no-open-roles — no
   local-file dependency.
3. **It was structurally doomed** — a config that hard-codes *which* jobs exist
   (hard-coded `start_urls`, or a `GENERATED_URLS` `list` of fixed ids) instead
   of *discovering* them, so it decays to worthless as those postings expire.
   `config_check` flags this class (`FROZEN_ENUMERATION`) immediately, before any
   data rots — the one case a data-only checker can only catch *after* the damage.

## Why not an LLM on every row

An LLM-as-judge pass was prototyped but is intentionally **not** included here.
The heuristics catch every *structural* defect we observed in the real data for
free; an LLM would only add value for *semantic* cross-field errors (e.g. a
description that is well-formed but about a different job than its title), which
we have not yet observed. Running an LLM per row would also reintroduce the
per-page inference cost scrapai's "AI once, deterministic forever" model exists
to avoid. If a semantic-error class shows up later, a *sampled* LLM tier can be
added on top.

## Usage

```bash
# Data quality — triage every spider, worst first. Exit 1 if any REVIEW/CRITICAL.
python qa/dq_check.py --project jobfeed
python qa/dq_check.py --project jobfeed --spider careers_hugoboss_com --rows
python qa/dq_check.py --project jobfeed --json dq.json      # for CI

# Drift — snapshot a known-good baseline, then compare later.
python qa/drift_check.py snapshot --project jobfeed --out qa/baseline.json
python qa/drift_check.py compare  --project jobfeed --baseline qa/baseline.json

# Config — flag configs that cannot discover new jobs. Exit 1 if any found.
python qa/config_check.py --project jobfeed
```

All three take `--db` (default `scrapai.db`) and `--project` (default `jobfeed`),
and emit `--json` for CI wiring. Each exits non-zero when it finds something that
warrants human review, so they drop straight into a cron/CI gate.

Suggested cadence: `dq_check` nightly / per export; `drift_check compare` weekly
(re-`snapshot` after each full fleet crawl); `config_check` after any bulk config
change.

## Tests

```bash
python -m pytest qa/tests/ -q
```

43 pure unit tests (no DB, no network) covering each field rule, the drift
classifier, and the config-shape detector — asserting both that bad input is
flagged and that good input is left alone (the false-positive guard).

## Findings from the current data (validation)

Run against the live `jobfeed` set (160 spiders / ~1,144 items), the suite found
real, hand-verified defects — evidence the checks work, not just that they run:

- `careers_hugoboss_com` — 8/15 rows have title+description+location all empty
  while `employer` stays a constant `"Hugo Boss"`; a sub-layout extraction break
  that a plain non-null check misses (`dq_check` CRITICAL; `config_check`
  FROZEN_ENUMERATION).
- Several Workday tenants (`target_wd5`, `aah_wd5`, …) store a **street address**
  as `location` instead of `City, ST` (`dq_check` `location_street_address`).
- `liberty_wd5` stores `"Hybrid"` (a workplace flag) as `location`.
- 5 configs (`careers_hugoboss_com`, `www_multitrabajos_com`, `guess`,
  `apply_workable_com_guess`, `flyzipline_com`) enumerate a frozen job set with
  no discovery mechanism and will decay as those postings expire
  (`config_check` FROZEN_ENUMERATION).

## Note on baselines

Most spiders currently hold only ~5 test-crawl rows, so a `drift_check` baseline
taken now is provisional. Take the real baseline after the first full fleet
crawl, when fill-rate and length distributions are statistically meaningful.
