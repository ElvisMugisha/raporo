# Raporo

> Product description goes here once the stack/scope is decided (see `docs/adr/`).

## Quick start (any machine)

```bash
git clone <repo-url> && cd raporo
./scripts/setup.sh   # installs Claude Code + Headroom, wires the token-saving proxy
claude               # start working — team, skills, rules load automatically
```

Details, portability rules, and troubleshooting: [docs/SETUP.md](docs/SETUP.md).

## How this repo is organized

```text
CLAUDE.md              # project rules & principles (loaded every AI session)
.claude/
  agents/              # the AI team: architect, code-reviewer, security-auditor,
                       #   test-engineer, devops-engineer, docs-writer
  skills/              # workflows: /new-feature, /bug-fix, /production-readiness, /adr
  settings.json        # shared permissions & settings
.mcp.json              # project-scoped MCP servers (currently none)
docs/
  SETUP.md             # new-machine setup guide
  adr/                 # architecture decision records
scripts/
  setup.sh             # idempotent bootstrap for a fresh machine
```

Everything above is committed — clone it and the entire AI-team setup works identically anywhere. This structure is also designed to be copied as a starter template for new projects.

## Development rules (short version)

- Branch off `dev`; `main` is always releasable.
- Build features via `/new-feature`; fix defects via `/bug-fix`.
- Nothing merges to `main` without `/production-readiness` saying **SHIP**.
- Architectural decisions get an ADR (`/adr`).
