# Raporo — Project Description (draft for Elvis's review)

*Written 2026-08-31 from Elvis's decisions (docs/PRODUCT.md) and three real sample reports from Rwandan businesses. This is the shared understanding we build from — correct anything wrong before we design.*

## The story

Across Rwanda, small businesses run on discipline and WhatsApp. Every evening, a shop attendant types a daily report by hand: what was ordered, what was sold and for how much, what came into stock, what went out, and a full count of what remains — then sends it to the owner. Owners forward these to partners and investors. The reports work, but they are typed by tired humans: totals go missing, restocks go unrecorded, and yesterday's count doesn't always explain today's.

Our samples show it directly. The clothing-shop report of 26.08 lists 49 "Shirts made in Rda"; on 28.08 the count is 55 after selling 7 — meaning 13 shirts were restocked that no report mentions. On 28.08 the "TOTAL SALES" line is empty. Jersey stock drops from 56 to 55 with no recorded sale. Nobody did anything wrong — this is simply what manual reporting does under real-life pressure.

**Raporo replaces the typing, not the discipline.** The seller records events as they happen — a sale, a restock, an order, a payment. Raporo does all the math, keeps stock permanently consistent, and produces the daily/weekly/biweekly/monthly report automatically: the same structure businesses already trust, but beautiful, accurate, branded with the shop's logo, and impressive enough to hand straight to a boss or investor.

## The second story: investment cycles

The Silver Rice report is a different, equally important use. Someone put **350,000 Rwf** into rice: 10 sacks at 35,000 each. Over 10 days, 8 sacks sold at negotiated prices (45k, 39k, 42k, 40.5k…) for **340,500 Rwf** revenue. The report closes with the accounting that matters, in Kinyarwanda:

- *Amafaranga yarangujwe* — money invested: **350,000**
- *Amafaranga yacurujwe* (8 sacks) — revenue: **340,500**
- *Igishoro cy'ibyacurujwe* — cost of what was sold (8 × 35k): **280,000**
- *Inyungu y'ibyacurujwe* — profit on what was sold: **60,500**
- *REST* — 2 sacks still in stock (70,000 of capital still working)

This is Elvis's "I give a friend 350K to invest" scenario, exactly as practiced today.

**Investors are first-class (added 2026-09-01).** An organization keeps a list of investors. Each investor has a profile with a capital account: every contribution is dated and recorded (the initial 350k, and any top-ups later — capital can grow over time). Cycles belong to an investor; under an investor's profile you see how their money is working: capital contributed, capital currently tied up in stock, revenue, igishoro, inyungu realized, payouts taken, ROI, and how long each cycle ran. Each investor gets their own report — per cycle and across all their cycles — separate from the shop's daily sales report. Raporo should make this a first-class concept: an **investment cycle** — capital in, purchases linked, sales tracked, and at any moment the answer to *how is my money doing?* (revenue, cost of goods sold, profit so far, capital still tied up in stock, over what period).

## What the samples teach us (decoded)

| Report section | Meaning (as understood) | Raporo concept |
| --- | --- | --- |
| **COMMAND** — `Complete(Albert) 70k (T.50k)`, `4Trauser(Joram) 100k✅` | Confirmed: customer orders by name at an agreed price; "T.50k" = 50k deposit paid; ✅ = fully paid/delivered. | Customer **order**: money actually received (deposit/full) counts in the day's sales; the unpaid balance becomes a tracked debt ("who owes us"), filterable so the owner can follow up. An order with nothing paid is visible but counts no revenue. |
| **SALES** — `6Shirt made in Rda 180k` | Line items: quantity × product = amount. Unit prices vary between sales (rice sacks: 45k, 39k, 42k…) — prices are negotiated per sale. | **Sale** with line items; actual price recorded per sale |
| **PAYMENT — 0** | Confirmed: money movements beyond plain sales — credit collections in, AND money out for purchases/inputs (restocks, materials to make suits/shirts), with payment methods recorded. | **Payments in** (credit book) + **money out** (purchases/expenses) — the basis for investment-vs-return, which-product-wins, where-to-cut analysis |
| **STOCK-IN** — `12jersey kids` | Restock events | **Restock** (with purchase cost, so profit is computable) |
| **STOCK OUT** | Items that left inventory that day | Derived automatically from sales/refunds — never typed |
| **CURRENT STOCK** — 9 numbered lines | Full end-of-day inventory | Derived automatically; always consistent |
| `Isengeri 1pc=8 / 3pcs=4` | The same product sold singly or as a 3-piece pack | Product **variants/packs** |
| Igishoro / Inyungu | Cost of goods sold / profit | **COGS & profit**, computed from restock costs |

## Who uses it

- **Owner/Admin** — creates the organization, brands it (logo, colors), invites members, defines custom roles and permissions, promotes/demotes, sees everything, receives reports.
- **Manager / Seller** (org-defined roles) — records sales, restocks, orders, payments as they happen.
- **The boss/investor** — often not a user at all: they receive the finished report and judge the business (and the app) by it. The report is the product's face.
- **Investor** — a profile in the organization's investor list (may or may not be a system user). Has a capital account (contributions and top-ups over time), owns cycles, receives per-investor reports: capital working, inyungu realized, payouts, ROI, duration.

## What Raporo promises

1. **Never do report math again.** Totals, stock-out, current stock, igishoro, inyungu — all derived, always consistent, gap-proof.
2. **Reports a boss remembers.** Clean, branded, simple to read — daily, weekly, biweekly (1–15 / 16–end), monthly, custom range — downloadable and schedulable.
3. **Fast.** A seller records a sale in seconds on a phone; an owner opens today's numbers instantly.
4. **Investment clarity.** Put money in, see exactly what it's doing, in the terms Rwandan business already uses.

## Foundation decisions already made (docs/PRODUCT.md, ADR 0006)

Django 6.1, PostgreSQL, Django templates + HTMX (ADR 0007 — replaced the earlier React choice), Docker, Redis/Celery when scheduling arrives · org-level custom RBAC · base currency Rwf with frozen-rate foreign payments · org timezone (default Africa/Kigali) · login by username/email/phone + password · EN/Kinyarwanda/FR · ≥10-year record retention · Dokploy/Coolify hosting.

## Decisions from the Q&A round (Elvis, 2026-08-31 evening)

1. **Orders (COMMAND):** deposits confirmed ("T.50k" = 50k paid; ✅ = fully paid/delivered). Revenue counted = money actually received; unpaid balances become tracked debts, filterable ("who owes us") for follow-up — including delivered-but-not-fully-paid.
2. **PAYMENT section:** both directions — credit collections (money in) and purchases/inputs (money out: restocks, materials for making goods), with payment methods recorded. Purpose: compare investment vs return, spot winning products, know where to invest more and where to cut.
3. **Pricing (confirmed 17:29):** reference price per product, actual negotiated price per sale, no ceiling — 40k, 45k, 55k all welcome. The floor **is the purchase cost**: a product can never be sold below what it was bought for (35k rice never sells at 30k). The floor comes from the recorded cost automatically, not from a manually set field.
4. **Costing:** weighted average for shop COGS; exact per-batch inside investment cycles; and always surface the **latest purchase cost** (current replacement cost) — it informs the floor and stock value.
5. **Investment cycles: v1.** 6. **Entry: both** live per-sale (mobile-first) and end-of-day batch. 7. **Customers: lightweight** (name, optional phone) on orders/credit. 8. **Delivery: WhatsApp-first** — beautiful shareable image + PDF, one-tap share; scheduled sending later. 9. **Expenses: in v1** — simple expense log so profit is honest. 10. **Connectivity: online-first**, entry screens tolerate brief drops gracefully.

## Final completeness round (Elvis, 2026-09-01 morning)

A-b: profit splits stored per cycle and computed. B-b: investor profiles optionally link to real accounts (read-only watching). C-b: co-investors allowed, % shares per cycle. D-a: write-offs with mandatory reasons. E-a plus: **soft deletion only — nothing is ever really deleted**; every action records its actor. F-b: **1–5 stores per organization**, store-scoped everything with per-store analytics + consolidated org view. G-b: alerts in v1 — low stock and **expiring goods** (restock batches carry optional expiry dates). Cross-cutting principle: every detail page (product, investor, store) shows everything under it with its actions attached. Confirmed inferences: org-level consolidated reports comparing all stores; optional expiry date per restock batch. **Invariant #1 (Elvis, verbatim intent): everything connects to a store and above it an org — data must never mix between stores or orgs at any point.**

## Explicitly not in v1

Online payment processing · social login · POS/e-commerce integrations · global launch (see docs/PRODUCT.md).
