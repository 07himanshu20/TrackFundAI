from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    # Auth endpoints (login, logout, refresh, me, audit-log)
    path('api/auth/', include('accounts.urls')),

    # Fund administration (CRUD for funds, schemes, entities, categories)
    path('api/funds/', include('funds.urls')),

    # Document vault
    path('api/documents/', include('documents.urls')),

    # Notifications
    path('api/notifications/', include('notifications.urls')),

    # Investments, valuations, KPIs, exits, board packs
    path('api/', include('investments.urls')),

    # LP Management (investors, commitments, capital calls, distributions)
    path('api/lp/', include('lp.urls')),

    # Fund Accounting (NAV, carry, ledger, fees)
    path('api/accounting/', include('accounting.urls')),

    # SEBI Compliance (reports, AML, CTR, alerts, calendar)
    path('api/compliance/', include('compliance.urls')),

    # Data Import (drag-and-drop fund Excel upload with Gemini AI mapping)
    path('api/dataimport/', include('dataimport.urls')),

    # Existing portfolio + legacy endpoints (preserved as-is)
    path('api/', include('api.urls')),

    # Email MIS Ingestion
    path('api/email-ingestion/', include('emailingestion.urls')),

    # Market Data Feeds (BSE/NSE/Bloomberg prices, FX rates)
    path('api/market-data/', include('marketdata.urls')),

    # Reporting module (LP letters, valuation certs, NAV statements, FATCA/CRS)
    path('api/reporting/', include('reporting.urls')),

    # ML Risk Scoring
    path('api/risk-scores/', include('riskscore.urls')),

    # IC Workflow (deal pipeline → IC presentation → vote → decision)
    path('api/ic/', include('ic_workflow.urls')),

    # Fund Close (fund lifecycle close + clawback + SEBI deregistration)
    path('api/fund-close/', include('fundclose.urls')),

    # TDS (withholding tax + Form 26Q)
    path('api/tds/', include('tds.urls')),

    # MIS Consolidation (Budget vs Actual, consolidated MIS, anomaly detection)
    path('api/mis/', include('mis_consolidation.urls')),

    # Market Research / Market Explorer (142 opportunities, AI studies)
    path('api/market/', include('marketresearch.urls')),

    # NL Chatbot (intent classification + SQL builder + Gemini response)
    path('api/chatbot/', include('chatbot.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
