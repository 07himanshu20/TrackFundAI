from django.urls import path
from . import views

urlpatterns = [
    path('query/', views.chat_query, name='chatbot-query'),
    path('history/', views.chat_history, name='chatbot-history'),
    path('<uuid:message_id>/feedback/', views.chat_feedback, name='chatbot-feedback'),
    path('conversations/', views.conversation_list, name='chatbot-conversations'),
    path('conversations/<uuid:conversation_id>/', views.conversation_messages, name='chatbot-conversation-messages'),
    path('conversations/<uuid:conversation_id>/delete/', views.conversation_delete, name='chatbot-conversation-delete'),
]
