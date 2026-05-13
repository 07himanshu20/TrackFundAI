from django.urls import path
from api import views, portfolio_views

urlpatterns = [
    # --- Health / legacy single-company endpoints (kept for compatibility) ---
    path("status/",          views.status_check,    name="status"),
    path("summary/",         views.summary,         name="summary"),
    path("monthly-pl/",      views.monthly_pl,      name="monthly_pl"),
    path("cash-flow/",       views.cash_flow,       name="cash_flow"),
    path("working-capital/", views.working_capital, name="working_capital"),
    path("sales-segments/",  views.sales_segments,  name="sales_segments"),
    path("full-data/",       views.full_data,       name="full_data"),
    path("chat/",            views.chat,            name="chat"),
    path("upload-mis/",      views.upload_mis,      name="upload_mis"),

    # --- AI Insights + Predictions + MIS Report Generation ---
    path("ai-insights/",     views.ai_insights,         name="ai_insights"),
    path("ai-predictions/",  views.ai_predictions,      name="ai_predictions"),
    path("generate-report/", views.generate_mis_report, name="generate_mis_report"),

    # --- New hierarchical portfolio endpoints ---
    path("portfolio/",                              portfolio_views.portfolio_root,      name="portfolio_root"),
    path("portfolio/compare/",                      portfolio_views.portfolio_compare,   name="portfolio_compare"),
    path("portfolio/chat/",                         portfolio_views.portfolio_chat,      name="portfolio_chat"),
    path("portfolio/reload/",                       portfolio_views.portfolio_reload,    name="portfolio_reload"),
    path("portfolio/ancestors/<str:node_id>/",      portfolio_views.portfolio_ancestors, name="portfolio_ancestors"),
    path("portfolio/node/<str:node_id>/",           portfolio_views.portfolio_node,      name="portfolio_node"),
]
