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
RIVAL])` that `resolve_scope()` already refused.

One shape reaches neither seam, so `union()` itself is overridden as well:
`QuerySet.union()` drops `self` when it is an `EmptyQuerySet` and, with exactly
one non-empty leg left, hands that leg back *without* building a combined query
at all - `for_store(A).none().union(all_objects.all())` returned every
organization's rows. Nothing is gained by it (the queryset returned is one the
caller already held, so no capability crosses), and three or more legs are
refused correctly because Django then does call `_combinator_query` on the first
surviving leg - but the seam is the contract, so the seam is consulted.
`intersection()` / `difference()` short-circuit too and need no override: their
short-circuit only ever returns an *empty* queryset.

A `|` or `^` involving a *sliced* store-scoped queryset is refused outright:
Django rebuilds a sliced operand through the base manager, which is neither
store-pinned nor live-only, so the merge cannot be proven safe. Combine first,
then slice.

The messages here are for developers, not users: reaching them means a bug in
our code, never bad input from a request. They stay untranslated on purpose.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from django.apps import apps as global_apps
from django.db import models
from django.db.models.sql import Query
from django.db.models.sql.where import AND
from django.utils import timezone

STORE_LABEL = "orgs.store"
STORE_FIELD = "store"

#: The organization pointer, named here rather than in `common/models.py`
#: because the write guards below reason about it and the import direction is
#: `managers <- models` (models imports this module, never the reverse).
#: `common.models` re-exports both names, which is where the checks read them.
ORG_LABEL = "orgs.organization"
ORG_FIELD = "org"
ORG_ATTNAME = f"{ORG_FIELD}_id"
STORE_ATTNAME = f"{STORE_FIELD}_id"


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


@dataclasses.dataclass(frozen=True, slots=True)
class ScopePin:
    """The whole pin: one organization, and one or more of *its* stores.

    Two fields rather than a bare set of store ids, because the organization is
    the fact every merge needs and the fact the old code computed and threw
    away. `resolve_scope()` already had to read `orgs_store` to refuse a
    mixed-organization set; keeping the answer turns every widening merge from a
    database round trip into an integer comparison.

    Frozen, because `sql.Query.combine` mutates `self`: a merge that could
    mutate a leg in place would leave a half-merged pin behind on a refusal, and
    the existing code already takes care to refuse *before* `super()` for that
    reason.

    What the `org_pk` is for, and what it is emphatically not for:

    * **the merge algebra.** Two pins may be combined only when their `org_pk`
      is the same integer. That is a real refusal and it replaces a query.
    * **a write.** `org_id` is stamped from here, and the composite foreign key
      `(store_id, org_id) -> orgs_store (id, org_id)` then proves it at the
      database.
    * **NOT a read predicate.** The org here is *derived from the stores the
      caller named*, so `WHERE org_id = <that org>` is tautological: a store id
      belonging to another organization would re-scope the query to the
      attacker's target rather than return nothing. The read predicate that
      does authorize comes from the tenant context, which does not exist yet.
      See `_pin` for why no `org_id` term is compiled into a read today.
    """

    org_pk: int
    store_pks: tuple[int, ...]


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


def _believable_org_pk(store) -> int | None:
    """`store.org_id`, but only from an instance entitled to be believed.

    A `Store` that came *out of* the database already had its ownership
    resolved by the query that produced it, so re-reading `orgs_store` to learn
    what the instance is holding is a round trip for an answer we have. Three
    conditions decide "came out of the database", and each excludes a way the
    attribute could be a fiction:

    * **`_state.adding is False` and `_state.db` is set** - so `Store(id=7,
      org_id=<a rival's org>)`, hand-built in Python, does not qualify and falls
      through to the resolving query, which refuses an unknown id. Note that the
      old code trusted such an instance's `pk` unconditionally, so this is
      strictly stronger than what it replaces.
    * **`org_id` is not deferred** - `.only("pk")` leaves the attribute absent,
      and touching it would fire a hidden refresh query per store.
    * **`org_id` is not None** - a store with no organization cannot exist
      (the column is NOT NULL), so a None here means "unknown", not "none".

    A caller who loads a real `Store` and then assigns a different `org_id` to
    it in Python is lying to the guard on purpose; that is code we wrote, not
    request data, and the composite foreign key still refuses the write.
    """
    if not isinstance(store, models.Model):
        return None
    state = getattr(store, "_state", None)
    if state is None or state.adding or state.db is None:
        return None
    if ORG_ATTNAME in store.get_deferred_fields():
        return None
    return getattr(store, ORG_ATTNAME, None)


def resolve_scope(stores, *, caller: str = "for_stores()") -> ScopePin:
    """Normalise a collection of stores into a pin, refusing a mixed set.

    Reporting across "my stores" is legitimate; reporting across two orgs'
    stores never is, and the caller usually cannot tell the difference by
    looking at a list of ids - so this resolves them, and now *keeps* the
    answer (see `ScopePin`).

    `caller` names the function the author actually called. The messages are
    otherwise the ones this code has always raised: one of them ("a query may
    never span organizations") is the sentence the whole guard exists to say,
    and a message naming a function nobody called is a defect this ledger has
    already recorded once.
    """
    if isinstance(stores, (str, bytes)) or not isinstance(stores, Iterable):
        raise TypeError(f"{caller} needs an iterable of stores, got {stores!r}.")
    # Insertion-ordered, de-duplicated: `dict` for the order, `None` for "this
    # id's owner is not known yet".
    owners: dict[int, int | None] = {}
    for store in stores:
        pk = _store_pk(store)
        if owners.get(pk) is None:
            owners[pk] = _believable_org_pk(store)
    if not owners:
        raise ValueError(f"{caller} needs at least one store.")

    if any(org is None for org in owners.values()):
        store_model = global_apps.get_model(*STORE_LABEL.split("."))
        resolved = dict(
            store_model.all_objects.filter(pk__in=list(owners))
            # `.order_by()` because `Store.Meta.ordering` otherwise leaks into a
            # `values_list` that never consumes order: measured as a pointless
            # `ORDER BY orgs_store.name ASC` on every pin resolution.
            .order_by()
            .values_list("pk", ORG_ATTNAME)
        )
        unknown = [pk for pk in owners if pk not in resolved]
        if unknown:
            raise ValueError(f"{caller} was given unknown store ids: {unknown}.")
        # The database wins over anything an instance was holding, and the whole
        # set is re-read rather than only the gaps: one query either way.
        owners = {pk: resolved[pk] for pk in owners}

    orgs = set(owners.values())
    if len(orgs) > 1:
        raise CrossStoreReferenceError(
            f"{caller} was given stores from {len(orgs)} organizations "
            f"({sorted(orgs)}); a query may never span organizations."
        )
    return ScopePin(org_pk=orgs.pop(), store_pks=tuple(owners))


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


def merge_pins(
    model, left: ScopePin | None, right: ScopePin | None, operator: str, *, narrow: bool
) -> ScopePin:
    """The pin a combined query carries - computed, no longer re-queried.

    A plain set union is what leaked: `for_stores([A, RIVAL])` is refused by
    `resolve_scope()`, so `for_store(A) | for_store(RIVAL)` is its synonym and
    has to be refused by the same rule. It still is, one step earlier: each leg
    was pinned by a database read that proved *its* stores lie in one
    organization, so the merge only has to ask whether the two organizations are
    the same integer. That is not weaker than re-resolving - the union of two
    sets each inside one organization is inside one organization, and the
    "unknown store id" half was already done per leg - and it costs no query.

    The rule order is load-bearing:

    1. **an unpinned leg is refused first.** Round 4 measured that a shortcut
       which hands back an empty pin lets `ScopedQuery.combine` mark the query
       scoped and compile it with no store predicate at all.
    2. **then the organizations must match**, or `CrossStoreReferenceError`.
    3. **then the store sets merge**: union by default; the intersection when
       `narrow=True`, which is `&` and `intersection()` only. `difference()`
       does not narrow - its rows come from the left leg, about which the right
       leg's pin says nothing - hence a flag rather than the connector, since
       the two call sites speak different vocabularies (`sql.AND`/`OR`/`XOR`
       against `"union"`/`"intersection"`/`"difference"`) and a string compared
       against one of them is dead code on the other path. An empty
       intersection falls back to the union, because a pin of no stores would
       read as "unpinned" downstream.
    """
    if left is None and right is None:
        raise UnscopedQueryError(
            f"{model.__name__} is store-scoped: `{operator}` combines two unpinned "
            f"querysets, so the combined query would carry no store predicate. "
            f"Pin every side with for_store()/for_stores()."
        )
    if left is None or right is None:
        # Not reachable through either call site - `refuse_scope_mismatch` runs
        # first on both the scoped and the unscoped side - and it still refuses
        # rather than adopting the pinned leg's scope, because the alternative
        # to a reachable refusal is a silently unpinned query.
        pinned, unpinned = ("left", "right") if left is not None else ("right", "left")
        raise UnscopedQueryError(
            f"{model.__name__} is store-scoped: `{operator}` combines a store-pinned "
            f"queryset ({pinned}) with an unpinned one ({unpinned}), so the combined "
            f"query would carry no store predicate. Pin every side with "
            f"for_store()/for_stores()."
        )
    if left.org_pk != right.org_pk:
        raise CrossStoreReferenceError(
            f"{model.__name__}: `{operator}` combines a query pinned to organization "
            f"{left.org_pk} with one pinned to organization {right.org_pk}; a query "
            f"may never span organizations."
        )
    merged = tuple(dict.fromkeys(left.store_pks + right.store_pks))
    if narrow:
        shared = set(left.store_pks) & set(right.store_pks)
        narrowed = tuple(pk for pk in merged if pk in shared)
        if narrowed:
            merged = narrowed
    return ScopePin(org_pk=left.org_pk, store_pks=merged)


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

    #: Written per instance by `ScopedQuerySet._pin`. ONE attribute, and the
    #: three names below are read-only views of it: round 4 measured a leak in
    #: which `store_scoped` was True while the store set was empty, and the
    #: query then compiled with no store predicate. A pin that cannot disagree
    #: with itself cannot reproduce that. `Query.clone()` copies `__dict__`, so
    #: the pin survives every chained `filter()` exactly as the flags did.
    scope_pin: ScopePin | None = None

    @property
    def store_scoped(self) -> bool:
        """Whether a store has been pinned. Kept as a name: `queryset_scope`,
        `refuse_scope_mix` and the 45-case operator matrix all read it."""
        return self.scope_pin is not None

    @property
    def store_scope_pks(self) -> tuple[int, ...]:
        return () if self.scope_pin is None else self.scope_pin.store_pks

    @property
    def org_scope_pk(self) -> int | None:
        """The pinned organization - for the merge algebra and for writes. NOT
        a read predicate; see `ScopePin`."""
        return None if self.scope_pin is None else self.scope_pin.org_pk

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


def queryset_pin(queryset) -> ScopePin | None:
    """The `ScopePin` a queryset carries, for callers that need the org."""
    return getattr(getattr(queryset, "query", None), "scope_pin", None)


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


def refuse_sliced_combine(operands, operator: str) -> None:
    """`|` / `^` on a *sliced* store-scoped queryset, refused by name.

    SQL cannot express "OR the first two rows of that", so `QuerySet.__or__` and
    `__xor__` quietly rebuild a sliced operand as
    `model._base_manager.filter(pk__in=<the slice>)`. `_base_manager` here is
    `all_objects`: neither store-pinned nor live-only. The slice itself survives
    as a subquery and still compiles through the scope guard, but the *outer*
    query carries no store predicate and no `deleted_at IS NULL`, so the merged
    query cannot be proven safe - tombstones reappear, and before
    `GuardedQuery.combine` refused the mismatch, `for_store(A)[:2] |
    for_store(RIVAL)` returned both organizations' rows.

    So this refusal is about the message, not the safety: `GuardedQuery.combine`
    is what actually stops the merge (and stops it for any operator Django adds
    later), but it can only say "one side is unpinned" - which reads as a lie to
    an author who pinned both sides and never asked for a base-manager query.

    `&` is deliberately not here. It takes no such rewrite: Django itself raises
    `TypeError("Cannot combine queries once a slice has been taken.")` for a
    sliced left operand, which is accurate, and a sliced right operand is merged
    as an ordinary WHERE clause with its limit dropped - no guarantee is lost.
    Non-scoped models are not here either: for them `_base_manager` is the same
    unfiltered `all_objects` the operands already came from, so the rewrite
    removes nothing.
    """
    for operand in operands:
        if isinstance(operand, ScopedQuerySet) and operand.query.is_sliced:
            raise UnscopedQueryError(
                f"{operand.model.__name__}: `{operator}` on a *sliced* queryset. Django "
                f"rebuilds a sliced operand through the base manager, which is neither "
                f"store-pinned nor live-only, so the merge cannot be proven safe. "
                f"Combine first, then slice."
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
        """The scoped side of the same seam: merge the two pins.

        Once a mixed merge is refused, both sides are pinned and the remaining
        question is *whose organization and whose stores*. Both checks run
        before `super()`, because `Query.combine` mutates `self` and a refusal
        should leave nothing half-merged behind.
        """
        self.refuse_scope_mismatch(rhs, connector)
        merged = merge_pins(
            self.model,
            self.scope_pin,
            getattr(rhs, "scope_pin", None),
            connector,
            narrow=connector == AND,
        )
        result = super().combine(rhs, connector)
        self.scope_pin = merged
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


#: Combinators whose result is provably inside *every* leg, so the merged pin
#: may narrow to the intersection of the legs' pins. `union` and `difference` are
#: absent on purpose: a union's rows come from either leg, and a difference's
#: come from the left one, which the right leg's pin does not bound.
NARROWING_COMBINATORS = frozenset({"intersection"})


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
        refuse_sliced_combine([self, other], "|")
        return super().__or__(other)

    def __and__(self, other):
        refuse_scope_mix(self, [other], "&")
        return super().__and__(other)

    def __xor__(self, other):
        refuse_scope_mix(self, [other], "^")
        refuse_sliced_combine([self, other], "^")
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

    def union(self, *other_qs, **kwargs):
        """Guarded here as well, because `union()` can skip the seam entirely.

        `QuerySet.union()` drops `self` when it is an `EmptyQuerySet` and, with
        exactly one non-empty leg left, returns that leg without ever calling
        `_combinator_query` - so `for_store(A).none().union(all_objects.all())`
        reached no refusal at all and iterated every organization's rows.
        Nothing is *gained* by it (Django hands back the caller's own queryset,
        so the caller could have iterated it directly, and `all_objects` is the
        sanctioned audit view), which is why this is a seam fix rather than a
        leak fix: every combination is refused where it is written.

        The short-circuit itself still stands where both legs are pinned -
        including to two different organizations. Nothing is merged there, so
        there is no combined query to resolve and no scope to widen: Django
        returns the surviving leg, which the caller already had and could have
        iterated on its own. `test_a_short_circuited_union_returns_the_surviving_
        leg_unchanged` pins that by identity, so the day Django merges instead,
        the merge goes through `_combinator_query` like every other.

        `intersection()` and `difference()` short-circuit on an empty leg too and
        need no override - their short-circuit only ever returns an *empty*
        queryset, so there is nothing to prove about the result.
        """
        refuse_scope_mix(self, other_qs, "union()")
        return super().union(*other_qs, **kwargs)


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
        """One store. Now resolved rather than taken on trust, which buys a
        consistent diagnostic: an unknown store id used to raise from
        `for_stores()` and return *silently empty* here, and a query that looks
        scoped and returns nothing is how scoping bugs hide. A saved `Store`
        instance still costs no query (`_believable_org_pk`).

        A pin is not an authorization. `for_store(<a rival's store>)` returns
        the rival's rows, here as before, because nothing in a scoping
        primitive knows who is asking. That is `require_store()`'s job, and a
        store id from request data must never reach this function.
        """
        pin = resolve_scope([store], caller="for_store()")
        return self._pin(self.filter(store_id=pin.store_pks[0]), pin)

    def for_stores(self, stores):
        pin = resolve_scope(stores)
        return self._pin(self.filter(store_id__in=pin.store_pks), pin)

    @staticmethod
    def _pin(queryset, pin: ScopePin):
        """Attach the pin. Deliberately attaches *only* the pin.

        No `org_id = %s` term is compiled into the read. It would be
        tautological - the org came from the stores the caller named, so a store
        id from another organization re-scopes the query to that organization
        instead of returning nothing - and a term that looks like a tenant guard
        without being one is worse than no term. The predicate that does
        authorize comes from the tenant context, which does not exist yet.

        When the org-leading composite indexes land, an `org_id` term becomes a
        *query-plan* requirement (a `(org_id, store_id, ...)` index cannot serve
        a `store_id`-only predicate). Add it then, with the plan measured before
        and after, and label it as what it is.
        """
        queryset.query.scope_pin = pin
        return queryset

    def _scope_pin(self) -> ScopePin | None:
        return getattr(self.query, "scope_pin", None)

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
        merged = self._scope_pin()
        for other in other_qs:
            merged = merge_pins(
                self.model,
                merged,
                queryset_pin(other),
                f"{combinator}()",
                narrow=combinator in NARROWING_COMBINATORS,
            )
        return self._pin(clone, merged)

    # -- writes ----------------------------------------------------------

    def _given_store_pk(self, mapping) -> int | None:
        if not mapping:
            return None
        for key in (STORE_ATTNAME, STORE_FIELD):
            value = mapping.get(key)
            if value is not None:
                return _store_pk(value)
        return None

    def _given_org_pk(self, mapping) -> int | None:
        """The same shape for `org`. Callers do not pass it - the column is
        `editable=False` and derived - but a write that names it must be
        checked rather than ignored."""
        if not mapping:
            return None
        for key in (ORG_ATTNAME, ORG_FIELD):
            value = mapping.get(key)
            if value is None:
                continue
            if isinstance(value, models.Model):
                if value._meta.label_lower != ORG_LABEL:
                    raise TypeError(
                        f"{self.model.__name__}.{ORG_FIELD} needs a {ORG_LABEL} "
                        f"instance, got {value._meta.label_lower}."
                    )
                if value.pk is None:
                    raise ValueError(
                        f"{self.model.__name__}.{ORG_FIELD} needs a saved "
                        f"organization; this one has no primary key."
                    )
                return value.pk
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"{self.model.__name__}.{ORG_FIELD} needs an {ORG_LABEL} instance "
                    f"or integer primary key, got {value!r}."
                )
            if value <= 0:
                raise ValueError(
                    f"{self.model.__name__}.{ORG_FIELD} needs a positive primary key, "
                    f"got {value!r}."
                )
            return value
        return None

    def _check_write_store(self, *mappings):
        """Refuse a write that names a store, or an org, outside the pin."""
        pin = self._scope_pin()
        if pin is None:
            return
        pinned = pin.store_pks
        for mapping in mappings:
            given = self._given_store_pk(mapping)
            if given is not None and given not in pinned:
                raise CrossStoreReferenceError(
                    f"{self.model.__name__}: this queryset is pinned to store(s) "
                    f"{list(pinned)}; store {given} is out of scope."
                )
            given_org = self._given_org_pk(mapping)
            if given_org is not None and given_org != pin.org_pk:
                raise CrossStoreReferenceError(
                    f"{self.model.__name__}: this queryset is pinned to organization "
                    f"{pin.org_pk}; organization {given_org} is out of scope."
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

    def _org_for_write(self, kwargs: dict) -> dict:
        """Stamp `org` from the pin, and refuse one that disagrees with it.

        Free and exact: the pin's organization and its stores came out of one
        read, so this cannot be inconsistent with `store`. Without a pin there
        is nothing to stamp from and `StoreScopedModel._derive_org()` does the
        work at `save()` instead, from the store.
        """
        pin = self._scope_pin()
        if pin is None:
            return kwargs
        kwargs = dict(kwargs)
        given = self._given_org_pk(kwargs)
        if given is not None and given != pin.org_pk:
            raise CrossStoreReferenceError(
                f"{self.model.__name__}: cannot create a row in organization {given} "
                f"from a queryset pinned to organization {pin.org_pk}."
            )
        kwargs.pop(ORG_FIELD, None)
        kwargs[ORG_ATTNAME] = pin.org_pk
        return kwargs

    def create(self, **kwargs):
        return super().create(**self._org_for_write(self._store_for_write(kwargs)))

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
        pin = self._scope_pin()
        pinned = () if pin is None else pin.store_pks
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
            # bulk_create never calls save(), so the same-store check and the
            # org derivation both have to be made here too. Order matters: the
            # pin's org is stamped first (free, and exact), then `_derive_org`
            # either fills it from the store or refuses a disagreement.
            if pin is not None and getattr(obj, ORG_ATTNAME, None) is None:
                setattr(obj, ORG_ATTNAME, pin.org_pk)
            derive = getattr(obj, "_derive_org", None)
            if derive is not None:
                derive()
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
        for key in (STORE_FIELD, STORE_ATTNAME):
            if key in kwargs:
                raise CrossStoreReferenceError(
                    f"{self.model.__name__}: update() cannot change `store`. "
                    f"Re-parenting a row is a service operation that must move its "
                    f"children too, not a bulk column write."
                )

    def _refuse_org_rehoming(self, kwargs) -> None:
        """`update()` may never rewrite `org`/`org_id`, for a stronger reason.

        `org` is *derived* from `store`, and the composite foreign key
        `(store_id, org_id) -> orgs_store (id, org_id)` is what proves the pair
        agrees. A bulk write to `org_id` alone therefore either breaks that key
        outright or - if it were ever paired with a matching `store_id` write -
        would move a business row into another organization, which is not an
        operation this product has. Re-homing a store is a service operation on
        `orgs_store`, not a column write on its rows.

        Refused in Python as well as at the database because the message is the
        point: an `IntegrityError` naming `<table>_store_same_org_fk` tells the
        author what broke, not what they should have done.
        """
        for key in (ORG_FIELD, ORG_ATTNAME):
            if key in kwargs:
                raise CrossStoreReferenceError(
                    f"{self.model.__name__}: update() cannot change `{ORG_FIELD}`. It "
                    f"is derived from `store` and proven by the "
                    f"{self.model._meta.db_table}_store_same_org_fk composite key; "
                    f"re-homing a store is a service operation, not a column write."
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
        self._refuse_org_rehoming(kwargs)
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
    "ORG_ATTNAME",
    "ORG_FIELD",
    "ORG_LABEL",
    "STORE_ATTNAME",
    "STORE_FIELD",
    "STORE_LABEL",
    "AllObjectsManager",
    "CrossStoreReferenceError",
    "HardDeleteForbidden",
    "NoHardDeleteQuerySet",
    "ScopePin",
    "ScopedQuerySet",
    "SoftDeleteManager",
    "SoftDeleteQuerySet",
    "StoreScopedManager",
    "UnscopedQueryError",
    "merge_pins",
    "queryset_pin",
    "queryset_scope",
    "resolve_scope",
]
