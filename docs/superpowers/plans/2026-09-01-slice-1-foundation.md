# Slice 1 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dockerized Django 6.1 project where a visitor registers with username+email+phone (creating their organization + first store as Owner), members join only via single-use invite links, login works with username/email/phone + password behind rate limiting, an always-present 2FA stage, and emailed non-enumerating password reset, custom org roles gate every action, every action is audit-logged, nothing hard-deletes, and the whole UI ships in EN/RW/FR with a header switcher.

**Architecture:** Modular monolith per `docs/superpowers/specs/2026-09-01-raporo-architecture-and-schema-design.md` (READ IT FIRST — this plan implements it). Service layer owns all state changes; views stay thin; templates + HTMX (vendored) render everything.

**Tech Stack:** Python 3.13 · Django 6.1 (non-negotiable) · PostgreSQL 17 · argon2-cffi (hashing) · pyotp (TOTP) · cryptography (secret encryption at rest) · pytest + pytest-django · ruff · Docker/compose. No DRF, no Node.

**Spec:** `docs/superpowers/specs/2026-09-01-raporo-architecture-and-schema-design.md`

## Global Constraints

- Every package must support Django 6.1; pin exact versions in `requirements.txt`.
- All state changes go through `apps/<app>/services.py`; business logic in a view fails review (ADR 0007).
- No hard deletes anywhere; soft-delete via `common.models.SoftDeleteModel`.
- Every service writes an `audit.AuditLog` row with actor.
- Login/reset flows must not reveal whether an identifier exists (no enumeration).
- Phone format: digits with country code, no `+`: regex `^[1-9][0-9]{7,14}$`.
- Languages: `en` (default), `rw`, `fr`; every user-facing string wrapped in gettext from the first commit.
- **Commits are Elvis's action** (agent git writes are denied). Each "Commit" step = hand Elvis the exact command and wait.
- Store timestamps UTC (`USE_TZ=True`); org display timezone default `Africa/Kigali`.

---

### Task 0: Project scaffold, Docker, pytest, healthcheck

**Files:**

- Create: `requirements.txt`, `manage.py`, `config/__init__.py`, `config/settings/{__init__,base,dev,prod}.py`, `config/urls.py`, `config/asgi.py`, `config/wsgi.py`, `docker/Dockerfile`, `compose.yaml`, `.env.example`, `pytest.ini`, `ruff.toml`, `apps/__init__.py`, `common/__init__.py`
- Test: `tests/test_healthz.py`

**Interfaces:**

- Produces: `config.settings.base` with `AUTH_USER_MODEL = "accounts.User"` (declared now, model in Task 2); `/healthz` returning 200 `{"status":"ok"}`; `pytest` runs against Postgres from compose.

- [ ] **Step 1: requirements.txt (pinned at latest satisfying Django 6.1; resolve exact pins at execution time with `pip index versions <pkg>`)**

```text
Django==6.1.*
psycopg[binary]==3.*
argon2-cffi==25.*
pyotp==2.*
cryptography==46.*
pytest==9.*
pytest-django==4.*
ruff==0.*
```

- [ ] **Step 2: `django-admin startproject config .` then split settings.** `config/settings/base.py` keys that differ from Django defaults:

```python
from pathlib import Path
import os
BASE_DIR = Path(__file__).resolve().parent.parent.parent
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "apps.accounts", "apps.orgs", "apps.audit",
]
AUTH_USER_MODEL = "accounts.User"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.Argon2PasswordHasher"]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": os.environ.get("POSTGRES_DB", "raporo"),
    "USER": os.environ.get("POSTGRES_USER", "raporo"),
    "PASSWORD": os.environ["POSTGRES_PASSWORD"],
    "HOST": os.environ.get("POSTGRES_HOST", "db"), "PORT": 5432,
}}
LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("rw", "Ikinyarwanda"), ("fr", "Français")]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True; USE_TZ = True; TIME_ZONE = "UTC"
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [{  # default engine block plus:
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "django.template.context_processors.i18n",
    ]},
}]
STATIC_URL = "static/"; STATICFILES_DIRS = [BASE_DIR / "static"]
SESSION_COOKIE_SECURE = True; SESSION_COOKIE_HTTPONLY = True; SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = True
ROOT_URLCONF = "config.urls"; WSGI_APPLICATION = "config.wsgi.application"
```

`dev.py`: `from .base import *` then `DEBUG=True`, `SESSION_COOKIE_SECURE=False`, `CSRF_COOKIE_SECURE=False`, `ALLOWED_HOSTS=["*"]`. `prod.py`: `from .base import *` then `ALLOWED_HOSTS` from env, `SECURE_SSL_REDIRECT=True`, `SECURE_HSTS_SECONDS=31536000`, `SECURE_HSTS_INCLUDE_SUBDOMAINS=True`, `SECURE_HSTS_PRELOAD=True`, `SECURE_REFERRER_POLICY="same-origin"`, `SECURE_CONTENT_TYPE_NOSNIFF=True`.

- [ ] **Step 3: healthz + urls**

```python
# config/urls.py
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

def healthz(request):
    return JsonResponse({"status": "ok"})

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", healthz),
    path("i18n/", include("django.conf.urls.i18n")),
]
```

- [ ] **Step 4: Docker.** `docker/Dockerfile` (multi-stage, non-root):

```dockerfile
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN useradd -m raporo && chown -R raporo:raporo /app
USER raporo
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
```

`compose.yaml`:

```yaml
services:
  web:
    build: { context: ., dockerfile: docker/Dockerfile }
    env_file: .env
    environment: { DJANGO_SETTINGS_MODULE: config.settings.dev }
    volumes: [".:/app"]
    ports: ["8000:8000"]
    depends_on: { db: { condition: service_healthy } }
  db:
    image: postgres:17
    env_file: .env
    healthcheck:
      {
        test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER"],
        interval: 3s,
        retries: 10,
      }
    volumes: ["pgdata:/var/lib/postgresql/data"]
volumes: { pgdata: {} }
```

`.env.example`: `DJANGO_SECRET_KEY=change-me`, `POSTGRES_DB=raporo`, `POSTGRES_USER=raporo`, `POSTGRES_PASSWORD=change-me`, `POSTGRES_HOST=db`. `pytest.ini`: `[pytest] DJANGO_SETTINGS_MODULE=config.settings.dev` + `python_files = test_*.py`.

- [ ] **Step 5: Failing test → pass.** `tests/test_healthz.py`:

```python
def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
```

Run `docker compose up -d db && docker compose run --rm web pytest -v` → expect PASS (test written before wiring healthz counts as the failing step if executed in order). `ruff check .` clean.

- [ ] **Step 6: Elvis commits:** `git add -A && git commit -m "feat: dockerized Django 6.1 scaffold with healthcheck, settings split, pytest"`

---

### Task 1: common — SoftDelete, Audited, StoreScoped bases

**Files:**

- Create: `common/models.py`, `common/managers.py`, `apps/common_tests/` → `tests/test_common_bases.py` (bases are tested through a tiny concrete model defined in the test app `tests/testapp/`)

**Interfaces:**

- Produces: `common.models.AuditedModel` (fields `created_at/by`, `updated_at/by`); `common.models.SoftDeleteModel` with `objects` (live only), `all_objects`, and `soft_delete(by)`; `common.models.StoreScopedModel` (abstract, `store` FK declared in Task 3 consumers via `store = models.ForeignKey("orgs.Store", ...)` — the base holds the manager contract `Model.objects.for_store(store)`; calling `.all()` without `for_store` raises `common.managers.UnscopedQueryError`.

- [ ] **Step 1: Failing tests** — soft-deleted row disappears from `objects`, remains in `all_objects`; `soft_delete` stamps `deleted_at/by`; unscoped query raises; `for_store` filters.

```python
def test_soft_delete_hides_from_default_manager(db, actor, thing):
    thing.soft_delete(by=actor)
    assert type(thing).objects.count() == 0
    assert type(thing).all_objects.count() == 1

def test_unscoped_query_raises(db):
    with pytest.raises(UnscopedQueryError):
        list(ScopedThing.objects.all())
```

- [ ] **Step 2: Run → FAIL (models missing).**
- [ ] **Step 3: Implement**

```python
# common/managers.py
class UnscopedQueryError(Exception):
    """Raised when a store-scoped model is queried without for_store()."""

class SoftDeleteQuerySet(models.QuerySet):
    def delete(self):  # bulk delete becomes bulk soft-delete
        return super().update(deleted_at=timezone.now())

class SoftDeleteManager(models.Manager):
    def get_queryset(self):
        return SoftDeleteQuerySet(self.model).filter(deleted_at__isnull=True)

class StoreScopedManager(SoftDeleteManager):
    def get_queryset(self):
        qs = super().get_queryset()
        if not getattr(qs, "_scoped", False):
            class Guard(qs.__class__):
                def _fetch_all(self):
                    raise UnscopedQueryError(self.model.__name__)
            qs.__class__ = Guard
        return qs
    def for_store(self, store):
        qs = SoftDeleteQuerySet(self.model).filter(deleted_at__isnull=True, store=store)
        qs._scoped = True
        return qs
```

```python
# common/models.py
class AuditedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    class Meta: abstract = True

class SoftDeleteModel(models.Model):
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    objects = SoftDeleteManager()
    all_objects = models.Manager()
    class Meta: abstract = True
    def soft_delete(self, by):
        self.deleted_at = timezone.now(); self.deleted_by = by
        self.save(update_fields=["deleted_at", "deleted_by"])
    def delete(self, *a, **kw):
        raise NotImplementedError("Hard delete is forbidden; use soft_delete(by=).")

class StoreScopedModel(SoftDeleteModel, AuditedModel):
    objects = StoreScopedManager()
    class Meta: abstract = True
```

- [ ] **Step 4: Run → PASS. `ruff check .` clean.**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: common bases - soft delete, audit stamps, store-scoped manager guard"`

---

### Task 2: accounts.User + audit app

**Files:**

- Create: `apps/accounts/{__init__,apps,models,managers}.py`, `apps/audit/{__init__,apps,models,services}.py`, migrations
- Test: `tests/test_user_model.py`, `tests/test_audit.py`

**Interfaces:**

- Produces: `accounts.User(username, email, phone, language, password)` — email REQUIRED (reset channel) — `USERNAME_FIELD="username"`, `REQUIRED_FIELDS=["email", "phone"]`; `audit.services.record(action, actor=None, org=None, store=None, target=None, changes=None, ip=None)` returning the `AuditLog` row. `AuditLog` fields per spec §4-audit.

- [ ] **Step 1: Failing tests** — phone regex rejects `+250...` and accepts `250788123456`; duplicate username/phone rejected; language defaults `en`; argon2 hash prefix; `audit.record` writes a row with actor and JSON changes.

```python
def test_phone_rejects_plus(db):
    with pytest.raises(ValidationError):
        User(username="a", phone="+250788123456").full_clean()

def test_password_is_argon2(db, user):
    assert user.password.startswith("argon2")

def test_audit_record(db, user):
    row = audit_services.record("user.created", actor=user, target=user, changes={"username": "a"})
    assert row.action == "user.created" and row.actor == user and row.target_type == "accounts.User"
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement**

```python
# apps/accounts/models.py
phone_validator = RegexValidator(r"^[1-9][0-9]{7,14}$",
    _("Enter the phone with country code, digits only, without +."))

class User(AbstractBaseUser, PermissionsMixin):
    username = models.CharField(max_length=60, unique=True)
    email = models.EmailField(unique=True)  # required: the password-reset channel for everyone
    phone = models.CharField(max_length=15, unique=True, validators=[phone_validator])
    language = models.CharField(max_length=2, default="en",
        choices=[("en", "English"), ("rw", "Ikinyarwanda"), ("fr", "Français")])
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(auto_now_add=True)
    objects = UserManager()   # managers.py: create_user/create_superuser normalizing email->None if blank
    USERNAME_FIELD = "username"; REQUIRED_FIELDS = ["phone"]
```

```python
# apps/audit/models.py  (append-only: no soft delete)
class AuditLog(models.Model):
    org = models.ForeignKey("orgs.Organization", null=True, on_delete=models.PROTECT, related_name="+")
    store = models.ForeignKey("orgs.Store", null=True, on_delete=models.PROTECT, related_name="+")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    action = models.SlugField(max_length=80)
    target_type = models.CharField(max_length=100, blank=True)
    target_id = models.BigIntegerField(null=True)
    changes = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    class Meta:
        indexes = [models.Index(fields=["org", "at"]), models.Index(fields=["target_type", "target_id"])]

# apps/audit/services.py
def record(action, *, actor=None, org=None, store=None, target=None, changes=None, ip=None):
    return AuditLog.objects.create(
        action=action, actor=actor, org=org, store=store,
        target_type=f"{target._meta.app_label}.{type(target).__name__}" if target else "",
        target_id=getattr(target, "pk", None), changes=changes or {}, ip=ip)
```

(`UserManager.create_user` requires username, email, phone. Note: `orgs` models don't exist yet — declare the FKs as strings; run `makemigrations accounts audit` only AFTER Task 3 creates orgs, or temporarily omit org/store FKs and add them in Task 3's migration. Choose the former: write models now, generate ALL migrations in Task 3 Step 4.)

- [ ] **Step 4: Run → PASS (after Task 3 migrations; keep tests marked `xfail(strict=False)` only until then if executing strictly in order — remove the mark in Task 3).**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: custom User (username/email/phone, language) and append-only audit log"`

---

### Task 3: orgs — Organization, Store, Role, Membership, StoreAccess (+ permission catalog)

**Files:**

- Create: `apps/orgs/{__init__,apps,models,permissions}.py`, all migrations (accounts, audit, orgs)
- Test: `tests/test_orgs_models.py`

**Interfaces:**

- Produces: models per spec §4-orgs; `orgs.permissions.PERMISSIONS` (frozenset of codes: `member.manage`, `role.manage`, `invite.create`, `store.manage`, `sale.record`, `sale.below_floor_override`, `stock.restock`, `stock.write_off`, `expense.record`, `cycle.manage`, `report.generate`, `audit.view`) and `PRESETS = {"Owner": PERMISSIONS, "Manager": {...}, "Seller": {"sale.record"}}`; `Role.has(code)`.

- [ ] **Step 1: Failing tests** — unique (org,name) store; role rejects unknown permission code on `full_clean`; membership unique per (user, org); presets contain expected codes.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** (fields exactly per spec §4; `Role.clean()` validates `set(self.permissions) <= PERMISSIONS`; `Organization.base_currency` default `"RWF"`, `timezone` default `"Africa/Kigali"`, `brand` JSONField default dict). `Store`, `Role`, `Membership`, `StoreAccess` inherit `SoftDeleteModel + AuditedModel` (org-level, not store-scoped). Then `makemigrations accounts audit orgs && migrate`.
- [ ] **Step 4: Run all tests (incl. Task 2's un-xfailed) → PASS.**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: orgs domain - organization, stores, roles with permission catalog, memberships"`

---

### Task 4: orgs services — register_owner, create_store (1–5 limit), check/require permission

**Files:**

- Create: `apps/orgs/services.py`, `apps/orgs/exceptions.py`
- Test: `tests/test_orgs_services.py`

**Interfaces:**

- Produces: `register_owner(*, username, email, phone, password, org_name, language="en") -> (user, org, store)` (atomic: user + org + default store "Main" + Owner preset role + membership + store access + audit rows); `create_store(org, actor, name) -> Store` raising `StoreLimitReached` beyond 5; `check_permission(user, org, code) -> bool`; `require_permission(user, org, code)` raising `PermissionDenied`; `grant_store_access(membership, store, actor)`.

- [ ] **Step 1: Failing tests**

```python
def test_register_owner_creates_everything(db):
    user, org, store = services.register_owner(username="eva", email="eva@example.rw", phone="250788000001",
                                               password="S3cure!pass", org_name="Eva Shop")
    m = Membership.objects.get(user=user, org=org)
    assert m.role.name == "Owner" and store.name == "Main"
    assert AuditLog.objects.filter(action="org.created").exists()

def test_store_limit_is_five(db, org_with_owner):
    org, owner = org_with_owner
    for i in range(4):  # "Main" already exists
        services.create_store(org, owner, f"S{i}")
    with pytest.raises(StoreLimitReached):
        services.create_store(org, owner, "S5")

def test_permission_denied_without_code(db, org_with_seller):
    org, seller = org_with_seller
    with pytest.raises(PermissionDenied):
        services.require_permission(seller, org, "store.manage")
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** `create_store` body (the race-safe count):

```python
@transaction.atomic
def create_store(org, actor, name):
    require_permission(actor, org, "store.manage")
    org_locked = Organization.objects.select_for_update().get(pk=org.pk)
    if Store.objects.filter(org=org_locked).count() >= 5:
        raise StoreLimitReached(_("An organization can have at most 5 stores."))
    store = Store.objects.create(org=org_locked, name=name, created_by=actor)
    audit.record("store.created", actor=actor, org=org_locked, store=store, target=store)
    return store
```

- [ ] **Step 4: Run → PASS** (include a `TransactionTestCase` double-thread race test for the limit).
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: org services - owner registration, 5-store limit, permission checks"`

---

### Task 5: login throttling + multi-identifier auth backend (no enumeration)

**Files:**

- Create: `apps/accounts/backends.py`, `apps/accounts/throttle.py`
- Modify: `config/settings/base.py` (AUTHENTICATION_BACKENDS, CACHES)
- Test: `tests/test_auth_backend.py`, `tests/test_throttle.py`

**Interfaces:**

- Produces: `MultiIdentifierBackend.authenticate(request, identifier, password)` resolving username OR email OR phone, always running one hash verify (constant-time-ish) even for unknown identifiers; `throttle.allow(identifier, ip) -> bool`, `throttle.fail(identifier, ip)`, `throttle.reset(identifier, ip)` — cache counters: 5 fails/identifier/15min and 20 fails/ip/15min → locked.

- [ ] **Step 1: Failing tests** — login by each of the three identifiers works; unknown identifier and wrong password produce the SAME backend result (None) with no exception difference; 6th failure locks; success resets.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Backend core:

```python
class MultiIdentifierBackend(BaseBackend):
    def authenticate(self, request, identifier=None, password=None, **kw):
        if not identifier or not password:
            return None
        q = Q(username=identifier) | Q(phone=identifier)
        if "@" in identifier: q |= Q(email__iexact=identifier)
        user = User.objects.filter(q, is_active=True).first()
        if user is None:
            User().set_password(password)   # burn a hash: uniform timing
            return None
        return user if user.check_password(password) else None
```

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: multi-identifier auth backend with uniform failures and login throttling"`

---

### Task 6: 2FA — TOTP models + services (staged from day one)

**Files:**

- Create: `apps/accounts/twofactor.py` (services), models added to `apps/accounts/models.py` (`TwoFactor`, `RecoveryCode`), migration
- Modify: `config/settings/base.py` (`TWOFACTOR_ENCRYPTION_KEY` from env, Fernet)
- Test: `tests/test_twofactor.py`

**Interfaces:**

- Produces: `enable_totp(user) -> (secret, otpauth_uri)` (stores Fernet-encrypted secret, unconfirmed); `confirm_totp(user, code) -> bool` (sets `confirmed_at`, generates 10 recovery codes, returns them once); `verify_totp(user, code) -> bool` (accepts TOTP or unused recovery code, single-use); `user_has_2fa(user) -> bool` (confirmed only).

- [ ] **Step 1: Failing tests** — enable→confirm with `pyotp.TOTP(secret).now()` flips `user_has_2fa`; wrong code doesn't confirm; recovery code works exactly once; secret at rest in DB is not plaintext (`assert secret not in row.totp_secret`).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** with `pyotp.random_base32()`, `cryptography.fernet.Fernet(settings.TWOFACTOR_ENCRYPTION_KEY)`, recovery codes = 10 × `secrets.token_hex(5)` stored as SHA-256 hashes.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: TOTP 2FA with encrypted secrets and one-time recovery codes"`

---

### Task 7: Invites — model + create/accept services (atomic single-use)

**Files:**

- Create: `apps/orgs/models.py` additions (`Invite`), `apps/orgs/services.py` additions, migration
- Test: `tests/test_invites.py`

**Interfaces:**

- Produces: `create_invite(org, actor, role, stores, days=7, contact="") -> (invite, raw_token)` — raw token `secrets.token_urlsafe(32)`, stored as SHA-256 hash, requires `invite.create` permission; `accept_invite(raw_token, user) -> Membership` — valid iff not used/revoked/expired, creates Membership + StoreAccess rows atomically and consumes the token with `SELECT … FOR UPDATE`; `revoke_invite(invite, actor)`.

- [ ] **Step 1: Failing tests** — accept creates membership scoped to the invite's stores; second accept raises `InviteInvalid`; expired raises; revoked raises; two concurrent accepts → exactly one membership (TransactionTestCase with threads).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Consumption core:

```python
@transaction.atomic
def accept_invite(raw_token, user):
    h = hashlib.sha256(raw_token.encode()).hexdigest()
    inv = (Invite.objects.select_for_update()
           .filter(token_hash=h, used_at__isnull=True, revoked_at__isnull=True,
                   expires_at__gt=timezone.now()).first())
    if inv is None:
        raise InviteInvalid(_("This invite link is not valid."))
    m = Membership.objects.create(user=user, org=inv.org, role=inv.role, created_by=user)
    for s in inv.stores.all():
        StoreAccess.objects.create(membership=m, store=s, created_by=user)
    inv.used_at, inv.used_by = timezone.now(), user
    inv.save(update_fields=["used_at", "used_by"])
    audit.record("invite.accepted", actor=user, org=inv.org, target=inv)
    return m
```

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: single-use expiring revocable invite links with atomic acceptance"`

---

### Task 8: Base template, i18n wiring, header language switcher (HTMX enters here)

**Files:**

- Create: `templates/base.html`, `templates/partials/header.html`, `static/js/htmx.min.js` (vendored, pinned — record version in `.claude/skills/VENDORED.md` style comment at file top is NOT possible in min.js; record it in `docs/ROADMAP.md` standing notes instead), `static/css/tokens.css`, `static/css/app.css`
- Modify: `config/urls.py` (already has `i18n/`)
- Test: `tests/test_i18n_switcher.py`

**Interfaces:**

- Produces: `base.html` blocks `{% block content %}`, `{% block title %}`; header shows language form posting to `{% url 'set_language' %}` with `<select name="language">` of the three languages; logged-in header shows user + logout; `tokens.css` defines `--color-*`, `--space-*`, `--radius-*`, `--font-*` custom properties (ux-designer refines values later — structure now).

- [ ] **Step 1: Failing test** — posting `language=rw` to the switcher makes the next response render in Kinyarwanda (assert a marker string translated in `locale/rw`); user's `language` field is updated when authenticated.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** base.html skeleton:

```html
{% load i18n static %}<!doctype html>
<html lang="{{ LANGUAGE_CODE }}">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{% block title %}Raporo{% endblock %}</title>
    <link rel="stylesheet" href="{% static 'css/tokens.css' %}" />
    <link rel="stylesheet" href="{% static 'css/app.css' %}" />
    <script src="{% static 'js/htmx.min.js' %}" defer></script>
  </head>
  <body hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'>
    {% include "partials/header.html" %}
    <main id="main">{% block content %}{% endblock %}</main>
  </body>
</html>
```

Small signal-receiver: on `user_logged_in` and on set_language POST by an authenticated user, persist `request.user.language`. Translate the first strings; run `django-admin makemessages -l rw -l fr && compilemessages`; commit real `rw`/`fr` translations for every string in this slice (localization gate — Elvis reviews the Kinyarwanda wording).

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: base layout with design tokens, vendored HTMX, EN/RW/FR header switcher"`

---

### Task 9: Registration + login (two-stage) + logout pages

**Files:**

- Create: `apps/accounts/{forms,views,urls}.py`, `templates/accounts/{register,login,login_2fa}.html`, `templates/accounts/partials/{register_form,login_form,twofa_form}.html`
- Modify: `config/urls.py` (`path("", include("apps.accounts.urls"))`)
- Test: `tests/test_auth_flows.py`

**Interfaces:**

- Consumes: `register_owner` (Task 4), `MultiIdentifierBackend` + `throttle` (Task 5), `verify_totp`/`user_has_2fa` (Task 6).
- Produces: URLs `register`, `login`, `login-2fa`, `logout`. Login POST: throttle check → authenticate → if `user_has_2fa`: stash `pre_2fa_user_id` in session, render stage 2; else `django_login` + session rotation. Stage 2 POST: verify code/recovery → complete login. All forms are HTMX fragments re-rendered with errors (422) and full-page fallbacks. Uniform error message: `_("Wrong credentials.")` — never which field.

- [ ] **Step 1: Failing tests** — register creates org+store and logs the user in; login works with each identifier; login with 2FA-enabled user requires stage 2 (session not authenticated until code verified); wrong code stays unauthenticated; lockout after 5 fails renders the throttle message; logout flushes the session; HTMX POST returns just the fragment, plain GET returns full page.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** thin views (each ≤ ~15 lines, all logic in existing services). Enumeration check in review: register form's duplicate-username/phone errors are generic (\_("Registration failed — check your details.")) with the specific hint only where it can't enumerate (client-side format validation).
- [ ] **Step 4: Run → PASS. Run `playwright-cli`: open /register, complete flow, screenshot; open /login, screenshot both stages.**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: register/login (two-stage 2FA-ready)/logout with HTMX fragments"`

---

### Task 10: Invite accept page + member management screen (minimal)

**Files:**

- Create: `apps/orgs/{forms,views,urls}.py`, `templates/orgs/{invite_accept,members}.html` + partials (member row, invite form/modal)
- Test: `tests/test_invite_flows.py`

**Interfaces:**

- Consumes: invite services (Task 7), permission services (Task 4).
- Produces: `GET /invite/<raw_token>` — valid: register-or-login then join; invalid: friendly dead-link page (no reason leakage: expired/revoked/used all read the same). `/org/members` — list members with role; create-invite form (role + stores multi-select) returns a one-time link to copy/share (WhatsApp-ready `https://wa.me/?text=...` share button); revoke button on pending invites. All gated by `invite.create` / `member.manage`.

- [ ] **Step 1: Failing tests** — full journey: owner creates invite → fresh browser registers via link → membership exists with chosen role/stores; invalid token page identical for used/expired/revoked (assert same body); members list denied to Seller role (denial test).
- [ ] **Step 2: Run → FAIL.** — **Step 3: Implement.** — **Step 4: Run → PASS + playwright-cli journey screenshot.**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: invite acceptance flow and member management with shareable links"`

---

### Task 11: Custom error pages + security headers + CI

**Files:**

- Create: `templates/404.html`, `templates/500.html`, `.github/workflows/ci.yml`
- Modify: `config/settings/prod.py` (CSP header via `SecurityMiddleware` extras: `SECURE_CROSS_ORIGIN_OPENER_POLICY`, and `Content-Security-Policy` via a 6-line custom middleware in `common/middleware.py`: `default-src 'self'`)
- Test: `tests/test_error_pages.py`

**Interfaces:**

- Produces: designed 404/500 (base layout, translated, link home); CI: on PR → `ruff check`, `python manage.py makemigrations --check`, `pytest` against a postgres:17 service, `python manage.py compilemessages` (fails on broken translations).

- [ ] **Step 1: Failing tests** — unknown URL renders 404 template (assert marker + 404 status); CSP header present in prod-settings client.
- [ ] **Step 2: FAIL → Step 3: Implement → Step 4: PASS.** ci.yml core:

```yaml
name: CI
on: [pull_request, push]
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      db: {image: postgres:17, env: {POSTGRES_PASSWORD: ci, POSTGRES_USER: raporo, POSTGRES_DB: raporo},
           options: >-
             --health-cmd "pg_isready -U raporo" --health-interval 5s --health-retries 10,
           ports: ["5432:5432"]}
    env: {DJANGO_SECRET_KEY: ci-only, POSTGRES_PASSWORD: ci, POSTGRES_HOST: localhost,
          DJANGO_SETTINGS_MODULE: config.settings.dev, TWOFACTOR_ENCRYPTION_KEY: "${{ github.run_id }}-not-secret"}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.13", cache: pip}
      - run: pip install -r requirements.txt
      - run: ruff check .
      - run: python manage.py makemigrations --check --dry-run
      - run: pytest -v
```

(Fix the TWOFACTOR key to a proper generated test constant at execution; Fernet needs a valid 32-byte urlsafe key — use a checked-in TEST-ONLY key in ci.yml.)

- [ ] **Step 5: Elvis commits:** `git commit -m "feat: designed error pages, CSP, CI pipeline (lint, migrations check, tests)"`

---

### Task 12: Slice gates (pipeline Phase 3.5–5 for this slice)

**Files:** none created — this task runs the team's gates and fixes what they find.

- [ ] **Step 1:** `code-reviewer` agent on the full slice diff → must APPROVE (fix findings first).
- [ ] **Step 2:** `security-engineer` agent full pass (auth, sessions, invites, 2FA, headers, enumeration, tenant scoping groundwork) → no Critical/High open.
- [ ] **Step 3:** `qa-engineer` exploratory pass: double-submits, back-button after logout, invite reuse from two tabs, language switch mid-login.
- [ ] **Step 4:** `privacy-compliance` quick pass (PII inventory: username/email/phone/language; no PII in logs).
- [ ] **Step 5:** Run `/production-readiness` → verdict; `localization-engineer` gate: zero untranslated strings (`makemessages` diff empty).
- [ ] **Step 6:** Update `docs/ROADMAP.md` (slice 1 → ✅ with date) + Elvis commits and merges per human-gate rule.

---

## Self-Review (done)

**Spec coverage:** spec §2 layout→T0; §3.2/3.3 bases→T1; §3.1 guard→T1 (full tenant enforcement exercised from slice 2 when store-scoped business models exist — noted); §4 accounts→T2/T6; §4 orgs→T3/T4/T7; §5 flows→T5/T9/T10; §7 i18n→T8; §8 errors→T11; §9 testing→each task + T12. MoneyFields (§3.5) deliberately deferred to slice 2 (first consumer) — matches YAGNI, recorded here. **Placeholders:** none — every step has code or an exact command. **Type consistency:** service names/signatures cross-checked (register_owner, create_store, create_invite/accept_invite, enable/confirm/verify_totp, throttle.allow/fail/reset, audit.record) — consistent across tasks. **Gap closed 2026-09-01:** Elvis decided reset links go via email for everyone → email is now required at registration (Task 2) and Task 9b implements the full non-enumerating reset flow. No open gaps.

---

### Task 9b: Password reset via emailed link (all users)

**Files:**
- Create: `templates/accounts/{password_reset,password_reset_sent,password_reset_confirm,password_reset_done}.html`, email templates `templates/accounts/email/password_reset.{subject.txt,body.txt}`
- Modify: `apps/accounts/urls.py`, `config/settings/base.py` (`EMAIL_BACKEND` console in dev; prod SMTP from env: `EMAIL_HOST/PORT/HOST_USER/HOST_PASSWORD/USE_TLS`, `DEFAULT_FROM_EMAIL`), `apps/accounts/views.py`
- Test: `tests/test_password_reset.py`

**Interfaces:**
- Consumes: `User.email` (required, Task 2).
- Produces: URLs `password-reset` (request form), `password-reset/sent`, `password-reset/<uidb64>/<token>/` (confirm), `password-reset/done`. Uses Django's `PasswordResetTokenGenerator` (single-use by design: token embeds the password hash) with `PASSWORD_RESET_TIMEOUT = 3600`.

- [ ] **Step 1: Failing tests** — submitting a known email sends exactly one message with a working link; submitting an unknown email returns the SAME "sent" page and sends nothing (non-enumeration: assert identical response bodies); the emailed link sets a new password once and is dead on second use; expired token (freeze time +2h) renders the invalid-link page; user can log in with the new password; throttle: reset requests rate-limited per identifier+IP like login.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** on Django's auth views (subclassed, our templates, translated strings, HTMX fragment variants), reusing `throttle` from Task 5 on the request view. Audit: `audit.record("password.reset_requested"/"password.reset_completed", actor=user)`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Elvis commits:** `git commit -m "feat: non-enumerating emailed password reset with throttling"`
