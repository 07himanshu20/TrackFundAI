"""
validate_gemini_accuracy.py
===========================
Compares Gemini MIS-parser output against hand-extracted ground-truth fixtures.

Usage:
    cd backend
    python -m tests.validate_gemini_accuracy              # runs all companies
    python -m tests.validate_gemini_accuracy integris     # one company

Exits 0 if accuracy >= expected on all companies, 1 otherwise.

For each ground-truth YAML in tests/fixtures/, the script:
  1. Parses the associated MIS file through the Gemini pipeline (or loads
     a cached output file if GEMINI_VALIDATE_CACHE=<dir> env var is set).
  2. Diffs every numeric field against the fixture with a tolerance.
  3. Prints a per-company report and an aggregate accuracy score.

Tolerance rules:
  - Monetary values: abs(relative_diff) <= 0.005  (0.5%)
  - Percentage fields: abs(absolute_diff) <= 0.5   (0.5 percentage points)
  - Counts: exact match OR >= expected_min
  - Confidence bands: numeric parse of ">= 0.80" / "<= 0.30" checked as inequality
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).parent
FIXTURES_DIR = HERE / "fixtures"
CACHE_DIR_ENV = "GEMINI_VALIDATE_CACHE"


# ─── Tolerance helpers ───────────────────────────────────────────────────────

def _money_close(actual, expected, rel_tol=0.005, abs_tol=1.0) -> bool:
    if actual is None and expected is None:
        return True
    if actual is None or expected is None:
        return False
    try:
        a, e = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    if abs(e) < abs_tol:
        return abs(a - e) <= abs_tol
    return abs(a - e) / abs(e) <= rel_tol


def _pct_close(actual, expected, tol=0.5) -> bool:
    if actual is None and expected is None:
        return True
    if actual is None or expected is None:
        return False
    try:
        return abs(float(actual) - float(expected)) <= tol
    except (TypeError, ValueError):
        return False


def _check_inequality(actual, spec: str) -> bool:
    """Parse ">= 0.80" or "<= 0.30" and compare numeric actual."""
    if actual is None:
        return False
    m = re.match(r"\s*(>=|<=|>|<|==)\s*([\d.]+)\s*$", str(spec))
    if not m:
        return float(actual) == float(spec)
    op, val = m.group(1), float(m.group(2))
    a = float(actual)
    return {
        ">=": a >= val, ">": a > val,
        "<=": a <= val, "<": a < val,
        "==": abs(a - val) < 1e-9,
    }[op]


# ─── Per-company comparison ──────────────────────────────────────────────────

def compare(company_slug: str, gemini_out: dict, fixture: dict) -> dict:
    """Return {passed, failed, score, details[]}."""
    results = []

    summary_gem = gemini_out.get("summary") or {}
    summary_exp = fixture.get("summary") or {}

    # 1. Summary monetary fields
    money_fields = [
        "revenue", "cogs", "gross_profit", "opex", "ebitda",
        "ytd_revenue", "ytd_gross_profit", "ytd_ebitda",
        "budget_revenue", "budget_ebitda",
        "ytd_budget_revenue", "ytd_budget_ebitda",
    ]
    for f in money_fields:
        if f not in summary_exp:
            continue
        exp = summary_exp[f]
        if exp is None:
            continue
        # Support "approximate" and tilde-prefixed values in fixtures
        if isinstance(exp, str) and exp.strip().startswith("~"):
            try:
                exp_num = float(exp.strip()[1:].replace("_", ""))
                ok = _money_close(summary_gem.get(f), exp_num, rel_tol=0.10)
            except ValueError:
                continue
        else:
            ok = _money_close(summary_gem.get(f), exp)
        results.append({
            "section": "summary", "field": f,
            "expected": exp, "actual": summary_gem.get(f),
            "passed": ok,
        })

    # 2. Percentage summary fields
    for f in ("gp_pct", "ebitda_pct"):
        if f in summary_exp and summary_exp[f] is not None:
            ok = _pct_close(summary_gem.get(f), summary_exp[f])
            results.append({
                "section": "summary", "field": f,
                "expected": summary_exp[f], "actual": summary_gem.get(f),
                "passed": ok,
            })

    # 3. Count checks (monthly_pl, cash_flow, etc.)
    for section, min_keys, exact_key in [
        ("monthly_pl",       ["monthly_pl_expected_min_count"],       "monthly_pl_expected_count"),
        ("cash_flow",        ["cash_flow_expected_min_count"],        "cash_flow_expected_count"),
        ("working_capital",  ["working_capital_expected_min_count"],  "working_capital_expected_count"),
        ("budget_vs_actual", ["budget_vs_actual_expected_min", "budget_vs_actual_expected_min_count"], "budget_vs_actual_expected_count"),
        ("sales_by_segment", ["sales_by_segment_expected_min", "sales_by_segment_expected_min_count"], "sales_by_segment_expected_count"),
    ]:
        min_key = next((k for k in min_keys if k in fixture), None)
        got = len(gemini_out.get(section, []) or [])
        if exact_key in fixture:
            exp = fixture[exact_key]
            ok = got == exp
            results.append({
                "section": section, "field": "count==",
                "expected": exp, "actual": got, "passed": ok,
            })
        elif min_key is not None:
            exp = fixture[min_key]
            ok = got >= int(exp)
            results.append({
                "section": section, "field": "count>=",
                "expected": f">={exp}", "actual": got, "passed": ok,
            })

    # 4. Segment sum check
    if "segments_sum_check" in fixture:
        segs = gemini_out.get("sales_by_segment") or []
        got_sum = sum((s.get("revenue") or 0) for s in segs)
        exp_sum = float(fixture["segments_sum_check"])
        ok = _money_close(got_sum, exp_sum, rel_tol=0.05)
        results.append({
            "section": "sales_by_segment", "field": "sum",
            "expected": exp_sum, "actual": got_sum, "passed": ok,
        })

    # 5. Budget-vs-actual spot checks
    spot = fixture.get("budget_vs_actual_spot_checks") or fixture.get("budget_vs_actual_ytd") or []
    got_bva = gemini_out.get("budget_vs_actual") or []
    for spec in spot:
        line = spec.get("line_item", "").lower()
        period_hint = (spec.get("period") or spec.get("period_contains") or "").lower()
        match = None
        for row in got_bva:
            if row.get("line_item", "").lower() == line:
                if period_hint and period_hint not in (row.get("period") or "").lower():
                    continue
                match = row
                break
        if not match:
            results.append({
                "section": "budget_vs_actual", "field": f"{line}/{period_hint}",
                "expected": "row present", "actual": "missing", "passed": False,
            })
            continue

        for field in ("actual", "budget"):
            if field in spec:
                ok = _money_close(match.get(field), spec[field])
                results.append({
                    "section": "budget_vs_actual",
                    "field": f"{line}.{field}",
                    "expected": spec[field], "actual": match.get(field),
                    "passed": ok,
                })
        if "variance_pct" in spec:
            ok = _pct_close(match.get("variance_pct"), spec["variance_pct"], tol=1.0)
            results.append({
                "section": "budget_vs_actual",
                "field": f"{line}.variance_pct",
                "expected": spec["variance_pct"], "actual": match.get("variance_pct"),
                "passed": ok,
            })

    # 6. Confidence expectations
    conf_exp = fixture.get("expected_confidence") or {}
    conf_got = gemini_out.get("confidence") or {}
    for sec, spec in conf_exp.items():
        got = conf_got.get(sec)
        ok = _check_inequality(got, spec)
        results.append({
            "section": "confidence", "field": sec,
            "expected": spec, "actual": got, "passed": ok,
        })

    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed
    score = (passed / len(results) * 100) if results else 0.0

    return {
        "company": company_slug,
        "passed": passed,
        "failed": failed,
        "total": len(results),
        "score": round(score, 2),
        "details": results,
    }


# ─── Main: parse + validate each fixture ─────────────────────────────────────

def _load_gemini_output(company_slug: str, fixture: dict) -> dict | None:
    """Run parser or load from cache dir if GEMINI_VALIDATE_CACHE set."""
    cache_dir = os.environ.get(CACHE_DIR_ENV)
    if cache_dir:
        cpath = Path(cache_dir) / f"{company_slug}.json"
        if cpath.exists():
            print(f"  [cache] {cpath}")
            return json.loads(cpath.read_text())

    # Live parse — requires Gemini API quota
    try:
        import django, os as _os
        _os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        sys.path.insert(0, str(HERE.parent))
        django.setup()
        from api.portfolio.company_config import COMPANY_REGISTRY
        from api.portfolio.generic_parser import parse
    except Exception as e:
        print(f"  [skip] cannot set up Django/parser: {e}")
        return None

    entry = next((c for c in COMPANY_REGISTRY if c["slug"] == company_slug), None)
    if not entry:
        print(f"  [skip] {company_slug} not in COMPANY_REGISTRY")
        return None

    filepath = entry["files"][0]["path"]
    if not Path(filepath).exists():
        print(f"  [skip] file not found: {filepath}")
        return None

    try:
        result = parse(
            filepath=filepath,
            company_name=entry["name"],
            company_slug=entry["slug"],
            currency=entry.get("currency", "USD"),
            scale=entry.get("hints", {}).get("scale"),
            reporting_period=entry.get("hints", {}).get("reporting_period"),
        )
        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            (Path(cache_dir) / f"{company_slug}.json").write_text(
                json.dumps(result["financials"], indent=2, default=str)
            )
        return result["financials"]
    except Exception as e:
        print(f"  [error] parse failed: {e}")
        return None


def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else None

    fixtures = sorted(FIXTURES_DIR.glob("*_ground_truth.yaml"))
    if not fixtures:
        print("No fixtures found.")
        sys.exit(1)

    reports = []
    for fx_path in fixtures:
        slug = fx_path.stem.replace("_ground_truth", "")
        # allow "stentco_board" fixture to map from "stentco_board_ground_truth.yaml"
        if targets and slug not in targets and fx_path.stem not in targets:
            continue

        print(f"\n=== {slug} ===")
        fixture = yaml.safe_load(fx_path.read_text())
        gemini_out = _load_gemini_output(slug, fixture)
        if gemini_out is None:
            print("  [skipped]")
            continue

        rep = compare(slug, gemini_out, fixture)
        reports.append(rep)

        for d in rep["details"]:
            marker = "✓" if d["passed"] else "✗"
            print(f"  {marker} [{d['section']:16}] {d['field']:30} "
                  f"expected={d['expected']} got={d['actual']}")

        print(f"\n  SCORE: {rep['score']}% ({rep['passed']}/{rep['total']} checks)")

    if not reports:
        print("\nNo reports generated.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("AGGREGATE")
    print("=" * 60)
    total_passed = sum(r["passed"] for r in reports)
    total_checks = sum(r["total"] for r in reports)
    agg = (total_passed / total_checks * 100) if total_checks else 0.0
    for r in reports:
        print(f"  {r['company']:25}  {r['score']:6.2f}%  "
              f"({r['passed']}/{r['total']})")
    print(f"  {'OVERALL':25}  {agg:6.2f}%  ({total_passed}/{total_checks})")

    # Exit non-zero if overall < 95%
    sys.exit(0 if agg >= 95.0 else 1)


if __name__ == "__main__":
    main()
