"""
Phase 6 Extractor — semantic Gemini + deterministic Python row extraction.

One Gemini call classifies sheets (domain, layout, column_map). Python then
reads every row deterministically using the map — zero row-level Gemini
calls, zero truncation, universal across any sheet name / column name /
architecture.

Entry point: run_phase6_import(import_file, progress_cb) — same contract as
run_phase3_import.
"""

from .orchestrator import run_phase6_import

__all__ = ['run_phase6_import']
