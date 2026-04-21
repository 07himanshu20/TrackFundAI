from rest_framework import serializers
from .models import (
    Investment, InvestmentTranche, Valuation,
    KPIDefinition, PortfolioKPI, ExitEvent, BoardMeeting,
)


# ── Investment & Tranche ─────────────────────────────────────

class InvestmentTrancheSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvestmentTranche
        fields = [
            'id', 'investment', 'tranche_number', 'amount', 'date',
            'shares_acquired', 'price_per_share',
            'pre_money_valuation', 'post_money_valuation',
            'round_name', 'notes', 'created_at',
        ]
        read_only_fields = ['id', 'investment', 'created_at']


class InvestmentListSerializer(serializers.ModelSerializer):
    instrument_type_display = serializers.CharField(
        source='get_instrument_type_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_status_display', read_only=True,
    )
    tranche_count = serializers.IntegerField(read_only=True)
    latest_valuation = serializers.DecimalField(
        max_digits=18, decimal_places=2, read_only=True,
    )

    class Meta:
        model = Investment
        fields = [
            'id', 'scheme', 'company_name', 'portfolio_node_id',
            'instrument_type', 'instrument_type_display',
            'ownership_pct', 'total_invested', 'investment_date',
            'currency', 'status', 'status_display',
            'sector', 'board_seat', 'tranche_count', 'latest_valuation',
            'created_at',
        ]


class InvestmentDetailSerializer(serializers.ModelSerializer):
    instrument_type_display = serializers.CharField(
        source='get_instrument_type_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_status_display', read_only=True,
    )
    tranches = InvestmentTrancheSerializer(many=True, read_only=True)

    class Meta:
        model = Investment
        fields = [
            'id', 'scheme', 'company_name', 'portfolio_node_id',
            'instrument_type', 'instrument_type_display',
            'ownership_pct', 'total_invested', 'investment_date',
            'currency', 'status', 'status_display',
            'sector', 'description', 'board_seat',
            'tranches',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'scheme', 'created_by', 'created_at', 'updated_at']


class InvestmentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Investment
        fields = [
            'company_name', 'portfolio_node_id', 'instrument_type',
            'ownership_pct', 'total_invested', 'investment_date',
            'currency', 'status', 'sector', 'description', 'board_seat',
        ]


# ── Valuation ────────────────────────────────────────────────

class ValuationSerializer(serializers.ModelSerializer):
    methodology_display = serializers.CharField(
        source='get_methodology_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_status_display', read_only=True,
    )

    class Meta:
        model = Valuation
        fields = [
            'id', 'investment', 'valuation_date', 'methodology',
            'methodology_display', 'fair_value', 'cost_basis',
            'unrealized_gain_loss', 'multiple', 'discount_rate',
            'comparable_companies', 'assumptions',
            'status', 'status_display',
            'submitted_by', 'approved_by', 'approved_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'investment', 'submitted_by', 'approved_by',
            'approved_at', 'created_at', 'updated_at',
        ]


class ValuationCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Valuation
        fields = [
            'valuation_date', 'methodology', 'fair_value', 'cost_basis',
            'unrealized_gain_loss', 'multiple', 'discount_rate',
            'comparable_companies', 'assumptions',
        ]


# ── KPI Definition ───────────────────────────────────────────

class KPIDefinitionSerializer(serializers.ModelSerializer):
    format_display = serializers.CharField(
        source='get_format_display', read_only=True,
    )
    frequency_display = serializers.CharField(
        source='get_frequency_display', read_only=True,
    )

    class Meta:
        model = KPIDefinition
        fields = [
            'id', 'name', 'slug', 'description',
            'format', 'format_display',
            'frequency', 'frequency_display',
            'is_required', 'sort_order', 'is_active',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


# ── Portfolio KPI (Founder submissions) ──────────────────────

class PortfolioKPISerializer(serializers.ModelSerializer):
    kpi_name = serializers.CharField(source='kpi_definition.name', read_only=True)
    status_display = serializers.CharField(
        source='get_status_display', read_only=True,
    )

    class Meta:
        model = PortfolioKPI
        fields = [
            'id', 'investment', 'kpi_definition', 'kpi_name',
            'period', 'value', 'notes',
            'status', 'status_display',
            'submitted_by', 'reviewed_by',
            'submitted_at', 'reviewed_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'investment', 'submitted_by', 'reviewed_by',
            'submitted_at', 'reviewed_at', 'created_at', 'updated_at',
        ]


class KPISubmitSerializer(serializers.Serializer):
    """Bulk KPI submission — founder submits multiple KPIs for a single period."""
    period = serializers.DateField()
    values = serializers.ListField(
        child=serializers.DictField(),
        help_text='List of {kpi_definition_id, value, notes?}',
    )


# ── Exit Event ───────────────────────────────────────────────

class ExitEventSerializer(serializers.ModelSerializer):
    exit_type_display = serializers.CharField(
        source='get_exit_type_display', read_only=True,
    )

    class Meta:
        model = ExitEvent
        fields = [
            'id', 'investment', 'exit_type', 'exit_type_display',
            'is_actual', 'exit_date', 'exit_valuation',
            'proceeds', 'realized_gain_loss', 'moic', 'irr_pct',
            'buyer_name', 'assumptions',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'investment', 'created_by', 'created_at', 'updated_at']


class ExitEventCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExitEvent
        fields = [
            'exit_type', 'is_actual', 'exit_date', 'exit_valuation',
            'proceeds', 'realized_gain_loss', 'moic', 'irr_pct',
            'buyer_name', 'assumptions',
        ]


# ── Board Meeting ────────────────────────────────────────────

class BoardMeetingSerializer(serializers.ModelSerializer):
    class Meta:
        model = BoardMeeting
        fields = [
            'id', 'investment', 'meeting_date', 'meeting_number',
            'agenda', 'minutes', 'attendees', 'resolutions',
            'next_meeting_date', 'document',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'investment', 'created_by', 'created_at', 'updated_at']
