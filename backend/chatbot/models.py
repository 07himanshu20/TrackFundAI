"""
Chatbot models — conversation history for audit and model improvement.
"""
import uuid
from django.conf import settings
from django.db import models


class ChatMessage(models.Model):
    """Persists each user query and chatbot response."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        'accounts.Organization', on_delete=models.CASCADE, related_name='chat_messages',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='chat_messages',
    )
    query = models.TextField()
    intent = models.CharField(max_length=50, blank=True)
    response = models.TextField()
    helpful = models.BooleanField(
        null=True, blank=True,
        help_text='User feedback: True = helpful, False = not helpful, None = no feedback',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', '-created_at']),
        ]

    def __str__(self):
        return f'Chat: {self.user} — {self.query[:50]}'
