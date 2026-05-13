from django.urls import path
from . import views

urlpatterns = [
    path('bva/', views.bva_list, name='bva-list'),
    path('consolidated/', views.consolidated_mis, name='consolidated-mis'),
    path('consolidated/run/', views.run_consolidation, name='mis-run-consolidation'),
    path('consolidated/6month/', views.six_month_rollup, name='mis-6month-rollup'),
    path('anomalies/', views.anomaly_alerts, name='mis-anomalies'),
    path('anomalies/<uuid:pk>/resolve/', views.resolve_anomaly, name='mis-anomaly-resolve'),
    path('submission-status/', views.mis_submission_status, name='mis-submission-status'),
]
