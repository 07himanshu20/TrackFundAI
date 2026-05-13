from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsGPUser
from .models import EmailMISSubmission, MailboxPollLog


@api_view(['GET'])
@permission_classes([IsGPUser])
def submission_list(request):
    """List recent email MIS submissions for the org."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    qs = EmailMISSubmission.objects.filter(
        organization=org,
    ).select_related('portfolio_company', 'import_file').order_by('-received_at')[:100]

    data = [
        {
            'id': str(s.id),
            'sender_email': s.sender_email,
            'sender_name': s.sender_name,
            'subject': s.subject,
            'received_at': s.received_at.isoformat() if s.received_at else None,
            'portfolio_company': s.portfolio_company.name if s.portfolio_company else None,
            'attachment_filename': s.attachment_filename,
            'status': s.status,
            'error_message': s.error_message,
            'import_file_id': str(s.import_file_id) if s.import_file_id else None,
        }
        for s in qs
    ]
    return Response(data)


@api_view(['POST'])
@permission_classes([IsGPUser])
def trigger_poll(request):
    """Manually trigger a mailbox poll for the current organization."""
    from .tasks import poll_single_org
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    poll_single_org.delay(str(org.id))
    return Response({'detail': 'Mailbox poll triggered.'})


@api_view(['GET'])
@permission_classes([IsGPUser])
def poll_logs(request):
    """List recent poll logs for the org."""
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    logs = MailboxPollLog.objects.filter(organization=org).order_by('-polled_at')[:50]
    data = [
        {
            'polled_at': lg.polled_at.isoformat(),
            'emails_found': lg.emails_found,
            'emails_new': lg.emails_new,
            'emails_processed': lg.emails_processed,
            'success': lg.success,
            'error_message': lg.error_message,
        }
        for lg in logs
    ]
    return Response(data)
