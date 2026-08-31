# Setting up on a new machine

Everything portable lives in git. Only two things are machine-level and installed by the bootstrap script: the Claude Code CLI and Headroom.

## Fresh machine, three commands

```bash
git clone <repo-url> && cd raporo
./scripts/setup.sh
headroom wrap claude --code-memory none
```

Then just run `claude`. Verify anytime with `./scripts/setup.sh --check`.

## What travels with the repo (nothing to reinstall)

| Piece | Location | Loaded by Claude Code |
|---|---|---|
| Rules & principles | `CLAUDE.md` | every session, automatically |
| Team (subagents) | `.claude/agents/*.md` | automatically |
| Workflows (skills) | `.claude/skills/*/SKILL.md` | as `/new-feature`, `/bug-fix`, `/production-readiness`, `/adr` |
| Shared settings & permissions | `.claude/settings.json` | automatically |
| MCP servers (when we add any) | `.mcp.json` | automatically (approve on first use) |
| Bootstrap | `scripts/setup.sh` | run manually once per machine |

Machine-local overrides go in `.claude/settings.local.json` and `CLAUDE.local.md` — both gitignored, never committed.

## Headroom (token compression)

Headroom is a local proxy that compresses tool outputs, file contents, and history before they reach the Anthropic API (roughly 15–20% savings for coding agents per their benchmarks), while preserving prompt-cache prefixes. It's a Python CLI, so it lives on the machine, not in the repo — `scripts/setup.sh` installs it via `uv`.

- `headroom wrap claude --code-memory none` — one-time per machine; routes the `claude` CLI through the proxy. We pass `--code-memory none` to keep the setup clean (skips Headroom's optional user-scoped Serena install, which would violate our everything-in-the-repo rule).
- `headroom unwrap claude` — restore direct connection.
- If Claude ever fails to connect after a reboot, the proxy isn't running: re-run `headroom wrap claude` or `headroom proxy --port 8787`.

## Requirements

- Linux, WSL2 (our primary: Ubuntu 24.04), or macOS
- Python ≥ 3.10 available (uv fetches 3.13 for Headroom automatically)
- `curl`, `git`

## Reusing this setup for a new project

This repo doubles as a starter template. For a new project, copy: `CLAUDE.md`, `.claude/`, `.mcp.json`, `scripts/setup.sh`, `docs/adr/0001-*.md`, and this file — then edit CLAUDE.md's project-specific sections. (Or make this repo a GitHub template and click "Use this template".)
