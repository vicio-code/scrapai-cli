#!/usr/bin/env python3
"""
Config-shape checker for jobfeed spiders (QA layer 3).

The two data checkers (dq_check, drift_check) inspect the OUTPUT rows. This one
inspects the spider CONFIG itself, read-only from the DB, to catch a STRUCTURAL
class of problem that only surfaces in the data much later — after jobs have
already decayed.

THE CLASS OF BUG (not a single instance):
    A config that hard-codes WHICH jobs exist instead of DISCOVERING which jobs
    exist. The job identities are a frozen snapshot; the config has no way to
    pick up new postings or notice old ones disappear. It silently decays to
    worthless. This shows up in several disguises:
      * hard-coded job-detail URLs in start_urls with no follow rule
        (careers_hugoboss_com, www_multitrabajos_com)
      * a GENERATED_URLS `list` var of hard-coded ids/tokens, with no
        range/date generation, sitemap, or link-following to find new ones
        (guess, apply_workable_com_guess, flyzipline_com)
    A HEALTHY config instead follows a listing, enumerates a page RANGE/date,
    or uses a sitemap — so new jobs appear automatically.

The checker detects the CLASS: "does this spider enumerate specific job
identities AND lack any discovery mechanism?" — so it also flags shapes not
seen yet (e.g. a hard-coded list in formdata), not just the known examples.

Checks (all read-only):
  FROZEN_ENUMERATION — enumerates specific job identities (hard-coded URLs or a
                       GENERATED_URLS `list` var) with no link-following,
                       range/date generation, sitemap, or pagination to
                       discover new jobs. Will decay as those jobs expire.
  NO_DISCOVERY       — no follow rule, no generated URLs, no sitemap, and a
                       single/zero start_url: scrapes a fixed tiny set only
                       (often a test leftover).
  NO_REQUIRED_SRC    — a required non-core field has no selector in any
                       callback (config structurally cannot populate it).

Note on tuning: GENERATED_URLS with a `range`/`date` var (att_jobs page sweep)
is treated as HEALTHY discovery. The one soft caveat — a range that has become
too small for a grown site — is a reality-vs-config drift a static checker
cannot see; drift_check catches that via volume drop. Stated in the proposal.

Usage:
    .venv/bin/python qa/config_check.py
    .venv/bin/python qa/config_check.py --json lint.json
Exit status: 1 if any FROZEN_ENUMERATION / NO_REQUIRED_SRC found, else 0.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys

REQUIRED_FIELDS = ("title", "description", "employer", "location")

# A start URL that looks like a specific job detail page (has an id-ish segment)
# rather than a listing/section landing page.
JOB_DETAIL_HINTS = re.compile(
    r"/job/|/jobs/|/empleos/|/positions/|/career[s]?/.+/|-\d{5,}", re.I)


def connect_ro(db_path):
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _looks_like_detail(url):
    return bool(JOB_DETAIL_HINTS.search(url or ""))


def _parse_json(val):
    """Settings/JSON columns may be stored as a JSON string or already parsed."""
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def lint(db_path, project):
    conn = connect_ro(db_path)
    spiders = conn.execute(
        "SELECT id, name, start_urls, callbacks_config FROM spiders WHERE project=?",
        [project]).fetchall()
    settings_by = {}
    for r in conn.execute("SELECT spider_id, key, value FROM spider_settings"):
        settings_by.setdefault(r["spider_id"], {})[r["key"]] = r["value"]

    findings = []
    for s in spiders:
        rules = conn.execute(
            "SELECT follow, callback, allow_patterns FROM spider_rules WHERE spider_id=?",
            [s["id"]]).fetchall()
        try:
            start_urls = json.loads(s["start_urls"]) if s["start_urls"] else []
        except (json.JSONDecodeError, TypeError):
            start_urls = []
        st = settings_by.get(s["id"], {})

        follow_rules = [r for r in rules if r["follow"]]
        n_detail_starts = sum(1 for u in start_urls if _looks_like_detail(u))

        # --- discovery vs enumeration analysis (the real class) -------------
        gen = _parse_json(st.get("GENERATED_URLS"))
        gen_list = gen_dynamic = False
        if gen:
            for entry in (gen if isinstance(gen, list) else [gen]):
                for v in (entry.get("vars") or {}).values():
                    if isinstance(v, dict) and v.get("type") == "list":
                        gen_list = True
                    if isinstance(v, dict) and v.get("type") in ("range", "date"):
                        gen_dynamic = True
            # a POST search backend with formdata enumeration counts as dynamic
            if any((e.get("method") or "").upper() == "POST" for e in
                   (gen if isinstance(gen, list) else [gen])):
                gen_dynamic = True

        sitemap = str(st.get("USE_SITEMAP", "")).lower() in ("true", "1")
        paginated = bool(st.get("PAGINATED_LISTINGS"))

        # DISCOVERY = any mechanism that finds new jobs over time.
        has_discovery = bool(follow_rules) or gen_dynamic or sitemap or paginated
        # ENUMERATION of specific job identities = frozen snapshot.
        enumerates_identities = (n_detail_starts >= 1 and not follow_rules) or gen_list

        issues = []

        if enumerates_identities and not has_discovery:
            how = []
            if gen_list:
                how.append("GENERATED_URLS `list` of hard-coded ids")
            if n_detail_starts and not follow_rules:
                how.append(f"{n_detail_starts} hard-coded job URL(s)")
            issues.append(("FROZEN_ENUMERATION",
                           f"enumerates specific jobs ({'; '.join(how)}) with no "
                           "discovery (no follow/range/sitemap) => will decay as "
                           "those jobs expire; a healthy config discovers ids instead"))
        elif not has_discovery and len(start_urls) <= 1 and not gen_list:
            issues.append(("NO_DISCOVERY",
                           "no follow rule, no generated/sitemap discovery, "
                           f"{len(start_urls)} start_url => scrapes a fixed tiny set"))

        # NO_REQUIRED_SRC — does the config source every required field?
        cb = _parse_json(s["callbacks_config"]) or {}
        sourced = set()
        for callback in (cb or {}).values():
            extract = (callback or {}).get("extract", {}) if isinstance(callback, dict) else {}
            sourced.update(extract.keys())
        # 'auto' core fields (title) may be filled by the reader; only flag the
        # non-core required fields that MUST have a selector.
        for fld in ("description", "employer", "location"):
            if sourced and fld not in sourced:
                issues.append(("NO_REQUIRED_SRC",
                               f"required field {fld!r} has no selector in any callback"))

        for code, note in issues:
            findings.append({"spider": s["name"], "code": code, "note": note})

    conn.close()
    return findings


def main():
    ap = argparse.ArgumentParser(description="jobfeed spider config-shape checker")
    ap.add_argument("--db", default="scrapai.db")
    ap.add_argument("--project", default="jobfeed")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    findings = lint(args.db, args.project)
    hard = [f for f in findings if f["code"] in ("FROZEN_ENUMERATION", "NO_REQUIRED_SRC")]

    print(f"\nConfig check — project={args.project}  findings={len(findings)}  "
          f"blocking={len(hard)}\n")
    if not findings:
        print("No config-shape problems found.")
    for f in findings:
        print(f"  [{f['code']}] {f['spider']}")
        print(f"       {f['note']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"project": args.project, "findings": findings}, fh, indent=2)
        print(f"\nWrote {args.json}")

    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()
