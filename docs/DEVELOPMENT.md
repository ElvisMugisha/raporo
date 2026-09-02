# Running and developing Raporo

Everything runs in Docker. You do not need Python, Postgres, or Node on your machine. The `web`
container has the interpreter and dependencies; the `db` container has Postgres 17.

> **State of the app (2026-09-01).** Slice 1 (foundation) is in progress. What exists today is the
> data layer — users, organizations, stores, roles, the audit trail — plus a healthcheck endpoint.
> There is no login page, no UI, and no seed data yet. Screens start arriving in slice 1's
> remaining tasks; [ROADMAP.md](ROADMAP.md) says what lands when.

## Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Docker Engine | 24+ | Verified on 29.7.2 |
| Docker Compose | v2+ (the `docker compose` plugin, not `docker-compose`) | Verified on v5.5.0 |
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
`/healthz`, so that one command gets you from an empty volume to a serving app.

Allow up to two minutes the first time. Postgres has to initialise its data directory before Django
can connect, and the healthchecks budget that much on purpose (`start_period`: db 60s, web 120s).
Measured runs land between 12 and 40 seconds on an already-built image, and `up --wait` returns on
the first successful probe, so a fast boot costs nothing. The budget is sized for a loaded machine,
not the typical one.

**About that `down -v`.** It destroys the `pgdata` volume. You need it only if the machine already
has a Raporo database volume created before `AUTH_USER_MODEL` was introduced: that history makes
`accounts.0001_initial` unapplicable, and migrations fail with a swappable-model error. A fresh
clone has no volume yet, so skip the line. One-time per machine, not part of the normal loop.

**One gap in `.env.example`.** `DJANGO_MEDIA_ROOT` is not in it. Development works without it
(`config/settings/base.py` falls back to `/var/tmp/raporo-media`), but `config/settings/prod.py`
reads it with no fallback, so a production deploy refuses to boot until it is set. Add the line by
hand; automation is blocked from writing `.env*` files on purpose.

## How the container boots

`docker/entrypoint.sh` is the image `ENTRYPOINT`, and its pre-boot sequence is the **default**.
Unless the command is on an explicit list of known one-off tooling, the container runs:

1. `manage.py check`, so a misconfigured app refuses to serve instead of half-working.
2. `manage.py migrate --noinput`, but **only if `RAPORO_AUTO_MIGRATE=1` and the command is a
   recognised server** — or `RAPORO_ROLE=server` says it is one. `compose.yaml` sets
   `RAPORO_AUTO_MIGRATE` for local dev. Deployments leave it unset and run `migrate` as their own
   reviewed step, because with more than one replica every new container would race the same
   migration lock, and an unreviewed schema change would ship with whatever code happened to carry
   it.

Everything the entrypoint prints about its own work goes to **stderr**, prefixed `entrypoint:`.
Django's `check` and `migrate` output goes to stdout, ahead of whatever your command prints.

The exempt list is matched *after* the entrypoint has resolved `python -m …` and `sh -c '…'`
wrappers down to the payload that will really run. What each command shape gets:

| Command shape | Pre-boot sequence |
| --- | --- |
| `pytest`, `py.test`, and any wrapper that resolves to one: `python -m pytest`, `bash -c 'pytest -q'` | **never**, whatever `RAPORO_ROLE` says |
| `ruff` | skipped |
| `bash` (or `sh`, `dash`, `ash`, `zsh`) **with no arguments** — a debug shell | skipped |
| `python manage.py <subcommand>`, anything except the three below | skipped |
| `python manage.py runserver` / `runserver_plus` / `testserver` | `check` + `migrate` |
| a resolvable wrapper around other exempt tooling: `python -m ruff`, `bash -c 'ruff check .'` | skipped |
| a wrapper it cannot resolve: `sh -c 'gunicorn …'`, `bash -c 'ruff check . && pytest -q'`, `bash -lc '…'`, `python -c '…'` | `check` only |
| anything else: `gunicorn`, `uvicorn`, `daphne`, `granian`, `coverage`, and whatever replaces them | `check` only |

`pytest` is on a never-list checked *before* `RAPORO_ROLE`, so no environment can turn a test run
into a migration: it creates and drops its own database, and a `migrate` here would land on the
*development* one. A command that only reached the guard because nothing recognised it gets
`check`, which is read-only, but not `migrate`; the
[`RAPORO_AUTO_MIGRATE` note](#environment-variables) says why that distinction exists.

Matching is on the basename of the **resolved payload**, not of the first argument. The resolver
walks `python [opts] -m MOD`, `python [opts] script.py` and `sh -c '<one simple command>'`, and it
is deliberately conservative: `python -c`, `python -`, a bare interpreter, `bash -lc`, a shell
given a script path or extra arguments, and any `-c` string containing a character outside
`[[:alnum:][:blank:]_.,:/=+-]` (so anything with `;`, `&&`, `|`, a redirection, a glob, quoting or
`$`) all resolve to *nothing*. Nothing resolved counts as unknown, and unknown is guarded. So
`/usr/local/bin/gunicorn` in a Kubernetes `command:` is the same command as bare `gunicorn`, and
`python -m pytest` the same as bare `pytest`, while `bash -c 'pytest -q && ruff check .'` is
neither.

The polarity is that way round deliberately. An earlier version of the script asked "does this look
like a server?" and ran the sequence if so, which was wrong in both directions:
`pytest -k runserver` migrated the live development database, while `sh -c "gunicorn …"` started a
server with no check and no migrate at all. Failing closed means an unfamiliar command is noisy
rather than unguarded, and `RAPORO_ROLE` (see [Environment variables](#environment-variables)) is
the override for the cases inference cannot reach.

An exempt command is exec'd in silence, with no entrypoint output whatsoever. Admin commands are on
that list for the same kind of reason as tests: a one-off `manage.py shell` must never silently
mutate the schema. The price of the default is that `python -c`, and any `bash -c` payload the
resolver will not touch, pay for a `check`: a second or two, read-only, no schema change.
`bash -c '<one known tool>'` pays nothing. See [Troubleshooting](#troubleshooting) for the opt-out.

The `web` service has its own healthcheck against `/healthz`, which is what makes
`docker compose up --wait` mean "migrated and answering requests" rather than "process started".
That makes it the right command in CI and in any script that needs the app ready before it does
something.

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

Reach for `manage.py migrate` after pulling a branch that adds migrations. It is not a first-run
step.

Use `docker compose run --rm` when the stack is down (it starts `db` for you and throws the
container away afterwards) and `docker compose exec web ...` when it is already up. `exec` never
invokes the `ENTRYPOINT`, which occasionally matters; see the fail-closed entry in
[Troubleshooting](#troubleshooting).

`dbshell` works because `compose.yaml` builds the Dockerfile's `dev` target, which adds
`postgresql-client` on top of the shared `base` stage. That costs about 59 MB (the Debian package
drags in perl and krb5), which is why the other target, `runtime`, does not carry it.

`runtime` is a placeholder, not a deployable image, and nothing deploys it today. It inherits
`CMD python manage.py runserver` — Django's development server, which Django itself documents as
unfit to serve a site — and `requirements.txt` has no WSGI/ASGI server yet. The stage exists so
that `--target runtime` is a stable name for a pipeline, and so "what is in a shipped image" has
one answer: `base`, no psql, no apt layer, no `tests/`, non-root, `/app` not writable by the
runtime user. A real server, and the reviewed configuration that goes with it (workers, timeouts,
graceful shutdown, access-log format, proxy headers), lands with the deploy task.

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
services; `.env.example` is the committed template. The two `RAPORO_*` variables are the exceptions:
`RAPORO_AUTO_MIGRATE` is set in `compose.yaml` itself, and `RAPORO_ROLE` is set nowhere — you pass
it per command when you need it. Never commit real values.

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
| `RAPORO_AUTO_MIGRATE` | `docker/entrypoint.sh` | No, and deliberately unset outside dev | `1` makes the entrypoint run `manage.py migrate` before starting the server — but only when the command is a recognised server, or `RAPORO_ROLE=server` says it is one. Anything else is told on stderr why it did not migrate. `compose.yaml` sets it for local dev, where there is one container and the alternative is a broken first run. Off by default everywhere else, for the reasons in [How the container boots](#how-the-container-boots). |
| `RAPORO_ROLE` | `docker/entrypoint.sh` | No, and set nowhere by default | Overrides the entrypoint's inference about whether a command needs the pre-boot sequence. `server` forces it (deploys, and launchers this script has never heard of) — except for `pytest`/`py.test`, which are never pre-booted and say so on stderr; `tooling` skips it and prints `entrypoint: RAPORO_ROLE=tooling — skipping pre-boot sequence for: <argv[0]>` on stderr so the skip is never silent; unset means infer from the command. Any other value exits 64 rather than being ignored, so a typo fails loudly instead of quietly picking a default. Pass it per command: `docker compose run --rm -e RAPORO_ROLE=tooling web python -c '…'`. |

> **`RAPORO_ROLE=server` goes in `compose.prod.yaml`, never in `compose.yaml`.** `docker compose
> run --rm web <cmd>` inherits the whole service environment, and `compose.yaml` is the file every
> one-off command inherits from — so `RAPORO_ROLE=server` there would force `check` + `migrate`
> onto a `manage.py shell`, a `ruff` run, an ad-hoc script. The never-list protects `pytest` from
> exactly this, and nothing else. In production the service really is a server and one-off jobs are
> their own spec, so nothing inherits a role it shouldn't; the never-list is what makes an inherited
> `server` role safe there.

> **`RAPORO_AUTO_MIGRATE=1` *is* in the `web` service's `environment:`, and every `docker compose
> run` does inherit it.** What makes that safe is the server gate, not luck: an inherited
> `RAPORO_AUTO_MIGRATE=1` is permission to migrate *when this container is a server*, and a command
> that only reached the guard because nothing recognised it is not one. It gets `check` and a stderr
> line explaining the skip. That is why the two variables can live in different places.

## Project layout

```text
manage.py              # Django entrypoint
compose.yaml           # dev stack: web (Django) + db (Postgres 17), pgdata volume, healthchecks
docker/Dockerfile      # python:3.13-slim, non-root `raporo` user; `dev` target (compose builds
                       #   this one) adds psql; `runtime` target is a placeholder, nothing
                       #   deploys it yet
docker/entrypoint.sh   # pre-boot check + opt-in, server-only migrate for everything except an
                       #   exempt tooling list (pytest — never — ruff, admin commands, bare shell)
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

Two rules before you touch `apps/`: business data is store-scoped and reached through `for_store()`
/ `for_stores()`, and nothing is ever hard-deleted. `common/managers.py` and `common/checks.py`
enforce both, and they raise at you rather than let a cross-tenant query run.

One more, if you add a **new top-level Python package**: add it to the `COPY` list in
`docker/Dockerfile`'s `base` stage. The stage copies path by path and `runtime` adds nothing on top,
so a package nobody lists is absent from the shipped image, and the dev `.:/app` bind mount hides
that locally until the first deploy fails. The `COPY` block says so itself; this is the pointer.

## Troubleshooting

**Migrations fail with a swappable-model / `AUTH_USER_MODEL` error on a new machine.**
An old `pgdata` volume predates the custom user model. Run `docker compose down -v`, then
`docker compose up --wait`; the entrypoint migrates the fresh volume. One-time per machine. Give it
up to two minutes before you decide it has hung: the healthchecks budget that much
(`start_period`: db 60s, web 120s), because a cold `initdb` is around 30 seconds of real work
before Django can connect at all. Measured on this project with an already-built image:
12s and 28s on two runs from an empty volume. The run that forced those budgets up took
102s, on a machine that was also running three test suites at the time — the case the budget
exists for, not the typical one.

**A one-off command runs `check` before it starts.**
Expected, not broken tooling. The entrypoint fails closed, so anything it cannot recognise as
one-off tooling gets a `check`, including `docker compose run --rm web python -c '...'` and
`docker compose run --rm web bash -c 'ruff check . && pytest -q'`. A shell payload the resolver
cannot reduce to one known command could be wrapping a server, and `python -c` could be anything.
`check` is read-only, costs a second or two, and its output lands on stdout ahead of yours. It will
not migrate — see the next entry. Three ways past it:

- opt out for the one command: `docker compose run --rm -e RAPORO_ROLE=tooling web python -c '...'`
- give the shell **one** simple command rather than a chain: `... web bash -c 'ruff check .'` is
  exempt, `... web bash -c 'ruff check . && pytest -q'` is not. Unwrapping it to
  `... web ruff check .` is exempt as well
- `docker compose exec web ...` when the stack is up: `exec` bypasses the `ENTRYPOINT` entirely and
  is unaffected

**`entrypoint: RAPORO_AUTO_MIGRATE=1 but this is not a recognised server — skipping migrate`.**
Not a bug, and the entrypoint line you are most likely to meet. `compose.yaml` sets
`RAPORO_AUTO_MIGRATE=1` on the `web` service and `docker compose run` inherits the service
environment, so every one-off command arrives carrying migrate permission it has no business
spending. The entrypoint wants a recognised *server* as well before it migrates, and tells you it
declined instead of migrating quietly. Your command ran; the schema was untouched. If you did want
the migration, run it as itself — `docker compose run --rm web python manage.py migrate` — or pass
`-e RAPORO_ROLE=server`, which is what the hint at the end of the message means.

**`web` exits or never turns healthy, and the log shows an entrypoint line.**
The entrypoint's own output goes to stderr, prefixed `entrypoint:`; `docker compose logs web` shows
it merged with Django's. A failing `running manage.py check` is a settings or model problem, not a
database one. `RAPORO_AUTO_MIGRATE not set — skipping migrate` in local dev means your
`compose.yaml` is not the committed one (an override may have replaced the `environment:` block);
run `docker compose config` to see the merged result. Seeing *no* `entrypoint:` lines at all is not
a symptom — an exempt command prints nothing by design.

Three more lines mean "I did not recognise this, so I guarded it". None is an error on its own;
each tells you which branch you landed in:

- `unrecognised command 'X' — running pre-boot checks (fail closed)` — argv resolved cleanly to
  `X` (the payload basename, so `granian` for `python -m granian`), and nothing on the tooling list
  matched it. A server this script has never heard of, or `coverage`.
- `could not resolve what 'X' will run — running pre-boot checks (fail closed)` — argv resolved to
  nothing, so `X` is only the wrapper it started from. `python -c`, `bash -lc`, and a `-c` string
  holding a pipe or an `&&` all land here.
- `ignoring RAPORO_ROLE=server: a test runner is never pre-booted` — something set
  `RAPORO_ROLE=server` in an environment a `pytest` run inherited. The test run is fine, but go and
  find it: every *other* command in that environment is being pre-booted, `manage.py shell`
  included.

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
That is `common.E100`, which is active only under `prod.py`. The append-only trigger waives its
TRUNCATE guard for `test_*` databases so Django can tear down test databases, and a production
database with that name would silently inherit the waiver. Rename the database.
