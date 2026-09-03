-- Raporo database role split: raporo_owner / raporo_app / raporo_backup.
--
-- PHASE 1 OF TWO. Run this as a **superuser**, connected to the application
-- database, via `scripts/db/bootstrap-roles.sh` (which supplies the psql
-- variables below). Do not run it by hand: the passwords are variables.
--
-- Phase 1 is everything that can be done *before a single table exists*:
-- roles, their attributes, who may connect, who owns the schema, and the
-- default privileges that every future table inherits. It runs once per
-- database (at `initdb` on a fresh dev volume, or as an operator step).
--
-- Phase 2 is `scripts/db/runtime-privileges.sql`, run **as raporo_owner after
-- every `migrate`** by `manage.py grant_runtime_privileges`. Anything that
-- names a table has to live there: this file also runs at `initdb` time, when
-- `audit_auditlog` does not exist yet. MEASURED, and it is why the split is
-- two files: with the table-level REVOKEs in this file, a wiped-volume `up`
-- left raporo_app holding UPDATE and DELETE on `audit_auditlog`, and
-- `DELETE FROM django_migrations` as the app role removed all 24 rows of
-- migration history.
--
-- WHY THIS FILE EXISTS AT ALL, AND WHY IT IS NOT A MIGRATION
-- ---------------------------------------------------------------------------
-- Roles are *cluster*-level objects, and the roles here need attributes only a
-- superuser can set (`BYPASSRLS` on raporo_backup, `CREATEDB` on the owner in
-- dev/CI). `raporo_owner` is deliberately `NOCREATEROLE`, so a Django
-- migration - which runs as raporo_owner - cannot create these roles even in
-- principle. The split therefore has to be bootstrapped by the one identity
-- that exists before it: the cluster's bootstrap superuser.
--
-- IDEMPOTENT AND CONVERGENT
-- ---------------------------------------------------------------------------
-- Safe to re-run, and re-running *repairs drift*: every role attribute and
-- every grant below is re-asserted rather than merely created, so a
-- hand-made change (a granted membership, a flipped BYPASSRLS) is removed on
-- the next run instead of surviving forever. Re-run it whenever a password
-- rotates or a role attribute is in doubt.
--
-- WHAT EACH ROLE IS FOR (docs/adr/0009, threat model §1)
-- ---------------------------------------------------------------------------
--   raporo_owner   owns the schema, every table, sequence, function and
--                  trigger. Runs `migrate`. Never serves a request. Not a
--                  superuser and NOT `BYPASSRLS` - it bypasses row-level
--                  security by being the table owner (with RLS `ENABLE`d and
--                  not `FORCE`d), which is the property `migrate`, data
--                  backfills and the test suite already depend on.
--   raporo_app     the runtime identity. Owns nothing. SELECT/INSERT/UPDATE/
--                  DELETE and nothing else. No TRUNCATE, no BYPASSRLS, no
--                  CREATE on the schema, no TEMPORARY on the database, and not
--                  a member of raporo_owner.
--   raporo_backup  BYPASSRLS, read-only. Exists because `pg_dump` as
--                  raporo_app produces a silently partial dump once policies
--                  land, and because without this role someone "fixes" backups
--                  by pointing them at the owner.
--
-- Expected psql variables (all required; see bootstrap-roles.sh):
--   owner_pw, app_pw, backup_pw   role passwords, from the secret store
--   dev_extras                    on|off - `on` grants CREATEDB to the owner
--
-- OPERATIONAL NOTE: these statements contain passwords. The postgres image
-- runs with `log_statement = none`, so they are not logged; if an operator
-- turns `log_statement` up to `ddl` or `all`, this script must be run with it
-- back off, or the passwords land in the server log.

\set ON_ERROR_STOP on

BEGIN;

-- --------------------------------------------------------------------------
-- 1. The roles themselves
-- --------------------------------------------------------------------------
-- `CREATE ROLE` has no `IF NOT EXISTS`, hence the DO blocks. Attributes are
-- set by the `ALTER ROLE`s below rather than at CREATE time, so that a role
-- that already exists converges to the same state as one created here.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'raporo_owner') THEN
        CREATE ROLE raporo_owner;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'raporo_app') THEN
        CREATE ROLE raporo_app;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'raporo_backup') THEN
        CREATE ROLE raporo_backup;
    END IF;
END
$$;

-- Every attribute is spelled out, including the NO* ones. They are the
-- security properties of this design, so they are asserted on every run and
-- not inferred from a default that a future PostgreSQL release may change.
ALTER ROLE raporo_owner WITH LOGIN NOSUPERUSER NOCREATEROLE NOREPLICATION
    NOBYPASSRLS PASSWORD :'owner_pw';
ALTER ROLE raporo_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS PASSWORD :'app_pw';
-- BYPASSRLS is the whole point of raporo_backup, and it is why the role is
-- read-only: it can see every tenant's rows and change none of them.
ALTER ROLE raporo_backup WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION BYPASSRLS PASSWORD :'backup_pw';

-- Membership in raporo_owner is the escalation path this whole design exists
-- to remove, so it is checked and removed on every run: a hand-granted
-- membership must not survive a bootstrap.
--
-- Guarded by EXISTS rather than revoked unconditionally, because an
-- unconditional REVOKE of a membership that is not there emits a WARNING on
-- every single run ("role ... has not been granted membership in role ..."),
-- twice - once to the client and once to the server log. A bootstrap that
-- always prints warnings teaches operators to ignore warnings. Silence here
-- means "nothing to repair"; the NOTICE below means "an escalation path
-- existed and has been removed", which is worth waking up for.
DO $$
DECLARE
    -- Not named `member`: that is also a column of pg_auth_members, and the
    -- reference in the WHERE clause below resolves ambiguously (measured:
    -- `ERROR: column reference "member" is ambiguous`, which aborted the whole
    -- bootstrap - loudly, at least).
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY['raporo_app', 'raporo_backup']
    LOOP
        IF EXISTS (
            SELECT 1
            FROM pg_auth_members m
            JOIN pg_roles grp ON grp.oid = m.roleid
            JOIN pg_roles mem ON mem.oid = m.member
            WHERE grp.rolname = 'raporo_owner'
              AND mem.rolname = role_name
        ) THEN
            EXECUTE format('REVOKE raporo_owner FROM %I', role_name);
            RAISE NOTICE
                'removed membership of % in raporo_owner - that is a privilege escalation path and should not have existed',
                role_name;
        END IF;
    END LOOP;
END
$$;

-- --------------------------------------------------------------------------
-- 2. The database: who may connect, and who may make temp objects
-- --------------------------------------------------------------------------
-- Withholding TEMPORARY from raporo_app is not tidiness. With no CREATE on
-- `public` (below) and no TEMPORARY here, raporo_app has nowhere to define a
-- SECURITY DEFINER function, which is the most direct way for a runtime role
-- to launder reads past a row-level policy.
DO $$
DECLARE
    db text := quote_ident(current_database());
BEGIN
    EXECUTE 'REVOKE ALL ON DATABASE ' || db || ' FROM PUBLIC';
    EXECUTE 'GRANT CONNECT, TEMPORARY ON DATABASE ' || db || ' TO raporo_owner';
    EXECUTE 'GRANT CONNECT ON DATABASE ' || db || ' TO raporo_app';
    EXECUTE 'GRANT CONNECT ON DATABASE ' || db || ' TO raporo_backup';
END
$$;

-- --------------------------------------------------------------------------
-- 3. The schema: owned by raporo_owner, USAGE-only for everyone else
-- --------------------------------------------------------------------------
ALTER SCHEMA public OWNER TO raporo_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
-- USAGE, never CREATE. See the SECURITY DEFINER note in section 2.
GRANT USAGE ON SCHEMA public TO raporo_app;
GRANT USAGE ON SCHEMA public TO raporo_backup;

-- --------------------------------------------------------------------------
-- 4. Hand every existing object to raporo_owner
-- --------------------------------------------------------------------------
-- Needed on any database that was migrated before the split existed, where
-- the tables are owned by the bootstrap superuser. Without this the app role
-- would face policies and triggers owned by a superuser - and, worse, the
-- owner could not later ALTER them.
--
-- `REASSIGN OWNED BY <superuser> TO raporo_owner` would be shorter and is
-- deliberately not used: it also reassigns shared objects (databases,
-- tablespaces) cluster-wide, which is far more than this file should do.
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT c.relname, c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
          AND c.relowner <> 'raporo_owner'::regrole
          -- An identity/serial sequence belongs to its table and follows the
          -- table's owner automatically; altering it directly is refused.
          AND NOT (
              c.relkind = 'S'
              AND EXISTS (
                  SELECT 1 FROM pg_depend d
                  WHERE d.classid = 'pg_class'::regclass
                    AND d.objid = c.oid
                    AND d.deptype IN ('i', 'a')
              )
          )
          -- Objects an extension owns are the extension's business.
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_class'::regclass
                AND d.objid = c.oid
                AND d.deptype = 'e'
          )
        ORDER BY c.relkind DESC  -- tables ('r') before sequences ('S')
    LOOP
        EXECUTE format(
            CASE obj.relkind
                WHEN 'S' THEN 'ALTER SEQUENCE public.%I OWNER TO raporo_owner'
                WHEN 'v' THEN 'ALTER VIEW public.%I OWNER TO raporo_owner'
                WHEN 'm' THEN 'ALTER MATERIALIZED VIEW public.%I OWNER TO raporo_owner'
                WHEN 'f' THEN 'ALTER FOREIGN TABLE public.%I OWNER TO raporo_owner'
                ELSE 'ALTER TABLE public.%I OWNER TO raporo_owner'
            END,
            obj.relname
        );
    END LOOP;
END
$$;

-- Functions too: the append-only audit trigger's function must be owned by
-- raporo_owner, or the owner cannot replace it in a later migration.
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT p.oid::regprocedure AS sig
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.proowner <> 'raporo_owner'::regrole
          AND NOT EXISTS (
              SELECT 1 FROM pg_depend d
              WHERE d.classid = 'pg_proc'::regclass
                AND d.objid = p.oid
                AND d.deptype = 'e'
          )
    LOOP
        EXECUTE format('ALTER ROUTINE %s OWNER TO raporo_owner', obj.sig);
    END LOOP;
END
$$;

-- --------------------------------------------------------------------------
-- 5. Default privileges, so a table added tomorrow is not a surprise
-- --------------------------------------------------------------------------
-- Without this, the first request after a migration that adds a table fails
-- with `permission denied`, and the fix under time pressure is a hand-run
-- `GRANT ALL` (or worse, serving as the owner). With it, `migrate` produces a
-- correctly-granted table on its own.
--
-- KNOWN GAP, and it is the reason `common.E102` is required: default
-- privileges carry *grants* forward but NOT row-level security. A table
-- created after this point arrives with DML granted and `relrowsecurity = f`.
-- That is silent, and once policies exist it is a cross-tenant leak. Boot-time
-- conformance is the only thing that makes it loud - it is not this file's job
-- and this file cannot do it.
--
-- `FOR ROLE raporo_owner` and not the superuser: `migrate` runs as the owner,
-- so the owner is the only role that creates application objects. A table
-- created by any other role arrives ungranted, which fails loudly and points
-- at the real mistake.
ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    REVOKE ALL ON TABLES FROM raporo_app;
ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM raporo_app;
ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    REVOKE ALL ON TABLES FROM raporo_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM raporo_backup;

ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO raporo_app;
ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO raporo_app;
ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO raporo_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE raporo_owner IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO raporo_backup;

-- --------------------------------------------------------------------------
-- 6. Development and CI only
-- --------------------------------------------------------------------------
-- `pytest-django` creates and drops `test_raporo` and runs `migrate` inside
-- it, so the test suite is a *migrator* workload by construction: it needs
-- CREATEDB and it needs to own what it creates. raporo_app must never have
-- CREATEDB, so the suite connects as raporo_owner - see
-- `config/settings/test.py`, which is where that switch is made explicit.
--
-- Production gets `NOCREATEDB`: nothing there creates a database, and the
-- migration credential is the one that travels through CI.
\if :dev_extras
ALTER ROLE raporo_owner WITH CREATEDB;
\else
ALTER ROLE raporo_owner WITH NOCREATEDB;
\endif

COMMIT;

-- What the operator should see. Kept in the file rather than in a runbook
-- because the whole value of this script is that its result is checkable.
\echo ''
\echo 'raporo role split — resulting state:'
SELECT rolname,
       rolsuper AS superuser,
       rolcreatedb AS createdb,
       rolcreaterole AS createrole,
       rolbypassrls AS bypassrls,
       rolcanlogin AS login
FROM pg_roles
WHERE rolname LIKE 'raporo%'
ORDER BY rolname;
