"""
Chatbot API views — NL query endpoint.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .engine import ChatbotHandler
from .models import ChatMessage


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def chat_query(request):
    """
    Main chatbot endpoint.
    Body: { "query": "What is the IRR of Fund I?", "fund_id": "...", "company_id": "..." }
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

    handler = ChatbotHandler(organization=org, user=request.user)
    result = handler.handle(
        query, fund=fund, company=company,
        fund_name_override=fund_name_override,
    )

    return Response(result)


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
