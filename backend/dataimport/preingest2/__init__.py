"""
preingest2 — Pre-Ingestion & Consolidation Layer, faithful to the v1.0 design
document (Stages 0-8 + semantic chunking + golden-record cache).

Lightweight, in-process implementation intended for design validation before
productionization:
  • disk-backed golden-record cache   (stands in for Postgres D3)
  • thread-pool per-file workers       (stands in for Temporal/Prefect)
  • Python recalculation spot-checks   (stands in for LibreOffice recalc)

Everything else follows the document exactly:
  S0 schema · S1 profile · S2 classify · S3 extract (semantic chunking, provenance,
  freeze) · S4 normalize · S5 map · S6 reconcile · S7 assemble · S8 verify.

Core principle (document): push work out of the model into deterministic code;
call the model once per unique file, freeze and re-serve. The model only
classifies/locates/reads; code parses, computes, reconciles, assembles, verifies.
"""

PIPELINE_VERSION = 'v2.0.0'
