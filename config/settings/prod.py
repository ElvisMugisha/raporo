"""Production settings: strict transport security on top of base."""

import os
from pathlib import Path

from .base import *  # noqa: F401,F403

# No silent default in production: uploaded files must land where the operator
# chose, on durable storage, outside the source tree.
MEDIA_ROOT = Path(os.environ["DJANGO_MEDIA_ROOT"])

ALLOWED_HOSTS = [
    h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h
]

# The append-only trigger (common/db.py) waives its TRUNCATE guard for any
# database whose name starts with `test_`, so Django's test databases can be
# torn down. A production database that is *mis-named* `test_...` would inherit
# that waiver silently. This flag turns `common.E100` on: `manage.py check`
# (which the container runs before boot) then refuses such a name outright.
ENFORCE_NON_TEST_DATABASE = True

SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
