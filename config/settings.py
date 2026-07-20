"""
Django settings for the Fuel Route Planner API.

Configuration is environment-driven so the same codebase runs three ways:

  1. Zero-setup local review:  SQLite + in-memory cache (just `pip install -r
     requirements.txt && python manage.py runserver`, nothing else to stand up).
  2. Docker Compose:           Postgres + Redis, wired automatically via
     DATABASE_URL / REDIS_URL (see docker-compose.yml).
  3. Real deployment (e.g. GCP): same env vars, point them at managed
     Cloud SQL / Memorystore.

See https://docs.djangoproject.com/en/5.2/topics/settings/
"""

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-only-key-do-not-use-in-production-1234567890",
)

DEBUG = env_bool("DJANGO_DEBUG", True)

ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()
]

# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "stations",
    "planner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --------------------------------------------------------------------------
# Database
#
# Defaults to a local SQLite file so the project runs with zero external
# services. Set DATABASE_URL (e.g. from docker-compose) to switch to
# Postgres without touching code -- this is the DB the team uses in
# production, per the job spec.
# --------------------------------------------------------------------------

DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

# --------------------------------------------------------------------------
# Caching
#
# Geocoding results and computed route plans are cached (see planner/services
# and planner/models.py). Falls back to a local in-memory cache when REDIS_URL
# isn't set, so caching behavior can be exercised without Redis installed.
# --------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL")

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "fuel-route-planner",
        }
    }

# --------------------------------------------------------------------------
# Password validation
# --------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# I18N / TZ
# --------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------
# Static files
# --------------------------------------------------------------------------

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Django REST Framework
# --------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("API_ANON_THROTTLE_RATE", "60/min"),
    },
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "EXCEPTION_HANDLER": "planner.exceptions.api_exception_handler",
}

# --------------------------------------------------------------------------
# Fuel Route Planner domain settings
#
# Centralized here (rather than hard-coded in services) so the assumptions
# behind every route plan are explicit, documented, and easy to tune.
# --------------------------------------------------------------------------

VEHICLE_MPG = float(os.environ.get("VEHICLE_MPG", "10"))
VEHICLE_RANGE_MILES = float(os.environ.get("VEHICLE_RANGE_MILES", "500"))
ROUTE_SEARCH_CORRIDOR_MILES = float(os.environ.get("ROUTE_SEARCH_CORRIDOR_MILES", "8"))

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_TIMEOUT_SECONDS = float(os.environ.get("OSRM_TIMEOUT_SECONDS", "12"))

NOMINATIM_BASE_URL = os.environ.get("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org")
NOMINATIM_TIMEOUT_SECONDS = float(os.environ.get("NOMINATIM_TIMEOUT_SECONDS", "8"))
NOMINATIM_USER_AGENT = os.environ.get(
    "NOMINATIM_USER_AGENT", "fuel-route-planner-api (contact: set NOMINATIM_USER_AGENT)"
)

GEOCODE_CACHE_TTL_SECONDS = int(os.environ.get("GEOCODE_CACHE_TTL_SECONDS", str(60 * 60 * 24 * 30)))
ROUTE_CACHE_TTL_SECONDS = int(os.environ.get("ROUTE_CACHE_TTL_SECONDS", str(60 * 60)))

US_CITIES_REFERENCE_CSV = BASE_DIR / "data" / "uscities.csv"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
}
