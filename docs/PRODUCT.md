# Raporo — Product Brief (v1 decisions, 2026-08-31)

Source of truth for `product-owner`; each feature gets its own Phase-1 spec refined from this. Stack: ADR 0006 + ADR 0007 (frontend = Django templates + HTMX; service layer keeps a future mobile API cheap).

## What it is
Sales-reporting SaaS for Rwandan businesses. Organizations register, add products, record stock movements and sales, then see fast reports (daily / weekly / biweekly / monthly / custom range) and download or schedule beautifully designed, boss-ready reports.

## Decisions

| Area | Decision |
|---|---|
| Data entry | Manual: products (new/edit), restocking, sales, refunds. No online payments — the payment *method* is recorded per sale. |
| Tenancy | Organization = tenant, multiple users per org. |
| Roles | Custom RBAC: org owners/admins create roles, define permissions, assign/promote/demote members. Ship presets (Owner, Manager, Seller) built on the same custom-role system. |
| Currency | Base currency **Rwf**. USD as a later addition. Customers may pay in another currency: record amount + currency + the exchange rate frozen at transaction time, plus the converted base-currency amount. Exact decimals only — never floats. |
| Timezone | Report boundaries use the **organization's timezone** (default `Africa/Kigali`); users may get a display timezone later. One truth per org, or two users see different "today" totals. |
| Biweekly | **Confirmed (2026-09-01): 1st–15th and 16th–end of month** (28th/29th/30th/31st all fall in the second half). |
| Report delivery | **WhatsApp-first** (changed from email-first in the Q&A round): reports render as a beautiful shareable image + PDF with one-tap share; scheduled/automated sending (email first, since WhatsApp automation is Meta-restricted) comes later as a setting — that's when Celery beat lands. |
| Pricing | Reference price per product + actual negotiated price per sale, no ceiling. The **floor is the purchase cost itself** — a sale can never go below what the product was bought for (not a manually set number; derived from recorded cost. Which cost basis — latest vs weighted — is pinned in the stock-slice design). |
| Costing (igishoro) | Weighted average for shop COGS; exact per-batch inside investment cycles; latest purchase cost always visible. |
| Orders & credit | Orders carry deposits and a paid/delivered lifecycle; revenue = money actually received; unpaid balances tracked per customer ("who owes us"). |
| Money out & expenses | Purchases/inputs recorded (restock costs, materials); simple expense log in v1 — profit is honest. |
| Investment cycles | v1 feature: capital in → linked purchases & sales → revenue, igishoro, inyungu, rest — at any moment. |
| Investors | First-class org-level list: investor profiles with dated capital accounts (initial + top-ups), optionally linkable to a real user account later for read-only "watch my money" access. A cycle can have **multiple investors with % shares**; each cycle stores the agreed **profit split** (investor/operator) and computes each side's share; payouts are recorded, dated entries. Per-investor and per-cycle reports: capital working, igishoro, inyungu, share, payouts, ROI, duration. The investor profile page shows *everything* about their money. |
| Stores | An organization has **1 to 5 stores/shops** (min 1, max 5, enforced). Stock, sales, orders, and analytics are store-scoped: entering a store shows everything under it, including how it is doing. Org level adds the consolidated view across stores. |
| Tenancy isolation (invariant #1) | Every record belongs to exactly one store, every store to exactly one org (confirmed 2026-09-01). Data must never mix across stores or orgs at any point — queries, APIs, reports, exports, caches. Org level gets the consolidated cross-store report to compare shops. A cross-tenant leak is a Critical release-blocking defect. |
| Write-offs | Stock adjustments with mandatory reason (damaged / lost / stolen / personal use / count correction), permission-gated, honest in reports, reducing cycle value where linked. |
| Alerts | v1: low-stock thresholds per product and **expiry alerts** — restock batches carry an optional expiry date (rice, perishables). |
| Soft deletion & audit | **No hard deletes anywhere** — soft deletion only. Every action in the system records its actor and time; edits/corrections are permission-gated and visible as history. Trust is the product. |
| Detail pages | Principle: every entity's page shows everything under it with quick actions attached — a product page shows its sales, purchases/restocks, stock history, profit, and lets you restock/edit right there; investor and store pages follow the same rule. |
| Customers | Lightweight records (name, optional phone) attached to orders/credit only. |
| Data entry | Both modes: live per-sale (mobile-first design center) and end-of-day batch. |
| Connectivity | Online-first; entry screens tolerate brief network drops gracefully. Full offline is out of v1 scope. |
| Report branding | Per-organization: logo, colors, layout/ordering preferences applied to generated reports. |
| Auth | Login with username OR email OR phone, + password. Phone stored with country code, digits only, no `+`. **Maximum-security posture (2026-09-01): argon2id hashing, login rate-limiting + lockout/backoff, secure session cookies, safe password reset, no user enumeration — fast for users, hostile to attackers.** Social login later — architecture leaves space, nothing built now. |
| 2FA | **Designed from the start**: TOTP (authenticator app) + one-time recovery codes, optional per user, org can require it for its members. Data model and login flow reserve the 2FA step from slice 1 even if UI polish lands later. SMS OTP deliberately not v1 (cost + SIM-swap risk). |
| Onboarding & invites | Self-registration creates an organization (registrant becomes Owner). **Everyone else joins by invite link**: org admins invite users to the org and specific store(s) with a pre-assigned role; links are single-use, expiring, revocable, and shareable over WhatsApp/email. No open signup into an existing org, ever. |
| Future API docs | When the API layer is built (mobile app / new frontend / integrations — ADR 0007 service layer makes it cheap), it must ship **documented from day one** (OpenAPI/Swagger auto-generated). Until then, the seam contracts (service signatures + URL/fragment map) are kept written per slice — they become the API spec's skeleton. |
| History & retention | Import of historical sales allowed, any age. Retention: keep financial records **at least 10 years** (Rwandan tax record-keeping), no auto-deletion in v1; archive/aggregate old data only when volume demands it (see ADR when that day comes). |
| Languages | English (default) + Kinyarwanda + French, all three complete from the start (confirmed 2026-09-01). Per-user preferred language saved in settings, **plus an always-visible switcher in the header** — switch any moment, like major platforms. Every user-facing string translated from day one; no feature ships with untranslated text. The login/register pages are switchable too (language choice can't hide behind login). |
| Hosting | Self-hosted Docker PaaS (Dokploy or Coolify) on a low-cost VPS — fits the everything-dockerized rule and a near-zero starting budget. |

## Glossary seeds (product-owner owns and grows this)
**Organization** (the tenant/business) · **Member** (a user inside an org) · **Role** (org-defined permission set) · **Product** · **Restock** (stock in) · **Sale** (stock out, has payment method + currency) · **Refund** · **Period** (daily/weekly/biweekly/monthly/custom, org-timezone-bounded) · **Report** (rendered, branded output of a period).

## Explicitly out of scope for v1
Online payment processing · social login · USD as base currency · POS/e-commerce integrations · global/multi-country launch.
