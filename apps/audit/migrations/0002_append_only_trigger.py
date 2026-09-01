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
Payment, CapitalEntry, Payout) install the identical guard with one call.
"""

from django.db import migrations

from common.db import (
    CREATE_APPEND_ONLY_FUNCTION,
    DROP_APPEND_ONLY_FUNCTION,
    append_only_triggers,
)

TRIGGERS_FORWARD, TRIGGERS_REVERSE = append_only_triggers("audit_auditlog")


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=CREATE_APPEND_ONLY_FUNCTION,
            reverse_sql=DROP_APPEND_ONLY_FUNCTION,
        ),
        migrations.RunSQL(
            sql=TRIGGERS_FORWARD,
            reverse_sql=TRIGGERS_REVERSE,
        ),
    ]
