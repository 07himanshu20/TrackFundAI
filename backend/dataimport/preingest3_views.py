"""
API for the preingest3 CIR extraction engine + the review-gate write-back loop.

    POST   /api/dataimport/preingest3/                  upload N files → start a run (async)
    GET    /api/dataimport/preingest3/list/             list preingest3 jobs
    GET    /api/dataimport/preingest3/<id>/             job status + full review payload
    POST   /api/dataimport/preingest3/<id>/run/         re-run this job's files (after a resolve)
    POST   /api/dataimport/preingest3/<id>/resolve/     confirm an entity alias (human write-back)
    POST   /api/dataimport/preingest3/<id>/ratecard/    supply a rate card (validated intake) → re-run
    DELETE /api/dataimport/preingest3/files/<file_id>/  remove one uploaded input file
    GET    /api/dataimport/preingest3/<id>/download/    download the consolidated workbook

Guardrails baked in (multi-tenant, multi-worker):
  • every endpoint is org-checked (job.organization must equal request.organization);
  • writes (upload / run / resolve / delete) require IsGPAdmin; reads require IsGPUser;
  • the run is an async background job (returns a job id immediately, worker polls);
  • the alias store is the DB-backed, org-scoped DbAliasLedger (transactional).
The engine itself stays Django-free; this module is the only wiring layer.
"""
import json
import logging
import os
import threading

from django.conf import settings
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.permissions import IsGPAdmin, IsGPUser
from .models import PreIngestInputFile, PreIngestJob, PreIngestAlias, preingest_output_path
from .preingest3_store import DbAliasLedger

logger = logging.getLogger(__name__)

_ENGINE = 'preingest3'
_MIS_DOMAINS = ('portfolio_investments', 'company', 'mis')
_CR = 'value_cr'

_GOLDEN_UNSET = object()


def _golden_store_base():
    """The durable, writable BASE directory for the preingest3 golden-record (value) cache — ACTIVATED in
    production so a repeat run, or a re-run after editing one file, reuses the unchanged files' finished
    extractions across processes instead of recomputing the whole fund. Defaults to a durable path under
    MEDIA_ROOT (the same writable volume the output workbook is saved to just below), so activation is real
    without any extra config; a deployment may override the location or DISABLE the cache entirely via
    settings.PREINGEST3_GOLDEN_STORE_DIR (set it to None/'' to turn the store off). The pipeline namespaces
    this base by org, so cross-tenant isolation holds even on one shared base dir — never key a number by
    anything less than the full content fingerprint, and never share a tenant's answers with another."""
    base = getattr(settings, 'PREINGEST3_GOLDEN_STORE_DIR', _GOLDEN_UNSET)
    if base is _GOLDEN_UNSET:
        base = os.path.join(settings.MEDIA_ROOT, 'preingest3', 'golden')
    return base or None


# ── CIR → review JSON (the stable contract the UI binds to) ──────────────────
def _fig_json(f):
    return {
        'concept': f.concept,
        'value_cr': None if f.value_cr is None else float(f.value_cr),
        'state': 'held' if f.held else ('gap' if f.gap else 'emitted'),
        'reason': f.hold_reason or '',
        'basis': f.basis or '', 'months': f.months,
        'source': f.provenance.source_file, 'sheet': f.provenance.sheet,
        'cell': f.provenance.cell, 'row_label': f.provenance.row_label,
    }


def _serialize(result, as_of, rate_card):
    from .preingest3.cir import Figure
    cir = result.cir
    companies = {}
    for r in cir.records:
        if r.domain not in _MIS_DOMAINS:
            continue
        name = r.entity_id or r.fields.get('company') or '—'
        row = companies.setdefault(name, {'company': name, 'figures': {}})
        for v in r.fields.values():
            if isinstance(v, Figure):
                row['figures'][v.concept] = _fig_json(v)

    def _total(concept):
        vals, held = [], False
        for c in companies.values():
            fj = c['figures'].get(concept)
            if not fj:
                continue
            if fj['state'] == 'emitted' and fj['value_cr'] is not None:
                vals.append(fj['value_cr'])
            elif fj['state'] == 'held':
                held = True
        return 'INCOMPLETE' if held else round(sum(vals), 4)

    files = [{'label': fr.label, 'role': fr.role, 'status': fr.status,
              'entity_id': fr.entity_id, 'reason': fr.reason} for fr in result.files]
    # boundary telemetry — 'the model ran / didn't' is now measured, not assumed
    model_info = {'metrics': getattr(result, 'model_metrics', None),
                  'health': getattr(result, 'model_health', None)}
    review = [{'label': rf.label, 'content_fp': rf.content_fp,
               'identifiers': rf.identifiers, 'candidates': rf.candidates,
               'reason': rf.reason} for rf in result.review_queue]
    return {
        'engine': _ENGINE, 'as_of': as_of, 'rate_card_id': rate_card.card_id,
        'counts': {
            'files': len(files),
            'attributed': sum(1 for f in files if f['status'] == 'attributed'),
            'held_files': len(review),
            'read_errors': sum(1 for f in files if f['status'] == 'read_error'),
            'companies': len(companies),
        },
        'files': files,
        'review_queue': review,
        'companies': sorted(companies.values(), key=lambda c: c['company']),
        'disclosures': [{'kind': d.get('kind', ''), 'entity': d.get('entity', ''),
                         'detail': d.get('detail', '')} for d in cir.disclosures],
        'totals': {
            'deployed_cost_cr': _total('cost'), 'fair_value_cr': _total('fair_value'),
            'revenue_annualized': _total('revenue'), 'ebitda_annualized': _total('ebitda'),
        },
        'model': model_info,
        # U6 Phase 5 — the uncovered-currency report is the CONTRACT the prompt reads: `prompt_required`
        # gates whether the UI asks at all; `uncovered` (+ top-level `as_of`) is what it REQUESTS a rate
        # for; the foreign-domicile buckets are DISCLOSED, not requested. None/empty ⇒ an all-INR run ⇒
        # no prompt. Passed straight through — it is already plain JSON from `uncovered_report`.
        'currency_report': result.currency_report,
    }


# ── background worker (async run) ────────────────────────────────────────────
def _parse_rate_card_field(raw):
    """A rate_card run-input field (dict or JSON string) → payload dict, or None.
    Raises ValueError on malformed JSON so the caller can reject with 400."""
    if raw in (None, '', {}):
        return None
    if isinstance(raw, dict):
        return raw
    return json.loads(raw)


# The disclosed RUN INPUTS that must survive the summary overwrite. `job.summary` conflates a run's
# INPUTS (what it was given) with its OUTPUTS (_serialize's view of the results); a run replaces the whole
# field with outputs, so without this carry-forward every input is dropped. That was masked for the rate
# card only because /ratecard/ re-injects it immediately before its own re-run — a coincidence of one flow,
# not a design. Carrying ALL inputs forward makes any re-run path durable (confirm base → supply a foreign
# rate → re-run keeps both), and is the root fix, not a per-feature stash. `as_of` is carried by the caller.
_RUN_INPUT_KEYS = ('rate_card', 'base_currency')


def _carry_run_inputs(prior, payload):
    """Copy the disclosed run inputs from the prior summary into the freshly-serialized payload, in place.
    Only present inputs are copied (a never-set input stays absent — no spurious keys), so the default path
    is unchanged."""
    for k in _RUN_INPUT_KEYS:
        if prior.get(k) is not None:
            payload[k] = prior[k]
    return payload


def _validate_base_currency(raw):
    """The fund's user-confirmed base reporting currency (the fund-level confirm-prompt). It is a THIRD
    positive-evidence source at resolve_currency, applied ONLY where a file has no token and no domicile;
    a file carrying its own foreign token/domicile still HOLDS under it (conflict guard). Empty → None.

    FAIL-CLOSED to INR: the whole system is INR-reporting BY CONSTRUCTION (ratecard.BASE_CURRENCY, the
    INR magnitude anchors, and FV-INR-by-source), so INR is the only base it can HONOR today. A non-INR
    base is REFUSED here rather than accepted and then silently misframed downstream — a foreign statement
    is handled by ITS currency + a rate (the rate-card box), never by changing the fund base. The engine's
    resolve_currency stays currency-general, so this gate is the single place to widen when a genuinely
    non-INR-reporting fund is ever onboarded (the documented BASE_CURRENCY assumption)."""
    if raw in (None, '', {}):
        return None
    from .preingest3.ratecard import BASE_CURRENCY
    bc = str(raw).strip().upper()
    if bc != BASE_CURRENCY:
        raise ValueError(
            f'base_currency must be {BASE_CURRENCY} — the system reports in {BASE_CURRENCY}; a '
            f'non-{BASE_CURRENCY}-reporting fund is not yet supported. A foreign statement is handled '
            f'by its own currency and a rate (the rate card), not by changing the fund base.')
    return bc


def _rows_from_schedule_upload(uploaded):
    """Parse an uploaded rate-schedule workbook into the first sheet-grid that carries a recognisable
    currency+rate header (format-agnostic — via the intake's own header matcher, never hardcoded
    positions). Returns the rows list, or None if no sheet has a schedule header. A server-side parse
    (reusing the engine's data_only reader) keeps BOTH intake paths behind the SAME fail-closed gate: an
    uploaded schedule is then validated exactly like typed entries, never trusted for being a file."""
    import tempfile
    from .preingest3.profiler import _read_grid
    from .preingest3.ratecard_intake import _match_header_row
    tmp = tempfile.NamedTemporaryFile(suffix=os.path.splitext(uploaded.name)[1] or '.xlsx', delete=False)
    try:
        for chunk in uploaded.chunks():
            tmp.write(chunk)
        tmp.close()
        grid = _read_grid(tmp.name)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    for rows in grid.values():
        if _match_header_row(rows) is not None:
            return rows
    return None


def _accept_card(*, manual, rows, as_of, uncovered):
    """U6 Phase 5 — validate a user-supplied Rate Card through the fail-closed INTAKE gate and disclose
    its actionability against the run. Returns (card, noop_currencies, still_uncovered); raises IntakeError
    on any VALIDITY failure (the caller turns that into a 400). The two inputs are CO-EQUAL — `manual`
    (typed pair/rate/date/source) and `rows` (an uploaded schedule grid) — and share one gate. A VALID
    rate the run does not need is NOT refused: it rides on the card (the standing/superset-card case) and
    is returned in `noop`. `uncovered` is the run's rate-actionable set (None if the job never reported)."""
    from .preingest3 import ratecard_intake
    card = (ratecard_intake.card_from_manual(list(manual), as_of=as_of) if manual
            else ratecard_intake.card_from_schedule(list(rows), as_of=as_of))
    noop = ratecard_intake.noop_currencies(card, uncovered=uncovered)
    still = sorted((uncovered or set()) - set(card.rates))       # what the run still needs, uncovered
    return card, noop, still


def _rate_card_for(summary, as_of):
    """U6 run-input selection. A supplied, ATTRIBUTED card (payload in job.summary)
    is used; absent → INR-only, so any foreign figure fail-closes to a HOLD downstream
    (unresolved frame / uncovered currency), NEVER a silent INR-default conversion. A
    malformed card raises → the run fails loudly (never a wrong-currency number)."""
    from .preingest3.ratecard import RateCard, default_inr_card
    payload = (summary or {}).get('rate_card')
    if payload:
        p = dict(payload)
        p.setdefault('as_of', as_of)
        return RateCard.from_input(p)
    return default_inr_card(as_of)


def _prior_rate_for(job, currency):
    """The currency's last-entered rate available to this org, for the sanity-band REFERENCE only — never
    a conversion input (the operative rate is entered fresh each upload). Per-currency lookback: THIS job's
    already-stored card first (an in-loop correction/typo), then the most recent OTHER job of the same org
    that carried the currency. None if never seen (a first upload → the band cannot check). Self-healing —
    it tracks whatever the last accepted rate was, org-scoped."""
    from .preingest3.quantity import to_decimal

    def _rate_in(summary):
        for r in ((summary or {}).get('rate_card') or {}).get('rates', []):
            if r.get('currency') == currency:
                return to_decimal(r.get('inr_per_unit', r.get('rate')))
        return None

    here = _rate_in(job.summary)
    if here is not None:
        return here
    prior = (PreIngestJob.objects.filter(organization=job.organization)
             .exclude(id=job.id).order_by('-created_at')[:100])          # bounded org-scoped lookback
    for pj in prior:
        r = _rate_in(pj.summary)
        if r is not None:
            return r
    return None


def _band_partition(job, manual):
    """Rate-entry sanity band (#2). Split manually-typed rates into (accepted, suspect, unverified) by the
    fat-finger decimal-shift fingerprint against each currency's OWN prior rate (_prior_rate_for):
      • suspect  — a power-of-ten shift vs the prior rate, NOT confirmed → HELD for review ('confirm or
                   re-enter'); dropped from the card so the currency holds via the existing uniform-currency
                   (FX_UNCOVERED) engine (no new hold type).
      • unverified — a first upload (no prior to check) → ACCEPTED (converts) + a soft disclosure; a confirm
                   with nothing to check is theatre, so it is never forced.
      • accepted — a genuine ±% move, a first upload, or an explicit confirmed:true override.
    A malformed entry (unparseable currency/rate) is left in `accepted` so the intake VALIDITY gate still
    refuses it (the band judges typo-plausibility, not validity)."""
    from .preingest3.ratecard_intake import _foreign_of_pair, _decimal_shift_k
    from .preingest3.quantity import to_decimal
    accepted, suspect, unverified = [], [], []
    for e in manual:
        ccy = _foreign_of_pair(e.get('currency', e.get('pair', '')))
        rate = to_decimal(e.get('inr_per_unit', e.get('rate')))
        ref = _prior_rate_for(job, ccy) if ccy else None
        k = _decimal_shift_k(rate, ref) if (ccy and rate is not None) else None
        if k is not None and not e.get('confirmed'):
            fold = ('×' if k > 0 else '÷') + f'10^{abs(k)}'
            suspect.append({'currency': ccy, 'rate': str(rate), 'prior_rate': str(ref), 'shift': fold,
                            'reason': f'{ccy} rate {rate} looks like a decimal-shift typo of the prior rate '
                                      f'{ref} ({fold}) — confirm or re-enter'})
        else:
            accepted.append(e)
            if ccy and rate is not None and ref is None:
                unverified.append({'currency': ccy, 'rate': str(rate),
                                   'reason': f'{ccy} rate accepted — no prior rate to sanity-check against '
                                             '(first upload for this currency; unverified)'})
    return accepted, suspect, unverified


def _run_job(job_id):
    from .preingest3 import pipeline, master_workbook, rate_governor

    job = PreIngestJob.objects.get(pk=job_id)
    try:
        job.status = 'processing'
        job.progress_pct = 0
        job.progress_message = 'Starting'
        job.save(update_fields=['status', 'progress_pct', 'progress_message'])

        files = [(os.path.splitext(inf.original_name)[0], inf.file.path)
                 for inf in job.input_files.all()]
        as_of = (job.summary or {}).get('as_of') or timezone.now().date().isoformat()
        rc = _rate_card_for(job.summary, as_of)
        store = DbAliasLedger(job.organization)

        def _progress(pct, msg):
            PreIngestJob.objects.filter(pk=job_id).update(
                progress_pct=int(pct), progress_message=str(msg)[:500])

        base_currency = (job.summary or {}).get('base_currency') or None
        # ── Model-ON (AI locator engaged) ──────────────────────────────────────────────────────────
        # Production pre-ingestion runs the Gemini locator (Vertex ADC) so concepts the deterministic
        # path cannot bind are LOCATED, re-read and verified — never a wrong number (doubt → HOLD).
        # Reversible: PREINGEST3_MODEL_ON=false → deterministic-only (the prior behaviour).
        # The client-side governor is set to the validated-safe cap (RPM=50 / burst=6) so a single
        # import's parallel workers stay under the shared-pool wall (0 self-429s measured, ~2.6 min);
        # max_workers overlaps the seconds-long locate waits. NOTE: this in-process governor covers
        # only THIS run under the current one-import-at-a-time lock — before multi-tenant concurrency,
        # promote it to the shared (Redis) governor at api.gemini_service.
        _model_on = os.environ.get('PREINGEST3_MODEL_ON', 'true').lower() in ('true', '1', 'yes')
        _mw = int(os.environ.get('PREINGEST3_MAX_WORKERS', '8'))
        if _model_on:
            rate_governor.configure(
                rpm=int(os.environ.get('PREINGEST3_RPM', '50')),
                tpm=int(os.environ.get('PREINGEST3_TPM', '150000')),
                burst=int(os.environ.get('PREINGEST3_RPM_BURST', '6')))
        result = pipeline.run(files, as_of=as_of, org=str(job.organization_id),
                              rate_card=rc, alias_store=store, store_dir=_golden_store_base(),
                              base_currency=base_currency, progress=_progress,
                              require_model=_model_on, max_workers=_mw)

        # DELIVERABLE = the consolidated Fund Master workbook — the full canonical sheet set
        # (MASTER_INPUTS, LP_REGISTER, CAPITAL_CALLS, PORTFOLIO_MASTER, VALUATIONS, QUOTED_UNQUOTED,
        # NAV_CALC, MOIC_TVPI_DPI, WATERFALL, SECTOR_ALLOCATION, EXITS, FEES, PORTFOLIO_KPI,
        # DASHBOARD_BRIDGE, RECONCILIATION), every tab always present (a not-extracted domain is
        # DISCLOSED, not dropped). The old download built the thin review-gate workbook (assemble.build) —
        # a different, smaller artifact — so NAV / capital-calls / waterfall / dashboard were absent from
        # the file even though the engine produced them. The product is the master workbook; build it here.
        # save_reproducible keeps the file byte-identical for the same CIR (the determinism guarantee).
        wb = master_workbook.build_master(result.cir, rate_card=rc,
                                          files=[label for label, _ in files])
        rel = preingest_output_path(job, job.output_name or 'TFAI.xlsx')
        abs_path = os.path.join(settings.MEDIA_ROOT, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        master_workbook.save_reproducible(wb, abs_path)

        payload = _serialize(result, as_of, rc)
        payload['as_of'] = as_of
        _carry_run_inputs(job.summary or {}, payload)
        held = payload['counts']['held_files'] + payload['counts']['read_errors']
        job.output_file = rel
        job.summary = payload
        job.progress_pct = 100
        job.progress_message = (
            f"Done — {payload['counts']['companies']} companies, "
            f"{payload['counts']['attributed']} attributed, {held} held for review")
        job.status = 'completed_with_errors' if held else 'completed'
        job.completed_at = timezone.now()
        job.save()
    except Exception as e:  # noqa: BLE001 — a failed run holds, never a wrong number
        logger.exception('[preingest3] job failed')
        job.status = 'failed'
        job.progress_message = str(e)[:500]
        job.error_log = (job.error_log or []) + [{'error': str(e)}]
        job.completed_at = timezone.now()
        job.save()


def _worker_name(job_id):
    return f'preingest3-{str(job_id)[:8]}'


def _worker_alive(job_id):
    """Is a run for this job actually executing RIGHT NOW in this process?

    _run_job runs in a daemon thread named by _worker_name; while it is alive the job is
    genuinely 'processing'. If the status is 'processing' but no such thread is alive, the
    run is ORPHANED — the process was restarted (dev autoreload, deploy, crash) mid-run and
    the daemon thread was killed before it could reach its completed/failed write. This is the
    exact liveness signal for the current single-process threading model: it needs no timeout
    heuristic and never mistakes a legitimately-long extraction for a dead one.

    NOTE (multi-process future): when runs move off in-process threads onto a broker (Celery /
    the shared governor), replace this with a broker liveness / heartbeat check — a thread in
    THIS process is no longer the whole truth once work can execute in a sibling worker.
    """
    name = _worker_name(job_id)
    return any(t.name == name and t.is_alive() for t in threading.enumerate())


def _kick(job_id):
    threading.Thread(target=_run_job, args=(str(job_id),), daemon=True,
                     name=_worker_name(job_id)).start()


def _get_job(request, job_id):
    """Org-checked fetch — a job from another tenant is a 404, never readable."""
    try:
        return PreIngestJob.objects.get(pk=job_id, organization=request.organization)
    except PreIngestJob.DoesNotExist:
        raise Http404


# ── endpoints ────────────────────────────────────────────────────────────────
@api_view(['POST'])
@permission_classes([IsGPAdmin])
@parser_classes([MultiPartParser, FormParser])
def upload(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)
    files = request.FILES.getlist('files')
    if not files:
        return Response({'detail': 'No files uploaded.'}, status=400)
    for f in files:
        if not f.name.lower().endswith(('.xlsx', '.xls')):
            return Response({'detail': f'Invalid file type: {f.name}. Only Excel is accepted.'},
                            status=400)
    try:
        rate_card = _parse_rate_card_field(request.data.get('rate_card'))
    except (ValueError, TypeError):
        return Response({'detail': 'rate_card must be valid JSON.'}, status=400)
    try:
        base_currency = _validate_base_currency(request.data.get('base_currency'))
    except ValueError as e:
        return Response({'detail': str(e)}, status=400)
    summary = {'engine': _ENGINE}
    if rate_card is not None:                      # U6: the card is a run input from creation
        summary['rate_card'] = rate_card
    if base_currency:                              # fund-base confirm: a disclosed run input from creation
        summary['base_currency'] = base_currency
    job = PreIngestJob.objects.create(
        organization=org, uploaded_by=request.user, total_files=len(files),
        output_name='TFAI.xlsx', status='pending', summary=summary)
    for f in files:
        PreIngestInputFile.objects.create(job=job, file=f, original_name=f.name)
    log_audit(request, 'create', 'preingest3', str(job.id),
              {'file_count': len(files), 'filenames': [f.name for f in files]})
    _kick(job.id)
    return Response({'job_id': str(job.id), 'file_count': len(files)},
                    status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsGPUser])
def job_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)
    jobs = PreIngestJob.objects.filter(organization=org, summary__engine=_ENGINE)[:30]
    return Response([{
        'job_id': str(j.id), 'status': j.status, 'progress_pct': j.progress_pct,
        'total_files': j.total_files, 'has_output': bool(j.output_file),
        'counts': (j.summary or {}).get('counts'),
        'created_at': j.created_at, 'completed_at': j.completed_at,
    } for j in jobs])


@api_view(['GET'])
@permission_classes([IsGPUser])
def job_detail(request, job_id):
    job = _get_job(request, job_id)
    files = [{'file_id': str(inf.id), 'name': inf.original_name}
             for inf in job.input_files.all()]
    return Response({
        'job_id': str(job.id), 'status': job.status, 'progress_pct': job.progress_pct,
        'progress_message': job.progress_message, 'total_files': job.total_files,
        'has_output': bool(job.output_file), 'input_files': files,
        'review': job.summary or {}, 'error_log': job.error_log,
        'created_at': job.created_at, 'completed_at': job.completed_at,
    })


@api_view(['POST'])
@permission_classes([IsGPAdmin])
def rerun(request, job_id):
    job = _get_job(request, job_id)
    if job.status == 'processing':
        # A re-run trigger must NEVER dead-end. Two very different situations wear the same
        # 'processing' status, and the old flat 409 collapsed them into one unrecoverable error:
        #   (1) LIVE  — a run really is executing (a cold model-on run takes many minutes, during
        #       which a second Confirm / Delete / rate-card / base-currency trigger can arrive).
        #       Starting a duplicate would double the Gemini spend and race the alias write-back,
        #       so we DON'T re-kick; we return 202 so the client simply attaches and polls the run
        #       already in flight — the outcome the user actually wanted.
        #   (2) ORPHANED — the status is stuck at 'processing' but no worker is alive (the process
        #       was restarted mid-run and the daemon thread died before writing completed/failed).
        #       The old code refused this forever. We reclaim it and start a fresh run below.
        if _worker_alive(job.id):
            return Response({'job_id': str(job.id), 'status': 'processing',
                             'detail': 'A run is already in progress — attach and watch it.'},
                            status=202)
        logger.warning('[preingest3] reclaiming orphaned processing job %s '
                       '(no live worker — process restarted mid-run)', job.id)
    # U6: a re-run may supply/replace the Rate Card (the 'hold → supply rate → re-run'
    # loop). An explicit empty value clears it back to INR-only; absent leaves it as-is.
    if 'rate_card' in request.data:
        try:
            payload = _parse_rate_card_field(request.data.get('rate_card'))
        except (ValueError, TypeError):
            return Response({'detail': 'rate_card must be valid JSON.'}, status=400)
        summary = job.summary or {}
        if payload is None:
            summary.pop('rate_card', None)
        else:
            summary['rate_card'] = payload
        job.summary = summary
        job.save(update_fields=['summary'])
    job.status = 'pending'
    job.progress_pct = 0
    job.save(update_fields=['status', 'progress_pct'])
    _kick(job.id)
    return Response({'job_id': str(job.id), 'status': 'pending'})


@api_view(['POST'])
@permission_classes([IsGPAdmin])
def resolve(request, job_id):
    """Confirm an entity alias — the human write-back. Privileged: writes durable,
    org-wide data that steers every future run for this tenant."""
    job = _get_job(request, job_id)
    identifiers = request.data.get('identifiers')
    entity_id = request.data.get('entity_id')
    if not identifiers or not entity_id:
        return Response({'detail': 'identifiers and entity_id are required.'}, status=400)
    store = DbAliasLedger(job.organization, confirmed_by=request.user)
    store.learn(list(identifiers), str(entity_id), provenance='human')
    log_audit(request, 'update', 'preingest3_alias', str(job.id),
              {'entity_id': entity_id, 'identifiers': identifiers})
    return Response({'ok': True, 'entity_id': entity_id,
                     'detail': 'Alias confirmed. Re-run to apply.'})


@api_view(['POST'])
@permission_classes([IsGPAdmin])
@parser_classes([JSONParser, MultiPartParser, FormParser])
def ratecard(request, job_id):
    """U6 Phase 5 — accept a user-supplied Rate Card through the VALIDATED intake gate and store it as
    this job's run input (the 'hold → supply rate → re-run' loop). THREE CO-EQUAL inputs, exactly one per
    call: `manual_rates` (a list of typed {currency, rate, date, source}), `schedule_rows` (a rate-schedule
    grid as JSON), or a multipart `schedule_file` (an uploaded rate-schedule workbook, parsed server-side).
    All funnel through the SAME fail-closed VALIDITY gate — manual entry is first-class, not a fallback. A
    card that fails validity is REFUSED 400 with the reason (never a silent or coerced rate). A VALID rate
    the run does not need is accepted onto the card and returned under `noop_currencies` (so a standing/org
    card, or a pasted full card, is not rejected for covering more than this run needs). The client then
    calls /run/ to apply the stored card. Privileged: the card is an attributed, disclosed run input."""
    job = _get_job(request, job_id)
    from .preingest3.ratecard_intake import IntakeError
    as_of = (job.summary or {}).get('as_of') or timezone.now().date().isoformat()
    manual = request.data.get('manual_rates')
    rows = request.data.get('schedule_rows')
    sched_file = request.FILES.get('schedule_file')
    if sched_file is not None:
        if not sched_file.name.lower().endswith(('.xlsx', '.xls')):
            return Response({'detail': 'Rate schedule must be an Excel file.'}, status=400)
        rows = _rows_from_schedule_upload(sched_file)
        if rows is None:
            return Response({'detail': 'No rate-schedule header (a currency column and a rate column) '
                                       'found in the uploaded file.'}, status=400)
    if bool(manual) == bool(rows):
        return Response({'detail': 'Supply exactly one of manual_rates, schedule_rows, or schedule_file.'},
                        status=400)
    # the run's rate-actionable set (from the last run's report) drives the no-op / still-uncovered
    # disclosure; None when the job has not reported yet (no report to disclose against).
    report = (job.summary or {}).get('currency_report')
    uncovered = None if report is None else {u['currency'] for u in (report.get('uncovered') or [])}
    # Rate-entry sanity band (#2): hold a suspected decimal-shift typo for review rather than converting a
    # ~10× wrong rate (which slips under the downstream anchor-plausibility backstop). Suspect rates drop
    # off the card so the currency holds via the existing uniform-currency engine; first uploads convert
    # with a soft 'unverified' note. Schedule/upload paths (rows) are not manually typed → no band.
    band_suspect, band_unverified = [], []
    if manual:
        manual, band_suspect, band_unverified = _band_partition(job, list(manual))
    try:
        if manual or rows:
            card, noop, _still_this = _accept_card(manual=manual, rows=rows, as_of=as_of, uncovered=uncovered)
        else:
            card, noop = None, []                     # every supplied rate this submission is held for review
    except IntakeError as e:
        return Response({'detail': f'Rate card refused: {e}'}, status=400)
    summary = job.summary or {}
    # MERGE, never overwrite. The card is CUMULATIVE across the 'hold → supply rate → re-run' loop:
    # a batch with two foreign currencies (e.g. SGD and MYR) is covered one currency per submission,
    # so overwriting the stored card dropped the currency confirmed a moment ago and the run re-reported
    # it as uncovered — an endless SGD↔MYR ping-pong that could NEVER cover both. Union the previously
    # stored rows with this submission's, keyed by currency; a re-supplied currency updates its row.
    prior_rows = (summary.get('rate_card') or {}).get('rates') or []
    merged = {r['currency']: r for r in prior_rows}
    accepted_rows = card.disclosure_rows() if card is not None else []
    for r in accepted_rows:
        merged[r['currency']] = r
    merged_rows = [merged[k] for k in sorted(merged)]
    summary['rate_card'] = {'as_of': (card.as_of if card is not None else as_of), 'rates': merged_rows}
    # Persist the band review, cumulative across the loop and keyed by currency, so the review-gate can show
    # 'confirm or re-enter' (suspect) and the soft 'no prior to check' note (unverified). A currency now on
    # the card (corrected or confirmed) clears from the suspect list.
    review = summary.get('rate_card_review') or {}
    suspect = {s['currency']: s for s in review.get('suspect', [])}
    unverified = {u['currency']: u for u in review.get('unverified', [])}
    suspect.update({s['currency']: s for s in band_suspect})
    unverified.update({u['currency']: u for u in band_unverified})
    for ccy in {r['currency'] for r in accepted_rows}:
        suspect.pop(ccy, None)
    summary['rate_card_review'] = {'suspect': [suspect[k] for k in sorted(suspect)],
                                   'unverified': [unverified[k] for k in sorted(unverified)]}
    job.summary = summary
    job.save(update_fields=['summary'])
    # still-uncovered is reported against the MERGED coverage, not just this one submission.
    still = sorted((uncovered or set()) - set(merged))
    log_audit(request, 'update', 'preingest3_ratecard', str(job.id),
              {'card_id': (card.card_id if card else None), 'currencies': sorted(merged),
               'held_for_review': [s['currency'] for s in band_suspect]})
    return Response({'ok': True, 'card_id': (card.card_id if card is not None else None),
                     'rates': merged_rows, 'noop_currencies': noop, 'still_uncovered': still,
                     'suspect_rates': summary['rate_card_review']['suspect'],
                     'unverified_rates': summary['rate_card_review']['unverified'],
                     'detail': ('Rate card accepted. Re-run to apply.' if card is not None else
                                'Supplied rate(s) held for review — confirm or re-enter.')})


@api_view(['POST'])
@permission_classes([IsGPAdmin])
def base_currency(request, job_id):
    """Confirm the batch's base reporting currency (the fund-level confirm-prompt payoff). Stored as a
    DISCLOSED run input in job.summary; the client re-runs (/run/) to apply — the same write-back loop as
    the rate card. Safety is carried by the engine, not this endpoint: a figure with its OWN foreign
    token/domicile still HOLDS under the base (conflict guard), and EVERY figure resolved via the base is
    surfaced under currency_report.base_currency_applied for per-batch review, so a newly-seen foreign
    entrant with no marker can never be swept into the base unseen. Pass an empty value to clear it."""
    job = _get_job(request, job_id)
    try:
        bc = _validate_base_currency(request.data.get('base_currency'))
    except ValueError as e:
        return Response({'detail': str(e)}, status=400)
    summary = job.summary or {}
    if bc:
        summary['base_currency'] = bc
    else:
        summary.pop('base_currency', None)
    job.summary = summary
    job.save(update_fields=['summary'])
    log_audit(request, 'update', 'preingest3_base_currency', str(job.id), {'base_currency': bc})
    return Response({'ok': True, 'base_currency': bc,
                    'detail': ('Base currency set. Re-run to apply.' if bc
                               else 'Base currency cleared. Re-run to apply.')})


@api_view(['DELETE'])
@permission_classes([IsGPAdmin])
def delete_file(request, file_id):
    org = request.organization
    try:
        inf = PreIngestInputFile.objects.get(pk=file_id, job__organization=org)
    except PreIngestInputFile.DoesNotExist:
        raise Http404
    job = inf.job
    name = inf.original_name
    try:
        inf.file.delete(save=False)
    except Exception:  # noqa: BLE001 — disk miss must not block record removal
        pass
    inf.delete()
    PreIngestJob.objects.filter(pk=job.id).update(total_files=job.input_files.count())
    log_audit(request, 'delete', 'preingest3_file', str(job.id), {'filename': name})
    return Response({'ok': True, 'deleted': name})


@api_view(['GET'])
@permission_classes([IsGPUser])
def download(request, job_id):
    job = _get_job(request, job_id)
    if not job.output_file:
        return Response({'detail': 'Output not ready.'}, status=404)
    abs_path = os.path.join(settings.MEDIA_ROOT, job.output_file)
    if not os.path.exists(abs_path):
        return Response({'detail': 'Output file missing on disk.'}, status=404)
    return FileResponse(
        open(abs_path, 'rb'), as_attachment=True, filename=job.output_name or 'TFAI.xlsx',
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
