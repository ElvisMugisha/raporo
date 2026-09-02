# Row-Level Security: design and threat model

Author: security-engineer · 2026-09-02 · branch `feat/slice-1-foundation`
Status: **design, ready to implement.** No code was written for this document.

Companion documents: `architect` owns the application/RLS responsibility split;
`database-engineer` owns the migration mechanics; `devops-engineer` owns credential
delivery. This document owns the **security substance**: the role model, the policy
shape, the failure modes of the context mechanism, and what an attacker can still do.

Every claim marked **MEASURED** was executed against a live PostgreSQL 18.6 in an
isolated compose project (`raporo-rls-sec`, torn down with `-v`), plus PgBouncer
1.25.2 in transaction-pooling mode. Probe transcripts are inline. Nothing in this
document is reasoned where it could be measured — that is this project's standing
lesson, and it changed three of the conclusions below.

---

## 0. What this work closes

At the end of fix round 3 I routed a finding to `devops-engineer` and it is still open:

> *the runtime DB role must not own these tables and must not hold TRUNCATE; compose
> currently connects as `raporo`, which is superuser and owner, so a compromised app
> process could wipe the audit trail in two statements.*

**This design is the answer to that finding, and it closes it rather than restating
it.** Section 1 splits the identity in two. Section 1.5 shows, measured, that the
runtime role cannot `TRUNCATE`, cannot `DROP TRIGGER`, cannot `DISABLE ROW LEVEL
SECURITY`, cannot `DROP POLICY`, cannot reassign ownership, and cannot `SET ROLE`
to the owner. The two-statement audit wipe stops being a privilege the app holds.

It also **unlocks a control that was correctly rejected in fix round 1.** The
`database-engineer` rejected `REVOKE UPDATE, DELETE ON audit_auditlog` on the
grounds that a single DB role would block Django's own migrations. With the role
split that objection evaporates: migrations run as the owner, the app never needs
`UPDATE` on the audit table, and the REVOKE turns audit forgery from a silent
zero-row no-op into a loud `42501 permission denied`. See §4.3 — it is measured,
and it is strictly better than what RLS alone gives that table.

### One correction to the premise I was handed

The brief says PostgreSQL 18 is "being verified in parallel". It landed during this
review: `compose.yaml` now pins `postgres:18` and mounts `pgdata:/var/lib/postgresql`.
Every measurement below is on 18.6, which is therefore the shipping target, not a
forecast. I spot-checked nothing on 17 because nothing here is version-dependent in
a way 17 and 18 disagree about.

---

## 1. The role model

Three lines, then the detail.

1. **`raporo_owner`** — owns the schema, every table, every policy, every trigger.
   Runs `migrate`. Never serves a request. Credentials live only in the migration
   job. Not superuser, no `BYPASSRLS`.
2. **`raporo_app`** — the runtime identity. Owns nothing, holds `SELECT/INSERT/
   UPDATE/DELETE` and nothing else, no `TRUNCATE`, no `BYPASSRLS`, no `CREATE`, no
   `CREATEDB`, no membership in `raporo_owner`. Subject to every policy.
3. **`raporo_backup`** *(optional, deploy-time)* — `BYPASSRLS` + read-only. Exists
   because `pg_dump` as `raporo_app` **fails** (measured, §6.5); without this role
   someone will "fix" backups by pointing them at the owner.

### 1.1 Why the owner is not just "postgres"

The bootstrap superuser (`POSTGRES_USER` in the image) creates the two roles and
then is never used again. Keeping `raporo_owner` distinct from the superuser means
a leaked migration credential cannot `ALTER SYSTEM`, create extensions, read
`pg_authid`, or drop the database — a real reduction, because the migration
credential is the one that travels through CI.

### 1.2 Bootstrap SQL (run once per database, as the superuser)

```sql
-- Roles. Passwords come from the secret store; never literals in a repo file.
CREATE ROLE raporo_owner LOGIN PASSWORD :'owner_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;
CREATE ROLE raporo_app   LOGIN PASSWORD :'app_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION;

-- The owner owns the schema; the app may look inside it and nothing more.
ALTER SCHEMA public OWNER TO raporo_owner;
REVOKE ALL   ON SCHEMA public FROM PUBLIC;
GRANT  USAGE ON SCHEMA public TO raporo_app;      -- USAGE, never CREATE
REVOKE ALL   ON DATABASE raporo FROM PUBLIC;
GRANT  CONNECT, TEMPORARY ON DATABASE raporo TO raporo_owner;
GRANT  CONNECT            ON DATABASE raporo TO raporo_app;   -- no TEMPORARY

-- Local/CI only: pytest builds and drops `test_raporo`. See §6.7.
ALTER ROLE raporo_owner CREATEDB;                 -- dev/CI databases only
```

`GRANT USAGE, not CREATE`, is load-bearing. **MEASURED:** with no `CREATE` on the
schema, `raporo_app` cannot define a `SECURITY DEFINER` function to launder its own
reads — `CREATE FUNCTION pwn() ... SECURITY DEFINER AS 'SELECT count(*) FROM
app.sales_sale'` → `ERROR: permission denied for schema app`. That single missing
grant closes the most obvious RLS escape. Withholding `TEMPORARY` on the database
removes the same trick via a temp-schema function.

### 1.3 Privileges, and the ones deliberately absent

```sql
-- Applied by a migration so the test database inherits it (see §6.7).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO raporo_app;
GRANT USAGE                          ON ALL SEQUENCES IN SCHEMA public TO raporo_app;

-- Every table the owner creates from now on is granted automatically.
ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES    TO raporo_app;
ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    GRANT USAGE                          ON SEQUENCES TO raporo_app;

-- The audit trail is insert-only for the runtime role, at the privilege level.
REVOKE UPDATE, DELETE ON audit_auditlog FROM raporo_app;

-- Django's own bookkeeping: the app never reads or writes it.
REVOKE ALL ON django_migrations FROM raporo_app;
```

**MEASURED:** `raporo_owner` can set its own default privileges, and a table created
afterwards is auto-granted the four DML privileges. **MEASURED, and it is the gap a
test must catch:** that same new table has `relrowsecurity = f` — default privileges
carry grants forward but **not** RLS. Forgetting `ENABLE ROW LEVEL SECURITY` on a
slice-2 table is therefore silent and it is a cross-tenant leak. §7.1 makes it loud.

Absent on purpose, each for a measured reason:

| Not granted | Why |
|---|---|
| `TRUNCATE` | RLS does **not** filter `TRUNCATE` at all. It is a separate privilege and withholding it is the only thing that stops a table wipe. **MEASURED:** `TRUNCATE sales_sale` and `TRUNCATE audit_auditlog` as `raporo_app` → `ERROR: permission denied for table`. |
| `SELECT` on sequences | `SELECT last_value FROM sales_sale_id_seq` is a cross-tenant volume oracle — total row count and growth rate for every org. **MEASURED:** after `REVOKE SELECT ON ALL SEQUENCES`, reading `last_value` → `ERROR: permission denied for sequence`, while `INSERT … RETURNING id` still works, because the column default needs only `USAGE`. |
| `BYPASSRLS` | The whole point. |
| `CREATE` on schema, `TEMPORARY` on database | §1.2. |
| `REFERENCES` | Lets a role create an FK to a protected table, which is an existence oracle by construction (§5.3). Not needed at runtime. |

### 1.4 Who runs migrations, and how the container switches identity

Two credentials, two code paths, no shared variable:

```
POSTGRES_USER / POSTGRES_PASSWORD          -> raporo_app       (serving)
RAPORO_MIGRATE_USER / RAPORO_MIGRATE_PASSWORD -> raporo_owner   (migrate only)
```

`config/settings/base.py` gains a second alias rather than a mutable `USER` key,
because a mutable key is a variable an attacker or a mistake can flip:

```python
DATABASES = {
    "default": { ... "USER": env["POSTGRES_USER"], ... },          # raporo_app
    "migrator": { ... "USER": env["RAPORO_MIGRATE_USER"], ... },   # raporo_owner
}
```

- `manage.py migrate --database=migrator` is the **only** command that uses the
  second alias. The alias's credentials are absent from the serving workload's
  environment entirely, so a compromised web process has nothing to switch to.
- `docker/entrypoint.sh` already has the right shape. Its `RAPORO_AUTO_MIGRATE`
  branch becomes `migrate --database=migrator`, and it must **fail loudly** if
  `RAPORO_MIGRATE_USER` is unset while `RAPORO_AUTO_MIGRATE=1` — not fall back to
  `default`. A fallback here recreates the exact single-role world this design
  exists to end. The entrypoint's existing polarity (guard by default, exempt by
  explicit list) is correct and does not change.
- Production has no auto-migrate. Migration is its own pipeline step, same image,
  `RAPORO_ROLE=tooling`, the owner credential injected only there. That was already
  `devops-engineer`'s recorded view; this design makes it a requirement.
- **Do not** implement this as "connect as owner then `SET ROLE raporo_app`."
  **MEASURED:** `RESET ROLE` mid-transaction climbs straight back out to the
  session user with full owner rights. `SET ROLE` is a convenience, not a boundary.
  The app must *log in* as `raporo_app`.

### 1.5 The runtime role cannot disarm any of it — MEASURED

```
-- as raporo_app, RLS enabled on sales_sale, append-only trigger on audit_auditlog
ALTER TABLE sales_sale DISABLE ROW LEVEL SECURITY;   ERROR: must be owner of table sales_sale
DROP POLICY org_isolation ON sales_sale;             ERROR: must be owner of relation sales_sale
DROP TRIGGER audit_auditlog_append_only ON …;        ERROR: must be owner of relation audit_auditlog
CREATE POLICY mine ON sales_sale USING (true);       ERROR: must be owner of table sales_sale
ALTER TABLE sales_sale OWNER TO raporo_app;          ERROR: must be owner of table sales_sale
TRUNCATE sales_sale;                                 ERROR: permission denied for table sales_sale
TRUNCATE audit_auditlog;                             ERROR: permission denied for table audit_auditlog
UPDATE audit_auditlog SET action='forged' …;          ERROR: relation … is append-only: UPDATE is not permitted
DELETE FROM audit_auditlog WHERE org_id=1;            ERROR: relation … is append-only: DELETE is not permitted
SET LOCAL row_security = off; SELECT … FROM sales_sale;
                                                     ERROR: query would be affected by row-level
                                                            security policy for table "sales_sale"
SET ROLE raporo_owner;                               ERROR: permission denied to set role "raporo_owner"
```

Note the `row_security = off` row: the GUC is *settable* by anyone, but the
subsequent query **errors** rather than returning unfiltered rows. Fail-closed and
loud. That is the one bypass an attacker who has read the PostgreSQL manual will
try first, and it is already shut.

### 1.6 Boot-time identity assertion — `common.E101`

RLS is enabled but **not forced** (§4.4), so the whole design rests on "the app is
not the owner". This project has learned what happens to a control nobody executes:
`common.E100` sat inert for two review rounds under the wrong check tag. So the
premise gets its own check, registered under `Tags.security` — **never**
`Tags.database`, which `CheckRegistry.run_checks` silently drops when no `--database`
alias is passed:

```
common.E101  the `default` connection's role must not own any application table,
             must not have BYPASSRLS or SUPERUSER, and must not hold TRUNCATE on
             any table. Gated on prod settings, same shape as E100.
common.E102  every table with an org-bearing column must have relrowsecurity = t
             and at least one policy.  (The §7.1 conformance query, at boot.)
```

E101/E102 do open a connection, unlike E100, so they run from the entrypoint's
pre-boot step with an explicit alias, or as a dedicated `manage.py rls_check`
invoked there. Whichever the `devops-engineer` prefers — but the test for them must
go through `django.core.checks.run_checks()` / the real command, not by calling the
function directly. A direct call is the tautology class the `code-reviewer` closed
in round 3 and it must not reopen here.

---

## 2. Trust boundaries and assets

| Boundary | Crosses it | Asset behind it |
|---|---|---|
| Browser → Django | session cookie, form/HTMX payloads, URL `public_id` (UUIDv7) | everything |
| View → service layer | `org`, `store`, record ids | the store/org scope decision |
| Service → ORM | querysets, `for_store()` pins | invariant #1 |
| **ORM → PostgreSQL** | **`raporo.org_id` GUC + SQL text** | **← RLS lives here** |
| App → filesystem | uploaded logos | stored XSS (closed, F7) |
| Deploy → DB | two credentials | schema, audit trail |

RLS defends exactly one boundary: the last mile into PostgreSQL. Its value is that
it is the only guard that is *not* a Python object a developer can forget to use.
Its limit is that it trusts one integer that the application itself supplies.

Assets, ranked: (1) other tenants' sales data — invariant #1; (2) the audit trail's
integrity; (3) user credentials and PII; (4) availability of reports.

Abuse cases RLS is meant to catch, and each is a bug class this codebase has
already produced at least once: a queryset built from `all_objects` in a hurry; a
new store-scoped model whose author forgets the manager; a reverse relation that
reappears; a raw SQL report; a `values_list()` that walks a join; a set-operator
synonym for an unscoped read. Fix rounds 1–3 closed five of those *individually*.
RLS closes the class.

---

## 3. The tenancy key, and one naming decision that must be pinned first

The settled design puts a denormalised organization column on `StoreScopedModel`
next to `store`, with a composite FK making disagreement impossible. Good — and the
composite FK is worth more than it looks (§5.3).

**Settled by ADR 0008, and it has a consequence worth naming.** ADR 0008 puts
`organization_id` on store-scoped tables, with a composite key
`(organization_id, store_id) → orgs_store (id, org_id)`. So the schema will carry
**two names for one concept**: `organization_id` on every store-scoped table and
`org_id` on `orgs_store`, `orgs_role`, `orgs_membership`, `orgs_storeaccess` and
`audit_auditlog`. That is legal and the composite FK handles the mismatch, but
policy SQL is a *string* — it does not follow a rename, and every policy plus its
`PINNED_SQL` hash would have to be re-issued if the names are ever unified.

Requirement either way: the column name must live in exactly one Python constant
that the policy-generating helper reads, and the conformance test in §9 must resolve
the tenancy column **from the model registry**, not from a hard-coded string — the
same defect shape that made `dumped_app_labels()` and the pin scan miss things in
round 3. This document writes `org_id` in policy examples; substitute
`organization_id` on store-scoped tables.

On the UUIDv7 public identifier: it is a good change and it removes the enumerable
integer from URLs. It is **not** an authorization control. UUIDv7 is time-ordered,
so it leaks row creation time and is partially predictable within a window. Treat it
as an opaque name, never as a capability. RLS plus the store-access check is what
makes it safe to expose. Also: a unique index on `public_id` is an existence oracle
for an attacker who can supply one at insert time (§5.4) — the app must never let a
client choose it.

---

## 4. Policy shape

### 4.1 The context accessor

```sql
CREATE OR REPLACE FUNCTION raporo_current_org() RETURNS bigint
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT nullif(pg_catalog.current_setting('raporo.org_id', true), '')::bigint
$$;
```

Four deliberate details, three of them measured:

- **`nullif(…, '')` is not optional.** The proposal's bare
  `current_setting('raporo.org_id', true)::bigint` is *unsafe in the availability
  sense and surprising in the security sense* — see §5, where an empty-string GUC
  raises `22P02` instead of returning no rows, and where the GUC becomes an empty
  string as soon as the connection has served one request.
- **`pg_catalog.`-qualified** so no `search_path` can shadow `current_setting`.
  `raporo_app` cannot create functions anywhere (§1.2), so this is defence in depth
  rather than a live hole — but it costs nothing.
- **No `SET search_path` clause on the function.** A SQL function with a `SET`
  clause **cannot be inlined by the planner**. **MEASURED**, same query, same
  index-only scan over 6 000 of 228 003 rows:

  ```
  policy uses raporo_current_org()          -> Index Cond: (org_id = (NULLIF(current_setting(...))::bigint)
                                               Execution Time: 1.280 ms
  policy uses the SET search_path variant   -> Index Cond: (org_id = raporo_current_org_pinned())
                                               Execution Time: 14.760 ms
  ```
  11.5x, for a cosmetic hardening that the missing `CREATE` grant already provides.
  Qualifying inside the body gets the safety and keeps the inlining.
- **`STABLE PARALLEL SAFE`**, not `IMMUTABLE`. `IMMUTABLE` would license the planner
  to fold the value at plan time and reuse it across requests with a cached plan —
  the connection-reuse leak of §6, in a new costume.

### 4.2 The store-scoped business tables (the slice-2 shape)

`USING` and `WITH CHECK` both, per command, no `FOR ALL` shorthand:

```sql
ALTER TABLE sales_sale ENABLE ROW LEVEL SECURITY;

CREATE POLICY sales_sale_org_select ON sales_sale FOR SELECT
    USING      (org_id = raporo_current_org());
CREATE POLICY sales_sale_org_insert ON sales_sale FOR INSERT
    WITH CHECK (org_id = raporo_current_org());
CREATE POLICY sales_sale_org_update ON sales_sale FOR UPDATE
    USING      (org_id = raporo_current_org())
    WITH CHECK (org_id = raporo_current_org());
CREATE POLICY sales_sale_org_delete ON sales_sale FOR DELETE
    USING      (org_id = raporo_current_org());
```

**Why a read-only policy is insufficient.** `USING` alone answers "which rows may I
see". It says nothing about which rows I may *create* or what a row may *become*.
**MEASURED**, context pinned to org 1, `USING`-only would have permitted every one
of these:

```
INSERT INTO sales_sale (org_id, store_id, total) VALUES (2, 20, 1);
    -> ERROR: new row violates row-level security policy   [WITH CHECK caught it]
UPDATE sales_sale SET org_id = 2, store_id = 20 WHERE id = 1;
    -> ERROR: new row violates row-level security policy   [WITH CHECK caught it]
```

The second is the one that matters and it is the classic mass-assignment shape: the
row is mine, `USING` lets me touch it, and I move it into someone else's tenant. A
`USING`-only policy is a *confidentiality* control with no *integrity* half. It also
runs the other way — a tenant could push a poisoned row into a rival's reports,
which for a reporting product is arguably worse than reading one.

The reads, for completeness — the guard is silent on the read side and loud on the
write side, which is the correct asymmetry:

```
UPDATE sales_sale SET total = 0 WHERE org_id = 2;   -> UPDATE 0     (silently filtered)
DELETE FROM sales_sale WHERE org_id = 2;            -> DELETE 0     (silently filtered)
UPDATE sales_sale SET total = total;                -> UPDATE 2     (only my own two rows)
DELETE FROM sales_sale;                             -> DELETE 2     (only my own two rows)
```

Note the last two: a `WHERE`-less blind write hits *only* the caller's tenant. That
is RLS earning its keep — the same statement without RLS is a company-ending event.

**Per-command policies, not `FOR ALL`.** Three reasons, all practical. (a) It lets
the audit log have a wider `INSERT` than `SELECT`, which it needs. (b) Omitting a
command is default-deny, so a table with only SELECT+INSERT policies is *append-only
by omission*. (c) `FOR ALL`'s `WITH CHECK` defaults to its `USING` expression, which
is convenient right up to the day the two must differ and someone edits the wrong one.

**Restrictive policies as a floor.** Permissive policies OR together. **MEASURED:**
adding one careless reporting policy —

```sql
CREATE POLICY oops_reporting ON sales_sale FOR SELECT USING (true);
```
— took visibility from "my org" to "every row in the table", instantly, with the
correct policy still in place. Because policy sets grow (a reporting read model, an
admin view, a support tool), add one `RESTRICTIVE` floor per table:

```sql
CREATE POLICY sales_sale_org_floor ON sales_sale AS RESTRICTIVE
    USING      (org_id = raporo_current_org())
    WITH CHECK (org_id = raporo_current_org());
```

**MEASURED:** with the floor in place, the same careless `USING (true)` policy left
visibility at 2 rows / 1 org. `RESTRICTIVE` policies AND with everything, so no
future permissive policy can widen the tenant boundary. This is the single cheapest
piece of future-proofing in the design and I want it on every tenant table.

### 4.3 `audit_auditlog` — the interesting case

Three properties collide: append-only, tenant-scoped, and legitimately carrying
org-only or store-only values (which the `MATCH SIMPLE` composite FK deliberately
permits). Resolution:

```sql
ALTER TABLE audit_auditlog ENABLE ROW LEVEL SECURITY;

-- Tenants read only their own org's rows. Platform rows (org IS NULL) are not
-- theirs and stay invisible; the owner and the backup role still see everything.
CREATE POLICY audit_org_select ON audit_auditlog FOR SELECT
    USING (org_id = raporo_current_org());

-- Writes: own-org rows always; a platform row only when there is genuinely no org
-- context yet (signup, login, password reset). Not "whenever convenient".
CREATE POLICY audit_org_insert ON audit_auditlog FOR INSERT
    WITH CHECK (
        org_id = raporo_current_org()
        OR (org_id IS NULL AND raporo_current_org() IS NULL)
    );

-- No UPDATE policy and no DELETE policy: default-deny at the row level …
-- … and REVOKE on top of it, because default-deny is silent and REVOKE is loud.
REVOKE UPDATE, DELETE ON audit_auditlog FROM raporo_app;

-- The trigger stays. It is the only guard that also binds the owner.
CREATE POLICY audit_org_floor ON audit_auditlog AS RESTRICTIVE
    USING (org_id = raporo_current_org() OR org_id IS NULL);
```

**Three measured findings shaped this, and two of them are traps.**

**(a) `RETURNING` needs the `SELECT` policy to pass, not just `WITH CHECK`.** This
is the sharpest edge in the whole design, because the Django ORM emits
`INSERT … RETURNING id` for every model with an auto pk — always, unavoidably.

```
-- context = org 1
INSERT INTO audit_auditlog (org_id, store_id, action) VALUES (1,10,'ok') RETURNING id, org_id;
    ->  id | org_id
        ----+--------
          7 |      1                                  [fine: WITH CHECK ok, SELECT ok]

INSERT INTO audit_auditlog (org_id, action) VALUES (NULL,'user.created');
    ->  INSERT 0 1                                    [fine: WITH CHECK allows org IS NULL]

INSERT INTO audit_auditlog (org_id, action) VALUES (NULL,'user.created') RETURNING id;
    ->  ERROR: new row violates row-level security policy for table "audit_auditlog"
```

Identical row, identical `WITH CHECK`, and the ORM-shaped statement fails. So a
policy set whose `WITH CHECK` is broader than its `SELECT` `USING` is **unusable
from Django** — the write succeeds in raw SQL and fails through the ORM, which is
the worst possible place for a discrepancy to live. Two ways out, and the design
must choose one explicitly:

1. **Platform audit rows are written on the migrator/owner path.** Cleanest
   security posture, but signup and login run on the app path, so it does not fit.
2. **Widen the `SELECT` policy to `org_id = raporo_current_org() OR (org_id IS NULL
   AND raporo_current_org() IS NULL)`** — mirroring the insert policy exactly. A
   session with no org context can then read platform rows. That is acceptable
   (they contain no tenant data by definition) *provided* the tenant-facing audit
   view is always queried with context set, which it is. **Recommended.** The
   important part is that `USING` and `WITH CHECK` must be kept **identical** on
   this table, and a test must assert they are — because the failure mode when they
   drift is a 500 on signup, in production, with no local reproduction.

**(b) RLS makes the append-only alarm go quiet, and the REVOKE brings it back.**
With `SELECT`+`INSERT` policies only, `UPDATE`/`DELETE` are default-denied *by
returning zero rows*:

```
-- context = org 1, no UPDATE/DELETE policy, UPDATE/DELETE still GRANTed
UPDATE audit_auditlog SET action='forged' WHERE org_id = 2;   -> UPDATE 0
DELETE FROM audit_auditlog WHERE org_id = 2;                  -> DELETE 0
UPDATE audit_auditlog SET action='forged' WHERE org_id = 1;   -> UPDATE 0
```

Zero rows means the row never entered the statement, so the `BEFORE UPDATE` trigger
never fired and **nothing raised**. Forgery attempts stop being detectable. After
the REVOKE:

```
UPDATE audit_auditlog SET action='forged' WHERE org_id=1;  -> ERROR: permission denied for table audit_auditlog
DELETE FROM audit_auditlog WHERE org_id=1;                 -> ERROR: permission denied for table audit_auditlog
-- and as the OWNER, which the REVOKE does not bind:
UPDATE audit_auditlog SET action='forged' WHERE org_id=1;  -> ERROR: relation app.audit_auditlog is append-only:
                                                                     UPDATE is not permitted
```

`REVOKE` binds the app loudly; the trigger binds the owner loudly; the RLS
default-deny is the quiet backstop underneath both. All three, and the frozen
`CREATE_APPEND_ONLY_FUNCTION_V1` needs no edit — which matters, because editing it
would break `PINNED_SQL`.

**(c) The `MATCH SIMPLE` blind spot becomes a hiding place.** A row with
`store_id` set and `org_id` NULL skips the composite FK entirely (that is what
`MATCH SIMPLE` means) *and* is invisible to every org-scoped reader. It is a place
to put audit rows nobody can see. Low severity — the owner and the backup role still
see them, and the insert policy above already refuses them whenever context is set —
but the fix is free and belongs in the same migration:

```sql
ALTER TABLE audit_auditlog
    ADD CONSTRAINT audit_auditlog_store_needs_org
    CHECK (store_id IS NULL OR org_id IS NOT NULL);
```

This does not weaken the `MATCH SIMPLE` behaviour the `database-engineer` blessed —
org-only rows stay legal, which is the case that mattered. It removes only
*store-without-org*, which was never meaningful.

### 4.4 Which tables get policies

| Table | RLS | Policy key | Note |
|---|---|---|---|
| every `StoreScopedModel` table (slice 2+) | yes | `org_id = raporo_current_org()` | + RESTRICTIVE floor |
| `orgs_organization` | yes | `id = raporo_current_org()` | its own pk *is* the tenancy key |
| `orgs_store` | yes | `org_id = …` | |
| `orgs_role` | yes | `org_id = …` | |
| `orgs_membership` | yes | see below | the bootstrap problem |
| `orgs_storeaccess` | yes | `org_id = …` | the denormalised `org` earns its keep |
| `audit_auditlog` | yes | §4.3 | |
| **`accounts_user`** | **no** | — | see below |
| `django_session`, `django_content_type`, `auth_permission`, `django_admin_log`, `django_migrations` | no | — | not tenant-scoped; `django_migrations` is REVOKEd outright |

**`accounts_user` gets no org policy, and that is a decision, not an omission.**
A user is not owned by an organization — `Membership` is `(user, org)` and nothing
stops one person belonging to several orgs, which is a real Rwanda-SME shape (an
accountant serving three shops). More decisively: **authentication reads
`accounts_user` before any org context can exist.** Any org policy on that table
either breaks login or needs an `OR context IS NULL` escape hatch, and an escape
hatch on the credential table is worse than no policy. So:

- No RLS on `accounts_user`. Cross-tenant user *enumeration* and user-PII exposure
  stay an application-layer responsibility. **Routed to `privacy-compliance`:** the
  one table holding Law 058/2021 personal data is the one table RLS does not cover,
  so the `anonymize()` service and the account-lookup surfaces carry that weight
  alone. This is not new exposure — it is today's posture, unchanged — but it should
  be recorded as a conscious carve-out rather than discovered later.
- A `raporo.user_id` GUC is **not** worth adding for this. It doubles the context
  surface and the thing it would protect (a user reading another user's row) is not
  invariant #1.

**`orgs_membership` has a bootstrap problem worth naming.** Login resolves *which
orgs this user belongs to* before an org is chosen, so that read happens with no
context. Two workable answers:

1. `USING (org_id = raporo_current_org() OR (raporo_current_org() IS NULL AND user_id = <the authenticated user>))` — needs the `raporo.user_id` GUC after all, and a `(user_id)` index.
2. **Recommended:** a single `SECURITY DEFINER` function owned by `raporo_owner`,
   `raporo_orgs_for_user(bigint) RETURNS TABLE (org_id bigint, store_id bigint)`,
   `EXECUTE` granted to `raporo_app`. It returns ids only — no names, no PII — and it
   is the one auditable hole in the fence, twenty lines long, reviewable in one
   sitting. The base table keeps a plain, unconditional `org_id = raporo_current_org()`
   policy with no NULL escape hatch anywhere.

   Mandatory if this is chosen: the function is `SET search_path = pg_catalog, public`
   (a `SECURITY DEFINER` function without a pinned `search_path` is a privilege-
   escalation primitive, and unlike §4.1 the inlining cost does not apply here —
   it is called once per login, not once per row), and it takes the user id as an
   argument the caller cannot forge into another user's — meaning the *caller* is
   the Django auth backend, which already knows the authenticated user, and the
   function must never be reachable from a request parameter.

---

## 5. Fail-closed context: verdict and probe output

### Verdict

**The claim holds — but only with `nullif`, and the proposal as written does not
have it.** The proposed `current_setting('raporo.org_id', true)::bigint` is safe in
the confidentiality sense in every case I could construct, but it does **not**
behave as described: it does not return "no rows" when context is missing, it
*raises*, in the single most common missing-context case, and that case is the one
that appears after the connection has served exactly one request.

| # | GUC state | `current_setting(…, true)` | naive `::bigint` | with `nullif(…, '')` |
|---|---|---|---|---|
| 1 | never set, fresh connection | `NULL` | **0 rows / insert refused** | 0 rows / insert refused |
| 2 | set to `''` | `''`, length 0, **not NULL** | **ERROR 22P02** | 0 rows / insert refused |
| 3 | set then `RESET` | `''`, **not NULL** | **ERROR 22P02** | 0 rows / insert refused |
| 4 | after a `SET LOCAL` transaction ends | `''`, **not NULL** | **ERROR 22P02** | 0 rows / insert refused |
| 5 | `'abc'` | `'abc'` | ERROR 22P02 | ERROR 22P02 |
| 6 | `'1 OR 1=1'` | `'1 OR 1=1'` | ERROR 22P02 | ERROR 22P02 |
| 7 | `'99999999999999999999'` | as set | ERROR 22003 | ERROR 22003 |
| 8 | `' 2 '` / `'+2'` | as set | **casts to 2, returns org 2's rows** | same |
| 9 | another org's id | as set | that org's rows | that org's rows |

Rows 2–4 are the finding. **`RESET` does not restore "unset"** — once a custom GUC
placeholder exists in a session it holds the empty string, not NULL. Rows 1 and 2–4
therefore differ, which means *the very first unguarded request on a connection
returns zero rows and every later unguarded request on the same connection throws
`22P02`*. A bug that appears only after the connection has served one request is
the kind that passes review and fails in production.

**Is a raised error better or worse than no rows?** Better, and I want the loud
version where I can get it — an exception is a Sentry event, an alert, a bug report;
zero rows is a report that says "no sales this period" and a shopkeeper who believes
it. This is a reporting product, so a silent zero is a *correctness* failure with
business consequences. But `22P02` is the wrong loud: it is a raw
`django.db.utils.DataError` from deep inside a query, with a message about bigint
syntax that names neither tenancy nor the missing context, arriving at whichever
query happened to run first. So:

- **Policies use `nullif`** → missing context is uniformly "no rows", never a
  data-type error. Predictable, and predictable is what a security control owes you.
- **The app raises the loud error itself**, early and with a useful message: the
  service-layer entry point asserts context is set and matches the org it was asked
  about (§7.3, `assert_org_context`). One `SELECT raporo_current_org()` per request
  at most, and it converts every missing-context bug from "empty report" into
  "loud failure at the boundary that caused it".
- **Canonicalise before setting.** Row 8 shows `' 2 '` and `'+2'` cast happily.
  Not a vulnerability — a caller who can set the GUC has already won — but the value
  passed to `set_config` must be `str(int(org_id))`, never a string from a request.

### Row 9 is the honest limit, and it deserves a paragraph

```
SET raporo.org_id = '1'; SELECT id, org_id, total FROM sales_sale ORDER BY id;
    ->  1 | 1 | 100.00
        2 | 1 | 200.00
SET raporo.org_id = '2'; SELECT id, org_id, total FROM sales_sale ORDER BY id;
    ->  3 | 2 | 999.00
-- and it needs no SET statement at all, only a function call inside a SELECT:
SELECT set_config('raporo.org_id','2',false);
SELECT id, org_id, total FROM sales_sale ORDER BY id;
    ->  3 | 2 | 999.00
-- and it can be re-pointed mid-transaction, after the context was correctly set:
BEGIN; SET LOCAL raporo.org_id='1'; SELECT count(*) FROM sales_sale;  -> 2
       SET LOCAL raporo.org_id='2'; SELECT count(*) FROM sales_sale;  -> 1  (RIVAL)
```

**Anyone who can execute arbitrary SQL owns the tenancy boundary.** The
`set_config()` variant is the one to remember: it needs no `SET` statement, only the
ability to call a function inside a `SELECT`, so a read-only injection point that
tolerates a function call is enough. RLS does not defend against SQL injection; it
defends against *missing filters*, which is a completely different and far more
common bug. Parameterised queries remain the primary control, and
`StoreScopedManager.raw()` must stay refused.

### 5.3 What the composite FK does for RLS, measured

RLS does **not** filter referential-integrity checks — PostgreSQL's RI triggers run
with row security off. That is normally an existence oracle. **MEASURED**, context
= org 1, store 20 and sale 3 belong to RIVAL and are invisible:

```
-- composite (store_id, org_id) FK  -- store 20 is RIVAL's:
INSERT INTO sales_sale (org_id, store_id, total) VALUES (1, 20, 5);
    -> ERROR: violates foreign key constraint "sales_sale_store_same_org_fk"
       DETAIL: Key is not present in table "orgs_store".
-- composite FK -- a store id that exists nowhere:
INSERT INTO sales_sale (org_id, store_id, total) VALUES (1, 9999, 5);
    -> ERROR: violates foreign key constraint "sales_sale_store_same_org_fk"
       DETAIL: Key is not present in table "orgs_store".          [IDENTICAL — no oracle]

-- SINGLE-column FK child -> invisible parent (sale 3, RIVAL's):
INSERT INTO sales_saleline (org_id, store_id, sale_id, qty) VALUES (1, 10, 3, 1);
    -> INSERT 0 1                                        *** ACCEPTED ***
-- SINGLE-column FK child -> a sale id that exists nowhere:
INSERT INTO sales_saleline (org_id, store_id, sale_id, qty) VALUES (1, 10, 99999, 1);
    -> ERROR: violates foreign key constraint "sales_saleline_sale_id_fkey"
```

The composite key makes the two cases indistinguishable and closes the oracle. The
single-column key does worse than leak: **it accepts the row**, creating an org-1
line item attached to org-2's sale, and RLS did not stop it. So:

> **Requirement, and it is the most important one in this document for slice 2.**
> Every FK from a store-scoped table to another store-scoped table must be composite
> on `(target_id, org_id)`, not just `(target_id)`. A single-column FK is an RLS
> bypass for writes and an existence oracle for reads. This is the same reasoning
> that produced the four `*_same_org_fk` keys; the denormalised `org_id` is what
> makes it mechanical instead of bespoke. `common.checks` should grow an `E007` that
> fails startup when a store-scoped→store-scoped FK is not backed by a composite
> constraint, because "remember to write the RunSQL" is exactly the class of
> instruction this codebase has watched fail four times.

Also measured, and reassuring: RLS's `WITH CHECK` and the composite FK are
complementary, not redundant. `INSERT (org_id=1, store_id=20)` passes `WITH CHECK`
(the org column *does* match the context) and is caught only by the FK — and it is
still caught when `SET CONSTRAINTS ALL DEFERRED` is in force, surfacing at `COMMIT`.

### 5.4 Oracles RLS does not close

- **Unique-constraint collisions.** `INSERT INTO orgs_store (id, org_id, …) VALUES
  (20, 1, …)` → `ERROR: duplicate key value violates unique constraint
  "orgs_store_pkey"`, proving row 20 exists in an invisible tenant. Mitigated by the
  UUIDv7 change *only if* clients can never supply an id or a `public_id`. `E005`
  already handles the business-uniqueness half of this.
- **Sequence values.** Closed by `REVOKE SELECT ON … SEQUENCES` (§1.3).
- **Timing.** Out of scope; not worth engineering against for this product.

---

## 6. The connection-reuse trap: proof of failure, proof of fix

Today `CONN_MAX_AGE` is unset (default `0`) and there is no pool, so every request
gets a fresh connection and the trap is not armed. It arms the moment anyone sets
`CONN_MAX_AGE`, enables Django's psycopg pool (`OPTIONS = {"pool": True}`), or puts
PgBouncer in front. All three are things a performance-tuning change does on a
Tuesday afternoon, which is exactly why the fix must be structural rather than a
warning in a docstring.

### 6.1 Bare `SET` leaks — MEASURED, one backend, two transactions

```
=== ONE backend for the whole file. Its pid: 989 ===

=== BARE `SET` — request 1 is RIVAL (org 2) ===
BEGIN;
SET raporo.org_id = '2';
SELECT pg_backend_pid(), current_setting('raporo.org_id',true), count(*), string_agg(DISTINCT org_id::text,',')
  FROM sales_sale;
     pid | ctx | rows_seen | orgs
     989 | 2   |         1 | 2
COMMIT;

--- request 2 arrives on the SAME connection and forgets to set context ---
BEGIN;
     pid | ctx | rows_seen | orgs
     989 | 2   |         1 | 2          <-- ACME's request reading RIVAL's row
--- and it can WRITE into the leaked org: ---
INSERT INTO sales_sale (org_id, store_id, total) VALUES (2,20,4242) RETURNING id, org_id, total;
      id | org_id |  total
      15 |      2 | 4242.00             <-- and write into RIVAL's tenant
```

A cross-tenant read *and* write, caused by the isolation mechanism. This is the
whole point of the section: the naive implementation of RLS is a Critical-severity
vulnerability that did not exist before RLS.

### 6.2 `SET LOCAL` does not leak — MEASURED, same backend

```
=== `SET LOCAL` — request 1 is RIVAL (org 2) ===
BEGIN; SET LOCAL raporo.org_id = '2';
     pid | ctx | rows_seen | orgs
     989 | 2   |         1 | 2
COMMIT;

--- request 2, same connection, no context set: ---
BEGIN;
     pid | ctx | is_null | len | rows_seen
     989 |     | f       |   0 |         0        <-- '' not NULL; nullif makes it 0 rows
INSERT INTO sales_sale (org_id, store_id, total) VALUES (2,20,4242);
     ERROR: new row violates row-level security policy for table "sales_sale"
```

Related semantics, all measured, all of which the implementation depends on:

| Behaviour | Result |
|---|---|
| `SET LOCAL` then `ROLLBACK` | reverts; next statement sees `''` → 0 rows |
| `set_config('raporo.org_id', '2', true)` | identical to `SET LOCAL`; reverts on commit |
| **`SET LOCAL` outside a transaction block** | **`WARNING: SET LOCAL can only be used in transaction blocks` — value does not stick, 0 rows.** A warning, not an error, so psycopg does not raise. Fail-closed but the app is simply broken. |
| `SET LOCAL` inside a savepoint, then `ROLLBACK TO SAVEPOINT` | reverts to the **outer** transaction's value |
| second `SET LOCAL` later in the same transaction | overrides — see §5 row 9 |
| fresh connection, GUC only ever touched by `SET LOCAL` | `NULL` before the first transaction, `''` after it |

### 6.3 The exact call, and where it lives

```python
# common/tenancy.py  (new module — name is the architect's call)

@contextmanager
def org_context(org_id: int):
    """Pin the database session to one organization for one transaction.

    MUST be the OUTERMOST atomic block. `SET LOCAL` is reverted by a
    subtransaction rollback, so context set inside a nested atomic() is lost
    the moment that block rolls back to its savepoint — measured.
    """
    with transaction.atomic():                      # a real BEGIN, never autocommit
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('raporo.org_id', %s, true)",
                [str(int(org_id))],                 # canonicalised; parameterised
            )
        yield
```

Five requirements on this, each traceable to a measurement above:

1. **`set_config(..., is_local => true)`, parameterised — not `SET LOCAL` with an
   interpolated value.** `SET` does not accept bind parameters, so `SET LOCAL` forces
   string building next to a tenancy key. `set_config` is a function call and takes
   `%s`. Given this codebase's standing rule about building SQL from request data,
   the parameterised form is the only acceptable one.
2. **`transaction.atomic()` unconditionally**, because `SET LOCAL` in autocommit is
   a no-op with a warning (measured).
3. **Outermost only.** A savepoint rollback reverts it (measured). Enforce with
   `assert not connection.in_atomic_block` — or, better, make `org_context` the thing
   that *opens* the request transaction so there is nothing to nest inside.
4. **Two entry points, no third.** (a) a middleware placed after
   `AuthenticationMiddleware`, wrapping `get_response`, for request-scoped work;
   (b) `org_context()` used explicitly by management commands, future Celery tasks,
   and data scripts. A tenant table read outside one of the two returns zero rows —
   fail-closed, and loud in dev because §7.3's assertion fires.
5. **`ATOMIC_REQUESTS` is not sufficient on its own** — it wraps only the view
   function, so middleware and template-time queries fall outside. If both are used,
   the middleware's atomic is the outer one and that is where context is set.

The middleware wrapping `get_response` means the whole request is one transaction.
That is a real trade-off (longer transactions, and streaming responses need care)
and it belongs to the `architect` and `performance-engineer`. The security position
is narrow: *whatever transaction shape is chosen, the context set must be
`is_local => true` and must sit in the outermost transaction.*

### 6.4 PgBouncer

**Transaction pooling with a bare `SET` is a cross-client leak. MEASURED**, two
separate client connections through PgBouncer 1.25.2, `pool_mode = transaction`,
`default_pool_size = 1`:

```
--- client 1: bare SET, then disconnect ---
BEGIN; SET raporo.org_id='2'; SELECT count(*) FROM app.sales_sale; COMMIT;
     server_pid 1626   rows_seen 1   orgs 2

--- client 2: BRAND NEW connection through the bouncer, no context set ---
SELECT pg_backend_pid(), current_setting('raporo.org_id',true) …;
     server_pid 1626   inherited_ctx 2   rows_seen 1   orgs 2
```

Same server backend, *different client*, and client 2 read RIVAL's row. This is
strictly worse than §6.1: there the two requests were at least the same application
process, here they can be different pods serving different tenants.

Confirmed with PgBouncer's **default** configuration, not a weakened one — the
config file showed `pool_mode = transaction` and no `server_reset_query` override.
`server_reset_query = DISCARD ALL` is not applied on release in transaction mode
unless `server_reset_query_always = 1`; the leak reproduced identically both with an
explicitly empty reset query and with the default.

**`SET LOCAL` survives transaction pooling. MEASURED**, fresh pool:

```
client 1 (org 2, SET LOCAL): server_pid 1785  rows=1  orgs=2
client 2 (no context):       server_pid 1785  inherited=''  rows=0
client 3 (org 1, SET LOCAL): server_pid 1785  rows=2
client 4 (no context):       server_pid 1785  inherited=''  rows=0
```

Which patterns survive which pool mode:

| Pattern | No pooling / `CONN_MAX_AGE=0` | `CONN_MAX_AGE>0`, Django psycopg pool, PgBouncer **session** | PgBouncer **transaction** | PgBouncer **statement** |
|---|---|---|---|---|
| bare `SET` at session level | works, by luck | **leaks across requests** | **leaks across clients** | broken |
| `SET LOCAL` / `set_config(…, true)` in a transaction | works | works | works | **broken** — no multi-statement transaction exists |
| `connection_created` signal to set context | wrong at any setting: fires once per *connection*, not per request | wrong | wrong | wrong |

Deployment rules that follow: PgBouncer **must** be `transaction` or `session`,
never `statement`; and if session pooling is ever chosen, `server_reset_query =
DISCARD ALL` must be verified present, because session pooling makes bare `SET`
*appear* to work while leaking to the next client that gets the connection.
Additionally, `raporo_app` must not be a PgBouncer `admin_users`/`stats_users`, and
`auth_query` must not run as the owner.

### 6.5 Backups

**MEASURED:** `pg_dump -U raporo_app --data-only -t app.sales_sale` →

```
pg_dump: error: query failed: ERROR: query would be affected by row-level security
                policy for table "sales_sale"
```

`pg_dump` sets `row_security = off` and refuses to produce a dump it cannot
guarantee is complete. Good — it fails loudly. The hazard is the "fix": running
`pg_dump --enable-row-security` (or a hand-rolled `COPY`) yields a **silently
partial** backup containing only whatever tenant the context happened to name — or
nothing at all. Backups run as `raporo_owner` or `raporo_backup`, and a restore test
must assert row counts, not just exit status.

Related, measured: `COPY table TO STDOUT` **is** correctly filtered by RLS (an
export as the app role returns only the caller's tenant, which is what a CSV export
wants), but `COPY … FROM` is **refused outright**: `ERROR: COPY FROM not supported
with row-level security. HINT: Use INSERT statements instead.` No consumer today
(`bulk_create` and `loaddata` both emit `INSERT`), but any future bulk-import path
must not plan on `COPY FROM`.

---

## 7. What RLS does **not** protect — plainly

**The sentence to rely on:**

> With org-level RLS in place, an application bug can no longer show one
> organization another organization's data. It can still show one **store's** data
> to a user who is only entitled to a **sibling store in the same organization**,
> because store-level entitlement is a *set* per user (`StoreAccess`), not a single
> value, and the database is only told the org. Inside an organization, RLS is blind
> and the application-layer guards are the entire defence.

Concretely, with org RLS live, the remaining reachable damage from an application bug:

1. **Cross-store, same-org reads and writes.** A user with access to "ACME Kigali"
   reads or edits "ACME Huye". The org column matches, so RLS is satisfied. This is
   the `for_store()` / `for_stores()` / `GuardedQuery` layer's job and nothing else's.
   Fix rounds 1–3 spent most of their effort here — S1, S1-new, S2, F1–F5 were all
   *within*-org or scope-mixing findings, and RLS would have caught **none** of them.
   Judgement: **keep every one of those guards.** RLS is a second wall, not a
   replacement, and the `security-engineer` position is that removing any
   application guard on the strength of this design is unjustified.
2. **Same-org privilege escalation.** Role and permission logic (`Role.has()`, the
   `PRESETS["Manager"]` self-promotion shape from F13) is entirely inside one org.
   RLS contributes nothing.
3. **Anything reachable via arbitrary SQL execution.** §5 row 9. Injection, a
   compromised process that can call `set_config`, a rogue migration.
4. **`accounts_user` and session data.** No policy (§4.4). User enumeration and
   user-PII exposure remain application concerns.
5. **Existence oracles via unique constraints** (§5.4) and single-column FKs (§5.3,
   until `E007` closes them).
6. **Silent wrong answers.** **MEASURED**, no context at all:
   `SELECT count(*), sum(total), max(id) FROM sales_sale` → `0, NULL, NULL`;
   `SELECT EXISTS (SELECT 1 FROM sales_sale WHERE total = 999)` → `f`. For a
   reporting product this is the availability/correctness cost of fail-closed: a
   missing-context bug produces a *plausible empty report*, not an error. §7.3's
   assertion exists for this and it is not optional.
7. **Anything above the database.** CSRF, XSS, session fixation, rate limiting,
   file uploads, TLS. Unchanged.

RLS's genuine, large win: it converts the *entire class* "a query that forgot its
tenant filter" from a leak into zero rows — across the ORM, raw SQL, `all_objects`,
reverse relations, joins, aggregates, set operators, admin, `dbshell`, and every
future reporting query nobody has written yet. That class has produced two Critical
and five High findings in this slice alone. Buying the class is worth the cost in §8.

---

## 8. Cost and sharp edges

### 8.1 The predicate on every query — MEASURED

228 003 rows, 40 orgs, 120 stores, PostgreSQL 18.6:

| Query | Role / RLS | Index | Plan | Buffers | Time |
|---|---|---|---|---|---|
| `sum(total) WHERE store_id=101` | app, RLS on | none | Seq Scan | 2131 | **13.0 ms** |
| same, hand-written org predicate | owner, RLS off | none | **Parallel** Seq Scan | 2131 | 13.8 ms |
| `store_id, sum(total) GROUP BY store_id` | app, RLS on | none | Seq Scan → HashAggregate | 2131 | **40.8 ms** |
| same | owner, RLS off | none | **Parallel** Gather Merge | 2139 | **12.0 ms** |
| `sum(total) WHERE store_id=101` | app, RLS on | `(org_id, store_id)` | Bitmap Index Scan | **26** | **0.65 ms** |
| `GROUP BY store_id` | app, RLS on | `(org_id, store_id)` | Bitmap Index Scan | **67** | **2.97 ms** |

**The real cost of RLS is not the predicate — it is losing parallel query.**
Isolated and measured: `current_setting` is `proparallel = 's'` (parallel safe), and
the *identical* predicate written by hand as the owner still produces a
`Parallel Seq Scan`. It is the presence of an RLS policy on the relation that makes
PostgreSQL plan the statement non-parallel. On the consolidated org report that is
**40.8 ms vs 12.0 ms — 3.4x** on a table that will be an order of magnitude larger.

**And it is entirely erased by one index.** `(org_id, store_id)` takes the same
query to 2.97 ms — 13.7x faster than the unindexed RLS plan and 4x faster than the
unindexed *parallel* plan — because the policy predicate becomes the **leading
index column** instead of a filter. The lesson for the two-index schema: RLS does
not make indexing more urgent in a vague way, it tells you exactly which index to
build.

> **Index rule for slice 2, and it should be in the `database-engineer`'s standard:**
> every index on a store-scoped table leads with `org_id`. `(org_id, store_id, <date>)`
> not `(store_id, <date>)`. A `store_id`-only index still works — measured, index
> scan on `store_id` with `org_id` as a heap Filter — but it re-checks the tenant
> predicate per row and cannot serve an org-wide report at all.

### 8.2 Where it compounds badly

- **Every joined table adds its own predicate.** MEASURED: a two-table join produced
  a policy filter on *both* scans. A five-table reporting join carries five.
- **`current_setting` is not `leakproof`**, so PostgreSQL will not push a user
  qual below an RLS security-barrier qual in the cases where it applies. Consequence
  in practice: a cheap, selective user predicate can be evaluated *after* the
  tenant predicate instead of before. On the plans measured here it did not bite,
  because with `(org_id, …)` leading, the tenant predicate *is* the selective one.
  Watch it when the reporting joins land.
- **`orgs_organization`'s policy is `id = raporo_current_org()`** — always a single
  pk lookup, free.
- **`audit_auditlog`** already has `(org, at)`, which leads with `org` and is
  therefore already the right shape. One correct index in the schema, by accident.
- **Write path**: `WITH CHECK` is one integer comparison per row. Immaterial next to
  the composite-FK lookups already being paid.

### 8.3 Debugging

- **Plans taken as the owner do not match production plans.** MEASURED, same query,
  same index: the app role got a `Bitmap Index Scan` with 26 buffers, the owner got
  a `Bitmap Index Scan` with **39 index searches** and 120 buffers, because it was
  planning a different predicate. `manage.py dbshell` connects as… whichever role
  the environment provides. Requirement: `dbshell` in dev connects as `raporo_app`,
  and `docs/DEVELOPMENT.md` gains a one-liner for reproducing a production plan
  (`BEGIN; SET LOCAL ROLE raporo_app; SET LOCAL raporo.org_id='…'; EXPLAIN …;
  ROLLBACK;`). Without this, every performance investigation measures the wrong thing.
- **"My query returns nothing"** becomes the most common dev complaint. §7.3's
  assertion is what makes the answer immediate instead of a twenty-minute hunt.
- Every `EXPLAIN` output now carries `NULLIF(current_setting('raporo.org_id'::text,
  true), ''::text)::bigint` in the filter. Noisy, and worth knowing when reading a plan.

### 8.4 Interaction with the existing guards

| Existing mechanism | Interaction | Verdict |
|---|---|---|
| append-only triggers | Fire only for rows the policy admits, so out-of-scope forgery attempts become a silent `UPDATE 0` (MEASURED). The `REVOKE` in §4.3 restores the loud failure for the app; the trigger keeps binding the owner. Trigger body needs **no** edit → `PINNED_SQL` untouched. | complementary, keep all three |
| `TRUNCATE` guard + its `test_*` / `raporo.allow_truncate` exemptions | RLS does not filter `TRUNCATE` at all. The exemptions become far less interesting once the app has no `TRUNCATE` privilege (MEASURED: `permission denied`). The round-2 measurement that `SET raporo.allow_truncate='on'; TRUNCATE` *succeeds* in a production-named DB is now unreachable from the app role. | **partly closes a residual I had accepted** |
| four `*_same_org_fk` composite FKs, `DEFERRABLE INITIALLY IMMEDIATE` | Orthogonal. MEASURED: an `(org_id=1, store_id=20)` insert passes `WITH CHECK` and is caught only by the FK, including under `SET CONSTRAINTS ALL DEFERRED` (at `COMMIT`). And §5.3: the composite shape is what closes the FK existence oracle. | keep, and **extend to every store-scoped→store-scoped FK** |
| `SET CONSTRAINTS` | Unaffected by RLS. The `loaddata` landmine (`check_constraints()` leaving `ALL DEFERRED` for the rest of the transaction) is unchanged; `tests/conftest.py::load_fixture` remains the remedy. | no change |
| `loaddata` / fixtures | Runs as the test connection's role. As the owner (§8.5) RLS does not apply and today's fixtures work untouched. If ever run as `raporo_app`, every insert needs matching context and the current fixtures — which create rows in **two orgs in one transaction** — become impossible. | run as owner |
| `GuardedQuery` / `ScopedQuerySet` / E001–E006 | Unchanged and still necessary (§7, item 1). | keep every one |
| `common.E100` | Unchanged. E101/E102 (§1.6) are its siblings and must not repeat its tag mistake. | — |

### 8.5 The question most likely to break the 369-test suite

**How the suite runs under a non-owner runtime role: it doesn't, and it must not.**

**The suite connects as `raporo_owner`, and because RLS is enabled but not `FORCE`d,
policies do not apply to it. Every one of the existing tests is unaffected — zero
changes, zero fixture rewrites, no risk of tests silently returning zero rows.**

The reasoning, and the measurements behind each step:

1. `pytest-django` creates and drops `test_raporo`, which needs `CREATEDB` — a
   privilege `raporo_app` must never have. It then runs `migrate`, which needs
   `CREATE`/ownership. The suite is therefore a *migrator* workload by construction.
2. **MEASURED:** with RLS enabled and not forced, the owner sees all 3 rows across
   both orgs — `current_user = raporo_owner, rows_owner_sees = 3, orgs = 2`.
   `tests/conftest.py` creates `org` + `other_org`, `store` + `foreign_store`,
   `product` + `foreign_product` in one transaction; as the owner that keeps working
   exactly as today.
3. **This is the decisive reason not to use `FORCE ROW LEVEL SECURITY`. MEASURED**,
   owner, `FORCE` on, no context:
   ```
   UPDATE sales_sale SET total = total;          -> UPDATE 0      (silent)
   DELETE FROM sales_sale WHERE total < 0;       -> DELETE 0      (silent)
   INSERT INTO sales_sale (…) VALUES (1,10,7);   -> ERROR: new row violates row-level security policy
   ```
   `FORCE` makes **data-migration backfills silently no-op**. A backfill that
   reports success and changed nothing is a worse failure than any leak `FORCE`
   would prevent, and the thing it protects against — the app connecting as the
   owner — is covered loudly by `common.E101`. **Recommendation: enable RLS, do not
   `FORCE` it, and assert the runtime identity at boot.** If `FORCE` is ever
   adopted, every data migration must run inside `org_context()` per org, and that
   is a large ongoing tax for a small marginal gain.
4. **RLS-specific tests assume the app role inside the test's own transaction.**
   MEASURED, and this is the mechanism:
   ```
   BEGIN;
     SET LOCAL ROLE raporo_app;
       -> current_user raporo_app | session_user raporo_owner | seen_no_ctx 0
     SET LOCAL raporo.org_id = '1';
       -> current_user raporo_app | seen_with_ctx 2
   COMMIT / ROLLBACK   -> back to raporo_owner automatically
   ```
   Prerequisites, both measured: `GRANT raporo_app TO raporo_owner` is **required**
   or `SET ROLE` fails with `permission denied to set role "raporo_app"`; and
   `SET LOCAL ROLE` reverts on both `COMMIT` and `ROLLBACK`, so a pytest `TestCase`
   (which wraps each test in an atomic block and rolls it back) cleans up by itself
   with no fixture teardown to forget. Grant the app role to the owner in **dev/CI
   bootstrap only**, never in production, and note that `RESET ROLE` inside the
   transaction climbs back out (MEASURED) — so this is a fidelity mechanism, not a
   security boundary. That is fine for tests and disqualifying for production
   (§1.4).
5. The grants and policies land in migrations, so the `test_raporo` database gets
   them too and the conformance tests in §9 have something to assert against.

Two consequences worth writing down:

- **Every RLS test must assume the app role, or it proves nothing.** A test that
  asserts "org 2's rows are invisible" while connected as the owner passes
  vacuously — the exact tautology class the `code-reviewer` has now caught three
  times in this slice. The `as_tenant(org)` fixture in §9 must be the only way
  those tests are written, and one test must prove that the fixture actually
  changes `current_user`.
- **RLS is therefore not exercised on the happy path of the existing 369 tests.**
  That is the honest cost of this choice, and it is why §9's conformance tests
  matter more than usual: they are the only thing standing between "RLS is enabled
  in production" and "someone believes RLS is enabled in production".

---

## 9. Tests that must exist

Behaviour tests, all via the `as_tenant` fixture (`transaction.atomic()` +
`SET LOCAL ROLE raporo_app` + `set_config`), each **materialising** results
(`sorted(r.name for r in qs)`) because a build-time refusal and a fetch-time leak
are indistinguishable until something iterates:

1. Cross-org read returns zero rows for `SELECT`, `count()`, `exists()`, `aggregate()`,
   a join, and a set operator — the same matrix the round-3 harness used.
2. Cross-org `INSERT` raises; tenant-hopping `UPDATE` (`org_id` reassignment) raises;
   cross-org `UPDATE`/`DELETE` affect 0 rows; a `WHERE`-less `UPDATE`/`DELETE`
   affects only the caller's tenant.
3. **Missing context**: unset, `''`, post-`RESET`, and post-`SET LOCAL`-transaction
   all yield 0 rows and a refused insert. Four cases, not one — rows 1–4 of §5.
4. Non-numeric and overflow GUC values raise, and the raised exception is the type
   the service layer expects.
5. **The connection-reuse regression.** Two transactions on one connection asserting
   the second sees nothing; and a test that fails if `org_context` is ever changed
   from `is_local => true` to a session `SET`. This is the test that stops the
   Critical in §6.1 from ever being introduced.
6. `org_context` outside a transaction raises rather than warning (the §6.2 no-op).
7. `org_context` nested inside another atomic block raises.
8. `INSERT … RETURNING` works for every insert the `WITH CHECK` policies permit —
   specifically the platform (`org IS NULL`) audit row. This is the §4.3(a) trap and
   it will otherwise be discovered by a failing signup in production.
9. Audit forgery as the app role: `UPDATE`/`DELETE` → `permission denied`; as the
   owner → the append-only trigger's `restrict_violation`; row byte-identical after
   (reuse the existing `snapshot()` helper).
10. `TRUNCATE` as the app role → `permission denied`, on every guarded table.
11. Single-column FK to a store-scoped table is refused by `common.E007`; composite
    FK insert against an invisible-but-existing row fails identically to one against
    a nonexistent row (the §5.3 oracle test).
12. `SELECT last_value` on a sequence → `permission denied`, while `INSERT … RETURNING id`
    still works.

Schema-conformance tests (read `pg_class` / `pg_policy` / `information_schema`, the
same style as the existing `pg_constraint` truth test). **These are the mechanism —
they are what stops slice 2 shipping a table with no policy, which §1.3 measured as
silent:**

13. Every table with an org-bearing column has `relrowsecurity = t` and ≥1 policy.
    Parametrised over the live model registry, so a new store-scoped model fails the
    suite until its policy migration exists. Must be mutation-tested by adding a
    throwaway table and confirming it goes red.
14. Every such table has a `RESTRICTIVE` floor policy.
15. `audit_auditlog`'s `SELECT` `USING` and `INSERT` `WITH CHECK` expressions are
    **identical** (the §4.3(a) drift guard).
16. `raporo_app` holds exactly `{SELECT, INSERT, UPDATE, DELETE}` on tenant tables,
    `{SELECT, INSERT}` on `audit_auditlog`, `TRUNCATE` on nothing, and no privilege
    on `django_migrations`.
17. `raporo_app` is not `rolsuper`, not `rolbypassrls`, not `rolcreatedb`, owns no
    table, and is not a member of `raporo_owner`.
18. `raporo_current_org()` is `STABLE`, `PARALLEL SAFE`, has **no** `proconfig`
    (no `SET` clause → still inlinable), and the plan for a tenant query contains
    the inlined `NULLIF(current_setting(...))` text rather than a function call.
    Cheap, and it pins the 11.5x measured in §4.1.
19. Every index on a store-scoped table leads with `org_id` (§8.1).
20. `common.E101` / `E102` fire through `django.core.checks.run_checks()` — never by
    calling the function directly — with a negative control that passes only for the
    right reason, and a mutation that proves the old wrong tag would fail 4 of them.

`§7.3 assert_org_context` (referenced above): the service-layer entry point compares
`SELECT raporo_current_org()` against the org it was asked to operate on and raises
if they differ or if it is NULL. Tested in both directions. This is what turns
"empty report" into "loud failure", and it is the answer to §5's "no rows vs error"
question — fail-closed at the database, fail-loud at the boundary.

---

## 10. Findings, with severities

These are findings against the **proposal as briefed**, to be closed by the
implementation. Nothing here is a finding against code on the branch today.

| # | Sev | Finding | Exploit in one sentence | Remediation |
|---|---|---|---|---|
| R1 | **Critical** | Setting the org GUC with a session-level `SET` while connections are reused | Request B on a reused connection (or a different client through PgBouncer transaction pooling) inherits request A's org and both reads and writes another tenant's data — §6.1, §6.4, measured | `set_config(…, is_local => true)` in the outermost transaction; regression test 5 |
| R2 | **High** | A store-scoped→store-scoped FK on a single column | RI triggers bypass RLS, so a child row is accepted pointing at an invisible parent in another org, and the error text distinguishes "invisible" from "nonexistent" — §5.3, measured | composite `(target_id, org_id)` FK on every such relation + `common.E007` |
| R3 | **High** | `USING`-only policies | A row I own is `UPDATE`d into another tenant (`SET org_id = <theirs>`) — §4.2, measured | `WITH CHECK` on `INSERT` and `UPDATE`, per command |
| R4 | **High** | New tenant table ships without `ENABLE ROW LEVEL SECURITY` | Default privileges grant DML automatically while RLS defaults to off, so the table is world-readable across tenants and nothing complains — §1.3, measured | conformance tests 13/14 + `common.E102`, both mutation-tested |
| R5 | Medium | Naive `current_setting(...)::bigint` without `nullif` | Missing context raises `22P02` instead of returning no rows on every request after the connection's first, so the fail-closed behaviour is inconsistent and the error is unattributable — §5, measured | `nullif(…, '')` in the accessor + `assert_org_context` at the service boundary |
| R6 | Medium | `WITH CHECK` broader than `SELECT USING` on `audit_auditlog` | `INSERT … RETURNING` — which the Django ORM always emits — fails for platform audit rows, breaking signup in production only | keep the two expressions identical; test 15 |
| R7 | Medium | A future permissive policy widens the boundary | One `USING (true)` reporting policy ORs with the tenant policy and exposes every row — §4.2, measured | one `RESTRICTIVE` floor per tenant table; test 14 |
| R8 | Medium | Backups run with the app credential | `pg_dump --enable-row-security` produces a silently partial backup that restores as an empty or single-tenant database | owner/`raporo_backup` only; restore test asserts row counts |
| R9 | Low | `SELECT` on sequences | `last_value` discloses global row volume and growth across all tenants | `GRANT USAGE`, not `SELECT`; test 12 |
| R10 | Low | Store-without-org audit rows | An audit row with `store_id` set and `org_id` NULL skips the `MATCH SIMPLE` FK and is invisible to every tenant reader | `CHECK (store_id IS NULL OR org_id IS NOT NULL)` |
| R11 | Low | `FORCE ROW LEVEL SECURITY` if adopted | A data-migration backfill silently updates zero rows — §8.5, measured | do not `FORCE`; assert runtime identity via `common.E101` |
| R12 | Info | UUIDv7 `public_id` treated as a capability | Time-ordered and partially predictable; also an insert-time existence oracle if a client can supply one | opaque name only; authorization always checked; clients never supply it |

**Merge-gate position.** This is a design review, so there is nothing to block. But
for the record, so the `tech-lead` gate has it in writing: **R1 and R2 are
merge-blocking on the implementation.** R1 is a Critical cross-tenant leak *created
by* the isolation mechanism, and it is invisible today only because `CONN_MAX_AGE`
is unset — the day someone tunes that, an RLS implementation with a bare `SET`
becomes the worst vulnerability this codebase has ever had. R2 is a High that RLS
does not cover and that slice 2's schema will produce by default.

---

## 10.5 Reconciliation with ADR 0009 (written in parallel)

`docs/adr/0009-row-level-security-for-organization-isolation.md` landed while this
review was running. Independent convergence on the important things: `NULLIF(...)`
in the accessor, `SET LOCAL` inside a transaction from exactly one door, `USING` and
`WITH CHECK` on all commands, store isolation staying in the application, and the
role split as a precondition ("until that split exists, this ADR is not implemented
— it is decoration"). Agreed on all of it, and the source-scan test that refuses a
bare `SET` on a `raporo.*` GUC anywhere else in the tree is a better mechanism than
anything I had planned for R1 — take it.

Two decisions I measured and disagree with. Both are cheap to change now.

**(a) `FORCE ROW LEVEL SECURITY` on every table, combined with `BYPASSRLS` on the
migrator, is self-cancelling — and the least-privilege-looking cleanup breaks it
silently.** `BYPASSRLS` bypasses `FORCE`, so as specified the owner is unaffected by
its own policies and `FORCE` buys nothing today. The two decisions are load-bearing
on each other. The day someone removes `BYPASSRLS` from the migrator — which is
exactly what a least-privilege review would recommend, and I would have recommended
it — `FORCE` starts applying to the owner, and then, MEASURED:

```
-- owner, FORCE on, no org context (a data-migration backfill)
UPDATE sales_sale SET total = total;        -> UPDATE 0     (silent)
DELETE FROM sales_sale WHERE total < 0;     -> DELETE 0     (silent)
INSERT INTO sales_sale (…) VALUES (1,10,7); -> ERROR: new row violates row-level security policy
```

and, because `pytest` runs as the migrator, **every fixture-based test in the suite
starts returning zero rows** — `tests/conftest.py` creates `org` and `other_org` in
one transaction, which becomes impossible. A backfill that reports success and
changed nothing is a worse outcome than the leak `FORCE` was meant to prevent.

Recommendation: **`ENABLE`, do not `FORCE`; drop `BYPASSRLS` from the migrator.**
The owner then bypasses because it is the owner (MEASURED: 3 rows across 2 orgs),
which is the property the test suite and every data migration already depend on,
with one fewer role attribute in the system. `FORCE`'s only real target — the app
connecting as the owner — is covered loudly and earlier by `common.E101` (§1.6).
If ADR 0009 keeps `FORCE` + `BYPASSRLS`, then the coupling must be pinned by a test
asserting the migrator has `rolbypassrls = t` **with the reason in the failure
message**, because otherwise the next reviewer removes it on sight.

**(b) The ADR does not mention `RETURNING`.** §4.3(a): `INSERT … RETURNING` is
checked against the `SELECT` policy, and the Django ORM always emits it. Any table
whose `WITH CHECK` is broader than its `USING` — which is precisely the audit log's
org-NULL case the ADR's "all commands" wording invites — fails through the ORM while
succeeding in raw SQL. This needs to be in the ADR's consequences, not discovered by
a failing signup.

Not a disagreement, just an addition: the ADR's rejection of store-level RLS is
right and for the right reason ("it relocates an authorization decision into a
mechanism that can only answer zero rows instead of `PermissionDenied`"). §7 says
the same thing from the attacker's side.

---

## 11. Rollout order

1. Bootstrap SQL for the two roles; compose and settings gain the second alias;
   entrypoint switches `migrate` to it and fails loudly without it. **Closes the
   routed devops finding on its own, before a single policy exists.**
2. `common.E101` (+ test through `run_checks()`), so the "app is not the owner"
   premise is executed rather than believed.
3. `org_context()` + the middleware + `assert_org_context`, with the §6 regression
   tests, **before** any policy is enabled — so the context plumbing is proven
   correct while a mistake is still harmless.
4. `raporo_current_org()`, then policies + `RESTRICTIVE` floors on `orgs_*` and
   `audit_auditlog`, plus the audit `REVOKE` and the `CHECK`.
5. Conformance tests 13–19 and `common.E102`.
6. `common.E007` and the composite-FK requirement, ahead of slice 2's schema.
7. Indexes leading with `org_id`, with the §8.1 numbers re-measured on real data.
8. Only then: the denormalised `org` column on `StoreScopedModel` and its policies,
   as slice 2's tables land.

Steps 1–3 deliver most of the security value and carry almost none of the risk.
Step 8 is the one that can quietly return zero rows, so it goes last, behind the
conformance tests that would catch it.

---

## Appendix: probe environment

```
PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2), isolated compose project `raporo-rls-sec`
PgBouncer 1.25.2, pool_mode = transaction, default_pool_size = 1
Roles: probe_owner (superuser, bootstrap) / raporo_migrator (owner) / raporo_app (runtime)
Schema: orgs_organization, orgs_store, sales_sale, sales_saleline, audit_auditlog
        — slice-2-shaped, with the four composite-FK and append-only patterns copied
        verbatim from apps/orgs/migrations/0001_initial.py and common/db.py
Scale test: 228 003 sales rows, 40 orgs, 120 stores
Project torn down with `docker compose -p raporo-rls-sec down -v`. The default
project on :8000 was not touched. No file was mounted into /app. No repo file
was modified other than this document.
```
