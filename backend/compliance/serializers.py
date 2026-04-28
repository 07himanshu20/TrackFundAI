from rest_framework import serializers
from .models import (
    SEBIReport, AMLDueDiligence, ComplianceTestReport,
    CTRChecklistItem, EquityThresholdAlert, ComplianceCalendar,
    PPMAmendment, SEBICircular, CircularAction,
)


# -- SEBI Report --

class SEBIReportListSerializer(serializers.ModelSerializer):
    fund_name = serializers.CharField(
        source='fund.name', read_only=True,
    )
    report_type_display = serializers.CharField(
        source='get_report_type_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_filing_status_display', read_only=True,
    )

    class Meta:
        model = SEBIReport
        fields = [
            'id', 'fund', 'fund_name', 'scheme',
            'report_type', 'report_type_display',
            'reporting_period_start', 'reporting_period_end',
            'due_date', 'filing_status', 'status_display',
            'filed_date', 'nav_reconciled_with_depository',
            'created_at',
        ]


class SEBIReportDetailSerializer(serializers.ModelSerializer):
    fund_name = serializers.CharField(
        source='fund.name', read_only=True,
    )
    report_type_display = serializers.CharField(
        source='get_report_type_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_filing_status_display', read_only=True,
    )

    class Meta:
        model = SEBIReport
        fields = [
            'id', 'fund', 'fund_name', 'scheme',
            'report_type', 'report_type_display',
            'reporting_period_start', 'reporting_period_end',
            'due_date', 'filing_status', 'status_display',
            'filed_date', 'si_portal_reference_number',
            'report_data', 'ivca_format_version',
            'nav_reconciled_with_depository',
            'prepared_by', 'reviewed_by',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# -- AML Due Diligence --

class AMLDueDiligenceSerializer(serializers.ModelSerializer):
    investor_name = serializers.CharField(
        source='investor.investor_name', read_only=True,
    )
    risk_rating_display = serializers.CharField(
        source='get_risk_rating_display', read_only=True,
    )

    class Meta:
        model = AMLDueDiligence
        fields = [
            'id', 'investor', 'investor_name',
            'is_land_border_country_investor', 'exceeds_50pct_threshold',
            'beneficial_owner_details', 'beneficial_owner_identified',
            'risk_rating', 'risk_rating_display',
            'risk_assessment_date', 'risk_notes',
            'custodian_reported', 'custodian_report_date',
            'str_filed', 'str_reference',
            'assessed_by',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# -- Compliance Test Report --

class CTRChecklistItemSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(
        source='get_compliance_status_display', read_only=True,
    )

    class Meta:
        model = CTRChecklistItem
        fields = [
            'id', 'compliance_test_report', 'check_number',
            'regulation_reference', 'description',
            'compliance_status', 'status_display',
            'evidence', 'remarks',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ComplianceTestReportListSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    compliance_display = serializers.CharField(
        source='get_overall_compliance_status_display', read_only=True,
    )
    report_status_display = serializers.CharField(
        source='get_report_status_display', read_only=True,
    )

    class Meta:
        model = ComplianceTestReport
        fields = [
            'id', 'scheme', 'scheme_name', 'financial_year',
            'overall_compliance_status', 'compliance_display',
            'report_status', 'report_status_display',
            'submitted_to_trustee_at',
            'created_at',
        ]


class ComplianceTestReportDetailSerializer(serializers.ModelSerializer):
    scheme_name = serializers.CharField(
        source='scheme.name', read_only=True,
    )
    compliance_display = serializers.CharField(
        source='get_overall_compliance_status_display', read_only=True,
    )
    report_status_display = serializers.CharField(
        source='get_report_status_display', read_only=True,
    )
    checklist_items = CTRChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = ComplianceTestReport
        fields = [
            'id', 'scheme', 'scheme_name', 'financial_year',
            'overall_compliance_status', 'compliance_display',
            'report_status', 'report_status_display',
            'submitted_to_trustee_at', 'trustee_acknowledged_at',
            'observations', 'remediation_plan',
            'prepared_by', 'checklist_items',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# -- Equity Threshold Alert --

class EquityThresholdAlertSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(
        source='investment.company_name', read_only=True,
    )

    class Meta:
        model = EquityThresholdAlert
        fields = [
            'id', 'investment', 'company_name',
            'threshold_breached', 'breach_date', 'stake_percentage',
            'custodian_notification_deadline',
            'custodian_notified', 'custodian_notified_date',
            'custodian_reference', 'resolved',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# -- Compliance Calendar --

class ComplianceCalendarSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(
        source='get_compliance_type_display', read_only=True,
    )
    recurrence_display = serializers.CharField(
        source='get_recurrence_display', read_only=True,
    )
    status_display = serializers.CharField(
        source='get_status_display', read_only=True,
    )
    assigned_to_name = serializers.CharField(
        source='assigned_to.username', read_only=True, default=None,
    )

    class Meta:
        model = ComplianceCalendar
        fields = [
            'id', 'organization', 'fund', 'scheme',
            'compliance_type', 'type_display',
            'title', 'description',
            'due_date', 'recurrence', 'recurrence_display',
            'advance_reminder_days',
            'status', 'status_display', 'completed_date',
            'assigned_to', 'assigned_to_name', 'notes',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'created_at', 'updated_at']


# -- PPM Amendment --

class PPMAmendmentListSerializer(serializers.ModelSerializer):
    fund_name = serializers.CharField(source='fund.name', read_only=True)
    scheme_name = serializers.CharField(source='scheme.name', read_only=True, default=None)
    amendment_type_display = serializers.CharField(source='get_amendment_type_display', read_only=True)
    status_display = serializers.CharField(source='get_approval_status_display', read_only=True)

    class Meta:
        model = PPMAmendment
        fields = [
            'id', 'fund', 'fund_name', 'scheme', 'scheme_name',
            'amendment_number', 'amendment_type', 'amendment_type_display',
            'title', 'approval_status', 'status_display',
            'sebi_filing_date', 'effective_date',
            'investor_notification_date', 'investor_exit_window_expiry',
            'created_at',
        ]


class PPMAmendmentDetailSerializer(serializers.ModelSerializer):
    fund_name = serializers.CharField(source='fund.name', read_only=True)
    scheme_name = serializers.CharField(source='scheme.name', read_only=True, default=None)
    amendment_type_display = serializers.CharField(source='get_amendment_type_display', read_only=True)
    status_display = serializers.CharField(source='get_approval_status_display', read_only=True)

    class Meta:
        model = PPMAmendment
        fields = [
            'id', 'fund', 'fund_name', 'scheme', 'scheme_name',
            'amendment_number', 'amendment_type', 'amendment_type_display',
            'title', 'description',
            'board_approval_date', 'trustee_approval_date',
            'sebi_filing_date', 'investor_notification_date',
            'effective_date',
            'investor_exit_window_days', 'investor_exit_window_expiry',
            'approval_status', 'status_display',
            'sebi_acknowledgement_number', 'document_url',
            'notes', 'prepared_by',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# -- SEBI Circular --

class CircularActionSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    priority_display = serializers.CharField(source='get_priority_display', read_only=True)
    assigned_to_name = serializers.CharField(
        source='assigned_to.username', read_only=True, default=None,
    )
    fund_name = serializers.CharField(source='fund.name', read_only=True, default=None)

    class Meta:
        model = CircularAction
        fields = [
            'id', 'circular',
            'fund', 'fund_name',
            'action_title', 'action_description',
            'priority', 'priority_display',
            'due_date', 'status', 'status_display',
            'completion_date', 'completion_notes',
            'deferred_reason',
            'assigned_to', 'assigned_to_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'circular', 'created_at', 'updated_at']


class SEBICircularListSerializer(serializers.ModelSerializer):
    impact_display = serializers.CharField(source='get_impact_level_display', read_only=True)
    applicability_display = serializers.CharField(source='get_applicability_display', read_only=True)
    pending_actions_count = serializers.SerializerMethodField()

    class Meta:
        model = SEBICircular
        fields = [
            'id', 'circular_number', 'circular_date', 'title',
            'applicability', 'applicability_display',
            'impact_level', 'impact_display',
            'compliance_deadline', 'ai_parsed', 'is_superseded',
            'pending_actions_count',
            'created_at',
        ]

    def get_pending_actions_count(self, obj):
        return obj.actions.filter(status__in=['pending', 'in_progress']).count()


class SEBICircularDetailSerializer(serializers.ModelSerializer):
    impact_display = serializers.CharField(source='get_impact_level_display', read_only=True)
    applicability_display = serializers.CharField(source='get_applicability_display', read_only=True)
    actions = CircularActionSerializer(many=True, read_only=True)

    class Meta:
        model = SEBICircular
        fields = [
            'id', 'circular_number', 'circular_date', 'title', 'summary',
            'applicability', 'applicability_display',
            'impact_level', 'impact_display',
            'compliance_deadline', 'sebi_url', 'full_text',
            'ai_parsed', 'ai_parsed_at',
            'is_superseded', 'superseded_by',
            'actions',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'organization', 'ai_parsed_at', 'created_at', 'updated_at']
