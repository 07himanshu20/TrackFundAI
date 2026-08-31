"""
Finance extension — SEBI compliance-RECONCILIATION (tie, not count).

Turns the SEBI compliance work from extract-as-disclosure into a UNIVERSAL, DECLARATIVE,
category-aware reconciliation: rules are DATA, one engine, fail-closed. Four ratified principles
(sebi_compliance_universal_solution.md):

  P1  No tie without a source.  Encode a tie only where the field is reported for this category AND
      backed by a real independent figure in the data. Never invent a synthetic tie. (Dropping AUM
      generalised: a reported field with no workbook source → MISSING-SOURCE hold, not a fabrication.)
  P2  Category-scoped, field-granular.  Rules are scoped by AIF category and reference the specific
      SEBI regulation, never a generic label. A Cat II fund runs Cat II's rules; Cat III ≠ Cat II.
  P3  Basis is declared, never inferred.  Every rule states its exact measurement basis. The
      concentration tie is invested COST ÷ investable funds — NEVER fair-value ÷ total FV (which gives
      a false 27.9% breach). The FV-basis trap is impossible by construction.
  P4  Check, never fit.  A reported compliance figure is what the INDEPENDENTLY-computed value is
      checked against — never a target we reverse-engineer an input to hit. `investable_funds` is
      supplied independently (corpus − estimated expenditure per Reg 2(1)(p)) or the tie stays HELD;
      it is never back-solved from the reported 11.6%. (Same epistemics as the waterfall's 61.6.)
  P5  Fail-closed + disclosed.  Missing source, unresolved required input, or missing filing →
      HELD / ABSENT + flag; never defaulted. New outputs ride the spine as SOFT reconciliation rows.

Adding a fund or a category = adding rows to the registry, never touching the engine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from . import lexicon


# ══════════════════════════════════════════════════════════════════════════════════
# 1. The compliance-rule registry — rules as DATA
# ══════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ComplianceRule:
    id: str
    reg_ref: str                     # canonical SEBI reference substring, e.g. '10(b)' (the row locator)
    category_scope: Tuple[str, ...]  # ('II',) — P2
    requirement: str                 # human label (for the emitted row)
    norm_op: str                     # '>=' | '<='
    threshold: Optional[Decimal]     # in the rule's value_type units
    source_kind: str                 # how to compute the INDEPENDENT value from the CIR
    value_type: str                  # 'amount_cr' | 'count' | 'fraction' | 'years' — parse + compare basis
    basis_note: str                  # P3 — explicit numerator/denominator definition
    threshold_alt: Optional[Decimal] = None   # dual norm (sponsor ≥ 2.5% OR ≥ ₹5 Cr)
    requires: Tuple[str, ...] = ()   # external inputs; unresolved → HELD (P4/P5)


# Cat II rule-set (the six verified ties) — authored as DATA
CAT_II_RULES: Tuple[ComplianceRule, ...] = (
    ComplianceRule('min_corpus', '10(b)', ('II',), 'Minimum corpus', '>=', Decimal('20'),
                   'corpus', 'amount_cr', 'Σ LP commitments (LP register total)'),
    ComplianceRule('min_investor_commitment', '10(c)', ('II',), 'Min investor commitment', '>=', Decimal('1'),
                   'min_commitment', 'amount_cr', 'min over per-LP commitments'),
    ComplianceRule('max_investors', '10(f)', ('II',), 'Max investors per scheme', '<=', Decimal('1000'),
                   'lp_count', 'count', 'count of LP register rows'),
    ComplianceRule('sponsor_interest', '10(d)', ('II',), 'Sponsor continuing interest', '>=', Decimal('0.025'),
                   'sponsor_frac', 'fraction', 'GP/sponsor commitment ÷ corpus (or ≥ ₹5 Cr)',
                   threshold_alt=Decimal('5')),
    ComplianceRule('concentration', '15(1)(c)', ('II',), 'Concentration in one investee', '<=', Decimal('0.25'),
                   'concentration', 'fraction',
                   'largest invested COST ÷ investable_funds — NEVER fair-value ÷ total FV (P3)',
                   requires=('investable_funds',)),
    ComplianceRule('min_tenure', '13(a)', ('II',), 'Close-ended minimum tenure', '>=', Decimal('3'),
                   'tenure_years', 'years', 'fund term from the LPA'),
)

REGISTRY: Dict[str, Tuple[ComplianceRule, ...]] = {'II': CAT_II_RULES}


@dataclass
class CompliancePrimitives:
    """The INDEPENDENT source figures, computed from the CIR (never from the compliance remark)."""
    category: Optional[str] = None
    corpus_cr: Optional[Decimal] = None
    commitments_cr: Tuple[Decimal, ...] = ()
    lp_count: Optional[int] = None
    sponsor_commitment_cr: Optional[Decimal] = None
    largest_cost_cr: Optional[Decimal] = None
    investable_funds_cr: Optional[Decimal] = None   # external (Reg 2(1)(p)); None → concentration HELD
    tenure_years: Optional[Decimal] = None

    def independent(self, rule: ComplianceRule) -> Optional[Decimal]:
        k = rule.source_kind
        if k == 'corpus':
            return self.corpus_cr
        if k == 'min_commitment':
            return min(self.commitments_cr) if self.commitments_cr else None
        if k == 'lp_count':
            return Decimal(self.lp_count) if self.lp_count is not None else None
        if k == 'sponsor_frac':
            if self.sponsor_commitment_cr is None or not self.corpus_cr:
                return None
            return self.sponsor_commitment_cr / self.corpus_cr
        if k == 'concentration':
            if self.investable_funds_cr is None or self.largest_cost_cr is None:
                return None                               # requires unmet → HELD (never FV-basis, never back-solve)
            return self.largest_cost_cr / self.investable_funds_cr
        if k == 'tenure_years':
            return self.tenure_years
        return None


# ══════════════════════════════════════════════════════════════════════════════════
# 2. Locating the reported side — the compliance CHECKLIST (kept apart from the calendar)
# ══════════════════════════════════════════════════════════════════════════════════
_PCT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*%')
_NUM_RE = re.compile(r'(\d+(?:,\d+)*(?:\.\d+)?)')


def _num(s: str) -> Optional[Decimal]:
    m = _NUM_RE.search(s or '')
    return Decimal(m.group(1).replace(',', '')) if m else None


def parse_reported(remark: str, value_type: str) -> Optional[Decimal]:
    """Parse the reported figure out of a free-text compliance remark, per the rule's value_type.
    Universal (first-number-of-the-right-kind), never a fixed cell."""
    remark = remark or ''
    if value_type == 'fraction':
        m = _PCT_RE.search(remark)
        return Decimal(m.group(1)) / Decimal('100') if m else None
    return _num(remark)                                   # amount_cr | count | years → first bare number


_REF_WORDS = re.compile(r'\b(reg|regulation|regn|rule|cir|circular)\b')
_REF_STRIP = re.compile(r'[^a-z0-9]')


def _norm_ref(s) -> str:
    """Canonicalise a SEBI regulation reference for matching. UNLIKE lexicon.normalise_label — which
    STRIPS parenthetical notes — this PRESERVES the parenthetical, because for a reg ref the '(b)' in
    '10(b)' is the discriminator, not a note. 'Reg 10(b)' → '10b'; '15(1)(c)' → '151c'. (Reusing the
    label normaliser here collapsed 10(b)/(c)/(d)/(f) all to '10' → wrong-row matches.)"""
    s = _REF_WORDS.sub(' ', str(s or '').lower())
    return _REF_STRIP.sub('', s)


def detect_category(grids: List[dict]) -> Optional[str]:
    """AIF category from the compliance title or the terms 'legal structure' — 'Cat II' → 'II'.
    Fail-closed: undetectable → None (the engine then holds every rule, never guesses a rule-set)."""
    txt = ' '.join(str(v) for grid in grids for rows in grid.values() for row in rows
                   for v in row if isinstance(v, str)).lower()
    for cat, keys in (('III', ('cat iii', 'category iii', 'cat-iii')),
                      ('II', ('cat ii', 'category ii', 'cat-ii')),
                      ('I', ('cat i', 'category i', 'cat-i'))):
        if any(k in txt for k in keys):
            return cat
    return None


def _find_checklist_rows(grids: List[dict]) -> Dict[str, dict]:
    """The compliance CHECKLIST rows keyed by SEBI ref. Distinguished from the filing CALENDAR by
    its header carrying 'requirement' + 'ref' + 'norm' (and NO 'due' column). Returns
    {norm_ref: {requirement, ref, norm, status, remark}}."""
    out: Dict[str, dict] = {}
    for grid in grids:
        for rows in grid.values():
            hdr = _cols = None
            for ri, row in enumerate(rows):
                norm = {lexicon.normalise_label(v): ci for ci, v in enumerate(row) if isinstance(v, str)}
                if 'requirement' in norm and 'ref' in norm and 'norm' in norm and 'due' not in norm:
                    hdr, _cols = ri, norm
                    break
            if hdr is None:
                continue
            ci = _cols
            for row in rows[hdr + 1:]:
                def _g(key):
                    c = ci.get(key)
                    v = row[c] if (c is not None and c < len(row)) else None
                    return str(v).strip() if v is not None and str(v).strip() else ''
                ref = _g('ref')
                if not ref:
                    continue
                out[_norm_ref(ref)] = {
                    'requirement': _g('requirement'), 'ref': ref, 'norm': _g('norm'),
                    'status': _g('status'), 'remark': _g('remark')}
    return out


def _reported_for(rule: ComplianceRule, rows: Dict[str, dict]) -> Optional[dict]:
    """The checklist row whose SEBI ref equals the rule's reg_ref (parenthetical-preserving match)."""
    key = _norm_ref(rule.reg_ref)
    if key in rows:
        return rows[key]
    return next((row for nref, row in rows.items() if nref == key), None)


# ══════════════════════════════════════════════════════════════════════════════════
# 3. The engine — one code path for every rule
# ══════════════════════════════════════════════════════════════════════════════════
_AMOUNT_TOL = Decimal('0.01')
_FRAC_TOL = Decimal('0.005')          # 0.5 percentage-point band for a %-remark ("~11.6%")
_COUNT_TOL = Decimal('0')
_YEARS_TOL = Decimal('0')


def _tol(value_type: str) -> Decimal:
    return {'amount_cr': _AMOUNT_TOL, 'fraction': _FRAC_TOL,
            'count': _COUNT_TOL, 'years': _YEARS_TOL}[value_type]


def reconcile_compliance(primitives: CompliancePrimitives, grids: List[dict], *,
                         category: Optional[str] = None):
    """Run every registry rule applicable to the fund's category. Returns (checks, disclosures).
    Emits PASS / FLAG / HELD / ABSENT as SOFT reconciliation rows with full provenance."""
    from . import reconcile
    checks: List[dict] = []
    disc: List[dict] = []

    cat = category or primitives.category or detect_category(grids)
    if cat is None or cat not in REGISTRY:
        disc.append({'kind': 'sebi_compliance',
                     'detail': f'AIF category not resolved (got {cat!r}) — every SEBI tie HELD (fail-closed, '
                               'never guess a category rule-set)'})
        return checks, disc
    disc.append({'kind': 'sebi_category', 'detail': f'AIF category = {cat}; running the Cat {cat} rule-set '
                 f'({len(REGISTRY[cat])} ties)'})

    rows = _find_checklist_rows(grids)

    for rule in REGISTRY[cat]:
        independent = primitives.independent(rule)
        row = _reported_for(rule, rows)
        reported = parse_reported(row['remark'], rule.value_type) if row else None

        # P4/P5: an unresolved required external input → HELD, never back-solved, never FV-basis
        if rule.requires and independent is None:
            missing = ', '.join(rule.requires)
            status = reconcile.INDETERMINATE
            verdict = 'HELD'
            detail = (f'{rule.requirement} ({rule.reg_ref}): independent value needs {missing} '
                      f'(basis: {rule.basis_note}) — HELD until supplied; never back-solved from the '
                      f'reported {row["remark"] if row else "n/a"}')
        elif independent is None:
            status = reconcile.INDETERMINATE
            verdict = 'HELD'
            detail = f'{rule.requirement} ({rule.reg_ref}): no independent source figure in the data — HELD (P1)'
        else:
            # NORM (independent vs regulatory threshold), honouring a dual sponsor threshold
            norm_ok = independent >= rule.threshold if rule.norm_op == '>=' else independent <= rule.threshold
            if not norm_ok and rule.source_kind == 'sponsor_frac' and rule.threshold_alt is not None:
                if primitives.sponsor_commitment_cr is not None:
                    norm_ok = primitives.sponsor_commitment_cr >= rule.threshold_alt
            # TIE (independent vs reported remark)
            if reported is None:
                status = reconcile.INDETERMINATE
                verdict = 'ABSENT'
                detail = (f'{rule.requirement} ({rule.reg_ref}): independent {independent} computed but NO reported '
                          f'remark to tie against — flagged ABSENT (P1: no tie without both sides)')
            else:
                tie_ok = (independent - reported).copy_abs() <= _tol(rule.value_type)
                if norm_ok and tie_ok:
                    status, verdict = reconcile.PASS, 'PASS'
                    detail = (f'{rule.requirement} ({rule.reg_ref}): independent {independent} ties reported '
                              f'{reported} AND satisfies {rule.norm_op} {rule.threshold} [{rule.basis_note}]')
                else:
                    status, verdict = reconcile.FAIL, 'FLAG'
                    why = []
                    if not tie_ok:
                        why.append(f'independent {independent} ≠ reported {reported}')
                    if not norm_ok:
                        why.append(f'breaches {rule.norm_op} {rule.threshold}')
                    detail = f'{rule.requirement} ({rule.reg_ref}): {"; ".join(why)} [{rule.basis_note}]'

        checks.append(reconcile._result(f'sebi_tie_{rule.id}', reconcile.SOFT, status,
                                        reg_ref=rule.reg_ref, verdict=verdict,
                                        independent='' if independent is None else str(independent),
                                        reported='' if reported is None else str(reported),
                                        basis=rule.basis_note, detail=detail))
        disc.append({'kind': f'sebi_tie_{rule.id}', 'detail': f'[{verdict}] {detail}'})

    return checks, disc


# ══════════════════════════════════════════════════════════════════════════════════
# 4. Calendar-completeness (universal; absence needs the user's authoritative required-set)
# ══════════════════════════════════════════════════════════════════════════════════
_FILED = ('filed', 'done', 'submitted', 'complete', 'compliant')
_OPEN = ('in progress', 'in-progress', 'ongoing', 'pending', 'on', 'due')


def check_calendar(calendar_records, *, required: Optional[Tuple[str, ...]] = None):
    """Filing-calendar completeness. Each present filing → PASS (filed) / OPEN (in-progress). A
    required-but-MISSING filing → ABSENT, but only when the user supplies the authoritative required
    set (P5: we never assert 'complete' from the sheet alone — absence-detection needs the required
    list). Returns (checks, disclosures)."""
    from . import reconcile
    checks: List[dict] = []
    disc: List[dict] = []
    present = {}
    for rec in (calendar_records or []):
        act = rec.fields.get('activity') or rec.fields.get('key') or ''
        status = str(rec.fields.get('status', '') or '').lower()
        act_l = lexicon.normalise_label(str(act))
        present[act_l] = (str(act), status)
        if any(k in status for k in _FILED):
            st, vd = reconcile.PASS, 'PASS'
        elif any(k in status for k in _OPEN):
            st, vd = reconcile.INDETERMINATE, 'OPEN'
        else:
            st, vd = reconcile.INDETERMINATE, 'OPEN'
        checks.append(reconcile._result('sebi_filing_status', reconcile.SOFT, st,
                                        filing=str(act), filing_status=status, verdict=vd))
    if required:
        for req in required:
            if not any(lexicon.normalise_label(req) in a for a in present):
                checks.append(reconcile._result('sebi_filing_absent', reconcile.SOFT, reconcile.FAIL,
                                                filing=req, verdict='ABSENT'))
                disc.append({'kind': 'sebi_filing_absent',
                             'detail': f'required filing ABSENT from the calendar: {req}'})
    else:
        disc.append({'kind': 'sebi_calendar',
                     'detail': f'{len(present)} filings read from the calendar; completeness reported as status-only '
                               '— required-set ABSENCE detection needs the user\'s authoritative filing calendar '
                               '(fail-closed: not claimed complete)'})
    return checks, disc


# ══════════════════════════════════════════════════════════════════════════════════
# 5. Primitive assembly from the CIR
# ══════════════════════════════════════════════════════════════════════════════════
_SPONSOR_TOKENS = ('sponsor', 'gp commitment', 'gp llp', 'general partner')
_TENURE_LABELS = ('fund term', 'tenure', 'term of the fund', 'fund tenure', 'term')


def _tenure_from_grids(grids: List[dict]) -> Optional[Decimal]:
    """Independent fund tenure (years) from the terms sheet — the LPA 'Fund term' row (e.g. '8 yrs + 2')."""
    for grid in grids:
        for rows in grid.values():
            for row in rows:
                for ci, v in enumerate(row):
                    if isinstance(v, str) and lexicon.normalise_label(v) in _TENURE_LABELS:
                        for c2 in range(ci + 1, len(row)):
                            n = _num(str(row[c2])) if row[c2] is not None else None
                            if n is not None:
                                return n
    return None


def build_primitives(*, lp_registers, cir_records, grids, investable_funds_cr=None,
                     category=None) -> CompliancePrimitives:
    """Assemble the INDEPENDENT compliance figures from the CIR objects the pipeline already holds."""
    from .cir import Figure

    corpus = next((reg.corpus_cr for reg in (lp_registers or []) if reg.corpus_cr is not None), None)

    commitments: List[Decimal] = []
    sponsor_cr: Optional[Decimal] = None
    lp_recs = [r for r in cir_records if r.domain == 'lp_register']
    for r in lp_recs:
        cf = r.fields.get('commitment')
        cval = cf.value_cr if isinstance(cf, Figure) and cf.confirmed else None
        if cval is not None:
            commitments.append(cval)
        name = (r.entity_id or '') + ' ' + str(r.fields.get('type', '') or '')
        if any(t in lexicon.normalise_label(name) for t in _SPONSOR_TOKENS) and cval is not None:
            sponsor_cr = cval

    costs = [r.fields.get('cost') for r in cir_records if r.domain == 'portfolio_investments']
    cost_vals = [c.value_cr for c in costs if isinstance(c, Figure) and c.confirmed]
    largest_cost = max(cost_vals) if cost_vals else None

    return CompliancePrimitives(
        category=category or detect_category(grids),
        corpus_cr=corpus,
        commitments_cr=tuple(commitments),
        lp_count=len(lp_recs) if lp_recs else None,
        sponsor_commitment_cr=sponsor_cr,
        largest_cost_cr=largest_cost,
        investable_funds_cr=investable_funds_cr,
        tenure_years=_tenure_from_grids(grids),
    )
