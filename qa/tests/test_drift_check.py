#!/usr/bin/env python3
"""
Unit tests for the drift classifier (qa/drift_check.py).

Pure tests: synthetic baseline vs current metric dicts, no DB, no network.
They prove the classifier distinguishes the drift types the proposal claims:
extraction break vs partial drift vs transport block vs legit-no-jobs vs OK.

Run:
    .venv/bin/python -m pytest qa/tests/ -q
    .venv/bin/python qa/tests/test_drift_check.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import drift_check as dc  # noqa: E402


def metrics(n, title=1.0, desc=1.0, emp=1.0, loc=1.0, dlen=2000.0):
    return {"n": n, "title_fill": title, "desc_fill": desc,
            "employer_fill": emp, "location_fill": loc, "desc_len_median": dlen}


HEALTHY = metrics(10)


def test_no_change_is_ok():
    v, _ = dc.classify("sp", HEALTHY, metrics(10), reg={})
    assert v == "OK"


def test_total_extraction_break():
    # every required field collapsed to 0 fill => EXTRACTION_DRIFT
    cur = metrics(10, title=0.0, desc=0.0, loc=0.0, dlen=0.0)
    v, reasons = dc.classify("sp", HEALTHY, cur, reg={})
    assert v == "EXTRACTION_DRIFT", (v, reasons)


def test_partial_drift_masked_by_constant_employer():
    # the hugoboss signature: title/desc/location drop to ~0.47, employer stays
    cur = metrics(15, title=0.47, desc=0.47, loc=0.47, emp=1.0)
    base = metrics(15)
    v, reasons = dc.classify("sp", base, cur, reg={})
    assert v == "PARTIAL_DRIFT", (v, reasons)
    assert any("fill" in r for r in reasons)


def test_zero_items_blocked_is_transport():
    # baseline had data, now 0, spider runs through browser => transport signal
    reg = {"sp": {"active": True, "transport": "browser"}}
    v, reasons = dc.classify("sp", HEALTHY, metrics(0), reg=reg)
    assert v == "NO_DATA"
    assert any("transport" in r for r in reasons)


def test_zero_items_inactive_is_expected():
    # baseline had data, now 0, spider deactivated => expected, not a bug
    reg = {"sp": {"active": False, "transport": "http"}}
    v, reasons = dc.classify("sp", HEALTHY, metrics(0), reg=reg)
    assert v == "NO_DATA"
    assert any("INACTIVE" in r or "inactive" in r.lower() for r in reasons)


def test_zero_items_http_active_asks_for_recrawl():
    # baseline had data, now 0, plain-http active spider => NO_DATA, re-crawl
    reg = {"sp": {"active": True, "transport": "http"}}
    v, reasons = dc.classify("sp", HEALTHY, metrics(0), reg=reg)
    assert v == "NO_DATA"
    assert any("re-crawl" in r for r in reasons)


def test_zero_both_sides_is_ok():
    # never had data, still none => not actionable
    v, _ = dc.classify("sp", metrics(0), metrics(0), reg={})
    assert v == "OK"


def test_volume_drop_without_fill_change():
    # same fills, far fewer items, no fill regression => VOLUME_DROP (soft)
    v, reasons = dc.classify("sp", metrics(100), metrics(20), reg={})
    assert v == "VOLUME_DROP", (v, reasons)


def test_description_length_collapse_is_drift():
    # fills fine but description bodies halved => layout change
    cur = metrics(10, dlen=500.0)   # baseline dlen 2000 -> 0.25 ratio
    v, reasons = dc.classify("sp", HEALTHY, cur, reg={})
    assert v in ("EXTRACTION_DRIFT", "PARTIAL_DRIFT")
    assert any("desc_len" in r for r in reasons)


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
