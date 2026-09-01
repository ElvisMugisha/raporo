"""Reusable PostgreSQL guards used from migrations.

Slice 2's ledger tables (StockMovement, Payment, CapitalEntry, Payout) are
append-only for the same reasons the audit log is, so this is written once,
here, and called from each migration that needs it.

Stability contract - now a mechanism, not a comment
---------------------------------------------------
Django tracks a migration by *name*, never by content. A migration that imports
SQL from this module therefore applies whatever text is current at the moment a
fresh database runs it, while a database that migrated last year keeps the
function body it installed back then. Editing SQL that a shipped migration
already applied silently forks the two.

Two things stop that here:

1. **Everything a migration may import is versioned** (`..._V1`,
   `append_only_triggers_v1`). There is no unversioned alias to grab, so an
   in-place edit is visibly the wrong move: the constant's name says which
   already-shipped migrations depend on its exact text.
2. **`tests/test_db_stability.py` pins a SHA-256 of every versioned string**
   (and of the SQL `audit/0002` actually carries). Any edit - even a whitespace
   one - fails the suite with instructions.

To change the guard's behaviour, do NOT edit a `_V1` name. Add
`CREATE_APPEND_ONLY_FUNCTION_V2` (and `append_only_triggers_v2` if the trigger
wiring changes) alongside it, add its hash to the pin test, and add a NEW
migration that runs it. V2 re-issues `CREATE OR REPLACE FUNCTION` for the same
Postgres function name on purpose: every table guarded by it must move together,
and both a fresh install (V1 then V2) and an already-migrated database (V2) end
on the same body. Migrations authored against V1 keep calling V1 forever, so
what they apply never changes.

Two things about a V2 that are easy to get wrong, because the mechanism above
only holds if both are done:

* **Order it explicitly. Django will not.** "V1 then V2" is only true on a fresh
  install if the graph says so. Migrations are ordered by their declared
  `dependencies`, and app A's migration has no implicit edge to app B's, so a V2
  shipped in a new app can be applied *before* `audit/0002` - leaving the fresh
  database on the V1 body while every already-migrated database ends on V2:
  precisely the fork this module exists to prevent, arriving through the one
  door left open. A V2 migration must depend on every migration that installs an
  earlier version of the function, `("audit", "0002_append_only_trigger")`
  included, and on any later one that does the same.
* **A V2's `reverse_sql` is `CREATE_APPEND_ONLY_FUNCTION_V1`, never a DROP.**
  Reversing a V2 must restore the previous *body*, not remove the function:
  `DROP FUNCTION raporo_append_only()` is refused by Postgres (dependent objects)
  for as long as a single guarded table still has a trigger on it, so a DROP
  reverse makes the migration unreversible the moment there is more than one
  guarded table - and correct only in the case where it does not matter.

Installing the guard on a NEW table (the slice-2 ledger case)
-------------------------------------------------------------
The function is a shared, database-wide object with exactly one owner:
`audit/0002` installs it and its reverse removes it. A migration that guards a
new table therefore carries **only the trigger operation**:

    dependencies = [
        ("ledger", "0001_initial"),
        ("audit", "0002_append_only_trigger"),   # the function must exist first
    ]

    FORWARD, REVERSE = append_only_triggers_v1("ledger_stockmovement")

    operations = [migrations.RunSQL(sql=FORWARD, reverse_sql=REVERSE)]

Copying `audit/0002` wholesale instead - both of its operations - installs a
second lifecycle for one shared object and makes the new migration irreversible
as soon as two tables are guarded: reversing drops its own triggers, then hits
`DROP FUNCTION` while `audit_auditlog`'s triggers still depend on it. Add the
hashes of the new table's forward and reverse text to `PINNED_MIGRATION_SQL` in
`tests/test_db_stability.py`; the helper's name being pinned does not pin the
text it returns for a table it has never been called with.
"""

#: The Postgres function name. Deliberately unversioned: the object is shared by
#: every guarded table, and a V2 body replaces it in place (see the docstring).
#: The SQL below spells it out literally rather than interpolating this constant,
#: so that renaming this cannot silently rewrite frozen text.
APPEND_ONLY_FUNCTION = "raporo_append_only"

#: FROZEN - shipped in apps/audit/migrations/0002_append_only_trigger.py.
#: Do not edit. Add a V2 constant + a new migration instead.
#:
#: Raises on UPDATE/DELETE unconditionally - that is the forgery guard, and it
#: has no escape hatch by design.
#:
#: TRUNCATE is refused too (TRUNCATE bypasses row triggers, so it needs its own
#: statement trigger), with two deliberate exceptions:
#:   * a database whose name starts with `test_`, which is how Django names test
#:     databases - `TransactionTestCase` teardown flushes tables with TRUNCATE
#:     and would otherwise be unable to clean up;
#:   * a session that has explicitly set `raporo.allow_truncate = 'on'`, for a
#:     reviewed retention purge run by a human.
#: Neither exception can help an attacker rewrite or remove individual rows.
#:
#: `raporo.enforce_truncate_guard = 'on'` turns both exemptions off for the
#: session, which is how the test suite proves the TRUNCATE trigger really does
#: refuse - inside a test database, where the first exemption would otherwise
#: always apply.
CREATE_APPEND_ONLY_FUNCTION_V1 = """
CREATE OR REPLACE FUNCTION raporo_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'TRUNCATE'
        AND current_setting('raporo.enforce_truncate_guard', true) IS DISTINCT FROM 'on'
        AND (
            current_setting('raporo.allow_truncate', true) = 'on'
            OR current_database() LIKE 'test!_%' ESCAPE '!'
        )
    THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'relation %.% is append-only: % is not permitted',
        TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

#: FROZEN - the reverse of `CREATE_APPEND_ONLY_FUNCTION_V1`. Do not edit.
DROP_APPEND_ONLY_FUNCTION_V1 = "DROP FUNCTION IF EXISTS raporo_append_only();"


def append_only_triggers_v1(table: str) -> tuple[str, str]:
    """Return (forward_sql, reverse_sql) making `table` append-only.

    FROZEN for every table name any shipped migration passes in. Do not edit;
    add `append_only_triggers_v2` instead (see the module docstring).

    The row trigger covers UPDATE and DELETE; the statement trigger covers
    TRUNCATE, which never fires row triggers.
    """
    row_trigger = f"{table}_append_only"
    truncate_trigger = f"{table}_no_truncate"
    forward = f"""
DROP TRIGGER IF EXISTS {row_trigger} ON {table};
CREATE TRIGGER {row_trigger}
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION raporo_append_only();

DROP TRIGGER IF EXISTS {truncate_trigger} ON {table};
CREATE TRIGGER {truncate_trigger}
    BEFORE TRUNCATE ON {table}
    FOR EACH STATEMENT EXECUTE FUNCTION raporo_append_only();
"""
    reverse = f"""
DROP TRIGGER IF EXISTS {row_trigger} ON {table};
DROP TRIGGER IF EXISTS {truncate_trigger} ON {table};
"""
    return forward, reverse
