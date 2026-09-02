# No `# syntax=` directive on purpose: this file uses no BuildKit-only
# features (`RUN --mount`, `COPY --exclude`, heredocs, `COPY --link`), so the
# directive would buy nothing and cost a registry round-trip for the frontend
# image on every build — which is a build failure the moment the network is
# unavailable. Add it back pinned to a digest on the day a feature here
# actually needs it.

# --- base: dependencies + application code, non-root -------------------------
# Everything a *served* image needs, and nothing else. `dev` adds its own
# extras on top; `runtime` adds nothing at all.
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The application is copied path by path rather than with `COPY . .`, because
# `COPY . .` is what put `tests/` into every shipped layer. A `rm -rf` in a
# later stage only writes a whiteout: the bytes stay in this stage's layer,
# `docker save` still shows them, and the comment claiming otherwise was
# false. Listing what a served image needs is the only way to keep something
# out of it in a Dockerfile with no `syntax=` directive.
#
# THE RULE FOR WHOEVER ADDS THE NEXT TOP-LEVEL PACKAGE: add it here. It is not
# picked up automatically, and in dev the `.:/app` bind mount hides the
# omission, so the first failure would be at deploy. The pipeline's
# `--target runtime` build must run `manage.py check` inside the image for
# that reason — carried forward with the deploy task.
#
# Deliberately absent: tests/ (dev only, below), pytest.ini and ruff.toml (dev
# only), scripts/setup.sh (bootstraps a developer's *host*, never runs in a
# container), README/LICENSE/git metadata. CLAUDE.md, .mcp.json, docs/ and
# every `.env*` are excluded from the build context by .dockerignore.
COPY manage.py ./
COPY apps/ ./apps/
COPY common/ ./common/
COPY config/ ./config/
COPY locale/ ./locale/
COPY static/ ./static/
COPY templates/ ./templates/

# The entrypoint is installed outside /app on purpose: the dev bind mount
# replaces /app wholesale, so an image-owned copy here cannot be shadowed (or
# stripped of its exec bit) by whatever the host checkout looks like.
COPY docker/entrypoint.sh /usr/local/bin/raporo-entrypoint
RUN chmod 0755 /usr/local/bin/raporo-entrypoint

# /app stays root-owned and world-readable: the runtime user needs to *read*
# the application, never to write it, and code a compromised process can
# rewrite is free persistence across a restart. Nothing here writes into /app —
# PYTHONDONTWRITEBYTECODE keeps __pycache__ out, static files are served from
# STATICFILES_DIRS, and the state lives in Postgres. MEDIA_ROOT and (once a
# deploy sets it) STATIC_ROOT must therefore stay outside /app; both are
# recorded as ops invariants in the slice-1 handoff. In dev the `.:/app` bind
# mount replaces this directory with the host checkout (owned by the
# developer, uid 1000 = the `raporo` user), which is what makes pytest's and
# ruff's caches writable there and only there.
RUN useradd -m raporo
USER raporo

ENTRYPOINT ["raporo-entrypoint"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# --- dev: adds the Postgres client and the test suite ------------------------
# `manage.py dbshell` shells out to `psql`, which python:3.14-slim does not
# ship. It never reaches the `runtime` target: compose builds this one, a
# pipeline builds `runtime` explicitly.
#
# The client major version must match the `db` service, and Debian trixie only
# packages 17 (`postgresql-client` there is 17+278) while the server is now 18.
# Both halves of that mismatch were measured against a live 18.6 server rather
# than assumed:
#   * psql 17 connects and queries fine, but an *interactive* session — which
#     is what `manage.py dbshell` is — opens with "WARNING: psql major version
#     17, server major version 18. Some psql features might not work." on every
#     invocation. (Piped or `-c` invocations print no banner, so this is
#     friction rather than breakage.)
#   * pg_dump 17 refuses outright: "aborting because of server version
#     mismatch". That is the tool a backup-restore rehearsal reaches for, and
#     it is the half that is not cosmetic.
# So the client comes from PGDG.
#
# It is taken out of the `postgres:18` image rather than fetched over the
# network, because the usual recipe — curl the PGDG signing key from
# apt.postgresql.org and write a sources list — means adding curl/gnupg and
# ca-certificates to this stage and trusting a key whose provenance is a URL at
# build time. postgres:18 already carries both files, and it is the exact image
# compose runs as the server, at the same tag, already pulled. apt still
# verifies everything it downloads against that key; only the key's delivery
# changed. `COPY --from=<image>` is a classic-builder feature, so the "no
# `syntax=` directive needed" note at the top of this file still holds.
#
# `tests/` and the two tool configs are added *here*, so the suite can be run
# from the image itself without the bind mount, and so no shipped layer ever
# contains them. COPY writes as uid 0 whatever USER is in effect, which keeps
# the root-owned-/app invariant above.
FROM base AS dev

USER root
COPY --from=postgres:18 /usr/local/share/keyrings/postgres.gpg.asc /usr/local/share/keyrings/postgres.gpg.asc
COPY --from=postgres:18 /etc/apt/sources.list.d/pgdg.list /etc/apt/sources.list.d/pgdg.list
# postgresql-client-18 pulls in postgresql-client-common, which is what puts
# `psql` on PATH (as pg_wrapper, which dispatches to the newest installed
# major) — that is the name `manage.py dbshell` executes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*
USER raporo

COPY pytest.ini ruff.toml ./
COPY tests/ ./tests/

# --- runtime: PLACEHOLDER, not a deployable image ----------------------------
# This stage is *not* production-ready and nothing deploys it today. It
# inherits `CMD python manage.py runserver`, which is Django's development
# server: single-threaded-ish, unhardened, and documented by Django itself as
# unfit to serve a site. There is no WSGI/ASGI server in requirements.txt.
#
# That is deliberate for slice 1. Adding gunicorn now would add a dependency
# with no consumer and no reviewed configuration (workers, timeouts, graceful
# shutdown, access-log format, proxy headers) — decisions that belong with the
# deploy task, alongside `SECURE_PROXY_SSL_HEADER`/`CSRF_TRUSTED_ORIGINS`,
# `STATIC_ROOT` and a production compose manifest, which are already carried
# forward to it.
#
# The stage exists now only so that `--target runtime` is a stable name in a
# pipeline and so the "what does a shipped image contain" question has one
# answer: **exactly `base`, byte for byte.** It adds no instruction on
# purpose. `base` is the enumerated list above — no psql, no apt-get layer, no
# tests, no pytest.ini/ruff.toml, non-root, /app not writable by the runtime
# user. Verifiable with `docker save`, not just asserted here: nothing that is
# absent was ever copied in and then deleted. (The three
# `deb.debian.org_*_InRelease` files under /var/lib/apt/lists come from
# upstream python:3.14-slim, not from us.)
# Whoever picks up the deploy task owns replacing the CMD with a real server
# and deleting this paragraph.
FROM base AS runtime
