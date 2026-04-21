"""
_build_gemini_cache.py
======================
Parses each real company individually through the Gemini pipeline and caches
the resulting `financials` dict as JSON under GEMINI_VALIDATE_CACHE dir
(default /tmp/gemini_cache). Skips companies whose cache already exists, so
re-runs after transient failures are cheap.

Usage (from backend/):
    GEMINI_VALIDATE_CACHE=/tmp/gemini_cache python -m tests._build_gemini_cache
    GEMINI_VALIDATE_CACHE=/tmp/gemini_cache python -m tests._build_gemini_cache analisa cpm
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import django  # noqa: E402
django.setup()

from api.portfolio.company_config import COMPANY_REGISTRY  # noqa: E402
from api.portfolio.generic_parser import parse as gemini_parse  # noqa: E402


def main() -> int:
    targets = sys.argv[1:]
    cache_dir = Path(os.environ.get("GEMINI_VALIDATE_CACHE", "/tmp/gemini_cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    for entry in COMPANY_REGISTRY:
        slug = entry["slug"]
        if targets and slug not in targets:
            continue
        if not entry.get("is_real"):
            continue

        out_path = cache_dir / f"{slug}.json"
        if out_path.exists():
            print(f"[skip] {slug} (cached at {out_path})")
            continue

        filepath = entry["files"][0]["path"]
        if not Path(filepath).exists():
            print(f"[skip] {slug}: file missing {filepath}")
            continue

        print(f"\n[parse] {slug}  ->  {out_path}")
        try:
            result = gemini_parse(
                filepath=filepath,
                company_name=entry["name"],
                company_slug=slug,
                currency=entry.get("currency", "USD"),
                scale=entry.get("hints", {}).get("scale"),
                reporting_period=entry.get("hints", {}).get("reporting_period"),
            )
            out_path.write_text(
                json.dumps(result["financials"], indent=2, default=str)
            )
            fin = result["financials"]
            rev = (fin.get("summary") or {}).get("revenue")
            print(f"  [ok] revenue={rev}  "
                  f"bva={len(fin.get('budget_vs_actual') or [])}  "
                  f"monthly={len(fin.get('monthly_pl') or [])}")
        except Exception as exc:
            print(f"  [fail] {slug}: {exc}")
            traceback.print_exc()
            failures.append(slug)

    if failures:
        print(f"\nFAILED: {failures}")
        return 1
    print("\nAll done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
