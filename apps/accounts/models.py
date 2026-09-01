"""accounts.User - the project's `AUTH_USER_MODEL`.

Three identifiers, all unique and all usable to log in (the auth backend
arrives in a later task): username, email, phone. Email is mandatory because
it is the password-reset channel for every user, phone-first ones included.
"""

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from apps.accounts.managers import UserManager
from common.managers import HardDeleteForbidden
from common.validators import (
    phone_validator,
    username_validator,
    validate_username_not_numeric,
)


class Language(models.TextChoices):
    """Kept in step with `settings.LANGUAGES` (a test asserts they match)."""

    EN = "en", _("English")
    RW = "rw", _("Ikinyarwanda")
    FR = "fr", _("Français")


class User(AbstractBaseUser, PermissionsMixin):
    username = models.CharField(
        _("username"),
        max_length=60,
        unique=True,
        validators=[username_validator, validate_username_not_numeric],
        help_text=_("Letters, digits and . + - _ only. Not an email address or a number."),
    )
    email = models.EmailField(
        _("email address"),
        unique=True,
        help_text=_("Required: password-reset links are sent here."),
    )
    phone = models.CharField(
        _("phone number"),
        max_length=15,
        unique=True,
        validators=[phone_validator],
        help_text=_("Country code first, digits only, without +. For example 250788123456."),
    )
    language = models.CharField(
        _("language"),
        max_length=2,
        choices=Language,
        default=Language.EN,
    )
    is_active = models.BooleanField(
        _("active"),
        default=True,
        help_text=_("Unselect this instead of deleting an account."),
    )
    is_staff = models.BooleanField(_("staff status"), default=False)
    date_joined = models.DateTimeField(_("date joined"), auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "username"
    EMAIL_FIELD = "email"
    # `createsuperuser` must ask for both: email is the reset channel, phone is
    # a login identifier.
    REQUIRED_FIELDS = ["email", "phone"]

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        # `objects` is also the base manager so the internal paths
        # (`_base_manager`, related descriptors) cannot hard-delete a user
        # either. It filters nothing, so it is safe in that role.
        base_manager_name = "objects"
        # Both an exact `unique=True` (on the fields) and the functional
        # constraints below are kept on purpose: an expression-only
        # UniqueConstraint reports under NON_FIELD_ERRORS, so the plain unique
        # is what gives a form its per-field "already taken" message, while the
        # functional one is what actually makes uniqueness case-insensitive.
        constraints = [
            # Case-insensitive uniqueness without the citext extension: a
            # functional unique index, which Django also validates in
            # `full_clean()`.
            models.UniqueConstraint(
                Lower("username"),
                name="accounts_user_username_ci_unique",
                violation_error_message=_("A user with that username already exists."),
            ),
            models.UniqueConstraint(
                Lower("email"),
                name="accounts_user_email_ci_unique",
                violation_error_message=_("A user with that email address already exists."),
            ),
        ]

    def __str__(self):
        return self.username

    def clean(self):
        super().clean()
        self.username = self.normalize_username((self.username or "").strip())
        self.email = type(self).objects.normalize_email((self.email or "").strip())
        self.phone = (self.phone or "").strip()

    def get_full_name(self):
        return self.username

    def get_short_name(self):
        return self.username

    def delete(self, *args, **kwargs):
        """No hard deletes anywhere, users included.

        Deactivation is `is_active = False`. Erasing or anonymising an account
        is a privacy decision (Law 058/2021) that has not been made yet, so
        there is deliberately no path here.
        """
        raise HardDeleteForbidden(
            "User: hard delete is forbidden; set is_active=False to deactivate."
        )
