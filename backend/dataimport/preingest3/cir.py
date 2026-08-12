"""
Section 13 — The Canonical Intermediate Representation (CIR).

The complete, normalised, verified dataset for a run. Extraction WRITES to it;
assembly READS ONLY from it and never touches a source file or the model. That
separation is what makes three properties true:
  • Assembly is a pure function — same CIR in, same workbook out.
  • The output template is swappable — a different schema is a different
    assembler over the same CIR, with no extraction change.
  • Extraction and assembly are testable in isolation.

Every figure carries its full lineage — source file fingerprint, sheet, cell,
the labels verified, which signals passed, the native value before
normalisation, the Rate Card applied, and any human approval. Lineage is not a
log written alongside the data; it is a FIELD of the data (build rule via
Section 13).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .quantity import Quantity


# ── signal names (U2) ────────────────────────────────────────────────────
SIG_EXISTENCE = 'existence'
SIG_LABEL = 'label'
SIG_IDENTITY = 'identity'


@dataclass
class Provenance:
    """Where a figure came from and what confirmed it. Attached to every Figure."""
    source_file: str                       # upload label
    content_fingerprint: str               # U3 level-2 hash of the source file
    sheet: str
    cell: str                              # e.g. 'Summary!K14', or '' for derived
    row_label: str = ''                    # what code re-read to the left
    col_label: str = ''                    # what code re-read above
    signals_passed: List[str] = field(default_factory=list)
    derived_from: List[str] = field(default_factory=list)  # cells/concepts, if computed
    approved_by: Optional[str] = None      # reviewer id if human-confirmed
    approved_at: Optional[str] = None
    note: str = ''

    def as_row(self) -> dict:
        return {
            'source_file': self.source_file, 'fingerprint': self.content_fingerprint,
            'sheet': self.sheet, 'cell': self.cell,
            'row_label': self.row_label, 'col_label': self.col_label,
            'signals': ','.join(self.signals_passed),
            'derived_from': ','.join(self.derived_from),
            'approved_by': self.approved_by or '', 'note': self.note,
        }


@dataclass
class Figure:
    """One verified datum: its normalised ₹Cr value, the native typed quantity it
    came from, and its provenance.

    A figure that could not be trusted is NEVER confirmed and must never enter a
    sum as an implicit 0 (the biggest back-half landmine):
      • gap   — the figure is absent in the source (a disclosed gap).
      • held  — extraction/normalisation ESCALATED it (ambiguous period,
                mislabelled unit, dissenting signals). A value may be present for
                the reviewer, but it is NOT trusted, so it is excluded from
                aggregates and any total that depends on it becomes indeterminate.
    Both are disclosed, never silently shipped (guarantee G3)."""
    concept: str
    value_cr: Optional[Decimal]            # normalised to ₹ Crore, or None if gap
    native: Optional[Quantity]             # the pre-normalisation typed quantity
    provenance: Provenance
    gap: bool = False                      # figure absent in source (form='absent')
    held: bool = False                     # escalated — value not trusted, excluded from sums
    basis: Optional[str] = None            # TTM | YTD | FY | partial | point_in_time
    value_basis: Optional[str] = None      # gross/net + denominator for multiples & returns,
                                           # or holding/equity for a fair-value figure. REQUIRED
                                           # on every multiple/return figure (formulas.require_basis)
                                           # — the universal guard against gross-vs-net conflation.
    months: Optional[int] = None           # period length (for basis-aware aggregation)
    stale: bool = False                    # period older than staleness threshold
    estimated_rate: bool = False           # FX used a management-estimate rate
    hold_reason: str = ''

    @property
    def confirmed(self) -> bool:
        return not self.gap and not self.held and self.value_cr is not None


@dataclass
class Record:
    """A logical entity in the CIR — an investment, an LP, a capital call, a
    company KPI row, a line item. `fields` mixes plain identity scalars (names,
    dates, ids) with Figure objects for every number."""
    domain: str
    entity_id: Optional[str] = None        # resolved via Alias Ledger (U4)
    fields: Dict[str, Any] = field(default_factory=dict)   # str -> scalar | Figure

    def value(self, concept, default=None):
        v = self.fields.get(concept)
        if isinstance(v, Figure):
            return v.value_cr if v.confirmed else default
        return v if v is not None else default

    def figures(self) -> List[Figure]:
        return [v for v in self.fields.values() if isinstance(v, Figure)]

    def as_flat_dict(self) -> dict:
        """Render to the plain source->value shape the assembler consumes. Figure
        fields collapse to their normalised ₹Cr value; scalars pass through."""
        out = {}
        for k, v in self.fields.items():
            if isinstance(v, Figure):
                out[k] = round(float(v.value_cr), 6) if v.confirmed else None
            else:
                out[k] = v
        if self.entity_id is not None:
            out.setdefault('entity_id', self.entity_id)
        return out


@dataclass
class CIR:
    """The whole run's verified dataset plus its determinism inputs."""
    records: List[Record] = field(default_factory=list)
    disclosures: List[dict] = field(default_factory=list)   # U7 disclosure rows
    checks: List[dict] = field(default_factory=list)        # U7 hard/soft results
    run_signature: str = ''
    rate_card_id: str = ''
    as_of: str = ''
    unresolved: List[dict] = field(default_factory=list)    # held items → review

    # ── writers (extraction side) ───────────────────────────────────────
    def add(self, record: Record):
        self.records.append(record)

    def disclose(self, kind: str, detail: str, **extra):
        row = {'kind': kind, 'detail': detail}
        row.update(extra)
        self.disclosures.append(row)

    def hold(self, reason: str, **ctx):
        self.unresolved.append({'reason': reason, **ctx})

    # ── readers (assembly side) ─────────────────────────────────────────
    def by_domain(self) -> Dict[str, List[Record]]:
        out: Dict[str, List[Record]] = {}
        for r in self.records:
            out.setdefault(r.domain, []).append(r)
        return out

    def domain_dicts(self) -> Dict[str, List[dict]]:
        """domain -> list of flat dicts, the shape the schema-driven assembler
        already understands. Deterministic order (records are appended in the
        canonical file order established at S1)."""
        out: Dict[str, List[dict]] = {}
        for r in self.records:
            out.setdefault(r.domain, []).append(r.as_flat_dict())
        return out

    def provenance_rows(self) -> List[dict]:
        rows = []
        for r in self.records:
            for f in r.figures():
                row = f.provenance.as_row()
                row.update({'domain': r.domain, 'entity': r.entity_id or '',
                            'concept': f.concept,
                            'value_cr': '' if f.value_cr is None else str(f.value_cr),
                            'gap': f.gap})
                rows.append(row)
        return rows

    @property
    def blocked(self) -> bool:
        """A genuine CONTRADICTION that prevents ANY delivery — a hard check that
        FAILED (two confirmed figures that must be equal are not). Held items and
        indeterminate checks do NOT block: they yield a PARTIAL delivery with
        disclosures (add-on #5 — one held figure/file cannot sink the workbook)."""
        return any(c.get('class') == 'hard' and c.get('status') == 'fail'
                   for c in self.checks)

    @property
    def has_holds(self) -> bool:
        """Held figures, held files, or indeterminate hard checks → the run is
        delivered PARTIAL (not a clean 'verified'), with everything disclosed."""
        if self.unresolved:
            return True
        if any(c.get('class') == 'hard' and c.get('status') == 'indeterminate'
               for c in self.checks):
            return True
        return any(f.held for r in self.records for f in r.figures())
