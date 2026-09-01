"""Abstract model bases that carry the cross-cutting invariants.

`common` is an installed app (`CommonConfig`) so that `common/checks.py` runs,
but it declares no concrete models: everything here is abstract, so it owns no
migrations. The concrete models in `apps/*` inherit these and their own
migrations carry the columns.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.managers import (
    STORE_FIELD,
    AllObjectsManager,
    CrossStoreReferenceError,
    HardDeleteForbidden,
    SoftDeleteManager,
    StoreScopedManager,
    require_actor,
    soft_delete_values,
)


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


class SoftDeleteModel(models.Model):
    """Rows are retired, never removed.

    `objects` sees live rows only; `all_objects` sees everything and exists for
    audits and data migrations. Neither can delete, and `all_objects` is also
    the `base_manager`, so the internal paths Django takes (`refresh_from_db`,
    related descriptors) cannot delete either.
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
    """

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
        self._assert_related_stores_match(kwargs.get("update_fields"))
        return super().save(**kwargs)

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


__all__ = ["AuditedModel", "SoftDeleteModel", "StoreScopedModel"]
