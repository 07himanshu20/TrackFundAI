from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.fund_access_helpers import get_accessible_fund_ids, user_has_fund_access
from accounts.permissions import IsGPUser
from .models import (
    ChartOfAccounts, NAVRecord, CarriedInterest,
    FundLedger, ManagementFeeSchedule,
)
from .serializers import (
    ChartOfAccountsSerializer,
    NAVRecordListSerializer, NAVRecordDetailSerializer,
    CarriedInterestSerializer,
    FundLedgerSerializer,
    ManagementFeeScheduleSerializer,
)


# -- Chart of Accounts CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def chart_of_accounts_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        qs = ChartOfAccounts.objects.filter(
            organization=org,
        ).select_related('parent_account')
        account_type = request.query_params.get('account_type')
        if account_type:
            qs = qs.filter(account_type=account_type)
        return Response(ChartOfAccountsSerializer(qs, many=True).data)

    ser = ChartOfAccountsSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    acct = ser.save(organization=org)
    log_audit(request, 'create', 'chart_of_accounts', acct.id, {
        'code': acct.account_code, 'name': acct.account_name,
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def chart_of_accounts_detail(request, account_id):
    org = request.organization
    try:
        acct = ChartOfAccounts.objects.get(pk=account_id, organization=org)
    except ChartOfAccounts.DoesNotExist:
        return Response({'detail': 'Account not found.'}, status=404)

    if request.method == 'GET':
        return Response(ChartOfAccountsSerializer(acct).data)

    if request.method == 'PUT':
        ser = ChartOfAccountsSerializer(acct, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'chart_of_accounts', acct.id, {
            'fields': list(request.data.keys()),
        })
        return Response(ser.data)

    log_audit(request, 'delete', 'chart_of_accounts', acct.id)
    acct.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# -- NAV Record CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def nav_record_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = NAVRecord.objects.filter(
            scheme__fund__organization=org,
            scheme__fund__id__in=fund_ids,
        ).select_related('scheme')
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(scheme_id=scheme_id)
        reconciled = request.query_params.get('reconciled')
        if reconciled is not None:
            qs = qs.filter(depository_reconciled=reconciled.lower() == 'true')
        return Response(NAVRecordListSerializer(qs, many=True).data)

    ser = NAVRecordDetailSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    nav = ser.save()
    log_audit(request, 'create', 'nav_record', nav.id, {
        'scheme': str(nav.scheme_id), 'date': str(nav.nav_date),
        'nav_per_unit': str(nav.nav_per_unit),
    })
    return Response(NAVRecordDetailSerializer(nav).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def nav_record_detail(request, nav_id):
    org = request.organization
    try:
        nav = NAVRecord.objects.select_related('scheme__fund').get(
            pk=nav_id, scheme__fund__organization=org,
        )
    except NAVRecord.DoesNotExist:
        return Response({'detail': 'NAV record not found.'}, status=404)

    if not user_has_fund_access(request.user, nav.scheme.fund):
        return Response({'detail': 'NAV record not found.'}, status=404)

    if request.method == 'GET':
        return Response(NAVRecordDetailSerializer(nav).data)

    ser = NAVRecordDetailSerializer(nav, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'nav_record', nav.id, {
        'fields': list(request.data.keys()),
    })
    return Response(NAVRecordDetailSerializer(nav).data)


# -- Carried Interest CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def carried_interest_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = CarriedInterest.objects.filter(
            scheme__fund__organization=org,
            scheme__fund__id__in=fund_ids,
        ).select_related('scheme')
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(scheme_id=scheme_id)
        return Response(CarriedInterestSerializer(qs, many=True).data)

    ser = CarriedInterestSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    carry = ser.save()
    log_audit(request, 'create', 'carried_interest', carry.id, {
        'scheme': str(carry.scheme_id), 'date': str(carry.calculation_date),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def carried_interest_detail(request, carry_id):
    org = request.organization
    try:
        carry = CarriedInterest.objects.select_related('scheme__fund').get(
            pk=carry_id, scheme__fund__organization=org,
        )
    except CarriedInterest.DoesNotExist:
        return Response({'detail': 'Carried interest record not found.'}, status=404)

    if not user_has_fund_access(request.user, carry.scheme.fund):
        return Response({'detail': 'Carried interest record not found.'}, status=404)

    if request.method == 'GET':
        return Response(CarriedInterestSerializer(carry).data)

    ser = CarriedInterestSerializer(carry, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'carried_interest', carry.id, {
        'fields': list(request.data.keys()),
    })
    return Response(ser.data)


# -- Fund Ledger CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def fund_ledger_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = FundLedger.objects.filter(
            scheme__fund__organization=org,
            scheme__fund__id__in=fund_ids,
        ).select_related('scheme', 'debit_account', 'credit_account')
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(scheme_id=scheme_id)
        ref_type = request.query_params.get('reference_type')
        if ref_type:
            qs = qs.filter(reference_type=ref_type)
        return Response(FundLedgerSerializer(qs, many=True).data)

    ser = FundLedgerSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    entry = ser.save(posted_by=request.user)
    log_audit(request, 'create', 'fund_ledger', entry.id, {
        'je': entry.journal_entry_number, 'amount': str(entry.amount),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsGPUser])
def fund_ledger_detail(request, entry_id):
    """Ledger entries are immutable -- read only (reverse to correct)."""
    org = request.organization
    try:
        entry = FundLedger.objects.select_related(
            'scheme__fund', 'debit_account', 'credit_account',
        ).get(pk=entry_id, scheme__fund__organization=org)
    except FundLedger.DoesNotExist:
        return Response({'detail': 'Ledger entry not found.'}, status=404)

    if not user_has_fund_access(request.user, entry.scheme.fund):
        return Response({'detail': 'Ledger entry not found.'}, status=404)

    return Response(FundLedgerSerializer(entry).data)


# -- Management Fee Schedule CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def management_fee_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = ManagementFeeSchedule.objects.filter(
            scheme__fund__organization=org,
            scheme__fund__id__in=fund_ids,
        ).select_related('scheme')
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(scheme_id=scheme_id)
        return Response(ManagementFeeScheduleSerializer(qs, many=True).data)

    ser = ManagementFeeScheduleSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    fee = ser.save()
    log_audit(request, 'create', 'management_fee', fee.id, {
        'scheme': str(fee.scheme_id),
        'period': f'{fee.period_start} to {fee.period_end}',
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def management_fee_detail(request, fee_id):
    org = request.organization
    try:
        fee = ManagementFeeSchedule.objects.select_related('scheme__fund').get(
            pk=fee_id, scheme__fund__organization=org,
        )
    except ManagementFeeSchedule.DoesNotExist:
        return Response({'detail': 'Fee schedule not found.'}, status=404)

    if not user_has_fund_access(request.user, fee.scheme.fund):
        return Response({'detail': 'Fee schedule not found.'}, status=404)

    if request.method == 'GET':
        return Response(ManagementFeeScheduleSerializer(fee).data)

    ser = ManagementFeeScheduleSerializer(fee, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'management_fee', fee.id, {
        'fields': list(request.data.keys()),
    })
    return Response(ser.data)
