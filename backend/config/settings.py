import os
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Environment-aware .env loading.
#   TFAI_ENV=local  -> .env.local   (your Mac)
#   TFAI_ENV=dev    -> .env.dev     (staging on the EC2 web server)
#   TFAI_ENV=prod   -> .env.prod    (production on the EC2 web server)
#   TFAI_ENV unset  -> .env         (legacy; current tunneled config)
TFAI_ENV = os.getenv('TFAI_ENV', '').strip()
if TFAI_ENV:
    _env_file = BASE_DIR / f'.env.{TFAI_ENV}'
    if not _env_file.exists():
        raise RuntimeError(f'TFAI_ENV={TFAI_ENV} requested but {_env_file} not found')
    load_dotenv(_env_file)
else:
    load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.getenv('SECRET_KEY', 'fallback-secret-key-change-in-production')
DEBUG = os.getenv('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

# Gemini Configuration — dual-mode (Vertex AI for prod, AI Studio for dev).
# See .env for the active backend. The SDK client construction lives in
# api/gemini_service.py:get_client() and branches on GOOGLE_GENAI_USE_VERTEXAI.
GOOGLE_GENAI_USE_VERTEXAI = os.getenv('GOOGLE_GENAI_USE_VERTEXAI', 'False')
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')

# AI Studio auth (used when GOOGLE_GENAI_USE_VERTEXAI=False)
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY', '')

# Vertex AI auth (used when GOOGLE_GENAI_USE_VERTEXAI=True).
# Requires `gcloud auth application-default login` on the host.
GOOGLE_CLOUD_PROJECT = os.getenv('GOOGLE_CLOUD_PROJECT', '')
GOOGLE_CLOUD_LOCATION = os.getenv('GOOGLE_CLOUD_LOCATION', 'us-central1')

# Excel MIS file path (optional — can also be uploaded via UI)
MIS_FILE_PATH = os.getenv('MIS_FILE_PATH', '')

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_celery_beat',
    'django_celery_results',
    'accounts',
    'funds',
    'documents',
    'notifications',
    'portfolio',
    'investments',
    'lp',
    'accounting',
    'compliance',
    'dataimport',
    'api',
    'emailingestion',
    'marketdata',
    'reporting',
    'riskscore',
    'ic_workflow',
    'fundclose',
    'tds',
    'mis_consolidation',
    'marketresearch',
    'chatbot',
]

AUTH_USER_MODEL = 'accounts.User'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=8),   # v5: 8h (was 2h)
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': False,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.common.CommonMiddleware',
    'accounts.middleware.OrganizationMiddleware',
    'accounts.middleware.CacheInvalidationMiddleware',
]

# CORS — allow frontend dev server
CORS_ALLOWED_ORIGINS = os.getenv(
    'CORS_ALLOWED_ORIGINS',
    'http://localhost:5500,http://127.0.0.1:5500'
).split(',')
CORS_ALLOW_ALL_ORIGINS = DEBUG  # permissive in dev
CORS_ALLOW_HEADERS = [
    'accept', 'accept-encoding', 'authorization', 'content-type',
    'dnt', 'origin', 'user-agent', 'x-csrftoken', 'x-requested-with',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {'context_processors': []},
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# Database — PostgreSQL when DATABASE_URL is set, otherwise SQLite for dev
_db_url = os.getenv('DATABASE_URL', '')
if _db_url:
    # Expected format: postgres://user:pass@host:port/dbname
    import re
    m = re.match(r'postgres(?:ql)?://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)', _db_url)
    if m:
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.postgresql',
                'USER': m.group(1),
                'PASSWORD': m.group(2),
                'HOST': m.group(3),
                'PORT': m.group(4),
                'NAME': m.group(5),
                'CONN_MAX_AGE': 600,        # Keep DB connections alive 10 min
                'CONN_HEALTH_CHECKS': True,  # Verify connection before reuse
                'OPTIONS': {
                    'connect_timeout': 10,
                    'options': '-c statement_timeout=30000',  # 30s query timeout
                },
            }
        }
    else:
        raise ValueError(f'Invalid DATABASE_URL format: {_db_url}')
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

STATIC_URL = '/static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# File uploads — for Excel MIS uploads
MEDIA_ROOT = BASE_DIR / 'media'
MEDIA_URL = '/media/'
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024  # 50 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': 'INFO'},
}

# -- Redis Cache --
# Uses the same Redis instance as Celery but a different DB (db=1 for cache, db=0 for Celery)
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
_redis_cache_url = REDIS_URL.rsplit('/', 1)[0] + '/1'  # Use db=1 for cache

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': _redis_cache_url,
        'TIMEOUT': 300,  # Default 5 min TTL
        'KEY_PREFIX': 'tfai',
        'OPTIONS': {
            'db': 1,
        },
    }
}

# -- Celery Configuration --
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = 'django-db'
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Asia/Kolkata'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

# -- Email Ingestion --
MIS_EMAIL_HOST = os.getenv('MIS_EMAIL_HOST', 'imap.gmail.com')
MIS_EMAIL_PORT = int(os.getenv('MIS_EMAIL_PORT', '993'))
MIS_EMAIL_USER = os.getenv('MIS_EMAIL_USER', '')
MIS_EMAIL_PASSWORD = os.getenv('MIS_EMAIL_PASSWORD', '')
MIS_EMAIL_FOLDER = os.getenv('MIS_EMAIL_FOLDER', 'INBOX')

# -- Market Data (BSE/NSE) --
BSE_API_KEY = os.getenv('BSE_API_KEY', '')
NSE_API_KEY = os.getenv('NSE_API_KEY', '')
BLOOMBERG_API_KEY = os.getenv('BLOOMBERG_API_KEY', '')
ALPHA_VANTAGE_API_KEY = os.getenv('ALPHA_VANTAGE_API_KEY', '')

# -- MFA --
MFA_SMS_PROVIDER = os.getenv('MFA_SMS_PROVIDER', 'msg91')  # msg91 / fast2sms
MSG91_AUTH_KEY = os.getenv('MSG91_AUTH_KEY', '')
FAST2SMS_API_KEY = os.getenv('FAST2SMS_API_KEY', '')

# -- SSO (stub placeholders for Google/Microsoft OAuth) --
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')
MICROSOFT_CLIENT_ID = os.getenv('MICROSOFT_CLIENT_ID', '')
MICROSOFT_CLIENT_SECRET = os.getenv('MICROSOFT_CLIENT_SECRET', '')

# -- Market Research / AI --
MARKET_RESEARCH_AI_ENABLED = os.getenv('MARKET_RESEARCH_AI_ENABLED', 'True') == 'True'

# -- Export Engine --
EXPORT_BASE_URL = os.getenv('EXPORT_BASE_URL', 'http://localhost:8000')
