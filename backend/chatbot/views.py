"""
Chatbot API views — NL query endpoint + conversation management.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .engine import ChatbotHandler
from .models import ChatMessage, ChatConversation


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def chat_query(request):
    """
    Main chatbot endpoint.
    Body: { "query": "...", "fund_id": "...", "company_id": "...", "conversation_id": "..." }
    """
    query = request.data.get('query', '').strip()
    if not query:
        return Response({'detail': 'query is required.'}, status=400)
    if len(query) > 1000:
        return Response({'detail': 'Query too long (max 1000 chars).'}, status=400)

    org = request.organization
    if not org:
        return Response({'detail': 'No organization context.'}, status=403)

    # Resolve optional fund/company
    fund = None
    company = None
    fund_id = request.data.get('fund_id')
    company_id = request.data.get('company_id')
    fund_name_override = request.data.get('fund_name')
    conversation_id = request.data.get('conversation_id')

    if fund_id:
        try:
            from funds.models import Fund
            fund = Fund.objects.select_related(
                'fund_category', 'manager_entity', 'trustee_entity',
                'custodian_entity', 'auditor_entity', 'sponsor_entity',
            ).get(pk=fund_id, organization=org)
        except Exception:
            pass

    if company_id:
        try:
            from investments.models import PortfolioCompany
            company = PortfolioCompany.objects.get(pk=company_id, organization=org)
        except Exception:
            pass

    # Resolve or create conversation
    conversation = None
    if conversation_id:
        try:
            conversation = ChatConversation.objects.get(
                pk=conversation_id, organization=org, user=request.user,
            )
        except ChatConversation.DoesNotExist:
            pass

    if not conversation:
        # Auto-create a new conversation, title from first query
        title = query[:100] if len(query) <= 100 else query[:97] + '...'
        conversation = ChatConversation.objects.create(
            organization=org, user=request.user, title=title,
        )
        _enforce_conversation_limit(org, request.user)

    handler = ChatbotHandler(organization=org, user=request.user)
    result = handler.handle(
        query, fund=fund, company=company,
        fund_name_override=fund_name_override,
    )

    # Link message to conversation
    message_id = result.get('message_id')
    if message_id:
        ChatMessage.objects.filter(pk=message_id).update(conversation=conversation)
        # Update conversation title if this is the first message
        if conversation.messages.count() <= 1:
            conversation.title = query[:100] if len(query) <= 100 else query[:97] + '...'
        conversation.save(update_fields=['updated_at', 'title'])

    result['conversation_id'] = str(conversation.pk)
    return Response(result)


def _enforce_conversation_limit(org, user, limit=10):
    """Keep only the most recent `limit` conversations per user."""
    conv_ids = list(
        ChatConversation.objects.filter(organization=org, user=user)
        .order_by('-updated_at')
        .values_list('pk', flat=True)
    )
    if len(conv_ids) > limit:
        old_ids = conv_ids[limit:]
        ChatConversation.objects.filter(pk__in=old_ids).delete()


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def chat_feedback(request, message_id):
    """
    Submit feedback on a chatbot response.
    Body: { "helpful": true/false }
    """
    org = request.organization
    try:
        msg = ChatMessage.objects.get(pk=message_id, organization=org)
    except ChatMessage.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    helpful = request.data.get('helpful')
    if helpful is not None:
        msg.helpful = bool(helpful)
        msg.save(update_fields=['helpful'])

    return Response({'detail': 'Feedback recorded.'})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def chat_history(request):
    """Return recent chat messages for the current user."""
    org = request.organization
    qs = ChatMessage.objects.filter(
        organization=org, user=request.user,
    )[:20]
    from rest_framework import serializers as drf_serializers

    class MsgSerializer(drf_serializers.ModelSerializer):
        class Meta:
            model = ChatMessage
            fields = ('id', 'query', 'intent', 'response', 'helpful', 'created_at')

    return Response(MsgSerializer(qs, many=True).data)


# ── Conversation endpoints ──────────────────────────────────────

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def conversation_list(request):
    """Return the user's recent conversations (max 10)."""
    org = request.organization
    convs = ChatConversation.objects.filter(
        organization=org, user=request.user,
    ).order_by('-updated_at')[:10]
    data = [
        {
            'id': str(c.pk),
            'title': c.title,
            'created_at': c.created_at.isoformat(),
            'updated_at': c.updated_at.isoformat(),
            'message_count': c.messages.count(),
        }
        for c in convs
    ]
    return Response(data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def conversation_messages(request, conversation_id):
    """Return all messages in a conversation, oldest first."""
    org = request.organization
    try:
        conv = ChatConversation.objects.get(
            pk=conversation_id, organization=org, user=request.user,
        )
    except ChatConversation.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    msgs = conv.messages.order_by('created_at')
    data = [
        {
            'id': str(m.pk),
            'query': m.query,
            'response': m.response,
            'intent': m.intent,
            'helpful': m.helpful,
            'created_at': m.created_at.isoformat(),
        }
        for m in msgs
    ]
    return Response({'id': str(conv.pk), 'title': conv.title, 'messages': data})


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def conversation_delete(request, conversation_id):
    """Delete a conversation and all its messages."""
    org = request.organization
    try:
        conv = ChatConversation.objects.get(
            pk=conversation_id, organization=org, user=request.user,
        )
    except ChatConversation.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    conv.delete()
    return Response({'detail': 'Deleted.'})
