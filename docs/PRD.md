# Raporo — Product Requirements (v1)

Date: 2026-09-03 · Owner: `product-owner` · Status: the contract for every Phase-1 spec

This document is the product layer's source of truth. Where an ADR or a spec owns a decision,
this document states the decision in one sentence and cites the file that owns it. Where a
decision does not exist yet, it appears in [Open product questions](#12-open-product-questions)
with a recommended default and nobody waits for it.

Companion documents: [PRODUCT.md](PRODUCT.md) (the original decision table),
[PROJECT-DESCRIPTION.md](PROJECT-DESCRIPTION.md) (the narrative and the decoded sample
reports), [ROADMAP.md](ROADMAP.md) (where the work actually is).

---

## 1. The problem

Across Rwanda, small retail businesses run on discipline and WhatsApp. Every evening a shop
attendant types the day's report by hand — what was ordered, what was sold and for how much,
what came into stock, what left, and a full count of what remains — and sends it to the owner,
who forwards it to partners and investors. The discipline is real; the typing is not reliable.
In the three real sample reports this product was designed from, a clothing shop's shirt count
goes from 49 on 26.08 to 55 on 28.08 after selling 7, meaning 13 shirts were restocked that no
report records; the 28.08 report's `TOTAL SALES` line is blank; a jersey disappears from stock
with no sale behind it. Nobody did anything wrong — that is simply what manual reporting does
under real pressure. The people who hurt are the owner, who cannot trust the numbers she is
judging her business by, and the attendant, who spends the end of every day doing arithmetic he
was never hired for. It matters now because the same owner is being asked for the same numbers
by investors who put cash into specific stock and want to know what their money is doing, and a
handwritten total is not an answer she can defend.

**Raporo replaces the typing, not the discipline.** The seller records events as they happen —
a sale, a restock, an order, a payment. Raporo does the arithmetic, keeps stock permanently
consistent, and produces the report automatically: the same structure these businesses already
trust, but accurate, branded with the shop's own logo, and good enough to hand straight to a
boss or an investor.

## 2. Who it is for

- **The owner of one to five shops.** Registers the business, brands it, invites staff, and
  reads the numbers. Often runs two visibly different businesses — a food shop and a clothing
  shop — from one account.
- **The manager and the seller.** On a phone, on the shop floor. They record events; they do
  not compute anything. Their screens must work in one hand, in Kinyarwanda, on a slow
  connection.
- **The boss or investor who never logs in.** They receive the finished report over WhatsApp
  and judge both the business and the software by it. The report is the product's face.
- **The investor who put cash into specific stock.** Named in the organization's investor list,
  with a dated capital account, and entitled to an answer to *how is my money doing* at any
  moment — in the terms Rwandan trading already uses (`igishoro`, `inyungu`, `REST`).

Raporo is Rwanda-first: base currency Rwf, EN/Kinyarwanda/FR, `Africa/Kigali` as the default
timezone, and Rwanda Law No. 058/2021 as the privacy law that governs
([privacy ruling](superpowers/specs/2026-09-02-privacy-law-058-2021-ruling.md)).

## 3. What the product does

**The period report is the centre of gravity. Everything else exists to feed it.**

A report covers a period — a day, a week, a biweekly half-month, a month, or a custom range —
bounded in the organization's timezone. It is produced per store, and at organization level as
a consolidated view across stores. It carries the store's own name and the branding resolved
through the chain in §4.7. It renders as a shareable image and a PDF. Its numbers are derived
from recorded events and never typed: totals, stock out, current stock, cost of goods sold
(`igishoro`) and profit (`inyungu`).

The events that feed it, in the order a shop generates them:

- **Restocks** — stock in, with the purchase cost that makes profit computable, and an optional
  expiry date for perishables.
- **Sales** — line items with the actual negotiated price, a payment method, and a currency.
- **Orders** — a customer's order at an agreed price, with deposits and a paid/delivered
  lifecycle. Revenue counts money actually received; the unpaid remainder becomes a tracked
  debt.
- **Payments** — in both directions: credit collected from customers, and money out for
  purchases and inputs.
- **Refunds and write-offs** — write-offs carry a mandatory reason and are honest in the report.
- **Expenses** — a simple log, so profit is not flattering.
- **Investment cycles** — capital in from a named investor, purchases linked to it, sales
  tracked against it, and at any moment revenue, `igishoro`, `inyungu`, capital still tied up
  in stock, each side's agreed share, and payouts taken.

Two rules about how it is used. **Both entry modes are supported**: live per-sale entry, which
is the mobile-first design centre, and end-of-day batch entry. **Every detail page shows
everything under it with its actions attached** — a product page shows its sales, restocks,
stock history and profit, and lets you restock right there; store and investor pages follow the
same rule.

## 4. Product rules everything else depends on

These are load-bearing. Changing one of them changes the product.

### 4.1 Invariant #1 — isolation (release-blocking)

A business row belongs to **exactly one store**, and every store belongs to exactly one
organization. **A query may never span two organizations** — not in a page, a report, an export,
a cache or a background job. A cross-tenant leak is a Critical, release-blocking defect, not a
bug to schedule.

Isolation is defended in three layers, deliberately: the query layer refuses unscoped and
mixed-scope reads and writes, the database refuses a row whose store and organization disagree
([ADR 0008](adr/0008-denormalised-organization-on-store-scoped-rows.md)), and row-level
security makes the organization boundary a database fact
([ADR 0009](adr/0009-row-level-security-for-organization-isolation.md)). Inside one
organization the database is blind, so store-level separation is entirely the application's job
— which is why the denial matrix in §11 is a release gate.

### 4.2 One organization per user

**A person belongs to exactly one organization** (Elvis, 2026-09-02). Stated plainly, and this
must be reflected in signup and invite copy rather than discovered in support:

- **There is no self-service path to a second organization.**
- **A person running two businesses gets two stores in one organization, not two accounts.**
  The branding chain (§4.7) already lets those two stores present as two businesses on their
  reports.
- A person who leaves an organization is free to join another; the historical membership
  remains.

### 4.3 Stores: one to five

An organization runs **between one and five stores** (`MAX_STORES_PER_ORG = 5`,
`apps/orgs/models.py:38`). Stock, sales, orders and analytics are store-scoped: entering a
store shows everything under it. Organization level adds the consolidated cross-store view.

### 4.4 Store access is granted per store — except the owner

**A user reaches only the stores they were granted, except the organization's owner, who
reaches every store in their organization** (Elvis, 2026-09-02). This is implemented as a
permission code, `store.access_all`, resolved in exactly one function, and **never as a role
name** — role names are user-editable and translatable, so a name check would be a live
vulnerability ([ADR 0011](adr/0011-org-wide-store-access-is-a-permission-code.md)). Owner
memberships carry no per-store access rows; the grant is the role. Denials are **404, never
403**, because a 403 confirms the row exists.

### 4.5 One account per person, three login identifiers

An account carries **username, email and phone**. All three are login identifiers and all three
are unique across the whole system. Email is required — it is the password-reset channel for
every user. Phone is canonicalised Rwanda-first: `0788123456`, `+250788123456`, `250788123456`
and a bare `788123456` all collapse to one stored `250788123456`, so one SIM means one account;
a number from another country needs its country code, and a bare national number from another
country is refused rather than guessed (Elvis, 2026-09-02). **A second business never means a
second phone.**

### 4.6 Money

Base currency is **Rwf**. Prices and totals always read in the store's base currency. Any amount
in a foreign currency — a customer paying USD, an investor contributing USD, a payout in USD —
**requires the exchange rate at record time; the form blocks submission without it**, shows the
converted amount before saving, and stores amount, currency, the frozen rate and the converted
base amount. Exact decimals only, never floats. Rate entry is manual in v1. One mechanism
serves sales payments, capital entries, payouts and expenses.

### 4.7 Branding chain

**Store → organization → Raporo default.** Nothing is ever unbranded. Each store carries an
explicit `use_own_branding` toggle, default off, so an empty field means *inherit* and never
*deliberately blank*; with the toggle on, fallback still applies per field, so an owner can
override the logo and keep the organization's colours. **The store name is always local** —
every store has its own name and it heads that store's report; only logo, colours and
typography travel down the chain. **A consolidated cross-store report carries organization
branding** by definition.

### 4.8 Periods and timezone

Report boundaries use the **organization's timezone** (default `Africa/Kigali`), one truth per
organization, or two people see different totals for "today". **Biweekly means the 1st–15th and
the 16th–end of month** (Elvis, 2026-09-01) — the 28th, 29th, 30th and 31st all fall in the
second half.

### 4.9 Nothing is ever hard-deleted

Soft deletion everywhere, plus an audit trail on every action recording who did it and when.
**The audit trail is append-only at the database level** — update, delete and truncate are
refused by the database, not by convention. Edits and corrections are permission-gated and
visible as history. Trust is the product. Erasure under Law 058/2021 works by anonymising the
*referents* an audit row points at, never by touching the trail
([privacy ruling §2](superpowers/specs/2026-09-02-privacy-law-058-2021-ruling.md)).

### 4.10 Auth posture

Session authentication, argon2id password hashing, login rate-limiting with lockout, secure
session cookies, a non-enumerating emailed password reset, and **2FA (TOTP + one-time recovery
codes) staged in from day one** so the login flow reserves its second stage from slice 1.
Invite links are atomic, single-use, expiring and revocable; **there is no open signup into an
existing organization, ever.**

### 4.11 Languages

**English, Kinyarwanda and French, all three complete from the start.** A per-user preferred
language, plus an always-visible switcher in the header that works before login. No feature
ships with an untranslated string.

### 4.12 Retention

Financial records are kept at least ten years (Rwandan tax record-keeping). Import of historical
sales is allowed at any age. No automatic deletion in v1.

## 5. Users, roles and access

Access is org-defined **custom RBAC**: an organization's owner creates roles, picks their
permissions from a fixed catalog of codes, and assigns, promotes or demotes members. Three
presets ship, built on the same custom-role machinery, and an organization may rename or replace
them (`apps/orgs/permissions.py`):

| Preset | May do |
| --- | --- |
| **Owner** | Everything in the catalog, including `store.access_all` (reaches every store in the organization), managing roles, managing stores, inviting people, and viewing the audit trail. |
| **Manager** | Runs the shop floor: records sales, restocks, write-offs and expenses, manages cycles, generates reports, invites people, manages members. **Does not** manage roles or stores, and **does not** hold `store.access_all` — a manager reaches only the stores they were granted. |
| **Seller** | Records sales. Nothing else. |

Two rules about the catalog, because they are what makes the presets safe:
**which stores** (the access resolver) and **which actions** (the permission check) are
orthogonal axes, so a custom role can hold `store.access_all` with only `report.generate` — an
accountant who reads every branch and writes nowhere. And **every code in the catalog must be
listed in a preset or in a declared unassigned set**, checked at startup, so adding a code can
never silently widen an existing role.

Two participants are not users at all: the **boss or investor who receives the report**, and the
**investor profile** in the organization's investor list, which may exist with no login (see
[Q10](#12-open-product-questions)).

## 6. Domain glossary

One canonical name per concept. Use these words in code, in UI copy, in tests and in
conversation.

**Organization** — the tenant; one business, one to five stores. Called `org` everywhere in code
and in the schema, never `organization`, because the pinned constraint SQL says `org`.
**Store** — a shop. The scope every business row hangs from.
**Member** / **Membership** — a person inside an organization, and the row that says so. One
live membership per person, ever.
**Role** — an organization-defined set of permission codes. Renameable and translatable, and
therefore never an authorization primitive by itself.
**Permission code** — a fixed catalog entry such as `sale.record` or `store.access_all`.
**Invite** — a single-use, expiring, revocable link that joins one person to one organization
with a pre-assigned role and a named set of stores.
**Product** — an item the shop sells. **Variant / pack** — the same product sold singly or as a
multi-piece pack (`Isengeri 1pc / 3pcs`).
**Restock** — stock in, with purchase cost and an optional expiry date. A **batch** is one
restock's quantity at one cost.
**Reference price** — the product's normal price. **Floor** — the lowest price a sale may carry,
derived from recorded cost, never typed (see [Q3](#12-open-product-questions)).
**Sale** — stock out, with line items, actual negotiated prices, a payment method and a currency.
**Refund** — money and stock returned.
**Write-off** — a stock adjustment with a mandatory reason: damaged, lost, stolen, personal use,
or count correction.
**Order** (the samples' `COMMAND`) — a customer's order at an agreed price, with a deposit and a
paid/delivered lifecycle.
**Customer** — a lightweight record: name, optional phone, attached to orders and credit only.
**Credit book** — the "who owes us" view of unpaid customer balances.
**Expense** — money out that is not stock.
**Investor** — a named person in the organization's investor list, with a **capital account**:
dated contributions and top-ups.
**Investment cycle** — capital in, linked purchases, linked sales, and the answer to *how is my
money doing*. Carries **co-investor percentage shares** and an agreed **profit split**
(investor / operator). A **payout** is a dated, recorded withdrawal.
**Period** — daily, weekly, biweekly, monthly or custom, bounded in the organization's timezone.
**Biweekly** — the 1st–15th and the 16th–end of month.
**Report** — the rendered, branded output of a period for one store. **Consolidated report** —
the organization-level report across all its stores, carrying organization branding.
**Branding chain** — store → organization → Raporo default, per field.
**Soft delete** — marking a row dead without removing it. The only kind of delete there is.
**Audit trail** — the append-only record of who did what, when.
**`public_id`** — the UUIDv7 surrogate that appears in URLs, so an organization's name never
does ([ADR 0010](adr/0010-uuidv7-public-identifiers.md)).

### Kinyarwanda terms, as the sample reports use them

These are the words the business already uses. They are the report's vocabulary, not a
translation of ours.

| Term | Meaning | Raporo concept |
| --- | --- | --- |
| **Igishoro** | Capital; the money that bought the goods. | Cost basis / cost of goods |
| **Igishoro cy'ibyacurujwe** | The cost of what was sold. | Cost of goods sold (COGS) |
| **Inyungu** | Profit. | Gross profit |
| **Inyungu y'ibyacurujwe** | The profit on what was sold. | Profit for the period or cycle |
| **Amafaranga yarangujwe** | Money invested. | Capital contributed to a cycle |
| **Amafaranga yacurujwe** | Money taken from goods sold. | Revenue |
| **REST** | What is still in stock — capital still working. | Closing stock and its value |

Worked example from the Silver Rice sample: 350,000 Rwf in (10 sacks at 35,000), 8 sacks sold
for 340,500 Rwf, `igishoro cy'ibyacurujwe` 280,000, `inyungu` 60,500, `REST` 2 sacks =
70,000 Rwf still working.

## 7. Acceptance criteria

Each criterion is observable behaviour, verifiable by `qa-engineer` without asking what was
meant. The bracketed number is the slice that delivers it (see §10). Slice 1 is stated most
precisely, because slice 1 is what is being finished now.

### 7.1 Identity and account (slice 1)

- **IDENT-1** A registration submitting a username, an email, a phone and a password creates
  exactly one account, and that account can log in with any one of the three identifiers plus
  the password.
- **IDENT-2** Each of username, email and phone is unique across the whole system: a second
  registration reusing any one of them is refused, whichever organization the first account
  belongs to.
- **IDENT-3** All four of `0788123456`, `+250788123456`, `250788123456` and `788123456` store
  the same value, and after an account holds one of them a second account is refused for all
  four. Separators (`0788 123 456`, `+250-788-123-456`) do not change the outcome.
- **IDENT-4** A number from another country is accepted only in full international form
  (`+254712345678` stores `254712345678`); a bare national number from another country is
  refused.
- **IDENT-5** Registration without an email is refused, and the refusal names the email field.
- **IDENT-6** An unknown identifier and a known identifier with a wrong password produce the
  same message, the same status and the same response body; neither reveals whether the account
  exists.
- **IDENT-7** After 5 failed attempts against one identifier within 15 minutes, or 20 against
  one IP address, further attempts are refused with a lockout message; a successful login clears
  the counters.
- **IDENT-8** Stored passwords are argon2id hashes; no plaintext or reversible form of a
  password exists in the database or in any log.
- **IDENT-9** Requesting a password reset for a registered email and for an unregistered email
  return byte-identical responses; exactly one message is sent in the first case and none in the
  second.
- **IDENT-10** The emailed reset link sets a new password once, is dead on a second use, and is
  dead more than one hour after it was issued.
- **IDENT-11** With 2FA confirmed, a correct password alone leaves the session unauthenticated
  and renders a second stage; only a valid TOTP code or an unused recovery code completes the
  login. A recovery code works exactly once.
- **IDENT-12** The TOTP secret is not readable in plaintext in the database row that stores it.
- **IDENT-13** Logging out ends the session; using the browser's back button afterwards does not
  show authenticated content.

### 7.2 Organization, stores and access (slice 1)

- **ACCESS-1** Registering creates, in one transaction, the account, the organization, its first
  store, the three preset roles and the founder's Owner membership — or, if any part fails,
  none of them.
- **ACCESS-2** An organization accepts up to five live stores; the sixth attempt is refused with
  a message stating the limit. Two simultaneous creation attempts against an organization
  holding four stores leave exactly five.
- **ACCESS-3** A person who already holds a live membership cannot acquire a second one, and the
  refusal distinguishes "already a member of this organization" from "belongs to another
  organization".
- **ACCESS-4** After a membership is soft-deleted, the same person can join a different
  organization, and the earlier membership is still visible as history.
- **ACCESS-5** A member reaches exactly the stores their live store-access rows name; requesting
  any other store in the same organization returns 404 with a body identical to a request for a
  store that does not exist.
- **ACCESS-6** A membership whose role holds `store.access_all` reaches every live store in its
  organization, including a store created after the role was granted, with no propagation step
  and no store-access rows.
- **ACCESS-7** A role named "Owner" that does not hold `store.access_all` reaches only its
  granted stores, and the real owner role reaches every store even when it is renamed to
  something else.
- **ACCESS-8** Neither the Manager nor the Seller preset holds `store.access_all`.
- **ACCESS-9** Startup fails if any permission code in the catalog appears in no preset and in
  no declared unassigned set.
- **ACCESS-10** An actor without the required permission code is refused, and the refusal is
  recorded in the audit trail under the actor's own organization.
- **ACCESS-11** Only a member holding `invite.create` can create an invite; the invite names one
  role and a set of stores, and the link is shown once.
- **ACCESS-12** Accepting an invite creates a membership with exactly the invited role and store
  set. A second acceptance is refused. Two simultaneous acceptances produce exactly one
  membership.
- **ACCESS-13** A used link, an expired link and a revoked link render an identical page that
  states no reason.
- **ACCESS-14** No URL, form or endpoint lets a person join an existing organization without an
  invite.

### 7.3 Isolation — invariant #1 (slice 1, then permanently)

- **TENANCY-1** Every business row references exactly one store, and that store references
  exactly one organization; a row whose store and organization disagree is refused by the
  database, including via raw SQL.
- **TENANCY-2** No read path returns rows from two organizations. Covered explicitly: direct
  queries, reverse relations, joins, aggregates, `count`, `exists`, and the set operators
  `|`, `&`, `^`, `union`, `intersection` and `difference` in both operand orders.
- **TENANCY-3** No write path moves a row between stores or attaches a row to another store's
  parent. Covered explicitly: `create`, `bulk_create`, `update`, `bulk_update`, saving a subset
  of fields, and expression-valued updates.
- **TENANCY-4** A query issued without an organization context returns zero rows rather than
  another organization's rows, and the service boundary raises an error that names the missing
  context. *(designed, not yet built — see ADR 0009)*
- **TENANCY-5** The generated denial matrix passes: two organizations, two stores in the first,
  an owner, two store-scoped members, a member of the other organization, and a decoy role named
  "Owner" with no real power. Every denial is 404. Replacing the access check with a role-name
  comparison must turn the decoy's result from 404 to 200 and the real owner's from 200 to 404.
- **TENANCY-6** No organization name or slug appears in a URL; pages address organizations and
  stores by their `public_id`.
- **TENANCY-7** The runtime database role cannot update, delete or truncate the audit table,
  drop a trigger or a policy, take ownership of a table, or assume the migrating role.
- **TENANCY-8** Every report, export and share artefact contains rows from exactly one
  organization; a consolidated report contains rows from exactly one organization's stores.
  [slice 4]

### 7.4 Audit trail and deletion (slice 1)

- **TRAIL-1** No path hard-deletes a row: instance delete, queryset delete, base-manager delete
  and the equivalent raw statement are all refused.
- **TRAIL-2** An audit row cannot be changed or removed once written: update, delete and
  truncate are refused by the database, an insert carrying a pre-set primary key is refused, and
  the stored row is byte-identical after a forgery attempt.
- **TRAIL-3** Every state-changing action records the actor, the organization, the store where
  applicable, the target and the time. An unattributed action is refused unless it is explicitly
  marked as a system action.
- **TRAIL-4** Recording a change never stores a personal identifier's value: keys for email,
  phone, username, a person's name, contact and address store `[redacted]`, while identifiers and
  non-personal values (a price, a permission list, a store limit breach) are stored intact.
- **TRAIL-5** Erasing a person anonymises the account they own — email replaced with a
  non-routable value, phone cleared, credentials and 2FA destroyed — leaves every audit row
  untouched, and afterwards no audit row identifies that person. *(designed, not yet built)*
- **TRAIL-6** Startup fails when a model referencing a user account ships without a recorded
  erasure decision. *(designed, not yet built — `common.E200`)*
- **TRAIL-7** Exporting one organization produces that organization's data and no other's;
  erasing one organization leaves every other organization's rows intact.

### 7.5 Language (slice 1)

- **LANG-1** Every user-facing string in the product renders in English, Kinyarwanda and French;
  a build with an untranslated string fails.
- **LANG-2** The header switcher appears on the registration, login and password-reset pages,
  before any authentication, and changes the language of the next response.
- **LANG-3** An authenticated user's language choice persists on their account across sessions
  and devices.

### 7.6 Money (slice 2, applies wherever money is recorded)

- **MONEY-1** An amount in the base currency saves with no rate.
- **MONEY-2** An amount in any other currency cannot be saved without an exchange rate; the form
  refuses submission and states why.
- **MONEY-3** Before saving a foreign-currency amount the screen shows the converted base
  amount; the saved row carries the amount, the currency, the rate used and the converted base
  amount.
- **MONEY-4** A later change to a rate does not change any already-recorded amount.
- **MONEY-5** No monetary total differs from the exact sum of its parts by any amount, at any
  scale.

### 7.7 Products and stock (slice 2)

- **STOCK-1** Current stock for any product equals opening stock plus recorded restocks minus
  recorded sales, refunds adjusted, minus write-offs — with no field a human can type it into.
- **STOCK-2** A restock records quantity, purchase cost and an optional expiry date.
- **STOCK-3** A write-off is refused without one of the five reasons, requires the write-off
  permission, and appears in the period's report.
- **STOCK-4** A product sold singly and as a pack tracks one stock quantity across both forms.
- **STOCK-5** A sale priced below the product's derived floor is refused, and the refusal names
  the floor. An actor holding the below-floor override may complete it, and the override is
  recorded in the audit trail. (see [Q3](#12-open-product-questions))
- **STOCK-6** The latest purchase cost of a product is visible on its page.

### 7.8 Selling and owing (slice 3)

- **SELL-1** A sale records the actual price per line, which may be above or below the reference
  price subject to STOCK-5.
- **SELL-2** An order with a deposit counts the deposit as revenue for the day it was received,
  and nothing more.
- **SELL-3** An order with nothing paid is visible and counts no revenue.
- **SELL-4** The unpaid remainder of an order appears against that customer in the credit book,
  including when the goods have been delivered.
- **SELL-5** Marking an order fully paid and delivered removes it from the credit book and
  leaves the payment history intact.
- **SELL-6** Every sale and payment records its payment method.

### 7.9 The report (slice 4)

- **REPORT-1** A biweekly period runs from the 1st to the 15th and from the 16th to the last day
  of the month, in the organization's timezone, in months of 28, 29, 30 and 31 days.
- **REPORT-2** Two users of the same organization on devices in different timezones see
  identical totals for the same named period.
- **REPORT-3** Changing an organization's timezone changes which period a subsequently recorded
  event falls into, and does not silently re-file already-recorded events without saying so.
- **REPORT-4** Every total in a report equals the sum of the events recorded in that period; a
  report never shows a blank where a total belongs.
- **REPORT-5** Stock out and current stock in a report are derived from sales, refunds and
  write-offs, with no entry field behind them.
- **REPORT-6** `igishoro` and `inyungu` are computed from recorded costs and appear in the
  report using those words in the Kinyarwanda rendering.
- **REPORT-7** A store's report shows that store's name, and the branding resolved store →
  organization → Raporo default, per field; a store with the own-branding toggle off shows the
  organization's logo and colours.
- **REPORT-8** The consolidated cross-store report carries the organization's branding and lists
  each store separately as well as the combined total.
- **REPORT-9** A report can be produced as an image and as a PDF, and shared from a phone in one
  action.
- **REPORT-10** Generating a report requires the report permission; a Seller-preset member
  cannot generate one.

### 7.10 Money intelligence (slice 5)

- **INVEST-1** An investor profile records dated capital contributions, including top-ups after
  the first.
- **INVEST-2** A cycle links capital, purchases and sales, and at any moment reports revenue,
  `igishoro`, `inyungu`, capital still tied up in stock, and the cycle's duration.
- **INVEST-3** A cycle with two or more investors splits by the recorded percentage shares, and
  the shares must total 100 before the cycle can be saved.
- **INVEST-4** Each cycle stores its agreed investor/operator profit split and shows each side's
  computed share.
- **INVEST-5** A payout is a dated entry that reduces the investor's claim and appears on their
  profile.
- **INVEST-6** An investor's profile shows their capital working, `inyungu` realised, payouts
  taken, return and duration across all their cycles.
- **INVEST-7** An expense recorded in a period reduces that period's reported profit.

### 7.11 Alerts and automation (slice 6)

- **ALERT-1** A product whose stock falls to or below its threshold raises a low-stock alert to
  members who can act on it.
- **ALERT-2** A restock batch with an expiry date raises an expiry alert before that date.
- **ALERT-3** A scheduled report is delivered for the period it names, at the configured time in
  the organization's timezone, and delivery is off until someone turns it on.
- **ALERT-4** Turning scheduled delivery on is an explicit, revocable choice per channel; no
  consent box is pre-ticked.
- **ALERT-5** Organization and store branding, including the per-store own-branding toggle, can
  be edited in settings and the next report reflects the change.

### 7.12 Platform behaviour (slice 1)

- **PLATFORM-1** A fresh clone reaches a healthy, migrated application with the documented
  commands, and the health endpoint answers 200.
- **PLATFORM-2** An unknown URL renders the designed, translated 404 page; a server error
  renders the designed, translated 500 page; neither leaks a stack trace in production settings.
- **PLATFORM-3** Production responses carry the security headers the security baseline requires.
- **PLATFORM-4** The application refuses to start in production settings when the database it is
  pointed at is named like a test database.
- **PLATFORM-5** Continuous integration fails on a lint error, a missing migration, a failing
  test or a broken translation catalogue.
- **PLATFORM-6** Entry screens tolerate a brief network drop without losing the data already
  typed.

## 8. What v1 explicitly does not do

Stated flatly, so nobody plans around a feature that is not coming in v1.

- **Social login.** Not built and not stubbed. The architecture leaves room; nothing more.
- **SMS one-time codes.** Deliberately not v1 — cost plus SIM-swap risk. 2FA is TOTP with
  recovery codes.
- **A second organization for one person.** One organization per user (§4.2). Two businesses
  means two stores in one organization.
- **More than five stores per organization.** The cap is enforced (§4.3).
- **USD, or anything other than Rwf, as a base currency.** Foreign amounts are recorded against
  Rwf with a rate (§4.6).
- **A public API.** No DRF endpoints, no OpenAPI document. Deferred until a real mobile app or
  integration consumer exists; the service layer is what keeps that cheap
  ([ADR 0007](adr/0007-frontend-django-templates-htmx.md)). When it is built it ships
  documented from day one.
- **POS and e-commerce integrations.** No till, no storefront, no catalogue sync.
- **Online payment processing.** Raporo records how a customer paid; it never moves money.
- **Full offline entry.** Online-first; entry screens tolerate brief drops (PLATFORM-6) and
  nothing more.
- **Automatic exchange-rate fetching.** Rates are typed in.
- **An investor login.** Investors are records inside an organization in v1
  (see [Q10](#12-open-product-questions)).
- **Organization-mandated 2FA.** Per-user and optional in v1
  (see [Q5](#12-open-product-questions)).
- **Automatic archiving or deletion of old data.** Nothing ages out in v1 (§4.12).
- **Global or multi-country launch.** Rwanda first, and the product's defaults say so.
- **Automatic WhatsApp sending.** Sharing is one tap by the user; automated scheduled delivery
  starts with email, in slice 6, because WhatsApp automation is restricted by Meta.
- **Payroll, tax filing and general-ledger accounting.** Raporo reports on trading; it is not
  the books.

## 9. LATER, with the reason for each

| Deferred | Why not now |
| --- | --- |
| Social login | No user has asked; every hour spent on it is an hour not spent on the report. |
| SMS OTP | Per-message cost plus SIM-swap exposure; TOTP is free and stronger. |
| USD base currency | One base currency keeps every total comparable; a second one multiplies every report path. |
| Public API + OpenAPI | No consumer exists; the service layer means adding it later is weeks, not a rewrite ([ADR 0007](adr/0007-frontend-django-templates-htmx.md)). |
| POS / e-commerce integrations | Each is a partner integration with its own data model; none of the sample businesses has a till. |
| More than five stores | Five covers every observed case, and the owner's store set is enumerated in queries below roughly a hundred ([ADR 0011](adr/0011-org-wide-store-access-is-a-permission-code.md)). |
| Full offline entry | A local write store plus conflict resolution is its own product; brief-drop tolerance covers the observed failure. |
| A second organization per user | The join table stays, so allowing it later is dropping one constraint rather than migrating data. |
| Investor read-only login | It makes Raporo that investor's own data controller, which is a privacy and support commitment, not a screen. |
| Organization-mandated 2FA | Needs an organization setting, an enrolment grace period and a lockout story; per-user 2FA delivers most of the protection. |
| Per-store confidentiality inside one organization | Needs store-level database isolation; revisit if separate legal entities or a per-store investor portal become real ([ADR 0011](adr/0011-org-wide-store-access-is-a-permission-code.md)). |
| Automatic rate fetching | A rate source is a processor and a dependency; manual entry with a frozen rate is already correct. |
| Data archiving / partition-level destruction | Volume does not demand it yet, but the audit table needs year partitioning before the second set of ledger tables lands (privacy finding P-8). |
| Global launch | The product's defaults, language set and privacy basis are all Rwandan; changing them is a re-launch, not a setting. |

## 10. Delivery order

Six slices, tracked in [ROADMAP.md](ROADMAP.md). Each runs the full `/new-feature` pipeline.

| # | Slice | Delivers |
| --- | --- | --- |
| 1 | Foundation | Accounts and the three identifiers, hardened auth, 2FA-ready login, invites, organizations, stores 1–5, custom RBAC, audit and soft-delete core, isolation guards, EN/RW/FR with the header switcher, the containerised skeleton. |
| 2 | Products & stock | Per-store stock, variants and packs, restocks with cost and expiry, the floor rule, reference and latest prices, write-offs with reasons, the money mechanism. |
| 3 | Selling & owing | Sales, orders with deposits and the paid/delivered lifecycle, customers, the credit book, payments in and out. |
| 4 | The Report | The period engine, per-store and consolidated reports, branding resolution, shareable image and PDF. |
| 5 | Money intelligence | Expenses, investors and capital accounts, cycles with co-investor shares, profit splits and payouts, detail-page analytics. |
| 6 | Alerts & automation | Low-stock and expiry alerts, scheduled report delivery, branding settings. |

## 11. Release gates that are product requirements, not engineering preferences

- **A cross-tenant leak is release-blocking.** The denial matrix (TENANCY-5) is a gate on any
  change touching access, not a follow-up.
- **No feature ships with an untranslated string** (LANG-1).
- **No period-boundary change ships without `data-reporting-engineer` signing off**; timezone
  and biweekly boundaries are the product's hardest correctness problem.
- **A privacy pass against Law 058/2021 before launch**, including the items the privacy ruling
  marks as launch blockers ([Q6](#12-open-product-questions)).

## 12. Open product questions

Each has a recommended default. Work proceeds on the default until Elvis says otherwise.

1. **Which day does a week start on?** Nothing has decided this and slice 4 cannot build the
   weekly period without it. *Recommend:* Monday to Sunday, matching ISO weeks and the way the
   sample reports date themselves, with no per-organization setting in v1.
2. **Where does a business day end?** A sale rung up at 00:30 after a late closing belongs to
   somebody's day. *Recommend:* the calendar day in the organization's timezone, midnight to
   midnight, with no configurable cut-off in v1 — and make the recorded time visible so a
   mis-filed sale can be seen.
3. **What is the floor, and can it be overridden?** [PRODUCT.md](PRODUCT.md) says a sale can
   never go below purchase cost, but the permission catalog already ships
   `sale.below_floor_override` ("Sell below the floor price"), so the built shape is a
   permission-gated exception. And the cost basis is unpinned: latest purchase cost or weighted
   average. *Recommend:* the floor is the **latest purchase cost** (it is the replacement cost,
   which is what stops a loss on the next re-buy) while reported `igishoro` uses the weighted
   average; the override exists, sits only on the Owner preset, requires a reason, and is
   audited. Fix PRODUCT.md's absolute wording either way.
4. **Can an organization change its base currency?** The column exists with an ISO validator
   and a Rwf default, while §4.6 and the out-of-scope list say Rwf only. *Recommend:* the field
   is read-only in the v1 UI and displays Rwf; the column stays so a later decision is additive.
5. **Is organization-mandated 2FA in v1?** PRODUCT.md promises "org can require it for its
   members"; the slice-1 plan builds per-user 2FA only. *Recommend:* per-user and optional in
   v1, organization enforcement in LATER, and correct PRODUCT.md.
6. **Where does the server live, and who is registered with the regulator?** If the VPS is
   outside Rwanda, every byte of personal data is a cross-border transfer under Arts 48–49; and
   NCSA registration, a data-processing annex in the Terms and a named DPO are launch
   obligations (privacy ruling OD2, OD4 — both launch blockers). *Recommend:* prefer a Rwandan
   or African host at comparable price, otherwise document the Art 48/49 route; register before
   the first real customer's data lands, with Elvis as DPO.
7. **How long do we keep IP addresses?** Needed before the login work lands (privacy ruling
   OD1). *Recommend:* no full IP addresses in the audit trail; truncate to /24 for security
   actions only; full addresses only in the 15-minute throttle cache.
8. **What happens to personal data after an account or organization closes, and whose ten-year
   duty is it?** (privacy ruling OD5, OD6). *Recommend:* erase identifiers 30 days after
   closure, keep financial records and the audit trail ten years, destroy at term by dropping a
   partition — and state the ten-year retention in the Terms as the organization's own
   instruction, since the tax duty binds the trader, not the software.
9. **Do shop and organization names stay in un-erasable audit rows?** They are commercial
   identities, but for a sole trader the shop's name can be the person's name, and redacting
   them would make rename history worthless. *Recommend:* keep them, and say so in the privacy
   notice. This one is a policy call, not a defect.
10. **Are investors data subjects in their own right?** They become so the moment they get a
    login (privacy ruling OD3). *Recommend:* records inside the organization for v1, read-only
    investor login deferred, and the investor's own report delivered by the owner.
11. **Do target users need RRA electronic billing (EBM) receipts?** Nobody has decided, and it
    is the kind of obligation a VAT-registered trader will ask about on day one. *Recommend:*
    out of v1, and ask two real traders whether they issue EBM receipts today before deciding
    whether it belongs in the roadmap at all.

## 13. Where decisions live

| Decision | Owner document |
| --- | --- |
| Stack: Django 6.1, PostgreSQL, Docker | [ADR 0006](adr/0006-stack-django-postgres-react.md) (frontend part superseded) |
| Frontend: Django templates + HTMX; service layer; DRF only when a consumer exists | [ADR 0007](adr/0007-frontend-django-templates-htmx.md) |
| The organization column on store-scoped rows | [ADR 0008](adr/0008-denormalised-organization-on-store-scoped-rows.md) |
| Row-level security for organization isolation | [ADR 0009](adr/0009-row-level-security-for-organization-isolation.md) |
| UUIDv7 public identifiers in URLs | [ADR 0010](adr/0010-uuidv7-public-identifiers.md) |
| Org-wide store access is a permission code, never a role name | [ADR 0011](adr/0011-org-wide-store-access-is-a-permission-code.md) |
| Personal data inventory, erasure, retention, launch obligations | [privacy ruling](superpowers/specs/2026-09-02-privacy-law-058-2021-ruling.md) |
| Module layout, schema, store-scoping machinery | [architecture & schema design](superpowers/specs/2026-09-01-raporo-architecture-and-schema-design.md), [tenancy hardening design](superpowers/specs/2026-09-02-tenancy-hardening-design.md) |
| Slice 1's fourteen tasks | [slice-1 plan](superpowers/plans/2026-09-01-slice-1-foundation.md) |
| Report rendering technology (HTML → PDF/image) | Undecided; an ADR during slice 4 design |
