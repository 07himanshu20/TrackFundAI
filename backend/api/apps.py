from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    def ready(self):
        """Load the MIS file from .env path at startup (if configured)."""
        from api import data_store
        data_store.load_from_env()
