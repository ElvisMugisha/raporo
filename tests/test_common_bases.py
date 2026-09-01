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
