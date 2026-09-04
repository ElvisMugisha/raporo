"""Production settings: strict transport security on top of base."""

import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

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
# that waiver silently. This flag turns `common.E100` on: any `manage.py check`
# run against these settings then fails, which is what a pre-boot check in the
# container makes fatal. `common.E100` is registered under `Tags.security`, not
# `Tags.database` - database-tagged checks are skipped unless an alias is passed
# explicitly, which is exactly how this guard sat inert for two review rounds.
ENFORCE_NON_TEST_DATABASE = True

SECURE_SSL_REDIRECT = True

# `SECURE_SSL_REDIRECT` alone, behind a TLS-terminating proxy, is an infinite
# redirect loop: the proxy speaks TLS to the client and plain HTTP to us, Django
# sees `http`, answers 301 to `https`, and the proxy forwards the retry as
# `http` again. That is a total outage on the first deploy, so `common.E101`
# refuses to boot while the pair is incomplete.
#
# The value cannot be shipped: which header carries the client's scheme is a
# property of the proxy in front of *this* deployment, and the deployment does
# not exist yet. So it is read from the environment, validated here, and its
# absence is a boot-time refusal rather than a guess:
#
#     DJANGO_SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
#
# Read the warning before setting it. Django trusts this header absolutely: if
# the proxy *appends* to `X-Forwarded-Proto` instead of overwriting it, or if
# anything can reach this process without passing through the proxy, then a
# client sending `X-Forwarded-Proto: https` makes `request.is_secure()` true,
# the redirect stop, and every "secure" cookie be sent over plain HTTP. Set it
# only for a header the proxy unconditionally overwrites, and only when the
# process is unreachable except through that proxy.
#
# Always bound, `None` when unset: an explicit `None` is what Django defaults to
# anyway, and leaving the name undefined instead makes the setting's value
# depend on import history rather than on the environment.
SECURE_PROXY_SSL_HEADER = None
_PROXY_SSL_HEADER = os.environ.get("DJANGO_SECURE_PROXY_SSL_HEADER", "").strip()
if _PROXY_SSL_HEADER:
    _parts = _PROXY_SSL_HEADER.split(",")
    if len(_parts) != 2:
        raise ImproperlyConfigured(
            "DJANGO_SECURE_PROXY_SSL_HEADER must be exactly "
            "'<WSGI_META_KEY>,<expected value>', for example "
            f"'HTTP_X_FORWARDED_PROTO,https'. Got {_PROXY_SSL_HEADER!r}."
        )
    _header, _expected = _parts
    # A WSGI META key, not a header name: Django reads `request.META[key]`, so
    # `X-Forwarded-Proto` or `X_FORWARDED_PROTO` would match nothing and the
    # redirect loop would survive a setting that looks configured.
    if not re.fullmatch(r"HTTP_[A-Z0-9_]+", _header):
        raise ImproperlyConfigured(
            f"DJANGO_SECURE_PROXY_SSL_HEADER names {_header!r}, which is not a WSGI "
            f"META key. Django looks the value up in request.META, so the name must "
            f"be the header upper-cased with dashes as underscores and an HTTP_ "
            f"prefix: X-Forwarded-Proto -> HTTP_X_FORWARDED_PROTO."
        )
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", _expected):
        raise ImproperlyConfigured(
            f"DJANGO_SECURE_PROXY_SSL_HEADER expects the value {_expected!r}, which is "
            f"not a single token. It is compared verbatim against the header, so "
            f"whitespace or an empty value can never match: use e.g. 'https'."
        )
    SECURE_PROXY_SSL_HEADER = (_header, _expected)

# The health probe is the one request that legitimately arrives over plain HTTP
# and cannot carry the proxy header: a container probe reaches this process on
# loopback, before and beside any proxy. Without this exemption the probe gets a
# 301 to a `https://127.0.0.1` nothing is listening on, the container is never
# healthy, and the deploy fails around a perfectly working application. Safe
# because the endpoint returns `{"status": "ok"}` and reads nothing - it is the
# only path exempt, matched anchored so `/healthzzz` is not.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]

SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CONTENT_TYPE_NOSNIFF = True
