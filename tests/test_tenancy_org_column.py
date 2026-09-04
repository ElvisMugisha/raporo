"""Invariant #1 as a property of the schema, not only of the code.

Every store-scoped table carries `org` alongside `store` and a composite
foreign key `(store_id, org_id) -> orgs_store (id, org_id)`. That key is the
only guard in this codebase that a data migration, a `psql` session or a
service written next year cannot forget, so these tests watch it refuse things
rather than checking it is there: a `grep` for the constraint name is a presence
check, and four slice-1 controls were present, correctly named, documented and
never executed.

`pg_constraint`, never `information_schema`. Two reasons and both are the
difference between a test and a decoration: `information_schema` exposes no
deferrability columns at all, so it cannot see the one property that decides
whether the negative tests below mean anything; and its views are filtered by
the privileges of `current_user`, so under the non-owner app role they return
fewer rows - a vacuous pass wearing a green tick.
"""

import pytest
from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.db.utils import OperationalError

from apps.orgs.models import Organization, Store
from common.managers import (
    CrossStoreReferenceError,
    UnscopedQueryError,
    resolve_scope,
)
from common.models import StoreScopedModel
from tests.testapp.models import Product, ScopedThing

#: The keys `orgs/0001_initial` and `audit/0001_initial` shipped. Hard-coded,
#: which is honest for a fixed set, and they are the premise that stops the
#: enumerating test below from passing vacuously on an empty registry.
SHIPPED_KEYS = [
    "audit_auditlog_store_same_org_fk",
    "orgs_membership_role_same_org_fk",
    "orgs_storeaccess_membership_same_org_fk",
    "orgs_storeaccess_store_same_org_fk",
]

#: `contype = 'f'` on every query. PostgreSQL 18 records `NOT NULL` as real
#: `pg_constraint` rows with `contype = 'n'`, so an unfiltered query returns
#: rows that were not there on PG 17 and the test goes red for the wrong reason.
CONSTRAINT_SQL = """
SELECT
    c.conname,
    c.condeferrable,
    c.condeferred,
    c.confmatchtype,
    (
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    ) AS columns,
    (
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum
    ) AS referenced_columns,
    target.relname AS referenced_table
FROM pg_constraint c
JOIN pg_class target ON target.oid = c.confrelid
WHERE c.contype = 'f' AND c.conname = %s
"""


def scoped_models():
    return [
        model
        for model in apps.get_models()
        if issubclass(model, StoreScopedModel) and not model._meta.abstract
    ]


def foreign_key(name: str):
    with connection.cursor() as cursor:
        cursor.execute(CONSTRAINT_SQL, [name])
        row = cursor.fetchone()
    return row


# --------------------------------------------------------------------------
# The key exists, on every table, in the one shape that means anything
# --------------------------------------------------------------------------


def test_every_store_scoped_table_has_its_same_org_key(db):
    """The loop lives here and not in the migration.

    A migration that enumerated the model registry at apply time would emit
    different SQL depending on which models happened to exist when it ran, which
    is the fork the SHA-256 pins exist to prevent. A *test* that enumerates the
    registry is the other half of the same bargain: it is what notices a slice-2
    table that shipped without its key.
    """
    models = scoped_models()

    # Premise, twice over. An enumeration that silently found nothing must fail,
    # and a `pg_constraint` query that can no longer see the four shipped keys
    # is broken rather than reassuring.
    assert len(models) >= 5, [model._meta.label for model in models]
    for name in SHIPPED_KEYS:
        assert foreign_key(name) is not None, name

    missing = []
    for model in models:
        table = model._meta.db_table
        row = foreign_key(f"{table}_store_same_org_fk")
        if row is None:
            missing.append(table)
            continue
        name, deferrable, deferred, matchtype, columns, referenced, target = row
        assert deferrable is True, name
        assert deferred is False, name
        # MATCH SIMPLE. 'f' would be MATCH FULL, which changes what happens when
        # a column is NULL - both are NOT NULL here, so this pins the default.
        assert matchtype == "s", name
        assert columns == ["store_id", "org_id"], name
        assert referenced == ["id", "org_id"], name
        assert target == "orgs_store", name

    assert missing == [], (
        f"These store-scoped tables have no <table>_store_same_org_fk: {missing}. "
        f"Add a migration carrying common.db.same_org_fk_v1(<table>) and pin both "
        f"statements in tests/test_db_stability.py."
    )


def test_the_key_is_deferrable_but_immediate_by_default(db):
    """Quoted from the catalogue, because the pair is what makes the negative
    tests below able to fire at all: `condeferrable = t` leaves
    `SET CONSTRAINTS ALL DEFERRED` available for a caller who needs it, and
    `condeferred = f` means the check happens at statement end - inside a
    `TestCase`, which never commits."""
    name, deferrable, deferred, *_ = foreign_key("testapp_scopedthing_store_same_org_fk")

    assert (deferrable, deferred) == (True, False), (name, deferrable, deferred)


def test_no_single_column_foreign_key_was_added_on_org(db):
    """`db_constraint=False`, asserted against the catalogue.

    A plain `org_id -> orgs_organization` key would guarantee nothing the
    composite key does not (validity is transitive through `orgs_store.org_id`)
    and would take `FOR KEY SHARE` on the organization row for every insert
    into every store-scoped table - see the lock test below.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.conname
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_class target ON target.oid = c.confrelid
            WHERE c.contype = 'f'
              AND t.relname LIKE 'testapp!_%' ESCAPE '!'
              AND target.relname = 'orgs_organization'
            ORDER BY c.conname
            """
        )
        found = [row[0] for row in cursor.fetchall()]

    # `testapp_category` is org-level, not store-scoped: its `org` FK is a real
    # one and must stay. Anything else here is a redundant key on a
    # store-scoped table.
    assert found == ["testapp_category_org_id_a3f2b4c8_fk_orgs_organization_id"] or all(
        name.startswith("testapp_category_") for name in found
    ), found


def test_no_index_was_created_for_the_org_column(db):
    """`db_index=False`: the indexes that serve a tenant predicate lead with
    `org`, and Django's automatic single-column FK index is a redundant prefix
    of every one of them - pure write cost on the hottest tables in the
    product."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT indexname, indexdef FROM pg_indexes
            WHERE tablename = 'testapp_scopedthing'
            ORDER BY indexname
            """
        )
        indexes = cursor.fetchall()

    # Premise: this table does have indexes, so "none of them is org-only" is
    # not an artefact of reading the wrong relation.
    assert indexes
    org_only = [
        name for name, definition in indexes if definition.endswith("(org_id)")
    ]
    assert org_only == [], org_only


# --------------------------------------------------------------------------
# The key refuses things. Watched, not assumed.
# --------------------------------------------------------------------------


def test_the_key_refuses_a_cross_organization_row_through_the_orm(
    db, store, other_org, actor
):
    """`all_objects` is the audit view, so it does not stamp `org` from a pin -
    and `_derive_org` cannot catch a value asserted with no cached store, by
    design (verifying it would cost a query on every save). This is the row the
    database has to refuse, and it does."""
    # Positive control first: the same write with the right organization works.
    ScopedThing.all_objects.create(
        store_id=store.pk, org_id=store.org_id, name="mine", created_by=actor
    )

    with pytest.raises(IntegrityError) as exc:
        ScopedThing.all_objects.create(
            store_id=store.pk, org_id=other_org.pk, name="theirs", created_by=actor
        )

    assert "testapp_scopedthing_store_same_org_fk" in str(exc.value)


def test_the_key_refuses_a_cross_organization_row_from_raw_sql(db, store, other_org):
    """The one that matters: no Python of ours is involved.

    A data migration, a psql session, a `COPY`, or a service that has not been
    written yet all arrive here. If this passes only through the ORM then the
    guarantee is a convention.
    """
    insert = """
        INSERT INTO testapp_scopedthing
            (created_at, updated_at, name, store_id, org_id, public_id)
        VALUES (now(), now(), %s, %s, %s, gen_random_uuid())
    """
    with connection.cursor() as cursor:
        # Positive control: the statement itself is valid.
        cursor.execute(insert, ["raw-mine", store.pk, store.org_id])

    with pytest.raises(IntegrityError) as exc, connection.cursor() as cursor:
        cursor.execute(insert, ["raw-theirs", store.pk, other_org.pk])

    assert "testapp_scopedthing_store_same_org_fk" in str(exc.value)


def test_the_key_refuses_an_update_that_re_homes_a_row_in_raw_sql(
    db, store, other_org, actor
):
    row = ScopedThing.all_objects.create(
        store_id=store.pk, org_id=store.org_id, name="mine", created_by=actor
    )

    with pytest.raises(IntegrityError) as exc, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE testapp_scopedthing SET org_id = %s WHERE id = %s",
            [other_org.pk, row.pk],
        )

    assert "testapp_scopedthing_store_same_org_fk" in str(exc.value)


def test_a_stores_organization_cannot_be_changed_while_it_has_rows(
    db, store, other_org, actor
):
    """The invariant that makes `ScopePin`'s cached organization sound.

    `merge_pins` compares two integers instead of re-reading `orgs_store`, which
    is only correct if a store cannot change organization underneath a pin. The
    composite key is what makes that true: with any store-scoped row present,
    PostgreSQL refuses the parent update outright (NO ACTION, and no
    `ON UPDATE` clause anywhere).
    """
    # Premise: with no rows, the update is merely a bad idea, not impossible -
    # so the refusal below is caused by the referencing row.
    Store.objects.filter(pk=store.pk).update(org=other_org)
    Store.objects.filter(pk=store.pk).update(org_id=store.org_id)

    ScopedThing.all_objects.create(
        store_id=store.pk, org_id=store.org_id, name="mine", created_by=actor
    )

    with pytest.raises(IntegrityError) as exc:
        Store.objects.filter(pk=store.pk).update(org=other_org)

    assert "testapp_scopedthing_store_same_org_fk" in str(exc.value)


# --------------------------------------------------------------------------
# The Python guards in front of it
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["org", "org_id"])
def test_update_refuses_to_rewrite_the_org_column(db, store, other_org, key, actor):
    ScopedThing.objects.create(store=store, name="mine", created_by=actor)
    value = other_org if key == "org" else other_org.pk

    with pytest.raises(CrossStoreReferenceError) as exc:
        ScopedThing.objects.for_store(store).update(**{key: value})

    message = str(exc.value)
    assert "cannot change `org`" in message
    assert "testapp_scopedthing_store_same_org_fk" in message
    # And nothing was written.
    assert ScopedThing.all_objects.get().org_id == store.org_id


def test_update_refuses_the_org_column_even_when_it_is_the_pinned_one(
    db, store, org, actor
):
    """Refused outright, not "refused when it disagrees": a bulk column write is
    never the way an organization pointer changes, and a guard that only fires
    on the wrong value teaches the wrong rule."""
    ScopedThing.objects.create(store=store, name="mine", created_by=actor)

    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_store(store).update(org_id=org.pk)


def test_update_still_works_on_ordinary_columns(db, store, actor):
    """The positive control the refusals above need."""
    ScopedThing.objects.create(store=store, name="mine", created_by=actor)

    assert ScopedThing.objects.for_store(store).update(name="renamed") == 1


def test_a_create_that_names_another_organization_is_refused(db, store, other_org):
    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_store(store).create(name="x", org_id=other_org.pk)


def test_get_or_create_refuses_another_organization(db, store, other_org):
    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_store(store).get_or_create(
            name="x", defaults={"org_id": other_org.pk}
        )


def test_a_bogus_org_value_is_refused_by_type_not_by_the_database(db, store):
    with pytest.raises(TypeError):
        ScopedThing.objects.for_store(store).create(name="x", org_id="1 OR 1=1")


# --------------------------------------------------------------------------
# Derivation: `org` is never asked for
# --------------------------------------------------------------------------


def test_create_through_a_pin_stamps_the_organization(
    db, store, actor, django_assert_num_queries
):
    """The production hot path, and it costs one statement: the INSERT.

    `for_store()` pins from a `Store` instance (no query), the pin carries the
    organization, and `_org_for_write` stamps it - so `_derive_org` finds the
    value already there and reads `orgs_store` not at all.
    """
    pinned = ScopedThing.objects.for_store(store)

    with django_assert_num_queries(1):
        row = pinned.create(name="x", created_by=actor)

    assert row.org_id == store.org_id
    assert ScopedThing.all_objects.get(pk=row.pk).org_id == store.org_id


def test_create_with_an_explicit_store_derives_the_organization(db, store, actor):
    row = ScopedThing.objects.create(store=store, name="x", created_by=actor)

    assert row.org_id == store.org_id


def test_create_from_a_bare_store_id_derives_the_organization(db, store, actor):
    """The path with no cached instance and no pin: one lookup, and it happens."""
    row = ScopedThing.objects.create(store_id=store.pk, name="x", created_by=actor)

    assert row.org_id == store.org_id


def test_deriving_the_organization_costs_no_query_when_the_store_is_cached(
    db, store, actor, django_assert_num_queries
):
    """`Model(store=<store>)` caches the instance, and the instance carries
    `org_id`, so the derivation is free on every ordinary create."""
    row = ScopedThing(store=store, name="x", created_by=actor)

    with django_assert_num_queries(1):  # the INSERT, and nothing else
        row.save()


def test_bulk_create_stamps_the_organization_on_every_row(db, store, other_store):
    rows = ScopedThing.objects.for_stores([store, other_store]).bulk_create(
        [
            ScopedThing(store=store, name="a"),
            ScopedThing(store=other_store, name="b"),
        ]
    )

    assert {row.org_id for row in rows} == {store.org_id}
    assert set(
        ScopedThing.all_objects.order_by("name").values_list("name", "org_id")
    ) == {("a", store.org_id), ("b", other_store.org_id)}


def test_bulk_create_derives_the_organization_without_a_pin(db, store):
    rows = ScopedThing.objects.bulk_create([ScopedThing(store=store, name="a")])

    assert rows[0].org_id == store.org_id


def test_full_clean_does_not_report_the_derived_org_as_missing(db, store):
    """Measured on Django 6.1: `clean_fields()` does not skip `editable=False`
    fields, so without the derivation in `clean_fields` this raises
    "This field cannot be null" on a perfectly valid row. The same bug was
    found and fixed once already, on `StoreAccess`."""
    ScopedThing(store=store, name="x").full_clean()


def test_moving_a_row_to_another_organizations_store_is_refused_in_python(
    db, store, foreign_store, actor
):
    """A cached store that disagrees with the row's organization is a tenancy
    violation, and it should say so before the foreign key does."""
    row = ScopedThing.objects.create(store=store, name="x", created_by=actor)

    row.store = foreign_store

    with pytest.raises(CrossStoreReferenceError) as exc:
        row.save()

    assert "derived from" in str(exc.value)


def test_moving_a_row_within_the_same_organization_is_not_refused(
    db, store, other_store, actor
):
    """The positive control: two stores of one organization, which is the
    legitimate case and must not be caught by the guard above."""
    row = ScopedThing.objects.create(store=store, name="x", created_by=actor)

    row.store = other_store
    row.save()

    assert ScopedThing.all_objects.get(pk=row.pk).org_id == store.org_id


def test_a_store_scoped_fk_into_another_store_is_still_refused(
    db, store, other_store, category, actor
):
    """E006/`_assert_related_stores_match` are not subsumed by the org column: a
    row in another *store* of the same organization passes every org check and
    is still a leak."""
    mine = Product.objects.create(
        store=store, category=category, name="p", created_by=actor
    )
    theirs = Product.objects.create(
        store=other_store, category=category, name="q", created_by=actor
    )

    assert mine.org_id == theirs.org_id  # premise: same organization
    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_store(store).update(name="x", store=other_store)


# --------------------------------------------------------------------------
# Why `db_constraint=False` (measured, not stylistic)
# --------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True, databases=["default", "migrator"])
def test_a_row_lock_on_the_organization_does_not_block_a_store_scoped_insert():
    """The measurement behind `db_constraint=False`, run rather than quoted.

    `create_store` enforces the 1-5 store ceiling under a row lock on the
    organization. With a real `org_id -> orgs_organization` key on every
    store-scoped table, each insert would take `FOR KEY SHARE` on that same row
    - so `create_store` would block every sale, every stock movement and every
    payment in the organization for the duration of its transaction, and vice
    versa. Without the key there is no interaction at all.

    `select_for_update()` here is the *strong* lock (FOR UPDATE) on purpose:
    that is the conflict `no_key=True` in `create_store` also avoids, and either
    fix alone works. Both together mean neither can be undone by accident.
    """
    org = Organization.objects.create(name="Locked", slug="locked")
    store = Store.objects.create(org=org, name="Main")

    try:
        with transaction.atomic(using="migrator"):
            locked = (
                Organization.objects.using("migrator")
                .select_for_update()
                .get(pk=org.pk)
            )
            assert locked.pk == org.pk  # premise: the lock really was taken

            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '4s'")
                # This is the assertion: it returns instead of timing out.
                row = ScopedThing.objects.for_store(store).create(name="sale")

            assert row.org_id == org.pk
    except OperationalError as exc:  # pragma: no cover - the failure mode
        pytest.fail(
            f"Inserting a store-scoped row blocked on a lock held against the "
            f"organization row: {exc}. That is what db_constraint=False on "
            f"StoreScopedModel.org exists to prevent."
        )
    finally:
        ScopedThing.all_objects.all().update(name="done")
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM testapp_scopedthing")
            cursor.execute("DELETE FROM orgs_store WHERE id = %s", [store.pk])
            cursor.execute("DELETE FROM orgs_organization WHERE id = %s", [org.pk])


# --------------------------------------------------------------------------
# The merge algebra now reads the pin's organization
# --------------------------------------------------------------------------


def test_a_cross_organization_merge_names_both_organizations(
    db, store, foreign_store
):
    """The refusal that replaced the re-resolution query, and its message."""
    with pytest.raises(CrossStoreReferenceError) as exc:
        list(
            ScopedThing.objects.for_store(store)
            | ScopedThing.objects.for_store(foreign_store)
        )

    message = str(exc.value)
    assert str(store.org_id) in message
    assert str(foreign_store.org_id) in message
    assert "may never span organizations" in message


def test_a_hand_built_store_cannot_pin_a_query_to_an_organization_it_names(
    db, store, foreign_store
):
    """The clause the whole merge algebra rests on, watched refusing.

    `merge_pins` traded a database read for an integer comparison, and it is
    only sound because every pin's organization came *out of the database*.
    `_believable_org_pk` is what makes that true: an instance built in Python
    has `_state.adding is True`, so its `org_id` is not believed and the pin
    falls through to the resolving query, where the database wins.

    The probe is the whole attack in one line: a real foreign primary key
    wearing my organization. Believe it and the two legs below agree on an
    integer, the merge is permitted, and `store_id IN (mine, theirs)` compiles
    with no organization predicate to contradict it - both tenants' rows in one
    result set.

    MEASURED with the clause mutated to `if state is None:` - i.e. any instance
    believed: 219 tests still passed, and the merge returned rows from both
    organizations.
    """
    liar = Store(id=foreign_store.pk, org_id=store.org_id)

    # Premise: the forgery is well-formed - a real, saved primary key, and an
    # `org_id` that is not the one the database holds for it.
    assert liar.pk == foreign_store.pk
    assert liar.org_id == store.org_id != foreign_store.org_id
    assert liar._state.adding is True

    # 1. The resolver answers with the *database's* owner, not the instance's.
    assert resolve_scope([liar]).org_pk == foreign_store.org_id

    # 2. So the merge is refused, and it names the organization the forgery
    #    tried to hide.
    with pytest.raises(CrossStoreReferenceError) as exc:
        list(
            ScopedThing.objects.for_store(store)
            | ScopedThing.objects.for_store(liar)
        )
    assert str(foreign_store.org_id) in str(exc.value)


def test_a_pin_carries_the_organization_it_was_resolved_to(db, store, other_store):
    pinned = ScopedThing.objects.for_stores([store, other_store])

    assert pinned.query.scope_pin.org_pk == store.org_id
    assert set(pinned.query.scope_pin.store_pks) == {store.pk, other_store.pk}
    # The three names the rest of the code and the operator matrix read are
    # views of that one pin, so they cannot disagree with it.
    assert pinned.query.store_scoped is True
    assert pinned.query.org_scope_pk == store.org_id


def test_an_unpinned_query_has_no_pin_at_all(db):
    unpinned = ScopedThing.objects.all()

    assert unpinned.query.scope_pin is None
    assert unpinned.query.store_scoped is False
    assert unpinned.query.org_scope_pk is None
    with pytest.raises(UnscopedQueryError):
        list(unpinned)


def test_the_read_query_carries_no_org_predicate(db, store):
    """Deliberate, and the subtle point of the whole refactor.

    The pin's organization is *derived from the stores the caller named*, so
    `WHERE org_id = <that org>` would be tautological: a store id belonging to
    another organization re-scopes the query to that organization instead of
    returning nothing. A term that looks like a tenant guard without being one
    is worse than no term, so it is not compiled today. The predicate that does
    authorize comes from the tenant context, which does not exist yet.

    When the org-leading composite indexes land, an `org_id` term becomes a
    query-plan requirement - a `(org_id, store_id, ...)` index cannot serve a
    `store_id`-only predicate. Add it then, measured, and labelled as that.
    """
    sql = str(ScopedThing.objects.for_store(store).query)
    where = sql.split(" WHERE ", 1)[1]

    # Premise: the split found a real WHERE clause with the store predicate in
    # it, so "no org predicate" is not an artefact of reading the wrong half.
    assert "store_id" in where
    assert "org_id" not in where
    # The column is still selected - it is a concrete field - which is exactly
    # why this test reads the WHERE clause and not the whole statement.
    assert "org_id" in sql.split(" WHERE ", 1)[0]
