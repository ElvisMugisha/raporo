"""The surrogate public identifier (ADR 0010).

Every row a user can act on is addressed by a URL in a server-rendered HTMX
app, and a sequential `BigAutoField` in that URL is an enumeration oracle. The
`public_id` column is the only identifier that crosses the process boundary.

Two properties are load-bearing and are the reason most of these tests exist:

* **The value is a real UUID before the row is saved.** The mechanism is a
  Python default (`uuid.uuid7`), not `db_default=UUID7()`. A `db_default` field
  returns a `DatabaseDefault` sentinel from `get_default()`, and of Django
  6.1's uniqueness paths only `Model.clean_fields()` skips that sentinel - so
  `full_clean()` on an unsaved instance would compile `WHERE public_id =
  UUIDV7()`, and a template rendering an unsaved object would put an expression
  object in a DOM id. `test_an_unsaved_row_already_carries_a_real_uuid` is that
  arbitration, pinned.
* **The unique index is global and unconditional** - the inverse of every
  other unique constraint in this codebase, and deliberately so. A soft-deleted
  row keeps its identifier for ever: conditioned on live rows, a tombstone
  would release its `public_id`, a later insert could take it, and a bookmarked
  URL or an audit reference would resolve to a different row. Reissuing an
  identifier is worse than reserving one.
"""

import uuid

import pytest
from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.db.models import BigAutoField, UUIDField

from apps.audit.models import AuditLog
from apps.orgs.models import Membership, Organization, Role, Store, StoreAccess
from common.models import PUBLIC_ID_FIELD, PublicIdModel, StoreScopedModel
from tests.testapp.models import Product

#: Every first-party app whose concrete models must carry the identifier.
#: Django's own `auth`/`contenttypes`/`sessions` tables are excluded by label:
#: they are not ours to reshape and none of them is addressed by a URL here.
FIRST_PARTY_APP_LABELS = {"accounts", "orgs", "audit"}

#: The models that exist today. A premise for the enumerating test below: an
#: enumeration that silently finds nothing must fail, not pass.
EXPECTED_FIRST_PARTY_MODELS = {
    "accounts.User",
    "audit.AuditLog",
    "orgs.Membership",
    "orgs.Organization",
    "orgs.Role",
    "orgs.Store",
    "orgs.StoreAccess",
}


def first_party_models():
    return [
        model
        for model in apps.get_models()
        if model._meta.app_label in FIRST_PARTY_APP_LABELS and not model._meta.abstract
    ]


# --------------------------------------------------------------------------
# Which models carry it
# --------------------------------------------------------------------------


def test_every_first_party_concrete_model_carries_a_public_id():
    """Inheriting the base is the part a new model can forget.

    Enumerated from the registry rather than listed, so a model added in slice
    2 acquires this coverage without anyone remembering - with the label set
    above as the premise, so a discovery bug reads as a failure.
    """
    found = {model._meta.label for model in first_party_models()}
    assert found == EXPECTED_FIRST_PARTY_MODELS  # premise

    missing = sorted(
        model._meta.label
        for model in first_party_models()
        if PUBLIC_ID_FIELD not in {field.name for field in model._meta.concrete_fields}
    )

    assert missing == [], (
        f"These models carry no {PUBLIC_ID_FIELD}: {missing}. Mix in "
        f"common.models.PublicIdModel - every row a URL can name needs one."
    )


def test_the_store_scoped_bases_carry_it_by_inheritance():
    """`SoftDeleteModel` is where it is mixed in, so `StoreScopedModel` and
    every slice-2 business table inherit it rather than declaring it."""
    assert issubclass(StoreScopedModel, PublicIdModel)
    assert issubclass(Product, PublicIdModel)
    assert Product._meta.get_field(PUBLIC_ID_FIELD).unique is True


def test_both_the_organization_and_the_store_carry_one_despite_the_slug():
    """The slug is not an identifier, and this is the assertion that says why.

    `orgs_organization_unique_live_slug` is conditioned on live rows *by
    design* - a soft-deleted organization releases its slug - so it is neither
    stable nor unique over time, and it is mutable and user-chosen. `Store` has
    no slug at all. Both facts are read off the models here rather than
    trusted, because both are the whole argument for the column.
    """
    slug_constraints = [
        constraint
        for constraint in Organization._meta.constraints
        if "slug" in (constraint.fields or ())
    ]
    assert [constraint.condition is not None for constraint in slug_constraints] == [True]
    assert "slug" not in {field.name for field in Store._meta.concrete_fields}

    for model in (Organization, Store):
        assert model._meta.get_field(PUBLIC_ID_FIELD)


def test_the_audit_row_has_its_own_identity():
    """An audit row is linkable from a UI. `target_id` staying a raw internal
    id is fine because it is never a URL; the audit row's own identity is."""
    assert issubclass(AuditLog, PublicIdModel)


# --------------------------------------------------------------------------
# The field's shape
# --------------------------------------------------------------------------


def test_the_field_is_a_non_editable_unique_uuid_with_a_python_default():
    field = PublicIdModel._meta.get_field(PUBLIC_ID_FIELD)

    assert isinstance(field, UUIDField)
    assert field.unique is True
    assert field.editable is False
    assert field.null is False
    assert field.default is uuid.uuid7
    assert field.has_db_default() is False


def test_the_field_gets_no_index_beyond_its_unique_constraint():
    """On PostgreSQL `unique=True` *is* a unique B-tree index, and it is the
    index the URL lookup uses. A second index would be pure write cost on every
    insert into every table in the product."""
    assert PublicIdModel._meta.get_field(PUBLIC_ID_FIELD).db_index is False


def test_the_identifier_is_not_the_primary_key():
    """A UUID primary key would double the width of every foreign key and of
    every `(id, org_id)` composite-FK target index, for a URL-cosmetics benefit
    a separate column already delivers."""
    for model in (Organization, Store, Role, Membership, StoreAccess, AuditLog, Product):
        assert isinstance(model._meta.pk, BigAutoField), model._meta.label
        assert model._meta.pk.name == "id"


# --------------------------------------------------------------------------
# The value
# --------------------------------------------------------------------------


def test_an_unsaved_row_already_carries_a_real_uuid():
    """The arbitration behind the Python default, as an assertion.

    Under `db_default=UUID7()` this attribute is a `DatabaseDefault`
    expression: `Model.validate_unique()` and `UniqueConstraint.validate()` do
    not skip it, so `full_clean()` would put it in a `WHERE` clause, and a
    template would render it into a DOM id.
    """
    assert isinstance(Organization(name="Unsaved", slug="unsaved").public_id, uuid.UUID)


def test_a_created_row_is_a_version_7_identifier_with_no_extra_query(db):
    org = Organization.objects.create(name="Eva Shop", slug="eva-shop")

    assert org.public_id.version == 7
    assert org.public_id == Organization.objects.get(pk=org.pk).public_id


def test_every_row_gets_its_own(db, actor):
    """Distinct across models and across rows: the identifier is the URL key,
    so a collision is a cross-tenant read."""
    org = Organization.objects.create(name="Eva Shop", slug="eva-shop")
    store = Store.objects.create(org=org, name="Main")
    other = Store.objects.create(org=org, name="Kimironko")

    identifiers = [org.public_id, store.public_id, other.public_id, actor.public_id]

    assert len(set(identifiers)) == len(identifiers)


def test_full_clean_reports_a_duplicate_identifier_against_the_field(db, org):
    """Proof the uniqueness path received a value rather than an expression.

    `editable=False` keeps `public_id` out of every form, so this is not a
    user-facing message - it is the observable difference between a real UUID
    and a `DatabaseDefault` sentinel reaching `validate_unique()`.
    """
    clash = Organization(name="Clash", slug="clash", public_id=org.public_id)

    with pytest.raises(Exception) as exc:
        clash.full_clean()

    assert PUBLIC_ID_FIELD in exc.value.error_dict


# --------------------------------------------------------------------------
# Immutable, never reissued
# --------------------------------------------------------------------------


def test_a_soft_deleted_row_keeps_its_identifier(db, actor, org):
    before = org.public_id
    org.soft_delete(by=actor)
    org.refresh_from_db()

    assert org.public_id == before


def test_a_tombstone_still_reserves_its_identifier(db, actor, org):
    """The inverse of every other unique constraint here, and the reason the
    identifier's index is unconditional: a bookmarked URL must never resolve to
    a different row than the one it named."""
    org.soft_delete(by=actor)

    with pytest.raises(IntegrityError), transaction.atomic():
        Organization.objects.create(name="Successor", slug="successor", public_id=org.public_id)


# --------------------------------------------------------------------------
# What PostgreSQL actually built
# --------------------------------------------------------------------------

INDEXES_ON_PUBLIC_ID = """
SELECT i.relname, x.indisunique, x.indpred IS NOT NULL AS partial
FROM pg_index x
JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
WHERE t.relname = %s
  AND (SELECT array_agg(a.attname::text ORDER BY a.attnum)
       FROM pg_attribute a
       WHERE a.attrelid = t.oid AND a.attnum = ANY (x.indkey)) = ARRAY[%s]
"""


def test_each_first_party_table_has_exactly_one_unconditional_unique_index_on_it(db):
    """Read out of PostgreSQL, not out of `Meta`.

    Three separate claims in one query, because all three are decisions rather
    than accidents: the index exists (so the URL lookup is an index scan), it
    is *unique* (so the identifier is one), and it is not *partial* (so a
    tombstone keeps its value). `exactly one` is the fourth: `db_index=True`
    alongside `unique=True` is the redundant write cost §C.3 measured away.
    """
    tables = sorted(model._meta.db_table for model in first_party_models())
    assert len(tables) == len(EXPECTED_FIRST_PARTY_MODELS)  # premise

    found = {}
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(INDEXES_ON_PUBLIC_ID, [table, PUBLIC_ID_FIELD])
            found[table] = cursor.fetchall()

    for table, rows in found.items():
        assert len(rows) == 1, f"{table}: {rows}"
        name, is_unique, is_partial = rows[0]
        assert is_unique is True, f"{table}.{name}"
        assert is_partial is False, f"{table}.{name}"


def test_the_column_is_a_not_null_uuid_in_postgres(db):
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname, a.atttypid::regtype::text, a.attnotnull
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            WHERE a.attname = %s AND c.relkind = 'r' AND c.relname = ANY(%s)
            ORDER BY c.relname
            """,
            [PUBLIC_ID_FIELD, sorted(model._meta.db_table for model in first_party_models())],
        )
        rows = cursor.fetchall()

    assert len(rows) == len(EXPECTED_FIRST_PARTY_MODELS)  # premise
    assert {(type_name, not_null) for _table, type_name, not_null in rows} == {("uuid", True)}
