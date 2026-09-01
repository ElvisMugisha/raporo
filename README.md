# Raporo

Sales reporting for Rwandan shops and small businesses. Sellers record what actually happens — a
sale, a restock, an order, a payment — and Raporo does the arithmetic and produces the daily,
weekly, biweekly or monthly report owners used to type by hand: accurate, branded with the shop's
logo, and ready to share on WhatsApp. Multi-store organizations, credit tracking, and investment
cycles are all v1 scope. Base currency Rwf; English, Kinyarwanda and French from the start. Full
brief: [docs/PRODUCT.md](docs/PRODUCT.md).

**Status:** in build. Slice 1 (foundation) is underway — the data layer and a healthcheck endpoint
exist; there is no UI yet. [docs/ROADMAP.md](docs/ROADMAP.md) is the live tracker.

## Run the app

```bash
git clone <repo-url> && cd raporo
cp .env.example .env
docker compose build
docker compose up --wait     # migrates on boot, then serves http://localhost:8000/healthz
```

Tests, lint, settings modules, environment variables, and troubleshooting:
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Work on it with the AI team

This repo carries a full Claude Code setup — agents, skills, rules, settings, MCP config, bootstrap.
Clone it anywhere and the whole team works identically.

```bash
./scripts/setup.sh   # installs Claude Code + Headroom, wires the token-saving proxy
claude               # team, skills, rules load automatically
```

Details, portability rules, and troubleshooting: [docs/SETUP.md](docs/SETUP.md).

## How this repo is organized

```text
CLAUDE.md              # project rules & principles (loaded every AI session)
manage.py              # Django entrypoint
compose.yaml           # dev stack: web (Django 6.1) + db (Postgres 17)
docker/                # application image + container entrypoint
requirements.txt       # pinned Python dependencies
config/                # Django project: settings/{base,dev,test,prod}.py, urls, wsgi/asgi
common/                # cross-cutting bases, managers, system checks, validators
apps/
  accounts/            # users (username / email / phone)
  orgs/                # organizations, stores, roles, memberships
  audit/               # append-only audit trail
templates/  static/    # Django templates + HTMX assets (no Node build)
locale/                # en / rw / fr translation catalogues
tests/                 # test suite
.claude/
  agents/              # the AI team: 19 senior roles — delivery, gates, advisory
                       #   (roster in CLAUDE.md, pipeline in /new-feature)
  skills/              # workflows: /new-feature, /bug-fix, /production-readiness, /adr
                       #   + vendored design/browser skills (see skills/VENDORED.md)
  settings.json        # shared permissions & settings
.mcp.json              # project-scoped MCP servers
docs/
  PRODUCT.md           # product brief (the decisions)
  ROADMAP.md           # living tracker: phases, slices, where we are now
  DEVELOPMENT.md       # running, testing, and developing the application
  SETUP.md             # AI-team setup on a new machine
  adr/                 # architecture decision records
scripts/
  setup.sh             # idempotent bootstrap for a fresh machine
```

Everything above is committed — clone it and both the application and the AI-team setup work
identically anywhere. The `.claude/` half doubles as a starter template for new projects
(see the last section of [docs/SETUP.md](docs/SETUP.md)).

## Development rules (short version)

- Branch off `dev`; `main` is always releasable.
- Build features via `/new-feature`; fix defects via `/bug-fix`.
- Nothing merges to `main` without `/production-readiness` saying **SHIP**.
- Architectural decisions get an ADR (`/adr`).
