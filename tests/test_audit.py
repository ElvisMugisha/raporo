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
        "user.created", actor=actor, target=actor, changes={"fields_set": ["username", "email"]}
    )

    assert row.pk is not None
    assert row.action == "user.created"
    assert row.actor == actor
    assert row.target_type == "accounts.User"
    assert row.target_id == actor.pk
    assert row.changes == {"fields_set": ["username", "email"]}
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


# --------------------------------------------------------------------------
# P-1 - `changes` carries no personal *values*
#
# The privacy ruling (docs/superpowers/specs/2026-09-02-privacy-law-058-2021-
# ruling.md) turns on one guarantee: an audit row must stop identifying anyone
# the moment the row it points at is anonymized. A row is append-only at the
# database level, so anything personal that lands in `changes` is un-erasable
# without a migration. These tests are that guarantee.
# --------------------------------------------------------------------------


def test_the_measured_leak_is_closed(actor):
    """The exact payload the ruling reproduced by execution. It leaked three values."""
    row = audit_services.record(
        "user.created",
        actor=actor,
        target=actor,
        changes={
            "email": "eva@example.rw",
            "phone": "250788000001",
            "password": "S3cret!",
            "username": "eva",
        },
    )
    row.refresh_from_db()

    assert row.changes == {
        "email": "[redacted]",
        "phone": "[redacted]",
        "password": "[redacted]",
        "username": "[redacted]",
    }


IDENTIFIER_KEYS = {
    "email": "eva@example.rw",
    "user_email": "eva@example.rw",
    "email_address": "eva@example.rw",
    "phone": "250788000001",
    "phone_number": "250788000001",
    "whatsapp_phone": "250788000001",
    "username": "eva",
    "first_name": "Eva",
    "last_name": "Mukamana",
    "full_name": "Eva Mukamana",
    "surname": "Mukamana",
    "nickname": "Eva",
    "contact": "250788000001",
    "contact_email": "eva@example.rw",
    "address": "KG 7 Ave, Kigali",
    "billing_address": "KG 7 Ave, Kigali",
    "customer": "Eva Mukamana",
    "customer_name": "Eva Mukamana",
    "investor": "Eva Mukamana",
    "investor_name": "Eva Mukamana",
    "logo": "orgs/41/logo.png",
    "logo_url": "https://example.rw/media/orgs/41/logo.png",
    "ip": "41.186.0.1",
    "client_ip": "41.186.0.1",
    "remote_ip": "41.186.0.1",
}


def test_every_identifier_key_is_redacted(actor):
    """One row, every identifier key. No value may survive anywhere in the payload."""
    row = audit_services.record("user.updated", actor=actor, changes=dict(IDENTIFIER_KEYS))
    row.refresh_from_db()

    assert row.changes == dict.fromkeys(IDENTIFIER_KEYS, "[redacted]")
    stored = str(row.changes)
    for value in set(IDENTIFIER_KEYS.values()):
        assert value not in stored


def test_every_credential_key_is_still_redacted(actor):
    """Built from the constant, so dropping a term from it fails here."""
    changes = {}
    for part in audit_services.CREDENTIAL_KEY_PARTS:
        changes[part] = "leak-me"
        changes[f"new_{part}_hash"] = "leak-me"

    row = audit_services.record("user.password_changed", actor=actor, changes=changes)
    row.refresh_from_db()

    assert set(row.changes) == set(changes)
    assert set(row.changes.values()) == {"[redacted]"}
    assert audit_services.CREDENTIAL_KEY_PARTS, "the credential denylist may not be emptied"


def test_non_personal_values_survive_verbatim(actor):
    """The trail's whole point. A diff of business facts must stay readable."""
    changes = {
        "price_before": "1500.00",
        "price_after": "1800.00",
        "currency": "RWF",
        "permissions_added": ["sale.create", "report.view"],
        "permissions_removed": [],
        "store_limit": 5,
        "stores_used": 5,
        "reason": "store_limit_reached",
        "period": "2026-09-01/2026-09-15",
    }

    row = audit_services.record("plan.limit_hit", actor=actor, changes=changes)
    row.refresh_from_db()

    assert row.changes == changes


#: Keys carrying a `name` segment, and whether the value may be stored.
#: Substring-matching `name` would redact the whole left column and gut the
#: trail; matching only person-qualified names keeps it useful. Pinned both
#: ways on purpose - widening or narrowing this is a policy change, not a tweak.
NAME_KEYS_KEPT = ("store_name", "role_name", "org_name", "permission_name", "filename")
NAME_KEYS_REDACTED = ("name", "owner_name", "member_name", "employee_name", "user_name")


def test_a_thing_name_survives_and_a_person_name_does_not(actor):
    changes = {key: f"value-of-{key}" for key in NAME_KEYS_KEPT + NAME_KEYS_REDACTED}

    row = audit_services.record("store.renamed", actor=actor, changes=changes)
    row.refresh_from_db()

    for key in NAME_KEYS_KEPT:
        assert row.changes[key] == f"value-of-{key}", f"{key} should stay readable"
    for key in NAME_KEYS_REDACTED:
        assert row.changes[key] == "[redacted]", f"{key} names a person"


def test_a_reference_to_a_personal_record_survives(actor):
    """The policy is: field names and IDs for anything personal. IDs are pointers."""
    changes = {
        "customer_id": 41,
        "customer_ids": [41, 42],
        "investor_id": 7,
        "contact_id": 9,
        "fields_cleared": ["username", "email", "phone"],
    }

    row = audit_services.record("user.erased", actor=actor, changes=changes)
    row.refresh_from_db()

    assert row.changes == changes


def test_a_credential_handle_is_not_exempted_by_its_id_suffix(actor):
    """The ID carve-out is for identifiers only. A handle to a secret is a secret."""
    row = audit_services.record(
        "session.revoked",
        actor=actor,
        changes={"session_id": "abc123", "token_id": "def456", "api_key_id": "ghi789"},
    )
    row.refresh_from_db()

    assert row.changes == {
        "session_id": "[redacted]",
        "token_id": "[redacted]",
        "api_key_id": "[redacted]",
    }


def test_redaction_reaches_into_nested_structures(actor):
    row = audit_services.record(
        "membership.created",
        actor=actor,
        changes={
            "member": {"email": "eva@example.rw", "role_name": "Cashier", "user_id": 41},
            "invites": [{"contact": "250788000001", "channel": "whatsapp"}],
        },
    )
    row.refresh_from_db()

    assert row.changes == {
        "member": {"email": "[redacted]", "role_name": "Cashier", "user_id": 41},
        "invites": [{"contact": "[redacted]", "channel": "whatsapp"}],
    }


#: Keys that must survive because a term on a denylist hides inside them.
#: The `ip` trap is why `ip` is segment-matched and not substring-matched:
#: substring `ip` is inside `description`, `membership`, `receipt` and
#: `relationship`, and redacting those would gut the trail wholesale.
SUBSTRING_TRAPS = (
    "description",
    "membership",
    "relationship",
    "receipt",
    "participants",
    "equipment",
    "zip_code",
    "discount",
    "bank_account",
    "note",
    "reason",
)


def test_a_denylist_term_hiding_inside_an_innocent_key_does_not_redact_it(actor):
    changes = {key: f"value-of-{key}" for key in SUBSTRING_TRAPS}

    row = audit_services.record("store.updated", actor=actor, changes=changes)
    row.refresh_from_db()

    assert row.changes == changes


def test_an_aggregate_over_personal_records_survives(actor):
    """A count identifies nobody, and an export row is worthless without it."""
    row = audit_services.record(
        "org.exported",
        actor=actor,
        changes={"customer_count": 812, "member_counts": {"active": 4}, "customer": "Eva"},
    )
    row.refresh_from_db()

    assert row.changes == {
        "customer_count": 812,
        "member_counts": {"active": 4},
        "customer": "[redacted]",
    }


def test_an_ip_is_redacted_however_it_is_spelled(actor):
    row = audit_services.record(
        "user.logged_in",
        actor=actor,
        changes={"ip": "41.186.0.1", "client_ip": "41.186.0.1", "ipv6": "2c0f:f000::1"},
    )
    row.refresh_from_db()

    assert set(row.changes.values()) == {"[redacted]"}
