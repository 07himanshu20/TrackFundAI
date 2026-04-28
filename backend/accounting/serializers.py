from rest_framework import serializers
from .models import (
    ChartOfAccounts, NAVRecord, CarriedInterest,
    FundLedger, ManagementFeeSchedule,
)


# -- Chart of Accounts --

class ChartOfAccountsSerializer(serializers.ModelSerializer):
    account_type_display = serializers.CharField(
        source='get_account_type_display', read_only=True,
    )
    parent_account_name = serializers.CharField(
        source='parent_account.account_name', read_only=True, default=None,
    )

    class Meta:
        model = ChartOfAccounts
        fields = [
            'id', 'organization', 'account_code', 'account_name',
            'account_type', 'account_type_display',
            'parent_account', 'parent_account_name',
            'description', 'is_active',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


# -- NAV Record --

class NAVRecordListSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )

    class Meta:
        model = NAVRecord
        fields = [
            'id', 'scheme', 'scheme_name', 'nav_date',
            'total_nav', 'total_units_outstanding', 'nav_per_unit',
            'depository_reconciled', 'depository_type',
            'created_at',
        ]


class NAVRecordDetailSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )

    class Meta:
        model = NAVRecord
        fields = [
            'id', 'scheme', 'scheme_name', 'nav_date',
            'total_nav', 'total_units_outstanding', 'nav_per_unit',
            'investments_at_fair_value', 'cash_and_equivalents',
            'receivables', 'management_fee_payable', 'other_liabilities',
            'depository_type', 'depository_reconciled',
            'depository_variance_amount',
            'approved_by', 'approved_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# -- Carried Interest --

class CarriedInterestSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_calculation_status_display', read_only=True,
    )

    class Meta:
        model = CarriedInterest
        fields = [
            'id', 'scheme', 'scheme_name', 'calculation_date',
            'total_distributions', 'total_called_capital',
            'preferred_return_amount', 'carry_base',
            'carry_amount_gross', 'carry_amount_net',
            'gp_clawback_provision',
            'calculation_status', 'status_display', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# -- Fund Ledger --

class FundLedgerSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    debit_account_name = serializers.CharField(
        source='debit_account.account_name', read_only=True,
    )
    credit_account_name = serializers.CharField(
        source='credit_account.account_name', read_only=True,
    )
    reference_type_display = serializers.CharField(
        source='get_reference_type_display', read_only=True,
    )

    class Meta:
        model = FundLedger
        fields = [
            'id', 'scheme', 'scheme_name',
            'journal_entry_number', 'entry_date', 'description',
            'debit_account', 'debit_account_name',
            'credit_account', 'credit_account_name',
            'amount',
            'reference_type', 'reference_type_display', 'reference_id',
            'posted_by', 'is_reversed',
            'created_at',
        ]
        read_only_fields = ['id', 'posted_by', 'created_at']


# -- Management Fee Schedule --

class ManagementFeeScheduleSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_fee_status_display', read_only=True,
    )

    class Meta:
        model = ManagementFeeSchedule
        fields = [
            'id', 'scheme', 'scheme_name',
            'period_start', 'period_end',
            'fee_basis_amount', 'fee_rate',
            'fee_amount', 'gst_amount', 'total_fee_with_gst',
            'fee_status', 'status_display',
            'invoice_number', 'invoice_date',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
