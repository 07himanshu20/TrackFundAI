from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    # Auth endpoints (login, logout, refresh, me, audit-log)
    path('api/auth/', include('accounts.urls')),

    # Fund administration (CRUD for funds, schemes, entities)
    path('api/funds/', include('funds.urls')),

    # Document vault
    path('api/documents/', include('documents.urls')),

    # Notifications
    path('api/notifications/', include('notifications.urls')),

    # Investments, valuations, KPIs, exits, board packs (Phase 2)
    path('api/', include('investments.urls')),

    # Existing portfolio + legacy endpoints (preserved as-is)
    path('api/', include('api.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
