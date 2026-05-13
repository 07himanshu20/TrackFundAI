"""
Market Explorer API views.
"""
from django.http import HttpResponse
from rest_framework import status, serializers as drf_serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import MarketOpportunity, MarketStudy, FilterPreset
from .generator import generate_market_study, generate_pdf_report


class OpportunitySerializer(drf_serializers.ModelSerializer):
    sector_display = drf_serializers.CharField(source='get_sector_display', read_only=True)
    country_display = drf_serializers.CharField(source='get_country_display', read_only=True)
    continent_display = drf_serializers.CharField(source='get_continent_display', read_only=True)
    stage_display = drf_serializers.CharField(source='get_investment_stage_display', read_only=True)
    fin_category_display = drf_serializers.CharField(source='get_financial_category_display', read_only=True)

    class Meta:
        model = MarketOpportunity
        fields = '__all__'
        read_only_fields = ('id', 'slug', 'created_at', 'updated_at')


class MarketStudySerializer(drf_serializers.ModelSerializer):
    opportunity_name = drf_serializers.CharField(source='opportunity.name', read_only=True)
    status_display = drf_serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = MarketStudy
        fields = '__all__'
        read_only_fields = ('id', 'organization', 'generated_by', 'created_at', 'updated_at',
                            'status', 'word_count', 'generation_time_seconds')


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def opportunity_list(request):
    """
    List market opportunities with 6-dimension filtering.
    Query params: sector, country, continent, investment_stage, financial_category, fund_type, q
    """
    qs = MarketOpportunity.objects.filter(is_active=True)

    # 6-Filter system
    for field in ['sector', 'country', 'continent', 'investment_stage', 'financial_category', 'fund_type']:
        val = request.query_params.get(field)
        if val:
            qs = qs.filter(**{field: val})

    # Text search
    q = request.query_params.get('q')
    if q:
        qs = qs.filter(name__icontains=q) | qs.filter(description__icontains=q)

    return Response({
        'count': qs.count(),
        'results': OpportunitySerializer(qs[:100], many=True).data,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def opportunity_detail(request, pk):
    try:
        opp = MarketOpportunity.objects.get(pk=pk, is_active=True)
    except MarketOpportunity.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)
    return Response(OpportunitySerializer(opp).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def filter_options(request):
    """Return all filter dimension choices for the frontend dropdowns."""
    return Response({
        'sectors': [{'value': k, 'label': v} for k, v in MarketOpportunity.SECTOR_CHOICES],
        'countries': [{'value': k, 'label': v} for k, v in MarketOpportunity.COUNTRY_CHOICES],
        'continents': [{'value': k, 'label': v} for k, v in MarketOpportunity.CONTINENT_CHOICES],
        'investment_stages': [{'value': k, 'label': v} for k, v in MarketOpportunity.STAGE_CHOICES],
        'financial_categories': [{'value': k, 'label': v} for k, v in MarketOpportunity.FIN_CATEGORY_CHOICES],
        'fund_types': [{'value': k, 'label': v} for k, v in MarketOpportunity.FUND_TYPE_CHOICES],
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def generate_study(request, opportunity_pk):
    """
    Trigger AI generation of a market study for an opportunity.
    Runs synchronously in dev (async via Celery when configured).
    """
    org = request.organization
    try:
        opp = MarketOpportunity.objects.get(pk=opportunity_pk, is_active=True)
    except MarketOpportunity.DoesNotExist:
        return Response({'detail': 'Opportunity not found.'}, status=404)

    # Get or create study
    study, created = MarketStudy.objects.get_or_create(
        opportunity=opp,
        organization=org,
        defaults={'generated_by': request.user, 'status': 'generating'},
    )

    if not created and study.status == 'complete':
        return Response({
            'detail': 'Study already exists.',
            'study_id': str(study.pk),
            'status': study.status,
        })

    # Generate (synchronously in dev; use .delay() with Celery in prod)
    try:
        from django.conf import settings
        if hasattr(settings, 'CELERY_BROKER_URL') and not settings.DEBUG:
            # Async via Celery
            from .tasks import generate_market_study_task
            generate_market_study_task.delay(str(study.pk))
            return Response({
                'detail': 'Study generation started.',
                'study_id': str(study.pk),
                'status': 'generating',
            }, status=status.HTTP_202_ACCEPTED)
        else:
            # Synchronous for dev
            generate_market_study(str(study.pk))
            study.refresh_from_db()
            return Response(MarketStudySerializer(study).data, status=status.HTTP_201_CREATED)
    except Exception as e:
        study.status = 'failed'
        study.error_message = str(e)
        study.save(update_fields=['status', 'error_message'])
        return Response({'detail': f'Generation failed: {str(e)}'}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def study_detail(request, study_pk):
    """Get a market study with all 11 sections."""
    org = request.organization
    try:
        study = MarketStudy.objects.get(pk=study_pk, organization=org)
    except MarketStudy.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)
    return Response(MarketStudySerializer(study).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def download_study_pdf(request, study_pk):
    """Generate and download the PDF for a market study."""
    org = request.organization
    try:
        study = MarketStudy.objects.get(pk=study_pk, organization=org, status='complete')
    except MarketStudy.DoesNotExist:
        return Response({'detail': 'Study not found or not yet complete.'}, status=404)

    try:
        pdf_bytes = generate_pdf_report(str(study.pk))
        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        filename = f'{study.opportunity.slug}_market_study.pdf'
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as e:
        return Response({'detail': f'PDF generation failed: {str(e)}'}, status=500)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_studies(request):
    """List all market studies generated for this organization."""
    org = request.organization
    qs = MarketStudy.objects.filter(organization=org).select_related('opportunity')
    return Response(MarketStudySerializer(qs, many=True).data)
