"""`loaddata` against the four `*_same_org_fk` composite foreign keys.

Those keys are `DEFERRABLE INITIALLY IMMEDIATE` - the strict end of the dial:
every write is checked at statement time, which is what makes "a membership
cannot hold another organization's role" a database fact rather than a `clean()`
convention. Django's own foreign keys are `DEFERRABLE INITIALLY DEFERRED`
instead, so ours behave differently from every other key in the schema and the
difference has to be pinned down before slice 2's ledger tables copy the pattern.

**Correcting the assumption this test was commissioned on.** The review said
Django's Postgres backend issues `SET CONSTRAINTS ALL DEFERRED` around fixture
loading, so out-of-order fixtures would keep working. It does not:
`postgresql.DatabaseWrapper` never overrides `disable_constraint_checking()`, so
the base implementation runs, returns `False`, and emits no SQL - Django simply
relies on its own keys already being `INITIALLY DEFERRED`. Only the *final*
`check_constraints()` touches `SET CONSTRAINTS`, and by then every row is in.
`test_django_does_not_defer_constraints_for_loaddata` pins that, so the day a
Django release changes it we find out here rather than in production.

⚠️ **Never assert that a bad write is refused after a `loaddata` in the same
test.** Postgres' `check_constraints()` ends with `SET CONSTRAINTS ALL DEFERRED`,
which is *transaction*-scoped, and every `TestCase` (so every pytest `db` test)
is one transaction. From the first `loaddata` onwards, all four composite keys
are deferred until that transaction ends: the violation you expect to raise is
accepted, your `pytest.raises` fails, or worse it passes vacuously somewhere
else and the error surfaces at teardown attributed to a different test. Slice 2
will load ledger fixtures and then assert cross-org writes are refused - that
combination is the landmine. `test_loaddata_leaves_the_composite_keys_deferred_
...` pins both halves.

**The remedy is a fixture, not this paragraph.** A warning here only reaches
people who open this file, and the next author to load a fixture will be writing
`tests/test_ledger_fixtures.py`. So `tests/conftest.py` - the file everyone
opens for fixtures - provides `load_fixture`, which loads and then re-arms with
`SET CONSTRAINTS ALL IMMEDIATE`. Use it; the tests below use raw `loaddata` only
where the un-remedied behaviour is the subject.

What that means in practice, and what these tests hold:

* a dependency-ordered fixture loads - and `dumpdata` emits one, so the
  backup/restore path works today and keeps working while slice 2 adds models;
* a child-first fixture is refused, loudly and by name. That is the real cost of
  `INITIALLY IMMEDIATE` and it belongs in a test, not in someone's memory;
* an inconsistent fixture is refused. Fixtures are loaded with
  `save_base(raw=True)`, so no model validation runs at all: the composite key
  is the only thing between a hand-edited fixture and a cross-tenant role.
"""

from pathlib import Path

import pytest
from django.apps import apps as global_apps
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connections, transaction

from apps.audit.models import AuditLog
from apps.orgs.models import Membership, Organization, Role, Store, StoreAccess

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: organization -> user -> store -> role -> membership -> store access -> audit
#: entry. The audit row is the fourth composite key, `audit_auditlog_store_
#: same_org_fk`, and it is the one that shares a table with the append-only
#: trigger - the combination worth exercising through the fixture path.
ORDERED = str(FIXTURES / "orgs_dependency_ordered.json")

#: The six orgs/accounts rows of `ORDERED`, reversed: every row precedes what it
#: points at.
CHILD_FIRST = str(FIXTURES / "orgs_child_first.json")

#: Dependency-ordered, so the *only* thing wrong with it is the cross-org row:
#: membership 2 sits in organization 1 while holding organization 2's role.
CROSS_ORG = str(FIXTURES / "orgs_cross_org_membership.json")

#: Children before parents - safe for raw DELETE.
TABLES_CHILD_FIRST = (
    "orgs_storeaccess",
    "orgs_membership",
    "orgs_role",
    "orgs_store",
    "orgs_organization",
    "accounts_user",
)

#: Excluded from the round-trip dump. Not application data: content types and
#: permissions are recreated by `migrate` and collide on their natural keys,
#: sessions are transient, and admin log entries point at content types.
#:
#: `audit` is excluded for a different and harder reason: an audit row cannot be
#: restored by `loaddata` at all. The append-only trigger refuses UPDATE and
#: DELETE, and `loaddata` writes with `save_base(raw=True)`, which issues an
#: UPDATE first - so re-loading a dumped audit row over an existing one is
#: refused by the trigger, and wiping the table first is refused by it too
#: (`TRUNCATE` inside a test transaction fails with "pending trigger events",
#: and the row's foreign key to `orgs_store` then blocks the store's DELETE).
#: Restoring audit history is a `pg_restore`-into-an-empty-database operation,
#: not a fixture one. Excluding it here keeps the dump honest about that.
NOT_APPLICATION_DATA = [
    "contenttypes",
    "auth.Permission",
    "sessions",
    "admin.LogEntry",
    "audit",
]


def test_django_does_not_defer_constraints_for_loaddata(db):
    """The premise every other test in this module rests on.

    `loaddata` wraps itself in `connection.constraint_checks_disabled()`, which
    sounds like deferral and is not: on Postgres the call is a no-op that reports
    it changed nothing. Our `INITIALLY IMMEDIATE` composite keys are therefore
    live for the whole load.
    """
    connection = connections[DEFAULT_DB_ALIAS]

    try:
        assert connection.disable_constraint_checking() is False
    finally:
        # A no-op on Postgres today, which is exactly what the assertion above
        # says. The day Django implements it, an unpaired call would leave
        # constraint checking off on a *session*-scoped connection and every
        # test after this one would run unguarded.
        connection.enable_constraint_checking()


def test_a_dependency_ordered_fixture_loads(load_fixture):
    load_fixture(ORDERED)

    access = StoreAccess.all_objects.get(pk=1)
    entry = AuditLog.objects.get(pk=1)

    assert access.org_id == access.membership.org_id == access.store.org_id
    assert Membership.all_objects.get(pk=1).role.org_id == access.org_id
    # The fourth composite key: the audit row's store belongs to its org.
    assert entry.org_id == entry.store.org_id == access.org_id


def test_a_child_first_fixture_is_refused_by_the_composite_key(db):
    """The documented cost of `INITIALLY IMMEDIATE`.

    The same orgs rows as `ORDERED`, reversed. Django's plain foreign keys are
    `INITIALLY DEFERRED` and tolerate it; ours do not, so a hand-written or
    hand-reordered fixture must list parents first. `dumpdata` already does
    (see the round-trip test), which is why this is a constraint on hand-editing
    rather than on the backup path.
    """
    with pytest.raises(IntegrityError) as exc:
        call_command("loaddata", CHILD_FIRST, verbosity=0)

    # By name, like the cross-org test: "some composite key fired" would also
    # pass if the wrong one did, and the two are checked in a different order.
    assert "orgs_storeaccess_membership_same_org_fk" in str(exc.value)
    # `loaddata` wraps itself in `atomic`, so nothing is left half-loaded.
    assert Organization.all_objects.count() == 0


def test_a_fixture_that_mixes_two_organizations_is_refused(db):
    """Ordering aside, the guard itself is armed through the fixture path.

    `Membership.clean()` says the same thing, but a fixture is loaded raw:
    `save_base(raw=True)` runs no model validation, so the database is the only
    thing that can refuse this row.
    """
    with pytest.raises(IntegrityError) as exc:
        call_command("loaddata", CROSS_ORG, verbosity=0)

    assert "orgs_membership_role_same_org_fk" in str(exc.value)
    assert Membership.all_objects.count() == 0
    assert Organization.all_objects.count() == 0


def dumped_app_labels() -> list[str]:
    """Every installed app that holds application data.

    Derived, not listed. `dumpdata "accounts" "orgs"` cannot catch what the
    round-trip test exists to catch: without `--natural-foreign` there is no
    `sort_dependencies` call at all, so across apps the emitted order is
    `INSTALLED_APPS` order and nothing else. A slice-2 app registered above
    `apps.orgs` produces an unrestorable full dump while a two-app dump stays
    green.

    Scanning every app is necessary but not sufficient: `dumpdata` emits nothing
    for an empty table, so the round-trip test only notices a badly ordered app
    once that app has at least one row *created in this test*. Adding a model to
    the schema is not enough - add it to the fixture data below as well.
    """
    return [config.label for config in global_apps.get_app_configs()]


def test_a_dumpdata_round_trip_restores_every_row(db, tmp_path):
    """The path that actually matters: dump, wipe, restore.

    This is the regression guard for slice 2. `dumpdata` emits models in
    registration order, so it produces a loadable file only while every model is
    defined after the models it points at. A ledger model declared above its
    parent would still pass every other test in the suite and break restore.

    It only notices a new app once that app has rows here, so extend the data
    below when slice 2 lands - and create it through managers, not through a
    service: services write audit rows, and the wipe below cannot remove those.
    """
    user = get_user_model().objects.create_user(
        username="eva",
        email="eva@example.rw",
        phone="250788000001",
        password="S3cure!passphrase",
    )
    org = Organization.objects.create(name="Eva Shop", slug="eva-shop")
    store = Store.objects.create(org=org, name="Main")
    role = Role.objects.create(org=org, name="Owner")
    membership = Membership.objects.create(user=user, org=org, role=role)
    access = StoreAccess.objects.create(membership=membership, store=store)

    dump = tmp_path / "dump.json"
    with dump.open("w", encoding="utf-8") as handle:
        call_command(
            "dumpdata",
            *dumped_app_labels(),
            exclude=NOT_APPLICATION_DATA,
            indent=2,
            stdout=handle,
        )

    # The wipe below is a plain child-first DELETE, and `audit_auditlog` cannot
    # be in it: the append-only trigger refuses DELETE, and the row's foreign key
    # to `orgs_store` would then refuse the store's DELETE as well. So this test
    # may not create audit rows - and every service call in slice 2 writes one,
    # which is why this is an assertion with a remedy rather than a comment: the
    # alternative is a foreign-key violation five lines further down that reads
    # like a broken dump.
    assert AuditLog.objects.count() == 0, (
        "This round trip cannot survive an audit row: the append-only trigger "
        "refuses both the DELETE that would wipe it and the UPDATE that "
        "`loaddata` issues to restore it. Build this test's data through the "
        "model managers rather than through a service, or move the test to "
        "`pytest.mark.django_db(transaction=True)` and TRUNCATE the tables "
        "outside the test transaction."
    )

    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        for table in TABLES_CHILD_FIRST:
            cursor.execute(f"DELETE FROM {table}")
    assert StoreAccess.all_objects.count() == 0

    call_command("loaddata", str(dump), verbosity=0)

    restored = StoreAccess.all_objects.get(pk=access.pk)

    assert restored.org_id == org.pk
    assert restored.membership_id == membership.pk
    assert Membership.all_objects.get(pk=membership.pk).role_id == role.pk
    assert get_user_model().objects.get(pk=user.pk).username == "eva"


# --------------------------------------------------------------------------
# The post-loaddata deferral window - a slice-2 landmine, pinned
# --------------------------------------------------------------------------


def a_membership_and_another_organizations_role():
    """A valid membership, plus a role that belongs to somebody else."""
    user = get_user_model().objects.create_user(
        username="chantal",
        email="chantal@example.rw",
        phone="250788000009",
        password="S3cure!passphrase",
    )
    mine = Organization.objects.create(name="Mine", slug="mine")
    theirs = Organization.objects.create(name="Theirs", slug="theirs")
    membership = Membership.objects.create(
        user=user, org=mine, role=Role.objects.create(org=mine, name="Owner")
    )
    return membership, Role.objects.create(org=theirs, name="Owner")


def point_at(membership, role) -> int:
    """The cross-org write, straight to SQL: `update()` skips `clean()`."""
    return Membership.all_objects.filter(pk=membership.pk).update(role=role)


def test_a_cross_organization_role_is_refused_at_statement_time(db):
    """The baseline the next test is measured against."""
    membership, their_role = a_membership_and_another_organizations_role()

    with pytest.raises(IntegrityError) as exc:
        point_at(membership, their_role)

    assert "orgs_membership_role_same_org_fk" in str(exc.value)


def test_loaddata_leaves_the_composite_keys_deferred_for_the_rest_of_the_transaction(db):
    """The landmine itself: the identical write, accepted, because of a
    `loaddata` earlier in the same transaction.

    `BaseDatabaseWrapper.check_constraints()` on Postgres is
    `SET CONSTRAINTS ALL IMMEDIATE` followed by `SET CONSTRAINTS ALL DEFERRED`,
    and the second one lasts until the transaction ends. Nothing about our keys
    changed - they are still armed, as the second half shows - but the moment
    they are checked moved to commit time, which in a test is teardown, where
    the failure is attributed to whatever ran last.
    """
    call_command("loaddata", ORDERED, verbosity=0)
    membership, their_role = a_membership_and_another_organizations_role()
    own_role_id = membership.role_id

    assert point_at(membership, their_role) == 1  # accepted. It must not be.

    with pytest.raises(IntegrityError) as exc, transaction.atomic():
        with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    assert "orgs_membership_role_same_org_fk" in str(exc.value)

    # Put the row back before leaving: the violation is still sitting in this
    # test's transaction, and `TestCase._fixture_teardown` runs the very same
    # check before rolling back - which would fail this test from teardown, in
    # exactly the confusing way the module docstring warns about.
    Membership.all_objects.filter(pk=membership.pk).update(role_id=own_role_id)


def test_set_constraints_all_immediate_restores_the_guard_after_a_loaddata(db):
    """The documented remedy, executed rather than asserted in a comment."""
    call_command("loaddata", ORDERED, verbosity=0)
    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    membership, their_role = a_membership_and_another_organizations_role()

    with pytest.raises(IntegrityError):
        point_at(membership, their_role)


def test_the_shared_load_fixture_re_arms_the_keys_by_itself(load_fixture):
    """The same remedy, reached without knowing it exists.

    `tests/conftest.py`'s `load_fixture` is the mechanism that replaces the
    warning in this module's docstring, so it needs the test the warning never
    had: after loading through it, the cross-organization write that the plain
    `loaddata` above silently accepts is refused at statement time again.
    """
    load_fixture(ORDERED)
    membership, their_role = a_membership_and_another_organizations_role()

    with pytest.raises(IntegrityError) as exc:
        point_at(membership, their_role)

    assert "orgs_membership_role_same_org_fk" in str(exc.value)
