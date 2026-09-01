"""Database-level append-only guard on `audit_auditlog`.

Python-only enforcement (`AuditLog.save()`, `AppendOnlyQuerySet`) is the first
line, not the last: a data migration, a management command using
`_base_manager`, `bulk_create(update_conflicts=True)`, or plain `psql` all reach
the table without going through the model. This installs the same guarantee
where the data lives.

Two triggers, because they cover different things:
  * a row trigger for UPDATE and DELETE - no exceptions, ever;
  * a statement trigger for TRUNCATE, which never fires row triggers. It has two
    deliberate exemptions (a `test_*` database, so `TransactionTestCase`
    teardown can flush; a session that explicitly sets
    `raporo.allow_truncate = 'on'`, for a reviewed retention purge). Neither can
    rewrite or remove an individual row.

`REVOKE UPDATE, DELETE` on the table was considered and rejected: Django runs
migrations as the same role, so it would block our own schema changes.

The SQL comes from `common/db.py` so slice 2's ledger tables (StockMovement,
Payment, CapitalEntry, Payout) install the identical guard with one call - the
**trigger** call. This migration owns the shared `raporo_append_only()` function:
it is the only place that creates it and the only place that drops it. A ledger
migration copying both operations from here would give one database-wide object
two lifecycles and be irreversible the moment a second table is guarded (its
reverse drops its own triggers, then fails on `DROP FUNCTION` while this table's
triggers still depend on the function). The shape to copy is in `common/db.py`:
depend on `("audit", "0002_append_only_trigger")`, carry the trigger operation
only, and pin the new table's forward/reverse text in `tests/test_db_stability`.

The `_V1` names are frozen text, not a moving target: Django replays a migration
by name, so anything this file imports must never change afterwards or a fresh
install stops matching an already-migrated database. `tests/test_db_stability.py`
pins a SHA-256 of exactly the four strings below. A behavioural change gets a V2
constant and a new migration - never an edit here or in `common/db.py`.
"""

from django.db import migrations

from common.db import (
    CREATE_APPEND_ONLY_FUNCTION_V1,
    DROP_APPEND_ONLY_FUNCTION_V1,
    append_only_triggers_v1,
)

TRIGGERS_FORWARD, TRIGGERS_REVERSE = append_only_triggers_v1("audit_auditlog")


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=CREATE_APPEND_ONLY_FUNCTION_V1,
            reverse_sql=DROP_APPEND_ONLY_FUNCTION_V1,
        ),
        migrations.RunSQL(
            sql=TRIGGERS_FORWARD,
            reverse_sql=TRIGGERS_REVERSE,
        ),
    ]
