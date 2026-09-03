"""The tenancy spine: organizations, their stores, and who may do what.

Business data never carries an organization pointer of its own: a store-scoped
row reaches its organization through `Store.org`, which is the single join to
get right (`common.checks` E004/E006 keep it that way, and a test asserts no
store-scoped model declares an `org` field). The models in *this* module are
the organization's own structure, so they do carry `org` - and the pairs that
must agree (`Membership.role`, `StoreAccess.store`, `AuditLog.store`) are tied
together by composite foreign keys in the migration, not by `clean()` alone:
`Model.objects.create()` never calls `clean()`.

Every model here is org-level - soft-deletable and audited, but not
`StoreScopedModel`: they describe stores rather than living inside one.
"""

from pathlib import Path
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.orgs.permissions import PERMISSIONS, unknown_codes
from common.models import AuditedModel, SoftDeleteModel
from common.validators import (
    ALLOWED_IMAGE_EXTENSIONS,
    currency_code_validator,
    image_extension_validator,
    validate_image_content,
    validate_image_size,
    validate_timezone,
)

#: Business rule: an organization runs between one and five stores. The
#: create_store service enforces the ceiling under a row lock.
MAX_STORES_PER_ORG = 5

#: Applies to every "unique among live rows" constraint below: a soft-deleted
#: row must not block reusing its name.
LIVE = Q(deleted_at__isnull=True)


def organization_logo_path(instance, filename: str) -> str:
    """Store logos under a random name.

    The uploaded filename is attacker-controlled: reusing it invites path games
    and lets one org guess another's URL. Only the (already validated)
    extension survives.
    """
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        extension = "png"
    return f"org-logos/{uuid4().hex}.{extension}"


class Organization(SoftDeleteModel, AuditedModel):
    name = models.CharField(_("name"), max_length=120)
    # Human-facing text - report filenames, share cards, branding - and
    # deliberately NOT a routing key. Its unique constraint below is
    # conditioned on live rows by design (a soft-deleted organization releases
    # its slug), it is mutable and user-chosen, and it would put the tenant's
    # name in every URL, referrer and proxy log. `public_id` is the identifier
    # (ADR 0010); the organization does not appear in a URL at all.
    slug = models.SlugField(_("slug"), max_length=140)
    logo = models.ImageField(
        _("logo"),
        upload_to=organization_logo_path,
        blank=True,
        validators=[image_extension_validator, validate_image_size, validate_image_content],
    )
    brand = models.JSONField(
        _("brand tokens"),
        default=dict,
        blank=True,
        help_text=_("Colour and typography tokens for this organization's reports."),
    )
    base_currency = models.CharField(
        _("base currency"),
        max_length=3,
        default="RWF",
        validators=[currency_code_validator],
    )
    timezone = models.CharField(
        _("time zone"),
        max_length=64,
        default="Africa/Kigali",
        validators=[validate_timezone],
        help_text=_("Reporting periods start and end in this zone."),
    )

    class Meta:
        verbose_name = _("organization")
        verbose_name_plural = _("organizations")
        ordering = ("name",)
        constraints = [
            # Live rows only: soft-deleting an organization must not reserve its
            # slug for ever, or a re-signup with the same name 500s.
            models.UniqueConstraint(
                fields=["slug"],
                condition=LIVE,
                name="orgs_organization_unique_live_slug",
                violation_error_message=_("That URL slug is taken."),
            )
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if not isinstance(self.brand, dict):
            raise ValidationError(
                {"brand": _("Brand tokens must be a set of key/value pairs.")}
            )


class Store(SoftDeleteModel, AuditedModel):
    org = models.ForeignKey(
        Organization,
        verbose_name=_("organization"),
        on_delete=models.PROTECT,
        related_name="stores",
    )
    name = models.CharField(_("name"), max_length=120)
    brand = models.JSONField(
        _("brand tokens"),
        default=dict,
        blank=True,
        help_text=_("Used only when this store uses its own branding."),
    )
    use_own_branding = models.BooleanField(
        _("use its own branding"),
        default=False,
        help_text=_("Off: inherit the organization's branding entirely."),
    )

    class Meta:
        verbose_name = _("store")
        verbose_name_plural = _("stores")
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "name"],
                condition=LIVE,
                name="orgs_store_unique_live_name_per_org",
                violation_error_message=_(
                    "This organization already has a store with that name."
                ),
            ),
            # Target for the composite foreign keys that tie a store to its
            # organization wherever the pair is stored together.
            models.UniqueConstraint(
                fields=["id", "org"],
                name="orgs_store_id_org_uniq",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if not isinstance(self.brand, dict):
            raise ValidationError(
                {"brand": _("Brand tokens must be a set of key/value pairs.")}
            )


class Role(SoftDeleteModel, AuditedModel):
    org = models.ForeignKey(
        Organization,
        verbose_name=_("organization"),
        on_delete=models.PROTECT,
        related_name="roles",
    )
    name = models.CharField(_("name"), max_length=60)
    permissions = models.JSONField(_("permissions"), default=list, blank=True)
    is_preset = models.BooleanField(_("preset"), default=False)

    class Meta:
        verbose_name = _("role")
        verbose_name_plural = _("roles")
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(
                fields=["org", "name"],
                condition=LIVE,
                name="orgs_role_unique_live_name_per_org",
                violation_error_message=_(
                    "This organization already has a role with that name."
                ),
            ),
            models.UniqueConstraint(
                fields=["id", "org"],
                name="orgs_role_id_org_uniq",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        codes = self.permissions
        if not isinstance(codes, list) or any(not isinstance(code, str) for code in codes):
            raise ValidationError(
                {"permissions": _("Permissions must be a list of permission codes.")}
            )
        if len(set(codes)) != len(codes):
            raise ValidationError(
                {"permissions": _("Each permission may only be listed once.")}
            )
        unknown = unknown_codes(codes)
        if unknown:
            raise ValidationError(
                {
                    "permissions": ValidationError(
                        _("Unknown permission: %(codes)s."),
                        code="unknown_permission",
                        params={"codes": ", ".join(unknown)},
                    )
                }
            )

    def has(self, code: str) -> bool:
        """True when this role grants `code`. Unknown codes are always False."""
        return bool(code) and code in PERMISSIONS and code in set(self.permissions or [])


class Membership(SoftDeleteModel, AuditedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.PROTECT,
        related_name="memberships",
    )
    org = models.ForeignKey(
        Organization,
        verbose_name=_("organization"),
        on_delete=models.PROTECT,
        related_name="memberships",
    )
    role = models.ForeignKey(
        Role,
        verbose_name=_("role"),
        on_delete=models.PROTECT,
        related_name="memberships",
    )

    class Meta:
        verbose_name = _("membership")
        verbose_name_plural = _("memberships")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "org"],
                condition=LIVE,
                name="orgs_membership_unique_live_user_per_org",
                violation_error_message=_(
                    "This person is already a member of this organization."
                ),
            ),
            models.UniqueConstraint(
                fields=["id", "org"],
                name="orgs_membership_id_org_uniq",
            ),
        ]

    def __str__(self):
        return f"{self.user} @ {self.org}"

    def clean(self):
        super().clean()
        if self.role_id and self.org_id and self.role.org_id != self.org_id:
            raise ValidationError(
                {"role": _("That role belongs to a different organization.")}
            )


class StoreAccess(SoftDeleteModel, AuditedModel):
    """Which stores a membership may work in. Materialised even for owners:
    explicit rows beat an implicit "owners see everything" rule.

    `org` is denormalized so the database can hold `(membership, org)` and
    `(store, org)` together and refuse a row that mixes two organizations.
    """

    membership = models.ForeignKey(
        Membership,
        verbose_name=_("membership"),
        on_delete=models.PROTECT,
        related_name="store_access",
    )
    store = models.ForeignKey(
        Store,
        verbose_name=_("store"),
        on_delete=models.PROTECT,
        related_name="access",
    )
    org = models.ForeignKey(
        Organization,
        verbose_name=_("organization"),
        on_delete=models.PROTECT,
        related_name="+",
        help_text=_("Denormalized from the membership so the database can enforce it."),
    )

    class Meta:
        verbose_name = _("store access")
        verbose_name_plural = _("store access")
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "store"],
                condition=LIVE,
                name="orgs_storeaccess_unique_live_membership_store",
                violation_error_message=_("This member already has access to that store."),
            )
        ]

    def __str__(self):
        return f"{self.membership} -> {self.store}"

    def _derive_org(self):
        """`org` is derived, never asked for: callers pass membership + store."""
        if self.org_id is None and self.membership_id is not None:
            self.org_id = self.membership.org_id

    def save(self, **kwargs):
        self._derive_org()
        return super().save(**kwargs)

    def clean_fields(self, exclude=None):
        # Derived before per-field validation, or `org` (non-null) would be
        # reported as missing on a perfectly valid membership + store pair.
        self._derive_org()
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        self._derive_org()
        if self.membership_id and self.store_id and self.store.org_id != self.membership.org_id:
            raise ValidationError(
                {"store": _("That store belongs to a different organization.")}
            )
