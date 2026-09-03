"""Base Django settings shared by every environment.

Environment-specific overrides live in `dev.py` and `prod.py`.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Abstract bases only (no models, no migrations); installed so its system
    # checks run.
    "common",
    "apps.accounts",
    "apps.orgs",
    "apps.audit",
]

AUTH_USER_MODEL = "accounts.User"

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
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
                "django.template.context_processors.i18n",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Two database identities, one per connection alias — never one alias with a
# swappable USER. See docs/adr/0009 and scripts/db/roles.sql.
#
#   default    raporo_app    serves every request. Owns nothing, holds
#                            SELECT/INSERT/UPDATE/DELETE and nothing else, and
#                            is subject to row-level security.
#   migrator   raporo_owner  owns the schema; runs `migrate`. Never serves.
#
# Why an alias and not `DATABASES["default"]["USER"] = ...`: a mutable key is a
# variable that one mistake, one settings override or one compromised process
# can flip, and it leaves the elevated credential inside the serving
# connection's configuration. An alias makes the elevated identity a *place*
# instead of a value — `--database=migrator` is the only door, and on a
# workload where the owner credential is not injected the door is not there at
# all (see below). "Connect as the owner, then SET ROLE raporo_app" is not an
# alternative: RESET ROLE climbs straight back out, so SET ROLE is a
# convenience and not a boundary.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "raporo"),
        # The fallback is the least-privileged role on purpose. It used to be
        # `raporo`, which in the postgres image is the bootstrap *superuser*
        # and the owner of every table — so a missing POSTGRES_USER silently
        # served requests with the one credential that can disable every
        # control in this schema.
        "USER": os.environ.get("POSTGRES_USER", "raporo_app"),
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

# The migrator alias exists only where the owner credential is injected: the
# dev container (which migrates on boot), the CI test step, and the deploy's
# migration job. A serving workload in production gets neither variable, so
# `migrator` is simply absent from DATABASES there.
#
# Absent, and not a copy of `default` with a note attached, because that is
# what makes the failure loud. MEASURED: `migrate --database=migrator` without
# the credential exits non-zero on Django's own argument parsing —
# `error: argument --database: invalid choice: 'migrator' (choose from
# 'default')` — before it opens a connection. A fallback to `default` would
# instead migrate as
# raporo_app, which either fails halfway through with `permission denied` or —
# if someone ever over-grants that role — quietly recreates the single-role
# world this split exists to end. Both variables are required together: half a
# credential is a typo, not a configuration.
_MIGRATE_USER = os.environ.get("RAPORO_MIGRATE_USER", "")
_MIGRATE_PASSWORD = os.environ.get("RAPORO_MIGRATE_PASSWORD", "")
if _MIGRATE_USER and _MIGRATE_PASSWORD:
    DATABASES["migrator"] = {
        **DATABASES["default"],
        "USER": _MIGRATE_USER,
        "PASSWORD": _MIGRATE_PASSWORD,
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization: Rwanda-first (English, Kinyarwanda, French).
LANGUAGE_CODE = "en"
LANGUAGES = [
    ("en", "English"),
    ("rw", "Ikinyarwanda"),
    ("fr", "Français"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
USE_TZ = True
TIME_ZONE = "UTC"

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Uploads (organization logos today). MEDIA_ROOT sits deliberately OUTSIDE
# BASE_DIR: a directory inside the source tree can end up served as static
# content or committed by accident. Serving media from a separate origin with a
# fixed safe Content-Type is devops-engineer's, at deploy; prod.py requires the
# path to be set explicitly.
MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", "/var/tmp/raporo-media"))

# Uploads are bounded before any of our code sees them. The logo validators cap
# one image at 2 MB (common.validators.MAX_IMAGE_BYTES); these stop a request
# from spending memory to get there.
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 3 * 1024 * 1024
FILE_UPLOAD_PERMISSIONS = 0o644

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Session / CSRF cookie hardening. Loosened only in dev.py for local HTTP.
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
