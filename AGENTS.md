# AGENTS.md

A pointer, not a second source of truth. Everything here either says where something lives or
states a rule that belongs nowhere else. If you find yourself wanting to copy a role definition
or an engineering rule into this file, don't — two copies of a rule means one of them is wrong
and nobody knows which.

## What this repo is

Raporo: period-based sales reporting for Rwandan retail businesses, plus the fully portable
AI-team setup that builds it. Product brief: [docs/PRD.md](docs/PRD.md). Clone anywhere, run
`scripts/setup.sh`, and the whole team works identically.

## Where to start

`CLAUDE.md` loads every session and carries the reading order. In short: `docs/ROADMAP.md` for
where we are, then **[docs/ARCHITECTURE-ESSENTIALS.md](docs/ARCHITECTURE-ESSENTIALS.md) in full
before writing code** — it is under 150 lines and every claim is marked BUILT or DESIGNED.
**DESIGNED means no code exists.**

## Where the team is defined

`.claude/agents/*.md` — 19 roles, one file each. That is the only definition. The pipeline that
sequences them is `.claude/skills/new-feature/SKILL.md`, reachable as `/new-feature`.

## Where the rules live

**With the owning role, by [ADR 0005](docs/adr/0005-rules-live-with-the-owning-role.md)** — so
they load when that role runs and cost nothing when it doesn't. Resilience is in
`backend-engineer`, the security baseline in `security-engineer`, data and schema rules in
`database-engineer`, period and aggregation rules in `data-reporting-engineer`, platform rules
in `devops-engineer`. Do not look for them in a central file; there isn't one, on purpose.

Architectural decisions are in `docs/adr/` (use the `adr` skill to add one). **ADRs 0008–0011
and every Amendment section correct earlier text** — read the amendment, not the original.

## The non-negotiables

1. **Production-grade only.** No placeholder code, no TODO-and-move-on. Every change ships with
   tests and passes them.
2. **Small, reviewed steps.** One logical change per commit. Feature branches off `dev`; `main`
   is always releasable.
3. **Security by default.** Never commit secrets. Validate all external input. Least privilege
   everywhere.
4. **Decisions are written down.** Anything architectural gets an ADR.
5. **Token discipline.** Search before reading whole files. Prefer subagents for broad
   exploration so raw file dumps stay out of the main context.

## Standing rules, earned expensively

Each of these cost this project real time. They are not style preferences.

- **Presence is not verification, and a grep is a presence check.** Four controls in slice 1
  were present, correctly named, documented, and never executed. **A guard is unverified until
  you have watched it refuse something.** `hasattr` lies about `__ror__` — use
  `in QuerySet.__dict__`, or evaluate the expression.
- **Validate a regression test by mutating the specific guard it targets**, and state the
  observed failure. A prescribed test once passed with its guard deleted, because a second
  guard raised first.
- **A reviewer's suggested mechanism is the least-verified artifact in the loop.** State the
  *property* and its acceptance test; label any mechanism "one option, untested". When an
  implementer overrules a prescription, send it back to the prescribing gate for
  acknowledgement — it has happened five times and the implementer was right every time.
- **No two parallel agents share a checkout.** One `git worktree` each, its own compose project
  (`-p <name>`), and its own scratchpad subdirectory. A shared tree cost a deleted security
  guard and a "mid-edit read" indistinguishable from a real regression.
- **Never mount into `/app/**`** — it leaves a zero-byte root-owned stub on the host, and an
  empty `.py` is ruff-clean so lint will not catch it.
- **Start a parallel round from a committed base**, or attribution is lost even with perfect
  file ownership.
- **Agents cannot run `git add`/`commit`/`push`.** Elvis commits, and only once every agent has
  reported.
- **`Read(./.env*)` is denied.** Report the variables you need; never work around it.
- **Never edit a `_V1` SQL constant** — add `_V2` plus a new migration.
- **Do not create empty scaffolding.** No placeholder modules, no empty directories. The
  intended structure is a diagram in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §5; scaffold
  a directory when the task that fills it starts. This repo's Dockerfile copies path by path,
  and an empty tree is the "documented control that never runs" failure by design.
- **Apply DRY, SoC and least privilege — but never at the cost of a guard.** Two overlapping
  mechanisms with different failure modes are defence in depth. Delete the duplication only
  when you can show the survivor catches everything.
- **Ask when a decision is the human's.** Ask once, in a batch, with recommendations.

## Correctness the product cannot ship without

- **Invariant #1:** a business row belongs to exactly one store, and a query may never span two
  organizations. A cross-tenant leak is release-blocking.
- **Period boundaries and timezones** are correctness-critical everywhere. Periods are
  biweekly — the 1st–15th and the 16th–end of month — in the organization's timezone.
  `data-reporting-engineer` gates anything touching sums, dates or timezones.
- **Denials are 404, never 403.** A 403 confirms the row exists.
