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
from common.models import PublicIdModel
from common.validators import (
    PHONE_INPUT_MAX_LENGTH,
    normalize_phone,
    phone_validator,
    username_validator,
    validate_username_not_numeric,
)


class PhoneField(models.CharField):
    """A phone column that canonicalises on the way in, on every path.

    `common.validators.normalize_phone()` is the single implementation; this
    field is the single place it is wired, because "normalise in the service"
    or "normalise in the form" is a guard that the next `objects.create()`
    walks straight past - and for a *unique* column that is not a cosmetic
    miss, it is two rows for one human phone.

    Three hooks, chosen for what they cover and not for symmetry:

    * `to_python()` - `Model.clean_fields()` calls it and assigns the result
      back, so `full_clean()`, every ModelForm and `loaddata`'s deserializer
      normalise *before* the field validators run, before `Model.clean()`, and
      before `validate_unique()` builds its query. This is why the guard runs
      early enough: `full_clean()` runs `clean_fields()` first, so anything
      later - including the old `clean()` hook - was too late to be believed.
    * `pre_save()` - reached from `Model.save_base()`, so `save()`,
      `objects.create()`, `create_user()`, `bulk_create()`, `loaddata` and the
      admin all normalise even though none of them calls `full_clean()`. It
      writes the canonical value back onto the instance, so the row and the
      object in memory never disagree.
    * `get_prep_value()` - the SQL floor: `QuerySet.update()`,
      `bulk_update()` and `filter(phone=...)`. The lookup half is what lets
      task 5's login find the account someone registered as `0788123456` when
      they type `+250788123456`.

    Not covered, and worth knowing: raw SQL, `psql`, and any pattern lookup
    (`phone__startswith=...`), which Django hands to the database without
    `get_prep_value()`. Closing the raw-SQL gap needs a database-level CHECK
    that the column is `NULL` or matches the canonical form; that is
    `database-engineer`'s call and belongs with the P-4 erasure columns.

    Lives here rather than in `common/` because `accounts` is its only user
    today. A shipped migration names this class by import path, so if a second
    model needs a phone, move the class and leave an alias behind - do not let
    `apps/accounts/migrations/0002` lose its import.
    """

    def to_python(self, value):
        value = super().to_python(value)
        if value is None or value == "":
            # Emptiness is `blank` / `null`'s business: `Field.validate()`
            # raises the standard required-field error a form knows how to
            # render, which is the right message for "you left this out".
            return value
        return normalize_phone(value)

    def canonical_or_none(self, value):
        """What may be written to the column: a canonical number, or `NULL`.

        `None` passes through because that is erasure. `""` does not: an empty
        string is "left out", not "erased", and it would satisfy both the
        column and its unique index while being nobody's phone number - which
        is how `objects.create(phone="")` slipped past NOT NULL before.
        """
        if value is None:
            return None
        return normalize_phone(value)

    def pre_save(self, model_instance, add):
        value = self.canonical_or_none(getattr(model_instance, self.attname))
        setattr(model_instance, self.attname, value)
        return value

    def get_prep_value(self, value):
        return super().get_prep_value(self.canonical_or_none(value))

    def formfield(self, **kwargs):
        # The stored form is at most 15 characters, but `+250 788 123 456` is
        # 16: a form sized to the column would reject the input before the
        # normaliser ever saw it. The column stays at 15 because that is what
        # gets stored.
        kwargs.setdefault("max_length", PHONE_INPUT_MAX_LENGTH)
        return super().formfield(**kwargs)


class Language(models.TextChoices):
    """Kept in step with `settings.LANGUAGES` (a test asserts they match)."""

    EN = "en", _("English")
    RW = "rw", _("Ikinyarwanda")
    FR = "fr", _("Français")


class User(PublicIdModel, AbstractBaseUser, PermissionsMixin):
    """Carries `PublicIdModel` explicitly: it descends from
    `AbstractBaseUser`, so it inherits none of `common`'s bases, and
    member-management URLs need a user identifier that is not the username -
    which is user-chosen, mutable, and a login credential."""

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
    # `null=True` is not "a phone is optional". It is the one state a phone
    # column needs that no phone number can express: *erased*. Law 058/2021
    # gives a data subject the right to erasure, and every value the canonical
    # form allows is a plausible real subscriber's number somewhere, so
    # synthesising one on erasure would attribute a closed account to a
    # stranger - and, the column being unique, would eventually collide with
    # them. Django's own advice against `null` on a string field carves out
    # exactly this case: a unique column that must allow more than one "no
    # value" (PostgreSQL treats NULLs as distinct in a unique index).
    #
    # Required-ness never rested on NOT NULL and does not now: `blank=False`
    # is what makes `full_clean()` and every ModelForm demand a number, and
    # `UserManager._create_user()` refuses an empty one. NOT NULL never
    # guaranteed a *usable* phone anyway - `objects.create(phone="")` satisfied
    # it - so the guarantee was always validation, and `PhoneField` now refuses
    # `""` on the write paths that skip validation too. Only a deliberate
    # `phone = None` writes NULL, which is precisely what erasure is.
    phone = PhoneField(
        _("phone number"),
        max_length=15,
        unique=True,
        null=True,
        validators=[phone_validator],
        help_text=_(
            "Write it as 0788123456. A number from another country needs its "
            "country code, as +254712345678."
        ),
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
        # `phone` is deliberately absent: `clean()` was too late for it.
        # `full_clean()` runs `clean_fields()` first, so a number written the
        # way Rwandans write it (`0788123456`) failed the field validator
        # before this method could canonicalise it - and `objects.create()`
        # never gets here at all. `PhoneField` owns it now, from `to_python()`,
        # `pre_save()` and `get_prep_value()`.

    def save(self, **kwargs):
        """Canonicalise the phone before `save_base()` opens a transaction.

        `PhoneField.pre_save()` would catch it a moment later, but by then we
        are inside the `atomic(savepoint=False)` block `save_base()` opens: a
        `ValidationError` escaping from there marks the *caller's* transaction
        for rollback, so a service that catches it cannot run another query and
        gets a `TransactionManagementError` instead of the validation error it
        handled. Raising here keeps a refused write a plain refusal.

        The field's hooks stay as the floor under this: `bulk_create()` and
        `QuerySet.update()` never come through `save()` at all.
        """
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "phone" in set(update_fields):
            self.phone = self._meta.get_field("phone").canonical_or_none(self.phone)
        return super().save(**kwargs)

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
