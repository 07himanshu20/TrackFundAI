"""
IC Workflow API views — Deal pipeline CRUD, IC presentation, voting, decisions.
"""
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import DealPipeline, ICPresentation, ICVote, ICDecision
from .serializers import (
    DealPipelineSerializer, ICPresentationSerializer, ICVoteSerializer, ICDecisionSerializer,
)

# All valid pipeline stages in funnel order
_ALL_STAGES = [
    'sourced', 'initial_screen', 'deep_dive',
    'term_sheet', 'ic_presentation', 'approved',
    'rejected', 'closed', 'passed',
]

# Map investment.status → DealPipeline.stage when seeding from investments
_STATUS_TO_STAGE = {
    'active':            'approved',
    'partially_exited':  'approved',
    'fully_exited':      'closed',
    'written_off':       'rejected',
}


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def deal_pipeline_list(request):
    org = request.organization
    if request.method == 'GET':
        stage   = request.query_params.get('stage')
        fund_id = request.query_params.get('fund')
        qs = DealPipeline.objects.filter(organization=org).select_related('fund')
        if fund_id:
            qs = qs.filter(fund_id=fund_id)
        if stage:
            qs = qs.filter(stage=stage)
        return Response(DealPipelineSerializer(qs, many=True).data)

    ser = DealPipelineSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    ser.save(organization=org, sourced_by=request.user)
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def deal_pipeline_detail(request, pk):
    org = request.organization
    try:
        deal = DealPipeline.objects.get(pk=pk, organization=org)
    except DealPipeline.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    if request.method == 'GET':
        return Response(DealPipelineSerializer(deal).data)

    ser = DealPipelineSerializer(deal, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(ser.data)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def ic_presentation_list(request, deal_pk):
    org = request.organization
    try:
        deal = DealPipeline.objects.get(pk=deal_pk, organization=org)
    except DealPipeline.DoesNotExist:
        return Response({'detail': 'Deal not found.'}, status=404)

    if request.method == 'GET':
        return Response(ICPresentationSerializer(deal.presentations.all(), many=True).data)

    ser = ICPresentationSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    presentation = ser.save(deal=deal, presenter=request.user)
    deal.stage = 'ic_presentation'
    deal.save(update_fields=['stage'])
    return Response(ICPresentationSerializer(presentation).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cast_vote(request, presentation_pk):
    """Cast or update IC member vote on a presentation."""
    org = request.organization
    try:
        pres = ICPresentation.objects.get(pk=presentation_pk, deal__organization=org)
    except ICPresentation.DoesNotExist:
        return Response({'detail': 'Presentation not found.'}, status=404)

    vote_val = request.data.get('vote')
    if vote_val not in ('approve', 'reject', 'abstain', 'defer'):
        return Response({'detail': 'Invalid vote value.'}, status=400)

    vote, created = ICVote.objects.update_or_create(
        presentation=pres,
        voter=request.user,
        defaults={
            'vote': vote_val,
            'comment': request.data.get('comment', ''),
            'conditions': request.data.get('conditions', ''),
        },
    )

    votes = pres.votes.all()
    approvals  = votes.filter(vote='approve').count()
    rejections = votes.filter(vote='reject').count()

    if approvals >= pres.quorum_required:
        pres.outcome = 'approved'
        pres.save(update_fields=['outcome'])
    elif rejections >= pres.quorum_required:
        pres.outcome = 'rejected'
        pres.save(update_fields=['outcome'])

    return Response({
        'vote': ICVoteSerializer(vote).data,
        'presentation_outcome': pres.outcome,
        'total_votes': votes.count(),
        'approvals': approvals,
        'rejections': rejections,
    }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def record_decision(request, presentation_pk):
    """Record final IC decision and optionally trigger capital call."""
    org = request.organization
    try:
        pres = ICPresentation.objects.get(pk=presentation_pk, deal__organization=org)
    except ICPresentation.DoesNotExist:
        return Response({'detail': 'Presentation not found.'}, status=404)

    ser = ICDecisionSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    decision = ser.save(presentation=pres, decided_by=request.user)
    return Response(ICDecisionSerializer(decision).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pipeline_summary(request):
    """
    Funnel summary: count of deals per stage.
    Returns a flat dict keyed by stage name so the frontend can read
    summary['sourced'], summary['approved'], etc. directly.
    'closed' deals (post-investment) are included in the 'approved' count
    for funnel display purposes.
    """
    org     = request.organization
    fund_id = request.query_params.get('fund')

    qs = DealPipeline.objects.filter(organization=org)
    if fund_id:
        qs = qs.filter(fund_id=fund_id)

    counts = {s: 0 for s in _ALL_STAGES}
    for row in qs.values('stage').annotate(count=Count('id')):
        if row['stage'] in counts:
            counts[row['stage']] = row['count']

    # 'closed' = investment made post-IC-approval → contributes to 'approved' funnel card
    counts['approved'] += counts.get('closed', 0)
    counts['total'] = qs.count()

    return Response(counts)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def seed_from_investments(request):
    """
    Idempotent: create DealPipeline records from existing Investment records
    for a fund. Each investment = one IC-approved deal. Safe to call multiple
    times — uses update_or_create keyed on (organization, fund, company_name).

    Body / query-param: fund_id (required)
    """
    from investments.models import Investment
    from funds.models import Fund

    org     = request.organization
    fund_id = request.data.get('fund_id') or request.query_params.get('fund')

    if not fund_id:
        return Response({'detail': 'fund_id required.'}, status=400)
    try:
        fund = Fund.objects.get(pk=fund_id, organization=org)
    except Fund.DoesNotExist:
        return Response({'detail': 'Fund not found.'}, status=404)

    qs = Investment.objects.filter(
        scheme__fund=fund,
    ).select_related('portfolio_company', 'scheme__fund')

    created_count = 0
    updated_count = 0

    for inv in qs:
        co             = inv.portfolio_company
        pipeline_stage = _STATUS_TO_STAGE.get(inv.status, 'approved')

        obj, created = DealPipeline.objects.update_or_create(
            organization=org,
            fund=fund,
            company_name=co.name,
            defaults={
                'sector':                   co.sector or inv.sector or '',
                'stage':                    pipeline_stage,
                'proposed_investment_inr':  inv.total_invested,
                'linked_portfolio_company': co,
                'source_channel':           'other',
                'sourced_date':             inv.investment_date,
            },
        )
        if created:
            created_count += 1
        else:
            updated_count += 1

    return Response({
        'created': created_count,
        'updated': updated_count,
        'total':   created_count + updated_count,
        'fund':    fund.name,
    })
