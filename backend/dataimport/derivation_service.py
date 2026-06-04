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
import operator
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
        try:
            from .models import DerivedMetric
            for dm in DerivedMetric.objects.filter(
                scheme=scheme,
                formula_expression='(direct value imported)'
            ).exclude(value=None):
                self._add(
                    key=f'imported__{dm.metric_key}',
                    value=_to_float(dm.value),
                    unit='direct',
                    description=(
                        f'Direct value extracted from Excel for {dm.metric_key} '
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

        # Sum every numeric field on the model
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

        # MAX for date fields — useful for "latest record" type info
        for f in model._meta.get_fields():
            if not isinstance(f, _TEMPORAL_FIELD_TYPES):
                continue
            try:
                dates = []
                for r in rows:
                    v = _safe_get(r, f.name, None)
                    if isinstance(v, datetime):
                        v = v.date()
                    if isinstance(v, date):
                        dates.append(v)
                if dates:
                    self._add(
                        key=f'max__{label_base}__{f.name}',
                        value=max(dates).isoformat(),
                        unit='date',
                        description=(
                            f'Most recent {model.__name__}.{f.name}'
                        ),
                        source=f'{model._meta.label}.{f.name}',
                    )
            except Exception:
                continue

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
    """
    try:
        from .models import DerivedMetric
        dm = DerivedMetric.objects.filter(
            scheme=scheme, metric_key=metric_key,
            formula_expression='(direct value imported)',
        ).exclude(value=None).first()
        if dm and dm.value is not None:
            return True, _to_float(dm.value)
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

        logger.info(
            '[Pass4] scheme=%s outcomes=%s',
            _safe_get(self.scheme, 'name', '?'),
            results,
        )
        return results

    # ────────────────────────────────────────────────────────────────────────

    def _derive_one(self, metric_key, meta, ctx, scheme_ctx):
        from .models import DerivedMetric

        # 0) Direct-value path: Excel already gave us this number → persist
        # with provenance and we're done.
        try:
            has_direct, direct_val = _direct_value_exists(self.scheme, metric_key)
        except Exception as e:
            logger.warning('Pass4 direct-check failed for %s: %s', metric_key, e)
            has_direct, direct_val = False, None

        if has_direct:
            self._persist(
                metric_key=metric_key,
                value=direct_val,
                formula='(direct value imported)',
                inputs_used={},
                ctx_inputs=ctx.inputs,
                confidence=1.0,
                reasoning='Direct value imported from Excel — no derivation needed.',
                candidates=[],
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

        formula = (gemini_out.get('formula_expression') or '').strip()
        inputs_used = gemini_out.get('inputs_used') or {}
        reasoning = (gemini_out.get('reasoning') or '')[:4000]
        candidates = gemini_out.get('candidates') or []
        try:
            confidence = float(gemini_out.get('confidence') or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        # 2) Evaluate the formula Python-side.
        try:
            computed = self._evaluate(formula, inputs_used)
        except Exception as e:
            logger.warning(
                'Pass4 evaluation failed for %s (formula=%r): %s',
                metric_key, formula, e,
            )
            computed = None

        # 3) Persist regardless of outcome — null value with provenance is
        # still useful to the dashboard (provenance panel will explain why).
        self._persist(
            metric_key=metric_key,
            value=computed,
            formula=formula[:2000],
            inputs_used=inputs_used,
            ctx_inputs=ctx.inputs,
            confidence=confidence,
            reasoning=reasoning,
            candidates=candidates,
        )

        return 'derived' if computed is not None else 'unviable'

    # ────────────────────────────────────────────────────────────────────────

    def _evaluate(self, formula, inputs_used):
        if not formula:
            return None
        f = formula.strip()
        # XIRR family: e.g. "XIRR(cashflow_series)" or just "XIRR"
        if f.lower().lstrip().startswith('xirr'):
            series = inputs_used.get('cashflow_series')
            if series is None:
                # Gemini may have placed the series under another key
                for v in inputs_used.values():
                    if isinstance(v, list):
                        series = v
                        break
            return _compute_xirr(series)

        # Arithmetic formula — build numeric namespace from inputs_used
        # (skip non-numeric entries like cashflow_series).
        variables = {}
        for k, v in inputs_used.items():
            fv = _to_float(v)
            if fv is not None:
                variables[k] = fv
        return _safe_eval(f, variables)

    # ────────────────────────────────────────────────────────────────────────

    def _persist(self, metric_key, value, formula, inputs_used, ctx_inputs,
                 confidence, reasoning, candidates):
        from .models import DerivedMetric
        try:
            DerivedMetric.objects.update_or_create(
                scheme=self.scheme,
                metric_key=metric_key,
                defaults={
                    'organization': self.organization,
                    'value': (Decimal(str(value)) if value is not None else None),
                    'formula_expression': (formula or '')[:2000],
                    'inputs_used': self._serialise_inputs(inputs_used, ctx_inputs),
                    'confidence': max(0.0, min(1.0, confidence)),
                    'gemini_reasoning': (reasoning or '')[:4000],
                    'candidate_formulas': candidates or [],
                    'source_import_file': self.source_import_file,
                },
            )
        except Exception as e:
            logger.error(
                'Pass4 PERSIST failed scheme=%s metric=%s: %s',
                _safe_get(self.scheme, 'id', '?'), metric_key, e,
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
