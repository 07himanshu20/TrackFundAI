"""Retro-run Pass 4 (metric derivation) for the most recently imported fund."""
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from funds.models import Fund, Scheme
from dataimport.models import ImportFile
from dataimport.derivation_service import MetricDerivationService, DerivationContext
import traceback

# Direct call to derive_metric_via_gemini for one metric, to expose the
# actual traceback inside the function rather than the bubbled error.
def _direct_trace_one(scheme):
    print('\n--- direct trace test: net_irr ---')
    try:
        ctx = DerivationContext(scheme).build()
        print(f'context built: {len(ctx.inputs)} inputs')
        # print first 5 inputs
        for i, (k, v) in enumerate(ctx.inputs.items()):
            if i >= 8: break
            vv = v.get('value')
            if isinstance(vv, list): vv = f'list({len(vv)})'
            print(f'  {k}: value={vv!r}  unit={v.get("unit")}')
        from dataimport.gemini_column_mapper import derive_metric_via_gemini
        from dataimport.canonical_schema import DERIVABLE_FUND_METRICS
        meta = DERIVABLE_FUND_METRICS['net_irr']
        print('calling derive_metric_via_gemini...')
        r = derive_metric_via_gemini(
            metric_key='net_irr', metric_meta=meta,
            available_inputs=ctx.inputs, scheme_context='trace test'
        )
        print('got result:', {k: (v if k != 'candidates' else f'list({len(v)})') for k, v in r.items()})
    except Exception:
        traceback.print_exc()

latest = ImportFile.objects.exclude(fund=None).order_by('-created_at').first()
if not latest:
    print('No imported fund found.')
    raise SystemExit(1)

fund = latest.fund
print(f'Latest import: {latest.original_filename}')
print(f'Fund: {fund.name}  (org={fund.organization.name})')

schemes = list(fund.schemes.all())
print(f'Schemes on fund: {len(schemes)}')

_direct_trace_one(schemes[0])

for sch in schemes:
    print(f'  --- scheme: {sch.name or sch.id} ---')
    svc = MetricDerivationService(
        organization=fund.organization,
        scheme=sch,
        source_import_file=latest,
    )
    outcomes = svc.derive_all()
    for metric_key, status in outcomes:
        print(f'    {metric_key:<10s} → {status}')

# Dump resulting DerivedMetric rows
from dataimport.models import DerivedMetric
print('\nDerivedMetric rows now in DB:')
for dm in DerivedMetric.objects.filter(scheme__fund=fund).order_by('metric_key'):
    val = f'{float(dm.value):.4f}' if dm.value is not None else 'NULL'
    print(f'  {dm.metric_key:<10s} value={val:<14s} confidence={dm.confidence or 0:.2f}')
    if dm.formula_expression:
        print(f'             formula: {dm.formula_expression}')
