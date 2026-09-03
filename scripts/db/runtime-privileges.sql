-- Raporo role split, PHASE 2: what raporo_app and raporo_backup may do to the
-- tables that exist right now.
--
-- Runs as **raporo_owner**, after every `migrate`:
--
--     python manage.py migrate --database=migrator
--     python manage.py grant_runtime_privileges --database=migrator
--
-- `docker/entrypoint.sh` does both, in that order, on a dev boot. A deploy's
-- migration job must do both too - they are one step in two commands, not two
-- steps. An operator with psql can also apply this file by hand, as
-- raporo_owner:
--
--     psql --single-transaction -f scripts/db/runtime-privileges.sql
--
-- WHY THIS IS A SEPARATE FILE FROM roles.sql
-- ---------------------------------------------------------------------------
-- Phase 1 (`roles.sql`) runs as the superuser at `initdb`, before any table
-- exists, so it cannot name one. Everything here names tables, so it has to
-- run after `migrate` - and it needs no superuser, because raporo_owner owns
-- every table and can therefore grant and revoke on all of them.
--
-- MEASURED, and the reason this file exists at all: with these statements in
-- phase 1, a wiped-volume `docker compose up` produced a database where
-- `raporo_app` still held UPDATE and DELETE on `audit_auditlog`, and
-- `DELETE FROM django_migrations` as the app role deleted all 24 rows of
-- migration history. The statements had run at `initdb`, when neither table
-- existed, and were silently no-ops.
--
-- IDEMPOTENT AND CONVERGENT
-- ---------------------------------------------------------------------------
-- Every grant is preceded by a `REVOKE ALL`, so this file is the complete
-- statement of what the two non-owning roles hold - not an addition to
-- whatever they held before. A privilege granted by hand under time pressure
-- (the classic being `GRANT ALL` to get a deploy moving) is removed by the
-- next run. Re-running it costs a few milliseconds and no locks worth caring
-- about beyond ACCESS EXCLUSIVE on each table for the duration of the GRANT.
--
-- PLAIN SQL ONLY
-- ---------------------------------------------------------------------------
-- No psql backslash commands and no explicit BEGIN/COMMIT, so that the same
-- bytes run through psycopg (the management command, which wraps the whole
-- file in one transaction) and through psql. An operator using psql directly
-- should add `--single-transaction`, or use the management command, which is
-- the supported path and needs no psql at all.
--
-- Takes no parameters. Nothing here is a secret.

-- --------------------------------------------------------------------------
-- 1. The runtime role: four DML privileges on every table, and nothing more
-- --------------------------------------------------------------------------
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM raporo_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM raporo_app;

-- Absent on purpose:
--   TRUNCATE   - row-level security does not filter TRUNCATE at all. It is a
--                separate privilege, and withholding it is the only thing that
--                stops a table wipe. This is what closes "a compromised app
--                process could wipe the audit trail in two statements".
--   REFERENCES - a foreign key to a protected table is an existence oracle by
--                construction, and nothing at runtime creates constraints.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO raporo_app;

-- USAGE, not SELECT. `SELECT last_value FROM accounts_user_id_seq` is a
-- cross-tenant volume oracle - total row count and growth rate, for every
-- organization, with no policy in the way. USAGE is all an INSERT needs,
-- including the `INSERT ... RETURNING id` the Django ORM always emits.
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO raporo_app;

-- --------------------------------------------------------------------------
-- 2. The backup role: read everything, change nothing
-- --------------------------------------------------------------------------
-- `pg_dump` needs SELECT on every table *and* every sequence, because it dumps
-- sequence state. This role has BYPASSRLS (phase 1) so that a dump taken once
-- policies exist is complete rather than silently one tenant's worth of rows.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM raporo_backup;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM raporo_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO raporo_backup;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO raporo_backup;

-- --------------------------------------------------------------------------
-- 3. Two tables the runtime role does not get all four privileges on
-- --------------------------------------------------------------------------
-- These run *after* section 1's `GRANT ... ON ALL TABLES`, which would
-- otherwise hand the privileges straight back. The order is load-bearing.
--
-- Guarded by EXISTS so this file also applies cleanly to a database whose
-- migrations have not all run yet (a partial deploy, a restored backup mid
-- upgrade). A missing table is not an error; the next run covers it.
DO $$
BEGIN
    -- The audit trail is append-only for the runtime role at the *privilege*
    -- level, not only via the trigger. The trigger (common/db.py) already
    -- refuses UPDATE and DELETE loudly, and this is the layer underneath it:
    -- the attempt now fails with 42501 before it reaches any of our code, and
    -- it keeps failing if a future migration drops the trigger by accident.
    -- INSERT and SELECT are everything the application needs on this table.
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'audit_auditlog'
    ) THEN
        REVOKE UPDATE, DELETE ON public.audit_auditlog FROM raporo_app;
    END IF;

    -- Django's own migration bookkeeping. SELECT is required and the rest is
    -- refused.
    --
    -- MEASURED both ways. With `REVOKE ALL` - which is what the threat model's
    -- §1.3 specifies - the dev server dies at boot: `manage.py runserver`
    -- calls `check_migrations()` unconditionally in `inner_run`, over the
    -- `default` connection, and gets `permission denied for table
    -- django_migrations`. And with the full four privileges (what the app had
    -- before this file existed) `DELETE FROM django_migrations` as raporo_app
    -- removed all 24 rows.
    --
    -- So: reading migration history discloses nothing a schema dump would not,
    -- and forging it is the interesting attack - a row added or removed here
    -- makes the next deploy skip a migration or re-run one against a schema
    -- that already has it. Read yes, write no.
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'django_migrations'
    ) THEN
        REVOKE ALL ON public.django_migrations FROM raporo_app;
        GRANT SELECT ON public.django_migrations TO raporo_app;
    END IF;
END
$$;
