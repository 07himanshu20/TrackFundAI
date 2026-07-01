"""
Data Import views — file upload, SSE streaming, job status.
"""
import json
import logging
import os
import queue
import threading
import time

from django.conf import settings as _dj_settings
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


# H1 — Concurrent-import sentinel. The Phase 3 orchestrator creates this
# file on import start and deletes it on completion (try/finally). If a
# second import is requested while it exists, we refuse with HTTP 409 so
# the user doesn't unknowingly queue parallel runs that can step on each
# other (Django dev server / single-worker prod / shared cache).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_IMPORT_SENTINEL = os.path.join(os.path.dirname(_PROJECT_ROOT), '.import_active')
_SENTINEL_MAX_AGE_S = int(os.environ.get('IMPORT_SENTINEL_MAX_AGE_S', '3600'))


def _import_in_flight() -> tuple[bool, dict]:
    """Return (in_flight, info). Treats a stale sentinel (> _SENTINEL_MAX_AGE_S)
    as cleared — a crashed worker shouldn't lock out future imports forever."""
    if not os.path.exists(_IMPORT_SENTINEL):
        return False, {}
    try:
        age = time.time() - os.path.getmtime(_IMPORT_SENTINEL)
        if age > _SENTINEL_MAX_AGE_S:
            try:
                os.remove(_IMPORT_SENTINEL)
            except Exception:
                pass
            return False, {'stale_cleared': True, 'age_s': round(age, 1)}
        with open(_IMPORT_SENTINEL) as f:
            body = f.read().strip().splitlines()
        return True, {
            'age_s': round(age, 1),
            'import_file_id': body[0] if body else None,
            'started_at': body[1] if len(body) > 1 else None,
        }
    except Exception:
        return True, {}

logger = logging.getLogger(__name__)

# Startup banner — emitted once at import time so the active extractor path
# and Gemini model are visible in every server log.
import os as _os_boot
_boot_phase6 = _os_boot.environ.get('USE_PHASE6', 'false').lower() in ('true', '1', 'yes')
_boot_phase3 = _os_boot.environ.get('USE_PHASE3', 'true').lower() in ('true', '1', 'yes')
_boot_model = _os_boot.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
_boot_vertex = _os_boot.environ.get('GOOGLE_GENAI_USE_VERTEXAI', 'False').lower() in ('true', '1', 'yes')
_boot_backend = 'Vertex AI (ADC)' if _boot_vertex else 'AI Studio (api_key)'
if _boot_phase6:
    _boot_extractor_desc = 'Phase 6 (single-call semantic classify + deterministic Python rows)'
elif _boot_phase3:
    _boot_extractor_desc = 'Phase 3 (Flavor A + B parallel layers)'
else:
    _boot_extractor_desc = 'single-call fallback'
logger.info(
    f'[BOOT] dataimport ready — extractor={_boot_extractor_desc}, '
    f'model={_boot_model}, backend={_boot_backend}'
)
del _os_boot, _boot_phase6, _boot_phase3, _boot_model, _boot_vertex, _boot_backend, _boot_extractor_desc


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

    in_flight, info = _import_in_flight()
    if in_flight:
        return Response(
            {
                'detail': 'Another import is already running. Wait for it to finish '
                          'before starting a new one.',
                'in_flight': info,
            },
            status=status.HTTP_409_CONFLICT,
        )

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


def _pct_to_phase(stage_pct):
    """Map a stage percentage to a named import phase for the frontend step indicator."""
    if stage_pct < 5:
        return 'file_upload'
    if stage_pct < 15:
        return 'sheet_scan'
    if stage_pct < 26:
        return 'ai_mapping'
    if stage_pct < 100:
        return 'data_import'
    return 'complete'


def _import_event_generator(job, user):
    """
    Generator that processes each file and yields SSE progress events in real time.

    Architecture: each file import runs in a background thread.  The thread
    calls progress_cb which puts events onto a queue.Queue.  The generator
    (main thread) reads from the queue with a short timeout so it can:
      - yield events immediately as the import thread produces them
      - yield SSE keep-alive comments on timeout so the connection never stalls
    This means the browser sees live decimal progress updates throughout the
    entire import — never a 0% freeze.
    """
    import django.db
    import os as _os
    # Phase 6 (semantic classify + deterministic Python rows) — takes priority
    # when USE_PHASE6=true. Otherwise Phase 3 (Flavor A+B parallel layers) —
    # DEFAULT-ON. Set USE_PHASE3=false to fall back to the single-call extractor
    # (emergency only).
    _USE_PHASE6 = _os.environ.get('USE_PHASE6', 'false').lower() in ('true', '1', 'yes')
    _USE_PHASE3 = _os.environ.get('USE_PHASE3', 'true').lower() in ('true', '1', 'yes')

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

        # Announce file start immediately (this is yielded before the thread starts)
        yield _sse_event({
            'pct': file_base_pct + 1,
            'msg': f'Starting {import_file.original_filename}...',
            'file': import_file.original_filename,
            'file_index': file_idx,
            'total_files': total_files,
            'phase': 'file_upload',
        })

        # Queue through which the import thread sends progress events
        event_queue = queue.Queue()

        # Capture loop vars for use in closures
        _fi = file_idx
        _tt = total_files
        _fn = import_file.original_filename
        _fb = file_base_pct
        _fs = file_span_pct

        def progress_cb(stage_pct, message,
                        _q=event_queue, _fi=_fi, _tt=_tt, _fn=_fn,
                        _fb=_fb, _fs=_fs):
            overall_pct = min(_fb + int(stage_pct * _fs / 100), 99)
            _q.put({
                'pct': overall_pct,
                'msg': message,
                'file': _fn,
                'file_index': _fi,
                'total_files': _tt,
                'phase': _pct_to_phase(stage_pct),
            })

        result_holder = [None]
        error_holder = [None]

        def _run_import(_rf=import_file, _pcb=progress_cb,
                        _q=event_queue,
                        _rh=result_holder, _eh=error_holder,
                        _phase6=_USE_PHASE6, _phase3=_USE_PHASE3):
            try:
                django.db.close_old_connections()
                if _phase6:
                    from .phase6_extractor import run_phase6_import
                    logger.info(f'Phase 6 (semantic + Python rows) for {_rf.original_filename}')
                    _rh[0] = run_phase6_import(_rf, _pcb)
                elif _phase3:
                    from .phase3_layers.orchestrator import run_phase3_import
                    logger.info(f'Phase 3 (Flavor A+B) for {_rf.original_filename}')
                    _rh[0] = run_phase3_import(_rf, _pcb)
                else:
                    from .single_call_extractor import run_phase2_import
                    logger.info(f'Single-call fallback for {_rf.original_filename}')
                    _rh[0] = run_phase2_import(_rf, _pcb)
            except Exception as exc:
                logger.exception(f'Import failed for {_rf.original_filename}')
                _eh[0] = exc
            finally:
                _q.put(None)
                django.db.close_old_connections()

        thread = threading.Thread(target=_run_import, daemon=True, name=f'import-{file_idx}')
        thread.start()

        # ── Stream progress events as they arrive from the import thread ──────
        heartbeat_tick = 0
        while True:
            try:
                evt = event_queue.get(timeout=2)   # 2-second poll
            except queue.Empty:
                # Nothing from import thread yet — send a keep-alive comment
                # so the browser SSE connection doesn't time out or reconnect
                heartbeat_tick += 1
                yield ": heartbeat\n\n"
                continue

            if evt is None:
                break   # Sentinel: import thread finished

            # NOTE: Do NOT write to DB here. The import thread holds
            # a @transaction.atomic write lock on SQLite for the entire
            # import duration. Any concurrent job.save() from this thread
            # causes "database is locked" which kills the import thread.
            # Progress is delivered live via SSE — DB is updated only at end.

            yield _sse_event(evt)

        thread.join(timeout=600)   # Safety: max 10 min per file

        result = result_holder[0] or {}
        result_failed = isinstance(result, dict) and result.get('status') == 'failed'

        if error_holder[0] or result_failed:
            if error_holder[0]:
                err_msg = str(error_holder[0])
            else:
                err_msg = (result.get('detail') or result.get('error')
                           or 'Import failed (no detail).')

            import_file.status = 'failed'
            import_file.error_detail = err_msg
            import_file.save(update_fields=['status', 'error_detail'])

            all_errors.append({
                'file': import_file.original_filename,
                'error': err_msg,
            })

            yield _sse_event({
                'event': 'file_error',
                'file': import_file.original_filename,
                'file_index': file_idx,
                'total_files': total_files,
                'error': err_msg,
            })
        else:
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

    # ── Finalise job ──────────────────────────────────────────────────────────
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

    # Post-import: invalidate Redis cache for all affected funds
    try:
        from config.cache_utils import invalidate_fund_cache
        invalidated_funds = set()
        for pf in pending_files:
            if pf.status == 'completed' and pf.fund_id:
                fund_id_str = str(pf.fund_id)
                if fund_id_str not in invalidated_funds:
                    invalidate_fund_cache(user.organization.id, pf.fund_id)
                    invalidated_funds.add(fund_id_str)
        if not invalidated_funds:
            # No specific fund — clear entire org cache
            invalidate_fund_cache(user.organization.id)
    except Exception as e:
        logger.warning(f'Post-import cache invalidation error: {e}')

    # Post-import: trigger async NAV/Carry/Fee/RiskScore recomputation
    try:
        from dataimport.tasks import post_import_recalculate
        for pf in pending_files:
            if pf.status == 'completed':
                post_import_recalculate.delay(str(pf.id))
    except Exception:
        pass  # Non-critical — recalculation can be triggered manually

    # Post-import: notify stakeholders of manual upload completion
    try:
        from accounts.models import Notification
        from accounts.models import User as _User
        org = user.organization
        completed_files = [pf for pf in pending_files if pf.status == 'completed']
        if completed_files:
            stakeholders = _User.objects.filter(
                organization=org,
                role__in=['gp_admin', 'analyst', 'fund_accountant'],
                is_active=True,
            ).exclude(pk=user.pk)  # Exclude uploader (they already know)
            file_names = ', '.join(pf.original_filename for pf in completed_files)
            for su in stakeholders:
                Notification.objects.create(
                    user=su,
                    title=f'New Data Import — {len(completed_files)} file(s) processed',
                    message=(
                        f'{user.get_full_name() or user.username} imported {len(completed_files)} file(s): '
                        f'{file_names}. '
                        f'Portfolio data has been updated.'
                    ),
                    notification_type='mis_import',
                    severity='low',
                )
    except Exception as e:
        logger.warning(f'Post-import stakeholder notification failed: {e}')


# ---------------------------------------------------------------------------
# Uploaded files listing — shows all previously uploaded files for the org
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsGPUser])
def uploaded_files_list(request):
    """
    List all successfully imported files for this organization.
    Groups by the latest import per fund, showing file details + fund info.
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    # Get all import files for this org (all statuses), newest first.
    # Include stuck/failed imports so users can see and delete them.
    files = (
        ImportFile.objects.filter(
            job__organization=org,
        )
        .exclude(status='pending')
        .select_related('job', 'job__uploaded_by', 'fund')
        .order_by('-created_at')
    )

    # Deduplicate: per fund show the newest file; per stuck import show it once
    seen_keys = set()
    result = []
    for f in files:
        # Key: fund UUID if known, else file name (for stuck/no-fund imports)
        fund_key = str(f.fund_id) if f.fund_id else f.original_filename
        if fund_key in seen_keys:
            continue
        seen_keys.add(fund_key)

        result.append({
            'id': str(f.id),
            'original_filename': f.original_filename,
            'file_size': f.file_size,
            'fund_id': str(f.fund_id) if f.fund_id else None,
            'fund_name': f.fund_name or (f.fund.name if f.fund else ''),
            'status': f.status,
            'uploaded_by': (
                f.job.uploaded_by.first_name or f.job.uploaded_by.username
            ) if f.job.uploaded_by else 'Unknown',
            'uploaded_at': (f.completed_at or f.created_at).isoformat(),
            'job_id': str(f.job_id),
            'result_summary': f.job.result_summary.get(
                f.original_filename, {}
            ) if f.job.result_summary else {},
        })

    return Response(result)


# ---------------------------------------------------------------------------
# Stuck / partial imports — started but never completed
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsGPUser])
def stuck_imports_list(request):
    """
    Return all imports that started but never completed (status: importing/mapping/failed).
    Includes counts of data already written to the DB so the user knows what's orphaned.
    """
    from funds.models import Scheme
    from investments.models import Investment, PortfolioCompany
    from lp.models import Commitment

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    stuck_files = (
        ImportFile.objects.filter(
            job__organization=org,
            status__in=['importing', 'mapping', 'failed'],
        )
        .select_related('job', 'job__uploaded_by', 'fund')
        .order_by('-created_at')
    )

    # Deduplicate by fund_id — keep newest per fund; for null-fund files keep each
    seen_keys = set()
    result = []
    for f in stuck_files:
        key = str(f.fund_id) if f.fund_id else str(f.id)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        # Count data already written to DB for this fund (partial write)
        data_counts = {}
        if f.fund:
            scheme_ids = list(
                Scheme.objects.filter(fund=f.fund).values_list('id', flat=True)
            )
            if scheme_ids:
                inv_count = Investment.objects.filter(
                    scheme_id__in=scheme_ids).count()
                co_count = PortfolioCompany.objects.filter(
                    investments__scheme_id__in=scheme_ids
                ).distinct().count()
                commitment_count = Commitment.objects.filter(
                    scheme_id__in=scheme_ids).count()
                if inv_count:
                    data_counts['investments'] = inv_count
                if co_count:
                    data_counts['companies'] = co_count
                if commitment_count:
                    data_counts['commitments'] = commitment_count

        result.append({
            'id': str(f.id),
            'original_filename': f.original_filename,
            'file_size': f.file_size,
            'fund_id': str(f.fund_id) if f.fund_id else None,
            'fund_name': f.fund_name or (f.fund.name if f.fund else ''),
            'status': f.status,
            'uploaded_at': f.created_at.isoformat(),
            'uploaded_by': (
                f.job.uploaded_by.first_name or f.job.uploaded_by.username
            ) if f.job.uploaded_by else 'Unknown',
            'data_in_db': data_counts,
        })

    return Response(result)


# ---------------------------------------------------------------------------
# Delete an imported file and ALL its associated fund data
# ---------------------------------------------------------------------------

@api_view(['DELETE'])
@permission_classes([IsGPAdmin])
def delete_imported_file(request, file_id):
    """
    Delete an imported file and cascade-delete all data created by that import.

    This deletes:
    - The Fund record (cascades to Schemes, Investments, CapitalCalls,
      Commitments, Distributions, NAV, Ledger, etc.)
    - Related PortfolioNodes from the portfolio hierarchy
    - Orphaned Investors and PortfolioCompanies (if no other references)
    - The ImportFile and its physical file on disk
    - Reloads the portfolio cache
    """
    from django.db import transaction
    from django.utils.text import slugify
    from funds.models import Fund, Scheme
    from lp.models import Investor, Commitment
    from investments.models import PortfolioCompany, Investment
    from portfolio.models import PortfolioSnapshot, PortfolioNode
    from accounting.models import ChartOfAccounts

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    try:
        import_file = ImportFile.objects.select_related('fund', 'job').get(
            pk=file_id,
            job__organization=org,
        )
    except ImportFile.DoesNotExist:
        return Response({'detail': 'File not found.'}, status=404)

    fund = import_file.fund
    fund_name = import_file.fund_name or (fund.name if fund else 'Unknown')
    deleted_summary = {}

    with transaction.atomic():
        if fund:
            # Count what will be deleted for the response
            schemes = Scheme.objects.filter(fund=fund)
            scheme_ids = set(schemes.values_list('id', flat=True))

            deleted_summary = {
                'fund': fund.name,
                'schemes': schemes.count(),
                'investments': Investment.objects.filter(
                    scheme_id__in=scheme_ids).count(),
                'commitments': Commitment.objects.filter(
                    scheme_id__in=scheme_ids).count(),
            }

            # Delete portfolio nodes for this fund
            fund_slug = slugify(fund.name)
            node_prefix = f'fund_{fund_slug}'
            nodes_deleted = PortfolioNode.objects.filter(
                snapshot__organization=org,
                node_id__startswith=node_prefix,
            ).count()
            PortfolioNode.objects.filter(
                snapshot__organization=org,
                node_id__startswith=node_prefix,
            ).delete()
            deleted_summary['portfolio_nodes'] = nodes_deleted

            # Find investors/companies that ONLY have references through this fund
            investor_ids_this_fund = set(
                Commitment.objects.filter(scheme_id__in=scheme_ids)
                .values_list('investor_id', flat=True)
            )

            company_ids_this_fund = set(
                Investment.objects.filter(scheme_id__in=scheme_ids)
                .values_list('portfolio_company_id', flat=True)
            )

            # Delete the fund — cascades to schemes, investments, commitments,
            # capital calls, distributions, NAV, ledger, carried interest, etc.
            fund.delete()

            # Clean up orphaned investors (no remaining commitments)
            if investor_ids_this_fund:
                orphaned_investors = Investor.objects.filter(
                    id__in=investor_ids_this_fund,
                    organization=org,
                ).exclude(
                    commitments__isnull=False,
                )
                orphaned_count = orphaned_investors.count()
                orphaned_investors.delete()
                deleted_summary['orphaned_investors_cleaned'] = orphaned_count

            # Clean up orphaned portfolio companies (no remaining investments)
            if company_ids_this_fund:
                orphaned_companies = PortfolioCompany.objects.filter(
                    id__in=company_ids_this_fund,
                    organization=org,
                ).exclude(
                    investments__isnull=False,
                )
                orphaned_count = orphaned_companies.count()
                orphaned_companies.delete()
                deleted_summary['orphaned_companies_cleaned'] = orphaned_count

            # Clean up empty portfolio snapshots
            empty_snapshots = PortfolioSnapshot.objects.filter(
                organization=org,
            ).exclude(nodes__isnull=False)
            empty_snapshots.delete()

        # Delete all ImportFile records pointing to this fund (duplicates and stuck imports)
        if fund:
            sibling_files = ImportFile.objects.filter(
                job__organization=org,
                fund_id=fund.id,
            ).exclude(pk=import_file.pk)
            sibling_files.delete()

        # Delete the physical file from disk
        if import_file.file:
            try:
                storage = import_file.file.storage
                if storage.exists(import_file.file.name):
                    storage.delete(import_file.file.name)
            except Exception as e:
                logger.warning(f'Could not delete file from disk: {e}')

        # Delete the import file record
        import_file.delete()

        # Clean up empty import jobs (no remaining files)
        ImportJob.objects.filter(
            organization=org,
            files__isnull=True,
        ).delete()

    # Invalidate Redis cache for the deleted fund's org
    try:
        from config.cache_utils import invalidate_org_cache
        invalidate_org_cache(org.id)
    except Exception as e:
        logger.warning(f'Post-delete cache invalidation error: {e}')

    # Reload portfolio cache for this org
    try:
        from api.portfolio import service as portfolio_service
        portfolio_service.reload(org.id)
    except Exception as e:
        logger.warning(f'Portfolio cache reload error: {e}')

    log_audit(request, 'delete', 'dataimport', str(file_id), {
        'fund_name': fund_name,
        'deleted': deleted_summary,
    })

    return Response({
        'ok': True,
        'fund_name': fund_name,
        'deleted': deleted_summary,
    })


@api_view(['GET'])
@permission_classes([IsGPUser])
def derived_metrics_list(request):
    """Return fund-level metrics (Phase 3 reconciler output) with provenance.

    Phase 3 writes every dashboard-displayed metric into FundMetric. The
    response shape is preserved from the Phase 1/2 era so the frontend's
    `fmValue()` / `wireProvenance()` continue to read the same fields,
    but the source is now solely FundMetric — DerivedMetric and the
    legacy Pass-4 anchor pipeline are gone.

    Query params:
        fund:   funds.Fund UUID (all schemes of the fund)
        scheme: funds.Scheme UUID (one scheme only)
    """
    from .models import FundMetric
    from .canonical_schema import DERIVABLE_FUND_METRICS

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    fm_qs = FundMetric.objects.filter(organization=org).select_related('scheme')

    scheme_id = request.GET.get('scheme')
    fund_id = request.GET.get('fund')
    if scheme_id:
        fm_qs = fm_qs.filter(scheme_id=scheme_id)
    elif fund_id:
        fm_qs = fm_qs.filter(scheme__fund_id=fund_id)

    metrics = []
    for fm in fm_qs:
        meta = DERIVABLE_FUND_METRICS.get(fm.metric_key, {}) if isinstance(DERIVABLE_FUND_METRICS, dict) else {}
        inputs_used = fm.inputs_used or {}
        # Phase 3 stores priority_rule_applied, source_sheet, source_cells,
        # provenance_kind, formula_expression, alternatives, disagreements,
        # quality_flag inside inputs_used.
        metrics.append({
            'scheme_id':          str(fm.scheme_id),
            'scheme_name':        fm.scheme.name if fm.scheme else '',
            'metric_key':         fm.metric_key,
            'canonical_key':      fm.metric_key,
            'metric_label':       meta.get('label', fm.metric_key),
            'metric_unit':        meta.get('unit', ''),
            'metric_description': meta.get('description', ''),
            'value':              float(fm.value) if fm.value is not None else None,
            'formula_expression': inputs_used.get('formula_expression') or '',
            'inputs_used':        inputs_used,
            'confidence':         None,
            'gemini_reasoning':   inputs_used.get('priority_rule_reason') or '',
            'candidate_formulas': [],
            'derived_at':         fm.updated_at.isoformat() if getattr(fm, 'updated_at', None) else None,
            'source':             fm.source or 'extracted',
            'provenance': {
                'source':        fm.source,
                'source_sheet':  inputs_used.get('source_sheet'),
                'source_cells':  inputs_used.get('source_cells') or [],
                'reasoning':     inputs_used.get('priority_rule_reason')
                                 or inputs_used.get('note'),
                'inputs_used':   inputs_used,
            },
        })

    return Response({'metrics': metrics})
