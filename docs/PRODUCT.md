# Raporo — Product Brief (v1 decisions, 2026-08-31)

Source of truth for `product-owner`; each feature gets its own Phase-1 spec refined from this. Stack: ADR 0006.

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
| Biweekly | 1st–15th and 16th–end of month (28th/29th/30th/31st all fall in the second half). ⚠ Confirm: the original wording said "1 to 14th, then 16th" which leaves the 15th unassigned — recorded here as 1–15 / 16–end pending Elvis's confirmation. |
| Report delivery | **WhatsApp-first** (changed from email-first in the Q&A round): reports render as a beautiful shareable image + PDF with one-tap share; scheduled/automated sending (email first, since WhatsApp automation is Meta-restricted) comes later as a setting — that's when Celery beat lands. |
| Pricing | Reference price per product + actual negotiated price per sale, no ceiling. The **floor is the purchase cost itself** — a sale can never go below what the product was bought for (not a manually set number; derived from recorded cost. Which cost basis — latest vs weighted — is pinned in the stock-slice design). |
| Costing (igishoro) | Weighted average for shop COGS; exact per-batch inside investment cycles; latest purchase cost always visible. |
| Orders & credit | Orders carry deposits and a paid/delivered lifecycle; revenue = money actually received; unpaid balances tracked per customer ("who owes us"). |
| Money out & expenses | Purchases/inputs recorded (restock costs, materials); simple expense log in v1 — profit is honest. |
| Investment cycles | v1 feature: capital in → linked purchases & sales → revenue, igishoro, inyungu, rest — at any moment. |
| Customers | Lightweight records (name, optional phone) attached to orders/credit only. |
| Data entry | Both modes: live per-sale (mobile-first design center) and end-of-day batch. |
| Connectivity | Online-first; entry screens tolerate brief network drops gracefully. Full offline is out of v1 scope. |
| Report branding | Per-organization: logo, colors, layout/ordering preferences applied to generated reports. |
| Auth | Login with username OR email OR phone, + password. Phone stored with country code, digits only, no `+` (normalized E.164 without the plus). Social login later — architecture leaves space, nothing built now. |
| History & retention | Import of historical sales allowed, any age. Retention: keep financial records **at least 10 years** (Rwandan tax record-keeping), no auto-deletion in v1; archive/aggregate old data only when volume demands it (see ADR when that day comes). |
| Languages | English + Kinyarwanda + French, switchable. Rwanda-first; English becomes default if the product later goes global. |
| Hosting | Self-hosted Docker PaaS (Dokploy or Coolify) on a low-cost VPS — fits the everything-dockerized rule and a near-zero starting budget. |

## Glossary seeds (product-owner owns and grows this)
**Organization** (the tenant/business) · **Member** (a user inside an org) · **Role** (org-defined permission set) · **Product** · **Restock** (stock in) · **Sale** (stock out, has payment method + currency) · **Refund** · **Period** (daily/weekly/biweekly/monthly/custom, org-timezone-bounded) · **Report** (rendered, branded output of a period).

## Explicitly out of scope for v1
Online payment processing · social login · USD as base currency · POS/e-commerce integrations · global/multi-country launch.
