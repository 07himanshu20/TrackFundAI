from django.urls import path
from . import views

urlpatterns = [
    # ── Investments (nested under scheme) ─────────────────────
    # GET/POST /api/schemes/{id}/investments/
    path('schemes/<uuid:scheme_id>/investments/',
         views.investment_list, name='investment-list'),

    # GET/PUT /api/investments/{id}/
    path('investments/<uuid:investment_id>/',
         views.investment_detail, name='investment-detail'),

    # ── Tranches (nested under investment) ────────────────────
    # GET/POST /api/investments/{id}/tranches/
    path('investments/<uuid:investment_id>/tranches/',
         views.tranche_list, name='tranche-list'),

    # ── Valuations ────────────────────────────────────────────
    # GET/POST /api/investments/{id}/valuations/
    path('investments/<uuid:investment_id>/valuations/',
         views.valuation_list, name='valuation-list'),

    # PUT /api/valuations/{id}/
    path('valuations/<uuid:valuation_id>/',
         views.valuation_update, name='valuation-update'),

    # POST /api/valuations/{id}/approve/
    path('valuations/<uuid:valuation_id>/approve/',
         views.valuation_approve, name='valuation-approve'),

    # ── Founder Portal / KPIs ─────────────────────────────────
    # GET /api/founder/companies/
    path('founder/companies/',
         views.founder_companies, name='founder-companies'),

    # POST /api/founder/companies/{id}/submit-kpi/
    path('founder/companies/<uuid:investment_id>/submit-kpi/',
         views.founder_submit_kpi, name='founder-submit-kpi'),

    # GET /api/founder/companies/{id}/kpi-history/
    path('founder/companies/<uuid:investment_id>/kpi-history/',
         views.founder_kpi_history, name='founder-kpi-history'),

    # GET /api/investments/{id}/kpis/  (GP view)
    path('investments/<uuid:investment_id>/kpis/',
         views.investment_kpis, name='investment-kpis'),

    # PUT /api/kpis/{id}/review/
    path('kpis/<uuid:kpi_id>/review/',
         views.kpi_review, name='kpi-review'),

    # ── Board Meetings (nested under investment) ───────────────
    # GET/POST /api/investments/{id}/board-meetings/
    path('investments/<uuid:investment_id>/board-meetings/',
         views.board_meeting_list, name='board-meeting-list'),

    # ── Exit Scenarios ────────────────────────────────────────
    # GET/POST /api/investments/{id}/exit-scenarios/
    path('investments/<uuid:investment_id>/exit-scenarios/',
         views.exit_scenario_list, name='exit-scenario-list'),

    # ── Fund-Level Portfolio Analytics ───────────────────────
    path('portfolio/burn-runway/',    views.portfolio_burn_runway,     name='portfolio-burn-runway'),
    path('portfolio/exits/',          views.portfolio_exits_summary,   name='portfolio-exits'),
    path('portfolio/kpis/',           views.portfolio_kpis_summary,    name='portfolio-kpis'),
    path('portfolio/saas-metrics/',   views.portfolio_saas_metrics,    name='portfolio-saas-metrics'),
    path('portfolio/quoted-unquoted/', views.portfolio_quoted_unquoted, name='portfolio-quoted-unquoted'),
    path('portfolio/investments/',    views.portfolio_investments_list,    name='portfolio-investments-list'),
    path('portfolio/valuations/',     views.portfolio_valuations_list,     name='portfolio-valuations-list'),
    path('portfolio/kpi-tracking/',   views.portfolio_kpi_tracking,        name='portfolio-kpi-tracking'),
    path('portfolio/kpi-matrix/',    views.portfolio_kpi_matrix,          name='portfolio-kpi-matrix'),
    path('portfolio/exit-scenarios/', views.portfolio_exit_scenarios_list, name='portfolio-exit-scenarios-list'),
    path('portfolio/board-meetings/', views.portfolio_board_meetings_list, name='portfolio-board-meetings-list'),
    path('portfolio/avg-holding/',    views.portfolio_avg_holding,         name='portfolio-avg-holding'),

    # ── Portfolio Companies ──────────────────────────────────
    # GET/POST /api/portfolio-companies/
    path('portfolio-companies/',
         views.portfolio_company_list, name='portfolio-company-list'),

    # GET/PUT/DELETE /api/portfolio-companies/{id}/
    path('portfolio-companies/<uuid:company_id>/',
         views.portfolio_company_detail, name='portfolio-company-detail'),

    # ── KPI Definitions ──────────────────────────────────────
    # GET/POST /api/kpi-definitions/
    path('kpi-definitions/',
         views.kpi_definition_list, name='kpi-definition-list'),

    # GET/PUT/DELETE /api/kpi-definitions/{id}/
    path('kpi-definitions/<uuid:kpi_def_id>/',
         views.kpi_definition_detail, name='kpi-definition-detail'),

    # ── Board Pack ────────────────────────────────────────────
    # POST /api/schemes/{id}/board-pack/generate/
    path('schemes/<uuid:scheme_id>/board-pack/generate/',
         views.board_pack_generate, name='board-pack-generate'),

    # ── Exit Signal Engine (v5 AI Analytics) ─────────────────
    path('portfolio-companies/<uuid:company_id>/exit-signal/',
         views.exit_signal_view, name='exit-signal'),

    # ── Feature Engineering / Risk Re-compute ─────────────────
    path('portfolio-companies/<uuid:company_id>/features/',
         views.company_features_view, name='company-features'),
]
