# 0006. Stack: Django 6.1 + DRF API, PostgreSQL, Redis/Celery when needed, React SPA, Docker everywhere

Date: 2026-08-31
Status: Accepted

## Context
Raporo is a sales-reporting product: clients create accounts, register their business and products, and view sales across daily/weekly/biweekly/monthly/custom ranges — fast — and download polished reports good enough to hand to their boss. The owner's strengths are Python/Django; the frontend is explicitly also a learning goal (React, learning by doing). A development environment with the chosen versions already exists and is active.

## Decision
Backend: **Python with Django 6.1 — non-negotiable — and Django REST Framework** for the API. We use Django 6.1's new capabilities deliberately rather than reflexively adding packages; every dependency must support Django 6.1, and otherwise we track latest stable versions. Data: **PostgreSQL** as the single source of truth. **Redis, Celery and beat workers are added when a real async/scheduled need exists** (report generation, scheduled emails), not preinstalled. Frontend: **React SPA**, kept boring and idiomatic because the owner is learning it by doing — every non-trivial frontend change is explained in plain language. **Everything is dockerized**: docker compose for local dev (web, db, redis, worker, frontend), production images per devops-engineer's standards.

Rejected: Django templates + HTMX (fits the product fine, but defeats the stated React learning goal); Next.js/SSR React (extra concepts on top of React itself — wrong first step for learning; revisit if SEO of authenticated pages ever matters, which it doesn't for a dashboard); FastAPI (Django's batteries — auth, admin, ORM, migrations — are exactly what a reports SaaS needs).

## Consequences
Easier: one well-known backend framework carries auth, ORM, migrations, admin; DRF gives the API conventions the integration-engineer's contracts will lean on; Docker keeps every machine identical. Harder: an SPA forces an explicit API auth decision (session+CSRF vs tokens) at design time — security-engineer owns that call in Phase 2; report rendering tech (how HTML becomes a beautiful PDF) is its own Phase-2 architect decision; React learning will slow early frontend slices — accepted deliberately. Period semantics (daily/weekly/biweekly boundaries, timezones) are the product's hardest correctness problem and belong to data-reporting-engineer from day one. Revisit the Redis/Celery "when needed" stance the moment report generation blocks a request.
