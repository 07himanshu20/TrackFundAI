"""
API for the pre-ingestion consolidation layer.

    POST  /api/dataimport/preingest/            upload N files -> start a job
    GET   /api/dataimport/preingest/            list jobs
    GET   /api/dataimport/preingest/<id>/       job status + full report
    GET   /api/dataimport/preingest/<id>/download/   download the TFAI.xlsx

The heavy work runs in a background thread (mirrors the existing import flow).
Each Gemini call is small (one batch of sheet fingerprints), so the layer is
safe to run per-organization without holding a global lock.
"""
import logging
import os
import threading

from django.conf import settings
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view, parser_classes, permission_classes,
)
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.permissions import IsGPAdmin, IsGPUser
from .models import PreIngestInputFile, PreIngestJob, preingest_output_path

logger = logging.getLogger(__name__)


def _run_consolidation(job_id):
    """Background worker: fingerprint -> map -> move -> write TFAI.xlsx."""
    from .preingest.consolidator import consolidate

    job = PreIngestJob.objects.get(pk=job_id)
    try:
        job.status = 'processing'
        job.progress_message = 'Starting consolidation'
        job.save(update_fields=['status', 'progress_message'])

        files = []
        for inf in job.input_files.all():
            files.append((os.path.splitext(inf.original_name)[0], inf.file.path))

        def _progress(pct, msg):
            PreIngestJob.objects.filter(pk=job_id).update(
                progress_pct=pct, progress_message=msg[:500])

        wb, summary = consolidate(files, progress=_progress)

        rel = preingest_output_path(job, job.output_name or 'TFAI.xlsx')
        abs_path = os.path.join(settings.MEDIA_ROOT, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        wb.save(abs_path)

        errored = any(a.get('status') not in ('ok', None) for a in summary.get('audit', []))
        populated = sum(1 for c in summary.get('sheet_row_counts', {}).values() if c)
        job.output_file = rel
        job.summary = summary
        job.progress_pct = 100
        job.progress_message = (
            f"Done — {summary.get('company_rows', 0)} portfolio companies, "
            f"{populated} of {len(summary.get('sheet_row_counts', {}))} fund sheets "
            f"populated across {summary.get('files', 0)} files")
        job.status = 'completed_with_errors' if errored else 'completed'
        job.completed_at = timezone.now()
        job.save()
    except Exception as e:  # noqa: BLE001
        logger.exception('[preingest] job failed')
        job.status = 'failed'
        job.progress_message = str(e)[:500]
        job.error_log = (job.error_log or []) + [{'error': str(e)}]
        job.completed_at = timezone.now()
        job.save()


@api_view(['POST'])
@permission_classes([IsGPAdmin])
@parser_classes([MultiPartParser, FormParser])
def preingest_upload(request):
    """Upload many raw client files and start one consolidation job."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    files = request.FILES.getlist('files')
    if not files:
        return Response({'detail': 'No files uploaded.'}, status=400)
    for f in files:
        if not f.name.lower().endswith(('.xlsx', '.xls')):
            return Response(
                {'detail': f'Invalid file type: {f.name}. Only Excel files are accepted.'},
                status=400)

    job = PreIngestJob.objects.create(
        organization=org, uploaded_by=request.user, total_files=len(files),
        output_name='TFAI.xlsx', status='pending',
    )
    for f in files:
        PreIngestInputFile.objects.create(job=job, file=f, original_name=f.name)

    log_audit(request, 'create', 'preingest', str(job.id),
              {'file_count': len(files), 'filenames': [f.name for f in files]})

    threading.Thread(target=_run_consolidation, args=(str(job.id),),
                     daemon=True, name=f'preingest-{str(job.id)[:8]}').start()

    return Response({'job_id': str(job.id), 'file_count': len(files)},
                    status=status.HTTP_201_CREATED)


def _job_payload(job):
    return {
        'job_id': str(job.id),
        'status': job.status,
        'progress_pct': job.progress_pct,
        'progress_message': job.progress_message,
        'total_files': job.total_files,
        'output_name': job.output_name,
        'has_output': bool(job.output_file),
        'summary': job.summary,
        'error_log': job.error_log,
        'created_at': job.created_at,
        'completed_at': job.completed_at,
    }


@api_view(['GET'])
@permission_classes([IsGPUser])
def preingest_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)
    jobs = PreIngestJob.objects.filter(organization=org)[:20]
    return Response([{
        'job_id': str(j.id), 'status': j.status, 'progress_pct': j.progress_pct,
        'total_files': j.total_files, 'output_name': j.output_name,
        'has_output': bool(j.output_file),
        'total_records': (j.summary or {}).get('total_records'),
        'review_items': (j.summary or {}).get('review_items'),
        'created_at': j.created_at, 'completed_at': j.completed_at,
    } for j in jobs])


@api_view(['GET'])
@permission_classes([IsGPUser])
def preingest_detail(request, job_id):
    org = request.organization
    try:
        job = PreIngestJob.objects.get(pk=job_id, organization=org)
    except PreIngestJob.DoesNotExist:
        raise Http404
    return Response(_job_payload(job))


@api_view(['GET'])
@permission_classes([IsGPUser])
def preingest_download(request, job_id):
    org = request.organization
    try:
        job = PreIngestJob.objects.get(pk=job_id, organization=org)
    except PreIngestJob.DoesNotExist:
        raise Http404
    if not job.output_file:
        return Response({'detail': 'Output not ready.'}, status=404)
    abs_path = os.path.join(settings.MEDIA_ROOT, job.output_file)
    if not os.path.exists(abs_path):
        return Response({'detail': 'Output file missing on disk.'}, status=404)
    return FileResponse(
        open(abs_path, 'rb'), as_attachment=True,
        filename=job.output_name or 'TFAI.xlsx',
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
