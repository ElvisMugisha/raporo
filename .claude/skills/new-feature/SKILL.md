---
name: new-feature
description: The standard workflow for building any feature - spec, design, TDD implementation, review, docs. Use whenever starting new functionality of any size.
---

# New Feature Workflow

Follow these phases in order. Do not skip phases for "small" features — shrink them instead.

## 1. Spec (minutes, not days)
Write 3–8 lines in the task/PR description: what the user can do after this ships, what is explicitly out of scope, and how we'll know it works (acceptance criteria). If you can't write the acceptance criteria, stop and ask.

## 2. Design
For anything touching more than one module, new dependencies, data model, or public API: invoke the `architect` agent with the spec. Architectural decisions get an ADR (`/adr`). Trivial features may skip to 3 — say so explicitly.

## 3. Branch
`git checkout dev && git pull && git checkout -b feature/<short-name>`

## 4. Implement with TDD
- Invoke `test-engineer` (or write tests yourself for small changes): failing tests first, from the acceptance criteria.
- Implement until tests pass. Keep commits small and logical.
- No placeholder code, no commented-out blocks, no TODOs without a linked issue.

## 5. Review gate (all three, can run in parallel)
- `code-reviewer` on the diff — must APPROVE.
- `security-auditor` if the change touches input, auth, files, network, deps, or config.
- Full test suite + lint green locally.

## 6. Docs & finish
- `docs-writer` if behavior, setup, or API changed.
- Run `/production-readiness` before merging to `main`.
- PR into `dev`; merge `dev` → `main` only via `/production-readiness`.
