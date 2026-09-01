---
name: bug-fix
description: Disciplined bug-fixing workflow - reproduce, failing test, fix root cause, verify. Use for any defect report or regression.
---

# Bug Fix Workflow

## 1. Reproduce
Reproduce the bug locally before touching code. If you cannot reproduce it, gather evidence (logs, inputs, versions) until you can, or document exactly why it's environment-specific. Never fix by guesswork.

## 2. Pin it with a failing test
Write the smallest test that fails because of this bug. This test is the definition of done and the permanent regression guard.

## 3. Root cause, not symptom
Find WHY it happens, not just where. If the fix is a special-case patch around the symptom, you haven't found the root cause yet. Ask: could the same root cause bite elsewhere? Check those places now.

## 4. Fix
Smallest change that fixes the root cause. Resist drive-by refactoring — separate commit/PR if the code needs it.

## 5. Verify
- The new test passes; the full suite passes.
- Re-run the original reproduction steps manually.
- `code-reviewer` on the diff.

## 6. Close the loop
In the commit/PR body: root cause in one sentence, why the fix is safe, and the test that guards it. If the bug reached production, note what would have caught it earlier (missing test class? missing validation?) and file that as a follow-up.
