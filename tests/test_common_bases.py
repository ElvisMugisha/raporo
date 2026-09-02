"""Invariant tests for the abstract bases in `common/`.

These are structural tests: each one fails if the corresponding invariant is
weakened, not merely if a helper is renamed.
"""

import pytest
from django.apps import apps
from django.core.exceptions import FieldError, ValidationError
from django.db import models

from apps.orgs.models import Store
from common.managers import (
    CrossStoreReferenceError,
    HardDeleteForbidden,
    UnscopedQueryError,
)
from common.models import StoreScopedModel
from tests.testapp.models import (
    Category,
    Product,
    SaleLine,
    ScopedThing,
    ScopedThingOwnMeta,
    Thing,
)

# --------------------------------------------------------------------------
# Soft delete
# --------------------------------------------------------------------------


def test_soft_delete_hides_from_default_manager(db, actor):
    thing = Thing.objects.create(name="crate", created_by=actor)

    thing.soft_delete(by=actor)

    assert Thing.objects.count() == 0
    assert Thing.all_objects.count() == 1


def test_soft_delete_stamps_who_and_when(db, actor):
    thing = Thing.objects.create(name="crate", created_by=actor)

    thing.soft_delete(by=actor)
    thing.refresh_from_db()

    assert thing.deleted_at is not None
    assert thing.deleted_by == actor
    assert thing.updated_by == actor


def test_soft_delete_is_idempotent(db, actor, other_actor):
    thing = Thing.objects.create(name="crate", created_by=actor)

    assert thing.soft_delete(by=actor) is True
    first_stamp = Thing.all_objects.get(pk=thing.pk).deleted_at

    assert thing.soft_delete(by=other_actor) is False
    row = Thing.all_objects.get(pk=thing.pk)
    assert row.deleted_at == first_stamp
    assert row.deleted_by == actor


def test_soft_delete_requires_an_actor_keyword(db, actor):
    thing = Thing.objects.create(name="crate", created_by=actor)

    with pytest.raises(TypeError):
        thing.soft_delete(actor)


def test_hard_delete_is_forbidden_on_instances(db, actor):
    thing = Thing.objects.create(name="crate", created_by=actor)

    with pytest.raises(HardDeleteForbidden):
        thing.delete()

    assert Thing.all_objects.count() == 1


def test_hard_delete_is_forbidden_on_querysets(db, actor):
    Thing.objects.create(name="crate", created_by=actor)

    with pytest.raises(HardDeleteForbidden):
        Thing.objects.all().delete()

    assert Thing.all_objects.count() == 1


def test_queryset_soft_delete_stamps_every_row(db, actor):
    Thing.objects.create(name="a", created_by=actor)
    Thing.objects.create(name="b", created_by=actor)

    assert Thing.objects.all().soft_delete(by=actor) == 2
    assert Thing.objects.count() == 0
    assert Thing.all_objects.filter(deleted_by=actor).count() == 2


# --------------------------------------------------------------------------
# Audit stamps
# --------------------------------------------------------------------------


def test_audited_model_stamps_timestamps(db, actor):
    thing = Thing.objects.create(name="crate", created_by=actor, updated_by=actor)

    assert thing.created_at is not None
    assert thing.updated_at is not None
    assert thing.created_by == actor
    assert thing.updated_by == actor


def test_audited_actor_fields_are_optional_but_protected(db, actor):
    thing = Thing.objects.create(name="crate")
    thing.full_clean()  # created_by/updated_by must not be "required"

    assert thing.created_by is None
    field = Thing._meta.get_field("created_by")
    assert field.remote_field.on_delete is models.PROTECT


# --------------------------------------------------------------------------
# Invariant #1: store scoping
# --------------------------------------------------------------------------


def test_no_store_scoped_model_carries_its_own_org_pointer(db):
    """Business data reaches its org through Store.org and nowhere else.

    A second path (an `org` column on a store-scoped table) is a second thing to
    keep in step, and the one that will drift.
    """
    offenders = []
    for model in apps.get_models():
        if not issubclass(model, StoreScopedModel) or model._meta.abstract:
            continue
        for field in model._meta.concrete_fields:
            if field.name in {"org", "organization"}:
                offenders.append(f"{model._meta.label}.{field.name}")

    assert offenders == []


def test_store_fk_is_declared_by_the_base_not_by_consumers(db):
    """A store-scoped model cannot forget to carry its store pointer."""
    field = ScopedThing._meta.get_field("store")

    assert field.related_model is Store
    assert field.null is False
    assert field.remote_field.on_delete is models.PROTECT


UNSCOPED_OPERATIONS = {
    "list": lambda qs: list(qs),
    "len": len,
    "bool": bool,
    "count": lambda qs: qs.count(),
    "exists": lambda qs: qs.exists(),
    "first": lambda qs: qs.first(),
    "last": lambda qs: qs.last(),
    "get": lambda qs: qs.get(pk=1),
    "aggregate": lambda qs: qs.aggregate(models.Count("pk")),
    "iterator": lambda qs: list(qs.iterator()),
    "values_list": lambda qs: list(qs.values_list("pk", flat=True)),
    "chained_filter": lambda qs: list(qs.filter(name="x")),
    "in_bulk": lambda qs: qs.in_bulk([1]),
    "explain": lambda qs: qs.explain(),
    "update": lambda qs: qs.update(name="x"),
}


@pytest.mark.parametrize("operation", UNSCOPED_OPERATIONS.values(), ids=UNSCOPED_OPERATIONS)
def test_unscoped_query_raises(db, operation):
    with pytest.raises(UnscopedQueryError):
        operation(ScopedThing.objects.all())


def test_unscoped_query_raises_on_the_manager_itself(db):
    with pytest.raises(UnscopedQueryError):
        list(ScopedThing.objects.filter(name="x"))


def test_unscoped_subquery_raises(db, store):
    """The leak vector a `_fetch_all`-only guard misses."""
    with pytest.raises(UnscopedQueryError):
        list(Store.objects.filter(pk__in=ScopedThing.objects.values("store_id")))


def test_raw_sql_is_refused_on_the_scoped_manager(db, store):
    with pytest.raises(UnscopedQueryError):
        list(ScopedThing.objects.raw("select * from testapp_scopedthing"))

    # The deliberate escape hatch still works.
    assert list(ScopedThing.all_objects.raw("select * from testapp_scopedthing")) == []


def test_own_meta_child_is_still_guarded(db):
    with pytest.raises(UnscopedQueryError):
        list(ScopedThingOwnMeta.objects.all())


def test_for_store_returns_only_that_store(db, store, other_store):
    mine = ScopedThing.objects.create(store=store, name="mine")
    ScopedThing.objects.create(store=other_store, name="theirs")

    rows = list(ScopedThing.objects.for_store(store))

    assert rows == [mine]


def test_for_store_hides_soft_deleted_rows(db, store, actor):
    live = ScopedThing.objects.create(store=store, name="live")
    gone = ScopedThing.objects.create(store=store, name="gone")
    gone.soft_delete(by=actor)

    assert list(ScopedThing.objects.for_store(store)) == [live]
    assert ScopedThing.all_objects.filter(store=store).count() == 2


def test_for_store_survives_further_chaining(db, store, other_store):
    ScopedThing.objects.create(store=store, name="keep")
    ScopedThing.objects.create(store=store, name="drop")
    ScopedThing.objects.create(store=other_store, name="keep")

    qs = ScopedThing.objects.for_store(store).filter(name="keep").order_by("pk")

    assert qs.count() == 1
    assert list(qs.values_list("name", flat=True)) == ["keep"]


def test_for_store_accepts_a_primary_key(db, store, other_store):
    ScopedThing.objects.create(store=store, name="mine")
    ScopedThing.objects.create(store=other_store, name="theirs")

    assert ScopedThing.objects.for_store(store.pk).count() == 1


@pytest.mark.parametrize("bad", [None, "", 0, [], "not-a-store"])
def test_for_store_rejects_a_missing_or_bogus_store(db, bad):
    with pytest.raises((TypeError, ValueError)):
        ScopedThing.objects.for_store(bad)


def test_for_store_rejects_a_saved_instance_of_another_model(db, org):
    """An Organization has a pk too: taking it would scope by the wrong id."""
    with pytest.raises(TypeError):
        ScopedThing.objects.for_store(org)


def test_for_stores_rejects_a_saved_instance_of_another_model(db, org, store):
    with pytest.raises(TypeError):
        ScopedThing.objects.for_stores([store, org])


def test_for_store_rejects_an_unsaved_store(db, org):
    with pytest.raises(ValueError):
        ScopedThing.objects.for_store(Store(org=org, name="unsaved"))


def test_for_stores_covers_several_stores_and_nothing_else(db, store, other_store, foreign_store):
    ScopedThing.objects.create(store=store, name="a")
    ScopedThing.objects.create(store=other_store, name="b")
    ScopedThing.objects.create(store=foreign_store, name="c")

    rows = ScopedThing.objects.for_stores([store, other_store])

    assert rows.count() == 2
    assert set(rows.values_list("name", flat=True)) == {"a", "b"}


def test_for_stores_rejects_an_empty_collection(db):
    with pytest.raises(ValueError):
        ScopedThing.objects.for_stores([])


def test_scoped_update_is_allowed_and_stays_scoped(db, store, other_store):
    ScopedThing.objects.create(store=store, name="a")
    ScopedThing.objects.create(store=other_store, name="a")

    assert ScopedThing.objects.for_store(store).update(name="b") == 1
    assert ScopedThing.objects.for_store(other_store).get().name == "a"


def test_scoped_queryset_still_refuses_hard_delete(db, store):
    ScopedThing.objects.create(store=store, name="a")

    with pytest.raises(HardDeleteForbidden):
        ScopedThing.objects.for_store(store).delete()


def test_creating_a_row_needs_a_store_named_or_pinned(db, store):
    thing = ScopedThing.objects.create(store=store, name="explicit")

    assert ScopedThing.all_objects.get(pk=thing.pk).store == store


def test_creating_a_row_with_no_store_at_all_is_refused(db):
    with pytest.raises(UnscopedQueryError):
        ScopedThing.objects.create(name="homeless")


def test_for_store_fills_in_the_store_on_create(db, store):
    thing = ScopedThing.objects.for_store(store).create(name="pinned")

    assert thing.store_id == store.pk


def test_for_store_refuses_to_create_in_another_store(db, store, other_store):
    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_store(store).create(store=other_store, name="smuggled")

    assert ScopedThing.all_objects.count() == 0


def test_for_store_refuses_to_bulk_create_in_another_store(db, store, other_store):
    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_store(store).bulk_create(
            [ScopedThing(store=other_store, name="smuggled")]
        )

    assert ScopedThing.all_objects.count() == 0


def test_bulk_create_fills_in_the_pinned_store(db, store):
    ScopedThing.objects.for_store(store).bulk_create([ScopedThing(name="a"), ScopedThing(name="b")])

    assert ScopedThing.objects.for_store(store).count() == 2


def test_update_cannot_move_a_row_to_another_store(db, store, other_store):
    ScopedThing.objects.create(store=store, name="a")

    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_store(store).update(store=other_store)

    assert ScopedThing.all_objects.get().store_id == store.pk


# --------------------------------------------------------------------------
# A1 (fix round 2) - the update path enforces the same-store FK invariant too.
# Each probe below is a leak that succeeded before the fix.
# --------------------------------------------------------------------------


def test_update_cannot_repoint_a_foreign_key_into_another_store(
    db, store, other_store, sale, product, category
):
    """`for_store(A).filter(...).update(product_id=<a store-B product>)` used to
    return 1 and leave the row reading the foreign product."""
    line = SaleLine.objects.create(store=store, sale=sale, product=product)
    product_b = Product.objects.create(store=other_store, category=category, name="rival")

    with pytest.raises(CrossStoreReferenceError):
        SaleLine.objects.for_store(store).filter(pk=line.pk).update(
            product_id=product_b.pk
        )

    assert SaleLine.all_objects.get(pk=line.pk).product_id == product.pk


def test_a_multi_store_pin_refuses_to_update_a_store_scoped_foreign_key(
    db, store, other_store, sale, product, category
):
    """A2 (fix round 3): membership in the pinned *set* is not the invariant.

    `save()` enforces that a row's FK targets live in *that row's own* store. A
    multi-store pin cannot know each row's store, so a single value can never be
    proven correct for every row it would hit - `for_stores([A, A2]).update(
    product_id=<an A2 product>)` used to update a store-A row to point at store
    A2's catalogue. Same organization, so not a tenant breach; still the same
    broken invariant, and store A's reports then count store A2's product.
    """
    line = SaleLine.objects.create(store=store, sale=sale, product=product)
    product_b = Product.objects.create(store=other_store, category=category, name="theirs")

    with pytest.raises(CrossStoreReferenceError):
        SaleLine.objects.for_stores([store, other_store]).filter(pk=line.pk).update(
            product_id=product_b.pk
        )

    assert SaleLine.all_objects.get(pk=line.pk).product_id == product.pk


def test_a_multi_store_pin_refuses_even_an_in_scope_foreign_key(
    db, store, other_store, sale, product
):
    """Refused even when the target is the row's own store: the pin cannot prove
    it, and approximating is what produced the leak above. Narrow the pin."""
    line = SaleLine.objects.create(store=store, sale=sale, product=product)

    with pytest.raises(CrossStoreReferenceError):
        SaleLine.objects.for_stores([store, other_store]).filter(pk=line.pk).update(
            product_id=product.pk
        )


def test_a_single_store_pin_still_updates_a_foreign_key_in_its_own_store(
    db, store, sale, product, category
):
    """The refusal above is scoped to multi-store pins; the single-store path
    keeps working, or the fix would just be a ban."""
    line = SaleLine.objects.create(store=store, sale=sale, product=product)
    other = Product.objects.create(store=store, category=category, name="second")

    updated = SaleLine.objects.for_store(store).filter(pk=line.pk).update(product=other)

    assert updated == 1
    assert SaleLine.all_objects.get(pk=line.pk).product_id == other.pk


def test_update_refuses_to_reparent_a_row_even_within_scope(db, store, other_store):
    """Re-parenting is a service operation (it must move children too), so a bulk
    `update(store_id=...)` is refused even when the target store is in scope."""
    thing = ScopedThing.objects.create(store=store, name="a")

    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_stores([store, other_store]).update(
            store_id=other_store.pk
        )

    assert ScopedThing.all_objects.get(pk=thing.pk).store_id == store.pk


def test_save_with_update_fields_store_revalidates_every_foreign_key(
    db, store, other_store, sale, product
):
    """`save(update_fields=["store"])` used to validate nothing: the field
    narrowing skipped every FK not named. Changing `store` must re-check all."""
    line = SaleLine.objects.create(store=store, sale=sale, product=product)
    line.store = other_store  # `sale` and `product` still live in `store`

    with pytest.raises(CrossStoreReferenceError):
        line.save(update_fields=["store"])

    assert SaleLine.all_objects.get(pk=line.pk).store_id == store.pk


def test_get_or_create_cannot_reach_into_another_store(db, store, other_store):
    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_store(store).get_or_create(
            name="a", defaults={"store": other_store}
        )


def test_get_or_create_uses_the_pinned_store(db, store):
    thing, created = ScopedThing.objects.for_store(store).get_or_create(name="a")

    assert created and thing.store_id == store.pk
    assert ScopedThing.objects.for_store(store).get_or_create(name="a")[1] is False


def test_all_objects_is_the_documented_escape_hatch(db, store, other_store, actor):
    a = ScopedThing.objects.create(store=store, name="a")
    ScopedThing.objects.create(store=other_store, name="b")
    a.soft_delete(by=actor)

    assert ScopedThing.all_objects.count() == 2


def test_django_internals_use_an_unguarded_default_manager(db, store):
    """Unique checks / admin / forms go through `_default_manager`; if it were
    the guarded manager, `full_clean()` on any store-scoped model would blow up.
    """
    assert ScopedThing._default_manager.name == "all_objects"
    assert ScopedThingOwnMeta._default_manager.name == "all_objects"

    ScopedThing(store=store, name="a").full_clean()


def test_store_scoped_models_have_no_reverse_accessor_from_store(db, store):
    """One way in: `Model.objects.for_store(store)`."""
    assert not hasattr(store, "scopedthing_set")
    assert not hasattr(store, "testapp_scopedthing_set")


def test_validation_error_is_not_how_scope_violations_surface(db):
    """UnscopedQueryError is a programming error, not a user-facing one."""
    assert not issubclass(UnscopedQueryError, ValidationError)


# --------------------------------------------------------------------------
# A1 - no traversable relation reaches store-scoped rows
# --------------------------------------------------------------------------


def test_an_org_level_parent_has_no_accessor_to_its_store_scoped_children(
    db, category, product, foreign_product
):
    """`category.products.all()` returned two orgs' rows; it must not exist."""
    assert not hasattr(category, "products")
    assert not hasattr(category, "product_set")


def test_a_store_scoped_parent_has_no_accessor_to_its_children(db, sale):
    assert not hasattr(sale, "lines")
    assert not hasattr(sale, "saleline_set")


def test_children_are_read_through_for_store(db, store, sale, product):
    SaleLine.objects.create(store=store, sale=sale, product=product, quantity=2)

    lines = SaleLine.objects.for_store(store)

    assert lines.count() == 1
    assert lines.get().sale_id == sale.pk


# --------------------------------------------------------------------------
# A2 - a foreign key may not cross stores
# --------------------------------------------------------------------------


def test_a_cross_store_foreign_key_is_refused_on_create(
    db, store, other_store, sale, category
):
    """Reproduced IDOR: a line in store B pointing at a sale in store A."""
    product_b = Product.objects.create(store=other_store, category=category, name="p")

    with pytest.raises(CrossStoreReferenceError):
        SaleLine.objects.create(store=other_store, sale=sale, product=product_b)

    assert SaleLine.all_objects.count() == 0


def test_a_cross_store_foreign_key_is_refused_on_a_plain_save(
    db, store, other_store, sale, category
):
    product_b = Product.objects.create(store=other_store, category=category, name="p")
    line = SaleLine(store=other_store, sale=sale, product=product_b)

    with pytest.raises(CrossStoreReferenceError):
        line.save()


def test_a_cross_store_foreign_key_is_refused_in_bulk_create(
    db, store, other_store, sale, category
):
    product_b = Product.objects.create(store=other_store, category=category, name="p")

    with pytest.raises(CrossStoreReferenceError):
        SaleLine.objects.for_store(other_store).bulk_create(
            [SaleLine(sale=sale, product=product_b)]
        )


def test_a_cross_store_foreign_key_is_refused_when_only_the_id_is_given(
    db, store, other_store, sale, category
):
    """The form case: a raw `sale_id` from the request body."""
    product_b = Product.objects.create(store=other_store, category=category, name="p")

    with pytest.raises(CrossStoreReferenceError):
        SaleLine.objects.create(
            store=other_store, sale_id=sale.pk, product_id=product_b.pk
        )


def test_same_store_foreign_keys_are_fine(db, store, sale, product):
    line = SaleLine.objects.create(store=store, sale=sale, product=product)

    assert line.pk is not None


def test_a_partial_save_skips_the_unrelated_relation_check(db, store, sale, product, actor):
    line = SaleLine.objects.create(store=store, sale=sale, product=product)

    assert line.soft_delete(by=actor) is True


# --------------------------------------------------------------------------
# A3 - a scoped query cannot walk out through a join
# --------------------------------------------------------------------------


def test_a_scoped_query_cannot_join_back_out_to_other_tenants(
    db, store, category, product, foreign_product
):
    """Reproduced: this returned ['my-secret-product', 'RIVAL-SECRET-PRODUCT']."""
    with pytest.raises(FieldError):
        list(
            Product.objects.for_store(store).values_list(
                "category__products__name", flat=True
            )
        )


def test_the_join_existence_oracle_is_gone(db, store, category, product, foreign_product):
    """Reproduced: `.exists()` on a rival's product name returned True."""
    with pytest.raises(FieldError):
        Product.objects.for_store(store).filter(
            category__products__name="RIVAL-SECRET-PRODUCT"
        ).exists()


def test_the_aggregate_shape_of_the_same_leak_is_gone(db, store, category, product):
    with pytest.raises(FieldError):
        Product.objects.for_store(store).aggregate(
            n=models.Count("category__products")
        )


def test_a_scoped_query_reads_its_own_store_only(db, store, product, foreign_product):
    assert list(Product.objects.for_store(store).values_list("name", flat=True)) == [
        "my-secret-product"
    ]


# --------------------------------------------------------------------------
# B4 - `all_objects` sees everything and deletes nothing
# --------------------------------------------------------------------------


def test_all_objects_cannot_hard_delete(db, actor, store):
    Thing.objects.create(name="a", created_by=actor)
    ScopedThing.objects.create(store=store, name="a")

    with pytest.raises(HardDeleteForbidden):
        Thing.all_objects.all().delete()
    with pytest.raises(HardDeleteForbidden):
        ScopedThing.all_objects.all().delete()

    assert Thing.all_objects.count() == 1
    assert ScopedThing.all_objects.count() == 1


def test_the_base_manager_cannot_hard_delete_either(db, actor, store):
    """`Model._base_manager` is the path Django itself takes."""
    Thing.objects.create(name="a", created_by=actor)

    assert Thing._base_manager.name == "all_objects"
    with pytest.raises(HardDeleteForbidden):
        Thing._base_manager.filter(name="a").delete()
    with pytest.raises(HardDeleteForbidden):
        ScopedThing._base_manager.all().delete()

    assert Thing.all_objects.count() == 1


def test_all_objects_still_sees_retired_rows(db, actor):
    thing = Thing.objects.create(name="a", created_by=actor)
    thing.soft_delete(by=actor)

    assert Thing.objects.count() == 0
    assert Thing.all_objects.count() == 1


# --------------------------------------------------------------------------
# D1 - the guard is closed under set operators
# --------------------------------------------------------------------------


def test_or_with_an_unscoped_queryset_is_refused(db, store, other_store):
    ScopedThing.objects.create(store=store, name="mine")
    ScopedThing.objects.create(store=other_store, name="theirs")

    with pytest.raises(UnscopedQueryError):
        list(ScopedThing.objects.for_store(store) | ScopedThing.all_objects.all())


def test_and_with_an_unscoped_queryset_is_refused(db, store):
    with pytest.raises(UnscopedQueryError):
        list(ScopedThing.objects.for_store(store) & ScopedThing.all_objects.all())


def test_or_of_two_scoped_querysets_stays_scoped(db, store, other_store, foreign_store):
    ScopedThing.objects.create(store=store, name="a")
    ScopedThing.objects.create(store=other_store, name="b")
    ScopedThing.objects.create(store=foreign_store, name="c")

    combined = ScopedThing.objects.for_store(store) | ScopedThing.objects.for_store(
        other_store
    )

    assert set(combined.values_list("name", flat=True)) == {"a", "b"}


def test_and_of_two_scoped_querysets_stays_scoped(db, store):
    ScopedThing.objects.create(store=store, name="a")

    combined = ScopedThing.objects.for_store(store) & ScopedThing.objects.for_store(store)

    assert combined.count() == 1


# --------------------------------------------------------------------------
# A1 (fix round 3) - the WHOLE combinator surface, in both operand orders.
#
# Two of these forms leaked across organizations after fix round 2, and the
# forms that already worked were entirely unpinned - which is how `^` shipped
# unguarded in the first place. The matrix is the point: it fails the day
# Django grows another combinator, or the day someone deletes one override.
# --------------------------------------------------------------------------

#: Every way two querysets of the same model can be merged into one.
COMBINATORS = [
    "__or__",
    "__and__",
    "__xor__",
    "__ror__",
    "__rand__",
    "__rxor__",
    "union",
    "intersection",
    "difference",
]


def combine(operator, left, right):
    """Apply `operator` to two querysets and *materialise* the result.

    Materialising is not incidental. A combined queryset is lazy, and the
    unscoped side has no compile-time guard to fall back on, so a refusal that
    only happens at build time and a leak that only happens at fetch time look
    identical until something iterates.
    """
    return sorted(row.name for row in getattr(left, operator)(right))


@pytest.fixture
def one_row_per_store(db, store, other_store, foreign_store):
    """`mine` and `mine2` are two stores of one org; `RIVAL` is another org's."""
    ScopedThing.objects.create(store=store, name="mine")
    ScopedThing.objects.create(store=other_store, name="mine2")
    ScopedThing.objects.create(store=foreign_store, name="RIVAL")


@pytest.mark.parametrize("operator", COMBINATORS)
def test_a_combinator_refuses_an_unscoped_right_hand_side(operator, one_row_per_store, store):
    with pytest.raises(UnscopedQueryError):
        combine(operator, ScopedThing.objects.for_store(store), ScopedThing.all_objects.all())


@pytest.mark.parametrize("operator", COMBINATORS)
def test_a_combinator_refuses_an_unscoped_left_hand_side(operator, one_row_per_store, store):
    """Operand order decides whose method Python calls, so the refusal has to
    exist on the unscoped side too - `all_objects.all() ^ for_store(A)` returned
    every other organization's rows."""
    with pytest.raises(UnscopedQueryError):
        combine(operator, ScopedThing.all_objects.all(), ScopedThing.objects.for_store(store))


@pytest.mark.parametrize("operator", COMBINATORS)
def test_a_combinator_refuses_a_cross_organization_merge(
    operator, one_row_per_store, store, foreign_store
):
    """`for_stores([A, RIVAL])` is refused; `for_store(A) | for_store(RIVAL)` is
    its synonym and must be refused by the same resolver. That shape is the IDOR:
    a store id taken off a request, combined with the caller's own."""
    with pytest.raises(CrossStoreReferenceError):
        combine(
            operator,
            ScopedThing.objects.for_store(store),
            ScopedThing.objects.for_store(foreign_store),
        )


@pytest.mark.parametrize("operator", COMBINATORS)
def test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order(
    operator, one_row_per_store, store, foreign_store
):
    with pytest.raises(CrossStoreReferenceError):
        combine(
            operator,
            ScopedThing.objects.for_store(foreign_store),
            ScopedThing.objects.for_store(store),
        )


@pytest.mark.parametrize("operator", COMBINATORS)
def test_a_combinator_allows_two_stores_of_one_organization(
    operator, one_row_per_store, store, other_store
):
    """The legitimate case still works - and returns nothing from the other org.

    Without this leg the matrix could be satisfied by refusing everything.
    """
    names = combine(
        operator,
        ScopedThing.objects.for_store(store),
        ScopedThing.objects.for_store(other_store),
    )

    assert set(names) <= {"mine", "mine2"}


@pytest.mark.parametrize("operator", ["__or__", "__xor__", "union"])
def test_a_merged_queryset_stays_pinned_to_both_stores(
    operator, one_row_per_store, store, other_store
):
    """The merge is not just allowed, it is *re-pinned*: the result carries the
    resolved store set, so a further combination is resolved against it too."""
    merged = getattr(ScopedThing.objects.for_store(store), operator)(
        ScopedThing.objects.for_store(other_store)
    )

    assert merged.query.store_scoped is True
    assert set(merged.query.store_scope_pks) == {store.pk, other_store.pk}


def test_the_combinator_guard_sits_on_the_query_seam_not_on_a_list_of_names():
    """`|`, `&` and `^` all funnel through `sql.Query.combine`; `union()`,
    `intersection()` and `difference()` all funnel through
    `QuerySet._combinator_query`. Guarding those two seams is what makes a
    combinator Django adds later covered by construction, so both must stay
    overridden - and both must still exist upstream."""
    from django.db.models.query import QuerySet
    from django.db.models.sql import Query

    from common.managers import GuardedQuery, ScopedQuery, ScopedQuerySet

    assert hasattr(Query, "combine")
    assert hasattr(QuerySet, "_combinator_query")
    assert "combine" in GuardedQuery.__dict__
    assert "combine" in ScopedQuery.__dict__
    assert "_combinator_query" in ScopedQuerySet.__dict__


# --------------------------------------------------------------------------
# A2 (fix round 4) - the shapes the A1 matrix could not see.
#
# A1 covers two whole querysets combined. These cover the three ways a leg
# stops being a whole queryset: it was *sliced* (Django rebuilds it through the
# base manager), it was *never pinned* (the merged pin set is empty), or it was
# *empty* (Django returns the other leg without merging anything at all). Each
# was measured leaking or raising the wrong type before this round.
# --------------------------------------------------------------------------

#: The operators that merge into one WHERE clause. `union()` / `intersection()`
#: / `difference()` are excluded here on purpose: `_combinator_query` calls
#: `clear_limits()`, so they never see a slice in the first place.
MERGING_OPERATORS = ["__or__", "__and__", "__xor__", "__ror__", "__rand__", "__rxor__"]

#: The subset Django rewrites through `model._base_manager` when an operand is
#: sliced. `&` merges a sliced right operand as an ordinary WHERE clause (with
#: its limit dropped) and raises `TypeError` for a sliced left one.
REWRITING_OPERATORS = ["__or__", "__xor__", "__ror__", "__rxor__"]


@pytest.mark.parametrize("operator", MERGING_OPERATORS)
def test_a_sliced_operand_cannot_smuggle_an_unpinned_query_into_a_merge(
    operator, one_row_per_store, store, foreign_store
):
    """The IDOR shape again, one slice further on.

    `QuerySet.__or__` cannot put a slice in a WHERE clause, so it replaces the
    sliced operand with `model._base_manager.filter(pk__in=<the slice>)` -
    `all_objects`, unpinned. The queryset-level guard has already run and passed
    (both operands *were* pinned), so what it hands `Query.combine` is a pinned
    query and an unpinned one. Deleting `refuse_scope_mismatch` from
    `GuardedQuery.combine` made this return `['RIVAL', 'mine']`.
    """
    with pytest.raises(UnscopedQueryError):
        combine(
            operator,
            ScopedThing.objects.for_store(store)[:2],
            ScopedThing.objects.for_store(foreign_store),
        )


@pytest.mark.parametrize("operator", MERGING_OPERATORS)
def test_a_sliced_operand_is_refused_in_the_other_position_too(
    operator, one_row_per_store, store, foreign_store
):
    with pytest.raises(UnscopedQueryError):
        combine(
            operator,
            ScopedThing.objects.for_store(store),
            ScopedThing.objects.for_store(foreign_store)[:2],
        )


@pytest.mark.parametrize("operator", REWRITING_OPERATORS)
@pytest.mark.parametrize("position", ["left", "right"])
def test_the_refusal_of_a_sliced_merge_names_the_slice_not_a_missing_pin(
    operator, position, one_row_per_store, store
):
    """Both sides pinned, to the same store, and it is still refused - so the
    message has to say what actually happened.

    The unpinned operand is one Django constructed, not one the author wrote, so
    "pin every side with for_store()" sends them looking for a bug that is not
    there. Refusing is right: the base-manager rewrite drops `deleted_at IS
    NULL` as well, so tombstones would come back.
    """
    whole = ScopedThing.objects.for_store(store)
    sliced = ScopedThing.objects.for_store(store)[:2]
    left, right = (sliced, whole) if position == "left" else (whole, sliced)

    with pytest.raises(UnscopedQueryError) as exc:
        combine(operator, left, right)

    message = str(exc.value)
    assert "sliced" in message
    assert "Combine first, then slice." in message
    assert "Pin every side" not in message


def test_the_query_seam_still_refuses_a_rewritten_slice_on_its_own(
    one_row_per_store, store, foreign_store
):
    """Defence in depth, and the only way to reach it.

    The test above is satisfied by the queryset-level refusal alone, so it says
    nothing about `GuardedQuery.combine` - the guard that actually stops the
    merge, and the one whose deletion leaked. Django's own `__or__` is therefore
    called directly here, bypassing our override, which is exactly the state a
    future refactor of `NoHardDeleteQuerySet` would leave behind.
    """
    with pytest.raises(UnscopedQueryError):
        list(
            models.QuerySet.__or__(
                ScopedThing.objects.for_store(store)[:2],
                ScopedThing.objects.for_store(foreign_store),
            )
        )


@pytest.mark.parametrize("operator", COMBINATORS)
def test_combining_two_unpinned_querysets_raises_the_documented_error(operator, db):
    """Neither side pinned: still a scope violation, and it must arrive as one.

    `CrossStoreReferenceError.__doc__` promises a single `except
    UnscopedQueryError` covers every invariant-#1 violation, and a service will
    write that `except`. This shape used to surface as `ValueError:
    for_stores() needs at least one store.` - the wrong type, naming a function
    the author never called.
    """
    with pytest.raises(UnscopedQueryError) as exc:
        combine(operator, ScopedThing.objects.all(), ScopedThing.objects.all())

    assert "unpinned" in str(exc.value)


def test_an_empty_leg_does_not_let_union_skip_the_guard(one_row_per_store, store):
    """`union()` drops an `EmptyQuerySet` `self` and, with one leg left, returns
    that leg without building a combined query - so `_combinator_query` never
    ran and `for_store(A).none().union(all_objects.all())` iterated every
    organization's rows.
    """
    with pytest.raises(UnscopedQueryError):
        list(ScopedThing.objects.for_store(store).none().union(ScopedThing.all_objects.all()))


def test_an_empty_leg_does_not_let_union_skip_the_guard_in_the_other_order(
    one_row_per_store, store
):
    with pytest.raises(UnscopedQueryError):
        list(ScopedThing.all_objects.none().union(ScopedThing.objects.for_store(store)))


def test_a_short_circuited_union_returns_the_surviving_leg_unchanged(
    one_row_per_store, store, other_store
):
    """Why the shape above is a seam fix and not a leak fix.

    Django does not merge anything here: it hands back the very queryset object
    the caller passed in, so no query is built and no scope is widened. The
    identity assertion is the point - the day Django starts merging instead,
    this fails and the merge goes through `_combinator_query` like every other.
    """
    surviving = ScopedThing.objects.for_store(other_store)

    result = ScopedThing.objects.for_store(store).none().union(surviving)

    assert result is surviving
    assert sorted(row.name for row in result) == ["mine2"]


# --------------------------------------------------------------------------
# A3 (fix round 4) - what a merged pin costs, and what it means
# --------------------------------------------------------------------------


def test_the_union_override_still_passes_djangos_keyword_through(
    one_row_per_store, store
):
    """`union()` is overridden, so keeping its signature honest is now our job:
    `all=True` is what asks for UNION ALL, and dropping it would silently
    de-duplicate a report."""
    both_legs = ScopedThing.objects.for_store(store).union(
        ScopedThing.objects.for_store(store), all=True
    )

    assert sorted(row.name for row in both_legs) == ["mine", "mine"]


def test_merging_a_pin_with_itself_costs_no_query(
    one_row_per_store, store, django_assert_num_queries
):
    """Building a queryset must not hit the database.

    Every merge re-resolves ownership against `orgs_store`, which is what makes
    `for_store(A) | for_store(RIVAL)` a synonym for the `for_stores([A, RIVAL])`
    that is refused - but a merge of one store set with itself adds no store, so
    there is nothing to resolve and `qs = a | a` stays lazy.
    """
    with django_assert_num_queries(0):
        ScopedThing.objects.for_store(store) | ScopedThing.objects.for_store(store)


def test_widening_a_pin_still_resolves_ownership(
    one_row_per_store, store, other_store, django_assert_num_queries
):
    """The counterpart: a merge that adds a store pays for the check that keeps
    the two stores in one organization."""
    with django_assert_num_queries(1):
        merged = ScopedThing.objects.for_store(store) | ScopedThing.objects.for_store(
            other_store
        )

    assert set(merged.query.store_scope_pks) == {store.pk, other_store.pk}


def test_intersection_narrows_its_pin_exactly_like_and(
    one_row_per_store, store, other_store
):
    """`&` and `intersection()` mean the same thing about stores.

    They used to disagree: the merge took the *connector*, and the two seams
    speak different vocabularies (`sql.AND` against `"intersection"`), so the
    narrowing branch was dead on the combinator path and `intersection()` pinned
    to both stores.
    """
    both = [store, other_store]

    narrowed_by_operator = ScopedThing.objects.for_stores(both) & ScopedThing.objects.for_store(
        other_store
    )
    narrowed_by_combinator = ScopedThing.objects.for_stores(both).intersection(
        ScopedThing.objects.for_store(other_store)
    )

    assert set(narrowed_by_operator.query.store_scope_pks) == {other_store.pk}
    assert set(narrowed_by_combinator.query.store_scope_pks) == {other_store.pk}


def test_union_and_difference_keep_the_wider_pin(one_row_per_store, store, other_store):
    """A pin is an upper bound on where rows can come from, so narrowing is only
    sound when the result is provably inside both legs. A difference's rows come
    from its left leg, which the right leg's pin says nothing about."""
    both = [store, other_store]

    united = ScopedThing.objects.for_stores(both).union(
        ScopedThing.objects.for_store(other_store)
    )
    differenced = ScopedThing.objects.for_stores(both).difference(
        ScopedThing.objects.for_store(other_store)
    )

    assert set(united.query.store_scope_pks) == {store.pk, other_store.pk}
    assert set(differenced.query.store_scope_pks) == {store.pk, other_store.pk}


# --------------------------------------------------------------------------
# D2 - for_stores() may not span organizations
# --------------------------------------------------------------------------


def test_for_stores_refuses_a_mixed_organization_set(db, store, foreign_store):
    with pytest.raises(CrossStoreReferenceError):
        ScopedThing.objects.for_stores([store, foreign_store])


def test_for_stores_refuses_unknown_store_ids(db, store):
    with pytest.raises(ValueError):
        ScopedThing.objects.for_stores([store.pk, store.pk + 10_000])


def test_for_stores_accepts_several_stores_of_one_org(db, store, other_store):
    ScopedThing.objects.create(store=store, name="a")
    ScopedThing.objects.create(store=other_store, name="b")

    assert ScopedThing.objects.for_stores([store, other_store]).count() == 2


# --------------------------------------------------------------------------
# D5 - an unattributable tombstone is refused
# --------------------------------------------------------------------------


def test_soft_delete_refuses_a_missing_actor(db, actor):
    thing = Thing.objects.create(name="a", created_by=actor)

    with pytest.raises(ValueError):
        thing.soft_delete(by=None)

    assert Thing.objects.count() == 1


def test_soft_delete_accepts_a_declared_system_action(db, actor):
    thing = Thing.objects.create(name="a", created_by=actor)

    assert thing.soft_delete(by=None, system=True) is True
    assert Thing.all_objects.get().deleted_by is None


def test_queryset_soft_delete_refuses_a_missing_actor(db, actor):
    Thing.objects.create(name="a", created_by=actor)

    with pytest.raises(ValueError):
        Thing.objects.all().soft_delete(by=None)


# --------------------------------------------------------------------------
# The residual caveat from A1, closed: `+` is not a traversable path
# --------------------------------------------------------------------------


def test_the_hidden_relation_cannot_be_traversed_from_the_parent(
    db, category, product, foreign_product
):
    """`related_name="+"` hides the accessor but leaves the literal `+` query
    name resolvable, which was still an existence oracle."""
    with pytest.raises(UnscopedQueryError):
        Category.objects.filter(**{"+__name": "RIVAL-SECRET-PRODUCT"}).exists()


def test_the_hidden_relation_cannot_be_traversed_from_a_scoped_query(
    db, store, category, product, foreign_product
):
    with pytest.raises(UnscopedQueryError):
        Product.objects.for_store(store).filter(
            **{"category__+__name": "RIVAL-SECRET-PRODUCT"}
        ).exists()


def test_the_hidden_relation_is_refused_on_all_objects_too(db, category):
    with pytest.raises(UnscopedQueryError):
        list(Category.all_objects.filter(**{"+__name": "x"}))
