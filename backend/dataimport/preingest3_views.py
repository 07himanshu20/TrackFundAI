"""
API for the preingest3 CIR extraction engine + the review-gate write-back loop.

    POST   /api/dataimport/preingest3/                  upload N files → start a run (async)
    GET    /api/dataimport/preingest3/list/             list preingest3 jobs
    GET    /api/dataimport/preingest3/<id>/             job status + full review payload
    POST   /api/dataimport/preingest3/<id>/run/         re-run this job's files (after a resolve)
    POST   /api/dataimport/preingest3/<id>/resolve/     confirm an entity alias (human write-back)
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
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.permissions import IsGPAdmin, IsGPUser
from .models import PreIngestInputFile, PreIngestJob, PreIngestAlias, preingest_output_path
from .preingest3_store import DbAliasLedger

logger = logging.getLogger(__name__)

_ENGINE = 'preingest3'
_MIS_DOMAINS = ('portfolio_investments', 'company', 'mis')
_CR = 'value_cr'


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


def _run_job(job_id):
    from .preingest3 import pipeline, assemble

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

        result = pipeline.run(files, as_of=as_of, org=str(job.organization_id),
                              rate_card=rc, alias_store=store, progress=_progress)

        wb = assemble.build(result.cir, rate_card=rc)
        rel = preingest_output_path(job, job.output_name or 'TFAI.xlsx')
        abs_path = os.path.join(settings.MEDIA_ROOT, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        wb.save(abs_path)

        payload = _serialize(result, as_of, rc)
        payload['as_of'] = as_of
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


def _kick(job_id):
    threading.Thread(target=_run_job, args=(str(job_id),), daemon=True,
                     name=f'preingest3-{str(job_id)[:8]}').start()


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
    summary = {'engine': _ENGINE}
    if rate_card is not None:                      # U6: the card is a run input from creation
        summary['rate_card'] = rate_card
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
        return Response({'detail': 'Job already running.'}, status=409)
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
