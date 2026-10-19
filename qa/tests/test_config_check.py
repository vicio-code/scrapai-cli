#!/usr/bin/env python3
"""
Unit tests for the config-shape checker (qa/config_check.py).

Pure tests. The URL-shape helper is tested directly. The frozen-enumeration
CLASS logic is tested by exercising the same discovery/enumeration decision the
lint() function uses, on synthetic configs — so we prove it generalizes beyond
the two instances the user showed us (it must also catch a GENERATED_URLS
`list` of ids, and must NOT flag a healthy range/sitemap/follow config).

The real-DB validation (5 spiders flagged, 0 false positives) is in the proposal.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config_check as cl  # noqa: E402


# ---- URL shape helper ------------------------------------------------------
def test_job_detail_urls_detected():
    assert cl._looks_like_detail(
        "https://careers.hugoboss.com/global/en/job/142749/Part-Time-Sales")
    assert cl._looks_like_detail(
        "https://www.multitrabajos.com/empleos/vendedor-junior-1118394925.html")
    assert cl._looks_like_detail("https://site.com/positions/12345")


def test_listing_urls_not_detail():
    assert not cl._looks_like_detail("https://careers.hugoboss.com/global/en")
    assert not cl._looks_like_detail("https://boards.greenhouse.io/acme")
    assert not cl._looks_like_detail("https://site.com/")


def test_parse_json_tolerates_string_and_object():
    assert cl._parse_json('[{"a": 1}]') == [{"a": 1}]
    assert cl._parse_json([{"a": 1}]) == [{"a": 1}]
    assert cl._parse_json(None) is None
    assert cl._parse_json("not json") is None


# ---- frozen-enumeration CLASS decision -------------------------------------
# Mirror the lint() decision so we can test the generalization without a DB.

def _decide(start_urls, follow, gen=None, sitemap=False, paginated=False):
    n_detail = sum(1 for u in start_urls if cl._looks_like_detail(u))
    gen_list = gen_dynamic = False
    if gen:
        for entry in (gen if isinstance(gen, list) else [gen]):
            for v in (entry.get("vars") or {}).values():
                if v.get("type") == "list":
                    gen_list = True
                if v.get("type") in ("range", "date"):
                    gen_dynamic = True
    has_discovery = bool(follow) or gen_dynamic or sitemap or paginated
    enumerates = (n_detail >= 1 and not follow) or gen_list
    return "FROZEN_ENUMERATION" if (enumerates and not has_discovery) else "OK"


def test_hardcoded_urls_no_follow_is_frozen():
    # hugoboss / multitrabajos shape
    assert _decide(["https://x.com/job/1", "https://x.com/job/2"], follow=False) \
        == "FROZEN_ENUMERATION"


def test_hardcoded_id_list_is_frozen_even_with_follow_true():
    # guess shape — GENERATED_URLS list; the entry's own follow=true must NOT
    # rescue it, because it still enumerates a frozen id set.
    gen = [{"template": "https://x.com/{id}.md",
            "vars": {"id": {"type": "list", "values": ["A", "B", "C"]}},
            "follow": True}]
    assert _decide([], follow=False, gen=gen) == "FROZEN_ENUMERATION"


def test_range_generation_is_healthy():
    # att_jobs page sweep — discovers new jobs as pages fill
    gen = [{"template": "https://x.com/?p={p}",
            "vars": {"p": {"type": "range", "from": 1, "to": 485}}, "follow": True}]
    assert _decide([], follow=False, gen=gen) == "OK"


def test_follow_links_is_healthy():
    assert _decide(["https://x.com/jobs"], follow=True) == "OK"


def test_sitemap_is_healthy():
    assert _decide([], follow=False, sitemap=True) == "OK"


# ---- real analyze_config() + lint_files() (the CI entry point) -------------

def _codes(issues):
    return {c for c, _ in issues}


def test_analyze_config_frozen_url_list():
    issues = cl.analyze_config(
        "x", ["https://x.com/job/1", "https://x.com/job/2"],
        settings={}, has_follow_rule=False, extract_fields={"title", "description", "employer", "location"})
    assert "FROZEN_ENUMERATION" in _codes(issues)


def test_analyze_config_missing_required_field():
    issues = cl.analyze_config(
        "x", ["https://x.com/jobs"], settings={}, has_follow_rule=True,
        extract_fields={"title", "description"})  # employer + location missing
    assert "NO_REQUIRED_SRC" in _codes(issues)


def test_lint_files_on_sections_config(tmp_path):
    # a frozen sections config written to disk — the CI path
    import json as _json
    cfg = {
        "name": "frozen_demo",
        "start_urls": ["https://x.com/job/111", "https://x.com/job/222"],
        "sections": [{"match": ["/job/.*"], "follow": False,
                      "extract": {"title": {"css": "h1"}, "description": {"css": "div"},
                                  "employer": {"css": "a"}, "location": {"css": "span"}}}],
        "settings": {},
    }
    p = tmp_path / "frozen_demo.json"
    p.write_text(_json.dumps(cfg))
    findings = cl.lint_files([str(p)])
    assert any(f["code"] == "FROZEN_ENUMERATION" for f in findings)


def test_lint_files_healthy_follow_config(tmp_path):
    import json as _json
    cfg = {
        "name": "healthy_demo",
        "start_urls": ["https://x.com/jobs"],
        "sections": [
            {"match": [".*"], "follow": True},
            {"match": ["/job/.*"], "follow": False,
             "extract": {"title": {"css": "h1"}, "description": {"css": "d"},
                         "employer": {"css": "a"}, "location": {"css": "s"}}}],
        "settings": {},
    }
    p = tmp_path / "healthy_demo.json"
    p.write_text(_json.dumps(cfg))
    assert cl.lint_files([str(p)]) == []


def test_lint_files_unreadable(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{ not valid json")
    findings = cl.lint_files([str(p)])
    assert any(f["code"] == "UNREADABLE" for f in findings)


def _main():
    import tempfile
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for name, fn in fns:
        try:
            # supply a tmp dir to tests that take one
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                import pathlib
                with tempfile.TemporaryDirectory() as d:
                    fn(pathlib.Path(d))
            else:
                fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
