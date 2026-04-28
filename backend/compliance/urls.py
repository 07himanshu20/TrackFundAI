from django.urls import path
from . import views

urlpatterns = [
    # SEBI Reports (QAR / AAR)
    path('reports/', views.sebi_report_list, name='sebi-report-list'),
    path('reports/<uuid:report_id>/', views.sebi_report_detail, name='sebi-report-detail'),

    # AML Due Diligence
    path('aml/', views.aml_list, name='aml-list'),
    path('aml/<uuid:aml_id>/', views.aml_detail, name='aml-detail'),

    # Compliance Test Reports (CTR)
    path('ctr/', views.ctr_list, name='ctr-list'),
    path('ctr/<uuid:ctr_id>/', views.ctr_detail, name='ctr-detail'),
    path('ctr/<uuid:ctr_id>/checklist/', views.ctr_checklist_list, name='ctr-checklist-list'),
    path('ctr/checklist/<uuid:item_id>/', views.ctr_checklist_detail, name='ctr-checklist-detail'),

    # Equity Threshold Alerts
    path('alerts/', views.threshold_alert_list, name='threshold-alert-list'),
    path('alerts/<uuid:alert_id>/', views.threshold_alert_detail, name='threshold-alert-detail'),

    # Compliance Calendar
    path('calendar/', views.calendar_list, name='compliance-calendar-list'),
    path('calendar/<uuid:event_id>/', views.calendar_detail, name='compliance-calendar-detail'),

    # PPM Amendments
    path('ppm/', views.ppm_amendment_list, name='ppm-amendment-list'),
    path('ppm/<uuid:amendment_id>/', views.ppm_amendment_detail, name='ppm-amendment-detail'),

    # SEBI Circulars
    path('circulars/', views.circular_list, name='sebi-circular-list'),
    path('circulars/<uuid:circular_id>/', views.circular_detail, name='sebi-circular-detail'),
    path('circulars/<uuid:circular_id>/actions/', views.circular_action_list, name='circular-action-list'),
    path('circular-actions/<uuid:action_id>/', views.circular_action_detail, name='circular-action-detail'),
]
