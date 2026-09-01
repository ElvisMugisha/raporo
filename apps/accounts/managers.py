"""User creation. Every path validates before it writes."""

from django.contrib.auth.models import BaseUserManager
from django.utils.translation import gettext_lazy as _

from common.managers import NoHardDeleteQuerySet


class UserManager(BaseUserManager.from_queryset(NoHardDeleteQuerySet)):
    """Creates users with the three mandatory identifiers.

    `create_user` runs `full_clean()` so the phone format, the language choice
    and case-insensitive uniqueness are enforced on the programmatic path too,
    not only on forms. The database constraints remain the arbiter, so callers
    must still be ready for `IntegrityError` under concurrency.

    Built on `NoHardDeleteQuerySet`: `User.objects.filter(...).delete()` is
    refused like every other table's. Deactivation is `is_active = False`.
    """

    def create_user(self, username, email, phone, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(username, email, phone, password, **extra_fields)

    def create_superuser(self, username, email, phone, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError(_("A superuser must have is_staff=True."))
        if extra_fields.get("is_superuser") is not True:
            raise ValueError(_("A superuser must have is_superuser=True."))
        return self._create_user(username, email, phone, password, **extra_fields)

    def _create_user(self, username, email, phone, password, **extra_fields):
        username = (username or "").strip()
        email = (email or "").strip()
        phone = (phone or "").strip()
        if not username:
            raise ValueError(_("A username is required."))
        if not email:
            raise ValueError(_("An email address is required: it is the password-reset channel."))
        if not phone:
            raise ValueError(_("A phone number is required."))

        user = self.model(
            username=self.model.normalize_username(username),
            email=self.normalize_email(email),
            phone=phone,
            **extra_fields,
        )
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.full_clean()
        user.save(using=self._db)
        return user
