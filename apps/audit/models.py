"""The append-only audit trail.

No soft delete, no update path: a row is written once and then only read.
`save()` and the queryset writers refuse anything else, so an accidental
`row.save()` after touching an attribute fails loudly instead of rewriting
history.
"""

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from common.models import PublicIdModel

#: Dotted, lowercase verb: `user.created`, `sale.below_floor_override`.
ACTION_REGEX = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$"
ACTION_MAX_LENGTH = 80

action_validator = RegexValidator(
    regex=ACTION_REGEX,
    message=_("An audit action looks like `user.created`: lowercase, dot-separated."),
    code="invalid_action",
)


class AppendOnlyError(NotImplementedError):
    """Raised on any attempt to change or remove an audit row."""


class AppendOnlyQuerySet(models.QuerySet):
    """No UPDATE, no DELETE, and no `bulk_create(update_conflicts=True)` -
    which is an UPDATE wearing an INSERT's clothes."""

    def update(self, **kwargs):
        raise AppendOnlyError("Audit rows are append-only: they cannot be updated.")

    def delete(self):
        raise AppendOnlyError("Audit rows are append-only: they cannot be deleted.")

    delete.queryset_only = True

    def _raw_delete(self, using):
        raise AppendOnlyError("Audit rows are append-only: they cannot be deleted.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        if update_conflicts:
            raise AppendOnlyError(
                "Audit rows are append-only: bulk_create(update_conflicts=True) would "
                "overwrite existing rows."
            )
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_conflicts=update_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class AuditLog(PublicIdModel):
    """One append-only row per recorded action.

    Carries `PublicIdModel` explicitly: an audit row is neither soft-deletable
    nor audited, so it inherits nothing that would bring the identifier along,
    and a future audit screen links to rows by URL. `target_id` staying a raw
    `BigIntegerField` is fine because it never appears in one.
    """

    org = models.ForeignKey(
        "orgs.Organization",
        verbose_name=_("organization"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    store = models.ForeignKey(
        "orgs.Store",
        verbose_name=_("store"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("actor"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        help_text=_("Empty when the system acted on its own."),
    )
    action = models.CharField(
        _("action"),
        max_length=ACTION_MAX_LENGTH,
        validators=[action_validator],
        db_index=True,
    )
    target_type = models.CharField(_("target type"), max_length=100, blank=True)
    target_id = models.BigIntegerField(_("target id"), null=True, blank=True)
    changes = models.JSONField(
        _("changes"), default=dict, blank=True, encoder=DjangoJSONEncoder
    )
    ip = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    at = models.DateTimeField(_("at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("audit entry")
        verbose_name_plural = _("audit entries")
        ordering = ("-at", "-id")
        # `objects` is also the base manager: without this, Django's internal
        # `_base_manager` is a plain manager and
        # `AuditLog._base_manager.filter(...).delete()` erases the trail.
        base_manager_name = "objects"
        indexes = [
            models.Index(fields=["org", "at"], name="audit_org_at_idx"),
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
        ]

    objects = AppendOnlyQuerySet.as_manager()

    def __str__(self):
        return f"{self.action} @ {self.at:%Y-%m-%d %H:%M:%S}"

    def save(self, **kwargs):
        """Insert only.

        `_state.adding` alone is not enough: it is still True on an instance
        constructed with an explicit `pk`, and Django's `_save_table` then takes
        the UPDATE branch, silently rewriting an existing row. Both conditions
        are checked and the insert is forced.
        """
        if not self._state.adding or self.pk is not None:
            raise AppendOnlyError(
                "Audit rows are append-only: write a new row instead of editing this one."
            )
        kwargs["force_insert"] = True
        return super().save(**kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("Audit rows are append-only: they cannot be deleted.")
