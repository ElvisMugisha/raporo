"""audit.AuditLog + audit.services.record: append-only, attributable."""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models, transaction
from django.utils import timezone

from apps.audit import services as audit_services
from apps.audit.models import AppendOnlyError, AuditLog, action_validator


def snapshot(pk) -> dict:
    """Every stored column of one row, for byte-for-byte comparison."""
    row = AuditLog.objects.get(pk=pk)
    return {field.attname: getattr(row, field.attname) for field in AuditLog._meta.fields}

pytestmark = pytest.mark.django_db


def test_record_writes_a_row(actor):
    row = audit_services.record(
        "user.created", actor=actor, target=actor, changes={"username": "eva"}
    )

    assert row.pk is not None
    assert row.action == "user.created"
    assert row.actor == actor
    assert row.target_type == "accounts.User"
    assert row.target_id == actor.pk
    assert row.changes == {"username": "eva"}
    assert row.at is not None


def test_record_without_a_target_or_actor_is_allowed(org):
    row = audit_services.record("system.housekeeping", org=org)

    assert row.actor is None
    assert row.target_type == ""
    assert row.target_id is None
    assert row.changes == {}


def test_record_derives_the_org_from_the_store(actor, store):
    row = audit_services.record("store.renamed", actor=actor, store=store, target=store)

    assert row.org_id == store.org_id


def test_record_rejects_a_store_from_another_org(actor, org, foreign_store):
    """Invariant #1 at the audit boundary: no cross-org rows."""
    with pytest.raises(ValueError):
        audit_services.record("store.renamed", actor=actor, org=org, store=foreign_store)


@pytest.mark.parametrize(
    "action",
    ["", "   ", "User.Created", "user created", "user..created", "user", "x" * 81, None],
)
def test_record_rejects_a_malformed_action(action):
    with pytest.raises((ValidationError, ValueError, TypeError)):
        audit_services.record(action)


def test_record_rejects_an_unsaved_target(org):
    from apps.orgs.models import Store

    with pytest.raises(ValueError):
        audit_services.record("store.created", target=Store(org=org, name="ghost"))


def test_record_redacts_sensitive_values(actor):
    row = audit_services.record(
        "user.password_changed",
        actor=actor,
        target=actor,
        changes={
            "password": "S3cure!passphrase",
            "nested": {"totp_secret": "ABCDEF", "keep": "visible"},
            "token_hash": "deadbeef",
        },
    )

    assert row.changes["password"] == "[redacted]"
    assert row.changes["nested"]["totp_secret"] == "[redacted]"
    assert row.changes["nested"]["keep"] == "visible"
    assert row.changes["token_hash"] == "[redacted]"


def test_record_rejects_changes_it_cannot_serialise(actor):
    with pytest.raises((TypeError, ValueError)):
        audit_services.record("user.updated", actor=actor, changes={"obj": object()})


def test_record_requires_a_mapping_for_changes(actor):
    with pytest.raises(TypeError):
        audit_services.record("user.updated", actor=actor, changes=["not", "a", "mapping"])


def test_record_serialises_decimals_and_dates(actor):
    from datetime import date
    from decimal import Decimal

    row = audit_services.record(
        "sale.recorded",
        actor=actor,
        changes={"amount": Decimal("1500.00"), "on": date(2026, 9, 1)},
    )
    row.refresh_from_db()

    assert row.changes == {"amount": "1500.00", "on": "2026-09-01"}


def test_record_stores_the_ip(actor):
    row = audit_services.record("user.logged_in", actor=actor, ip="41.186.0.1")

    assert row.ip == "41.186.0.1"


def test_record_rejects_a_bogus_ip(actor):
    with pytest.raises(ValidationError):
        audit_services.record("user.logged_in", actor=actor, ip="not-an-ip")


def test_audit_rows_cannot_be_updated(actor):
    row = audit_services.record("user.created", actor=actor, target=actor)

    row.action = "user.deleted"
    with pytest.raises(AppendOnlyError):
        row.save()

    assert AuditLog.objects.get(pk=row.pk).action == "user.created"


def test_audit_rows_cannot_be_bulk_updated(actor):
    audit_services.record("user.created", actor=actor, target=actor)

    with pytest.raises(AppendOnlyError):
        AuditLog.objects.all().update(action="user.deleted")


def test_audit_rows_cannot_be_deleted(actor):
    row = audit_services.record("user.created", actor=actor, target=actor)

    with pytest.raises(AppendOnlyError):
        row.delete()
    with pytest.raises(AppendOnlyError):
        AuditLog.objects.all().delete()

    assert AuditLog.objects.count() == 1


def test_the_action_field_carries_the_validator():
    """`record()` validates too; this keeps forms and the admin honest."""
    assert action_validator in AuditLog._meta.get_field("action").validators


def test_audit_log_has_no_soft_delete_columns():
    field_names = {f.name for f in AuditLog._meta.get_fields()}

    assert "deleted_at" not in field_names
    assert "deleted_by" not in field_names


def test_the_actor_reference_is_protected(actor):
    """The trail keeps its subject: the FK is PROTECT, and users cannot be
    hard-deleted at all (see test_user_model.py)."""
    audit_services.record("user.created", actor=actor, target=actor)

    assert AuditLog._meta.get_field("actor").remote_field.on_delete is models.PROTECT


def test_newest_rows_come_first(actor):
    first = audit_services.record("user.created", actor=actor)
    second = audit_services.record("user.updated", actor=actor)

    assert list(AuditLog.objects.all()) == [second, first]


# --------------------------------------------------------------------------
# B1 - the log is not forgeable in-process
# --------------------------------------------------------------------------


def test_an_audit_row_cannot_be_overwritten_through_an_explicit_pk(actor, org):
    """Reproduced: constructing with `pk=1` and saving rewrote row 1, turning
    Mallory's below-floor override into a login by Alice.

    `_state.adding` is True on such an instance, and Django's `_save_table`
    takes the UPDATE branch when a pk is set.
    """
    original = audit_services.record(
        "sale.below_floor_override", actor=actor, org=org, changes={"who": "mallory"}
    )
    before = snapshot(original.pk)

    forgery = AuditLog(
        pk=original.pk,
        action="user.login",
        actor=actor,
        org=org,
        changes={},
        at=timezone.now(),
    )
    with pytest.raises(AppendOnlyError):
        forgery.save()

    assert snapshot(original.pk) == before
    assert AuditLog.objects.count() == 1


def test_an_audit_row_cannot_be_overwritten_with_force_insert(actor, org):
    original = audit_services.record("sale.below_floor_override", actor=actor, org=org)
    before = snapshot(original.pk)

    with pytest.raises(AppendOnlyError):
        AuditLog(pk=original.pk, action="user.login", actor=actor, org=org).save(
            force_insert=True
        )

    assert snapshot(original.pk) == before


def test_the_base_manager_cannot_delete_audit_rows(actor):
    """`AppendOnlyQuerySet` on `objects` alone left `_base_manager` open."""
    row = audit_services.record("user.created", actor=actor)

    assert AuditLog._base_manager.name == "objects"
    with pytest.raises(AppendOnlyError):
        AuditLog._base_manager.filter(pk=row.pk).delete()

    assert AuditLog.objects.count() == 1


def test_bulk_create_cannot_be_turned_into_an_upsert(actor, org):
    row = audit_services.record("user.created", actor=actor, org=org)

    with pytest.raises(AppendOnlyError):
        AuditLog.objects.bulk_create(
            [AuditLog(pk=row.pk, action="user.login", actor=actor, org=org)],
            update_conflicts=True,
            update_fields=["action"],
            unique_fields=["id"],
        )

    assert AuditLog.objects.get(pk=row.pk).action == "user.created"


# --------------------------------------------------------------------------
# B2 - and not forgeable from outside the ORM either
# --------------------------------------------------------------------------


def test_the_append_only_triggers_exist_in_postgres(db):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT tgname FROM pg_trigger
            WHERE NOT tgisinternal
              AND tgrelid = 'audit_auditlog'::regclass
            ORDER BY tgname
            """
        )
        found = [row[0] for row in cursor.fetchall()]

    assert found == ["audit_auditlog_append_only", "audit_auditlog_no_truncate"]


def test_the_database_refuses_a_raw_update(actor):
    row = audit_services.record("sale.below_floor_override", actor=actor)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE audit_auditlog SET action = %s WHERE id = %s",
                    ["user.login", row.pk],
                )

    assert AuditLog.objects.get(pk=row.pk).action == "sale.below_floor_override"


def test_the_database_refuses_a_raw_delete(actor):
    row = audit_services.record("sale.below_floor_override", actor=actor)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM audit_auditlog WHERE id = %s", [row.pk])

    assert AuditLog.objects.filter(pk=row.pk).exists()


def test_the_database_refuses_a_truncate(db):
    """TRUNCATE never fires row triggers, hence its own statement trigger.

    The guard exempts `test_*` databases so `TransactionTestCase` teardown can
    flush; `raporo.enforce_truncate_guard` turns that exemption off, which is
    how this test reaches the real production behaviour.

    No rows are written first: Postgres refuses TRUNCATE outright in a
    transaction that still has pending trigger events on the table, and that
    refusal would mask the one being tested.
    """
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL raporo.enforce_truncate_guard = 'on'")
                cursor.execute("TRUNCATE audit_auditlog")


# --------------------------------------------------------------------------
# A5 - an audit row may not mix one org with another org's store
# --------------------------------------------------------------------------


def test_audit_rows_cannot_mix_an_org_with_a_foreign_store(org, foreign_store):
    """`record()` checks this, but `objects.create()` skips `record()`."""
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AuditLog.objects.create(action="store.renamed", org=org, store=foreign_store)


def test_an_org_only_or_store_only_audit_row_is_still_legal(org, store):
    assert audit_services.record("org.created", org=org).pk is not None
    assert AuditLog.objects.create(action="store.created", store=store).pk is not None


# --------------------------------------------------------------------------
# D6 - the payload is bounded
# --------------------------------------------------------------------------


def test_a_long_string_is_truncated_distinguishably(actor):
    row = audit_services.record("user.updated", actor=actor, changes={"note": "x" * 40_000})
    row.refresh_from_db()

    assert row.changes["note"].endswith("...[truncated]")
    assert "[redacted]" not in row.changes["note"]
    assert len(row.changes["note"]) < 2_000


def test_an_oversized_payload_is_replaced_by_a_marker(actor):
    changes = {f"field_{index}": "y" * 900 for index in range(40)}

    row = audit_services.record("user.updated", actor=actor, changes=changes)
    row.refresh_from_db()

    assert row.changes["_truncated"] == "...[truncated]"
    assert row.changes["_original_bytes"] > 16 * 1024
    assert len(str(row.changes).encode()) < 16 * 1024
