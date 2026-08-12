"""
API for the preingest2 (document-faithful) consolidation layer — testing UI.

    POST  /api/dataimport/preingest2/            upload N files -> start job
    GET   /api/dataimport/preingest2/<id>/       status + report
    GET   /api/dataimport/preingest2/<id>/download/   download the workbook
"""
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
from .models import PreIngestInputFile, PreIngestJob, preingest_output_path

logger = logging.getLogger(__name__)


def _run(job_id):
    from .preingest2.pipeline import consolidate

    job = PreIngestJob.objects.get(pk=job_id)
    try:
        job.status = 'processing'
        job.progress_message = 'Starting (preingest2)'
        job.save(update_fields=['status', 'progress_message'])

        files = [(os.path.splitext(inf.original_name)[0], inf.file.path)
                 for inf in job.input_files.all()]

        def _p(pct, msg):
            PreIngestJob.objects.filter(pk=job_id).update(
                progress_pct=pct, progress_message=msg[:500])

        wb, summary = consolidate(files, progress=_p)
        rel = preingest_output_path(job, job.output_name or 'TFAI.xlsx')
        abs_path = os.path.join(settings.MEDIA_ROOT, rel)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        wb.save(abs_path)

        job.output_file = rel
        job.summary = summary
        job.progress_pct = 100
        populated = sum(1 for c in summary['sheet_row_counts'].values() if c)
        job.progress_message = (f"Done — {summary['company_rows']} companies, "
                                f"{populated}/{len(summary['sheet_row_counts'])} sheets, "
                                f"{summary['cache']['frozen_records']} cached")
        job.status = 'completed_with_errors' if summary.get('blocked') else 'completed'
        job.completed_at = timezone.now()
        job.save()
    except Exception as e:  # noqa: BLE001
        logger.exception('[preingest2] job failed')
        job.status = 'failed'
        job.progress_message = str(e)[:500]
        job.completed_at = timezone.now()
        job.save()


@api_view(['POST'])
@permission_classes([IsGPAdmin])
@parser_classes([MultiPartParser, FormParser])
def preingest2_upload(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)
    files = request.FILES.getlist('files')
    if not files:
        return Response({'detail': 'No files uploaded.'}, status=400)
    for f in files:
        if not f.name.lower().endswith(('.xlsx', '.xls')):
            return Response({'detail': f'Invalid file type: {f.name}.'}, status=400)
    job = PreIngestJob.objects.create(
        organization=org, uploaded_by=request.user, total_files=len(files),
        output_name='TFAI.xlsx', status='pending')
    for f in files:
        PreIngestInputFile.objects.create(job=job, file=f, original_name=f.name)
    log_audit(request, 'create', 'preingest2', str(job.id),
              {'file_count': len(files)})
    threading.Thread(target=_run, args=(str(job.id),), daemon=True,
                     name=f'preingest2-{str(job.id)[:8]}').start()
    return Response({'job_id': str(job.id), 'file_count': len(files)},
                    status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsGPUser])
def preingest2_detail(request, job_id):
    try:
        job = PreIngestJob.objects.get(pk=job_id, organization=request.organization)
    except PreIngestJob.DoesNotExist:
        raise Http404
    return Response({
        'job_id': str(job.id), 'status': job.status, 'progress_pct': job.progress_pct,
        'progress_message': job.progress_message, 'total_files': job.total_files,
        'has_output': bool(job.output_file), 'summary': job.summary,
        'created_at': job.created_at, 'completed_at': job.completed_at,
    })


@api_view(['GET'])
@permission_classes([IsGPUser])
def preingest2_download(request, job_id):
    try:
        job = PreIngestJob.objects.get(pk=job_id, organization=request.organization)
    except PreIngestJob.DoesNotExist:
        raise Http404
    if not job.output_file:
        return Response({'detail': 'Output not ready.'}, status=404)
    abs_path = os.path.join(settings.MEDIA_ROOT, job.output_file)
    if not os.path.exists(abs_path):
        return Response({'detail': 'Output missing.'}, status=404)
    return FileResponse(open(abs_path, 'rb'), as_attachment=True,
                        filename=job.output_name or 'TFAI.xlsx',
                        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
