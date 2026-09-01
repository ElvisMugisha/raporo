"""Query-layer enforcement of the tenancy and deletion invariants.

1. **Store scoping.** A store-scoped model can only be read, updated or created
   through `for_store()` / `for_stores()`. Anything else raises
   `UnscopedQueryError`, and a write that names a different store than the one
   pinned raises `CrossStoreReferenceError`.
2. **No hard deletes.** `delete()` is refused on instances and on every
   queryset - including `all_objects`, which exists to *see* retired rows, not
   to remove them. `soft_delete(by=...)` stamps the row instead.

The scope guard sits on the SQL `Query`, not on `QuerySet._fetch_all`. Every
read - `count()`, `exists()`, `aggregate()`, `iterator()`, `values_list()`,
`explain()`, and crucially a queryset used as a *subquery* inside someone
else's query - has to build a compiler, so one hook covers paths a
`_fetch_all` override silently misses. Writes are guarded on the queryset
because Django swaps the query class (`UpdateQuery`) or skips it entirely
(`create`, `bulk_create`).

Combining two querysets is guarded the same way - at the seam, not on a list of
method names. `|`, `&` and `^` all reach `sql.Query.combine`; `union()`,
`intersection()` and `difference()` all reach `QuerySet._combinator_query`. Both
are overridden, so an operator Django adds later is covered by construction:
enumerating dunders is exactly how `^` shipped unguarded, and how
`for_store(A) | for_store(RIVAL)` stayed a synonym for the `for_stores([A,
RIVAL])` that `_store_pks()` already refused.

The messages here are for developers, not users: reaching them means a bug in
our code, never bad input from a request. They stay untranslated on purpose.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.apps import apps as global_apps
from django.db import models
from django.db.models.sql import Query
from django.db.models.sql.where import AND
from django.utils import timezone

STORE_LABEL = "orgs.store"
STORE_FIELD = "store"


class UnscopedQueryError(Exception):
    """Raised when a store-scoped model is used without pinning a store."""


class CrossStoreReferenceError(UnscopedQueryError):
    """Raised when a row would reference, or land in, a different store.

    A subclass of `UnscopedQueryError` so a single `except` covers every
    invariant-#1 violation.
    """


class HardDeleteForbidden(NotImplementedError):
    """Raised on any attempt to remove a row for good.

    Subclasses `NotImplementedError` so `except NotImplementedError` (what the
    plan's sketch promised) keeps working.
    """


def _store_pk(store) -> int:
    """Normalise a Store instance / primary key, refusing anything else.

    Accepting `None` here would produce `WHERE store_id IS NULL` - a query that
    looks scoped and returns nothing, which is exactly how scoping bugs hide.
    """
    if store is None:
        raise TypeError("for_store() needs a Store instance or primary key, got None.")
    if isinstance(store, models.Model):
        if store._meta.label_lower != STORE_LABEL:
            raise TypeError(
                f"for_store() needs a {STORE_LABEL} instance, "
                f"got {store._meta.label_lower}."
            )
        if store.pk is None:
            raise ValueError("for_store() needs a saved Store; this one has no primary key.")
        return store.pk
    if isinstance(store, bool) or not isinstance(store, int):
        raise TypeError(
            f"for_store() needs a Store instance or integer primary key, got {store!r}."
        )
    if store <= 0:
        raise ValueError(f"for_store() needs a positive primary key, got {store!r}.")
    return store


def _store_pks(stores) -> list[int]:
    """Normalise a collection of stores and refuse a mixed-organization set.

    Reporting across "my stores" is legitimate; reporting across two orgs'
    stores never is, and the caller usually cannot tell the difference by
    looking at a list of ids - so this resolves them.
    """
    if isinstance(stores, (str, bytes)) or not isinstance(stores, Iterable):
        raise TypeError(f"for_stores() needs an iterable of stores, got {stores!r}.")
    seen: dict[int, None] = {}
    for store in stores:
        seen[_store_pk(store)] = None
    if not seen:
        raise ValueError("for_stores() needs at least one store.")
    pks = list(seen)

    store_model = global_apps.get_model(*STORE_LABEL.split("."))
    owners = dict(store_model.all_objects.filter(pk__in=pks).values_list("pk", "org_id"))
    unknown = [pk for pk in pks if pk not in owners]
    if unknown:
        raise ValueError(f"for_stores() was given unknown store ids: {unknown}.")
    orgs = set(owners.values())
    if len(orgs) > 1:
        raise CrossStoreReferenceError(
            f"for_stores() was given stores from {len(orgs)} organizations "
            f"({sorted(orgs)}); a query may never span organizations."
        )
    return pks


def soft_delete_values(model, by) -> dict:
    """Column values that mark a row deleted, including the audit stamps the
    model actually has (`SoftDeleteModel` alone has no `updated_by`)."""
    now = timezone.now()
    values = {"deleted_at": now, "deleted_by": by}
    names = {field.name for field in model._meta.fields}
    if "updated_at" in names:
        values["updated_at"] = now
    if "updated_by" in names:
        values["updated_by"] = by
    return values


def require_actor(by, system: bool):
    """An unattributable tombstone is not an option: either a user did it, or
    the caller says out loud that the system did."""
    if by is None and not system:
        raise ValueError(
            "soft_delete() needs the user who did it: pass by=<user>, or "
            "system=True if this really is a system-initiated deletion."
        )
    return by


def merge_scope_pks(left_pks, right_pks, connector: str) -> tuple[int, ...]:
    """The store set a combined query is pinned to, resolved - never assumed.

    A plain set union is what leaked: `for_stores([A, RIVAL])` is refused by
    `_store_pks()`, so `for_store(A) | for_store(RIVAL)` is its synonym and has
    to go through the same resolver. Every merge therefore re-resolves ownership
    against the database, which also catches a pin whose store was deleted
    between the two legs.

    `AND` narrows to the intersection when there is one (the conjunction can
    only return rows in both); an empty intersection falls back to the resolved
    union, because a pin of no stores would read as "unpinned" downstream.
    """
    resolved = _store_pks(sorted(set(left_pks) | set(right_pks)))
    if connector == AND:
        narrowed = set(left_pks) & set(right_pks)
        if narrowed:
            return tuple(pk for pk in resolved if pk in narrowed)
    return tuple(resolved)


class GuardedQuery(Query):
    """Refuses to resolve the literal `+` query name of a hidden relation.

    `related_name="+"` removes the accessor and the readable query name, but
    Django still resolves the literal `+` as a path segment, so
    `Category.objects.filter(**{"+__name": "RIVAL"})` and
    `Product.objects.for_store(s).filter(**{"category__+__name": ...})` remain
    existence oracles for another tenant's rows *if the lookup key comes from
    user input*. Hidden relations exist precisely so those rows cannot be
    reached by traversal, so the traversal is refused here too.

    Standing rule regardless: never build an ORM lookup key from request data.

    It also carries the scope flags for *both* kinds of query, so the `combine()`
    seam below can compare a scoped query with an unscoped one from either side.
    """

    #: Overwritten per instance by `ScopedQuerySet._pin`.
    store_scoped = False
    store_scope_pks: tuple[int, ...] = ()

    def refuse_scope_mismatch(self, rhs, connector: str) -> None:
        """A merge of a pinned query with an unpinned one produces a single
        WHERE clause with no store predicate at all."""
        if bool(self.store_scoped) != bool(getattr(rhs, "store_scoped", False)):
            pinned, unpinned = (
                ("left", "right") if self.store_scoped else ("right", "left")
            )
            raise UnscopedQueryError(
                f"{self.model.__name__}: `{connector}` combines a store-pinned query "
                f"({pinned}) with an unpinned one ({unpinned}); the combined query "
                f"would carry no store predicate. Pin every side with "
                f"for_store()/for_stores()."
            )

    def combine(self, rhs, connector):
        """`|`, `&` and `^` all funnel through here - guard the seam, not names.

        `QuerySet.__or__` / `__and__` / `__xor__` each build the merged query by
        calling `Query.combine`, so one override covers all three *and* whatever
        combinator a later Django adds, which is the lesson `get_compiler`
        already taught this codebase: enumerating dunders is how `^` shipped
        unguarded.

        Here, on the unscoped base class, the only question is agreement.
        """
        self.refuse_scope_mismatch(rhs, connector)
        return super().combine(rhs, connector)

    def names_to_path(self, names, *args, **kwargs):
        for name in names:
            if isinstance(name, str) and name.endswith("+"):
                raise UnscopedQueryError(
                    f"{self.model.__name__}: {name!r} is a hidden relation's query "
                    f"name. Hidden relations are not traversable; read store-scoped "
                    f"rows through Model.objects.for_store(store)."
                )
        return super().names_to_path(names, *args, **kwargs)


def queryset_scope(queryset) -> tuple[bool, tuple[int, ...]]:
    """(is this queryset pinned to store(s), which ones)."""
    query = getattr(queryset, "query", None)
    return (
        bool(getattr(query, "store_scoped", False)),
        tuple(getattr(query, "store_scope_pks", ()) or ()),
    )


def refuse_scope_mix(left, others, operator: str) -> None:
    """A combined query is one query: every leg must agree about scoping.

    Combining a pinned queryset with an unpinned one produces a result with no
    store predicate at all, and the operand order decides whose `__or__` runs -
    so this check lives on the *unscoped* side too.
    """
    left_scoped, _ = queryset_scope(left)
    for other in others:
        other_scoped, _ = queryset_scope(other)
        if other_scoped != left_scoped:
            pinned, unpinned = ("left", "right") if left_scoped else ("right", "left")
            raise UnscopedQueryError(
                f"{type(left).__name__}: `{operator}` combines a store-pinned queryset "
                f"({pinned}) with an unpinned one ({unpinned}); the combined query would "
                f"carry no store predicate. Pin every side with for_store()/for_stores()."
            )


def guarded_queryset(manager, queryset_class):
    """A queryset of `queryset_class` on a `GuardedQuery`."""
    return queryset_class(
        model=manager.model,
        query=GuardedQuery(manager.model),
        using=manager._db,
        hints=manager._hints,
    )


class ScopedQuery(GuardedQuery):
    """A `Query` that refuses to compile until a store has been pinned."""

    def combine(self, rhs, connector):
        """The scoped side of the same seam: re-resolve the merged pin set.

        Once a mixed merge is refused, both sides are pinned and the remaining
        question is *whose stores*. Both checks run before `super()`, because
        `Query.combine` mutates `self` and a refusal should leave nothing
        half-merged behind.
        """
        self.refuse_scope_mismatch(rhs, connector)
        merged = merge_scope_pks(
            self.store_scope_pks, getattr(rhs, "store_scope_pks", ()), connector
        )
        result = super().combine(rhs, connector)
        self.store_scoped = True
        self.store_scope_pks = merged
        return result

    def get_compiler(self, *args, **kwargs):
        if not self.store_scoped:
            raise UnscopedQueryError(
                f"{self.model.__name__} is store-scoped: query it through "
                f"{self.model.__name__}.objects.for_store(store) "
                f"(or .for_stores([...])). Use .all_objects only for audits and "
                f"data migrations."
            )
        return super().get_compiler(*args, **kwargs)


class NoHardDeleteQuerySet(models.QuerySet):
    """Refuses deletion without hiding retired rows.

    This is what `all_objects` is built from: it must stay a complete,
    unfiltered view (it is also the models' `base_manager`), while still being
    unable to erase anything.
    """

    def delete(self):
        raise HardDeleteForbidden(
            f"{self.model.__name__}: hard delete is forbidden; "
            f"use .soft_delete(by=<user>) instead."
        )

    delete.queryset_only = True

    def _raw_delete(self, using):
        raise HardDeleteForbidden(
            f"{self.model.__name__}: hard delete is forbidden; "
            f"use .soft_delete(by=<user>) instead."
        )

    # `Query.combine` (guarded on GuardedQuery) covers `|`, `&` and `^` once the
    # merged query has been built. These two seams cover what it cannot see:
    #
    #  * the operators short-circuit on an empty queryset *before* calling
    #    `combine`, so a mixed pair could slip past unmerged;
    #  * `union()` / `intersection()` / `difference()` never call `combine` at
    #    all - they hang the legs off `combined_queries` instead, through
    #    `_combinator_query`, which is their own single seam;
    #  * operand order decides which method Python calls, and Django's operators
    #    never return NotImplemented, so a refusal that lived only on the scoped
    #    side would never run for `unscoped OP scoped`.

    def __or__(self, other):
        refuse_scope_mix(self, [other], "|")
        return super().__or__(other)

    def __and__(self, other):
        refuse_scope_mix(self, [other], "&")
        return super().__and__(other)

    def __xor__(self, other):
        refuse_scope_mix(self, [other], "^")
        return super().__xor__(other)

    # Django's QuerySet defines no reflected operators, so there is no `super()`
    # to call: `x.__ror__(y)` means `y | x`, and OR/AND/XOR are commutative as
    # set operations, so the forward operator gives the same rows.

    def __ror__(self, other):
        refuse_scope_mix(self, [other], "|")
        return self.__or__(other)

    def __rand__(self, other):
        refuse_scope_mix(self, [other], "&")
        return self.__and__(other)

    def __rxor__(self, other):
        refuse_scope_mix(self, [other], "^")
        return self.__xor__(other)

    def _combinator_query(self, combinator, *other_qs, **kwargs):
        """The seam behind `union()`, `intersection()` and `difference()`."""
        refuse_scope_mix(self, other_qs, f"{combinator}()")
        return super()._combinator_query(combinator, *other_qs, **kwargs)


class SoftDeleteQuerySet(NoHardDeleteQuerySet):
    """Live rows only; deletion is a stamp."""

    def soft_delete(self, *, by, system: bool = False):
        """Stamp every row in this queryset. Returns the number of rows hit.

        Does not write audit rows - the calling service owns that, because only
        it knows the action name and the request context.
        """
        require_actor(by, system)
        return self.update(**soft_delete_values(self.model, by))

    soft_delete.queryset_only = True  # never exposed as Model.objects.soft_delete()


class ScopedQuerySet(SoftDeleteQuerySet):
    """Live rows of one store (or an explicit set of stores in one org)."""

    # -- pinning ---------------------------------------------------------

    def for_store(self, store):
        pk = _store_pk(store)
        return self._pin(self.filter(store_id=pk), (pk,))

    def for_stores(self, stores):
        pks = _store_pks(stores)
        return self._pin(self.filter(store_id__in=pks), tuple(pks))

    @staticmethod
    def _pin(queryset, pks: tuple[int, ...]):
        queryset.query.store_scoped = True
        queryset.query.store_scope_pks = pks
        return queryset

    def _scope_pks(self) -> tuple[int, ...]:
        return tuple(getattr(self.query, "store_scope_pks", ()) or ())

    def _is_scoped(self) -> bool:
        return bool(getattr(self.query, "store_scoped", False))

    def _require_scope(self, what: str = "write"):
        if not self._is_scoped():
            raise UnscopedQueryError(
                f"{self.model.__name__} is store-scoped: {what} through "
                f"{self.model.__name__}.objects.for_store(store)."
            )

    # -- set operators ---------------------------------------------------
    # `a | b` merges two queries into one WHERE clause and would otherwise
    # inherit the left operand's scope flag while dragging the right operand's
    # rows in with it. That merge is guarded once, at the seam every operator
    # goes through (`ScopedQuery.combine`), rather than on a list of dunder
    # names - so `^`, and whatever Django adds next, are covered by
    # construction. The only combinators that skip `combine` are the ones that
    # build `combined_queries` instead, and they share the seam below.

    def _combinator_query(self, combinator, *other_qs, **kwargs):
        """`union()` / `intersection()` / `difference()`, re-pinned.

        Each leg is compiled separately, so each leg's own scope guard fires -
        but nothing would have stopped the *set of legs* from spanning two
        organizations, which is what `for_stores()` exists to refuse. The
        refusal for an unpinned leg lives on `NoHardDeleteQuerySet`, which this
        call passes through first.
        """
        clone = super()._combinator_query(combinator, *other_qs, **kwargs)
        merged = self._scope_pks()
        for other in other_qs:
            merged = merge_scope_pks(merged, queryset_scope(other)[1], combinator)
        return self._pin(clone, merged)

    # -- writes ----------------------------------------------------------

    def _given_store_pk(self, mapping) -> int | None:
        if not mapping:
            return None
        for key in ("store_id", STORE_FIELD):
            value = mapping.get(key)
            if value is not None:
                return _store_pk(value)
        return None

    def _check_write_store(self, *mappings):
        """Refuse a write that names a store outside the pinned scope."""
        pinned = self._scope_pks()
        if not pinned:
            return
        for mapping in mappings:
            given = self._given_store_pk(mapping)
            if given is not None and given not in pinned:
                raise CrossStoreReferenceError(
                    f"{self.model.__name__}: this queryset is pinned to store(s) "
                    f"{list(pinned)}; store {given} is out of scope."
                )

    def _store_for_write(self, kwargs: dict) -> dict:
        """Fill in / validate `store` for a single-row create."""
        kwargs = dict(kwargs)
        given = self._given_store_pk(kwargs)
        pinned = self._scope_pks()

        if not pinned:
            if given is None:
                raise UnscopedQueryError(
                    f"{self.model.__name__} is store-scoped: create it through "
                    f"{self.model.__name__}.objects.for_store(store).create(...), "
                    f"or pass store=<store> explicitly."
                )
            return kwargs
        if given is None:
            if len(pinned) != 1:
                raise UnscopedQueryError(
                    f"{self.model.__name__}: for_stores() pins {len(pinned)} stores, so a "
                    f"create must name its store explicitly."
                )
            kwargs.pop(STORE_FIELD, None)
            kwargs["store_id"] = pinned[0]
            return kwargs
        if given not in pinned:
            raise CrossStoreReferenceError(
                f"{self.model.__name__}: cannot create a row in store {given} from a "
                f"queryset pinned to store(s) {list(pinned)}."
            )
        return kwargs

    def create(self, **kwargs):
        return super().create(**self._store_for_write(kwargs))

    def get_or_create(self, defaults=None, **kwargs):
        self._check_write_store(kwargs, defaults)
        return super().get_or_create(defaults=defaults, **kwargs)

    def update_or_create(self, defaults=None, create_defaults=None, **kwargs):
        self._check_write_store(kwargs, defaults, create_defaults)
        return super().update_or_create(
            defaults=defaults, create_defaults=create_defaults, **kwargs
        )

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        pinned = self._scope_pks()
        for obj in objs:
            if obj.store_id is None:
                if len(pinned) != 1:
                    raise UnscopedQueryError(
                        f"{self.model.__name__}: bulk_create needs a store on every row, or "
                        f"a queryset pinned to exactly one store."
                    )
                obj.store_id = pinned[0]
            elif pinned and obj.store_id not in pinned:
                raise CrossStoreReferenceError(
                    f"{self.model.__name__}: bulk_create row belongs to store "
                    f"{obj.store_id}, outside the pinned scope {list(pinned)}."
                )
            # bulk_create never calls save(), so the same-store check has to be
            # made here too.
            check = getattr(obj, "_assert_related_stores_match", None)
            if check is not None:
                check()
        return super().bulk_create(objs, *args, **kwargs)

    def _store_scoped_fk_fields(self):
        """Concrete FK fields on this model that point at another store-scoped
        model (the same set `StoreScopedModel._assert_related_stores_match`
        walks on `save()`)."""
        # Local import: `common.models` imports from this module, so a top-level
        # import would be circular. By query time both modules are loaded.
        from common.models import StoreScopedModel

        for field in self.model._meta.concrete_fields:
            if not field.is_relation or field.name == STORE_FIELD:
                continue
            related = field.related_model
            if isinstance(related, type) and issubclass(related, StoreScopedModel):
                yield field

    def _refuse_store_reparenting(self, kwargs) -> None:
        """`update()` may never rewrite `store`/`store_id`.

        Re-parenting a row is a service operation that must also move its
        children into the new store; a bulk column write cannot do that, so it
        would strand a parent in one store with children in another. Refused
        outright - even when the target store is inside the pinned scope.
        """
        for key in (STORE_FIELD, f"{STORE_FIELD}_id"):
            if key in kwargs:
                raise CrossStoreReferenceError(
                    f"{self.model.__name__}: update() cannot change `store`. "
                    f"Re-parenting a row is a service operation that must move its "
                    f"children too, not a bulk column write."
                )

    def _check_update_fk_stores(self, kwargs) -> None:
        """Every store-scoped FK named in an update must be the row's own store.

        Mirrors the create/save same-store invariant on the update path: without
        it, `for_store(A).filter(...).update(product_id=<a store-B product>)`
        silently points a row at another tenant's row.

        Membership in the *pinned set* is not that invariant. `save()` compares
        an FK's store with the row's own store, and a multi-store pin cannot
        know each row's store - `for_stores([A, A2]).update(product_id=<an A2
        product>)` would repoint store A's rows at store A2's catalogue, which
        is the same broken invariant one organization further in. So a
        multi-store pin refuses outright rather than approximating; narrow the
        pin to one store, where membership and equality are the same test.
        """
        pinned = self._scope_pks()
        if not pinned:
            return
        for field in self._store_scoped_fk_fields():
            for key in (field.name, field.attname):
                if key not in kwargs:
                    continue
                if len(pinned) != 1:
                    raise CrossStoreReferenceError(
                        f"{self.model.__name__}.{field.name}: this queryset pins "
                        f"{len(pinned)} stores {list(pinned)}, so no single value can "
                        f"be proven to match the store of every row it would hit. "
                        f"Update through a queryset pinned to exactly one store."
                    )
                value = kwargs[key]
                if hasattr(value, "resolve_expression"):
                    # A query expression (e.g. bulk_update's Case, or F()): its
                    # target store cannot be resolved here, so it cannot be
                    # proven in-scope. Refuse rather than let it through.
                    raise CrossStoreReferenceError(
                        f"{self.model.__name__}.{field.name}: a store-scoped foreign "
                        f"key cannot be updated with a query expression; its target "
                        f"store cannot be verified. Move the row through a service."
                    )
                related_pk = getattr(value, "pk", value)
                if related_pk is None:
                    continue
                related_store_id = (
                    field.related_model.all_objects.filter(pk=related_pk)
                    .values_list("store_id", flat=True)
                    .first()
                )
                if related_store_id is not None and related_store_id not in pinned:
                    raise CrossStoreReferenceError(
                        f"{self.model.__name__}.{field.name}: cannot point at "
                        f"{field.related_model.__name__} {related_pk} in store "
                        f"{related_store_id} from a queryset pinned to "
                        f"{list(pinned)}."
                    )

    def update(self, **kwargs):
        """Refuses an unscoped, re-parenting or cross-store update.

        Note for callers of `bulk_update()`: it runs its `update()` calls inside
        `transaction.atomic(savepoint=False)`, so a `CrossStoreReferenceError`
        raised here escapes an atomic block with no savepoint to roll back to -
        the *surrounding* transaction is marked for rollback and cannot be
        continued. Catching it and carrying on in the same transaction will fail
        with `TransactionManagementError`; validate before the write, or start
        the whole operation again in a new transaction.
        """
        self._require_scope()
        self._refuse_store_reparenting(kwargs)
        self._check_update_fk_stores(kwargs)
        return super().update(**kwargs)


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    def get_queryset(self):
        return guarded_queryset(self, SoftDeleteQuerySet).filter(deleted_at__isnull=True)


class AllObjectsManager(models.Manager.from_queryset(NoHardDeleteQuerySet)):
    """The complete, unfiltered view of a soft-deletable table.

    Used as `all_objects` and as `base_manager_name`, so it must not filter rows
    - but it still cannot delete them, and it still refuses hidden-relation
    traversal.
    """

    def get_queryset(self):
        return guarded_queryset(self, NoHardDeleteQuerySet)


class StoreScopedManager(models.Manager.from_queryset(ScopedQuerySet)):
    def raw(self, raw_query, *args, **kwargs):
        """Raw SQL cannot be scope-checked, so it is refused here.

        Reporting that genuinely needs raw SQL goes through `all_objects.raw()`,
        which is grep-able and reviewable.
        """
        raise UnscopedQueryError(
            f"{self.model.__name__}: raw SQL bypasses store scoping. Use "
            f"{self.model.__name__}.objects.for_store(store), or "
            f"{self.model.__name__}.all_objects.raw() if you really mean it."
        )

    def get_queryset(self):
        queryset = ScopedQuerySet(
            model=self.model,
            query=ScopedQuery(self.model),
            using=self._db,
            hints=self._hints,
        )
        return queryset.filter(deleted_at__isnull=True)


__all__ = [
    "AllObjectsManager",
    "CrossStoreReferenceError",
    "HardDeleteForbidden",
    "NoHardDeleteQuerySet",
    "ScopedQuerySet",
    "SoftDeleteManager",
    "SoftDeleteQuerySet",
    "StoreScopedManager",
    "UnscopedQueryError",
]
