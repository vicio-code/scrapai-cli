#!/usr/bin/env python3
"""
Spider drift / breakage detector for the jobfeed project (Problem 2).

An INDEPENDENT layer on top of `./scrapai health`. It exists because health has
blind spots for jobfeed specifically:

  * health inspects the `content` column, but jobfeed stores the job body in
    metadata_json.description — so health is blind to the real required fields.
    (Verified: careers_hugoboss_com has content=NULL on every row yet health's
    extraction check reads `content`.)
  * health is point-in-time pass/fail with hardcoded thresholds — it has no
    baseline, so it cannot see partial drift (50 jobs last run, 2 now) or
    distinguish a layout change from a transport block from "no open roles".

This tool is schema-aware (reads description/employer/location from
metadata_json) and baseline-driven. Two subcommands:

    snapshot  — compute per-spider fingerprint metrics from the DB and save a
                baseline JSON. Run after a known-good crawl.
    compare   — recompute metrics now, diff against the baseline, and classify
                each spider: OK | NO_DATA | EXTRACTION_DRIFT | PARTIAL_DRIFT |
                FILL_DRIFT | VOLUME_DROP | NEW | GONE. Cross-references
                tracking.csv (registry of what should exist + transport) to
                separate extraction breaks from transport breaks from
                legitimately-no-jobs.

Read-only on the DB (sqlite3, mode=ro). Never imports scrapai framework code.

Usage:
    .venv/bin/python qa/drift_check.py snapshot --out qa/baseline_jobfeed.json
    .venv/bin/python qa/drift_check.py compare  --baseline qa/baseline_jobfeed.json
    .venv/bin/python qa/drift_check.py compare  --baseline qa/baseline_jobfeed.json --json drift.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

REQUIRED_META = ("description", "employer", "location")


def connect_ro(db_path):
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_registry(db_path, project, tracking_path=None):
    """Production source of truth, read from the DB (not a local CSV):

      spider_name -> {active, transport, expected_employer}

    - active:   spiders.active — the registry of what SHOULD exist. A retired/
                blocked spider is deactivated (active=0), so a 0-item active
                spider is suspicious while a 0-item inactive one is expected.
    - transport: derived from spider_settings — whether the spider runs through
                the browser/Cloudflare path (prone to transport blocks) vs plain
                HTTP. This is what the spider ACTUALLY runs with, so it is more
                reliable than any external note.

    tracking.csv is now OPTIONAL and used only to enrich `expected_employer`
    when present; its absence changes nothing operationally.
    """
    conn = connect_ro(db_path)
    reg = {}
    for r in conn.execute(
            "SELECT id, name, active FROM spiders WHERE project = ?", [project]):
        reg[r["name"]] = {
            "id": r["id"],
            "active": bool(r["active"]),
            "transport": "http",          # default; upgraded below
            "expected_employer": "",
        }
    # Derive transport from the spider's own settings.
    name_by_id = {v["id"]: k for k, v in reg.items()}
    for r in conn.execute("SELECT spider_id, key, value FROM spider_settings"):
        sp = name_by_id.get(r["spider_id"])
        if not sp:
            continue
        key = (r["key"] or "").upper()
        truthy = str(r["value"]).strip().lower() in ("true", "1", "yes")
        if key in ("BROWSER_ENABLED", "CLOUDFLARE_ENABLED") and truthy:
            reg[sp]["transport"] = "browser"
        elif key == "CURL_CFFI_ENABLED" and truthy and reg[sp]["transport"] == "http":
            reg[sp]["transport"] = "curl_cffi"
    conn.close()

    # Optional CSV enrichment (employer name only) — safe if the file is absent.
    if tracking_path and os.path.exists(tracking_path):
        with open(tracking_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sp = (row.get("spider_name") or "").strip()
                if sp in reg:
                    reg[sp]["expected_employer"] = (row.get("employer") or "").strip()
    return reg


def compute_metrics(db_path, project):
    """Per-spider fingerprint: counts, fill rates, description-length stats."""
    conn = connect_ro(db_path)
    rows = conn.execute("""
        SELECT s.name AS spider, i.title, i.metadata_json
        FROM scraped_items i JOIN spiders s ON i.spider_id = s.id
        WHERE s.project = ?
    """, [project]).fetchall()
    # also capture spiders that exist but have zero items
    all_spiders = [r["name"] for r in conn.execute(
        "SELECT name FROM spiders WHERE project = ?", [project])]
    conn.close()

    agg = {sp: {"n": 0, "title_ok": 0, "desc_ok": 0, "employer_ok": 0,
                "location_ok": 0, "desc_lens": []} for sp in all_spiders}

    for r in rows:
        sp = r["spider"]
        b = agg[sp]
        b["n"] += 1
        if r["title"] and r["title"].strip():
            b["title_ok"] += 1
        try:
            meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        for fld, key in (("desc_ok", "description"), ("employer_ok", "employer"),
                         ("location_ok", "location")):
            v = meta.get(key)
            if isinstance(v, str) and v.strip():
                b[fld] += 1
        d = meta.get("description")
        if isinstance(d, str) and d.strip():
            b["desc_lens"].append(len(d.strip()))

    metrics = {}
    for sp, b in agg.items():
        n = b["n"]
        lens = b["desc_lens"]
        metrics[sp] = {
            "n": n,
            "title_fill": round(b["title_ok"] / n, 3) if n else 0.0,
            "desc_fill": round(b["desc_ok"] / n, 3) if n else 0.0,
            "employer_fill": round(b["employer_ok"] / n, 3) if n else 0.0,
            "location_fill": round(b["location_ok"] / n, 3) if n else 0.0,
            "desc_len_median": round(statistics.median(lens), 1) if lens else 0.0,
        }
    return metrics


def cmd_snapshot(args):
    metrics = compute_metrics(args.db, args.project)
    payload = {
        "project": args.project,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "spiders": metrics,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    nonzero = sum(1 for m in metrics.values() if m["n"] > 0)
    print(f"Snapshot written to {args.out}: {len(metrics)} spiders "
          f"({nonzero} with data) for project={args.project}")


# ---- drift classification --------------------------------------------------
# Thresholds — tune in the scheduling note.
VOLUME_DROP_FRAC = 0.5      # current n < 50% of baseline n => volume drop
FILL_DROP_ABS = 0.25        # a required field's fill rate fell by >= 25 pts
DESC_LEN_DROP_FRAC = 0.5    # median description length halved => layout change


def classify(sp, base, cur, reg):
    """Return (verdict, reasons[]). cur/base are metric dicts; reg is the
    DB-derived registry entry {active, transport, ...}."""
    reasons = []
    info = reg.get(sp, {})
    active = info.get("active", True)
    transport = info.get("transport", "http")

    bn, cn = base["n"], cur["n"]

    # Spider vanished from DB entirely but baseline had data.
    if cn == 0 and bn > 0:
        # Distinguish transport vs legit-no-jobs vs extraction.
        # We can't fetch here (compare is DB-only + cheap); we classify on
        # signals and defer the live confirmation to the re-crawl/health step.
        if not active:
            return "NO_DATA", ["0 items; spider is INACTIVE (deactivated — expected)"]
        if transport in ("browser", "curl_cffi"):
            return "NO_DATA", [
                f"0 items vs {bn} in baseline; transport={transport} "
                "(browser/TLS path — likely a transport block, re-crawl to confirm)"]
        return "NO_DATA", [
            f"0 items now vs {bn} in baseline — re-crawl to tell "
            f"transport-block from no-open-roles (transport={transport})"]

    if cn == 0 and bn == 0:
        return "OK", ["no data in baseline or now"]

    # Required-field fill-rate regressions = extraction drift (schema-aware,
    # the thing health misses for jobfeed).
    for fld in ("title_fill", "desc_fill", "employer_fill", "location_fill"):
        drop = base[fld] - cur[fld]
        if drop >= FILL_DROP_ABS:
            reasons.append(f"{fld} {base[fld]:.0%}→{cur[fld]:.0%} (−{drop:.0%})")

    # Description length collapse = layout change even if fields non-empty.
    if base["desc_len_median"] > 0:
        ratio = cur["desc_len_median"] / base["desc_len_median"]
        if ratio < DESC_LEN_DROP_FRAC:
            reasons.append(
                f"desc_len_median {base['desc_len_median']:.0f}→"
                f"{cur['desc_len_median']:.0f} (×{ratio:.2f})")

    # Volume drop (fewer jobs returned). Soft — could be legit.
    volume_dropped = bn > 0 and cn < bn * VOLUME_DROP_FRAC
    if volume_dropped:
        reasons.append(f"volume {bn}→{cn} items")

    # Verdict precedence: a fill/length regression means the extractor is now
    # producing worse rows => EXTRACTION_DRIFT (highest-signal, actionable).
    field_regressed = any(k.endswith("_fill") or k.startswith("desc_len")
                          for k in [r.split()[0] for r in reasons])
    if any("fill" in r for r in reasons) or any("desc_len" in r for r in reasons):
        # If only SOME rows regressed (cur fills middling, not zero) it's partial.
        min_fill = min(cur["title_fill"], cur["desc_fill"], cur["location_fill"])
        if min_fill == 0:
            return "EXTRACTION_DRIFT", reasons
        return "PARTIAL_DRIFT", reasons
    if volume_dropped:
        return "VOLUME_DROP", reasons

    return "OK", reasons or ["within tolerance"]


def cmd_compare(args):
    with open(args.baseline) as f:
        base_doc = json.load(f)
    base = base_doc["spiders"]
    cur = compute_metrics(args.db, args.project)
    reg = load_registry(args.db, args.project, args.tracking)

    base_names = set(base)
    cur_names = set(cur)

    results = []
    for sp in sorted(base_names | cur_names):
        if sp not in base:
            results.append(("NEW", sp, [f"{cur[sp]['n']} items; not in baseline"]))
            continue
        if sp not in cur:
            results.append(("GONE", sp, ["spider removed from DB since baseline"]))
            continue
        verdict, reasons = classify(sp, base[sp], cur[sp], reg)
        results.append((verdict, sp, reasons))

    order = {"EXTRACTION_DRIFT": 0, "PARTIAL_DRIFT": 1, "NO_DATA": 2,
             "FILL_DRIFT": 3, "VOLUME_DROP": 4, "GONE": 5, "NEW": 6, "OK": 7}
    results.sort(key=lambda t: order.get(t[0], 9))

    actionable = [r for r in results if r[0] in (
        "EXTRACTION_DRIFT", "PARTIAL_DRIFT", "NO_DATA", "FILL_DRIFT", "VOLUME_DROP", "GONE")]

    print(f"\nDrift report — project={args.project}  baseline={base_doc.get('created_at','?')}")
    print(f"spiders={len(results)}  actionable={len(actionable)}\n")
    print(f"{'VERDICT':17} SPIDER")
    print("-" * 80)
    for verdict, sp, reasons in results:
        if verdict == "OK" and not args.all:
            continue
        print(f"{verdict:17} {sp}")
        for r in reasons:
            print(f"{'':17}   • {r}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"project": args.project,
                       "baseline_created_at": base_doc.get("created_at"),
                       "compared_at": datetime.now(timezone.utc).isoformat(),
                       "results": [{"verdict": v, "spider": s, "reasons": r}
                                   for v, s, r in results]}, f, indent=2)
        print(f"\nWrote {args.json}")

    sys.exit(1 if actionable else 0)


def main():
    ap = argparse.ArgumentParser(description="jobfeed spider drift/breakage detector")
    ap.add_argument("--db", default="scrapai.db")
    ap.add_argument("--project", default="jobfeed")
    ap.add_argument("--tracking", default="data/jobfeed/tracking.csv",
                    help="OPTIONAL CSV for employer-name enrichment only; the "
                         "registry (active + transport) comes from the DB")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="save a baseline fingerprint")
    s.add_argument("--out", default="qa/baseline_jobfeed.json")
    s.set_defaults(func=cmd_snapshot)

    c = sub.add_parser("compare", help="compare current data to a baseline")
    c.add_argument("--baseline", default="qa/baseline_jobfeed.json")
    c.add_argument("--json", default=None)
    c.add_argument("--all", action="store_true", help="show OK spiders too")
    c.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
