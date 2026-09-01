# Running and developing Raporo

Everything runs in Docker. You do not need Python, Postgres, or Node on your machine. The `web`
container has the interpreter and dependencies; the `db` container has Postgres 17.

> **State of the app (2026-09-01).** Slice 1 (foundation) is still in progress. What exists today
> is the data layer — users, organizations, stores, roles, the audit trail — plus a healthcheck
> endpoint. There is no login page, no UI, and no seed data yet. Screens start arriving in slice 1's
> remaining tasks; see [ROADMAP.md](ROADMAP.md) for what lands when.

## Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Docker Engine | 24+ | Verified on 29.6.2 |
| Docker Compose | v2+ (the `docker compose` plugin, not `docker-compose`) | Verified on v5.3.1 |
| git | any recent | |

On WSL2, run everything from inside the Linux filesystem (`~/projects/...`), not `/mnt/c`: bind
mounts across the Windows boundary are slow enough to be noticeable on every test run.

## First run on a new machine

```bash
git clone <repo-url> && cd raporo
cp .env.example .env          # compose declares `env_file: .env`; nothing starts without it
docker compose build
docker compose down -v        # see the note below: one-time, only on a machine that has run
                              # this project before with an older database volume
docker compose up --wait      # starts Postgres, migrates, serves on http://localhost:8000
docker compose run --rm web pytest -q
```

There is no separate `migrate` step. The image entrypoint runs `manage.py check` and then
`manage.py migrate` before the server starts, and `up --wait` returns only once the app answers on
`/healthz`, so that one command gets you from empty volume to serving app. Allow about 90 seconds
the first time: Postgres has to initialise its data directory before Django can connect.

**About that `down -v`.** It destroys the `pgdata` volume. You only need it if the machine already
has a Raporo database volume created before `AUTH_USER_MODEL` was introduced. That history makes
`accounts.0001_initial` unapplicable, and migrations fail with a swappable-model error. On a genuinely
fresh clone there is no volume yet and you can skip the line. It is a one-time-per-machine fix, not
part of the normal loop.

**One thing `.env.example` is still missing.** `DJANGO_MEDIA_ROOT` is not in it yet. Development
works without it (`config/settings/base.py` falls back to `/var/tmp/raporo-media`), but
`config/settings/prod.py` reads it with no fallback, so a production deploy will refuse to boot until
it is set. Add a line for it to `.env.example` by hand (automation is blocked from writing `.env*`
files on purpose).

## How the container boots

`docker/entrypoint.sh` is the image `ENTRYPOINT`. It only does anything when the container command
starts an application server (`runserver`, `gunicorn`, `uvicorn`, `daphne`):

1. `manage.py check` always runs, so a misconfigured app refuses to serve instead of half-working.
2. `manage.py migrate --noinput` runs **only if `RAPORO_AUTO_MIGRATE=1`**. `compose.yaml` sets that
   for local dev. Deployments leave it unset and run `migrate` as their own reviewed step, because
   with more than one replica every new container would race the same migration lock, and an
   unreviewed schema change would ship with whatever code happened to carry it.

Every other command is exec'd untouched with no pre-boot sequence: `pytest`, `ruff`,
`manage.py <anything>`, `bash`. Tests create and drop their own database, and a one-off
`manage.py shell` must never silently mutate the schema.

The `web` service also has a healthcheck against `/healthz`, which is why `docker compose up --wait`
means "migrated and answering requests" rather than "process started". That makes it the right
command in CI and in any script that needs the app ready before it does something.

## The everyday loop

Run the app:

```bash
docker compose up                        # http://localhost:8000, Ctrl-C to stop
docker compose up -d                     # ...or detached
docker compose up --wait                 # detached, and blocks until /healthz answers
docker compose logs -f web               # follow the server log
docker compose down                      # stop (keeps the database volume)
```

`runserver` picks up code changes automatically: the repo is bind-mounted at `/app`, so editing a
file on the host reloads the server in the container. There is no build step for templates, CSS, or
JS (Django templates + HTMX, no Node toolchain).

What you can hit today:

| URL | What it does |
| --- | --- |
| `/healthz` | Returns `{"status": "ok"}`. Used by deploy health probes. |
| `/admin/` | Django admin. Reachable, but no application models are registered yet, so it is empty. |
| `/` | 404 — no home page exists yet. Expected, not a broken install. |

Run the checks. All four should be clean before you push:

```bash
docker compose run --rm web pytest -q                                    # ~30s on an idle machine
docker compose run --rm web ruff check .
docker compose run --rm web python manage.py check
docker compose run --rm web python manage.py makemigrations --check --dry-run
```

`makemigrations --check --dry-run` fails if a model change has no migration. Treat a failure as
"you forgot to generate a migration", not as a flaky check.

Narrow the test run while you work:

```bash
docker compose run --rm web pytest tests/test_orgs_models.py -q
docker compose run --rm web pytest tests/test_healthz.py::test_healthz_returns_ok -q
docker compose run --rm web pytest -q -k "soft_delete"
docker compose run --rm web pytest -q --reuse-db      # skip the create/drop of the test database
```

`--reuse-db` is safe until a migration changes; after that, add `--create-db` once.

Other things you will want:

```bash
docker compose run --rm web python manage.py makemigrations <app>
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py shell
docker compose run --rm web python manage.py dbshell             # SQL prompt on the dev database
docker compose run --rm web bash                                 # poke around the container
```

`manage.py migrate` is still there when you want it (after pulling a branch that adds migrations,
say). It is no longer needed as a separate first-run step.

Use `docker compose run --rm` when the stack is down (it starts `db` for you and throws the
container away afterwards) and `docker compose exec web ...` when it is already up.

`dbshell` works because `compose.yaml` builds the Dockerfile's `dev` target, which adds
`postgresql-client` on top of the deployable image. The default `runtime` target does not carry it:
a psql client in a production image is 59 MB of attack surface that nothing there needs.

## Settings modules

`config/settings/` is split by environment. Every module inherits from `base`.

| Module | Used by | What it changes |
| --- | --- | --- |
| `config.settings.base` | nothing directly | Apps, middleware, database, i18n (en/rw/fr), upload limits, argon2 hashing, secure cookies. Reads `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` with no fallback, so it fails fast when `.env` is missing. |
| `config.settings.dev` | the `web` service (`compose.yaml` sets `DJANGO_SETTINGS_MODULE`) | `DEBUG=True`, `ALLOWED_HOSTS=["*"]`, cookie `Secure` flags off so plain HTTP works locally. |
| `config.settings.test` | the test suite (`pytest.ini` pins `--ds=config.settings.test`, which wins over the compose variable) | `dev` plus `tests.testapp`, a throwaway app holding concrete stand-ins for the abstract bases in `common/`. Installed only here, so its tables never reach a real database and `makemigrations --check` stays clean. |
| `config.settings.prod` | deployments | Requires `DJANGO_MEDIA_ROOT`, reads `DJANGO_ALLOWED_HOSTS`, turns on HSTS/SSL redirect, and enables the `common.E100` check that refuses a database named `test_*` (the entrypoint's `manage.py check` is what makes that guard bite before the server starts). Not used locally. |

## Environment variables

Everything down to `DJANGO_ALLOWED_HOSTS` lives in `.env` (gitignored) and is loaded by both compose
services; `.env.example` is the committed template. `RAPORO_AUTO_MIGRATE` is the exception — it is
set in `compose.yaml` itself, not in `.env`. Never commit real values.

| Variable | Read by | Required? | What it is |
| --- | --- | --- | --- |
| `DJANGO_SECRET_KEY` | `base.py` | Yes, everywhere | Django's signing key (sessions, CSRF, password-reset tokens). Any random string locally; a unique high-entropy value per deployment. |
| `POSTGRES_PASSWORD` | `base.py`, and the `db` image on first init | Yes, everywhere | Password for the application database role. |
| `POSTGRES_DB` | `base.py` (defaults to `raporo`), and the `db` image | Yes in practice | Database name. The `db` container creates it on first start of an empty volume. |
| `POSTGRES_USER` | `base.py` (defaults to `raporo`), the `db` image, and the compose healthcheck (`pg_isready -U`) | Yes in practice | Database role. Leaving it unset makes the `db` healthcheck fail, so `web` never starts. |
| `POSTGRES_HOST` | `base.py` (defaults to `db`) | No | Hostname of Postgres. `db` is the compose service name; override only when pointing at an external database. |
| `POSTGRES_PORT` | `base.py` (defaults to `5432`) | No | Postgres port. |
| `DJANGO_MEDIA_ROOT` | `base.py` (defaults to `/var/tmp/raporo-media`), `prod.py` (no fallback) | Production only | Where uploads (organization logos today) are written. Deliberately outside the source tree so uploaded files can never be served as static content. Not yet in `.env.example`. |
| `DJANGO_ALLOWED_HOSTS` | `prod.py` | Production only | Comma-separated hostnames. Ignored in dev, which allows `*`. |
| `RAPORO_AUTO_MIGRATE` | `docker/entrypoint.sh` | No, and deliberately unset outside dev | `1` makes the entrypoint run `manage.py migrate` before starting the server. `compose.yaml` sets it for local dev, where there is one container and the alternative is a broken first run. Off by default: with several replicas a rolling deploy would have every container racing the same migration lock, and an unreviewed schema change would ship with whatever code carried it. |

## Project layout

```text
manage.py              # Django entrypoint
compose.yaml           # dev stack: web (Django) + db (Postgres 17), pgdata volume, healthchecks
docker/Dockerfile      # python:3.13-slim, non-root `raporo` user; `dev` target adds psql,
                       #   `runtime` target is what deploys
docker/entrypoint.sh   # pre-boot check + opt-in migrate for server commands only
requirements.txt       # pinned dependencies (Django 6.1, psycopg 3, argon2, pyotp, Pillow, pytest, ruff)
pytest.ini             # pins the test settings module
ruff.toml              # lint + import-order config (line length 100, target py313)

config/                # project config: settings/{base,dev,test,prod}.py, urls.py, wsgi.py, asgi.py
common/                # cross-cutting building blocks, no concrete models of its own
  models.py            #   abstract bases (soft delete, audit stamps, store scoping)
  managers.py          #   query-layer enforcement: store scoping, no hard deletes
  checks.py            #   system checks that keep tenant isolation structurally true
  db.py                #   reusable PostgreSQL guards (append-only triggers) used from migrations
  validators.py        #   shared field validators (phone, username, image uploads, timezone)
apps/
  accounts/            #   User (AUTH_USER_MODEL): username / email / phone, all three log-in-able
  orgs/                #   the tenancy spine: Organization, Store, Role, Membership, StoreAccess
  audit/               #   append-only audit trail (write once, then read only)
tests/                 # the suite, plus `testapp/` — concrete stand-ins for common/'s abstract bases
templates/             # Django templates (empty until the UI slices)
static/                # static assets (empty until the UI slices)
locale/                # translation catalogues for en / rw / fr
docs/                  # this guide, PRODUCT.md, ROADMAP.md, adr/
```

Two rules worth knowing before you touch `apps/`: business data is store-scoped and reached through
`for_store()` / `for_stores()`, and nothing is ever hard-deleted. `common/managers.py` and
`common/checks.py` enforce both, and they will raise at you rather than let a cross-tenant query run.

## Troubleshooting

**Migrations fail with a swappable-model / `AUTH_USER_MODEL` error on a new machine.**
An old `pgdata` volume predates the custom user model. `docker compose down -v`, then
`docker compose up --wait` — the entrypoint migrates the fresh volume. One-time per machine.

**`web` exits or never turns healthy, and the log shows an entrypoint line.**
Read the two lines the entrypoint prints. `running manage.py check` failing means a settings or
model problem, not a database one. `RAPORO_AUTO_MIGRATE not set — skipping migrate` in local dev
means your `compose.yaml` is not the committed one (an override may have replaced the
`environment:` block); run `docker compose config` to see the merged result.

**`docker compose up` fails with "port is already allocated" (8000).**
Something else holds the port, often a previous detached run. `docker compose down` first; if it
persists, find the owner with `ss -ltnp | grep :8000`. To move the app to another port, create a
`compose.override.yaml` (gitignored, so it stays machine-local and Compose picks it up
automatically):

```yaml
services:
  web:
    ports: !override
      - "8011:8000"
```

The `!override` tag matters. Compose merges list values by appending, so without it you get *both*
mappings and the 8000 conflict is still there.

**A new dependency is not there / `ModuleNotFoundError` after pulling.**
`requirements.txt` is baked into the image. Run `docker compose build` after any change to it, or
after a pull that touched it. The source bind mount does not cover installed packages.

**`DJANGO_MEDIA_ROOT` missing in production.**
`prod.py` reads it with no fallback and the process exits at import time. Set it on the host to a
durable path outside the source tree.

**Tests fail during database setup right after another test run.**
The previous `--rm` container can still hold a connection while the next run tries to drop and
recreate the test database. Re-run, or use `--reuse-db` to skip the create/drop cycle.

**`manage.py check` complains about a database named `test_*`.**
That is `common.E100`, on only under `prod.py`. The append-only trigger waives its TRUNCATE guard
for `test_*` databases so Django can tear down test databases; a production database with that name
would silently inherit the waiver. Rename the database.
