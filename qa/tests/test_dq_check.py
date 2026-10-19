#!/usr/bin/env python3
"""
Unit tests for the heuristic data-quality checker (qa/dq_check.py).

These are PURE tests: synthetic field values, no DB, no network, no crawling.
They prove two things the real-data validation could not prove on its own:
  1. the checker FLAGS each class of bad value (true positives), and
  2. the checker stays QUIET on good values (no false positives).

Run:
    .venv/bin/python -m pytest qa/tests/ -q
    # or without pytest:
    .venv/bin/python qa/tests/test_dq_check.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dq_check as dq  # noqa: E402


def codes(issues):
    return {c for _, c, _ in issues}


# ---------------------------------------------------------------------------
# TITLE
# ---------------------------------------------------------------------------
def test_title_good():
    assert dq.check_title("Senior Software Engineer") == []


def test_title_empty_is_critical():
    issues = dq.check_title("")
    assert ("crit", "title_empty", issues[0][2]) == issues[0]


def test_title_none_is_critical():
    assert "title_empty" in codes(dq.check_title(None))


def test_title_junk_label():
    assert "title_junk" in codes(dq.check_title("Apply Now"))


def test_title_too_long_is_sentence():
    assert "title_long" in codes(dq.check_title("x" * 200))


def test_title_nonlatin_flagged():
    # mostly non-Latin script
    assert "title_nonlatin" in codes(dq.check_title("エンジニア募集 シニア"))


def test_title_international_but_latin_ok():
    # accents are fine — must NOT flag (real HUGO BOSS French title)
    assert dq.check_title("Contrôleur de Gestion France - CDD 9 mois (H/F)") == []


# ---------------------------------------------------------------------------
# DESCRIPTION
# ---------------------------------------------------------------------------
def test_description_good_long():
    assert dq.check_description("A real job description. " * 50) == []


def test_description_empty_is_critical():
    assert "desc_empty" in codes(dq.check_description(""))


def test_description_too_short_is_critical():
    assert "desc_too_short" in codes(dq.check_description("Great job!"))


def test_description_stub_is_warn():
    # between DESC_MIN_LEN (120) and DESC_STUB_LEN (250) => warn, not critical
    text = ("We are hiring a registered nurse for the day shift at our downtown "
            "clinic. Apply today to join a supportive and growing care team here.")
    assert 120 <= len(text) < 250, len(text)
    assert "desc_stub" in codes(dq.check_description(text))


def test_description_boilerplate_opening_flagged():
    text = "We use cookies to improve your experience. " + ("filler " * 60)
    assert "desc_boilerplate" in codes(dq.check_description(text))


def test_description_privacy_in_footer_not_flagged():
    # long, real description that merely mentions privacy policy at the end.
    # This is the false positive we fixed — must stay quiet.
    text = ("About the role. " * 100) + " See our privacy policy for details."
    assert "desc_boilerplate" not in codes(dq.check_description(text))


# ---------------------------------------------------------------------------
# EMPLOYER
# ---------------------------------------------------------------------------
def test_employer_good():
    assert dq.check_employer("Hugo Boss", "careers_hugoboss_com") == []


def test_employer_empty_is_critical():
    assert "employer_empty" in codes(dq.check_employer(None, "x"))


def test_employer_sentence_flagged():
    sentence = "We are a leading global provider of innovative solutions for enterprises"
    assert "employer_sentence" in codes(dq.check_employer(sentence, "x"))


def test_employer_acronym_vs_expansion_is_info_not_warn():
    # 'IJM' expected, data says the full name — same company, must be info only.
    issues = dq.check_employer("International Justice Mission", "ijm", "IJM")
    sev_for_mismatch = [s for s, c, _ in issues if c == "employer_mismatch"]
    # either no mismatch raised, or raised only at info level (never warn/crit)
    assert all(s == "info" for s in sev_for_mismatch)


# ---------------------------------------------------------------------------
# LOCATION
# ---------------------------------------------------------------------------
def test_location_good_city_state():
    assert dq.check_location("Chester, New York") == []


def test_location_empty_is_critical():
    assert "location_empty" in codes(dq.check_location(""))


def test_location_workplace_flag_flagged():
    assert "location_is_flag" in codes(dq.check_location("Hybrid"))
    assert "location_is_flag" in codes(dq.check_location("Remote"))


def test_location_street_address_flagged():
    # the real target_wd5 / aah defect: street address instead of City, ST
    assert "location_street_address" in codes(
        dq.check_location("1080 Third Crossing Way, Mebane,NC 27302"))
    assert "location_street_address" in codes(
        dq.check_location("Advocate Trinity Hospital - 2320 E 93rd St"))


def test_location_region_hybrid_combo_not_street():
    # 'United States - Remote' / 'Virginia - Hybrid' are region+flag, not a
    # street address — must not raise the street-address signal.
    assert "location_street_address" not in codes(
        dq.check_location("United States - Remote"))


def test_location_plain_city_not_street():
    assert "location_street_address" not in codes(dq.check_location("London"))


# ---------------------------------------------------------------------------
# SCORING / TRIAGE
# ---------------------------------------------------------------------------
def test_scoring_critical_when_many_bad_rows():
    # 8 of 15 rows critical => CRITICAL (the hugoboss shape)
    bucket = {"rows": 15, "crit_rows": 8, "warn_rows": 0}
    _, verdict = dq.score_spider(bucket)
    assert verdict == "CRITICAL"


def test_scoring_review_when_some_warn_rows():
    bucket = {"rows": 4, "crit_rows": 0, "warn_rows": 2}
    _, verdict = dq.score_spider(bucket)
    assert verdict in ("REVIEW", "WATCH")  # 2/4 warn -> density 1.5 -> REVIEW


def test_scoring_ok_when_clean():
    bucket = {"rows": 10, "crit_rows": 0, "warn_rows": 0}
    _, verdict = dq.score_spider(bucket)
    assert verdict == "OK"


# ---------------------------------------------------------------------------
# tiny runner so it works without pytest installed
# ---------------------------------------------------------------------------
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
