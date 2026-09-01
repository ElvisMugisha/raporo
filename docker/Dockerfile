# No `# syntax=` directive on purpose: this file uses no BuildKit-only
# features (`RUN --mount`, heredocs, `COPY --link`), so the directive would buy
# nothing and cost a registry round-trip for the frontend image on every build
# — which is a build failure the moment the network is unavailable. Add it back
# pinned to a digest on the day a feature here actually needs it.

# --- base: dependencies + application code, non-root -------------------------
# Everything both targets share. Nothing that only one of them needs.
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The entrypoint is installed outside /app on purpose: the dev bind mount
# replaces /app wholesale, so an image-owned copy here cannot be shadowed (or
# stripped of its exec bit) by whatever the host checkout looks like.
COPY docker/entrypoint.sh /usr/local/bin/raporo-entrypoint
RUN chmod 0755 /usr/local/bin/raporo-entrypoint

# /app stays root-owned and world-readable: the runtime user needs to *read*
# the application, never to write it, and code a compromised process can
# rewrite is free persistence across a restart. Nothing here writes into /app —
# PYTHONDONTWRITEBYTECODE keeps __pycache__ out, static files are served from
# STATICFILES_DIRS, and the state lives in Postgres. In dev the `.:/app` bind
# mount replaces this directory with the host checkout (owned by the developer,
# uid 1000 = the `raporo` user), which is what makes pytest's and ruff's caches
# writable there and only there.
RUN useradd -m raporo
USER raporo

ENTRYPOINT ["raporo-entrypoint"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# --- dev: adds the Postgres client -------------------------------------------
# `manage.py dbshell` shells out to `psql`, which python:3.13-slim does not
# ship. Debian trixie's postgresql-client is 17, matching the `db` service.
# It costs ~59 MB (the Debian package drags in perl and krb5) and never
# reaches the `runtime` target: compose builds this one, a pipeline builds
# `runtime` explicitly. Keeps `tests/` so the suite can also be run from the
# image itself, without the bind mount.
FROM base AS dev

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
USER raporo

# --- runtime: PLACEHOLDER, not a deployable image ----------------------------
# This stage is *not* production-ready and nothing deploys it today. It
# inherits `CMD python manage.py runserver`, which is Django's development
# server: single-threaded-ish, unhardened, and documented by Django itself as
# unfit to serve a site. There is no WSGI/ASGI server in requirements.txt.
#
# That is deliberate for slice 1. Adding gunicorn now would add a dependency
# with no consumer and no reviewed configuration (workers, timeouts, graceful
# shutdown, access-log format, proxy headers) — decisions that belong with the
# deploy task, alongside `SECURE_PROXY_SSL_HEADER`/`CSRF_TRUSTED_ORIGINS` and a
# production compose manifest, which are already carried forward to it.
#
# The stage exists now only so that `--target runtime` is a stable name in a
# pipeline and so the "what does a shipped image contain" question has one
# answer: base, no psql, no apt-get layer, no tests, non-root, /app not
# writable by the runtime user. (The three `deb.debian.org_*_InRelease` files
# under /var/lib/apt/lists come from upstream python:3.13-slim, not from us.)
# Whoever picks up the deploy task owns replacing the CMD with a real server
# and deleting this paragraph.
FROM base AS runtime

# Test code is dead surface in a served image: nothing imports it, and it is
# the one directory that routinely grows fixtures and helpers that assume a
# throwaway database. `.dockerignore` cannot drop it, because `dev` wants it.
# (CLAUDE.md and .mcp.json are excluded from every stage by .dockerignore.)
USER root
RUN rm -rf /app/tests
USER raporo
