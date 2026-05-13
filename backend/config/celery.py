"""
Celery application configuration for TrackFundAI.

Workers handle:
  - Email MIS ingestion (scheduled + on-demand)
  - Market data price feed pulls (BSE/NSE/Bloomberg)
  - Post-import NAV/IRR/MOIC/Carry recalculation
  - Risk score computation
  - Reporting calendar reminders (T+3, T+5)
  - Quarterly valuation cycle triggers
"""

import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('trackfundai')

# Load Celery config from Django settings (CELERY_ prefix)
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks from all installed apps
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
