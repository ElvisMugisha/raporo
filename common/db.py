"""Reusable PostgreSQL guards used from migrations.

Slice 2's ledger tables (StockMovement, Payment, CapitalEntry, Payout) are
append-only for the same reasons the audit log is, so this is written once,
here, and called from each migration that needs it.

Stability contract: the SQL below is *frozen*. Migrations that already ran hold
a copy of whatever `CREATE OR REPLACE FUNCTION` text was current when they ran,
so changing the function body here changes behaviour only for databases that
run a later migration calling it again. Any behavioural change therefore needs
a new migration that re-issues `CREATE OR REPLACE FUNCTION`.
"""

APPEND_ONLY_FUNCTION = "raporo_append_only"

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
CREATE_APPEND_ONLY_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {APPEND_ONLY_FUNCTION}() RETURNS trigger
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

DROP_APPEND_ONLY_FUNCTION = f"DROP FUNCTION IF EXISTS {APPEND_ONLY_FUNCTION}();"


def append_only_triggers(table: str) -> tuple[str, str]:
    """Return (forward_sql, reverse_sql) making `table` append-only.

    The row trigger covers UPDATE and DELETE; the statement trigger covers
    TRUNCATE, which never fires row triggers.
    """
    row_trigger = f"{table}_append_only"
    truncate_trigger = f"{table}_no_truncate"
    forward = f"""
DROP TRIGGER IF EXISTS {row_trigger} ON {table};
CREATE TRIGGER {row_trigger}
    BEFORE UPDATE OR DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}();

DROP TRIGGER IF EXISTS {truncate_trigger} ON {table};
CREATE TRIGGER {truncate_trigger}
    BEFORE TRUNCATE ON {table}
    FOR EACH STATEMENT EXECUTE FUNCTION {APPEND_ONLY_FUNCTION}();
"""
    reverse = f"""
DROP TRIGGER IF EXISTS {row_trigger} ON {table};
DROP TRIGGER IF EXISTS {truncate_trigger} ON {table};
"""
    return forward, reverse
