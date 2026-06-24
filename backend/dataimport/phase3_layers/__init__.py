"""
Phase 3 — Flavor A (semantic layering) + Flavor B (inner row chunking).

Replaces the Phase 1 multi-pass chain AND the Phase 2 single-call extractor.
Always runs 3 parallel Gemini calls (Layer 1 Identity / Layer 2 Universe /
Layer 3 Time-series). Any layer whose estimated output exceeds the budget
sub-chunks via Flavor B — those sub-calls run in the SAME ThreadPoolExecutor,
so wall time stays at MAX(individual call durations), not SUM.

Entry point: run_phase3_import(import_file, progress_cb)
"""

from .orchestrator import run_phase3_import

__all__ = ['run_phase3_import']
