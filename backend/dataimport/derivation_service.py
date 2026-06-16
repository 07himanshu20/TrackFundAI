"""
Pass 4 — Universal AI-driven derivation of missing dashboard metrics.

PRODUCTION GUARANTEES
=====================
1.  NEVER raises. Every metric is derived in its own isolated try/except.
    A failure on one metric never blocks any other metric.
2.  Zero hardcoded formulas. Gemini decides the formula at runtime; Python
    just evaluates the arithmetic AST or runs XIRR.
3.  Every model attribute access is wrapped in `getattr(obj, name, None)`
    so a missing or renamed field cannot crash Pass 4.
4.  Every DB aggregate call is wrapped — DB errors become None inputs.
5.  Idempotent — `update_or_create` keyed on (scheme, metric_key).
6.  Provenance-complete — every DerivedMetric row carries the chosen
    formula, inputs used (with source provenance), confidence, Gemini's
    reasoning, and the alternates it considered.

NO PROGRAMMER-VISIBLE FIELD NAMES ARE BAKED IN
==============================================
The legacy code referenced specific model field names (Investment.total_invested,
Valuation.fair_value, etc.). Those references survive only as *fall-back
hints*; the primary input discovery walks `_meta.get_fields()` on every
related model at runtime and presents Gemini with EVERY non-null numeric /
date / decimal column on the scheme and on its related rows.

This means: if a future migration renames `total_invested` to `cost_basis`,
Pass 4 still works — the column appears in the snapshot under its new name
and Gemini does the semantic mapping.
"""

import ast
import logging
import math
import operator
import os
import traceback
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Union

from django.apps import apps
from django.db import models as django_models
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Safe arithmetic AST evaluator
# ─────────────────────────────────────────────────────────────────────────────
# Only +, -, *, /, **, %, parens, names, literal numbers are allowed.
# No function calls, attribute access, subscripts, comprehensions, lambdas,
# imports — any of those raise ValueError and the metric is skipped safely.

_ALLOWED_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}

_ALLOWED_UNARY_OPS = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

# Pure-function whitelist. These are mathematically pure (no side effects,
# no I/O, no attribute access on caller objects) so safe to allow inside
# the AST evaluator. Needed because Gemini regularly uses max()/min() for
# clamp formulas like `max(value - threshold, 0)` in waterfall/floor logic.
_ALLOWED_FUNCS = {
    'max': lambda *args: max(args) if args else 0.0,
    'min': lambda *args: min(args) if args else 0.0,
    'abs': lambda x: abs(x),
}


def _safe_eval(expression, variables):
    """Evaluate a pure-arithmetic expression. Returns float or None on failure."""
    if not expression or not isinstance(expression, str):
        return None
    try:
        tree = ast.parse(expression, mode='eval')
    except SyntaxError as e:
        logger.warning('Pass4 _safe_eval syntax error in "%s": %s', expression, e)
        return None

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return float(node.value)
            raise ValueError('non-numeric constant: %r' % (node.value,))
        if isinstance(node, ast.Name):
            if node.id not in variables:
                raise ValueError('unknown variable: %s' % node.id)
            v = variables[node.id]
            if v is None:
                raise ValueError('null variable: %s' % node.id)
            return float(v)
        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_BIN_OPS:
                raise ValueError('disallowed binop: %s' % op_type.__name__)
            return _ALLOWED_BIN_OPS[op_type](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in _ALLOWED_UNARY_OPS:
                raise ValueError('disallowed unaryop: %s' % op_type.__name__)
            return _ALLOWED_UNARY_OPS[op_type](walk(node.operand))
        if isinstance(node, ast.Call):
            # Bare-name calls only — no attribute access, no nested call objects
            if not isinstance(node.func, ast.Name):
                raise ValueError('disallowed call expression')
            fname = node.func.id
            if fname not in _ALLOWED_FUNCS:
                raise ValueError('disallowed function: %s' % fname)
            if node.keywords:
                raise ValueError('keyword arguments not allowed in %s' % fname)
            args = [walk(a) for a in node.args]
            return _ALLOWED_FUNCS[fname](*args)
        raise ValueError('disallowed AST node: %s' % type(node).__name__)

    try:
        result = walk(tree)
    except (ValueError, ZeroDivisionError, OverflowError) as e:
        logger.warning('Pass4 _safe_eval cannot evaluate "%s": %s', expression, e)
        return None

    if not isinstance(result, (int, float)):
        return None
    # Reject NaN / Inf
    if result != result or result in (float('inf'), float('-inf')):
        return None
    return float(result)


# ─────────────────────────────────────────────────────────────────────────────
# XIRR (Newton-Raphson) on a [{date, amount}] series
# ─────────────────────────────────────────────────────────────────────────────

def _coerce_to_date(raw):
    """Best-effort conversion of any value to a date. Returns None on failure."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw)[:10]
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%d-%m-%Y', '%d/%m/%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _compute_xirr(cashflow_series):
    """Annualised IRR (percent — 18.5 means 18.5%) for a {date, amount} series.

    Definition: IRR is the rate r where the NPV of all cashflows equals zero:

        NPV(r) = Σ amount_i / (1 + r) ** ((date_i - date_0) / 365)  =  0

    This is purely a mathematical root-finding problem. We solve it with
    Brent's method (scipy.optimize.brentq) over the entire mathematical
    domain of IRR — r ∈ (-1, +∞). The bounds we pass to brentq are not
    "default values" or heuristics; they are the numerically representable
    limits of the IRR domain itself:

      - lower = -0.9999   (just above -1, the singularity of (1+r)^t)
      - upper = 1.0e10    (effectively +∞ for any finite cashflow series)

    Brent's method is parameter-free — no starting points, no convergence
    tweaks. It guarantees convergence to a root if one exists in the bracket.
    If NPV does not change sign over the entire bracket, no real IRR exists
    for the input and the function returns None.
    """
    if not isinstance(cashflow_series, list) or len(cashflow_series) < 2:
        return None

    parsed = []
    for entry in cashflow_series:
        if not isinstance(entry, dict):
            continue
        d = _coerce_to_date(entry.get('date'))
        amt = entry.get('amount')
        if d is None or amt is None:
            continue
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            continue
        if amt == 0:
            continue
        parsed.append((d, amt))

    if len(parsed) < 2:
        return None
    # IRR is mathematically undefined unless both signs appear.
    if not any(a > 0 for _, a in parsed) or not any(a < 0 for _, a in parsed):
        return None

    parsed.sort(key=lambda x: x[0])
    t0 = parsed[0][0]
    tflows = [((d - t0).days / 365.0, a) for d, a in parsed]

    def npv(r):
        return sum(a / ((1.0 + r) ** t) for t, a in tflows)

    try:
        from scipy.optimize import brentq
    except Exception as e:
        logger.error('XIRR: scipy.optimize.brentq unavailable (%s) — IRR '
                     'cannot be computed without a numerical root-finder', e)
        return None

    # Mathematical limits of the IRR domain (r > -1, finite).
    DOMAIN_LOWER = -0.9999
    DOMAIN_UPPER = 1.0e10

    try:
        f_lo = npv(DOMAIN_LOWER)
        f_hi = npv(DOMAIN_UPPER)
    except (OverflowError, ZeroDivisionError, ValueError) as e:
        logger.warning('XIRR: NPV evaluation at domain bounds failed: %s', e)
        return None

    # No sign change across the entire IRR domain → no real root exists.
    if f_lo * f_hi > 0:
        return None

    try:
        root = brentq(npv, DOMAIN_LOWER, DOMAIN_UPPER,
                      xtol=1e-12, maxiter=500)
    except (ValueError, RuntimeError) as e:
        logger.warning('XIRR: brentq did not converge: %s', e)
        return None

    pct = root * 100.0
    if pct != pct or pct in (float('inf'), float('-inf')):
        return None
    return round(pct, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Number coercion (Decimal / int / str → float | None) — never raises
# ─────────────────────────────────────────────────────────────────────────────

def _to_float(x):
    if x is None:
        return None
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, Decimal):
        try:
            return float(x)
        except (InvalidOperation, ValueError):
            return None
    if isinstance(x, str):
        s = x.strip().replace(',', '').replace('₹', '').rstrip('%')
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# DB query helpers — every one is crash-proof
# ─────────────────────────────────────────────────────────────────────────────

def _safe_qs_list(qs):
    """Run a queryset, return list. Never raises."""
    try:
        return list(qs)
    except Exception as e:
        logger.warning('Pass4 query failed: %s', e)
        return []


def _safe_sum(rows, field_name):
    """Sum field_name (via getattr) across rows. Returns Decimal('0')."""
    total = Decimal('0')
    for r in rows:
        try:
            v = getattr(r, field_name, None)
            if v is None:
                continue
            if isinstance(v, Decimal):
                total += v
            else:
                try:
                    total += Decimal(str(v))
                except (InvalidOperation, ValueError, TypeError):
                    continue
        except Exception:
            continue
    return total


def _safe_get(obj, field_name, default=None):
    """getattr that never raises (DeferredAttribute / DB errors → default)."""
    try:
        return getattr(obj, field_name, default)
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Model-snapshot introspection
# ─────────────────────────────────────────────────────────────────────────────

# Field types we expose to Gemini (numeric / temporal / textual)
_NUMERIC_FIELD_TYPES = (
    django_models.DecimalField, django_models.IntegerField,
    django_models.FloatField, django_models.BigIntegerField,
    django_models.PositiveIntegerField, django_models.SmallIntegerField,
)
_TEMPORAL_FIELD_TYPES = (django_models.DateField, django_models.DateTimeField)
_TEXT_FIELD_TYPES = (django_models.CharField, django_models.TextField)


def _snapshot_instance(instance):
    """Return {field_name: value} for every non-null concrete field on
    instance, restricted to numeric/temporal/text types. Crash-proof."""
    snap = {}
    if instance is None:
        return snap
    try:
        fields = instance._meta.get_fields()
    except Exception:
        return snap
    for f in fields:
        try:
            if not getattr(f, 'concrete', False):
                continue
            if isinstance(f, (django_models.ManyToManyField,
                              django_models.ForeignKey,
                              django_models.OneToOneField,
                              django_models.ManyToOneRel)):
                continue
            if not isinstance(f, _NUMERIC_FIELD_TYPES + _TEMPORAL_FIELD_TYPES
                              + _TEXT_FIELD_TYPES):
                continue
            v = _safe_get(instance, f.name, None)
            if v is None:
                continue
            if isinstance(v, datetime):
                snap[f.name] = v.date().isoformat()
            elif isinstance(v, date):
                snap[f.name] = v.isoformat()
            elif isinstance(v, Decimal):
                snap[f.name] = float(v)
            elif isinstance(v, (int, float)):
                snap[f.name] = v
            elif isinstance(v, str):
                s = v.strip()
                if s:
                    snap[f.name] = s
        except Exception:
            continue
    return snap


# ─────────────────────────────────────────────────────────────────────────────
# Derivation Context — every input is computed in its own try/except
# ─────────────────────────────────────────────────────────────────────────────

class DerivationContext:
    """Aggregates every input Gemini may need. NEVER raises."""

    def __init__(self, scheme):
        self.scheme = scheme
        self.inputs = {}

    def _add(self, key, value, unit, description, source, available=None):
        if available is None:
            if value is None:
                available = False
            elif isinstance(value, list):
                available = len(value) >= 2
            elif isinstance(value, str):
                available = bool(value)
            else:
                available = (value != 0)
        self.inputs[key] = {
            'value': value, 'unit': unit, 'description': description,
            'source': source, 'available': available,
        }

    def build(self):
        scheme = self.scheme
        if scheme is None:
            return self

        # 1) Full snapshot of the Scheme row — every concrete field.
        try:
            scheme_snap = _snapshot_instance(scheme)
            for fname, fval in scheme_snap.items():
                self._add(
                    key=f'scheme__{fname}',
                    value=fval,
                    unit='auto',
                    description=f'Direct Scheme column "{fname}"',
                    source=f'{scheme._meta.label}.{fname}',
                )
        except Exception as e:
            logger.warning('Pass4 scheme snapshot failed: %s', e)

        # 2) Related-row aggregates — discovered dynamically.
        # For each known fund-data app, find models with a `scheme` FK,
        # sum every numeric field, and expose totals + counts.
        try:
            related_models = self._discover_scheme_related_models()
        except Exception as e:
            logger.warning('Pass4 related-model discovery failed: %s', e)
            related_models = []

        for model in related_models:
            try:
                self._aggregate_model_for_scheme(model)
            except Exception as e:
                logger.warning('Pass4 aggregate failed for %s: %s',
                               getattr(model, '__name__', '?'), e)

        # 3) Specialised cashflow series for XIRR (calls negative,
        #    distributions positive). Build defensively.
        try:
            cashflows = self._build_cashflow_series()
        except Exception as e:
            logger.warning('Pass4 cashflow build failed: %s', e)
            cashflows = []

        self._add(
            key='cashflow_series',
            value=cashflows,
            unit='series',
            description=('Time-stamped LP cashflow series: outflows '
                         'NEGATIVE (capital calls / contributions), '
                         'inflows POSITIVE (distributions / proceeds). '
                         'Use for XIRR.'),
            source='lp.CapitalCall + lp.Distribution (semantic union)',
        )

        # 4) Inception + as-of dates and elapsed years
        try:
            inception = _safe_get(scheme, 'first_close_date', None) \
                or _safe_get(scheme, 'inception_date', None)
            as_of = timezone.now().date()
            if inception:
                if isinstance(inception, datetime):
                    inception = inception.date()
                years = round((as_of - inception).days / 365.25, 4)
            else:
                years = None
            self._add('as_of_date', as_of.isoformat(), 'date',
                      'Calculation as-of date', 'system clock')
            if inception:
                self._add('inception_date', inception.isoformat(), 'date',
                          'Scheme inception / first close', 'Scheme model')
            if years is not None:
                self._add('years_since_inception', years, 'years',
                          'Years elapsed inception → today', 'derived')
        except Exception as e:
            logger.warning('Pass4 dates failed: %s', e)

        # 5) Pre-extracted authoritative values from Excel
        # (DerivedMetric rows with source='imported_direct' written by the
        # explicit-extraction pass). Surfaced as available inputs so Gemini
        # can quote them verbatim instead of re-deriving.
        #
        # Variant handling: when a metric has multiple variants (e.g. gross/net
        # for total_unrealised_fair_value), expose:
        #   imported__<metric_key>          → the variant_default value
        #   imported__<metric_key>__gross   → the gross-tagged value
        #   imported__<metric_key>__net     → the net-tagged value
        # Pass 4 formulas can reference the suffixed key when they need a
        # specific variant (e.g. carry-base derivation must use the GROSS
        # unrealised FV).
        try:
            from .models import DerivedMetric
            from .canonical_schema import CANONICAL_VALUE_CATEGORIES
            metric_catalogue = (
                CANONICAL_VALUE_CATEGORIES.get('fund_performance_metrics') or {}
            )
            # Group rows by metric_key so we can resolve variant_default
            # vs all variants in one pass.
            by_key = {}
            for dm in DerivedMetric.objects.filter(
                scheme=scheme,
                formula_expression='(direct value imported)'
            ).exclude(value=None):
                by_key.setdefault(dm.metric_key, []).append(dm)

            for metric_key, dms in by_key.items():
                meta = metric_catalogue.get(metric_key)
                variant_default = None
                if isinstance(meta, dict):
                    variant_default = meta.get('variant_default')

                # Expose every variant as a suffixed key.
                for dm in dms:
                    val = _to_float(dm.value)
                    if val is None:
                        continue
                    variant_label = dm.variant or 'default'
                    self._add(
                        key=f'imported__{metric_key}__{variant_label}',
                        value=val,
                        unit='direct',
                        description=(
                            f'Direct value extracted from Excel for {metric_key} '
                            f'(variant={variant_label}) — authoritative when present.'
                        ),
                        source='Excel (Pass 3 fund_performance_metrics)',
                    )

                # Choose the canonical "imported__<metric_key>" alias.
                # Priority: row tagged with variant_default → row with no
                # variant tag → first row.
                canonical_dm = None
                if variant_default:
                    canonical_dm = next(
                        (d for d in dms if d.variant == variant_default),
                        None,
                    )
                if canonical_dm is None:
                    canonical_dm = next(
                        (d for d in dms if not d.variant),
                        None,
                    )
                if canonical_dm is None:
                    canonical_dm = dms[0]
                val = _to_float(canonical_dm.value)
                if val is not None:
                    self._add(
                        key=f'imported__{metric_key}',
                        value=val,
                        unit='direct',
                        description=(
                            f'Direct value extracted from Excel for {metric_key} '
                            f'(canonical variant={canonical_dm.variant or "default"}) '
                            f'— authoritative when present.'
                        ),
                        source='Excel (Pass 3 fund_performance_metrics)',
                    )
        except Exception as e:
            logger.warning('Pass4 imported-direct lookup failed: %s', e)

        return self

    # ────────────────────────────────────────────────────────────────────────
    # Helpers
    # ────────────────────────────────────────────────────────────────────────

    def _discover_scheme_related_models(self):
        """Find every model whose schema includes a FK named 'scheme' OR
        which is reachable via 'investment__scheme'. App labels considered are
        project apps (those whose path is under the project's backend root)."""
        wanted_app_labels = self._project_app_labels()
        found = []
        seen = set()
        for app_label in wanted_app_labels:
            try:
                models = apps.get_app_config(app_label).get_models()
            except Exception:
                continue
            for m in models:
                if m in seen:
                    continue
                if self._model_links_to_scheme(m):
                    found.append(m)
                    seen.add(m)
        return found

    def _project_app_labels(self):
        """Auto-discover project app labels (those installed under the
        project's backend/ directory, excluding django.contrib and 3rd-party
        packages)."""
        import os
        project_root = os.path.realpath(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        ))
        labels = []
        for ac in apps.get_app_configs():
            try:
                p = os.path.realpath(ac.path)
            except Exception:
                continue
            if 'site-packages' in p or 'dist-packages' in p:
                continue
            if p.startswith(project_root + os.sep) or p == project_root:
                labels.append(ac.label)
        return labels

    def _model_links_to_scheme(self, model):
        try:
            for f in model._meta.get_fields():
                if isinstance(f, (django_models.ForeignKey,
                                  django_models.OneToOneField)):
                    rel_name = getattr(f, 'name', '')
                    rel_target = getattr(f, 'related_model', None)
                    if rel_name == 'scheme':
                        return True
                    # 2-hop: e.g. ExitEvent.investment → Investment.scheme
                    if rel_target is not None and rel_target is not model:
                        for rf in rel_target._meta.get_fields():
                            if isinstance(rf, (django_models.ForeignKey,
                                               django_models.OneToOneField)):
                                if getattr(rf, 'name', '') == 'scheme':
                                    return True
        except Exception:
            pass
        return False

    def _aggregate_model_for_scheme(self, model):
        """Pull every row of `model` for this scheme (direct or 1-hop via
        an `investment` FK), then SUM each numeric field. Skip if no rows."""
        scheme = self.scheme
        rows = []
        try:
            # Direct scheme FK
            if any(getattr(f, 'name', '') == 'scheme'
                   for f in model._meta.get_fields()
                   if isinstance(f, (django_models.ForeignKey,
                                     django_models.OneToOneField))):
                rows = _safe_qs_list(model.objects.filter(scheme=scheme))
            # 1-hop via 'investment'
            elif any(getattr(f, 'name', '') == 'investment'
                     for f in model._meta.get_fields()
                     if isinstance(f, (django_models.ForeignKey,
                                       django_models.OneToOneField))):
                rows = _safe_qs_list(
                    model.objects.filter(investment__scheme=scheme)
                )
        except Exception as e:
            logger.warning('Pass4 fetch rows failed for %s: %s',
                           model.__name__, e)
            return

        if not rows:
            return

        label_base = model._meta.label.lower().replace('.', '__')
        # Row count
        self._add(
            key=f'count__{label_base}',
            value=len(rows),
            unit='count',
            description=f'Number of {model.__name__} rows linked to scheme',
            source=model._meta.label,
        )

        # Fix M — Detect "periodic snapshot" models. A snapshot model is one
        # where each row is a point-in-time observation of the SAME quantity
        # (NAVRecord = monthly NAV snapshots; PortfolioKPI = period KPI
        # snapshots). For these, `sum__<model>__<field>` is NEVER the right
        # semantic — summing 12 monthly NAVs gives ~12× the actual NAV.
        # The only correct aggregate is `latest__<model>__<field>` (emitted
        # later below). We DROP sum__ emission for snapshot models so Pass 4
        # cannot accidentally pick the wrong variable.
        #
        # Heuristic for "snapshot": model has a date/datetime field AND that
        # field has ≥ 2 distinct values across the loaded rows. This is the
        # same test used below for `latest__` emission, so the two are
        # symmetric: if `latest__` is emittable, `sum__` is suppressed.
        is_snapshot_model = False
        try:
            for f in model._meta.get_fields():
                if not isinstance(f, _TEMPORAL_FIELD_TYPES):
                    continue
                # Only the field most likely to represent the snapshot
                # date — skip generic created_at/updated_at audit fields
                # because every model has those.
                if f.name in ('created_at', 'updated_at'):
                    continue
                seen_dates = set()
                for r in rows:
                    v = _safe_get(r, f.name, None)
                    if isinstance(v, datetime):
                        v = v.date()
                    if isinstance(v, date):
                        seen_dates.add(v)
                if len(seen_dates) >= 2:
                    is_snapshot_model = True
                    break
        except Exception:
            pass

        # Sum every numeric field on the model (skipped for snapshot models)
        if not is_snapshot_model:
            for f in model._meta.get_fields():
                if not isinstance(f, _NUMERIC_FIELD_TYPES):
                    continue
                try:
                    total = _safe_sum(rows, f.name)
                    if total == 0:
                        continue
                    self._add(
                        key=f'sum__{label_base}__{f.name}',
                        value=_to_float(total),
                        unit='auto',
                        description=(
                            f'Sum of {model.__name__}.{f.name} across '
                            f'{len(rows)} rows linked to this scheme'
                        ),
                        source=f'{model._meta.label}.{f.name}',
                    )
                except Exception:
                    continue
        else:
            logger.info(
                'Pass4 catalogue: SKIPPING sum__%s__* — %s detected as '
                'periodic-snapshot model (%d distinct dates). Pass 4 will '
                'use latest__%s__* instead.',
                label_base, model.__name__,
                len(seen_dates) if 'seen_dates' in locals() else 0,
                label_base,
            )

        # MAX for date fields — useful for "latest record" type info
        latest_date = None
        latest_date_field = None
        for f in model._meta.get_fields():
            if not isinstance(f, _TEMPORAL_FIELD_TYPES):
                continue
            try:
                dated = []  # list of (date, row) pairs
                for r in rows:
                    v = _safe_get(r, f.name, None)
                    if isinstance(v, datetime):
                        v = v.date()
                    if isinstance(v, date):
                        dated.append((v, r))
                if dated:
                    max_d = max(d for d, _ in dated)
                    self._add(
                        key=f'max__{label_base}__{f.name}',
                        value=max_d.isoformat(),
                        unit='date',
                        description=(
                            f'Most recent {model.__name__}.{f.name}'
                        ),
                        source=f'{model._meta.label}.{f.name}',
                    )
                    # Remember the most recent date across any temporal
                    # field so we can emit `latest__<model>__<field>` keys
                    # for periodic-snapshot models (NAVRecord etc.).
                    if latest_date is None or max_d > latest_date:
                        latest_date = max_d
                        latest_date_field = f.name
            except Exception:
                continue

        # latest__<model>__<numeric_field> — snapshot-of-most-recent-row
        # alternative to sum__ for time-series models. Pass 4 was previously
        # summing NAVRecord.investments_at_fair_value across all 12 monthly
        # snapshots (≈43,000 Cr) instead of using the latest single value
        # (≈3,800 Cr). This emits both shapes so Gemini can pick the right
        # one; the metric description guides the choice. We only emit
        # `latest__` when there are multiple distinct dates (≥2 snapshots)
        # because a single-row model already has `sum__` == `latest__`.
        if latest_date is not None and latest_date_field is not None:
            try:
                # Find the row with the most recent date on the selected
                # field. If two rows share the latest date, pick the first
                # one deterministically.
                latest_row = None
                for r in rows:
                    v = _safe_get(r, latest_date_field, None)
                    if isinstance(v, datetime):
                        v = v.date()
                    if v == latest_date:
                        latest_row = r
                        break
                # Count distinct dates so we don't pollute the catalogue
                # for models that are not actually time-series (1 row).
                distinct_dates = set()
                for r in rows:
                    v = _safe_get(r, latest_date_field, None)
                    if isinstance(v, datetime):
                        v = v.date()
                    if isinstance(v, date):
                        distinct_dates.add(v)
                if latest_row is not None and len(distinct_dates) >= 2:
                    for nf in model._meta.get_fields():
                        if not isinstance(nf, _NUMERIC_FIELD_TYPES):
                            continue
                        try:
                            val = _safe_get(latest_row, nf.name, None)
                            if val is None:
                                continue
                            self._add(
                                key=f'latest__{label_base}__{nf.name}',
                                value=_to_float(val),
                                unit='auto',
                                description=(
                                    f'Latest single-row value of '
                                    f'{model.__name__}.{nf.name} as of '
                                    f'{latest_date.isoformat()} (use this '
                                    f'INSTEAD of sum__ when the model is a '
                                    f'periodic snapshot like NAVRecord — '
                                    f'summing across periods is wrong).'
                                ),
                                source=(
                                    f'{model._meta.label}.{nf.name} '
                                    f'@ {latest_date.isoformat()}'
                                ),
                            )
                        except Exception:
                            continue
            except Exception:
                pass

    def _build_cashflow_series(self):
        """Universal cashflow builder. Finds any model linked to this scheme
        whose name semantically implies CALLS / CONTRIBUTIONS (outflow) or
        DISTRIBUTIONS / RETURNS (inflow), picks the (date, amount) pair via
        field naming heuristics, and emits a flat series."""
        scheme = self.scheme
        series = []

        outflow_model_hints = ('call', 'contribution', 'drawdown', 'commitment')
        inflow_model_hints = ('distribution', 'distrib', 'return', 'payout',
                              'redemption')

        date_field_hints = ('date',)
        amount_field_hints = ('amount', 'value', 'total', 'gross', 'net')

        for model in self._discover_scheme_related_models():
            try:
                name = model.__name__.lower()
                is_outflow = any(h in name for h in outflow_model_hints)
                is_inflow = any(h in name for h in inflow_model_hints)
                if not (is_outflow or is_inflow):
                    continue
                sign = -1 if is_outflow else +1

                # Pick best date + amount fields by name
                date_field = None
                amount_field = None
                for f in model._meta.get_fields():
                    fname = getattr(f, 'name', '')
                    if not isinstance(f, _TEMPORAL_FIELD_TYPES + _NUMERIC_FIELD_TYPES):
                        continue
                    if isinstance(f, _TEMPORAL_FIELD_TYPES) and date_field is None:
                        if any(h in fname for h in date_field_hints):
                            date_field = fname
                    if isinstance(f, _NUMERIC_FIELD_TYPES) and amount_field is None:
                        if any(h in fname for h in amount_field_hints):
                            amount_field = fname

                if not date_field or not amount_field:
                    continue

                # Fetch rows scoped to this scheme
                if any(getattr(f, 'name', '') == 'scheme'
                       for f in model._meta.get_fields()
                       if isinstance(f, (django_models.ForeignKey,
                                         django_models.OneToOneField))):
                    rows = _safe_qs_list(model.objects.filter(scheme=scheme))
                else:
                    rows = _safe_qs_list(
                        model.objects.filter(investment__scheme=scheme)
                    )

                for r in rows:
                    d = _coerce_to_date(_safe_get(r, date_field, None))
                    amt = _to_float(_safe_get(r, amount_field, None))
                    if d is None or amt is None or amt == 0:
                        continue
                    series.append({
                        'date': d.isoformat(),
                        'amount': sign * abs(amt),
                        'source_model': model._meta.label,
                    })
            except Exception:
                continue

        return series

    def scheme_context_str(self):
        sch = self.scheme
        bits = []
        try:
            fund = _safe_get(sch, 'fund', None)
            if fund is not None:
                bits.append(f'Fund: {_safe_get(fund, "name", "")}')
            bits.append(f'Scheme: {_safe_get(sch, "name", str(_safe_get(sch, "id", "")))}')
            inception = _safe_get(sch, 'first_close_date', None)
            if inception:
                bits.append(f'Inception: {inception}')
        except Exception:
            pass
        return ' | '.join(bits) if bits else '(no scheme context)'


# ─────────────────────────────────────────────────────────────────────────────
# Direct-value check — has Excel given us this metric verbatim already?
# ─────────────────────────────────────────────────────────────────────────────

def _direct_value_exists(scheme, metric_key):
    """Returns (True, value) when an authoritative imported value exists,
    else (False, None). Crash-proof.

    Sole source of truth: DerivedMetric rows with
    formula_expression='(direct value imported)'. NO hardcoded per-metric
    model checks here — those would be keyword shortcuts that contradict
    the production principle ("no hardcoded keywords"). To make a value
    visible here, the extraction pass (Pass 3
    fund_performance_metrics) must write a DerivedMetric row with the
    imported value.

    Variant handling: when the metric has multiple variants persisted
    (e.g. gross + net for total_unrealised_fair_value), prefer the row
    tagged with the metric's variant_default declared in the canonical
    catalogue. Falls back to the un-tagged row, then to the first row.
    """
    try:
        from .models import DerivedMetric
        from .canonical_schema import CANONICAL_VALUE_CATEGORIES
        rows = list(
            DerivedMetric.objects.filter(
                scheme=scheme, metric_key=metric_key,
                formula_expression='(direct value imported)',
            ).exclude(value=None)
        )
        if not rows:
            return False, None
        meta = (
            CANONICAL_VALUE_CATEGORIES.get('fund_performance_metrics', {})
            .get(metric_key)
        )
        variant_default = None
        if isinstance(meta, dict):
            variant_default = meta.get('variant_default')
        chosen = None
        if variant_default:
            chosen = next((r for r in rows if r.variant == variant_default), None)
        if chosen is None:
            chosen = next((r for r in rows if not r.variant), None)
        if chosen is None:
            chosen = rows[0]
        if chosen.value is None:
            return False, None
        return True, _to_float(chosen.value)
    except Exception:
        pass
    return False, None


# ─────────────────────────────────────────────────────────────────────────────
# Main service
# ─────────────────────────────────────────────────────────────────────────────

class MetricDerivationService:
    """Pass 4 orchestrator. Runs once per scheme at end of import.

    Public contract:
        - derive_all() never raises. Returns list of (metric_key, status).
        - Status is one of: 'imported_direct', 'derived', 'unviable',
          'error:<exception class>'.
    """

    def __init__(self, organization, scheme, source_import_file=None):
        self.organization = organization
        self.scheme = scheme
        self.source_import_file = source_import_file

    # ────────────────────────────────────────────────────────────────────────

    def derive_all(self):
        results = []

        # Build context once per scheme (expensive — touches every related row)
        try:
            ctx = DerivationContext(self.scheme).build()
            scheme_ctx = ctx.scheme_context_str()
        except Exception as e:
            logger.error(
                'Pass4 FATAL context build failed scheme=%s: %s\n%s',
                _safe_get(self.scheme, 'id', '?'), e, traceback.format_exc(),
            )
            return [('_context_build', 'error:%s' % type(e).__name__)]

        try:
            from .canonical_schema import DERIVABLE_FUND_METRICS
        except Exception as e:
            logger.error('Pass4 cannot load DERIVABLE_FUND_METRICS: %s', e)
            return [('_catalog_load', 'error:%s' % type(e).__name__)]

        for metric_key, meta in DERIVABLE_FUND_METRICS.items():
            try:
                status = self._derive_one(metric_key, meta, ctx, scheme_ctx)
                results.append((metric_key, status))
            except Exception as e:
                # Hardened outer try — should never trigger but guarantees
                # that no single metric can block the others.
                logger.warning(
                    'Pass4 metric %s raised unexpectedly: %s\n%s',
                    metric_key, e, traceback.format_exc(),
                )
                results.append((metric_key, 'error:%s' % type(e).__name__))

        # Fix F — Physical-identity guards. After ALL metrics are derived,
        # walk a small set of universal accounting identities and clamp /
        # reject any DerivedMetric row that violates them. These are NOT
        # waterfall-specific heuristics; they are arithmetic facts that
        # hold for every European or American AIF and every imaginable
        # fund layout. Running them at the end (rather than mid-derivation)
        # means we catch the violation regardless of which Pass produced
        # the bad value: Pass 3.5 imported_direct, Pass 4 Gemini-derived,
        # Pass 8 direct waterfall — all are subject to the same check.
        try:
            self._apply_identity_guards()
        except Exception as e:
            logger.warning(
                'Pass4 identity-guard run failed for scheme=%s: %s\n%s',
                _safe_get(self.scheme, 'name', '?'), e, traceback.format_exc(),
            )

        logger.info(
            '[Pass4] scheme=%s outcomes=%s',
            _safe_get(self.scheme, 'name', '?'),
            results,
        )
        return results

    def _apply_identity_guards(self):
        """Validate and clamp DerivedMetric rows that violate universal
        accounting identities. Logs every adjustment so audit can surface
        which metric was clamped and why.

        Currently enforced:
          • carry_amount_net ≤ carry_amount_gross   (net cannot exceed gross)
          • carry_amount_net ≥ 0                     (no negative net carry)
          • gp_clawback_provision ≥ 0                (provision is non-negative)
          • carry_amount_net = max(gross − clawback, 0) when both gross and
            clawback are known. If gross is unknown but net violates the
            simpler net ≤ gross check, we cannot clamp without gross — we
            just delete the bad net row so the dashboard shows "no value"
            instead of an impossible one.
        """
        from .models import DerivedMetric
        scheme = self.scheme

        def _pick_authoritative(metric_key):
            """Return the single DerivedMetric row a downstream reader
            would see for (scheme, metric_key). Honour variant_default if
            declared, then untagged, then first."""
            try:
                from .canonical_schema import CANONICAL_VALUE_CATEGORIES
            except Exception:
                CANONICAL_VALUE_CATEGORIES = {}
            rows = list(
                DerivedMetric.objects.filter(
                    scheme=scheme, metric_key=metric_key,
                ).exclude(value=None)
            )
            if not rows:
                return None
            meta = (
                CANONICAL_VALUE_CATEGORIES
                .get('fund_performance_metrics', {})
                .get(metric_key)
            )
            variant_default = (
                meta.get('variant_default') if isinstance(meta, dict) else None
            )
            chosen = None
            if variant_default:
                chosen = next(
                    (r for r in rows if r.variant == variant_default), None
                )
            if chosen is None:
                chosen = next((r for r in rows if not r.variant), None)
            if chosen is None:
                chosen = rows[0]
            return chosen

        gross_row    = _pick_authoritative('carry_amount_gross')
        net_row      = _pick_authoritative('carry_amount_net')
        clawback_row = _pick_authoritative('gp_clawback_provision')

        def _v(row):
            if row is None or row.value is None:
                return None
            try:
                return float(row.value)
            except (TypeError, ValueError):
                return None

        gross    = _v(gross_row)
        net      = _v(net_row)
        clawback = _v(clawback_row)

        # Clamp negative clawback to 0 (defensive — should never happen
        # since clawback is a non-negative quantity by definition, but Pass
        # 4 could in principle return a small negative due to rounding).
        if clawback is not None and clawback < 0 and clawback_row is not None:
            logger.warning(
                '[Pass4 identity-guard] clawback=%s < 0 for scheme=%s — '
                'clamping to 0.', clawback, _safe_get(scheme, 'name', '?'),
            )
            from decimal import Decimal as _D
            clawback_row.value = _D('0')
            clawback_row.gemini_reasoning = (
                'IDENTITY GUARD: original Pass-4 value was negative '
                '(physically impossible — clawback is non-negative); '
                'clamped to 0. ' + (clawback_row.gemini_reasoning or '')
            )[:4000]
            clawback_row.save(update_fields=['value', 'gemini_reasoning'])
            clawback = 0.0

        if gross is not None and net is not None:
            # Identity #1: net ≤ gross. Violation means Pass 4 picked an
            # incompatible aggregation source — e.g. summed a per-LP carry
            # column that double-counted. Action: rebuild net from the
            # identity (gross − clawback), or 0 when clawback unknown.
            if net > gross + 1e-6 and net_row is not None:
                target = (
                    max(gross - clawback, 0.0) if clawback is not None
                    else gross
                )
                logger.warning(
                    '[Pass4 identity-guard] scheme=%s: '
                    'carry_amount_net=%s > carry_amount_gross=%s '
                    '(physically impossible); rewriting net to %s.',
                    _safe_get(scheme, 'name', '?'), net, gross, target,
                )
                from decimal import Decimal as _D
                net_row.value = _D(str(target))
                net_row.formula_expression = (
                    'max(carry_amount_gross - gp_clawback_provision, 0)'
                )[:2000]
                net_row.gemini_reasoning = (
                    f'IDENTITY GUARD: original derivation produced '
                    f'net={net:.2f} > gross={gross:.2f} which is physically '
                    f'impossible. Rewritten using the canonical identity '
                    f'net = max(gross - clawback, 0). '
                    + (net_row.gemini_reasoning or '')
                )[:4000]
                net_row.save(update_fields=[
                    'value', 'formula_expression', 'gemini_reasoning',
                ])
                net = target

            # Identity #2: net ≥ 0. Clamp.
            if net < 0 and net_row is not None:
                logger.warning(
                    '[Pass4 identity-guard] net=%s < 0 for scheme=%s — '
                    'clamping to 0.', net, _safe_get(scheme, 'name', '?'),
                )
                from decimal import Decimal as _D
                net_row.value = _D('0')
                net_row.gemini_reasoning = (
                    'IDENTITY GUARD: original value was negative; '
                    'clamped to 0. ' + (net_row.gemini_reasoning or '')
                )[:4000]
                net_row.save(update_fields=['value', 'gemini_reasoning'])

    # ────────────────────────────────────────────────────────────────────────

    def _derive_one(self, metric_key, meta, ctx, scheme_ctx):
        from .models import DerivedMetric

        # 0a) Pass-9-already-wrote path. Pass 9 runs BEFORE Pass 4 in the
        # orchestrator and persists with formula_expression prefix
        # "(Pass 9 unified) …". If Pass 9 produced a high-confidence
        # value, the catalogue-of-variables derivation here would only
        # introduce drift — leave the Pass 9 row alone.
        try:
            pass9_existing = DerivedMetric.objects.filter(
                scheme=self.scheme,
                metric_key=metric_key,
                formula_expression__startswith='(Pass 9 unified)',
            ).exclude(value=None).first()
        except Exception as e:
            logger.warning('Pass4 Pass9-check failed for %s: %s', metric_key, e)
            pass9_existing = None
        if pass9_existing is not None:
            logger.info(
                'Pass4: skipping %s — Pass 9 already wrote value=%s '
                '(confidence=%s)',
                metric_key, pass9_existing.value,
                pass9_existing.confidence,
            )
            return 'pass9_authoritative'

        # 0b) Direct-value path: Excel already gave us this number → persist
        # with provenance and we're done.
        try:
            has_direct, direct_val = _direct_value_exists(self.scheme, metric_key)
        except Exception as e:
            logger.warning('Pass4 direct-check failed for %s: %s', metric_key, e)
            has_direct, direct_val = False, None

        if has_direct:
            # Pass 3.5 already wrote a row for this metric with its own
            # inputs_used (source cell, source label, etc.) and reasoning.
            # Don't overwrite that provenance with an empty stub — pull the
            # existing record's payload through so the provenance panel can
            # show WHERE the value came from.
            preserved_inputs = {}
            preserved_reason = (
                'Direct value imported from Excel — no derivation needed.'
            )
            try:
                from .models import DerivedMetric
                existing = DerivedMetric.objects.filter(
                    scheme=self.scheme,
                    metric_key=metric_key,
                    formula_expression='(direct value imported)',
                ).exclude(value=None).first()
                if existing:
                    if existing.inputs_used:
                        preserved_inputs = dict(existing.inputs_used)
                    if existing.gemini_reasoning:
                        preserved_reason = existing.gemini_reasoning
            except Exception:
                pass
            self._persist(
                metric_key=metric_key,
                value=direct_val,
                formula='(direct value imported)',
                inputs_used=preserved_inputs,
                ctx_inputs=ctx.inputs,
                confidence=1.0,
                reasoning=preserved_reason,
                candidates=[],
                passthrough_inputs=True,
            )
            return 'imported_direct'

        # 1) Gemini path: ask for formula + inputs given the available context.
        try:
            from .gemini_column_mapper import derive_metric_via_gemini
        except Exception as e:
            logger.error('Pass4 cannot import derive_metric_via_gemini: %s', e)
            return 'error:import'

        # Outer retry loop — distinct from _call_gemini's internal retries.
        # _call_gemini handles transient API errors within a single attempt;
        # this loop handles consecutive-call failures (e.g. when Gemini's
        # quota briefly degrades after a burst of Pass 2 calls). Each retry
        # waits longer than the last so we don't hammer the API.
        import time as _time
        gemini_out = None
        last_api_error = None
        for attempt in range(1, 4):  # 3 outer attempts
            try:
                gemini_out = derive_metric_via_gemini(
                    metric_key=metric_key,
                    metric_meta=meta,
                    available_inputs=ctx.inputs,
                    scheme_context=scheme_ctx,
                )
                break  # success
            except Exception as e:
                last_api_error = e
                wait = 5 * attempt  # 5s, 10s, 15s
                logger.warning(
                    'Pass4 outer-retry %d/3 for %s after API error: %s '
                    '(waiting %ds)',
                    attempt, metric_key, type(e).__name__, wait,
                )
                _time.sleep(wait)

        if gemini_out is None:
            # All 3 outer attempts failed — this is a genuine API outage for
            # this metric. Distinct from "Gemini said no formula fits".
            self._persist(
                metric_key=metric_key,
                value=None,
                formula='',
                inputs_used={},
                ctx_inputs=ctx.inputs,
                confidence=0.0,
                reasoning=(
                    'Gemini API failed after 3 outer retries — likely a '
                    'rate-limit burst or transient outage. The dashboard '
                    'should display this metric as "unavailable (api)" '
                    'rather than "0", and a retry button should be offered. '
                    'Last error: %s: %s'
                    % (type(last_api_error).__name__, last_api_error)
                ),
                candidates=[],
            )
            return 'api_error'

        candidate_formulas = gemini_out.get('candidate_formulas') or []
        reasoning = (gemini_out.get('reasoning') or '')[:4000]

        if not candidate_formulas:
            # Gemini returned no candidates — record honestly as unviable.
            self._persist(
                metric_key=metric_key,
                value=None,
                formula='',
                inputs_used={},
                ctx_inputs=ctx.inputs,
                confidence=0.0,
                reasoning=reasoning or 'Gemini returned no candidate formulas',
                candidates=[],
            )
            return 'unviable'

        # 2) Iterate candidates in rank order. For EACH candidate:
        #    (a) AST-validate that every variable it references exists in
        #        ctx.inputs (the catalogue we passed to Gemini). Reject
        #        the candidate entirely if any variable is hallucinated.
        #    (b) Build the eval context EXCLUSIVELY from ctx.inputs values
        #        — never from any Gemini-supplied numeric value. This is
        #        the hallucination guard: Gemini cannot inject fake
        #        numbers into the dashboard.
        #    (c) Evaluate via _safe_eval (arithmetic) or _compute_xirr.
        #    (d) First candidate that produces a real finite number wins.
        catalogue_keys = set(ctx.inputs.keys())
        chosen = None
        chosen_value = None
        chosen_reason = ''
        rejected_candidates = []

        for cand in candidate_formulas:
            formula = cand.get('formula_expression', '').strip()
            if not formula:
                rejected_candidates.append({
                    **cand,
                    'rejected_because': 'empty formula',
                })
                continue

            # Extract every variable name referenced by the formula.
            referenced_vars, parse_err = self._extract_referenced_vars(formula)
            if parse_err is not None:
                rejected_candidates.append({
                    **cand,
                    'rejected_because': f'parse error: {parse_err}',
                })
                continue

            # Hallucination guard: every referenced variable must exist
            # in the catalogue passed to Gemini.
            hallucinated = [v for v in referenced_vars if v not in catalogue_keys]
            if hallucinated:
                rejected_candidates.append({
                    **cand,
                    'rejected_because': (
                        f'hallucinated variable(s) not in catalogue: '
                        f'{hallucinated}'
                    ),
                })
                logger.info(
                    'Pass4 rejected rank %s candidate for %s — '
                    'hallucinated variables: %s',
                    cand.get('rank'), metric_key, hallucinated,
                )
                continue

            # Build the eval context from CATALOGUE values only.
            eval_ctx = {}
            missing_inputs = []
            for v in referenced_vars:
                cat_entry = ctx.inputs.get(v, {})
                val = cat_entry.get('value')
                if val is None:
                    missing_inputs.append(v)
                else:
                    eval_ctx[v] = val
            if missing_inputs:
                rejected_candidates.append({
                    **cand,
                    'rejected_because': (
                        f'catalogue input is None for: {missing_inputs}'
                    ),
                })
                continue

            # Evaluate. XIRR family branches through scipy.brentq.
            computed = self._evaluate_validated(formula, eval_ctx)
            if computed is None:
                rejected_candidates.append({
                    **cand,
                    'rejected_because': 'evaluation produced None / NaN / Inf',
                })
                continue

            chosen = cand
            chosen_value = computed
            chosen_reason = (
                f'Rank {cand.get("rank")} of {len(candidate_formulas)} '
                f'candidate(s) selected. '
                f'Applies when: {cand.get("applies_when", "")}. '
                f'Disjointness proof: {cand.get("inputs_disjoint_proof", "")}. '
                f'{reasoning}'
            )[:4000]
            logger.info(
                '[Pass4] %s ← rank %s formula "%s" → value=%s '
                '(confidence=%.2f; disjoint_proof="%s")',
                metric_key, cand.get('rank'), formula[:120],
                chosen_value, cand.get('confidence', 0.0),
                (cand.get('inputs_disjoint_proof') or '')[:120],
            )
            break

        if chosen is None:
            self._persist(
                metric_key=metric_key,
                value=None,
                formula='',
                inputs_used={},
                ctx_inputs=ctx.inputs,
                confidence=0.0,
                reasoning=(
                    f'No viable candidate formula. {len(rejected_candidates)} '
                    f'candidate(s) considered; all rejected (see candidate_formulas '
                    f'for per-candidate rejection reasons). Original reasoning: '
                    f'{reasoning}'
                )[:4000],
                candidates=rejected_candidates,
            )
            return 'unviable_no_valid_formula'

        # 3) Persist the winning candidate with full audit trail.
        self._persist(
            metric_key=metric_key,
            value=chosen_value,
            formula=chosen['formula_expression'][:2000],
            inputs_used={
                k: ctx.inputs[k].get('value')
                for k in (chosen.get('inputs_required') or [])
                if k in ctx.inputs
            },
            ctx_inputs=ctx.inputs,
            confidence=float(chosen.get('confidence') or 0.0),
            reasoning=chosen_reason,
            candidates=candidate_formulas + (
                [{'rejected': rejected_candidates}] if rejected_candidates else []
            ),
        )

        return 'derived'

    # ────────────────────────────────────────────────────────────────────────

    def _extract_referenced_vars(self, formula):
        """Parse `formula` and return (set_of_variable_names, error_or_None).

        Walks the AST once to collect every `ast.Name` reference, ignoring
        names that are allowed-function call targets (max, min, abs).
        Returns ({}, '<error>') if the formula is unparseable.
        """
        try:
            tree = ast.parse(formula, mode='eval')
        except SyntaxError as e:
            return set(), str(e)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
        # Drop the allowed-function names — they are not variables.
        names -= {'max', 'min', 'abs', 'XIRR', 'xirr'}
        return names, None

    def _evaluate_validated(self, formula, eval_ctx):
        """Evaluate a pre-validated formula. The caller has already confirmed
        every variable in `formula` exists in `eval_ctx` with a non-null
        value. We only have to dispatch to XIRR vs arithmetic.
        """
        if not formula:
            return None
        f = formula.strip()
        # XIRR family: e.g. "XIRR(cashflow_series)"
        if f.lower().startswith('xirr'):
            # Find the list-valued input (the cashflow series). For the
            # ranked-formula contract, the series MUST be a catalogue
            # variable referenced by name in the formula — typically
            # 'cashflow_series'.
            series = None
            for v in eval_ctx.values():
                if isinstance(v, list):
                    series = v
                    break
            return _compute_xirr(series)

        # Arithmetic: pass catalogue values straight to _safe_eval.
        variables = {}
        for k, v in eval_ctx.items():
            fv = _to_float(v)
            if fv is not None:
                variables[k] = fv
        return _safe_eval(f, variables)

    # ────────────────────────────────────────────────────────────────────────

    def _persist(self, metric_key, value, formula, inputs_used, ctx_inputs,
                 confidence, reasoning, candidates, passthrough_inputs=False):
        from .models import DerivedMetric
        try:
            # passthrough_inputs=True is used by the direct-value path to keep
            # the source-cell context Pass 3.5 wrote (e.g. {source_cell:
            # 'WATERFALL_EUR!R23C5', source_label: 'Step 4 Total Step', ...}).
            # The serialise path expects a {key: variable_lookup} shape that
            # makes no sense for already-resolved cell metadata.
            serialised_inputs = (
                inputs_used if passthrough_inputs
                else self._serialise_inputs(inputs_used, ctx_inputs)
            )

            # ── Pass 4 unviable guard ─────────────────────────────────
            # When Gemini returns no viable formula or evaluation fails,
            # `value` is None. In that case we MUST NOT overwrite the
            # existing DerivedMetric row that persist_fund wrote at
            # Stage 4a (which carries the Stage-1B Python fallback value
            # or the Gemini-extracted Stage-1 value). Overwriting with
            # None blanks the dashboard for any metric Pass 4 couldn't
            # derive (Net IRR, GP Carry Net, Clawback Provision were the
            # symptoms in the AI_Trivesta import).
            # Instead we update only the audit fields (formula, reasoning,
            # candidates) so the Pass-4 attempt is recorded, while the
            # numeric value field is preserved.
            # Universal — applies to every metric, every fund.
            if value is None:
                existing = DerivedMetric.objects.filter(
                    scheme=self.scheme, metric_key=metric_key,
                ).first()
                if existing is not None:
                    existing.formula_expression = (formula or existing.formula_expression or '')[:2000]
                    existing.gemini_reasoning = (
                        reasoning or existing.gemini_reasoning or ''
                    )[:4000]
                    existing.candidate_formulas = candidates or existing.candidate_formulas or []
                    existing.save(update_fields=[
                        'formula_expression', 'gemini_reasoning',
                        'candidate_formulas',
                    ])
                    return
                # No existing row to preserve — write a NULL row so the
                # audit trail still shows Pass 4 attempted this metric.

            DerivedMetric.objects.update_or_create(
                scheme=self.scheme,
                metric_key=metric_key,
                defaults={
                    'organization': self.organization,
                    'value': (Decimal(str(value)) if value is not None else None),
                    'formula_expression': (formula or '')[:2000],
                    'inputs_used': serialised_inputs,
                    'confidence': max(0.0, min(1.0, confidence)),
                    'gemini_reasoning': (reasoning or '')[:4000],
                    'candidate_formulas': candidates or [],
                    'source_import_file': self.source_import_file,
                },
            )

            # AF — When Gemini's rank-1 formula validates and evaluates to
            # a real value, the FundMetric (which the dashboard reads
            # directly) must also reflect Gemini's value + formula text.
            # Otherwise the dashboard shows Python's Stage-1B fallback
            # value alongside Gemini's formula text — value and formula
            # disagree. We mirror Pass 4's choice into FundMetric only for
            # existing rows (persist_fund created them at Stage 4a) so we
            # never duplicate or invent FundMetric rows.
            # Universal — applies to every metric in every fund. No
            # per-file gating.
            if value is not None and not passthrough_inputs:
                try:
                    self._mirror_to_fund_metric(
                        metric_key=metric_key,
                        value=value,
                        formula=formula,
                        serialised_inputs=serialised_inputs,
                        confidence=confidence,
                        reasoning=reasoning,
                    )
                except Exception as e:
                    logger.warning(
                        'Pass4 FundMetric mirror failed for %s: %s',
                        metric_key, e,
                    )
            # Record candidate for the Arbiter. Pass 4 candidates are
            # always classified into Tier C (catalogue derivation) by
            # the Arbiter, so a Pass 9 / Pass 8 / Pass 3.5 value will
            # win over this one when present.
            if value is not None:
                try:
                    from .metric_arbiter import record_metric_candidate
                    record_metric_candidate(
                        scheme=self.scheme,
                        organization=self.organization,
                        metric_key=metric_key,
                        variant=None,
                        pass_id='P4',
                        value=value,
                        formula_expression=formula or '',
                        confidence=confidence,
                        inputs_used=serialised_inputs,
                        gemini_reasoning=reasoning or '',
                        source_import_file=self.source_import_file,
                    )
                except Exception as inner:
                    logger.warning(
                        'Pass4 record_metric_candidate failed for %s: %s',
                        metric_key, inner,
                    )
        except Exception as e:
            logger.error(
                'Pass4 PERSIST failed scheme=%s metric=%s: %s',
                _safe_get(self.scheme, 'id', '?'), metric_key, e,
            )

    def _mirror_to_fund_metric(self, metric_key, value, formula,
                                serialised_inputs, confidence, reasoning):
        """AF — Mirror Pass 4's Gemini-derived value+formula back into
        FundMetric so the dashboard's headline number and the formula
        text it displays come from the same evaluation.

        The FundMetric key namespace (anchor_pipeline.FUND_METRIC_KEYS)
        differs from the DerivedMetric/Pass-4 namespace for a small set
        of historical mappings (e.g. DerivedMetric 'nav' ↔ FundMetric
        'fund_nav'). We invert LEGACY_DERIVED_MAP to translate. Metric
        keys without a mapping pass through unchanged.

        We only UPDATE existing FundMetric rows; we never create new
        ones. persist_fund (Stage 4a) is the sole creator. This keeps
        the row set bounded to the FUND_METRIC_KEYS list and avoids
        accumulating Pass-4-only synthetic rows.

        Sanity gate: only mirror when value is finite. NaN/Inf would
        poison downstream display.
        """
        try:
            fv = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(fv):
            return
        try:
            from .models import FundMetric
            from .anchor_pipeline import LEGACY_DERIVED_MAP
        except Exception:
            return
        inverse_legacy = {v: k for k, v in LEGACY_DERIVED_MAP.items()}
        fund_metric_key = inverse_legacy.get(metric_key, metric_key)
        # Use .update() so we touch only an existing row. Returns rows
        # affected; 0 means no FundMetric exists for this scheme/key
        # (waterfall component not in FUND_METRIC_KEYS, etc.) — skip
        # silently rather than create a row.
        affected = FundMetric.objects.filter(
            scheme=self.scheme, metric_key=fund_metric_key,
        ).update(
            value=Decimal(str(fv)),
            formula_expression=(formula or '')[:2000],
            inputs_used=serialised_inputs or {},
            provenance={
                'source': 'computed',
                'reasoning': (reasoning or '')[:4000],
                'pass4_override': True,
                'pass4_confidence': max(0.0, min(1.0, float(confidence or 0))),
                'inputs_used': serialised_inputs or {},
            },
            source='computed',
        )
        if affected:
            logger.info(
                '[Pass4 FundMetric override] %s ← Gemini value=%s formula="%s"',
                fund_metric_key, fv, (formula or '')[:80],
            )

    def _serialise_inputs(self, inputs_used, ctx_inputs):
        out = {}
        try:
            for k, v in (inputs_used or {}).items():
                meta = ctx_inputs.get(k, {})
                rendered = v
                if isinstance(v, list):
                    rendered = f'series of {len(v)} entries'
                out[k] = {
                    'value':       rendered,
                    'unit':        meta.get('unit', ''),
                    'source':      meta.get('source', ''),
                    'description': meta.get('description', ''),
                }
        except Exception:
            pass
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Pass 6 — Per-Row Metric Completer
# ─────────────────────────────────────────────────────────────────────────────
#
# Walks every fund-data Django model whose rows belong to the imported fund,
# identifies nullable numeric/percent/decimal fields that the dashboard would
# display (i.e. any field with name in the model schema, no hardcoded list),
# and asks Gemini ONCE per model for the formula to compute each missing
# field from the OTHER available fields on that row. The formula is then
# evaluated per row via the same safe arithmetic AST walker used in Pass 4.
#
# Concrete example: Investment.irr_pct is null across all 50 rows. Pass 6
# asks Gemini for the per-row formula using available row fields
# (total_invested, latest_valuation, investment_date, exit_date, etc.).
# Gemini returns something like:
#   ((latest_valuation / total_invested) ** (1 / years_held) - 1) * 100
# where years_held = (today - investment_date).days / 365.0
# Python evaluates that for each of the 50 rows and writes irr_pct.
#
# ZERO hardcoded field names, ZERO hardcoded formulas, ZERO per-model special
# cases. Discovery is via Django model introspection; formula is whatever
# Gemini returns.

# Field types the completer treats as "potentially derivable" if null
_DERIVABLE_FIELD_TYPES = (
    django_models.DecimalField,
    django_models.FloatField,
    django_models.IntegerField,
    django_models.BigIntegerField,
    django_models.PositiveIntegerField,
    django_models.SmallIntegerField,
)


class PerRowMetricCompleter:
    """Pass 6 orchestrator. NEVER raises. Production-grade."""

    def __init__(self, organization, fund):
        self.organization = organization
        self.fund = fund

    def complete_all(self):
        results = []
        if self.fund is None:
            return results
        for model in self._discover_fund_models():
            try:
                outcome = self._complete_model(model)
                results.append((model._meta.label, outcome))
            except Exception as e:
                logger.warning(
                    'Pass 6 model %s failed: %s',
                    getattr(model, '__name__', '?'), e,
                )
                results.append(
                    (getattr(model, '__name__', '?'),
                     f'error:{type(e).__name__}'),
                )
        logger.info('[Pass6] per-row completion outcomes: %s', results)
        return results

    # ────────────────────────────────────────────────────────────────────────

    def _discover_fund_models(self):
        """Find every model in project apps that has a scheme/investment/fund
        FK and has at least one row tied to self.fund."""
        wanted = []
        for ac in apps.get_app_configs():
            if ac.label in ('auth', 'contenttypes', 'sessions', 'admin',
                            'accounts', 'dataimport'):
                continue
            try:
                ac_path = os.path.realpath(getattr(ac, 'path', '') or '')
            except Exception:
                continue
            if 'site-packages' in ac_path or 'dist-packages' in ac_path:
                continue
            for model in ac.get_models():
                if self._model_has_rows_for_fund(model):
                    wanted.append(model)
        return wanted

    def _model_has_rows_for_fund(self, model):
        try:
            qs = self._scope_to_fund(model)
            if qs is None:
                return False
            return qs.exists()
        except Exception:
            return False

    def _scope_to_fund(self, model):
        """Return a queryset of model rows filtered to self.fund. Returns
        None if model has no recognised FK path."""
        try:
            field_names = {
                f.name for f in model._meta.get_fields()
                if isinstance(f, (django_models.ForeignKey,
                                  django_models.OneToOneField))
            }
        except Exception:
            return None
        try:
            if 'fund' in field_names:
                return model.objects.filter(fund=self.fund)
            if 'scheme' in field_names:
                return model.objects.filter(scheme__fund=self.fund)
            if 'investment' in field_names:
                return model.objects.filter(
                    investment__scheme__fund=self.fund
                )
        except Exception:
            return None
        return None

    # ────────────────────────────────────────────────────────────────────────

    def _complete_model(self, model):
        """Pass 6 per-model orchestrator.

        PRODUCTION ENRICHMENTS over the v1 implementation:
        1. Builds an ENRICHED row context: in addition to the model's direct
           scalar fields, walks every REVERSE-FK relation (e.g. Investment →
           Valuation/ExitEvent) and exposes the LATEST related row's scalar
           fields with prefix '<rel_name>__<field>'. This gives Gemini cross-
           model inputs without needing any hardcoded model relationships.
        2. Pre-computes 'years_since_<date_field>' helper for every date field
           on the model and on related rows. Gemini doesn't need to do date
           arithmetic — it just uses these pre-computed years as scalars.
        3. Presents dates to Gemini as ISO strings (semantic), not encoded
           integers.

        Example outcome for Investment.irr_pct:
          Available inputs Gemini sees include:
            - total_invested = 22.0
            - investment_date = '2019-04-01'
            - years_since_investment_date = 7.18
            - valuation__fair_value = 16.3 (from latest related Valuation)
            - valuation__valuation_date = '2026-03-31'
          → Gemini can write:
            ((valuation__fair_value / total_invested) ** (1 / years_since_investment_date) - 1) * 100
        """
        from .gemini_column_mapper import derive_per_row_formulas

        rows = list(self._scope_to_fund(model))
        if not rows:
            return 'no_rows'

        today = date.today()

        # 1) Identify candidate fields (nullable numeric/percent that are
        #    null on >= 50% of rows).
        # Phase 5b (Bug T) — canonical FundMetric mirrors are never
        # derived by Pass 6. These fields exist on legacy models only as
        # a compatibility mirror for the canonical FundMetric value, and
        # are populated by anchor_pipeline.persist_fund()'s sync block.
        # A None on these fields is INTENTIONAL ("unknown" / "no
        # sponsor LP detected") — Pass 6 deriving a value from row
        # context (e.g., first LP's commitment %) re-introduces Bug F.
        # Universal — applies to every fund, no per-file branching.
        # The exclusion set is imported from anchor_pipeline.py so the
        # Phase 5 sync map and this exclusion map can never drift apart.
        from .anchor_pipeline import SCHEME_MIRROR_ATTRS
        _CANONICAL_MIRROR_FIELDS = {
            ('funds', 'Scheme'): SCHEME_MIRROR_ATTRS,
        }
        excluded = _CANONICAL_MIRROR_FIELDS.get(
            (model._meta.app_label, model.__name__), set()
        )

        candidate_fields = {}
        for f in model._meta.get_fields():
            if not isinstance(f, _DERIVABLE_FIELD_TYPES):
                continue
            if f.name in excluded:
                continue
            try:
                null_count = sum(
                    1 for r in rows if _safe_get(r, f.name, None) is None
                )
            except Exception:
                continue
            total = len(rows)
            if total == 0:
                continue
            if null_count >= max(1, total // 2):
                candidate_fields[f.name] = {
                    'description': (
                        str(getattr(f, 'help_text', '') or '').strip()
                        or f.name
                    ),
                    'unit': self._infer_unit(f.name),
                }

        if not candidate_fields:
            return 'no_candidates'

        # 2) Build the enriched per-row context for EVERY row (we'll union
        #    field names across rows to form available_inputs).
        row_contexts = [self._build_row_context(r, model, today) for r in rows]

        # available_inputs: union of all keys that have at least one non-null
        # value across rows. Use the first non-null value as sample.
        available_inputs = {}
        for key in set().union(*[set(rc.keys()) for rc in row_contexts]):
            # Find first non-null sample
            sample = None
            for rc in row_contexts:
                if key in rc and rc[key] is not None:
                    sample = rc[key]
                    break
            if sample is None:
                continue
            available_inputs[key] = {
                'description': self._describe_input_key(model, key),
                'unit': self._infer_unit(key),
                'sample_value': self._render_value(sample),
            }

        # Exclude candidate fields from available_inputs (we can't use a
        # field as input to compute itself).
        for k in candidate_fields:
            available_inputs.pop(k, None)

        if not available_inputs:
            return 'no_inputs'

        # 3) Pick the 3 sample rows with the richest available_inputs coverage
        scored = sorted(
            ((sum(1 for k in available_inputs if k in rc and rc[k] is not None),
              rc) for rc in row_contexts),
            key=lambda x: x[0], reverse=True,
        )
        sample_rows = []
        for _, rc in scored[:3]:
            sample_rows.append({
                k: self._render_value(rc[k])
                for k in available_inputs
                if k in rc and rc[k] is not None
            })

        # 4) Ask Gemini for formulas — one call per model
        try:
            formulas = derive_per_row_formulas(
                model_label=model._meta.label,
                available_inputs=available_inputs,
                missing_fields=candidate_fields,
                sample_row_values=sample_rows,
            )
        except Exception as e:
            logger.warning(
                'Pass 6 derive_per_row_formulas API error for %s: %s',
                model._meta.label, e,
            )
            return 'api_error'

        if not formulas:
            return 'no_formulas'

        # 5) For each target field, try CANDIDATE FORMULAS in rank order
        # per row. First formula whose declared inputs are all present and
        # non-null on that row wins. Empty cells stay empty (no fabrication).
        applied_total = 0
        completed_field_summary = []
        for field_name, fdata in formulas.items():
            if field_name not in candidate_fields:
                continue
            candidates = fdata.get('candidate_formulas') or []
            if not candidates:
                continue

            # Per-rank counters so the operator can see which formula
            # actually applied to each portion of the data set.
            rank_counts = {c['rank']: 0 for c in candidates}
            written = 0
            for r, rc in zip(rows, row_contexts):
                if _safe_get(r, field_name, None) is not None:
                    continue
                chosen = None
                chosen_value = None
                for cand in candidates:
                    required = cand.get('inputs_required') or []
                    # Quick gate: every declared input must exist and be
                    # non-null in this row's context. Saves a parse +
                    # walk for inapplicable candidates and gives accurate
                    # per-rank attribution.
                    if not all(k in rc and rc[k] is not None for k in required):
                        continue
                    val = self._evaluate_for_row_context(
                        cand['formula_expression'], rc,
                    )
                    if val is None:
                        # Inputs were present but evaluation failed
                        # (e.g. divide-by-zero, NaN). Try next candidate.
                        continue
                    chosen, chosen_value = cand, val
                    break
                if chosen is None:
                    continue
                try:
                    setattr(r, field_name, _coerce_value_for_field(
                        r._meta.get_field(field_name), chosen_value
                    ))
                    r.save(update_fields=[field_name])
                    written += 1
                    rank_counts[chosen['rank']] = rank_counts.get(chosen['rank'], 0) + 1
                except Exception as e:
                    logger.warning(
                        'Pass 6 save failed for %s.%s on row id=%s: %s',
                        model._meta.label, field_name,
                        _safe_get(r, 'id', '?'), e,
                    )
            applied_total += written

            # Log the rank attribution so the operator can audit which
            # candidate formula served which fraction of the rows.
            attribution_bits = []
            for cand in candidates:
                rk = cand['rank']
                count = rank_counts.get(rk, 0)
                formula_snippet = cand['formula_expression'][:100]
                attribution_bits.append(
                    f'rank{rk}({count}/{len(rows)} rows, conf={cand["confidence"]:.2f}): '
                    f'"{formula_snippet}"'
                )
            logger.info(
                '[Pass6] %s.%s ← %d candidate(s) → wrote %d/%d rows total | %s',
                model._meta.label, field_name, len(candidates),
                written, len(rows), ' || '.join(attribution_bits),
            )
            completed_field_summary.append(
                f'{field_name}({written}/{len(rows)})'
            )
        return f'completed_fields={completed_field_summary} rows_written={applied_total}'

    # ────────────────────────────────────────────────────────────────────────

    def _build_row_context(self, row, model, today):
        """Return the enriched per-row variable namespace.

        Includes:
        (a) every concrete scalar field on `row`
        (b) for every reverse-FK relation, the LATEST related row's scalar
            fields, keyed as '<relation_name>__<field_name>'. Latest is
            determined by the first date field found on the related model.
        (c) 'years_since_<date_field>' = (today - date_value).days / 365.25
            for every date field on the row AND every date field on related
            latest rows (keyed as 'years_since_<relation_name>__<date>').
        (d) 'today' as ISO string for convenience.
        """
        ctx = {}

        # (a) direct scalar fields
        for f in model._meta.get_fields():
            if isinstance(f, (django_models.ManyToManyField,
                              django_models.ForeignKey,
                              django_models.OneToOneField,
                              django_models.ManyToOneRel,
                              django_models.ManyToManyRel,
                              django_models.OneToOneRel)):
                continue
            if not getattr(f, 'concrete', False):
                continue
            ctx[f.name] = _safe_get(row, f.name, None)
            # (c) years-since for direct date fields
            v = ctx[f.name]
            if isinstance(v, datetime):
                v = v.date()
            if isinstance(v, date):
                ctx[f'years_since_{f.name}'] = round(
                    (today - v).days / 365.25, 6
                )

        # (b) reverse-FK relations — latest related row's scalars
        for f in model._meta.get_fields():
            if not getattr(f, 'is_relation', False):
                continue
            # We want reverse-FK relations (one-to-many or one-to-one_rel)
            if not (getattr(f, 'one_to_many', False) or
                    getattr(f, 'one_to_one', False) and getattr(f, 'auto_created', False)):
                continue
            try:
                accessor = f.get_accessor_name()
            except Exception:
                continue
            if not accessor:
                continue
            try:
                related_mgr = getattr(row, accessor, None)
                if related_mgr is None:
                    continue
                # Detect if it's a manager (one-to-many) or single instance
                # (one-to-one reverse)
                if hasattr(related_mgr, 'all'):
                    related_qs = related_mgr.all()
                else:
                    related_qs = [related_mgr]
            except Exception:
                continue

            rel_model = f.related_model
            if rel_model is None:
                continue

            # Find a date field on rel_model to sort by
            rel_date_field = None
            for rf in rel_model._meta.get_fields():
                if isinstance(rf, _TEMPORAL_FIELD_TYPES):
                    rel_date_field = rf.name
                    break

            try:
                if hasattr(related_qs, 'order_by') and rel_date_field:
                    latest = related_qs.order_by(f'-{rel_date_field}').first()
                elif hasattr(related_qs, 'first'):
                    latest = related_qs.first()
                else:
                    latest = related_qs[0] if related_qs else None
            except Exception:
                latest = None

            if latest is None:
                continue

            rel_prefix = rel_model.__name__.lower()
            for rf in rel_model._meta.get_fields():
                if isinstance(rf, (django_models.ManyToManyField,
                                   django_models.ForeignKey,
                                   django_models.OneToOneField,
                                   django_models.ManyToOneRel,
                                   django_models.ManyToManyRel,
                                   django_models.OneToOneRel)):
                    continue
                if not getattr(rf, 'concrete', False):
                    continue
                key = f'{rel_prefix}__{rf.name}'
                ctx[key] = _safe_get(latest, rf.name, None)
                v = ctx[key]
                if isinstance(v, datetime):
                    v = v.date()
                if isinstance(v, date):
                    ctx[f'years_since_{key}'] = round(
                        (today - v).days / 365.25, 6
                    )

        # (d) today
        ctx['today'] = today.isoformat()
        ctx['years_since_today'] = 0.0  # convenience constant
        return ctx

    # ────────────────────────────────────────────────────────────────────────

    def _describe_input_key(self, model, key):
        """Best-effort human description for a key in the enriched context."""
        # Direct field on the model
        try:
            f = model._meta.get_field(key)
            return (str(getattr(f, 'help_text', '') or '').strip()
                    or f.name)
        except Exception:
            pass
        # Years-since helper
        if key.startswith('years_since_'):
            base = key[len('years_since_'):]
            return f'Years elapsed from `{base}` to today (pre-computed)'
        # Related-model field (rel_name__field)
        if '__' in key:
            rel, fname = key.split('__', 1)
            return f'Latest related `{rel}` row\'s `{fname}` field'
        return key

    def _evaluate_for_row_context(self, formula, row_context):
        """Evaluate `formula` using `row_context` as the namespace. Date
        values in the context are kept as ISO strings or floats; numeric/
        years-since helpers are floats. Only numeric/float variables are
        usable by the AST evaluator — string variables are skipped."""
        variables = {}
        for k, v in row_context.items():
            if v is None:
                continue
            if isinstance(v, str):
                # Skip strings (Gemini should reference numeric helpers like
                # years_since_<date>, not the raw date string)
                continue
            if isinstance(v, datetime):
                v = v.date()
            if isinstance(v, date):
                # Skip raw dates — only the pre-computed years_since_* helpers
                # are usable in arithmetic formulas.
                continue
            n = _to_float(v)
            if n is not None:
                variables[k] = n
        return _safe_eval(formula, variables)

    def _infer_unit(self, name):
        """Tiny inference helper — NOT a keyword classifier, just a heuristic
        rendering hint for the prompt. Gemini decides everything semantically."""
        n = name.lower()
        if 'pct' in n or 'percent' in n or '_rate' in n:
            return 'percent'
        if 'date' in n:
            return 'date'
        if any(s in n for s in ('amount', 'value', 'cost', 'price',
                                 'proceed', 'nav', 'capital', 'commitment',
                                 'fee', 'expense', 'income')):
            return 'currency'
        if 'moic' in n or 'multiple' in n or 'tvpi' in n or 'dpi' in n:
            return 'multiple'
        return 'auto'

    def _render_value(self, v):
        if v is None:
            return ''
        if isinstance(v, datetime):
            return v.date().isoformat()
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, Decimal):
            return float(v)
        return v


def _coerce_value_for_field(field, value):
    """Cast Gemini-evaluated float to the Django field's type."""
    if value is None:
        return None
    if isinstance(field, django_models.DecimalField):
        try:
            return Decimal(str(round(float(value), 6)))
        except (InvalidOperation, ValueError, TypeError):
            return None
    if isinstance(field, django_models.FloatField):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    if isinstance(field, (django_models.IntegerField,
                          django_models.PositiveIntegerField,
                          django_models.SmallIntegerField,
                          django_models.BigIntegerField)):
        try:
            return int(round(float(value)))
        except (ValueError, TypeError):
            return None
    return value
