---
name: production-readiness
description: The ship checklist. Run before merging to main or releasing. Verifies tests, security, docs, config, and operability - and blocks the merge if anything fails.
---

# Production Readiness Checklist

Run every item. Report each as PASS / FAIL / N-A (with one-line justification for every N-A). Any FAIL blocks the merge — no exceptions, no "we'll fix it after".

## Correctness
- [ ] Full test suite green locally and in CI.
- [ ] New behavior covered by tests that would fail if it broke.
- [ ] Lint and typecheck clean.

## Security
- [ ] `security-auditor` ran on the change set; no Critical/High findings open.
- [ ] No secrets in the diff (`git diff main... | grep -iE 'api[_-]?key|secret|password|token'` plus judgment).
- [ ] Dependency audit clean or exceptions documented.

## Operability
- [ ] Errors are logged with enough context to debug in production; no swallowed exceptions.
- [ ] New config has sane defaults and is documented; app fails fast and clearly when required config is missing.
- [ ] Migrations (if any) are reversible or have a documented rollback plan.

## Docs & hygiene
- [ ] README / setup docs still true (fresh-machine perspective).
- [ ] ADR written for any architectural decision in this change set.
- [ ] Changelog updated if behavior is user-visible.
- [ ] No dead code, debug prints, or commented-out blocks introduced.

## Verdict
End with a single line: **SHIP** or **BLOCKED — <the failing items>**.
