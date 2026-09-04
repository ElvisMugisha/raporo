"""Abstract model bases that carry the cross-cutting invariants.

`common` is an installed app (`CommonConfig`) so that `common/checks.py` runs,
but it declares no concrete models: everything here is abstract, so it owns no
migrations. The concrete models in `apps/*` inherit these and their own
migrations carry the columns.

This module is also where the column *names* the system checks reason about
are published (`ORG_FIELD`, `PUBLIC_ID_FIELD`, and the sets built from them).
They sit next to the bases that declare the columns so that changing the rule
in `common/checks.py` means changing the base, and not maintaining a second
spelling of the same fact. `ORG_FIELD` itself is defined one module earlier, in
`common/managers.py`, because the write guards there stamp the same column and
the import direction is `managers <- models <- checks`.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.managers import (
    ORG_ATTNAME,
    ORG_FIELD,
    ORG_LABEL,
    STORE_ATTNAME,
    STORE_FIELD,
    AllObjectsManager,
    CrossStoreReferenceError,
    HardDeleteForbidden,
    SoftDeleteManager,
    StoreScopedManager,
    require_actor,
    soft_delete_values,
)

#: The organization pointer, re-exported here because `common.checks` reads the
#: tenant column names from this module and must not keep a second spelling of
#: them. The definition itself lives in `common/managers.py`, one step earlier
#: in the import order (`managers <- models <- checks`), because the write
#: guards there reason about the same column.

#: The surrogate identifier that crosses the process boundary (ADR 0010).
PUBLIC_ID_FIELD = "public_id"

#: A unique constraint on a store-scoped table has to name at least one of
#: these, or it is enforced across every tenant.
ORG_COLUMNS = frozenset({ORG_FIELD, ORG_ATTNAME})
STORE_COLUMNS = frozenset({STORE_FIELD, STORE_ATTNAME})
TENANT_COLUMNS = ORG_COLUMNS | STORE_COLUMNS

#: The one column whose uniqueness is deliberately global and unconditional.
IDENTITY_COLUMNS = frozenset({PUBLIC_ID_FIELD})


class PublicIdModel(models.Model):
    """The identifier a URL may name (ADR 0010).

    Every row a user can act on is addressed by a URL in a server-rendered
    HTMX app. A sequential `BigAutoField` there is an enumeration oracle: the
    difference between "id 400 is not yours" and "id 4000 does not exist"
    leaks the size and growth rate of every other tenant on the platform.
    `public_id` is the only identifier that crosses the process boundary; the
    primary key stays internal and stays the target of the composite foreign
    keys.

    Three properties of the declaration below are load-bearing:

    * **A Python default, not `db_default=UUID7()`.** Of Django 6.1's
      uniqueness paths only `Model.clean_fields()` skips a `DatabaseDefault`
      sentinel - `validate_unique()`, `_get_unique_checks()`,
      `validate_constraints()` and `UniqueConstraint.validate()` all read the
      attribute - so under a database default `full_clean()` on an *unsaved*
      instance compiles `WHERE public_id = UUIDV7()`, and a template rendering
      an unsaved object puts an expression object in a DOM id. A Python
      default means the value is a real UUID from the moment the object
      exists. PostgreSQL 18 makes `db_default=UUID7()` *available*; it does
      not make it correct here. It may be added alongside this default once
      the uniqueness paths handle the sentinel - Django prefers the Python
      default when both are set - and it would buy only the raw-SQL insert
      path, which nothing in this codebase uses.
    * **`unique=True` on the field, not a `UniqueConstraint` in `Meta`.** On
      PostgreSQL `unique=True` *is* a unique B-tree index, and it is the index
      the URL lookup uses, so `db_index` stays off: a second index would be
      pure write cost on every insert into every table. Living on the field
      also means a subclass that declares its own `Meta` without inheriting
      this one cannot silently drop it - the accident `common.E002` exists
      for. `common.E005` carries the matching exemption, and it is the only
      non-primary-key `unique=True` in the schema.
    * **Unconditional uniqueness** - the inverse of every other unique
      constraint here, deliberately. A soft-deleted row keeps its identifier
      for ever. Conditioned on live rows, a tombstone would release its
      `public_id`, a later insert could take it, and a bookmarked URL or an
      audit reference would then resolve to a different row. Reissuing an
      identifier is worse than reserving one.

    UUIDv7 rather than v4 because it is time-ordered: measured on 300k rows,
    v7's unique index came out 16% smaller and its inserts 7% faster than v4's.

    This is not an authorization control. It removes *enumeration*; the store
    pin removes authorization risk. A valid identifier belonging to another
    tenant must still be a 404, which is a property of the selector that reads
    it, not of the column.
    """

    public_id = models.UUIDField(
        _("public id"),
        default=uuid.uuid7,
        editable=False,
        unique=True,
    )

    class Meta:
        abstract = True


class AuditedModel(models.Model):
    """Who created this row, who touched it last, and when.

    The actor columns are nullable (system-initiated rows have no user) and
    PROTECTed: a user who acted cannot be erased from underneath the trail.
    """

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("updated by"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    class Meta:
        abstract = True


class SoftDeleteModel(PublicIdModel):
    """Rows are retired, never removed.

    `objects` sees live rows only; `all_objects` sees everything and exists for
    audits and data migrations. Neither can delete, and `all_objects` is also
    the `base_manager`, so the internal paths Django takes (`refresh_from_db`,
    related descriptors) cannot delete either.

    Inherits `PublicIdModel`, which is how every organization, store, role,
    membership, store access and store-scoped business row gets its public
    identifier from one declaration. `PublicIdModel` is mixed in *here* rather
    than into `AuditedModel` because the two are combined independently
    (`Organization(SoftDeleteModel, AuditedModel)`): declaring the column on
    both would hand the same field to a model twice and Django would raise
    `FieldError`. `accounts.User` and `audit.AuditLog` are neither
    soft-deletable nor audited, so they mix `PublicIdModel` in directly.
    """

    deleted_at = models.DateTimeField(_("deleted at"), null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("deleted by"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True
        base_manager_name = "all_objects"

    def soft_delete(self, *, by, system: bool = False) -> bool:
        """Stamp this row as deleted. Returns False if it already was.

        Idempotent on purpose: an at-least-once caller must not be able to
        rewrite the original actor or timestamp. Writes no audit row - the
        calling service owns that.
        """
        if self.pk is None:
            raise ValueError(
                f"{type(self).__name__}: cannot soft-delete a row that was never saved."
            )
        require_actor(by, system)
        if self.deleted_at is not None:
            return False

        values = soft_delete_values(type(self), by)
        for field, value in values.items():
            setattr(self, field, value)
        self.save(update_fields=list(values))
        return True

    def delete(self, *args, **kwargs):
        raise HardDeleteForbidden(
            f"{type(self).__name__}: hard delete is forbidden; use soft_delete(by=<user>)."
        )


class StoreScopedModel(SoftDeleteModel, AuditedModel):
    """Invariant #1: business data belongs to exactly one store.

    The `store` FK lives here, not in each consumer, so a store-scoped model
    cannot be declared without it. There is no reverse accessor
    (`related_name="+"`) on purpose: `Model.objects.for_store(store)` is the
    single way in, which keeps a scope-blind, soft-delete-blind
    `store.thing_set.all()` from existing. `common.checks` (E004) enforces the
    same rule for every other relation touching a store-scoped model.

    `Meta.default_manager_name` points at `all_objects` because Django itself
    reaches for `_default_manager` (unique validation, admin, forms). If that
    were the guarded manager, `full_clean()` on any store-scoped model would
    raise `UnscopedQueryError`. Two things make that hold in practice: the
    declaration order below (`all_objects` first) and `common.checks` E002,
    which fails startup if any subclass ends up with a different default
    manager - including the easy accident of declaring an extra manager of its
    own, which makes Django skip the abstract-Meta fallback entirely.

    `save()` refuses a row whose store-scoped foreign keys point into another
    store, so a form that accepts a raw `<related>_id` cannot be walked into
    someone else's data.

    `org` is denormalised alongside `store` so that PostgreSQL itself can refuse
    a row whose two tenant columns disagree, through the composite key
    `(store_id, org_id) -> orgs_store (id, org_id)` that every concrete table's
    migration installs (`common.db.same_org_fk_v1`). It is derived from `store`,
    never asked for, and `common.E007` fails startup for a subclass that loses
    the column or re-declares it in a weaker shape.
    """

    org = models.ForeignKey(
        ORG_LABEL,
        verbose_name=_("organization"),
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
        db_index=False,
        db_constraint=False,
    )
    store = models.ForeignKey(
        "orgs.Store",
        verbose_name=_("store"),
        on_delete=models.PROTECT,
        related_name="+",
    )

    all_objects = AllObjectsManager()
    objects = StoreScopedManager()

    class Meta:
        abstract = True
        base_manager_name = "all_objects"
        default_manager_name = "all_objects"

    def save(self, **kwargs):
        self._derive_org()
        self._assert_related_stores_match(kwargs.get("update_fields"))
        return super().save(**kwargs)

    def clean_fields(self, exclude=None):
        """Derive `org` before per-field validation.

        Measured on Django 6.1: `Model.clean_fields()` does *not* skip
        `editable=False` fields, so without this `full_clean()` reports a
        non-null `org` as missing on a perfectly valid store + name pair. The
        same bug was found and fixed once already, on `StoreAccess`.
        """
        self._derive_org()
        super().clean_fields(exclude=exclude)

    def _derive_org(self) -> None:
        """`org` follows `store`. It is never asked for, and never guessed.

        Three sources, and the order is the point:

        1. **already set** - by `ScopedQuerySet._org_for_write` from the pin, or
           by `bulk_create`. Free, and consistent by construction: the pin's org
           and its stores came out of one read.
        2. **a cached `store` instance** - free, because the instance carries
           `org_id`. `Model(store=<store>)` and `obj.store = <store>` both leave
           one cached, which is every ordinary create.
        3. **one `SELECT org_id FROM orgs_store WHERE id = %s`** - and only when
           `org_id` is still unknown, so an ordinary `save()` of an existing row
           costs nothing.

        Where two sources are both known and disagree, this raises rather than
        picking one: `obj.store = <another organization's store>; obj.save()` is
        an attempt to move a business row between tenants, and it should say so
        in Python, not arrive as a foreign-key violation.

        Where the value is only *asserted* - `org_id` set with no cached store,
        which no sanctioned write path produces - the composite foreign key is
        the backstop, and a wrong value surfaces as an `IntegrityError` naming
        `<table>_store_same_org_fk`. Verifying it here instead would put a query
        on every single save to catch a case only hand-written code can reach.
        """
        if self.store_id is None:
            # No store to derive from; the NOT NULL constraint will speak.
            return
        store_field = self._meta.get_field(STORE_FIELD)
        cached = store_field.get_cached_value(self, default=None)
        from_store = None
        if (
            cached is not None
            and cached.pk == self.store_id
            and ORG_ATTNAME not in cached.get_deferred_fields()
        ):
            from_store = getattr(cached, ORG_ATTNAME, None)
        if from_store is None and self.org_id is None:
            from_store = (
                store_field.related_model.all_objects.filter(pk=self.store_id)
                # `Store.Meta.ordering` would otherwise add an ORDER BY that
                # this single-row lookup never consumes.
                .order_by()
                .values_list(ORG_ATTNAME, flat=True)
                .first()
            )
        if from_store is None:
            # Unknown store, or a deferred/absent org on the cached instance.
            # The foreign key on `store_id` will refuse a row that does not
            # exist; there is nothing to derive from either way.
            return
        if self.org_id is not None and self.org_id != from_store:
            raise CrossStoreReferenceError(
                f"{type(self).__name__}: store {self.store_id} belongs to "
                f"organization {from_store}, but this row carries organization "
                f"{self.org_id}. `{ORG_FIELD}` is derived from `{STORE_FIELD}`; "
                f"moving a row between organizations is not an operation."
            )
        self.org_id = from_store

    def _assert_related_stores_match(self, only_fields=None):
        """Every store-scoped row this one points at must live in its store.

        Enforced in `save()` rather than `clean()`: `objects.create()` and the
        admin's inline saves never call `full_clean()`, and this is the check
        that stops an IDOR through a foreign key.
        """
        if self.store_id is None:
            # No scope to compare against; the NOT NULL constraint will speak.
            return
        wanted = set(only_fields) if only_fields is not None else None
        if wanted is not None and wanted & {STORE_FIELD, f"{STORE_FIELD}_id"}:
            # The row is changing store, so every store-scoped FK it holds must
            # be re-checked against the new store - not only the named fields.
            # Otherwise `save(update_fields=["store"])` moves a row into a store
            # its foreign keys do not belong to, unchallenged.
            wanted = None
        for field in self._meta.concrete_fields:
            if not field.is_relation or field.name == STORE_FIELD:
                continue
            if wanted is not None and not {field.name, field.attname} & wanted:
                continue
            related_model = field.related_model
            if not (
                isinstance(related_model, type)
                and issubclass(related_model, StoreScopedModel)
            ):
                continue
            related_pk = getattr(self, field.attname)
            if related_pk is None:
                continue

            cached = field.get_cached_value(self, default=None)
            if cached is not None and cached.pk == related_pk:
                related_store_id = cached.store_id
            else:
                related_store_id = (
                    related_model.all_objects.filter(pk=related_pk)
                    .values_list("store_id", flat=True)
                    .first()
                )
            if related_store_id is None:
                # Row does not exist; the database's foreign key will say so.
                continue
            if related_store_id != self.store_id:
                raise CrossStoreReferenceError(
                    f"{type(self).__name__}.{field.name} points at "
                    f"{related_model.__name__} {related_pk} in store "
                    f"{related_store_id}, but this row belongs to store "
                    f"{self.store_id}."
                )


__all__ = [
    "IDENTITY_COLUMNS",
    "ORG_ATTNAME",
    "ORG_COLUMNS",
    "ORG_FIELD",
    "ORG_LABEL",
    "PUBLIC_ID_FIELD",
    "STORE_ATTNAME",
    "STORE_COLUMNS",
    "STORE_FIELD",
    "TENANT_COLUMNS",
    "AuditedModel",
    "PublicIdModel",
    "SoftDeleteModel",
    "StoreScopedModel",
]
