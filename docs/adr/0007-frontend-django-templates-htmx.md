# 0007. Frontend: Django templates + HTMX, superseding ADR 0006's React choice

Date: 2026-09-01
Status: Accepted (supersedes the frontend portion of ADR 0006)

## Context

ADR 0006 chose a React SPA, purchased deliberately as React tuition: Elvis wanted to learn React by doing. On 2026-09-01, after a full trade-off discussion (recorded in the session), Elvis explicitly set the React learning goal aside and chose shipping speed, simplicity, and his existing Django expertise. Raporo's UI — forms, tables, detail pages, rendered reports — is exactly the form-and-data territory where server-rendered HTML excels, and a solo Django expert is fastest in that world.

## Decision

The frontend is **Django templates + HTMX**: the server renders full pages; HTMX swaps HTML fragments for interactivity (record a sale, filter a report, switch store) with no full-page reloads and no JavaScript framework. Alpine.js may be added later as a sprinkle for small client-side widgets — it is not part of the initial build. There is no separate frontend project, no Node build pipeline, and no client-side i18n dictionary: Django's translation system covers everything.

**The enabling rule that keeps the future open: all business logic lives in a service layer; views stay thin.** HTML views call the same `record_sale(...)`-style services a future DRF endpoint would call. When a mobile app or third-party integration becomes real, we add DRF views over the existing services — weeks of work, not a rewrite. DRF therefore leaves the day-one stack and returns when a real API consumer exists.

Rejected: React SPA (both apps + API contract + second i18n layer, justified only by the learning goal that has been dropped); building the DRF API anyway "for later" (violates YAGNI; the service layer preserves the option at near-zero cost); heavier hybrid frameworks (Inertia, Next.js) — wrong complexity for a server-rendered product.

## Consequences

Easier: one codebase, one language, one deploy, one Docker service fewer; first paint is fast (no JS bundle); Django auto-escaping + session/CSRF auth apply unchanged (security baseline intact); i18n single-sourced; Elvis works at full native speed. Harder: no React education from this project (explicitly accepted); extremely app-like interactions (drag-and-drop builders, heavy client state) would need reevaluation — Raporo's screens don't; the service-layer discipline must be enforced in review from slice 1 (backend-engineer + code-reviewer own it), because it is what makes the future API cheap. Revisit when a mobile app or external API consumer is committed — that triggers DRF endpoints over the existing services, not a redesign.
