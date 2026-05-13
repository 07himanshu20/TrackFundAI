from django.urls import path
from . import views

urlpatterns = [
    path('calendar/', views.calendar_list, name='reporting-calendar-list'),
    path('calendar/<uuid:obligation_id>/', views.calendar_detail, name='reporting-calendar-detail'),
    path('calendar/<uuid:obligation_id>/submit/', views.mark_submitted, name='reporting-mark-submitted'),
    path('calendar/<uuid:obligation_id>/generate/', views.generate_report, name='reporting-generate'),
    path('reports/', views.generated_reports_list, name='generated-reports-list'),
    path('update-calendar/', views.trigger_calendar_update, name='trigger-calendar-update'),
    path('export/<str:report_type>/', views.excel_export, name='excel-export'),
]
