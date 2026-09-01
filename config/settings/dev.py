"""Local development settings: loosened cookie security for plain HTTP."""

from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
