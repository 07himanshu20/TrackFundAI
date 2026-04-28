from rest_framework import serializers
from .models import (
    BankAccount, Investor, Commitment, CapitalCall,
    CapitalCallLineItem, Distribution, DistributionLineItem,
    LPCapitalAccount,
)


# -- Bank Account --

class BankAccountSerializer(serializers.ModelSerializer):
    account_type_display = serializers.CharField(
        source='get_account_type_display', read_only=True,
    )

    class Meta:
        model = BankAccount
        fields = [
            'id', 'organization', 'account_holder_name', 'bank_name',
            'branch_name', 'account_number', 'ifsc_code', 'swift_code',
            'account_type', 'account_type_display', 'is_primary',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


# -- Investor --

class InvestorListSerializer(serializers.ModelSerializer):
    investor_type_display = serializers.CharField(
        source='get_investor_type_display', read_only=True,
    )
    kyc_status_display = serializers.CharField(
        source='get_kyc_status_display', read_only=True,
    )

    class Meta:
        model = Investor
        fields = [
            'id', 'investor_name', 'investor_type', 'investor_type_display',
            'email', 'phone', 'pan', 'kyc_status', 'kyc_status_display',
            'is_accredited_investor', 'is_land_border_country',
            'is_politically_exposed', 'is_active',
        ]


class InvestorDetailSerializer(serializers.ModelSerializer):
    investor_type_display = serializers.CharField(
        source='get_investor_type_display', read_only=True,
    )
    kyc_status_display = serializers.CharField(
        source='get_kyc_status_display', read_only=True,
    )
    fatca_status_display = serializers.CharField(
        source='get_fatca_status_display', read_only=True,
    )
    primary_bank_account_detail = BankAccountSerializer(
        source='primary_bank_account', read_only=True,
    )

    class Meta:
        model = Investor
        fields = [
            'id', 'organization', 'investor_name',
            'investor_type', 'investor_type_display',
            'contact_person', 'email', 'phone',
            'address', 'city', 'state', 'country',
            'pan', 'aadhaar_last_4', 'ckyc_number',
            'kyc_status', 'kyc_status_display',
            'kyc_completed_date', 'kyc_expiry_date',
            'is_accredited_investor', 'accreditation_date',
            'is_land_border_country', 'land_border_country_name',
            'is_politically_exposed',
            'fatca_status', 'fatca_status_display',
            'primary_bank_account', 'primary_bank_account_detail',
            'portal_user', 'is_active',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class InvestorCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Investor
        fields = [
            'investor_name', 'investor_type', 'contact_person',
            'email', 'phone', 'address', 'city', 'state', 'country',
            'pan', 'aadhaar_last_4', 'ckyc_number',
            'kyc_status', 'kyc_completed_date', 'kyc_expiry_date',
            'is_accredited_investor', 'accreditation_date',
            'is_land_border_country', 'land_border_country_name',
            'is_politically_exposed', 'fatca_status',
            'primary_bank_account', 'portal_user',
        ]


# -- Commitment --

class CommitmentSerializer(serializers.ModelSerializer):
    investor_name = serializers.CharField(
        source='investor.investor_name', read_only=True,
    )
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    close_type_display = serializers.CharField(
        source='get_close_type_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_commitment_status_display', read_only=True,
    )

    class Meta:
        model = Commitment
        fields = [
            'id', 'investor', 'investor_name', 'scheme', 'scheme_name',
            'commitment_amount', 'commitment_date',
            'close_type', 'close_type_display',
            'units_allocated', 'side_letter_exists',
            'subscription_form_url',
            'commitment_status', 'status_display',
            'primary_bank_account',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# -- Capital Call --

class CapitalCallLineItemSerializer(serializers.ModelSerializer):
    investor_name = serializers.CharField(
        source='commitment.investor.investor_name', read_only=True,
    )
    payment_status_display = serializers.CharField(
        source='get_payment_status_display', read_only=True,
    )

    class Meta:
        model = CapitalCallLineItem
        fields = [
            'id', 'capital_call', 'commitment', 'investor_name',
            'called_amount', 'cumulative_called_pct', 'units_allotted',
            'payment_status', 'payment_status_display',
            'amount_received', 'payment_date', 'utr_number',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CapitalCallListSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_call_status_display', read_only=True,
    )

    class Meta:
        model = CapitalCall
        fields = [
            'id', 'scheme', 'scheme_name', 'call_number',
            'call_date', 'payment_due_date', 'call_percentage',
            'total_call_amount', 'call_status', 'status_display',
            'created_at',
        ]


class CapitalCallDetailSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_call_status_display', read_only=True,
    )
    line_items = CapitalCallLineItemSerializer(many=True, read_only=True)

    class Meta:
        model = CapitalCall
        fields = [
            'id', 'scheme', 'scheme_name', 'call_number',
            'call_date', 'payment_due_date', 'call_percentage',
            'total_call_amount', 'purpose',
            'call_status', 'status_display',
            'line_items',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']


# -- Distribution --

class DistributionLineItemSerializer(serializers.ModelSerializer):
    investor_name = serializers.CharField(
        source='commitment.investor.investor_name', read_only=True,
    )

    class Meta:
        model = DistributionLineItem
        fields = [
            'id', 'distribution', 'commitment', 'investor_name',
            'gross_amount', 'tds_rate', 'tds_amount', 'net_amount',
            'units_redeemed', 'payment_date', 'utr_number',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class DistributionListSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    type_display = serializers.CharField(
        source='get_distribution_type_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_distribution_status_display', read_only=True,
    )

    class Meta:
        model = Distribution
        fields = [
            'id', 'scheme', 'scheme_name', 'distribution_number',
            'distribution_date', 'distribution_type', 'type_display',
            'total_gross_amount', 'total_tds_amount', 'total_net_amount',
            'distribution_status', 'status_display',
            'created_at',
        ]


class DistributionDetailSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    type_display = serializers.CharField(
        source='get_distribution_type_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_distribution_status_display', read_only=True,
    )
    line_items = DistributionLineItemSerializer(many=True, read_only=True)

    class Meta:
        model = Distribution
        fields = [
            'id', 'scheme', 'scheme_name', 'distribution_number',
            'distribution_date', 'distribution_type', 'type_display',
            'total_gross_amount', 'total_tds_amount', 'total_net_amount',
            'related_exit_event', 'distribution_status', 'status_display',
            'notes', 'line_items',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']


# -- LP Capital Account --

class LPCapitalAccountSerializer(serializers.ModelSerializer):
    investor_name = serializers.CharField(
        source='commitment.investor.investor_name', read_only=True,
    )
    scheme_name = serializers.CharField(
        source='commitment.scheme.name', read_only=True,
    )

    class Meta:
        model = LPCapitalAccount
        fields = [
            'id', 'commitment', 'investor_name', 'scheme_name',
            'as_of_date',
            'committed_capital', 'called_capital', 'uncalled_capital',
            'distributed_capital', 'unrealized_value', 'total_value',
            'irr', 'tvpi', 'dpi', 'rvpi', 'moic',
            'units_held',
            'management_fee_charged', 'carried_interest_charged',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
