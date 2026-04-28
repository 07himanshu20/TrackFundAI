from decimal import Decimal
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.audit import log_audit
from accounts.fund_access_helpers import get_accessible_fund_ids, user_has_fund_access
from accounts.permissions import IsGPUser
from notifications.helpers import notify_user
from .models import (
    BankAccount, Investor, Commitment, CapitalCall,
    CapitalCallLineItem, Distribution, DistributionLineItem,
    LPCapitalAccount,
)
from .serializers import (
    BankAccountSerializer,
    InvestorListSerializer, InvestorDetailSerializer, InvestorCreateSerializer,
    CommitmentSerializer,
    CapitalCallListSerializer, CapitalCallDetailSerializer,
    CapitalCallLineItemSerializer,
    DistributionListSerializer, DistributionDetailSerializer,
    DistributionLineItemSerializer,
    LPCapitalAccountSerializer,
)


# -- Investor CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def investor_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        # Only show investors who have commitments to funds user can access
        qs = Investor.objects.filter(
            organization=org,
            commitments__scheme__fund__id__in=fund_ids,
        ).distinct()
        investor_type = request.query_params.get('investor_type')
        if investor_type:
            qs = qs.filter(investor_type=investor_type)
        kyc_status = request.query_params.get('kyc_status')
        if kyc_status:
            qs = qs.filter(kyc_status=kyc_status)
        return Response(InvestorListSerializer(qs, many=True).data)

    ser = InvestorCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    investor = ser.save(organization=org)
    log_audit(request, 'create', 'investor', investor.id, {
        'name': investor.investor_name, 'type': investor.investor_type,
    })
    return Response(InvestorDetailSerializer(investor).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def investor_detail(request, investor_id):
    org = request.organization
    try:
        investor = Investor.objects.select_related(
            'primary_bank_account',
        ).get(pk=investor_id, organization=org)
    except Investor.DoesNotExist:
        return Response({'detail': 'Investor not found.'}, status=404)

    if request.method == 'GET':
        return Response(InvestorDetailSerializer(investor).data)

    if request.method == 'PUT':
        ser = InvestorCreateSerializer(investor, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'investor', investor.id, {
            'name': investor.investor_name, 'fields': list(request.data.keys()),
        })
        return Response(InvestorDetailSerializer(investor).data)

    log_audit(request, 'delete', 'investor', investor.id, {
        'name': investor.investor_name,
    })
    investor.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# -- Bank Account CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def bank_account_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        qs = BankAccount.objects.filter(organization=org)
        return Response(BankAccountSerializer(qs, many=True).data)

    ser = BankAccountSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    acct = ser.save(organization=org)
    log_audit(request, 'create', 'bank_account', acct.id, {
        'bank': acct.bank_name, 'holder': acct.account_holder_name,
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def bank_account_detail(request, account_id):
    org = request.organization
    try:
        acct = BankAccount.objects.get(pk=account_id, organization=org)
    except BankAccount.DoesNotExist:
        return Response({'detail': 'Bank account not found.'}, status=404)

    if request.method == 'GET':
        return Response(BankAccountSerializer(acct).data)

    if request.method == 'PUT':
        ser = BankAccountSerializer(acct, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'bank_account', acct.id, {
            'fields': list(request.data.keys()),
        })
        return Response(ser.data)

    log_audit(request, 'delete', 'bank_account', acct.id)
    acct.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# -- Commitment CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def commitment_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = Commitment.objects.filter(
            investor__organization=org,
            scheme__fund__id__in=fund_ids,
        ).select_related('investor', 'scheme')
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(scheme_id=scheme_id)
        investor_id = request.query_params.get('investor')
        if investor_id:
            qs = qs.filter(investor_id=investor_id)
        return Response(CommitmentSerializer(qs, many=True).data)

    ser = CommitmentSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    commitment = ser.save()
    log_audit(request, 'create', 'commitment', commitment.id, {
        'investor': str(commitment.investor_id),
        'scheme': str(commitment.scheme_id),
        'amount': str(commitment.commitment_amount),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def commitment_detail(request, commitment_id):
    org = request.organization
    try:
        commitment = Commitment.objects.select_related(
            'investor', 'scheme__fund',
        ).get(pk=commitment_id, investor__organization=org)
    except Commitment.DoesNotExist:
        return Response({'detail': 'Commitment not found.'}, status=404)

    if not user_has_fund_access(request.user, commitment.scheme.fund):
        return Response({'detail': 'Commitment not found.'}, status=404)

    if request.method == 'GET':
        return Response(CommitmentSerializer(commitment).data)

    if request.method == 'PUT':
        ser = CommitmentSerializer(commitment, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'commitment', commitment.id, {
            'fields': list(request.data.keys()),
        })
        return Response(ser.data)

    log_audit(request, 'delete', 'commitment', commitment.id)
    commitment.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# -- Capital Call CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def capital_call_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = CapitalCall.objects.filter(
            scheme__fund__organization=org,
            scheme__fund__id__in=fund_ids,
        ).select_related('scheme')
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(scheme_id=scheme_id)
        return Response(CapitalCallListSerializer(qs, many=True).data)

    ser = CapitalCallDetailSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    call = ser.save(created_by=request.user)
    log_audit(request, 'create', 'capital_call', call.id, {
        'scheme': str(call.scheme_id), 'call_number': call.call_number,
    })
    return Response(CapitalCallDetailSerializer(call).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def capital_call_detail(request, call_id):
    org = request.organization
    try:
        call = CapitalCall.objects.select_related('scheme__fund').prefetch_related(
            'line_items__commitment__investor',
        ).get(pk=call_id, scheme__fund__organization=org)
    except CapitalCall.DoesNotExist:
        return Response({'detail': 'Capital call not found.'}, status=404)

    if not user_has_fund_access(request.user, call.scheme.fund):
        return Response({'detail': 'Capital call not found.'}, status=404)

    if request.method == 'GET':
        return Response(CapitalCallDetailSerializer(call).data)

    if request.method == 'PUT':
        ser = CapitalCallDetailSerializer(call, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'capital_call', call.id, {
            'fields': list(request.data.keys()),
        })
        return Response(CapitalCallDetailSerializer(call).data)

    log_audit(request, 'delete', 'capital_call', call.id)
    call.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# -- Capital Call Line Items --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def capital_call_line_item_list(request, call_id):
    org = request.organization
    try:
        call = CapitalCall.objects.select_related('scheme__fund').get(
            pk=call_id, scheme__fund__organization=org,
        )
    except CapitalCall.DoesNotExist:
        return Response({'detail': 'Capital call not found.'}, status=404)

    if not user_has_fund_access(request.user, call.scheme.fund):
        return Response({'detail': 'Capital call not found.'}, status=404)

    if request.method == 'GET':
        items = call.line_items.select_related('commitment__investor').all()
        return Response(CapitalCallLineItemSerializer(items, many=True).data)

    data = request.data.copy()
    data['capital_call'] = str(call.id)
    ser = CapitalCallLineItemSerializer(data=data)
    ser.is_valid(raise_exception=True)
    item = ser.save()
    log_audit(request, 'create', 'capital_call_line_item', item.id, {
        'call': str(call.id), 'commitment': str(item.commitment_id),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


# -- Distribution CRUD --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def distribution_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = Distribution.objects.filter(
            scheme__fund__organization=org,
            scheme__fund__id__in=fund_ids,
        ).select_related('scheme')
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(scheme_id=scheme_id)
        return Response(DistributionListSerializer(qs, many=True).data)

    ser = DistributionDetailSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    dist = ser.save(created_by=request.user)
    log_audit(request, 'create', 'distribution', dist.id, {
        'scheme': str(dist.scheme_id), 'number': dist.distribution_number,
    })
    return Response(DistributionDetailSerializer(dist).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsGPUser])
def distribution_detail(request, distribution_id):
    org = request.organization
    try:
        dist = Distribution.objects.select_related('scheme__fund').prefetch_related(
            'line_items__commitment__investor',
        ).get(pk=distribution_id, scheme__fund__organization=org)
    except Distribution.DoesNotExist:
        return Response({'detail': 'Distribution not found.'}, status=404)

    if not user_has_fund_access(request.user, dist.scheme.fund):
        return Response({'detail': 'Distribution not found.'}, status=404)

    if request.method == 'GET':
        return Response(DistributionDetailSerializer(dist).data)

    if request.method == 'PUT':
        ser = DistributionDetailSerializer(dist, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        log_audit(request, 'update', 'distribution', dist.id, {
            'fields': list(request.data.keys()),
        })
        return Response(DistributionDetailSerializer(dist).data)

    log_audit(request, 'delete', 'distribution', dist.id)
    dist.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# -- Distribution Line Items --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def distribution_line_item_list(request, distribution_id):
    org = request.organization
    try:
        dist = Distribution.objects.select_related('scheme__fund').get(
            pk=distribution_id, scheme__fund__organization=org,
        )
    except Distribution.DoesNotExist:
        return Response({'detail': 'Distribution not found.'}, status=404)

    if not user_has_fund_access(request.user, dist.scheme.fund):
        return Response({'detail': 'Distribution not found.'}, status=404)

    if request.method == 'GET':
        items = dist.line_items.select_related('commitment__investor').all()
        return Response(DistributionLineItemSerializer(items, many=True).data)

    data = request.data.copy()
    data['distribution'] = str(dist.id)
    ser = DistributionLineItemSerializer(data=data)
    ser.is_valid(raise_exception=True)
    item = ser.save()
    log_audit(request, 'create', 'distribution_line_item', item.id, {
        'distribution': str(dist.id), 'commitment': str(item.commitment_id),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


# -- LP Capital Account --

@api_view(['GET', 'POST'])
@permission_classes([IsGPUser])
def lp_capital_account_list(request):
    org = request.organization
    if not org:
        return Response({'detail': 'No organization.'}, status=403)

    if request.method == 'GET':
        fund_ids = get_accessible_fund_ids(request.user)
        qs = LPCapitalAccount.objects.filter(
            commitment__investor__organization=org,
            commitment__scheme__fund__id__in=fund_ids,
        ).select_related('commitment__investor', 'commitment__scheme')
        commitment_id = request.query_params.get('commitment')
        if commitment_id:
            qs = qs.filter(commitment_id=commitment_id)
        scheme_id = request.query_params.get('scheme')
        if scheme_id:
            qs = qs.filter(commitment__scheme_id=scheme_id)
        as_of = request.query_params.get('as_of_date')
        if as_of:
            qs = qs.filter(as_of_date=as_of)
        return Response(LPCapitalAccountSerializer(qs, many=True).data)

    ser = LPCapitalAccountSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    acct = ser.save()
    log_audit(request, 'create', 'lp_capital_account', acct.id, {
        'commitment': str(acct.commitment_id), 'date': str(acct.as_of_date),
    })
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PUT'])
@permission_classes([IsGPUser])
def lp_capital_account_detail(request, account_id):
    org = request.organization
    try:
        acct = LPCapitalAccount.objects.select_related(
            'commitment__investor', 'commitment__scheme__fund',
        ).get(pk=account_id, commitment__investor__organization=org)
    except LPCapitalAccount.DoesNotExist:
        return Response({'detail': 'Capital account not found.'}, status=404)

    if not user_has_fund_access(request.user, acct.commitment.scheme.fund):
        return Response({'detail': 'Capital account not found.'}, status=404)

    if request.method == 'GET':
        return Response(LPCapitalAccountSerializer(acct).data)

    ser = LPCapitalAccountSerializer(acct, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    log_audit(request, 'update', 'lp_capital_account', acct.id, {
        'fields': list(request.data.keys()),
    })
    return Response(ser.data)


# ═══════════════════════════════════════════════════════════════
# ACTION ENDPOINTS — Beyond CRUD
# ═══════════════════════════════════════════════════════════════


# -- KYC Verification --

@api_view(['POST'])
@permission_classes([IsGPUser])
def verify_kyc(request, investor_id):
    """
    Trigger KYC verification for an investor.
    Updates kyc_status and sets completion/expiry dates.
    Accepts: { "action": "approve" | "reject" | "request_review",
               "kyc_expiry_date": "YYYY-MM-DD" (optional) }
    """
    org = request.organization
    try:
        investor = Investor.objects.get(pk=investor_id, organization=org)
    except Investor.DoesNotExist:
        return Response({'detail': 'Investor not found.'}, status=404)

    action = request.data.get('action', '').lower()
    valid_actions = ['approve', 'reject', 'request_review']
    if action not in valid_actions:
        return Response(
            {'detail': f'action must be one of: {", ".join(valid_actions)}'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if action == 'approve':
        investor.kyc_status = 'completed'
        investor.kyc_completed_date = timezone.now().date()
        expiry = request.data.get('kyc_expiry_date')
        if expiry:
            investor.kyc_expiry_date = expiry
    elif action == 'reject':
        investor.kyc_status = 'rejected'
    elif action == 'request_review':
        investor.kyc_status = 'in_progress'

    investor.save()

    log_audit(request, 'update', 'investor', investor.id, {
        'action': f'kyc_{action}', 'name': investor.investor_name,
    })

    # Notify the LP's portal user if linked
    if investor.portal_user:
        notify_user(
            investor.portal_user,
            f'KYC {action.replace("_", " ").title()}',
            f'Your KYC status has been updated to: {investor.get_kyc_status_display()}.',
            category='compliance',
            resource_type='investor',
            resource_id=investor.id,
            created_by=request.user,
        )

    return Response({
        'detail': f'KYC {action} successful.',
        'investor': InvestorDetailSerializer(investor).data,
    })


# -- Bank Verification (Penny Drop) --

@api_view(['POST'])
@permission_classes([IsGPUser])
def verify_bank(request, investor_id):
    """
    Trigger penny-drop bank account verification for an investor.
    In production, this would call NPCI/bank API.
    For now, validates that bank account data is complete and marks as verified.
    Accepts: { "bank_account_id": "<uuid>" }
    """
    org = request.organization
    try:
        investor = Investor.objects.get(pk=investor_id, organization=org)
    except Investor.DoesNotExist:
        return Response({'detail': 'Investor not found.'}, status=404)

    bank_account_id = request.data.get('bank_account_id')
    if not bank_account_id:
        # If no specific bank account, use primary
        bank_account = investor.primary_bank_account
        if not bank_account:
            return Response(
                {'detail': 'No bank account linked to this investor.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        try:
            bank_account = BankAccount.objects.get(pk=bank_account_id, organization=org)
        except BankAccount.DoesNotExist:
            return Response({'detail': 'Bank account not found.'}, status=404)

    # Validate required fields
    missing = []
    if not bank_account.account_number:
        missing.append('account_number')
    if not bank_account.ifsc_code:
        missing.append('ifsc_code')
    if not bank_account.account_holder_name:
        missing.append('account_holder_name')
    if missing:
        return Response({
            'detail': f'Bank account missing required fields: {", ".join(missing)}',
            'verified': False,
        }, status=status.HTTP_400_BAD_REQUEST)

    # In production: call penny-drop API here
    # For now, mark as verified by linking it as primary
    investor.primary_bank_account = bank_account
    investor.save()

    log_audit(request, 'update', 'investor', investor.id, {
        'action': 'bank_verification',
        'bank_account': str(bank_account.id),
    })

    return Response({
        'detail': 'Bank account verified successfully.',
        'verified': True,
        'bank_account': BankAccountSerializer(bank_account).data,
    })


# -- Send Capital Call Notices --

@api_view(['POST'])
@permission_classes([IsGPUser])
def send_call_notices(request, call_id):
    """
    Send capital call notices to all LPs in the call.
    Creates in-app notifications for each LP's portal user.
    Updates call status to 'sent'.
    In Phase 6, this will also trigger email + WhatsApp delivery.
    """
    org = request.organization
    try:
        call = CapitalCall.objects.select_related('scheme__fund').prefetch_related(
            'line_items__commitment__investor',
        ).get(pk=call_id, scheme__fund__organization=org)
    except CapitalCall.DoesNotExist:
        return Response({'detail': 'Capital call not found.'}, status=404)

    if not user_has_fund_access(request.user, call.scheme.fund):
        return Response({'detail': 'Capital call not found.'}, status=404)

    line_items = call.line_items.select_related(
        'commitment__investor__portal_user',
    ).all()

    if not line_items.exists():
        return Response(
            {'detail': 'No line items found for this capital call. Add line items first.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    notified_count = 0
    for item in line_items:
        investor = item.commitment.investor
        portal_user = investor.portal_user

        if portal_user:
            notify_user(
                portal_user,
                f'Capital Call #{call.call_number} — {call.scheme.name}',
                f'A capital call of ₹{item.called_amount:,.2f} ({call.call_percentage}% of commitment) '
                f'has been issued. Payment due by {call.payment_due_date.strftime("%d %b %Y")}.',
                category='capital_call',
                priority='high',
                resource_type='capital_call',
                resource_id=call.id,
                created_by=request.user,
            )
            notified_count += 1

    # Update call status to 'sent'
    if call.call_status == 'draft':
        call.call_status = 'sent'
        call.save()

    log_audit(request, 'update', 'capital_call', call.id, {
        'action': 'send_notices',
        'notified_count': notified_count,
        'total_line_items': line_items.count(),
    })

    return Response({
        'detail': f'Notices sent. {notified_count} LP(s) notified via in-app notification.',
        'notified_count': notified_count,
        'total_line_items': line_items.count(),
        'call_status': call.call_status,
    })


# -- UTR Reconciliation --

@api_view(['POST'])
@permission_classes([IsGPUser])
def match_utr(request, call_id):
    """
    Match a UTR (payment reference) to a capital call line item.
    Accepts: { "commitment_id": "<uuid>", "utr_number": "...",
               "amount_received": 1000000.00, "payment_date": "YYYY-MM-DD" }
    """
    org = request.organization
    try:
        call = CapitalCall.objects.select_related('scheme__fund').get(
            pk=call_id, scheme__fund__organization=org,
        )
    except CapitalCall.DoesNotExist:
        return Response({'detail': 'Capital call not found.'}, status=404)

    if not user_has_fund_access(request.user, call.scheme.fund):
        return Response({'detail': 'Capital call not found.'}, status=404)

    commitment_id = request.data.get('commitment_id')
    utr_number = request.data.get('utr_number', '')
    amount_received = request.data.get('amount_received')
    payment_date = request.data.get('payment_date')

    if not commitment_id or amount_received is None:
        return Response(
            {'detail': 'commitment_id and amount_received are required.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        item = CapitalCallLineItem.objects.get(
            capital_call=call, commitment_id=commitment_id,
        )
    except CapitalCallLineItem.DoesNotExist:
        return Response({'detail': 'Line item not found for this commitment.'}, status=404)

    item.utr_number = utr_number
    item.amount_received = Decimal(str(amount_received))
    if payment_date:
        item.payment_date = payment_date

    # Determine payment status
    if item.amount_received >= item.called_amount:
        item.payment_status = 'paid'
    elif item.amount_received > 0:
        item.payment_status = 'partial'

    item.save()

    # Check if all line items are paid — update call status
    all_items = call.line_items.all()
    all_paid = all(li.payment_status == 'paid' for li in all_items)
    if all_paid and all_items.exists():
        call.call_status = 'paid'
        call.save()

    log_audit(request, 'update', 'capital_call_line_item', item.id, {
        'action': 'utr_match', 'utr': utr_number, 'amount': str(amount_received),
    })

    return Response({
        'detail': f'UTR matched. Payment status: {item.get_payment_status_display()}.',
        'line_item': CapitalCallLineItemSerializer(item).data,
        'call_status': call.call_status,
    })


# -- Process Distribution --

@api_view(['POST'])
@permission_classes([IsGPUser])
def process_distribution(request, distribution_id):
    """
    Process a distribution — mark as distributed, notify LPs.
    Accepts: { "payment_date": "YYYY-MM-DD" (optional, defaults to today) }
    """
    org = request.organization
    try:
        dist = Distribution.objects.select_related('scheme__fund').prefetch_related(
            'line_items__commitment__investor__portal_user',
        ).get(pk=distribution_id, scheme__fund__organization=org)
    except Distribution.DoesNotExist:
        return Response({'detail': 'Distribution not found.'}, status=404)

    if not user_has_fund_access(request.user, dist.scheme.fund):
        return Response({'detail': 'Distribution not found.'}, status=404)

    if dist.distribution_status == 'distributed':
        return Response(
            {'detail': 'Distribution has already been processed.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    payment_date = request.data.get('payment_date', timezone.now().date().isoformat())

    # Update all line items with payment date
    line_items = dist.line_items.select_related(
        'commitment__investor__portal_user',
    ).all()

    notified_count = 0
    for item in line_items:
        item.payment_date = payment_date
        item.save()

        # Notify LP
        investor = item.commitment.investor
        if investor.portal_user:
            notify_user(
                investor.portal_user,
                f'Distribution #{dist.distribution_number} — {dist.scheme.name}',
                f'A {dist.get_distribution_type_display()} distribution of ₹{item.net_amount:,.2f} '
                f'(net of TDS ₹{item.tds_amount:,.2f}) has been processed to your account.',
                category='distribution',
                priority='high',
                resource_type='distribution',
                resource_id=dist.id,
                created_by=request.user,
            )
            notified_count += 1

    # Update distribution status
    dist.distribution_status = 'distributed'
    dist.save()

    log_audit(request, 'update', 'distribution', dist.id, {
        'action': 'process', 'payment_date': payment_date,
        'notified_count': notified_count,
    })

    return Response({
        'detail': f'Distribution processed. {notified_count} LP(s) notified.',
        'distribution': DistributionDetailSerializer(dist).data,
    })


# -- Unit Allotment --

@api_view(['POST'])
@permission_classes([IsGPUser])
def allot_units(request, scheme_id):
    """
    Allot units to LPs for a scheme at a given NAV per unit.
    Accepts: { "nav_per_unit": 100.00, "allotments": [
        { "commitment_id": "<uuid>", "units": 1000.00 }, ...
    ] }
    OR auto-calculate: { "nav_per_unit": 100.00 }
    When no allotments provided, auto-calculates units = called_capital / nav_per_unit
    for all active commitments.
    """
    from funds.models import Scheme
    org = request.organization
    try:
        scheme = Scheme.objects.select_related('fund').get(
            pk=scheme_id, fund__organization=org,
        )
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if not user_has_fund_access(request.user, scheme.fund):
        return Response({'detail': 'Scheme not found.'}, status=404)

    nav_per_unit = request.data.get('nav_per_unit')
    if not nav_per_unit or Decimal(str(nav_per_unit)) <= 0:
        return Response(
            {'detail': 'nav_per_unit is required and must be > 0.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    nav_per_unit = Decimal(str(nav_per_unit))

    allotments = request.data.get('allotments')
    results = []

    if allotments:
        # Manual allotment
        for entry in allotments:
            try:
                commitment = Commitment.objects.get(
                    pk=entry['commitment_id'], scheme=scheme,
                )
            except Commitment.DoesNotExist:
                continue
            commitment.units_allocated = Decimal(str(entry['units']))
            commitment.save()
            results.append({
                'commitment_id': str(commitment.id),
                'investor': commitment.investor.investor_name,
                'units': str(commitment.units_allocated),
            })
    else:
        # Auto-calculate from called capital
        active_commitments = Commitment.objects.filter(
            scheme=scheme, commitment_status='active',
        ).select_related('investor')

        for commitment in active_commitments:
            # Sum all called amounts for this commitment
            total_called = CapitalCallLineItem.objects.filter(
                commitment=commitment, payment_status='paid',
            ).aggregate(total=Sum('amount_received'))['total'] or Decimal('0')

            if total_called > 0:
                units = (total_called / nav_per_unit).quantize(Decimal('0.000001'))
                commitment.units_allocated = units
                commitment.save()
                results.append({
                    'commitment_id': str(commitment.id),
                    'investor': commitment.investor.investor_name,
                    'called_capital': str(total_called),
                    'units': str(units),
                })

    log_audit(request, 'create', 'unit_allotment', str(scheme.id), {
        'nav_per_unit': str(nav_per_unit), 'allotments_count': len(results),
    })

    return Response({
        'detail': f'{len(results)} commitment(s) updated with unit allotments.',
        'nav_per_unit': str(nav_per_unit),
        'allotments': results,
    })


# -- LP Portal Dashboard --

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def lp_dashboard(request):
    """
    LP portal dashboard — returns the logged-in LP's capital account summary
    with IRR, TVPI, DPI, RVPI, MOIC across all their commitments.
    """
    user = request.user

    # Find investor profile for this user
    try:
        investor = Investor.objects.get(portal_user=user)
    except Investor.DoesNotExist:
        # Fallback: if user is GP, return aggregate for all investors in org
        if user.role in ('gp_admin', 'gp_user', 'platform_admin'):
            org = request.organization
            if not org:
                return Response({'detail': 'No organization.'}, status=403)
            fund_ids = get_accessible_fund_ids(user)
            accounts = LPCapitalAccount.objects.filter(
                commitment__investor__organization=org,
                commitment__scheme__fund__id__in=fund_ids,
            ).select_related('commitment__investor', 'commitment__scheme')

            # Get the latest snapshot per commitment
            latest = {}
            for acc in accounts:
                key = str(acc.commitment_id)
                if key not in latest or acc.as_of_date > latest[key].as_of_date:
                    latest[key] = acc

            snapshots = list(latest.values())
            return Response(_build_dashboard_summary(snapshots, is_gp=True))

        return Response({'detail': 'No investor profile linked to this user.'}, status=404)

    # Get all commitments for this investor
    commitment_ids = Commitment.objects.filter(
        investor=investor,
    ).values_list('id', flat=True)

    # Get latest capital account snapshot per commitment
    accounts = LPCapitalAccount.objects.filter(
        commitment_id__in=commitment_ids,
    ).select_related('commitment__scheme')

    latest = {}
    for acc in accounts:
        key = str(acc.commitment_id)
        if key not in latest or acc.as_of_date > latest[key].as_of_date:
            latest[key] = acc

    snapshots = list(latest.values())

    return Response(_build_dashboard_summary(snapshots, is_gp=False))


def _build_dashboard_summary(snapshots, is_gp=False):
    """Build the dashboard summary dict from capital account snapshots."""
    total_committed = sum(s.committed_capital for s in snapshots)
    total_called = sum(s.called_capital for s in snapshots)
    total_uncalled = sum(s.uncalled_capital for s in snapshots)
    total_distributed = sum(s.distributed_capital for s in snapshots)
    total_unrealized = sum(s.unrealized_value for s in snapshots)
    total_value = sum(s.total_value for s in snapshots)

    # Weighted averages for ratios
    tvpi = float(total_value / total_called) if total_called else None
    dpi = float(total_distributed / total_called) if total_called else None
    rvpi = float(total_unrealized / total_called) if total_called else None
    moic = tvpi  # MOIC and TVPI are equivalent at aggregate level

    # IRR: use weighted average of individual IRRs
    irr_values = [(s.irr, s.called_capital) for s in snapshots if s.irr is not None]
    if irr_values and total_called:
        weighted_irr = sum(float(irr) * float(cap) for irr, cap in irr_values) / float(total_called)
    else:
        weighted_irr = None

    summary = {
        'total_committed': str(total_committed),
        'total_called': str(total_called),
        'total_uncalled': str(total_uncalled),
        'total_distributed': str(total_distributed),
        'total_unrealized': str(total_unrealized),
        'total_value': str(total_value),
        'irr': round(weighted_irr, 4) if weighted_irr is not None else None,
        'tvpi': round(tvpi, 4) if tvpi is not None else None,
        'dpi': round(dpi, 4) if dpi is not None else None,
        'rvpi': round(rvpi, 4) if rvpi is not None else None,
        'moic': round(moic, 4) if moic is not None else None,
        'commitment_count': len(snapshots),
        'is_gp_view': is_gp,
    }

    # Per-scheme breakdown
    scheme_map = {}
    for s in snapshots:
        scheme_name = s.commitment.scheme.name
        if scheme_name not in scheme_map:
            scheme_map[scheme_name] = {
                'scheme_name': scheme_name,
                'committed': Decimal('0'), 'called': Decimal('0'),
                'distributed': Decimal('0'), 'unrealized': Decimal('0'),
                'total_value': Decimal('0'),
            }
        sm = scheme_map[scheme_name]
        sm['committed'] += s.committed_capital
        sm['called'] += s.called_capital
        sm['distributed'] += s.distributed_capital
        sm['unrealized'] += s.unrealized_value
        sm['total_value'] += s.total_value

    summary['schemes'] = [
        {k: str(v) if isinstance(v, Decimal) else v for k, v in sm.items()}
        for sm in scheme_map.values()
    ]

    return summary


# -- Waterfall Simulator --

@api_view(['POST'])
@permission_classes([IsGPUser])
def waterfall_simulate(request, scheme_id):
    """
    Run a waterfall (carried interest) simulation for a scheme.
    Accepts: {
        "total_distributions": 200000000,
        "called_capital": 100000000,
        "hurdle_rate": 8.0,
        "carry_pct": 20.0,
        "tenure_years": 7
    }
    Returns European waterfall breakdown.
    """
    from funds.models import Scheme
    org = request.organization
    try:
        scheme = Scheme.objects.select_related('fund').get(
            pk=scheme_id, fund__organization=org,
        )
    except Scheme.DoesNotExist:
        return Response({'detail': 'Scheme not found.'}, status=404)

    if not user_has_fund_access(request.user, scheme.fund):
        return Response({'detail': 'Scheme not found.'}, status=404)

    total_dist = Decimal(str(request.data.get('total_distributions', 0)))
    called_cap = Decimal(str(request.data.get('called_capital', 0)))
    hurdle_rate = float(request.data.get('hurdle_rate', getattr(scheme, 'hurdle_rate_pct', 8) or 8))
    carry_pct = float(request.data.get('carry_pct', getattr(scheme, 'carry_pct', 20) or 20))
    tenure_years = int(request.data.get('tenure_years', 7))

    if called_cap <= 0:
        return Response(
            {'detail': 'called_capital must be > 0.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # European waterfall calculation
    hurdle_decimal = Decimal(str(hurdle_rate / 100))
    carry_decimal = Decimal(str(carry_pct / 100))

    # Preferred return (compound)
    pref_return = called_cap * (Decimal(str((1 + hurdle_rate / 100) ** tenure_years)) - 1)
    hurdle_amount = called_cap + pref_return

    # Carry base = distributions above hurdle
    carry_base = max(Decimal('0'), total_dist - hurdle_amount)
    gp_carry = carry_base * carry_decimal
    lp_total = total_dist - gp_carry

    fund_moic = float(total_dist / called_cap) if called_cap else 0
    lp_moic = float(lp_total / called_cap) if called_cap else 0

    steps = [
        {
            'name': 'Return of Capital',
            'description': 'Called capital returned to LPs first',
            'amount': str(min(total_dist, called_cap)),
            'recipient': 'LP',
        },
        {
            'name': 'Preferred Return',
            'description': f'{hurdle_rate}% compounded over {tenure_years} years',
            'amount': str(min(max(Decimal('0'), total_dist - called_cap), pref_return)),
            'recipient': 'LP',
        },
        {
            'name': 'Carried Interest (GP)',
            'description': f'{carry_pct}% of profits above hurdle',
            'amount': str(gp_carry),
            'recipient': 'GP',
        },
        {
            'name': 'Remaining to LPs',
            'description': 'LP share of profits above hurdle',
            'amount': str(carry_base - gp_carry),
            'recipient': 'LP',
        },
    ]

    return Response({
        'scheme': scheme.name,
        'inputs': {
            'total_distributions': str(total_dist),
            'called_capital': str(called_cap),
            'hurdle_rate': hurdle_rate,
            'carry_pct': carry_pct,
            'tenure_years': tenure_years,
        },
        'results': {
            'preferred_return': str(pref_return),
            'carry_base': str(carry_base),
            'gp_carry': str(gp_carry),
            'lp_total': str(lp_total),
            'fund_moic': round(fund_moic, 4),
            'lp_moic': round(lp_moic, 4),
        },
        'steps': steps,
    })
