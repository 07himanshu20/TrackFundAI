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
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
