from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Notification
from .serializers import NotificationSerializer


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def notification_list(request):
    """List notifications for the current user."""
    qs = Notification.objects.filter(recipient=request.user)

    # Filter by read status
    is_read = request.query_params.get('is_read')
    if is_read is not None:
        qs = qs.filter(is_read=is_read.lower() == 'true')

    # Filter by category
    category = request.query_params.get('category')
    if category:
        qs = qs.filter(category=category)

    return Response(NotificationSerializer(qs[:50], many=True).data)


@api_view(['PUT'])
@permission_classes([IsAuthenticated])
def notification_mark_read(request, notif_id):
    """Mark a single notification as read."""
    try:
        notif = Notification.objects.get(pk=notif_id, recipient=request.user)
    except Notification.DoesNotExist:
        return Response({'detail': 'Notification not found.'}, status=404)

    if not notif.is_read:
        notif.is_read = True
        notif.read_at = timezone.now()
        notif.save(update_fields=['is_read', 'read_at'])

    return Response(NotificationSerializer(notif).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def notification_mark_all_read(request):
    """Mark all unread notifications as read for the current user."""
    now = timezone.now()
    count = Notification.objects.filter(
        recipient=request.user, is_read=False,
    ).update(is_read=True, read_at=now)

    return Response({'marked_read': count})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def notification_unread_count(request):
    """Return the count of unread notifications (for badge display)."""
    count = Notification.objects.filter(
        recipient=request.user, is_read=False,
    ).count()
    return Response({'unread_count': count})
