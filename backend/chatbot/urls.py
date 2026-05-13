from django.urls import path
from . import views

urlpatterns = [
    path('query/', views.chat_query, name='chatbot-query'),
    path('history/', views.chat_history, name='chatbot-history'),
    path('<uuid:message_id>/feedback/', views.chat_feedback, name='chatbot-feedback'),
]
