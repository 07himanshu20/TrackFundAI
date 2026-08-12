"""
preingest3 — Pre-Ingestion & Consolidation Layer, Technical Design v2.0.

A fan-out of per-file workers into a deterministic reduction. Supersedes
preingest2. The governing sentence of the design:

    The model is a POINTER, not a source of numbers; correctness is established
    by deterministic evidence the model never saw; and every unique file shape
    is understood once and reused forever.

Stage map (S0–S9); only S3/S4 touch the model, both cache-gated:
  S0 contract     — pinned output schema + check catalogue + lexicon + tolerances
  S1 identity     — byte / content / layout fingerprints; Rate Card coverage; sort
  S2 profiler     — bounded structural map per sheet (addresses, never bulk cells)
  S3 classifier   — fund / mis / UNRECOGNISED (model)
  S4 locator      — addresses + claimed labels + declared units (model, never values)
  S5 read+triangulate — open cells; existence / label / identity signals (code)
  S6 alias ledger — entity resolution (code, model only for bounded adjudication)
  S7 normalise    — Rate Card + Period Algebra, total function, Decimal (code)
  S8 reconcile    — hard / soft / disclosure check catalogue (code)
  S9 assemble     — CIR → workbook, headless recalc release gate (code)

Everything downstream of extraction reads only the Canonical Intermediate
Representation (cir.py) — never a source file, never the model.
"""

PACKAGE_VERSION = 'preingest3-2.0.0'
