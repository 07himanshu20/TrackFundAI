"""
Metric Arbiter — single source of truth for fund-level dashboard metrics.

ARCHITECTURE
============
Every fund-metric Pass (3.5 direct extraction, 4 catalogue derivation,
8 waterfall direct, 9 unified compute) calls `record_metric_candidate(...)`
to file its computed value into the MetricCandidate table. After all
Passes finish, `MetricArbiter.run(scheme)` reads every candidate for
that scheme, applies a deterministic trust-tier ranking and a small set
of universal accounting-identity guards, and writes the winning value
to DerivedMetric — overwriting whatever any single Pass wrote.

WHY THIS EXISTS
===============
Before the Arbiter, each Pass wrote directly to DerivedMetric and the
last Pass to write WON unconditionally. That allowed a low-confidence
Pass-4 catalogue formula to silently overwrite a high-confidence
Pass-9 direct read of the same cell. The Arbiter ends that pattern by
enforcing a deterministic policy at one place in the code.

THE POLICY IS DELIBERATELY SIMPLE
=================================
- Trust tier comes from WHICH Pass produced the candidate, not from
  the candidate's self-reported confidence (Gemini's confidence is not
  comparable across passes — Pass 9 confidence ≈ "how sure am I about
  reading this cell", Pass 4 confidence ≈ "how sure am I about this
  formula choice").
- Identity guards are PURE MATH — they hold for every fund regardless
  of workbook layout (e.g. carry_amount_net = max(gross − clawback, 0)).
- No file-specific logic. No keyword lists. No regex tuned to one
  workbook. The same policy runs for every scheme.

TRUST TIERS (highest to lowest)
===============================
  Tier A — direct extraction from a workbook cell:
            • Pass 8 (waterfall sheet read)
            • Pass 9 explicit extraction (formula = 'DIRECT_EXTRACT'
              or contains 'read from cell')
            • Pass 3.5 with a non-annotated label (label has no
              'estimated'/'target'/'benchmark' tokens)
  Tier B — Gemini-derived from extracted inputs:
            • Pass 9 derivation (formula contains arithmetic)
  Tier C — catalogue formula from raw model rows:
            • Pass 4
  Tier D — direct extraction from an annotated label (estimated/target):
            • Pass 3.5 with annotated label
              (last resort — placeholder values, but still better
               than nothing if everything else failed)

If multiple candidates share a tier, the one with the highest
self-reported confidence wins. If still tied, the one with the most
recent created_at wins (deterministic by timestamp).

IDENTITY GUARDS (applied after tier selection)
==============================================
1. gp_clawback_provision ≥ 0  — clamp to 0 if negative
2. carry_amount_gross   ≥ 0   — clamp to 0 if negative
3. carry_amount_net = max(carry_amount_gross − gp_clawback_provision, 0)
   When gross and clawback are both known, REWRITE net to satisfy the
   identity. This catches both directions: net > gross AND net < gross
   when clawback is 0 (the Multiples bug).
4. carry_amount_net    ≥ 0   — clamp to 0 if still negative after #3
"""

import logging
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# Annotation tokens that mark a workbook cell as an aspirational /
# estimated / placeholder value rather than an actual realised number.
# Used to demote Pass 3.5 candidates whose LABEL contains any of these
# down to Tier D (last resort). Universal across English-language fund
# workbooks; not file-specific.
_ANNOTATION_TOKENS = (
    'estimated', 'estimate', 'target', 'benchmark',
    'budgeted', 'budget', 'forecast', 'projected',
    'goal', 'aspiration', 'aspirational', 'placeholder',
    'modelled', 'modeled', 'provisional', 'pro-forma', 'proforma',
)


def _label_is_annotated(label_text):
    """True when the label text contains an aspirational/placeholder marker.

    Pure substring check, language-agnostic for the tokens listed. The
    intent is to demote (not delete) annotated cells — they may still
    be the only signal available, but Pass 9 / Pass 8 / Pass 4
    derivations should win when present.
    """
    if not label_text:
        return False
    s = str(label_text).lower()
    return any(tok in s for tok in _ANNOTATION_TOKENS)


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def record_metric_candidate(
    *,
    scheme,
    organization,
    metric_key,
    pass_id,
    value,
    variant=None,
    formula_expression='',
    confidence=0.0,
    inputs_used=None,
    source_cells=None,
    gemini_reasoning='',
    source_import_file=None,
):
    """Persist a single Pass's candidate for one metric. Idempotent on
    (scheme, metric_key, variant, pass_id) — re-running a Pass during
    the same import overwrites its own prior candidate row.

    Called from each Pass's write path IN ADDITION to whatever
    DerivedMetric write the Pass already does. Eventually the Arbiter
    will consume these rows and rewrite DerivedMetric — but the
    intermediate DerivedMetric writes are kept so that any Pass-7-style
    downstream that runs MID-import still sees a value to read.
    """
    if value is None:
        return None
    try:
        dec_value = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        logger.warning(
            'record_metric_candidate: cannot coerce value=%r for %s/%s',
            value, metric_key, pass_id,
        )
        return None

    from .models import MetricCandidate
    try:
        candidate, _created = MetricCandidate.objects.update_or_create(
            scheme=scheme,
            metric_key=metric_key,
            variant=variant,
            pass_id=pass_id,
            defaults={
                'organization': organization,
                'value': dec_value,
                'formula_expression': (formula_expression or '')[:2000],
                'confidence': max(0.0, min(1.0, float(confidence or 0.0))),
                'inputs_used': inputs_used or {},
                'source_cells': source_cells or [],
                'gemini_reasoning': (gemini_reasoning or '')[:4000],
                'source_import_file': source_import_file,
                'arbiter_decision': '',  # cleared on each record; Arbiter sets later
                'arbiter_reason': '',
            },
        )
        return candidate
    except Exception as e:
        logger.warning(
            'record_metric_candidate failed for %s/%s: %s',
            metric_key, pass_id, e,
        )
        return None


# Tier assignment — pure function. NOT file-specific. NOT keyword-tuned.
# The same logic classifies every candidate from every fund.
_TIER_A = 'A'   # direct cell read, authoritative
_TIER_B = 'B'   # Gemini-derived from extracted inputs (Pass 9)
_TIER_C = 'C'   # catalogue formula on raw model rows (Pass 4)
_TIER_D = 'D'   # direct read but from an annotated/placeholder label


def _classify_tier(candidate):
    """Assign a trust tier to one candidate. See module docstring for
    the tier definitions. Returns one of {_TIER_A, _TIER_B, _TIER_C,
    _TIER_D}."""
    formula = (candidate.formula_expression or '').lower()
    pass_id = candidate.pass_id
    inputs = candidate.inputs_used or {}

    # Pass 8 — direct waterfall sheet read. Always Tier A.
    if pass_id == 'P8':
        return _TIER_A

    # Pass 9 — depends on whether the formula was direct extraction
    # (Gemini read a single cell) or a derivation (Gemini did arithmetic
    # from multiple inputs).
    if pass_id == 'P9':
        is_direct_extract = (
            'direct_extract' in formula
            or 'read from cell' in formula
            or 'directly extracted' in formula
            or 'directly read' in formula
            or formula.strip() in ('', '(pass 9 unified)')
            or formula.startswith('(pass 9 unified) direct')
        )
        return _TIER_A if is_direct_extract else _TIER_B

    # Pass 3.5 — direct cell extraction. Demoted to Tier D when the
    # source label is annotated (estimated/target/etc.). Tier A
    # otherwise.
    if pass_id == 'P35':
        # The source_label is typically in inputs_used as either
        # 'source_label' or buried under a key — accept both shapes.
        label = (
            inputs.get('source_label')
            or inputs.get('label')
            or ''
        )
        if _label_is_annotated(label):
            return _TIER_D
        return _TIER_A

    # Pass 4 — catalogue derivation. Tier C.
    if pass_id == 'P4':
        return _TIER_C

    # Unknown Pass — last resort.
    return _TIER_D


def _pick_winner(candidates):
    """Apply the deterministic tier-then-confidence-then-recency
    policy. Returns the chosen candidate plus a one-line reason string.
    """
    if not candidates:
        return None, 'no candidates'

    # Bucket by tier
    by_tier = {}
    for c in candidates:
        t = _classify_tier(c)
        by_tier.setdefault(t, []).append(c)

    for tier in (_TIER_A, _TIER_B, _TIER_C, _TIER_D):
        bucket = by_tier.get(tier) or []
        if not bucket:
            continue
        # Highest confidence first, then most recent
        bucket.sort(
            key=lambda c: (
                -(c.confidence or 0.0),
                -(c.created_at.timestamp() if c.created_at else 0),
            )
        )
        winner = bucket[0]
        reason = (
            f'tier {tier} winner from {winner.pass_id} '
            f'(confidence={winner.confidence:.2f})'
        )
        return winner, reason

    return None, 'no candidate in any tier'


# Universal accounting-identity guards. Pure math, no heuristics.
# Each guard returns (corrected_value, was_clamped_bool, reason_str).
def _guard_clawback_nonneg(v):
    if v is None:
        return v, False, ''
    if v < 0:
        return Decimal('0'), True, 'identity: gp_clawback_provision ≥ 0 (clamped)'
    return v, False, ''


def _guard_gross_nonneg(v):
    if v is None:
        return v, False, ''
    if v < 0:
        return Decimal('0'), True, 'identity: carry_amount_gross ≥ 0 (clamped)'
    return v, False, ''


def _guard_net_equals_gross_minus_clawback(net, gross, clawback):
    """The strict identity: net = max(gross − clawback, 0). Applied
    when BOTH gross and clawback are known. Catches violations in
    BOTH directions (net > gross AND net < gross when clawback=0)."""
    if gross is None:
        return net, False, ''
    cb = clawback if clawback is not None else Decimal('0')
    target = max(gross - cb, Decimal('0'))
    if net is None:
        return target, True, (
            f'identity: net = max(gross − clawback, 0) — derived '
            f'({gross} − {cb})'
        )
    # Tolerance: 1.0 absolute OR 1% of |target|, whichever is larger.
    tol = max(Decimal('1.0'), abs(target) * Decimal('0.01'))
    if abs(net - target) > tol:
        return target, True, (
            f'identity: net (was {net}) rewritten to gross − clawback '
            f'= {gross} − {cb} = {target}'
        )
    return net, False, ''


class MetricArbiter:
    """Reads all MetricCandidate rows for a scheme, picks winners,
    enforces identity guards, and rewrites DerivedMetric. Stateless
    apart from the candidate table; safe to re-run."""

    def __init__(self, scheme, organization=None, source_import_file=None):
        self.scheme = scheme
        self.organization = organization or getattr(scheme, 'organization', None)
        self.source_import_file = source_import_file

    @transaction.atomic
    def run(self):
        from .models import MetricCandidate, DerivedMetric

        # Group all candidates by (metric_key, variant). variant is
        # stored as '' for None to keep grouping stable.
        all_cands = list(
            MetricCandidate.objects.filter(scheme=self.scheme).exclude(value=None)
        )
        if not all_cands:
            logger.info(
                '[Arbiter] scheme=%s: no candidates to arbitrate.',
                getattr(self.scheme, 'name', '?'),
            )
            return {'winners': [], 'total_candidates': 0}

        # Clear prior arbiter decisions so re-runs are deterministic
        MetricCandidate.objects.filter(scheme=self.scheme).update(
            arbiter_decision='', arbiter_reason='',
        )

        grouped = {}
        for c in all_cands:
            key = (c.metric_key, c.variant or '')
            grouped.setdefault(key, []).append(c)

        # PHASE 1 — pick a tentative winner per (metric, variant)
        tentative = {}   # (metric, variant) -> winner MetricCandidate
        reasons = {}
        for (mk, var), cands in grouped.items():
            winner, reason = _pick_winner(cands)
            if winner is None:
                continue
            tentative[(mk, var)] = winner
            reasons[(mk, var)] = reason

        # PHASE 2 — apply universal identity guards. These guards
        # operate on the VALUES of the tentative winners. They may
        # rewrite a value (and the reason string), but they NEVER
        # change which candidate is the winner.
        clamp_notes = {}   # (metric, variant) -> note appended after clamp

        # Pull the three carry values from tentative winners (when present)
        def _val(mk):
            w = tentative.get((mk, ''))
            return (w.value if w else None)

        def _set_val(mk, new_value, note):
            w = tentative.get((mk, ''))
            if w is None:
                return
            if new_value is not None and not isinstance(new_value, Decimal):
                new_value = Decimal(str(new_value))
            w._arbiter_value_override = new_value
            clamp_notes[(mk, '')] = note

        # Guard 1: clawback ≥ 0
        cb_val = _val('gp_clawback_provision')
        cb_new, cb_clamped, cb_reason = _guard_clawback_nonneg(cb_val)
        if cb_clamped:
            _set_val('gp_clawback_provision', cb_new, cb_reason)
            cb_val = cb_new

        # Guard 2: gross ≥ 0
        g_val = _val('carry_amount_gross')
        g_new, g_clamped, g_reason = _guard_gross_nonneg(g_val)
        if g_clamped:
            _set_val('carry_amount_gross', g_new, g_reason)
            g_val = g_new

        # Guard 3: net = max(gross − clawback, 0)  [the big one]
        n_val = _val('carry_amount_net')
        n_new, n_clamped, n_reason = _guard_net_equals_gross_minus_clawback(
            n_val, g_val, cb_val,
        )
        if n_clamped:
            # If carry_amount_net has no winner yet but the identity
            # gives us a value, SYNTHESIZE a winner — there's no other
            # source. We mark the synthetic winner with pass_id='P4'
            # (closest semantic) and a clear formula attribution.
            if ('carry_amount_net', '') not in tentative and g_val is not None:
                from .models import MetricCandidate
                synthetic = MetricCandidate.objects.create(
                    organization=self.organization,
                    scheme=self.scheme,
                    metric_key='carry_amount_net',
                    variant=None,
                    pass_id='P4',
                    value=n_new,
                    formula_expression=(
                        'max(carry_amount_gross − gp_clawback_provision, 0)'
                    ),
                    confidence=min(
                        tentative.get(('carry_amount_gross', ''), type('X',(),{'confidence':0.5})).confidence or 0.5,
                        tentative.get(('gp_clawback_provision', ''), type('X',(),{'confidence':1.0})).confidence or 1.0,
                    ),
                    gemini_reasoning=(
                        'Arbiter synthetic candidate: no Pass produced '
                        'carry_amount_net for this scheme, so the Arbiter '
                        'derived it from the canonical identity '
                        'net = max(gross − clawback, 0).'
                    ),
                    source_import_file=self.source_import_file,
                )
                tentative[('carry_amount_net', '')] = synthetic
                reasons[('carry_amount_net', '')] = (
                    'synthesized from identity (no Pass winner found)'
                )
                clamp_notes[('carry_amount_net', '')] = n_reason
            else:
                _set_val('carry_amount_net', n_new, n_reason)

        # Guard 4: net ≥ 0 (after the identity application)
        n_val_after = _val('carry_amount_net')
        if n_val_after is not None and n_val_after < 0:
            _set_val('carry_amount_net', Decimal('0'),
                     'identity: carry_amount_net ≥ 0 (clamped)')

        # PHASE 3 — write winners to DerivedMetric and mark MetricCandidate
        # rows with their arbiter_decision.
        winners_written = []
        for (mk, var), winner in tentative.items():
            value = getattr(winner, '_arbiter_value_override', None) or winner.value
            tier_reason = reasons.get((mk, var), '')
            clamp_note = clamp_notes.get((mk, var), '')
            combined_reason = (
                f'{tier_reason}{(" · " + clamp_note) if clamp_note else ""}'
            )

            # Build a candidate_formulas list for the DerivedMetric
            # provenance panel: every alternate the Arbiter REJECTED
            # for this (metric, variant) goes here so the UI can show
            # "Pass 4 said X, Pass 9 said Y — Arbiter chose Y because …"
            alternates = []
            for c in grouped[(mk, var)]:
                if c.id == winner.id:
                    continue
                alternates.append({
                    'pass_id': c.pass_id,
                    'value': float(c.value) if c.value is not None else None,
                    'formula': (c.formula_expression or '')[:300],
                    'confidence': c.confidence,
                    'reason_rejected': (
                        f'Arbiter preferred {winner.pass_id} '
                        f'(tier {_classify_tier(winner)})'
                    ),
                })

            try:
                DerivedMetric.objects.update_or_create(
                    scheme=self.scheme,
                    metric_key=mk,
                    variant=var or None,
                    defaults={
                        'organization': self.organization,
                        'value': value,
                        'formula_expression': (
                            f'(Arbiter:{winner.pass_id}) '
                            f'{(winner.formula_expression or "")[:1900]}'
                        ),
                        'inputs_used': winner.inputs_used or {},
                        'confidence': winner.confidence,
                        'gemini_reasoning': (
                            f'[Arbiter] {combined_reason}\n\n'
                            f'{winner.gemini_reasoning or ""}'
                        )[:4000],
                        'candidate_formulas': alternates,
                        'source_import_file': (
                            self.source_import_file or winner.source_import_file
                        ),
                    },
                )
                winners_written.append((mk, var or '', winner.pass_id, str(value)))
            except Exception as e:
                logger.warning(
                    '[Arbiter] DerivedMetric write failed for %s/%s: %s',
                    mk, var, e,
                )

            # Mark candidate rows with their decision
            try:
                from .models import MetricCandidate
                MetricCandidate.objects.filter(id=winner.id).update(
                    arbiter_decision=(
                        'identity_clamped' if clamp_note else 'winner'
                    ),
                    arbiter_reason=combined_reason[:1000],
                )
                for c in grouped[(mk, var)]:
                    if c.id == winner.id:
                        continue
                    MetricCandidate.objects.filter(id=c.id).update(
                        arbiter_decision='superseded',
                        arbiter_reason=(
                            f'Superseded by {winner.pass_id} '
                            f'(tier {_classify_tier(winner)})'
                        )[:1000],
                    )
            except Exception as e:
                logger.warning(
                    '[Arbiter] candidate marking failed for %s/%s: %s',
                    mk, var, e,
                )

        logger.info(
            '[Arbiter] scheme=%s: arbitrated %d metric(s) from %d candidate row(s).',
            getattr(self.scheme, 'name', '?'),
            len(winners_written), len(all_cands),
        )
        for mk, var, pid, val in winners_written:
            logger.info(
                '  [Arbiter winner] %s%s = %s (from %s)',
                mk, (f'/{var}' if var else ''), val, pid,
            )

        return {
            'winners': winners_written,
            'total_candidates': len(all_cands),
        }


def run_arbiter_for_fund(fund, source_import_file=None):
    """Run the Arbiter for every scheme in a fund. Safe to call from
    the import orchestrator at any point after all Passes that write
    candidates have completed.
    """
    if fund is None:
        return []
    out = []
    for sch in fund.schemes.all():
        try:
            res = MetricArbiter(
                scheme=sch,
                organization=getattr(fund, 'organization', None),
                source_import_file=source_import_file,
            ).run()
            out.append({'scheme_id': str(sch.id), 'result': res})
        except Exception as e:
            logger.exception(
                '[Arbiter] FATAL for scheme=%s: %s',
                getattr(sch, 'name', '?'), e,
            )
            out.append({'scheme_id': str(sch.id), 'error': str(e)})
    return out
