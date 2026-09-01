"""Concrete models that exist only to exercise the abstract bases in `common/`.

Installed by `config.settings.test` only. Never migrated into a real database.

`Category` / `Product` / `Sale` / `SaleLine` deliberately mirror the shapes
slice 2 will add - an org-level parent with store-scoped children, and a
store-scoped parent with store-scoped children - because those are the shapes
that produced the reproduced cross-tenant leaks.
"""

from django.db import models
from django.db.models import Q

from common.models import AuditedModel, SoftDeleteModel, StoreScopedModel

LIVE = Q(deleted_at__isnull=True)


class Thing(SoftDeleteModel, AuditedModel):
    """Soft-deletable, audited, org-level (not store-scoped) stand-in."""

    name = models.CharField(max_length=50)


class ScopedThing(StoreScopedModel):
    """Store-scoped stand-in. Inherits the `store` FK from the base."""

    name = models.CharField(max_length=50)


class ScopedThingOwnMeta(StoreScopedModel):
    """Store-scoped child that declares its own Meta WITHOUT inheriting the
    base's - the common mistake that must not disarm the default manager."""

    name = models.CharField(max_length=50)

    class Meta:
        verbose_name = "scoped thing with its own meta"


class Category(SoftDeleteModel, AuditedModel):
    """Org-level parent of a store-scoped model.

    Its reverse accessor to `Product` is suppressed (`related_name="+"` on the
    child's FK); the accessor to its own organization is not, because
    org-level models are not scope-guarded.
    """

    org = models.ForeignKey("orgs.Organization", on_delete=models.PROTECT, related_name="+")
    name = models.CharField(max_length=50)


class Product(StoreScopedModel):
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="+")
    name = models.CharField(max_length=50)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["store", "name"],
                condition=LIVE,
                name="testapp_product_unique_live_name_per_store",
            )
        ]


class Sale(StoreScopedModel):
    reference = models.CharField(max_length=50)


class SaleLine(StoreScopedModel):
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name="+")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(default=1)
