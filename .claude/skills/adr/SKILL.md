---
name: adr
description: Write an Architecture Decision Record in docs/adr/. Use whenever a decision is hard to reverse - stack choice, data model, API shape, dependency, infrastructure.
---

# Architecture Decision Record

1. Find the next number: highest `NNNN-*.md` in `docs/adr/` plus one.
2. Create `docs/adr/NNNN-<kebab-case-title>.md` with exactly this structure:

```markdown
# NNNN. <Title — the decision as a statement>

Date: <YYYY-MM-DD>
Status: Accepted | Proposed | Superseded by NNNN

## Context
What forces are at play — the problem, constraints, and what happens if we do nothing. 3–10 sentences. A stranger must understand the situation from this alone.

## Decision
What we chose, stated actively: "We will ...". Include the concrete alternatives that were rejected and one line each on why.

## Consequences
What becomes easier, what becomes harder, what we're now committed to, and what would trigger revisiting this decision.
```

Rules:
- One decision per ADR. Two decisions = two files.
- Never edit an accepted ADR's decision — write a new one that supersedes it and update the old one's Status line.
- Keep it under a page. If it needs more, the decision probably isn't crisp yet.
