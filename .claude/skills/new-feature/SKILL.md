---
name: new-feature
description: The standard workflow for building any feature - the five-phase team pipeline from spec to ship. Use whenever starting new functionality of any size.
---

# New Feature — the five-phase pipeline

Follow the phases in order. Shrink phases for small features — never skip the gates. Agent roster: CLAUDE.md.

## Phase 1 — Define
`product-owner` (problem statement, acceptance criteria, scope, glossary) → `tech-lead` (vertical slices, sequencing) → `architect` (module boundaries, dependency direction; `/adr` for hard-to-reverse decisions). If you can't write the acceptance criteria, stop and ask.

## Phase 2 — Design (parallel, then converge)
`ux-designer` (flows, every interaction state, tokens, accessibility) ∥ `database-engineer` (schema, constraints, indexes) ∥ `architect` (structure) → `integration-engineer` writes the API contract, both build engineers sign off → `security-engineer` threat-models the design.

## Phase 3 — Build (per vertical slice)
1. Branch: `git checkout dev && git pull && git checkout -b feature/<short-name>`.
2. `backend-engineer` ∥ `frontend-engineer` — parallel against the signed contract; TDD (superpowers:test-driven-development); no placeholder code, no TODOs without a linked issue.
3. `integration-engineer` proves the seam: end-to-end journeys including failure paths.
4. `qa-engineer` proves behavior: denial tests, exploratory pass.
5. Blocking gates: `code-reviewer` on every diff (must APPROVE); `security-engineer` if auth/tenant/input/files/network/deps/config touched; `data-reporting-engineer` if aggregation/export/period logic touched.
6. `tech-lead` merge gate: verifies every required gate actually produced output, then hands the human the go/no-go (commits/merges are human actions in this project).

## Phase 4 — Harden
`performance-engineer` (budgets met, hot paths profiled) → `sre-observability` (instrumented, alerts with runbooks) → `devops-engineer` (pipeline, container, environments).

## Phase 5 — Ship
`privacy-compliance` (PII/GDPR gate) → `tech-writer` (docs, changelog) → `craft-editor` (de-AI-ify all user-facing prose) → run `/production-readiness` → `devops-engineer` deploys. Merge `dev` → `main` only on a **SHIP** verdict.
