# Privacy ruling — Rwanda Law No. 058/2021 and the slice-1 data model

**Date:** 2026-09-02 · **Branch:** `feat/slice-1-foundation` · **Author:** `privacy-compliance`
**Scope:** the schema as built at commit `5e8e768` (accounts, orgs, audit, common).
**Requested by:** tech-lead merge gate, as the blocker on Task 4.

> **Controller note (2026-09-02):** P-1 below was reasoned from source by the reviewing agent,
> which had read-only tooling. The controller subsequently **reproduced it by execution**:
>
> ```
> record("user.created", changes={"email": "eva@example.rw", "phone": "250788000001",
>                                 "password": "S3cret!", "username": "eva"})
>
> STORED changes = {'email': 'eva@example.rw', 'phone': '250788000001',
>                   'password': '[redacted]', 'username': 'eva'}
> LEAKED VALUES  = ['eva@example.rw', '250788000001', 'eva']
> ```
>
> The password is redacted. The email, phone and username persist verbatim into a table whose
> plpgsql trigger refuses `UPDATE` and `DELETE`. **P-1 is confirmed, not suspected.**

## 0. Verdict, up front

**Task 4 MAY PROCEED**, conditional on two in-scope requirements landing in Task 4's own commit
(C1 and C4 in §6). Nothing in the current model is un-fixable and nothing requires redesign.

But the honest answer to the question the gate actually asked is not the comfortable one:

> **The audit trail is not PII-free today.** `apps/audit/services.py` redacts *credentials*, not
> *identifiers*. `record("user.created", changes={"email": ...})` stores that email verbatim,
> forever, in a table that refuses UPDATE and DELETE at the database level. Task 4 is precisely
> the code that would do this. So the append-only trigger *is* a compliance problem — for exactly
> one round, and only because `changes` is an unconstrained channel.

Close that channel and the question collapses to the good outcome: an audit row then contains only
foreign keys, an action verb, a class label, an integer and a timestamp — all of which stop
identifying anyone the moment the `User` row they point at is anonymized. **Erasure operates on
referents, not on the trail.** That is the resolution of the deletion-versus-immutability tension,
and it lets the append-only guard keep zero DELETE exemptions, which is a real security asset
worth preserving.

## 1. Q1 — Does the current audit redaction policy satisfy Law 058/2021?

**Ruling: No, not as written. One Critical finding (P-1). The fix is small.**

From `apps/audit/services.py`:

- `SENSITIVE_KEY_PARTS` holds eleven substrings: `password`, `passphrase`, `secret`, `token`,
  `totp`, `otp`, `recovery_code`, `api_key`, `authorization`, `session`, `cookie`. **Every one is a
  credential term. Not one is an identifier term** — no `email`, `phone`, `username`, `name`,
  `contact`, `address`, `ip`.
- `_redact()` replaces a value only when `_is_sensitive(key)` matches. Everything else passes
  through untouched, except strings over 1024 chars, which are *truncated* — a truncated free-text
  note still contains whatever PII its first 1024 characters held.
- `_clean_changes()` then JSON-serialises whatever survived and stores it.

The parts of the prior security verdict that **do** hold:

- **IDs-only logging is real.** `logger.info("audit.recorded", …)` emits `audit_id`, `audit_action`,
  `actor_id`, `org_id`, `store_id`, `target_type`, `target_id`. No identifiers. And `action` is
  regex-constrained, so PII cannot ride in on the verb.
- **IP validation is real** — but validation is a correctness control, not a privacy one.
- **`target_type` cannot be poisoned.** `record()` does not accept it; it is derived from
  `type(target)._meta`.
- **`actor`, `org`, `store`, `target_id` are pointers, not content.** Pseudonymous while the
  referent exists; non-identifying once it does not.

Legal analysis. Under Art 3's definition, `changes` content like an email or phone is plainly
personal data. Art 52 permits retention only until the purpose is fulfilled, with longer retention
where authorised by law or required by contract. An audit trail has a genuine legal-obligation
footing — Art 16 requires logging processing operations, Art 17 requires a documented retention
period — but that footing covers *the fact that a change occurred, by whom, to which record*. It
does not authorise permanently immutable storage of the **values** of a person's identifiers. Once
such a value is in the table, Art 23 (erasure) becomes literally unimplementable without a
migration.

**Verdict:** the trail's *structure* is compliant and well-designed. Its *payload discipline* is
not yet enforced.

## 2. Q2 — The erasure pathway

**Ruling: anonymize-in-place of the referents, composed with soft delete of the relationships.
One service, `erase_user()`. Not soft-delete-alone, and never hard delete.**

- **Hard delete** is barred by the project invariant and by `PROTECT` on every provenance FK.
  Deleting a user would require rewriting the provenance of every financial row they touched —
  destroying the evidentiary integrity Art 16 asks us to maintain.
- **Soft delete alone** does not satisfy Art 23. `deleted_at IS NOT NULL` with `email`, `phone`
  and `username` still populated is a retained record of an identified person with no live
  purpose. `User` is not even a `SoftDeleteModel` today; it has `is_active` only, and
  `User.delete()` raises `HardDeleteForbidden` with a docstring saying this decision has not been
  made. **This document makes it.**
- **Anonymize-in-place** satisfies Art 23 because erasure is discharged when the data no longer
  relates to an identifiable natural person. It preserves referential integrity, keeps the audit
  trail truthful, and needs no exemption in the append-only trigger.

### `erase_user(user, *, by, reason)` — required behaviour

Atomic, idempotent, writes exactly one audit row.

| Field | New value | Note |
|---|---|---|
| `username` | `erased-user-<pk>` | Passes `username_validator` and `validate_username_not_numeric`. |
| `email` | `erased-<pk>@erased.invalid` | `.invalid` is RFC 2606 reserved — can never be assigned to a real person or receive mail. Satisfies the `Lower(email)` CI unique constraint. |
| `phone` | **`NULL`** — requires a schema change | See P-4. Do **not** synthesise a number: `PHONE_REGEX` is `^[1-9][0-9]{7,14}$`, so every value it accepts is a plausible real subscriber number somewhere. Inventing one attributes an erased account to a stranger. |
| `password` | `set_unusable_password()` | |
| `language` | reset to `en` | Weak quasi-identifier. |
| `is_active`, `is_staff`, `is_superuser` | `False` | |
| `groups`, `user_permissions` | `.clear()` | Join records, not audited business data. An explicit carve-out from "no hard deletes anywhere" for tech-lead to bless. |
| `erased_at`, `erased_by` | set — **new columns** | Makes the state queryable and the operation idempotent. Add now while migrations are free. |

`last_login` and `date_joined` stay: once identifiers are gone they describe an anonymous account.

**On `Membership`:** `soft_delete(by=…)` every membership. Required — anonymizing the `User` alone
would leave `erased-user-412` in the org's live member list.

**On `StoreAccess`:** `soft_delete(by=…)` every row belonging to those memberships. `PROTECT` gives
no cascade, so the service must walk the graph. This is the same gap the code-reviewer carried
forward for Task 4 ("soft-deleting a parent leaves live children pointing at a dead store") — the
same walk serves both.

**On `AuditLog`:** **nothing.** No row written, rewritten or removed. `actor` keeps pointing at the
now-anonymous row. This is the mechanism, not a compromise.

**On credentials arriving in Task 6/7:** `TwoFactor.totp_secret` and `RecoveryCode` hashes must be
destroyed — they are credentials, not audit evidence. `Invite.contact` cleared wherever the erased
person is the invitee. Carry into Tasks 6 and 7 as acceptance criteria.

**One audit row:** `record("user.erased", actor=<by>, target=<user>, changes={"reason": "<enum>",
"fields_cleared": ["username","email","phone","password"]})` — field *names*, never values.

### The structural guarantee

Recommend **`common.E200`** (E001–E006 is model invariants, E100 is settings; 200 is a clean band):

> Every concrete model with a ForeignKey to `settings.AUTH_USER_MODEL` must appear in an
> `ERASURE_PLAN` mapping the model to `ANONYMIZE` / `SOFT_DELETE` / `RETAIN_FK`. Startup fails when
> a new model with a user FK is added without a recorded decision.

The highest-value item in this ruling. The failure mode this project guards against is "nobody
remembered this table held personal data", and E200 makes it impossible by construction. Must exist
before the first real user account (Task 5 or 9) — not before Task 4, which creates no models.

## 3. Q3 — Is an IP address personal data, and how long may we keep it?

**Ruling: yes, personal data. Do not keep it in the audit trail at all.**

An IP in `AuditLog` arrives bound to `actor_id` and a timestamp. Even if the address alone is weakly
identifying — and in Rwanda it often is, since most users reach us through MTN/Airtel carrier NAT —
the *combination* is information relating to an identified natural person under Art 3.

**Recommended design (OD1):**

1. **`AuditLog.ip` is not populated by ordinary business services.** Task 4 passes no `ip`.
2. **Where a security purpose genuinely exists** (login success/failure, password reset, 2FA change,
   invite acceptance), store a **truncated network prefix**: IPv4 → `/24`, IPv6 → `/48`. Serves the
   actual purpose (credential stuffing from one network, impossible-travel, ASN geography) while
   being materially less identifying, and degrades to nothing once the actor is anonymized.
3. **Full IP addresses live only in the throttle cache.** Task 5 already designs
   `throttle.allow(identifier, ip)` with 15-minute counters. That *is* the retention limit, and it
   already exists. State it explicitly so nobody later moves those counters into Postgres "for
   durability" and quietly creates a permanent IP log.
4. **If forensics needs full IPs**, a separate `SecurityEvent` table that is *not* append-only, with
   a hard TTL (recommend 90 days) and a tested purge command.

**Is append-only compatible with a retention limit?** Yes, given (1)–(3) — nothing in an audit row
then needs to expire.

**End-of-retention destruction is a separate, real problem.** At year 10+ the rows must genuinely
go, and today the only path is a whole-table `TRUNCATE` behind the `raporo.allow_truncate` GUC —
all-or-nothing, never per-org. Recommend **declarative range partitioning by year on `at`** for
`AuditLog` and slice 2's four ledger tables, adopted *before* those tables exist. Destruction then
becomes `DETACH PARTITION` + `DROP TABLE`: reviewed DDL, per-year, untouched by the row and
statement triggers, which guard DML only. **An ADR for `database-engineer`, before slice 2 makes it
four times more expensive.**

**A per-org audit purge before year 10 is deliberately not provided.** An org that closes has its
referents anonymized; its audit rows survive as non-identifying records. Recommending a DELETE
exemption would trade a genuine forgery guarantee for a marginal one. Stated as a conscious
trade-off so it is not rediscovered as an oversight.

## 4. Q4 — The per-tenant export and delete commands

### `manage.py export_org <slug> --out <dir>`

Serves Art 18, Art 20, and the practical duty to hand a customer their data on exit.

**Covers, in order:** `Organization` (row + the logo file) → `Store` → `Role` → `Membership` (with
each member's identifiers) → `StoreAccess` → `Invite` (metadata only) → all `AuditLog` rows for the
org → from slice 2 on, every store-scoped business row, store by store.

**Excludes:** password hashes, TOTP secrets, recovery-code hashes, invite token hashes, session
data. An export is a data-subject artefact, not a backup.

**Produces:** JSONL per table plus CSV per table, and a `manifest.json` with schema version,
generated-at, per-table row counts and a SHA-256 per file. Row counts are what makes the export
auditable — without them nobody can tell a complete export from a silently truncated one.

**Must be scoped through the tenancy guard.** An export command is the single most likely place in
this codebase for invariant #1 to be bypassed, because `all_objects` is right there and convenient.
Require a test that runs the export with two orgs populated and asserts byte-level absence of the
other org's rows.

**Writes one audit row** (`org.exported`) with actor, row counts and manifest hash. Never to a
world-readable path.

**Also needed: `export_user <id>`** — one person's own identifiers plus their own activity. The
Art 18/20 artefact for a staff member, and not the same document as the org export. A staff member's
rights run against Raporo for their *account*, and against their employer for the *business records*
they created. The Terms must say so.

### `manage.py erase_org <slug> --confirm <slug>`

Not a database drop. Four things, in order:

1. **Revoke access.** All memberships and store access soft-deleted; all sessions invalidated; all
   outstanding invites revoked and `contact` cleared. First, because everything after is slower and
   the account must shut immediately.
2. **Erase account-operation personal data.** `erase_user()` for every member whose *only*
   membership was in this org. Members who belong to another org keep their account and lose only
   this membership — a real case, easy to get wrong.
3. **Anonymize the org's own identifying content.** `name` → `Erased organization <pk>`, `slug` →
   `erased-<pk>` (safe: the slug unique constraint is conditioned on live rows), `logo` → **delete
   the file from storage** and clear the field, `brand` → `{}`. This matters more than it looks:
   for a Rwandan sole trader the shop name frequently *is* the owner's name, and the logo is
   frequently a photograph.
4. **Retain, non-identifying:** financial records and the audit trail, for the retention period,
   destroyed at term by partition drop.

**What legitimately survives:** financial records (Art 52 second limb — see OD6, the 10-year duty
binds the taxpayer, not its software vendor, so our cover is "required by contract"); the audit
trail including the audit of the deletion itself (`org.erased` is the evidence the request was
honoured); aggregates that identify nobody.

**The conflict, plainly:** Art 23 says erase; Art 16 says log; Art 52 resolves it by permitting
retention that is authorised or contractually required. The append-only trigger makes the resolution
*load-bearing* rather than rhetorical — we cannot quietly delete the trail even if we wanted to.
That is a feature, and it is only survivable because the trail's personal-data content reduces to
pointers. **Which is precisely why C1 gates Task 4.**

## 5. Q5 — PII inventory, lawful basis, consent

### As built (slice 1)

| Field / location | Personal data | Basis | Minimisation | Retention |
|---|---|---|---|---|
| `User.username` | yes | contractual necessity | fine | account life → anonymized |
| `User.email` | yes | contractual necessity — sole password-reset channel | defensible **because** it is the only reset channel; **may not be repurposed for marketing without separate consent** | → synthetic `.invalid` |
| `User.phone` | yes | contractual necessity (login + WhatsApp delivery) | three login identifiers is more than strictly needed, but phone-first is correct for Rwanda | → **NULL** |
| `User.password` | credential | contractual necessity | argon2id, confirmed | never exported, destroyed |
| `User.language` | weak | contractual necessity | fine | reset on erasure |
| `last_login`, `date_joined` | yes | contract + legitimate interest | fine | with the account |
| `created_by`/`updated_by`/`deleted_by` (every row) | pseudonymous | legal obligation (Art 16) + legitimate interest | fine | with the record |
| `AuditLog.actor/org/store/target_id` | pseudonymous | as above | fine | with the trail |
| **`AuditLog.changes`** | **unconstrained** | **none** | **P-1 Critical** | **un-erasable** |
| **`AuditLog.ip`** | **yes** | legitimate interest, unbounded | **P-2 High** | **none today** |
| `Organization.name`, `slug` | yes, when a sole trader | contractual necessity | fine | anonymized |
| **`Organization.logo`** | **potentially yes** | contractual necessity | **EXIF not stripped; old file never deleted on replace** | **P-5** |
| `Organization.brand`, `Store.brand` | free-form JSON | — | `clean()` only checks it is a dict | P-10 Low |
| `Membership`, `StoreAccess`, `Role` | the *fact of employment at a named shop* is personal data | contract / legitimate interest | fine | soft-deleted on departure |
| `Store.name` | sometimes a person's name | contractual necessity | fine | anonymized |

### Anticipated

- **`Invite.contact` (Task 7).** An email or phone belonging to someone who is **not a user and has
  consented to nothing**, stored on the inviter's say-so. The sharpest minimisation issue in
  slice 1. Basis: legitimate interest of the org in staffing — thin but workable **only if** it is
  optional, cleared on use/expiry/revocation, and never used for anything else. **P-6, High.**
- **`Customer`.** Third-party data subjects who never touch Raporo. **Raporo is processor; the
  Organization is controller.** Requires a written DPA with every org, no cross-tenant customer
  directory ever, and an anonymize-in-place path — a customer's Art 23 request against a shop
  cannot delete the sale, because the sale is a tax record.
- **Investor profiles with dated capital accounts.** A named natural person's full financial
  position. Not a special category, but high-sensitivity in practice. **See OD3.**
- **Unpaid customer balances.** Nearest thing in v1 to profiling with a financial effect. Ruled
  *not* a DPIA trigger — a debtor ledger is ordinary bookkeeping — but on the register, because
  anything that grows into scoring or cross-org sharing crosses the line.
- **2FA secrets and recovery codes (Task 6).** Credentials. Destroyed on erasure, never exported.
- **Throttle counters keyed by identifier + IP (Task 5).** Personal data with a 15-minute TTL.
  Compliant by construction — keep it that way.

### Consent

**Nothing in the product as specified requires consent, and nothing collects consent today.** Every
field rests on contractual necessity, legal obligation or legitimate interest, which is correct —
consent for data you need to run the service is a legal fiction and revoking it would break the
account.

Consent becomes required, and needs a model, when: **scheduled report delivery** lands (granular per
channel, revocable, **defaulted off**) · **any marketing use of `User.email`** · **any analytics on
public pages**.

Standing rule for `ux-designer` and `frontend-engineer`: a pre-ticked consent box is a finding, and
consent must be as easy to withdraw as to give.

### Processors and cross-border transfers

**Runtime today: none.** Postgres in Docker, media on local disk. Verified — the only application
logger is `raporo.audit` and it emits IDs only.

**Anticipated, each a processor requiring a contract and each potentially a cross-border transfer
under Arts 48–49:** SMTP/ESP · WhatsApp/Meta · the VPS host (**OD2 — the big one**) · object storage
· any error tracker.

**Explicit warning for `sre-observability`:** an error tracker of the Sentry class captures request
bodies and local variables by default. Dropped in unconfigured, it would exfiltrate passwords,
emails and phone numbers to a third-country processor on the first 500. If one is added: PII
scrubbing configured before the DSN, `send_default_pii=False`, and a documented transfer basis.

## 6. Q6 — What must be true before Task 4 writes the first service

- **C1 (required, Critical) — `audit.record` must refuse identifier values in `changes`.** Extend
  the redactor with a PII key set (`email`, `phone`, `username`, `name`, `first_name`, `last_name`,
  `full_name`, `contact`, `address`, `customer`, `investor`, `ip`, `logo`). Test:
  `record("user.created", changes={"email": "eva@example.rw"})` stores `{"email": "[redacted]"}`.
- **C2 (standing) — `changes` carries field names and IDs for anything personal; values only for
  non-personal fields.** A price change, a permission list, a store-limit breach: values are fine
  and wanted. A name, an email, a phone: name only. Write this into `apps/audit/services.py`'s
  docstring where the next service author works.
- **C3 (required) — no `register_owner` audit row may echo the user's identifiers.**
- **C4 (required) — Task 4 services pass no `ip`.** Registration is the one place a service is
  tempted to log the signup IP.
- **C5 (standing) — permission denials log `user_id`, `org_id`, `code`. Never username or email.**
- **C6 (standing) — no exception message from an orgs service may be logged with its payload.**
  `full_clean()` on a `User` can embed the email in a `ValidationError`.
- **C7 (standing) — an audit row for a soft delete must not echo the deleted row's personal
  fields.**
- **C8 (before Task 5/9) — `common.E200` and the `ERASURE_PLAN`.**
- **C9 (Low) — `audit.record()` is the only sanctioned writer**, but `AuditLog(...)` remains
  constructible. Consider a test asserting no module outside `apps/audit` imports it for writing.

## 7. Findings

| # | Sev | Location | Regulation | Remediation |
|---|---|---|---|---|
| P-1 | **Critical** | `apps/audit/services.py:29-60` | Arts 23, 52; minimisation | C1. **Confirmed by execution.** Blocks Task 4's commit, not its start |
| P-2 | High | `apps/audit/models.py:106` `AuditLog.ip` | Art 52 | §3 |
| P-3 | High | `apps/accounts/models.py:115-124` — no erasure path | Art 23 | `erase_user()` + E200 |
| P-4 | High | `User.phone` NOT NULL + unique | Arts 4, 23 | Make `phone` nullable at the DB level so erasure sets NULL instead of inventing a stranger's number. Free today |
| P-5 | Medium | `apps/orgs/models.py:61-66`; `common/validators.py:116-170` | Arts 4, 52 | Re-encode uploads to strip EXIF (also kills the polyglot class); delete the old file on replace |
| P-6 | Medium | `Invite.contact` (Task 7) | Arts 4, 52 | Optional; cleared on use/expiry/revoke; in the erasure path |
| P-7 | Medium | no `LOGGING` config | Art 16 | App logging is clean today. Add a no-PII policy; keep password-reset URLs out of prod access logs |
| P-8 | Medium | `apps/audit/migrations/0002`; `common/db.py` | Art 52 | Range-partition by year before slice 2 |
| P-9 | Medium | `common/db.py:107-111` | Art 16 | Role-separated TRUNCATE privilege. No migration issues TRUNCATE, so the objection that killed `REVOKE UPDATE, DELETE` does not apply |
| P-10 | Low (launch-blocking) | project-wide | Arts 5, 17, 43-44, 48-49 | Privacy notice; ROPA; NCSA registration as controller **and** processor; DPA annex; named DPO; breach runbook |

## 8. DPIA and breach notification

**DPIA: not required for v1 as scoped.** No special categories, no large-scale processing, no
systematic monitoring of a public area, no automated decisions with legal effect.

**Re-assess when:** scheduled report delivery to third parties starts · customer credit tracking
acquires anything scoring-shaped · investors get logins into another org's financials · the product
goes multi-country.

**Breach: Art 43 gives 48 hours** to notify the DPO after becoming aware; **Art 44 gives 72 hours**
for the written report. Data subjects must also be told unless the breach is unlikely to be high
risk. Needed before launch: who notices, who decides it is a breach, who files. Two notes specific
to this architecture — the audit trail is a genuine asset, because it is what lets us scope a breach
inside 48 hours; the flip side is that a breach *of the audit table* is a breach of the one table
that cannot be selectively purged.

## 9. Open decisions for Elvis

- **OD1 — IP retention.** *Recommend:* no full IPs in `AuditLog`; truncate to /24 and /48 for
  security actions only; full IPs only in the 15-minute throttle cache; a separate `SecurityEvent`
  table with a 90-day purge if forensics needs more. **Needed before Task 5.**
- **OD2 — Where does the VPS live?** PRODUCT.md says "low-cost VPS", and most are EU or US. If the
  host is outside Rwanda, **every byte of personal data we hold is a cross-border transfer** under
  Arts 48-49. *Recommend:* prefer a Rwandan or African host if the price is comparable; otherwise
  document the Art 48/49 route and apply to the NCSA. **Launch blocker, not a Task 4 blocker.**
- **OD3 — Are investors data subjects in their own right?** If an investor gets a read-only login
  they become an account holder and Raporo is their **controller**. *Recommend:*
  records-inside-the-org for v1, read-only login deferred.
- **OD4 — NCSA/DPO registration, DPA annex, named DPO.** *Recommend:* register before the first real
  customer's data lands; DPA annex in the Terms; Elvis as DPO. **Launch blocker.**
- **OD5 — How long after account closure do we hold account personal data?** *Recommend:* erase
  identifiers 30 days after closure; retain financial records and the audit trail 10 years; destroy
  at term by partition drop.
- **OD6 — Whose 10-year duty is it?** *Honest uncertainty:* Rwandan tax record-keeping obligations
  bind the taxpayer — the shop — not its software vendor. *Recommend:* state the 10-year retention
  in the Terms as a contractual instruction from the org, putting us on Art 52's "required by
  contract" limb rather than a legal duty we may not personally owe.

## 10. Citation confidence

Article numbers were checked against secondary sources and the NCSA Data Protection & Privacy
Office's article-by-article pages. The Official Gazette text could not be fetched in that thread,
so confidence is graded rather than asserted:

- **High:** Art 23 erasure · Art 24 rectification · Art 43 breach notification 48h · Art 44 breach
  report 72h · Arts 48-49 cross-border · Art 52 retention · Arts 56-62 offences, incl. Art 60 and
  Art 62 (5% of annual turnover).
- **Medium:** Art 16 logging · Art 17 records of processing · Art 18 right to personal data ·
  Art 19 objection · Art 20 portability · Art 26 representation · Arts 27-28 supervisory authority ·
  Arts 30-35 registration certificate · Art 53 administrative misconducts.
- **Uncertain, flagged rather than guessed:** the exact article for the **eight lawful bases**
  (substance well attested, sources disagree between Arts 5 and 6) · the exact article for the
  **processing principles** · the article imposing **registration** itself · the **DPIA**
  obligation, which appears derived from the risk-based principle plus the DPO's 2023 guidelines
  rather than an express article.

Before this document is relied on externally — an NCSA filing, a customer DPA, a privacy notice —
the Medium and Uncertain numbers should be confirmed against the Official Gazette n° Special of
15/10/2021. **No substantive ruling above turns on a number graded Uncertain.**

## 11. Final verdict for the merge gate

**Task 4 may proceed. No Critical finding is left open once C1 lands, and C1 is in Task 4's own
scope.**

1. **C1 in Task 4's commit** — with a test proving `changes={"email": …}` stores `[redacted]`.
   Without this, Task 4's first service creates permanently un-erasable personal data, and the
   verdict is **BLOCK**.
2. **C4 in Task 4's commit** — no Task 4 service passes `ip`.
3. **C2, C3, C5-C7 recorded as standing rules** in `apps/audit/services.py`'s docstring.
4. **This document is the erasure decision** the `User.delete()` docstring says does not exist.
   Update that docstring to point here when `erase_user()` lands.
5. **P-3, P-4 and C8 scheduled before the first real user account** (Task 5 or 9).
6. **OD1 answered before Task 5. OD2 and OD4 answered before launch.**

### Sources

[RwandaLII — Law 058/2021](https://rwandalii.org/akn/rw/act/law/2021/58/eng@2021-10-15) ·
[NCSA Official Gazette n° Special of 15/10/2021](https://cyber.gov.rw/fileadmin/user_upload/NCSA/Documents/Laws/OG_Special_of_15.10.2021_Amakuru_bwite.pdf) ·
[Rwanda DPO — Art. 52](https://dpo.gov.rw/dpp-law/dpp-article-52.html) ·
[Rwanda DPO — Sharing, transfer, storage and retention](https://dpo.gov.rw/dpp-law/sharing-transferstorage-and-retention-of-personal-data) ·
[Rwanda DPO — DPIA Guidelines (Dec 2023)](https://www.dpo.gov.rw/fileadmin/DPO/ComplianceTools/-_dpia-guide-and-form.pdf) ·
[Rwanda DPO — Breach Notification Form](https://dpo.gov.rw/fileadmin/DPO/ComplianceTools/Personal%20Data%20Breach%20Notification%20Form.pdf) ·
[Rwanda DPO — Registration Guide](https://dpo.gov.rw/fileadmin/DPO/ComplianceTools/registration-guide-for-data-controller-and-processor.pdf) ·
[DLA Piper — Rwanda](https://www.dlapiperdataprotection.com/index.html?t=about&c=RW) ·
[ALN Rwanda — Notable Developments](https://aln.africa/wp-content/uploads/2025/02/Notable-Developments-in-Rwandas-Data-Protection-and-Privacy-Regulatory-Landscape-Legal-Alert-ALN-Rwanda.pdf)
