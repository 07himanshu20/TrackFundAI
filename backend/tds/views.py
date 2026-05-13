"""
TDS API views — TDS withholding CRUD + Form 26Q generation.
"""
import datetime
from rest_framework import status, serializers as drf_serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import TDSWithholding, Form26QReturn


class TDSWithholdingSerializer(drf_serializers.ModelSerializer):
    payment_nature_display = drf_serializers.CharField(source='get_payment_nature_display', read_only=True)

    class Meta:
        model = TDSWithholding
        fields = '__all__'
        read_only_fields = ('id', 'organization', 'created_at', 'updated_at',
                            'base_tax_inr', 'surcharge_inr', 'cess_inr',
                            'total_tds_inr', 'net_payment_inr', 'quarter', 'financial_year')


class Form26QSerializer(drf_serializers.ModelSerializer):
    status_display = drf_serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Form26QReturn
        fields = '__all__'
        read_only_fields = ('id', 'organization', 'created_at', 'updated_at',
                            'total_transactions', 'total_gross_payment_inr',
                            'total_tds_deducted_inr', 'total_tds_deposited_inr')


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def tds_withholding_list(request):
    org = request.organization
    if request.method == 'GET':
        fy = request.query_params.get('financial_year')
        q = request.query_params.get('quarter')
        qs = TDSWithholding.objects.filter(organization=org)
        if fy:
            qs = qs.filter(financial_year=fy)
        if q:
            qs = qs.filter(quarter=q)
        return Response(TDSWithholdingSerializer(qs, many=True).data)

    ser = TDSWithholdingSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    record = ser.save(organization=org, created_by=request.user)
    return Response(TDSWithholdingSerializer(record).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'PATCH'])
@permission_classes([IsAuthenticated])
def tds_withholding_detail(request, pk):
    org = request.organization
    try:
        rec = TDSWithholding.objects.get(pk=pk, organization=org)
    except TDSWithholding.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    if request.method == 'GET':
        return Response(TDSWithholdingSerializer(rec).data)
    ser = TDSWithholdingSerializer(rec, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(TDSWithholdingSerializer(rec).data)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def form26q_list(request):
    org = request.organization
    if request.method == 'GET':
        qs = Form26QReturn.objects.filter(organization=org)
        return Response(Form26QSerializer(qs, many=True).data)

    ser = Form26QSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    ret = ser.save(organization=org)
    return Response(Form26QSerializer(ret).data, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def compute_form26q(request, pk):
    """Compute aggregate TDS for the quarter and update the return."""
    org = request.organization
    try:
        ret = Form26QReturn.objects.get(pk=pk, organization=org)
    except Form26QReturn.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    ret.compute()
    return Response(Form26QSerializer(ret).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def file_form26q(request, pk):
    """Mark a 26Q return as filed (after actual filing via TRACES)."""
    org = request.organization
    try:
        ret = Form26QReturn.objects.get(pk=pk, organization=org)
    except Form26QReturn.DoesNotExist:
        return Response({'detail': 'Not found.'}, status=404)

    traces_ack = request.data.get('traces_ack_no', '')
    if not traces_ack:
        return Response({'detail': 'traces_ack_no is required.'}, status=400)

    ret.traces_ack_no = traces_ack
    ret.filed_date = datetime.date.today()
    ret.filed_by = request.user
    ret.status = 'filed'
    ret.save()
    return Response(Form26QSerializer(ret).data)
