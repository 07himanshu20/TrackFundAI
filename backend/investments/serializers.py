from rest_framework import serializers
from .models import (
    PortfolioCompany, Investment, InvestmentTranche, Valuation,
    KPIDefinition, PortfolioKPI, CompanyFinancials, ExitEvent, BoardMeeting,
)


# ── Portfolio Company ──────────────────────────────────────────

class PortfolioCompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = PortfolioCompany
        fields = [
            'id', 'organization', 'name', 'cin', 'pan',
            'sector', 'sub_sector',
            'incorporation_date', 'headquarters_city', 'headquarters_country',
            'website', 'founder_names', 'co_investors', 'description',
            'is_active', 'portfolio_node_id',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


class PortfolioCompanyListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for dropdowns and portfolio lists.

    Universal aggregates exposed (computed from Investment + ExitEvent rows
    that already have per-investment IRR/MOIC populated by Phase 4):
      • total_cost  — Σ Investment.total_invested in current fund context
      • total_fv    — Σ (exit proceeds for exited + estimated FV for active)
      • aggregate_irr_pct — XIRR over the combined cash-flow stream of all
                           investments this company has in current fund context
      • aggregate_moic   — total_fv / total_cost

    Context `fund_id` (str, optional) restricts the aggregation to investments
    in that single fund. Without context, aggregates span all funds the
    company is in (works for org-wide views too).
    """
    total_cost = serializers.SerializerMethodField()
    total_fv = serializers.SerializerMethodField()
    aggregate_irr_pct = serializers.SerializerMethodField()
    aggregate_moic = serializers.SerializerMethodField()

    class Meta:
        model = PortfolioCompany
        fields = ['id', 'name', 'sector', 'sub_sector',
                  'headquarters_city', 'headquarters_country',
                  'co_investors',
                  'is_active', 'is_quoted', 'listing_exchange',
                  'total_cost', 'total_fv',
                  'aggregate_irr_pct', 'aggregate_moic']

    # ---- Aggregation helpers (universal — no fund-specific logic) ----

    def _company_invs(self, obj):
        """Investments for this company, restricted to context['fund_id'] if set."""
        qs = obj.investments.all()
        fund_id = (self.context or {}).get('fund_id')
        if fund_id:
            qs = qs.filter(scheme__fund_id=fund_id)
        return qs

    def get_total_cost(self, obj):
        from django.db.models import Sum
        v = self._company_invs(obj).aggregate(s=Sum('total_invested'))['s']
        return float(v) if v is not None else None

    def get_total_fv(self, obj):
        """Sum of (exit proceeds for exited) + (cost × fund_markup proxy for active).
        Per-investment FV requires Valuation rows; when those exist, the Phase 4
        derivation already used them. Here we approximate the company-level FV
        the same way Phase 4 did, so dashboard numbers reconcile.
        """
        from decimal import Decimal
        from investments.models import ExitEvent
        total = Decimal('0')
        any_value = False
        for inv in self._company_invs(obj).prefetch_related('exit_scenarios'):
            exits = ExitEvent.objects.filter(investment=inv)
            if exits.exists():
                for e in exits:
                    p = e.net_exit_proceeds or e.proceeds
                    if p is not None:
                        total += Decimal(str(p))
                        any_value = True
            elif inv.moic is not None and inv.total_invested is not None:
                total += Decimal(str(inv.moic)) * Decimal(str(inv.total_invested))
                any_value = True
        return float(total) if any_value else None

    def get_aggregate_irr_pct(self, obj):
        """Compute company-level IRR by combining cash flows across this
        company's investments. Pure-Python XIRR (same algorithm as Phase 4).
        """
        from decimal import Decimal
        from investments.models import ExitEvent
        from dataimport.phase4_derivations import _xirr, _latest_fund_markup, _estimate_current_fv
        from datetime import date as _date
        invs = list(self._company_invs(obj).prefetch_related('tranches'))
        if not invs:
            return None
        # Need a fund context to compute markup proxy for active investments
        fund = invs[0].scheme.fund
        markup = _latest_fund_markup(fund)
        cashflows = []
        today = _date.today()
        for inv in invs:
            for t in inv.tranches.all():
                if t.amount is not None and t.date is not None:
                    cashflows.append((t.date, -Decimal(str(t.amount))))
            exits = ExitEvent.objects.filter(investment=inv)
            had_exit = False
            for e in exits:
                amt = e.net_exit_proceeds or e.proceeds
                if e.exit_date and amt:
                    cashflows.append((e.exit_date, Decimal(str(amt))))
                    had_exit = True
            if not had_exit:
                fv = _estimate_current_fv(inv, markup)
                if fv:
                    cashflows.append((today, fv))
        irr = _xirr(cashflows)
        return float(irr) if irr is not None else None

    def get_aggregate_moic(self, obj):
        cost = self.get_total_cost(obj)
        fv = self.get_total_fv(obj)
        if cost and cost > 0 and fv is not None:
            return round(fv / cost, 4)
        return None


# ── Investment & Tranche ─────���───────────────────────────────

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
    portfolio_company_name = serializers.CharField(
        source='portfolio_company.name', read_only=True, default=None,
    )

    class Meta:
        model = Investment
        fields = [
            'id', 'scheme', 'company_name',
            'portfolio_company', 'portfolio_company_name',
            'portfolio_node_id',
            'instrument_type', 'instrument_type_display',
            'ownership_pct', 'percentage_stake_fully_diluted',
            'exceeds_10pct_threshold', 'threshold_breach_date',
            'total_invested', 'investment_date',
            'currency', 'status', 'status_display',
            'sector', 'stage', 'irr_pct', 'moic',
            'board_seat', 'is_lead_investor',
            'tranche_count', 'latest_valuation',
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
    portfolio_company_detail = PortfolioCompanyListSerializer(
        source='portfolio_company', read_only=True,
    )

    class Meta:
        model = Investment
        fields = [
            'id', 'scheme', 'company_name',
            'portfolio_company', 'portfolio_company_detail',
            'portfolio_node_id',
            'instrument_type', 'instrument_type_display',
            'ownership_pct', 'percentage_stake_fully_diluted',
            'exceeds_10pct_threshold', 'threshold_breach_date',
            'total_invested', 'investment_date',
            'currency', 'status', 'status_display',
            'sector', 'stage', 'irr_pct', 'moic',
            'description', 'board_seat', 'is_lead_investor',
            'write_off_date',
            'tranches',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'scheme', 'created_by', 'created_at', 'updated_at']


class InvestmentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Investment
        fields = [
            'company_name', 'portfolio_company', 'portfolio_node_id',
            'instrument_type', 'ownership_pct', 'percentage_stake_fully_diluted',
            'total_invested', 'investment_date',
            'currency', 'status', 'sector', 'description',
            'board_seat', 'is_lead_investor', 'write_off_date',
        ]


# ── Valuation ─────────���──────────────────────────────────────

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
            'methodology_display',
            'fair_value', 'fair_value_of_holding', 'enterprise_value',
            'cost_basis', 'unrealized_gain_loss', 'multiple',
            'fvtpl_movement',
            'discount_rate', 'comparable_companies', 'assumptions',
            'valuer_name', 'valuer_reg_number',
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
            'valuation_date', 'methodology',
            'fair_value', 'fair_value_of_holding', 'enterprise_value',
            'cost_basis', 'unrealized_gain_loss', 'multiple',
            'fvtpl_movement',
            'discount_rate', 'comparable_companies', 'assumptions',
            'valuer_name', 'valuer_reg_number',
        ]


# ── KPI Definition ─────��─────────────────────────────────────

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
    source_display = serializers.CharField(
        source='get_source_display', read_only=True,
    )

    class Meta:
        model = PortfolioKPI
        fields = [
            'id', 'investment', 'portfolio_company',
            'kpi_definition', 'kpi_name',
            'period', 'period_end_date', 'value', 'notes',
            'source', 'source_display',
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


# ── Exit Event ─────────��─────────────────────────────────────

class ExitEventSerializer(serializers.ModelSerializer):
    exit_type_display = serializers.CharField(
        source='get_exit_type_display', read_only=True,
    )
    gain_loss_nature_display = serializers.CharField(
        source='get_gain_loss_nature_display', read_only=True,
    )

    class Meta:
        model = ExitEvent
        fields = [
            'id', 'investment', 'exit_type', 'exit_type_display',
            'is_actual', 'exit_date', 'exit_valuation',
            'proceeds', 'net_exit_proceeds', 'realized_gain_loss',
            'gain_loss_nature', 'gain_loss_nature_display',
            'moic', 'exit_multiple', 'irr_pct', 'irr_on_exit',
            'buyer_name', 'assumptions',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'investment', 'created_by', 'created_at', 'updated_at']


class ExitEventCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExitEvent
        fields = [
            'exit_type', 'is_actual', 'exit_date', 'exit_valuation',
            'proceeds', 'net_exit_proceeds', 'realized_gain_loss',
            'gain_loss_nature',
            'moic', 'exit_multiple', 'irr_pct', 'irr_on_exit',
            'buyer_name', 'assumptions',
        ]


# ── Company Financials (Burn & Runway) ───────────────────────

class CompanyFinancialsSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='investment.company_name', read_only=True)

    class Meta:
        model = CompanyFinancials
        fields = [
            'id', 'investment', 'portfolio_company', 'company_name',
            'period', 'gross_burn', 'net_burn', 'cash_balance', 'runway_months',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# ── Board Meeting ──────────────────────────────────────────────

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
