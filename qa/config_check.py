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
cannot see; drift_check catches that via volume drop.

Two modes (same checks):
  DB   — read spiders from the database (data-side use):
           python qa/config_check.py --project jobfeed
  FILE — lint raw config JSON directly, no DB (intended for CI on the config
         store, so a decay-prone config is blocked before it ships):
           python qa/config_check.py templates/collection/*.json
Exit status: 1 if any FROZEN_ENUMERATION / NO_REQUIRED_SRC / UNREADABLE, else 0.
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


def analyze_config(name, start_urls, settings, has_follow_rule, extract_fields):
    """Core shape analysis, independent of where the config came from.

    Args (already normalized by a loader):
      name              spider name (for reporting)
      start_urls        list[str]
      settings          dict (GENERATED_URLS / USE_SITEMAP / PAGINATED_LISTINGS)
      has_follow_rule   bool — does any rule/section follow links?
      extract_fields    set[str] — field names the config extracts (for coverage)

    Returns list[(code, note)].
    """
    start_urls = start_urls or []
    settings = settings or {}
    n_detail_starts = sum(1 for u in start_urls if _looks_like_detail(u))

    gen = _parse_json(settings.get("GENERATED_URLS"))
    gen_list = gen_dynamic = False
    if gen:
        for entry in (gen if isinstance(gen, list) else [gen]):
            for v in (entry.get("vars") or {}).values():
                if isinstance(v, dict) and v.get("type") == "list":
                    gen_list = True
                if isinstance(v, dict) and v.get("type") in ("range", "date"):
                    gen_dynamic = True
        if any((e.get("method") or "").upper() == "POST" for e in
               (gen if isinstance(gen, list) else [gen])):
            gen_dynamic = True

    sitemap = str(settings.get("USE_SITEMAP", "")).lower() in ("true", "1")
    paginated = bool(settings.get("PAGINATED_LISTINGS"))

    has_discovery = bool(has_follow_rule) or gen_dynamic or sitemap or paginated
    enumerates_identities = (n_detail_starts >= 1 and not has_follow_rule) or gen_list

    issues = []
    if enumerates_identities and not has_discovery:
        how = []
        if gen_list:
            how.append("GENERATED_URLS `list` of hard-coded ids")
        if n_detail_starts and not has_follow_rule:
            how.append(f"{n_detail_starts} hard-coded job URL(s)")
        issues.append(("FROZEN_ENUMERATION",
                       f"enumerates specific jobs ({'; '.join(how)}) with no "
                       "discovery (no follow/range/sitemap) => will decay as "
                       "those jobs expire; a healthy config discovers ids instead"))
    elif not has_discovery and len(start_urls) <= 1 and not gen_list:
        issues.append(("NO_DISCOVERY",
                       "no follow rule, no generated/sitemap discovery, "
                       f"{len(start_urls)} start_url => scrapes a fixed tiny set"))

    for fld in ("description", "employer", "location"):
        if extract_fields and fld not in extract_fields:
            issues.append(("NO_REQUIRED_SRC",
                           f"required field {fld!r} has no selector/section source"))
    return issues


def lint_db(db_path, project):
    """Loader: feed analyze_config() from the DB (data-side use)."""
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
            "SELECT follow FROM spider_rules WHERE spider_id=?", [s["id"]]).fetchall()
        has_follow = any(r["follow"] for r in rules)
        start_urls = _parse_json(s["start_urls"]) or []
        cb = _parse_json(s["callbacks_config"]) or {}
        extract_fields = set()
        for callback in (cb or {}).values():
            ex = (callback or {}).get("extract", {}) if isinstance(callback, dict) else {}
            extract_fields.update(ex.keys())
        for code, note in analyze_config(
                s["name"], start_urls, settings_by.get(s["id"], {}),
                has_follow, extract_fields):
            findings.append({"spider": s["name"], "code": code, "note": note})
    conn.close()
    return findings


def lint_files(paths):
    """Loader: feed analyze_config() from raw config JSON files (CI use, no DB).

    Reads the `sections` authoring format (what gets committed): a config has
    top-level `start_urls`, `settings`, and `sections[]` where each section has
    `match`, optional `follow`, and optional `extract` (a field->selector dict,
    "auto", or absent)."""
    findings = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            findings.append({"spider": os.path.basename(path),
                             "code": "UNREADABLE", "note": f"cannot parse: {e}"})
            continue
        name = cfg.get("name") or os.path.splitext(os.path.basename(path))[0]
        start_urls = cfg.get("start_urls") or []
        settings = cfg.get("settings") or {}
        sections = cfg.get("sections") or []

        has_follow = any(sec.get("follow") for sec in sections if isinstance(sec, dict))
        extract_fields = set()
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            ex = sec.get("extract")
            if isinstance(ex, dict):
                extract_fields.update(ex.keys())
            elif ex == "auto":
                extract_fields.update(("title", "content", "author", "published_date"))
        # legacy rules+callbacks shape: fall back to those if no sections
        if not sections and cfg.get("rules"):
            has_follow = any(r.get("follow") for r in cfg["rules"] if isinstance(r, dict))
            for callback in (cfg.get("callbacks") or {}).values():
                ex = (callback or {}).get("extract", {}) if isinstance(callback, dict) else {}
                extract_fields.update(ex.keys())

        for code, note in analyze_config(
                name, start_urls, settings, has_follow, extract_fields):
            findings.append({"spider": name, "code": code, "note": note})
    return findings


def main():
    ap = argparse.ArgumentParser(
        description="scrapai spider config-shape checker (DB or raw-JSON mode)")
    ap.add_argument("files", nargs="*",
                    help="config JSON files to lint (CI mode, no DB needed). "
                         "If omitted, reads spiders from the DB.")
    ap.add_argument("--db", default="scrapai.db", help="DB mode: database path")
    ap.add_argument("--project", default="jobfeed", help="DB mode: project name")
    ap.add_argument("--json", default=None, help="write findings to this JSON file")
    args = ap.parse_args()

    if args.files:
        findings = lint_files(args.files)
        scope = f"{len(args.files)} file(s)"
    else:
        findings = lint_db(args.db, args.project)
        scope = f"project={args.project}"

    hard = [f for f in findings
            if f["code"] in ("FROZEN_ENUMERATION", "NO_REQUIRED_SRC", "UNREADABLE")]

    print(f"\nConfig check — {scope}  findings={len(findings)}  "
          f"blocking={len(hard)}\n")
    if not findings:
        print("No config-shape problems found.")
    for f in findings:
        print(f"  [{f['code']}] {f['spider']}")
        print(f"       {f['note']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"scope": scope, "findings": findings}, fh, indent=2)
        print(f"\nWrote {args.json}")

    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()
