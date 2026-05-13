from django.urls import path
from . import views

urlpatterns = [
    path('submissions/', views.submission_list, name='email-submission-list'),
    path('poll/', views.trigger_poll, name='email-trigger-poll'),
    path('poll-logs/', views.poll_logs, name='email-poll-logs'),
]
