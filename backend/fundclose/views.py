"""
Fund Close API — FundCloseEvent, ClawbackCalculation, SEBIDeregistration.
"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import serializers as drf_serializers

from .models import FundCloseEvent, ClawbackCalculation, SEBIDeregistration


# ---------------------------------------------------------------------------
# Inline serializers
# ---------------------------------------------------------------------------

class FundCloseEventSerializer(drf_serializers.ModelSerializer):
    status_display = drf_serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = FundCloseEvent
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at', 'initiated_by')


class ClawbackSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = ClawbackCalculation
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'calculated_by',
                            'return_of_capital_inr', 'preferred_return_inr',
                            'profit_above_hurdle_inr', 'gp_carry_owed_inr',
                            'clawback_amount_inr', 'clawback_direction')


class SEBIDeregistrationSerializer(drf_serializers.ModelSerializer):
    status_display = drf_serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = SEBIDeregistration
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def fund_close_list(request):
    from funds.models import Fund
    org = request.organization

    if request.method == 'GET':
        fund_id = request.query_params.get('fund')
        qs = FundCloseEvent.objects.filter(fund__organization=org)
        if fund_id:
            qs = qs.filter(fund_id=fund_id)
        return Response(FundCloseEventSerializer(qs, many=True).data)

    ser = FundCloseEventSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    event = ser.save(initiated_by=request.user)
    return Response(FundCloseEventSerializer(event).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def fund_close_detail(request, pk):
    org = request.organization
    try:
        event = FundCloseEvent.objects.get(pk=pk, fund__organization=org)
    except FundCloseEvent.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    if request.method == 'GET':
        return Response(FundCloseEventSerializer(event).data)
    ser = FundCloseEventSerializer(event, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(FundCloseEventSerializer(event).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def compute_clawback(request, close_event_pk):
    """
    Compute European waterfall and clawback for a fund close event.
    Body: { total_committed_capital_inr, total_drawn_capital_inr,
            total_distributions_inr, gp_carry_paid_inr,
            hurdle_rate_pct (optional, default 8),
            carry_rate_pct (optional, default 20),
            fund_life_years }
    """
    org = request.organization
    try:
        event = FundCloseEvent.objects.get(pk=close_event_pk, fund__organization=org)
    except FundCloseEvent.DoesNotExist:
        return Response({'detail': 'Fund close event not found.'}, status=404)

    from decimal import Decimal
    import datetime

    try:
        fund_life_years = float(request.data.get('fund_life_years', 7))
        calc, _ = ClawbackCalculation.objects.get_or_create(
            close_event=event,
            defaults={'calc_date': datetime.date.today()},
        )
        calc.total_committed_capital_inr = Decimal(str(request.data['total_committed_capital_inr']))
        calc.total_drawn_capital_inr = Decimal(str(request.data['total_drawn_capital_inr']))
        calc.total_distributions_inr = Decimal(str(request.data['total_distributions_inr']))
        calc.gp_carry_paid_inr = Decimal(str(request.data.get('gp_carry_paid_inr', 0)))
        calc.hurdle_rate_pct = Decimal(str(request.data.get('hurdle_rate_pct', '8.00')))
        calc.carry_rate_pct = Decimal(str(request.data.get('carry_rate_pct', '20.00')))
        calc.calc_date = datetime.date.today()
        calc.calculated_by = request.user
        calc.compute_waterfall(fund_life_years)
        calc.save()
    except (KeyError, Exception) as e:
        return Response({'detail': str(e)}, status=400)

    return Response(ClawbackSerializer(calc).data)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def sebi_deregistration(request, close_event_pk):
    org = request.organization
    try:
        event = FundCloseEvent.objects.get(pk=close_event_pk, fund__organization=org)
    except FundCloseEvent.DoesNotExist:
        return Response({'detail': 'Fund close event not found.'}, status=404)

    if request.method == 'GET':
        try:
            dereg = event.sebi_deregistration
            return Response(SEBIDeregistrationSerializer(dereg).data)
        except SEBIDeregistration.DoesNotExist:
            return Response({'detail': 'No deregistration record yet.'}, status=404)

    try:
        dereg = event.sebi_deregistration
        ser = SEBIDeregistrationSerializer(dereg, data=request.data, partial=True)
    except SEBIDeregistration.DoesNotExist:
        ser = SEBIDeregistrationSerializer(data=request.data)

    ser.is_valid(raise_exception=True)
    ser.save(close_event=event)
    return Response(ser.data, status=status.HTTP_201_CREATED)
