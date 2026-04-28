"""
Data Import views — file upload, SSE streaming, job status.
"""
import json
import logging
import time

from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.permissions import IsGPAdmin, IsGPUser
from .models import ImportJob, ImportFile
from .serializers import ImportJobSerializer, ImportJobStatusSerializer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Upload endpoint — accepts 1+ Excel files, creates ImportJob
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsGPAdmin])
@parser_classes([MultiPartParser, FormParser])
def upload_fund_files(request):
    """Accept one or more .xlsx files and create an ImportJob for processing."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    files = request.FILES.getlist('files')
    if not files:
        return Response({'detail': 'No files uploaded.'}, status=400)

    # Validate all files are Excel
    for f in files:
        if not f.name.lower().endswith(('.xlsx', '.xls')):
            return Response(
                {'detail': f'Invalid file type: {f.name}. Only .xlsx files are accepted.'},
                status=400,
            )

    # Create job
    job = ImportJob.objects.create(
        organization=org,
        uploaded_by=request.user,
        total_files=len(files),
    )

    # Save each file
    for f in files:
        ImportFile.objects.create(
            job=job,
            file=f,
            original_filename=f.name,
            file_size=f.size,
        )

    log_audit(request, 'create', 'dataimport', str(job.id), {
        'file_count': len(files),
        'filenames': [f.name for f in files],
    })

    return Response({
        'job_id': str(job.id),
        'file_count': len(files),
    }, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Job list / detail
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsGPUser])
def job_list(request):
    """List import jobs for this organization."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)
    jobs = ImportJob.objects.filter(organization=org)[:20]
    return Response(ImportJobSerializer(jobs, many=True).data)


@api_view(['GET'])
@permission_classes([IsGPUser])
def job_detail(request, job_id):
    """Get details of a specific import job."""
    org = request.organization
    try:
        job = ImportJob.objects.get(pk=job_id, organization=org)
    except ImportJob.DoesNotExist:
        return Response({'detail': 'Job not found.'}, status=404)
    return Response(ImportJobSerializer(job).data)


# ---------------------------------------------------------------------------
# Job status (polling fallback)
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsGPUser])
def job_status(request, job_id):
    """Lightweight status endpoint for polling fallback."""
    org = request.organization
    try:
        job = ImportJob.objects.get(pk=job_id, organization=org)
    except ImportJob.DoesNotExist:
        return Response({'detail': 'Job not found.'}, status=404)
    return Response(ImportJobStatusSerializer(job).data)


# ---------------------------------------------------------------------------
# SSE streaming endpoint — processes import and streams progress
# ---------------------------------------------------------------------------

def _validate_jwt_from_query(request):
    """Validate JWT token from query string (EventSource can't set headers)."""
    from rest_framework_simplejwt.tokens import AccessToken
    from rest_framework_simplejwt.exceptions import TokenError
    from accounts.models import User

    token_str = request.GET.get('token', '')
    if not token_str:
        return None

    try:
        token = AccessToken(token_str)
        user_id = token.get('user_id')
        return User.objects.get(pk=user_id)
    except (TokenError, User.DoesNotExist):
        return None


def import_stream(request, job_id):
    """
    SSE endpoint: processes each pending file in the job and streams
    progress events in real time.

    Uses StreamingHttpResponse (works with WSGI, no Channels needed).
    JWT auth is via ?token= query param since EventSource can't set headers.
    """
    user = _validate_jwt_from_query(request)
    if not user or not hasattr(user, 'organization') or not user.organization:
        return StreamingHttpResponse(
            _sse_error('Authentication failed'),
            content_type='text/event-stream',
            status=401,
        )

    try:
        job = ImportJob.objects.get(pk=job_id, organization=user.organization)
    except ImportJob.DoesNotExist:
        return StreamingHttpResponse(
            _sse_error('Job not found'),
            content_type='text/event-stream',
            status=404,
        )

    response = StreamingHttpResponse(
        _import_event_generator(job, user),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    response['Access-Control-Allow-Origin'] = '*'
    return response


def _sse_event(data):
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


def _sse_error(message):
    """Yield a single error SSE event."""
    yield _sse_event({'event': 'error', 'error': message})


def _import_event_generator(job, user):
    """
    Generator that processes each file and yields SSE progress events.
    This runs synchronously — one WSGI worker is occupied per active import.
    """
    from .import_service import FundImportService

    pending_files = list(job.files.filter(status='pending'))
    if not pending_files:
        yield _sse_event({'event': 'job_complete', 'msg': 'No pending files to process.'})
        return

    job.status = 'processing'
    job.save(update_fields=['status'])

    total_files = len(pending_files)
    all_results = {}
    all_errors = []
    completed_count = 0

    for file_idx, import_file in enumerate(pending_files):
        file_base_pct = int((file_idx / total_files) * 100)
        file_span_pct = int(100 / total_files)

        yield _sse_event({
            'pct': file_base_pct,
            'msg': f'Starting {import_file.original_filename}...',
            'file': import_file.original_filename,
            'file_index': file_idx,
            'total_files': total_files,
        })

        def progress_callback(stage_pct, message):
            overall_pct = file_base_pct + int(stage_pct * file_span_pct / 100)
            overall_pct = min(overall_pct, 99)
            job.progress_pct = overall_pct
            job.progress_message = message
            job.save(update_fields=['progress_pct', 'progress_message'])

        # Yield wrapper — we need to yield from inside the callback
        progress_events = []

        def progress_cb(stage_pct, message):
            overall_pct = file_base_pct + int(stage_pct * file_span_pct / 100)
            overall_pct = min(overall_pct, 99)
            job.progress_pct = overall_pct
            job.progress_message = message
            job.save(update_fields=['progress_pct', 'progress_message'])
            progress_events.append({
                'pct': overall_pct,
                'msg': message,
                'file': import_file.original_filename,
                'file_index': file_idx,
                'total_files': total_files,
            })

        service = FundImportService(
            organization=user.organization,
            user=user,
        )

        try:
            result = service.import_file(import_file, progress_cb)

            # Yield all accumulated progress events
            for evt in progress_events:
                yield _sse_event(evt)
            progress_events.clear()

            import_file.status = 'completed'
            import_file.completed_at = timezone.now()
            import_file.save(update_fields=['status', 'completed_at'])

            completed_count += 1
            job.completed_files = completed_count
            job.save(update_fields=['completed_files'])

            all_results[import_file.original_filename] = result

            yield _sse_event({
                'event': 'file_complete',
                'file': import_file.original_filename,
                'file_index': file_idx,
                'result': result,
            })

        except Exception as e:
            logger.exception(f'Import failed for {import_file.original_filename}')

            # Yield any progress events that were accumulated
            for evt in progress_events:
                yield _sse_event(evt)
            progress_events.clear()

            import_file.status = 'failed'
            import_file.error_detail = str(e)
            import_file.save(update_fields=['status', 'error_detail'])

            all_errors.append({
                'file': import_file.original_filename,
                'error': str(e),
            })

            yield _sse_event({
                'event': 'file_error',
                'file': import_file.original_filename,
                'error': str(e),
            })

        # Heartbeat between files
        yield ": heartbeat\n\n"

    # Finalize job
    job.progress_pct = 100
    job.progress_message = 'Import complete'
    job.result_summary = all_results
    job.error_log = all_errors
    job.completed_at = timezone.now()
    job.status = 'completed' if not all_errors else 'completed_with_errors'
    job.save()

    yield _sse_event({
        'event': 'job_complete',
        'pct': 100,
        'results': all_results,
        'errors': all_errors,
    })
