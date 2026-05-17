from collections import defaultdict
from decimal import Decimal

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.fund_access_helpers import get_accessible_fund_ids, user_has_fund_access
from accounts.permissions import IsGPUser
from config.cache_utils import cached_api_view, invalidate_fund_cache
from funds.models import Scheme
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
@cached_api_view(timeout=900)
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
@cached_api_view(timeout=600)
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
        fund_id = request.query_params.get('fund')
        if fund_id:
            qs = qs.filter(scheme__fund_id=fund_id)
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
@cached_api_view(timeout=600)
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
        fund_id = request.query_params.get('fund')
        if fund_id:
            qs = qs.filter(scheme__fund_id=fund_id)
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
@cached_api_view(timeout=300)
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
        fund_id = request.query_params.get('fund')
        if fund_id:
            qs = qs.filter(scheme__fund_id=fund_id)
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
@cached_api_view(timeout=600)
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
        fund_id = request.query_params.get('fund')
        if fund_id:
            qs = qs.filter(scheme__fund_id=fund_id)
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


# -- Financial Statements (computed from ledger) --

@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def financial_statements(request, scheme_id, stmt_type):
    """Compute financial statements from ledger entries.

    stmt_type: 'bs' (Balance Sheet), 'is' (Income Statement), 'cf' (Cash Flow Statement)
    """
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    try:
        scheme = Scheme.objects.select_related('fund').get(pk=scheme_id)
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if not user_has_fund_access(request.user, scheme.fund):
        return Response({'detail': 'Scheme not found.'}, status=404)

    # Fetch all ledger entries for this scheme
    entries = FundLedger.objects.filter(
        scheme=scheme, is_reversed=False,
    ).select_related('debit_account', 'credit_account')

    # Build account balances from double-entry ledger
    # Debit increases asset/expense, Credit increases liability/equity/income
    account_balances = defaultdict(Decimal)
    account_info = {}

    for entry in entries:
        amt = entry.amount or Decimal('0')
        if entry.debit_account:
            acct = entry.debit_account
            account_info[acct.id] = {
                'account_name': acct.account_name,
                'account_code': acct.account_code,
                'account_type': acct.account_type,
            }
            # Debit: increase for asset/expense, decrease for liability/equity/income
            if acct.account_type in ('asset', 'expense'):
                account_balances[acct.id] += amt
            else:
                account_balances[acct.id] -= amt

        if entry.credit_account:
            acct = entry.credit_account
            account_info[acct.id] = {
                'account_name': acct.account_name,
                'account_code': acct.account_code,
                'account_type': acct.account_type,
            }
            # Credit: increase for liability/equity/income, decrease for asset/expense
            if acct.account_type in ('liability', 'equity', 'income'):
                account_balances[acct.id] += amt
            else:
                account_balances[acct.id] -= amt

    # Group by account type
    by_type = defaultdict(list)
    for acct_id, balance in account_balances.items():
        if balance == 0:
            continue
        info = account_info[acct_id]
        by_type[info['account_type']].append({
            'account_name': info['account_name'],
            'account_code': info['account_code'],
            'balance': float(abs(balance)),
        })

    # Sort each group by account code
    for atype in by_type:
        by_type[atype].sort(key=lambda x: x['account_code'])

    if stmt_type == 'bs':
        # Balance Sheet
        assets = by_type.get('asset', [])
        liabilities = by_type.get('liability', [])
        equity = by_type.get('equity', [])
        return Response({
            'assets': assets,
            'liabilities': liabilities,
            'equity': equity,
            'total_assets': sum(r['balance'] for r in assets),
            'total_liabilities': sum(r['balance'] for r in liabilities),
            'total_equity': sum(r['balance'] for r in equity),
        })

    elif stmt_type == 'is':
        # Income Statement
        income = by_type.get('income', [])
        expenses = by_type.get('expense', [])
        total_income = sum(r['balance'] for r in income)
        total_expenses = sum(r['balance'] for r in expenses)
        return Response({
            'income': income,
            'expenses': expenses,
            'total_income': total_income,
            'total_expenses': total_expenses,
            'net_income': total_income - total_expenses,
        })

    elif stmt_type == 'cf':
        # Cash Flow Statement — classify ledger entries by activity type
        operating = []
        investing = []
        financing = []

        # Re-scan entries for cash flow classification
        for entry in entries:
            amt = float(entry.amount or 0)
            ref = entry.reference_type or ''
            desc = entry.description or ''
            debit_code = entry.debit_account.account_code if entry.debit_account else ''
            credit_code = entry.credit_account.account_code if entry.credit_account else ''

            # Cash inflows: debit to Cash (1000)
            # Cash outflows: credit from Cash (1000)
            is_cash_debit = debit_code == '1000'
            is_cash_credit = credit_code == '1000'

            if not is_cash_debit and not is_cash_credit:
                continue  # Non-cash entry, skip for cash flow

            cash_amount = amt if is_cash_debit else -amt

            # Classify by reference type and accounts involved
            if ref in ('capital_call',):
                financing.append({
                    'description': desc or 'Capital call received',
                    'amount': cash_amount,
                })
            elif ref in ('distribution',):
                financing.append({
                    'description': desc or 'Distribution to LPs',
                    'amount': cash_amount,
                })
            elif ref in ('investment',):
                investing.append({
                    'description': desc or 'Investment made',
                    'amount': cash_amount,
                })
            elif ref in ('management_fee', 'expense'):
                operating.append({
                    'description': desc or 'Operating expense',
                    'amount': cash_amount,
                })
            elif ref in ('carried_interest',):
                operating.append({
                    'description': desc or 'Carried interest',
                    'amount': cash_amount,
                })
            else:
                # Classify by account involved
                other_code = credit_code if is_cash_debit else debit_code
                if other_code.startswith('4'):  # Income accounts
                    operating.append({
                        'description': desc or 'Income received',
                        'amount': cash_amount,
                    })
                elif other_code.startswith('5'):  # Expense accounts
                    operating.append({
                        'description': desc or 'Expense paid',
                        'amount': cash_amount,
                    })
                elif other_code.startswith('11'):  # Investment accounts
                    investing.append({
                        'description': desc or 'Investment activity',
                        'amount': cash_amount,
                    })
                elif other_code.startswith('3'):  # Equity accounts
                    financing.append({
                        'description': desc or 'Financing activity',
                        'amount': cash_amount,
                    })
                else:
                    operating.append({
                        'description': desc or 'Other cash flow',
                        'amount': cash_amount,
                    })

        net_operating = sum(r['amount'] for r in operating)
        net_investing = sum(r['amount'] for r in investing)
        net_financing = sum(r['amount'] for r in financing)

        return Response({
            'operating': operating,
            'investing': investing,
            'financing': financing,
            'net_operating': net_operating,
            'net_investing': net_investing,
            'net_financing': net_financing,
            'net_cash_flow': net_operating + net_investing + net_financing,
        })

    return Response({'detail': f'Unknown statement type: {stmt_type}'}, status=400)


# -- NAV Compute (engine-driven) --

@api_view(['POST'])
@permission_classes([IsGPUser])
def compute_nav(request, scheme_id):
    """Trigger NAV computation for a scheme as of a given date.

    Body: { "as_of_date": "2025-03-31" }  (optional; defaults to today)
    """
    from funds.models import Scheme
    from .nav_engine import compute_nav as _compute_nav
    import datetime

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    try:
        scheme = Scheme.objects.select_related('fund').get(pk=scheme_id)
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if not user_has_fund_access(request.user, scheme.fund):
        return Response({'detail': 'Scheme not found.'}, status=404)

    as_of_date = None
    raw_date = request.data.get('as_of_date')
    if raw_date:
        try:
            as_of_date = datetime.date.fromisoformat(raw_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=400)

    nav_record = _compute_nav(scheme, as_of_date)
    log_audit(request, 'compute', 'nav', nav_record.id, {
        'scheme': str(scheme_id), 'nav_date': str(nav_record.nav_date),
        'nav_per_unit': str(nav_record.nav_per_unit),
    })
    return Response(NAVRecordDetailSerializer(nav_record).data)


# -- Carry Compute (engine-driven) --

@api_view(['POST'])
@permission_classes([IsGPUser])
def compute_carry(request, scheme_id):
    """Trigger carry waterfall computation for a scheme.

    Body: { "as_of_date": "2025-03-31" }  (optional; defaults to today)
    """
    from funds.models import Scheme
    from .carry_engine import compute_carry as _compute_carry
    import datetime

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    try:
        scheme = Scheme.objects.select_related('fund').get(pk=scheme_id)
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if not user_has_fund_access(request.user, scheme.fund):
        return Response({'detail': 'Scheme not found.'}, status=404)

    as_of_date = None
    raw_date = request.data.get('as_of_date')
    if raw_date:
        try:
            import datetime as dt
            as_of_date = dt.date.fromisoformat(raw_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=400)

    carry_record = _compute_carry(scheme, as_of_date)
    log_audit(request, 'compute', 'carry', carry_record.id, {
        'scheme': str(scheme_id), 'date': str(carry_record.calculation_date),
        'carry_gross': str(carry_record.carry_amount_gross),
    })
    return Response(CarriedInterestSerializer(carry_record).data)


# -- Fee Compute (engine-driven) --

@api_view(['POST'])
@permission_classes([IsGPUser])
def compute_fee(request, scheme_id):
    """Trigger management fee computation for a scheme for a period.

    Body: { "period_start": "2025-01-01", "period_end": "2025-03-31" }
    """
    from funds.models import Scheme
    from .fee_engine import compute_management_fee
    import datetime as dt

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    try:
        scheme = Scheme.objects.select_related('fund').get(pk=scheme_id)
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if not user_has_fund_access(request.user, scheme.fund):
        return Response({'detail': 'Scheme not found.'}, status=404)

    try:
        period_start = dt.date.fromisoformat(request.data['period_start'])
        period_end = dt.date.fromisoformat(request.data['period_end'])
    except (KeyError, ValueError):
        return Response(
            {'detail': 'period_start and period_end required (YYYY-MM-DD).'},
            status=400,
        )

    if period_end <= period_start:
        return Response({'detail': 'period_end must be after period_start.'}, status=400)

    fee_schedule = compute_management_fee(scheme, period_start, period_end)
    log_audit(request, 'compute', 'management_fee', fee_schedule.id, {
        'scheme': str(scheme_id),
        'period': f'{period_start} to {period_end}',
        'fee_amount': str(fee_schedule.fee_amount),
    })
    return Response(ManagementFeeScheduleSerializer(fee_schedule).data)


# -- Trial Balance --

@api_view(['GET'])
@permission_classes([IsGPUser])
@cached_api_view(timeout=600)
def trial_balance(request, scheme_id):
    """Return a trial balance for a scheme as of a given date.

    Query param: ?as_of_date=2025-03-31  (optional; defaults to today)
    """
    from funds.models import Scheme
    from .trial_balance import generate_trial_balance
    import datetime as dt

    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    try:
        scheme = Scheme.objects.select_related('fund').get(pk=scheme_id)
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if not user_has_fund_access(request.user, scheme.fund):
        return Response({'detail': 'Scheme not found.'}, status=404)

    as_of_date = None
    raw_date = request.query_params.get('as_of_date')
    if raw_date:
        try:
            as_of_date = dt.date.fromisoformat(raw_date)
        except ValueError:
            return Response({'detail': 'Invalid date format. Use YYYY-MM-DD.'}, status=400)

    result = generate_trial_balance(scheme, as_of_date)
    return Response(result)
