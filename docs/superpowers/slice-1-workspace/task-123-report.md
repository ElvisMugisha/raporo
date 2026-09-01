# Task 1+2+3 report - foundation data layer (common bases, accounts, audit, orgs)

Branch: `feat/plugins-and-skill-setup` (nothing committed - `git add`/`git commit` are denied for me).
Everything below ran through `docker compose run --rm web ...` from `/home/elvis/projects/2026/personal/raporo`.

## Status: DONE_WITH_CONCERNS

165 tests pass (the pre-existing healthz test included), `ruff check .` is clean, no missing
migrations under either settings module, and `manage.py check` is silent. The concerns are all
carry-forward items for later tasks, not open defects here - see the last two sections.

---

## 1. What I built

### `common/` (plain top-level package, no models of its own)

- **`common/managers.py`** - the query-layer enforcement.
  - `UnscopedQueryError(Exception)`, `HardDeleteForbidden(NotImplementedError)`.
  - `ScopedQuery(sql.Query)`: refuses to build a compiler until a store has been pinned. This is
    where I departed from the brief's sketch (details in section 5): the guard sits on the SQL
    query rather than on `QuerySet._fetch_all`, so it also covers `count()`, `exists()`,
    `aggregate()`, `iterator()`, `values_list()`, `in_bulk()`, `explain()` **and a store-scoped
    queryset used as a subquery inside somebody else's query**. The brief's `_fetch_all` override
    caught none of those.
  - `SoftDeleteQuerySet`: `delete()` and `_raw_delete()` raise; `soft_delete(by=...)` bulk-stamps
    and returns the row count. Both are marked `queryset_only`, so `Model.objects.soft_delete()`
    never exists (a manager-level bulk soft-delete of every live row is not an API I want lying
    around).
  - `ScopedQuerySet(SoftDeleteQuerySet)`: `for_store(store)`, `for_stores([...])`, plus a scope
    check on `update()` (Django swaps the query class for `UpdateQuery`, so the compiler hook
    cannot see writes).
  - `SoftDeleteManager`, `StoreScopedManager`. The scoped manager also refuses `raw()` - raw SQL
    cannot be scope-checked, and `all_objects.raw()` remains as the grep-able escape hatch.
  - `_store_pk()` rejects `None` (which would silently produce `WHERE store_id IS NULL` - a query
    that *looks* scoped and returns nothing), rejects unsaved stores, rejects non-positive pks, and
    rejects instances of other models (an `Organization` has a pk too; taking it would scope by the
    wrong id).

- **`common/models.py`** - `AuditedModel`, `SoftDeleteModel`, `StoreScopedModel`.
  - `soft_delete(by=...)` is keyword-only and **idempotent**: a second call returns `False` and
    leaves the original actor/timestamp intact, so an at-least-once caller cannot rewrite history.
    It also stamps `updated_at`/`updated_by` when the model is audited, and refuses to run on an
    unsaved instance.
  - `StoreScopedModel` declares the **`store` FK itself** (not the consumers, as the brief had it):
    a store-scoped model now cannot be written without its store pointer. `related_name="+"`, so
    there is exactly one way in - `Model.objects.for_store(store)` - and no soft-delete-blind
    `store.product_set.all()` exists to be misused. `PROTECT` still fires through hidden relations.
  - `Meta.default_manager_name = "all_objects"`: Django itself uses `_default_manager` for unique
    validation, the admin and forms. With the guarded manager there, `full_clean()` on any
    store-scoped model would raise `UnscopedQueryError` - a trap that would get "fixed" by deleting
    the guard. There is a test for exactly this (`test_django_internals_use_an_unguarded_default_manager`).

- **`common/checks.py` + `common/apps.py`** (added, not in the brief) - Django system checks
  `common.E001/E002/E003`. The one hole left in the default-manager arrangement is a concrete
  model that declares its own `objects`; that model would silently lose the guard, or make the
  guarded manager the default and break `full_clean()`. The check turns both into a startup error,
  and also asserts the `store` FK still points at a non-nullable `orgs.Store`. `common` is now in
  `INSTALLED_APPS` (abstract models only, so no migrations) purely so the checks run on every
  `check` / `migrate` / test-database build.

- **`common/validators.py`** - `phone_validator` (`^[1-9][0-9]{7,14}$`), `currency_code_validator`
  (`^[A-Z]{3}$`), `validate_timezone` (against a cached `zoneinfo.available_timezones()`; a typo
  in an org's timezone would silently shift every reporting period, so it is validated, not trusted).

### `apps/accounts/`

- `User(AbstractBaseUser, PermissionsMixin)`: `username` (60, `UnicodeUsernameValidator`), `email`
  (required, the password-reset channel), `phone` (15, digits with country code, no `+`),
  `language` (`Language` TextChoices, default `en`), `is_active`, `is_staff`, `date_joined`.
  `USERNAME_FIELD="username"`, `EMAIL_FIELD="email"`, `REQUIRED_FIELDS=["email", "phone"]`.
- Case-insensitive uniqueness on username and email via functional `UniqueConstraint(Lower(...))`
  instead of the spec's `citext` - no extension needed, Django validates it in `full_clean()`, and
  the resulting `btree (lower(email))` index is exactly what the multi-identifier login backend in
  task 5 will want. Verified in Postgres (section 4).
- `UserManager.create_user/create_superuser` require username + email + phone, normalise them, set
  an unusable password when none is given, and **run `full_clean()` before saving** so the phone
  format and the language choice are enforced on the programmatic path too. The DB constraints
  remain the arbiter, so callers must still expect `IntegrityError` under concurrency.

### `apps/audit/`

- `AuditLog`: org/store/actor (all nullable, all `PROTECT`), `action`, `target_type`, `target_id`,
  `changes` (JSONB with `DjangoJSONEncoder`), `ip`, `at`. Indexes `(org, at)` and
  `(target_type, target_id)`, plus `ordering = ("-at", "-id")`.
- Append-only for real: `save()` raises `AppendOnlyError` on anything but an insert, `delete()`
  raises, and the manager's queryset refuses `update()` / `delete()` / `_raw_delete()` so a bulk
  path cannot rewrite the trail either.
- `services.record(action, *, actor, org, store, target, changes, ip)` - the contract signature,
  unchanged. It validates at the boundary: action shape and length, target must be a *saved* model
  instance, `changes` must be a JSON-serialisable mapping, `ip` is validated, and a `store` whose
  `org_id` disagrees with the passed `org` is refused outright (invariant #1 at the audit boundary).
  A `store` alone derives its org. Sensitive keys (`password`, `secret`, `token`, `totp`, `otp`,
  `recovery_code`, `api_key`, `authorization`, `session`, `cookie`) are redacted recursively before
  the write, with a nesting cap. It emits one structured `logger.info("audit.recorded", extra=...)`
  with the row id, action, actor/org/store/target ids for `sre-observability` to pick up.

### `apps/orgs/`

- `permissions.py`: the 12 codes exactly as specified, as a `frozenset` derived from
  `PERMISSION_LABELS` (translated labels, so the role editor in task 10 has no excuse to invent
  English strings). `PRESETS = {Owner: all, Manager: all - {role.manage, store.manage}, Seller:
  {sale.record}}`, plus `PERMISSION_CHOICES` and `unknown_codes()`.
- `Organization` (name, slug unique, logo, brand JSONB, `base_currency="RWF"`,
  `timezone="Africa/Kigali"`), `Store` (org FK - the only org pointer in the schema), `Role`
  (permissions JSONB validated against the catalog in `clean()`, `is_preset`, `has(code)`),
  `Membership` (user/org/role), `StoreAccess` (membership/store). All `SoftDeleteModel +
  AuditedModel`, all `PROTECT`.
- Every "unique" rule is a **partial** unique constraint on live rows (`condition=LIVE`), so a
  soft-deleted store/role/membership does not reserve its name forever.
- Cross-org guards in `clean()`: a `Membership` cannot take a `Role` from another org, and a
  `StoreAccess` cannot point at a `Store` from another org. Both have tests.
- `MAX_STORES_PER_ORG = 5` exported for task 4's locked `create_store`.

### Test scaffolding

The abstract bases need concrete models to be tested. `tests/testapp/` (installed **only** by the
new `config/settings/test.py`) holds `Thing` (soft-delete + audited), `ScopedThing` (store-scoped)
and `ScopedThingOwnMeta` (store-scoped child that declares its own `Meta` without inheriting the
base's - the mistake that would otherwise disarm the default-manager arrangement).

---

## 2. Files created

Absolute paths.

Implementation:
- `/home/elvis/projects/2026/personal/raporo/common/apps.py`
- `/home/elvis/projects/2026/personal/raporo/common/checks.py`
- `/home/elvis/projects/2026/personal/raporo/common/managers.py`
- `/home/elvis/projects/2026/personal/raporo/common/models.py`
- `/home/elvis/projects/2026/personal/raporo/common/validators.py`
- `/home/elvis/projects/2026/personal/raporo/apps/accounts/managers.py`
- `/home/elvis/projects/2026/personal/raporo/apps/accounts/models.py` (was a comment stub)
- `/home/elvis/projects/2026/personal/raporo/apps/accounts/migrations/__init__.py`
- `/home/elvis/projects/2026/personal/raporo/apps/accounts/migrations/0001_initial.py`
- `/home/elvis/projects/2026/personal/raporo/apps/audit/models.py` (was a comment stub)
- `/home/elvis/projects/2026/personal/raporo/apps/audit/services.py`
- `/home/elvis/projects/2026/personal/raporo/apps/audit/migrations/__init__.py`
- `/home/elvis/projects/2026/personal/raporo/apps/audit/migrations/0001_initial.py`
- `/home/elvis/projects/2026/personal/raporo/apps/orgs/models.py` (was a comment stub)
- `/home/elvis/projects/2026/personal/raporo/apps/orgs/permissions.py`
- `/home/elvis/projects/2026/personal/raporo/apps/orgs/migrations/__init__.py`
- `/home/elvis/projects/2026/personal/raporo/apps/orgs/migrations/0001_initial.py`
- `/home/elvis/projects/2026/personal/raporo/config/settings/test.py`

Tests:
- `/home/elvis/projects/2026/personal/raporo/tests/conftest.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_common_bases.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_common_checks.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_user_model.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_audit.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_orgs_models.py`
- `/home/elvis/projects/2026/personal/raporo/tests/testapp/__init__.py`
- `/home/elvis/projects/2026/personal/raporo/tests/testapp/apps.py`
- `/home/elvis/projects/2026/personal/raporo/tests/testapp/models.py`
- `/home/elvis/projects/2026/personal/raporo/tests/testapp/migrations/__init__.py`
- `/home/elvis/projects/2026/personal/raporo/tests/testapp/migrations/0001_initial.py`

## 3. Files modified

- `/home/elvis/projects/2026/personal/raporo/config/settings/base.py`
  - `AUTH_USER_MODEL = "accounts.User"` uncommented (carry-forward item 2), stale note removed.
  - `"common"` added to `INSTALLED_APPS` (system checks; no models, no migrations).
  - `MEDIA_URL` / `MEDIA_ROOT` added so `Organization.logo` has a real destination instead of
    dropping uploads next to the code. Serving media in dev and choosing prod storage stays with
    `devops-engineer`.
- `/home/elvis/projects/2026/personal/raporo/pytest.ini`
  - `addopts = --ds=config.settings.test`. Needed: the compose service exports
    `DJANGO_SETTINGS_MODULE=config.settings.dev`, and pytest-django lets that env var win over the
    ini key, so the ini key alone was ignored (proven in section 4). `--ds` has the highest
    precedence. The ini key is kept for anyone running pytest outside compose.
- `/home/elvis/projects/2026/personal/raporo/ruff.toml`
  - `[lint.per-file-ignores]` for `**/migrations/*.py`: `E501` (Django's generated field lines run
    to 400 characters) and `I001` (Django emits `import common.validators` next to the django
    imports, which our isort config calls first-party). Generated code we do not hand-format.
- `/home/elvis/projects/2026/personal/raporo/.gitignore`
  - `/media/` (uploads must never be committed).

---

## 4. Verification - verbatim output

### 4.1 The failing-first run (Step 1/2, before any implementation existed)

```
$ docker compose run --rm web pytest -x -q --create-db
ImportError while loading conftest '/app/tests/conftest.py'.
tests/conftest.py:6: in <module>
    from apps.orgs.models import Organization, Store
E   ImportError: cannot import name 'Organization' from 'apps.orgs.models' (/app/apps/orgs/models.py)
```

Two more genuine reds on the way to green, both worth recording:

```
$ docker compose run --rm web pytest -q --create-db
E   RuntimeError: Model class tests.testapp.models.Thing doesn't declare an explicit app_label
    and isn't in an application in INSTALLED_APPS.
```
-> the compose env var was overriding `DJANGO_SETTINGS_MODULE` from `pytest.ini`; fixed with
`addopts = --ds=...`.

```
$ docker compose run --rm web pytest -q --create-db
E   django.db.utils.ProgrammingError: relation "accounts_user" does not exist
    ALTER TABLE "testapp_thing" ADD CONSTRAINT ... FOREIGN KEY ("created_by_id")
    REFERENCES "accounts_user" ("id")
```
-> `migrate --run-syncdb` creates unmigrated apps' tables *before* running migrations, so the test
app's FKs pointed at tables that did not exist yet. Fixed by giving `tests/testapp` its own
migration (generated under the test settings), which puts it in the dependency graph.

### 4.2 Migration generation (once, at the end, per carry-forward item 3)

```
$ docker compose down -v          # carry-forward item 1, before the first migrate
$ docker compose run --rm web python manage.py makemigrations accounts audit orgs
Migrations for 'accounts':
  apps/accounts/migrations/0001_initial.py
    + Create model User
Migrations for 'orgs':
  apps/orgs/migrations/0001_initial.py
    + Create model Organization
    + Create model Role
    + Create model Membership
    + Create model Store
    + Create model StoreAccess
    + Create constraint orgs_role_unique_live_name_per_org on model role
    + Create constraint orgs_membership_unique_live_user_per_org on model membership
    + Create constraint orgs_store_unique_live_name_per_org on model store
    + Create constraint orgs_storeaccess_unique_live_membership_store on model storeaccess
Migrations for 'audit':
  apps/audit/migrations/0001_initial.py
    + Create model AuditLog
```

```
$ docker compose run --rm web python manage.py migrate
Operations to perform:
  Apply all migrations: accounts, admin, audit, auth, contenttypes, orgs, sessions
Running migrations:
  Applying contenttypes.0001_initial... OK
  ... (auth 0001-0012) ...
  Applying accounts.0001_initial... OK
  Applying admin.0001_initial... OK
  Applying admin.0002_logentry_remove_auto_add... OK
  Applying admin.0003_logentry_add_action_flag_choices... OK
  Applying orgs.0001_initial... OK
  Applying audit.0001_initial... OK
  Applying sessions.0001_initial... OK
```

A clean database accepts the swappable user model - the problem carry-forward item 1 warned about
is gone.

### 4.3 The schema Postgres actually got

```
$ docker compose run --rm web python manage.py shell -c "<pg_indexes query>"
accounts_user_email_ci_unique       :: btree (lower((email)::text))
accounts_user_email_key             :: btree (email)
accounts_user_phone_key             :: btree (phone)
accounts_user_username_ci_unique    :: btree (lower((username)::text))
accounts_user_username_key          :: btree (username)
orgs_store_unique_live_name_per_org  :: btree (org_id, name) WHERE (deleted_at IS NULL)
```

The store-name uniqueness really is partial, and the case-insensitive indexes really are
functional.

### 4.4 `createsuperuser` smoke test (the manager's real entry point)

```
$ docker compose run --rm -e DJANGO_SUPERUSER_PASSWORD=... web python manage.py createsuperuser \
    --noinput --username root --email root@example.rw --phone 250788000009
Superuser created successfully.
hash: argon2$argon2id$v=19$m=102400,
staff/superuser: True True
lang: en
auth ok: True
```

argon2id, and `authenticate()` finds it. (I deleted that row again afterwards so the dev database
has no `root` account with a known password.)

### 4.5 Full suite

```
$ docker compose run --rm web pytest -v --create-db
============================= test session starts ==============================
platform linux -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0 -- /usr/local/bin/python3.13
cachedir: .pytest_cache
django: version: 6.1, settings: config.settings.test (from option)
rootdir: /app
configfile: pytest.ini
plugins: django-4.14.0
collecting ... collected 165 items

tests/test_audit.py::test_record_writes_a_row PASSED                     [  0%]
tests/test_audit.py::test_record_without_a_target_or_actor_is_allowed PASSED [  1%]
tests/test_audit.py::test_record_derives_the_org_from_the_store PASSED   [  1%]
tests/test_audit.py::test_record_rejects_a_store_from_another_org PASSED [  2%]
tests/test_audit.py::test_record_rejects_a_malformed_action[] PASSED     [  3%]
tests/test_audit.py::test_record_rejects_a_malformed_action[   ] PASSED  [  3%]
tests/test_audit.py::test_record_rejects_a_malformed_action[User.Created] PASSED [  4%]
tests/test_audit.py::test_record_rejects_a_malformed_action[user created] PASSED [  4%]
tests/test_audit.py::test_record_rejects_a_malformed_action[user..created] PASSED [  5%]
tests/test_audit.py::test_record_rejects_a_malformed_action[user] PASSED [  6%]
tests/test_audit.py::test_record_rejects_a_malformed_action[xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx] PASSED [  6%]
tests/test_audit.py::test_record_rejects_a_malformed_action[None] PASSED [  7%]
tests/test_audit.py::test_record_rejects_an_unsaved_target PASSED        [  7%]
tests/test_audit.py::test_record_redacts_sensitive_values PASSED         [  8%]
tests/test_audit.py::test_record_rejects_changes_it_cannot_serialise PASSED [  9%]
tests/test_audit.py::test_record_requires_a_mapping_for_changes PASSED   [  9%]
tests/test_audit.py::test_record_serialises_decimals_and_dates PASSED    [ 10%]
tests/test_audit.py::test_record_stores_the_ip PASSED                    [ 10%]
tests/test_audit.py::test_record_rejects_a_bogus_ip PASSED               [ 11%]
tests/test_audit.py::test_audit_rows_cannot_be_updated PASSED            [ 12%]
tests/test_audit.py::test_audit_rows_cannot_be_bulk_updated PASSED       [ 12%]
tests/test_audit.py::test_audit_rows_cannot_be_deleted PASSED            [ 13%]
tests/test_audit.py::test_the_action_field_carries_the_validator PASSED  [ 13%]
tests/test_audit.py::test_audit_log_has_no_soft_delete_columns PASSED    [ 14%]
tests/test_audit.py::test_actor_cannot_be_hard_deleted_out_from_under_the_log PASSED [ 15%]
tests/test_audit.py::test_newest_rows_come_first PASSED                  [ 15%]
tests/test_common_bases.py::test_soft_delete_hides_from_default_manager PASSED [ 16%]
tests/test_common_bases.py::test_soft_delete_stamps_who_and_when PASSED  [ 16%]
tests/test_common_bases.py::test_soft_delete_is_idempotent PASSED        [ 17%]
tests/test_common_bases.py::test_soft_delete_requires_an_actor_keyword PASSED [ 18%]
tests/test_common_bases.py::test_hard_delete_is_forbidden_on_instances PASSED [ 18%]
tests/test_common_bases.py::test_hard_delete_is_forbidden_on_querysets PASSED [ 19%]
tests/test_common_bases.py::test_queryset_soft_delete_stamps_every_row PASSED [ 20%]
tests/test_common_bases.py::test_hard_delete_forbidden_is_a_notimplementederror PASSED [ 20%]
tests/test_common_bases.py::test_audited_model_stamps_timestamps PASSED  [ 21%]
tests/test_common_bases.py::test_audited_actor_fields_are_optional_but_protected PASSED [ 21%]
tests/test_common_bases.py::test_store_fk_is_declared_by_the_base_not_by_consumers PASSED [ 22%]
tests/test_common_bases.py::test_unscoped_query_raises[list] PASSED      [ 23%]
tests/test_common_bases.py::test_unscoped_query_raises[len] PASSED       [ 23%]
tests/test_common_bases.py::test_unscoped_query_raises[bool] PASSED      [ 24%]
tests/test_common_bases.py::test_unscoped_query_raises[count] PASSED     [ 24%]
tests/test_common_bases.py::test_unscoped_query_raises[exists] PASSED    [ 25%]
tests/test_common_bases.py::test_unscoped_query_raises[first] PASSED     [ 26%]
tests/test_common_bases.py::test_unscoped_query_raises[last] PASSED      [ 26%]
tests/test_common_bases.py::test_unscoped_query_raises[get] PASSED       [ 27%]
tests/test_common_bases.py::test_unscoped_query_raises[aggregate] PASSED [ 27%]
tests/test_common_bases.py::test_unscoped_query_raises[iterator] PASSED  [ 28%]
tests/test_common_bases.py::test_unscoped_query_raises[values_list] PASSED [ 29%]
tests/test_common_bases.py::test_unscoped_query_raises[chained_filter] PASSED [ 29%]
tests/test_common_bases.py::test_unscoped_query_raises[in_bulk] PASSED   [ 30%]
tests/test_common_bases.py::test_unscoped_query_raises[explain] PASSED   [ 30%]
tests/test_common_bases.py::test_unscoped_query_raises[update] PASSED    [ 31%]
tests/test_common_bases.py::test_unscoped_query_raises_on_the_manager_itself PASSED [ 32%]
tests/test_common_bases.py::test_unscoped_subquery_raises PASSED         [ 32%]
tests/test_common_bases.py::test_raw_sql_is_refused_on_the_scoped_manager PASSED [ 33%]
tests/test_common_bases.py::test_own_meta_child_is_still_guarded PASSED  [ 33%]
tests/test_common_bases.py::test_for_store_returns_only_that_store PASSED [ 34%]
tests/test_common_bases.py::test_for_store_hides_soft_deleted_rows PASSED [ 35%]
tests/test_common_bases.py::test_for_store_survives_further_chaining PASSED [ 35%]
tests/test_common_bases.py::test_for_store_accepts_a_primary_key PASSED  [ 36%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[None] PASSED [ 36%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[] PASSED [ 37%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[0] PASSED [ 38%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[bad3] PASSED [ 38%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[not-a-store] PASSED [ 39%]
tests/test_common_bases.py::test_for_store_rejects_a_saved_instance_of_another_model PASSED [ 40%]
tests/test_common_bases.py::test_for_stores_rejects_a_saved_instance_of_another_model PASSED [ 40%]
tests/test_common_bases.py::test_for_store_rejects_an_unsaved_store PASSED [ 41%]
tests/test_common_bases.py::test_for_stores_covers_several_stores_and_nothing_else PASSED [ 41%]
tests/test_common_bases.py::test_for_stores_rejects_an_empty_collection PASSED [ 42%]
tests/test_common_bases.py::test_scoped_update_is_allowed_and_stays_scoped PASSED [ 43%]
tests/test_common_bases.py::test_scoped_queryset_still_refuses_hard_delete PASSED [ 43%]
tests/test_common_bases.py::test_creating_a_row_does_not_need_a_scope PASSED [ 44%]
tests/test_common_bases.py::test_all_objects_is_the_documented_escape_hatch PASSED [ 44%]
tests/test_common_bases.py::test_django_internals_use_an_unguarded_default_manager PASSED [ 45%]
tests/test_common_bases.py::test_store_scoped_models_have_no_reverse_accessor_from_store PASSED [ 46%]
tests/test_common_bases.py::test_removing_the_scope_filter_would_be_caught PASSED [ 46%]
tests/test_common_bases.py::test_validation_error_is_not_how_scope_violations_surface PASSED [ 47%]
tests/test_healthz.py::test_healthz_returns_ok PASSED                    [ 47%]
tests/test_orgs_models.py::test_organization_defaults_are_rwanda_first PASSED [ 48%]
tests/test_orgs_models.py::test_organization_slug_is_unique PASSED       [ 49%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[rwf] PASSED [ 49%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[RW] PASSED [ 50%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[RWFX] PASSED [ 50%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[R1F] PASSED [ 51%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[] PASSED [ 52%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[Africa/Kigaly] PASSED [ 52%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[CAT] PASSED [ 53%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[] PASSED [ 53%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[UTC+2] PASSED [ 54%]
tests/test_orgs_models.py::test_organization_accepts_another_real_timezone PASSED [ 55%]
tests/test_orgs_models.py::test_store_name_is_unique_within_an_org PASSED [ 55%]
tests/test_orgs_models.py::test_store_name_uniqueness_is_enforced_by_the_database PASSED [ 56%]
tests/test_orgs_models.py::test_the_same_store_name_is_fine_in_another_org PASSED [ 56%]
tests/test_orgs_models.py::test_a_soft_deleted_store_name_can_be_reused PASSED [ 57%]
tests/test_orgs_models.py::test_store_carries_the_only_org_pointer PASSED [ 58%]
tests/test_orgs_models.py::test_store_limit_constant_is_five PASSED      [ 58%]
tests/test_orgs_models.py::test_permission_catalog_is_exactly_the_agreed_set PASSED [ 59%]
tests/test_orgs_models.py::test_presets_are_owner_manager_seller PASSED  [ 60%]
tests/test_orgs_models.py::test_manager_preset_runs_a_store_but_does_not_own_the_org PASSED [ 60%]
tests/test_orgs_models.py::test_role_rejects_an_unknown_permission_code PASSED [ 61%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[sale.record] PASSED [ 61%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[bad1] PASSED [ 62%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[bad2] PASSED [ 63%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[bad3] PASSED [ 63%]
tests/test_orgs_models.py::test_role_accepts_catalog_codes_and_answers_has PASSED [ 64%]
tests/test_orgs_models.py::test_role_name_is_unique_within_an_org PASSED [ 64%]
tests/test_orgs_models.py::test_role_defaults_to_no_permissions_and_not_preset PASSED [ 65%]
tests/test_orgs_models.py::test_membership_is_unique_per_user_and_org PASSED [ 66%]
tests/test_orgs_models.py::test_membership_uniqueness_is_enforced_by_the_database PASSED [ 66%]
tests/test_orgs_models.py::test_membership_rejects_a_role_from_another_org PASSED [ 67%]
tests/test_orgs_models.py::test_store_access_rejects_a_store_from_another_org PASSED [ 67%]
tests/test_orgs_models.py::test_store_access_is_unique_per_membership_and_store PASSED [ 68%]
tests/test_orgs_models.py::test_store_access_accepts_a_store_in_the_same_org PASSED [ 69%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Store.orgs_store_unique_live_name_per_org] PASSED [ 69%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Role.orgs_role_unique_live_name_per_org] PASSED [ 70%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Membership.orgs_membership_unique_live_user_per_org] PASSED [ 70%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[StoreAccess.orgs_storeaccess_unique_live_membership_store] PASSED [ 71%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Organization] PASSED [ 72%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Store] PASSED [ 72%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Role] PASSED [ 73%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Membership] PASSED [ 73%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[StoreAccess] PASSED [ 74%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Organization] PASSED [ 75%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Store] PASSED [ 75%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Role] PASSED [ 76%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Membership] PASSED [ 76%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[StoreAccess] PASSED [ 77%]
tests/test_orgs_models.py::test_orgs_models_are_not_store_scoped PASSED  [ 78%]
tests/test_user_model.py::test_the_project_user_model_is_ours PASSED     [ 78%]
tests/test_user_model.py::test_username_field_and_required_fields PASSED [ 79%]
tests/test_user_model.py::test_phone_rejects_bad_formats[+250788123456] PASSED [ 80%]
tests/test_user_model.py::test_phone_rejects_bad_formats[0788123456] PASSED [ 80%]
tests/test_user_model.py::test_phone_rejects_bad_formats[250 788 123 456] PASSED [ 81%]
tests/test_user_model.py::test_phone_rejects_bad_formats[250788] PASSED  [ 81%]
tests/test_user_model.py::test_phone_rejects_bad_formats[2507881234567890] PASSED [ 82%]
tests/test_user_model.py::test_phone_rejects_bad_formats[25078812345a] PASSED [ 83%]
tests/test_user_model.py::test_phone_rejects_bad_formats[] PASSED        [ 83%]
tests/test_user_model.py::test_phone_accepts_country_code_digits[250788123456] PASSED [ 84%]
tests/test_user_model.py::test_phone_accepts_country_code_digits[12345678] PASSED [ 84%]
tests/test_user_model.py::test_phone_accepts_country_code_digits[999999999999999] PASSED [ 85%]
tests/test_user_model.py::test_email_is_required PASSED                  [ 86%]
tests/test_user_model.py::test_email_must_be_unique_case_insensitively PASSED [ 86%]
tests/test_user_model.py::test_email_uniqueness_is_enforced_by_the_database_too PASSED [ 87%]
tests/test_user_model.py::test_username_must_be_unique_case_insensitively PASSED [ 87%]
tests/test_user_model.py::test_phone_must_be_unique PASSED               [ 88%]
tests/test_user_model.py::test_language_defaults_to_english PASSED       [ 89%]
tests/test_user_model.py::test_language_choices_match_the_configured_languages PASSED [ 89%]
tests/test_user_model.py::test_language_rejects_an_unconfigured_code PASSED [ 90%]
tests/test_user_model.py::test_password_is_argon2 PASSED                 [ 90%]
tests/test_user_model.py::test_create_user_validates_its_input PASSED    [ 91%]
tests/test_user_model.py::test_create_user_requires_identity_fields[username] PASSED [ 92%]
tests/test_user_model.py::test_create_user_requires_identity_fields[email] PASSED [ 92%]
tests/test_user_model.py::test_create_user_requires_identity_fields[phone] PASSED [ 93%]
tests/test_user_model.py::test_create_user_normalises_the_email_domain PASSED [ 93%]
tests/test_user_model.py::test_create_user_without_a_password_cannot_log_in PASSED [ 94%]
tests/test_user_model.py::test_create_superuser_is_staff_and_superuser PASSED [ 95%]
tests/test_user_model.py::test_create_superuser_refuses_to_be_downgraded PASSED [ 95%]
tests/test_user_model.py::test_new_users_are_active_and_not_staff PASSED [ 96%]
tests/test_common_checks.py::test_the_real_models_pass PASSED            [ 96%]
tests/test_common_checks.py::test_a_model_that_overrides_objects_is_rejected PASSED [ 97%]
tests/test_common_checks.py::test_a_model_that_makes_the_guarded_manager_the_default_is_rejected PASSED [ 98%]
tests/test_common_checks.py::test_a_model_that_repoints_the_store_fk_is_rejected PASSED [ 98%]
tests/test_common_checks.py::test_a_model_that_makes_the_store_optional_is_rejected PASSED [ 99%]
tests/test_common_checks.py::test_non_scoped_models_are_ignored PASSED   [100%]

============================= 165 passed in 11.63s =============================
```

### 4.6 ruff

```
$ docker compose run --rm web ruff check .
All checks passed!
```

### 4.7 Missing migrations

```
$ docker compose run --rm web python manage.py makemigrations --check --dry-run
No changes detected
```

Also checked under the test settings, so the test app's migration cannot drift out of sync with the
bases:

```
$ docker compose run --rm web python manage.py makemigrations --check --dry-run --settings=config.settings.test
No changes detected
```

### 4.8 System checks

```
$ docker compose run --rm web python manage.py check
System check identified no issues (0 silenced).
```

---

## 5. Do the tests actually fail when an invariant is removed?

I did not want to take that on trust, so I ran a mutation pass: each named mutation removes exactly
one invariant from the source, runs the suite, and restores the file. (The harness lived in
`.mutate.py` / `.mutation-backup/` / `.mutation-run.sh` and was deleted afterwards - it is not in
the tree.)

```
BASELINE: 164 passed in 5.82s
CAUGHT    no_scope_read_guard                  17 failed, 147 passed in 5.60s
CAUGHT    no_scope_write_guard                 1 failed, 163 passed in 4.95s
CAUGHT    raw_sql_allowed                      1 failed, 163 passed in 4.32s
CAUGHT    no_softdelete_filter                 3 failed, 161 passed in 4.25s
CAUGHT    scoped_manager_shows_deleted         1 failed, 163 passed in 4.35s
CAUGHT    for_store_accepts_none               1 failed, 163 passed in 4.39s
CAUGHT    for_store_accepts_any_model          2 failed, 162 passed in 4.25s
CAUGHT    queryset_hard_delete_allowed         5 failed, 159 passed in 4.52s
CAUGHT    hard_delete_allowed                  1 failed, 163 passed in 4.35s
CAUGHT    soft_delete_not_idempotent           1 failed, 163 passed in 4.36s
CAUGHT    guarded_default_manager              2 failed, 162 passed in 4.22s
SURVIVED! manager_order_swapped                164 passed in 4.31s
CAUGHT    checks_disabled                      2 failed, 162 passed in 4.38s
CAUGHT    store_fk_check_disabled              2 failed, 162 passed in 4.33s
CAUGHT    audit_updatable                      1 failed, 163 passed in 4.27s
CAUGHT    audit_bulk_updatable                 1 failed, 163 passed in 4.37s
SURVIVED! audit_action_unvalidated             164 passed in 4.25s
CAUGHT    no_ci_unique                         2 failed, 162 passed in 4.50s
CAUGHT    no_phone_validator                   6 failed, 158 passed in 4.30s
CAUGHT    email_optional                       1 failed, 163 passed in 4.32s
CAUGHT    no_role_validation                   1 failed, 163 passed in 4.33s
CAUGHT    no_cross_org_role_check              1 failed, 163 passed in 4.13s
CAUGHT    no_cross_org_store_access_check      1 failed, 163 passed in 4.25s
CAUGHT    no_timezone_validator                3 failed, 161 passed in 4.36s
CAUGHT    no_currency_validator                3 failed, 161 passed in 4.26s
CAUGHT    store_unique_ignores_soft_delete     1 failed, 163 passed in 4.54s
RESTORED: 164 passed in 4.18s
```

Both survivors are honest, and I checked each one:

- **`manager_order_swapped`** - swapping the declaration order of `all_objects` and `objects` in
  `StoreScopedModel`. It survives because the load-bearing mechanism is
  `Meta.default_manager_name`, which Django picks up from the abstract base through the MRO even
  when a subclass declares its own `Meta` (verified directly: `ScopedThingOwnMeta._meta.
  default_manager_name is None` but `_default_manager.name == "all_objects"`). Declaration order is
  a redundant second layer, and `guarded_default_manager` (mutating the real mechanism) *is* caught.
  I fixed the docstring, which had this backwards.
- **`audit_action_unvalidated`** - removing `validators=[action_validator]` from the model field.
  It survived because `record()` validates the action itself, which is the only sanctioned writer.
  I added `test_the_action_field_carries_the_validator` so the field-level layer is pinned too, and
  re-ran the mutation:

```
$ python .mutate.py audit_action_unvalidated && pytest -q --reuse-db tests/test_audit.py
FAILED tests/test_audit.py::test_the_action_field_carries_the_validator - Ass...
1 failed, 25 passed in 1.15s
```

Two gaps the pass exposed in my own tests, both closed before the final run:

- `for_store()` accepting a saved instance of *another* model went untested - a leak, because an
  `Organization` has a pk too. Added `test_for_store_rejects_a_saved_instance_of_another_model`
  (and the `for_stores` variant).
- Removing `condition=LIVE` from the store constraint went undetected, because `_default_manager`
  on a soft-delete model cannot see deleted rows, so `full_clean()` never notices. Replaced the
  hopeful `full_clean()` assertion with a structural one:
  `test_unique_constraints_only_cover_live_rows` asserts every live-unique constraint's condition
  directly, for all four models.

---

## 6. Deviations from the brief (and why)

1. **The scope guard is on `sql.Query.get_compiler`, not on a per-call `Guard` subclass of
   `_fetch_all`.** The brief's sketch (`qs.__class__ = Guard` with a `_fetch_all` override) misses
   `count()`, `exists()`, `aggregate()`, `iterator()`, `in_bulk()`, `explain()`, `update()` and -
   worst - a scoped model used as a subquery, all of which reach the database without ever calling
   `_fetch_all`. It also mints a new class per call, which breaks pickling. The compiler hook is one
   method and covers every read path Django has now or adds later; writes are guarded on the
   queryset because Django swaps the query class for `UpdateQuery`. `objects.raw()` is refused
   outright since raw SQL cannot be checked.
2. **`StoreScopedModel` declares the `store` FK.** The brief left it to each consumer. Declaring it
   in the base makes the invariant structural (a consumer cannot forget it) and `related_name="+"`
   removes the soft-delete-blind reverse accessor. `common/checks.py` errors if a subclass repoints
   or nullifies it.
3. **`Meta.default_manager_name = "all_objects"` on `StoreScopedModel`, plus system checks.** Not in
   the brief. Without it, Django's own unique validation (`_perform_unique_checks`,
   `validate_constraints`) runs through the guarded manager and `full_clean()` raises
   `UnscopedQueryError` on every store-scoped model - the kind of trap that gets "fixed" by deleting
   the guard.
4. **`SoftDeleteQuerySet.delete()` raises instead of silently bulk-soft-deleting.** The brief had
   `delete()` do `update(deleted_at=now())`, which loses the actor entirely and contradicts
   invariant #3 (everything attributable). Explicit `soft_delete(by=...)` is the replacement, on both
   the instance and the queryset.
5. **`HardDeleteForbidden(NotImplementedError)`** rather than a bare `NotImplementedError`, so
   callers can catch the specific case; `except NotImplementedError` still works as the brief
   promised.
6. **`soft_delete()` is idempotent and keyword-only**, and stamps `updated_at`/`updated_by` when the
   model is audited. At-least-once callers are a fact of life; a retry must not rewrite the original
   actor.
7. **`AuditLog.action` is a `CharField` with a dotted-slug `RegexValidator`, not a `SlugField`.**
   Django's `validate_slug` rejects dots, so the brief's own example (`"user.created"`) would have
   failed `full_clean()` on a `SlugField`. The brief's test only passed because `create()` skips
   validation - a latent trap.
8. **`REQUIRED_FIELDS = ["email", "phone"]`.** The brief's prose said both, its code sketch said
   `["phone"]`. Since email is required and unique, `createsuperuser` has to ask for it; the code
   sketch would have made the command unusable.
9. **`create_user` runs `full_clean()`**, and both it and `create_superuser` reject blank
   username/email/phone with `ValueError`. The brief's note about "normalizing email -> None if
   blank" is stale - email became mandatory in the decisions that bind this task.
10. **Case-insensitive uniqueness via `UniqueConstraint(Lower(...))` instead of `citext`.** No
    extension, no `django.contrib.postgres` dependency, validated by `full_clean()`, and the
    functional index is what the multi-identifier login backend will query. (`citext` is
    discouraged upstream in favour of exactly this.)
11. **Uniqueness in `orgs` is partial on live rows** (`condition=deleted_at IS NULL`). The spec
    wrote plain `UNIQUE(org, name)`; combined with soft delete that would reserve a deleted store's
    name forever. This matches the spec's own "among live rows" wording for `catalog`.
12. **`audit.services.record()` validates and redacts.** The brief's `record()` was a bare
    `objects.create()`. Added: action shape/length, saved-target check, mapping + JSON-serialisable
    `changes`, IP validation, secret redaction, and a refusal to write a row whose store and org
    disagree. Signature unchanged, so the seam contract holds.
13. **`Membership.clean` / `StoreAccess.clean` cross-org checks**, `Organization.timezone` and
    `base_currency` validators, `Role.permissions` shape checks (list, strings, no duplicates).
    Cheap boundary validation the brief did not spell out.
14. **`PRESETS["Manager"]` = everything except `role.manage` and `store.manage`.** The brief left it
    as `{...}`. A manager runs the shop floor (sales, stock, expenses, cycles, reports, invites,
    audit view, below-floor override) but cannot reshape the organization. Flagging it for
    `product-owner` to confirm.
15. **New file `config/settings/test.py` and a migration for `tests/testapp`.** The brief wanted the
    bases tested through concrete models; those models must live in an installed app, and they must
    not reach a real database. Test settings keep them out of dev/prod, and the migration keeps
    `migrate --run-syncdb` from creating their tables before `accounts_user` exists.
16. **`MEDIA_URL`/`MEDIA_ROOT` and `/media/` in `.gitignore`.** `Organization.logo` is in the spec;
    a `FileField` with nowhere to write is not something I want to leave as a surprise. It is a
    `FileField`, not `ImageField`, deliberately: `ImageField` needs Pillow, and no upload path
    exists yet to justify a new dependency.

No new Python dependencies. Everything is Django-native: ORM, validators, system checks,
`TextChoices`, functional constraints, `zoneinfo`.

---

## 7. Concerns and carry-forward for later tasks

**Schema items for `database-engineer` to bless (I generated the migrations, but these are schema
decisions):**

1. `accounts_user` carries both an exact unique index and a `lower()` functional unique index on
   `username` and on `email`. The exact one is redundant for correctness; I kept it for the clean
   field-level `ValidationError`. Drop it if the duplication offends.
2. `AuditLog` is append-only in application code only. The spec's invariant #4 asks for a DB trigger
   as belt and braces on ledger tables; I did not add one for the audit table because triggers are
   `database-engineer`'s call. Worth a follow-up when the ledger tables land in slice 2.
3. No index on `AuditLog.actor` or `store` alone (only `(org, at)` and `(target_type, target_id)`).
   Fine until the audit-view screen exists; revisit with `performance-engineer` then.

**For task 4 (orgs services):**

4. `MAX_STORES_PER_ORG` is in `apps/orgs/models.py` - use it rather than a literal `5`.
5. `Store.objects` is a plain soft-delete manager (Store is org-level, not store-scoped), so
   `Organization.objects.select_for_update()` + `Store.objects.filter(org=...)` in the brief's
   `create_store` sketch works as written.
6. Services must set `created_by` / `updated_by` themselves - the bases only provide the columns.
   Nothing stamps them automatically, by design (no thread-local current-user magic).
7. `register_owner` should create the three preset roles from `PRESETS` with `is_preset=True`, and
   `Membership.clean()` will reject a role from another org if the wiring is ever wrong.

**For task 5+ (auth, forms, HTMX views):**

8. `UnscopedQueryError` is a programming error, not user input - it must never be rendered as a form
   error. If it reaches a view, that view queried a store-scoped model without a store.
9. Store-scoped **ModelForms** must be given an explicit queryset for any `ModelChoiceField`.
   `_default_manager` is `all_objects` (deliberately - see deviation 3), so a naive form field would
   offer every store's rows. This is the sharpest edge I am leaving behind.
10. Same for the Django admin when store-scoped models get registered: admin uses
    `_default_manager`, so it sees everything, including soft-deleted rows. Admin delete must be
    disabled (the spec says so) - `Model.delete()` already raises, but the admin's bulk action uses
    the collector and would fail with an unfriendly 500 rather than a clean refusal.
11. Store-scoped models should use **partial** unique constraints (`condition=deleted_at IS NULL`),
    not `unique=True`, because `_default_manager` there can see deleted rows and would produce false
    "already exists" errors. Documented in `test_unique_constraints_only_cover_live_rows` as the
    pattern to copy.
12. `record()` writes inside the caller's transaction: if that transaction rolls back, the audit row
    disappears with it. Correct for now (no half-truths in the trail), but a genuinely
    tamper-evident trail needs an outbox - out of scope until there is a queue.
13. Login throttling, rate limits and the no-enumeration login responses are task 5's; nothing in
    this task exposes an endpoint, so there is nothing to rate-limit yet.
14. Translations: every user-facing string here is wrapped in `gettext_lazy`. Developer-facing
    exception messages (`UnscopedQueryError`, `HardDeleteForbidden`, `AppendOnlyError`, the
    `ValueError`s in `record()`) are deliberately **not** wrapped - they are bug reports for us, never
    shown to a user. If the i18n gate greps for unwrapped strings, it should exempt those.
15. `PRESETS["Manager"]` (deviation 14) and the `Organization.logo` storage decision are the two
    product/platform questions I answered on my own authority. Both are cheap to change now and
    expensive later.


---
---

# FIX ROUND 1 - response to code-reviewer / database-engineer / security-engineer

## Status: DONE_WITH_CONCERNS

Every item in `task-123-fix-round-1.md` is done. Nothing was deferred except where
the "Explicitly NOT in this round" list already parked it. 254 tests pass from a
wiped database, `ruff check .` is clean, `manage.py check` is silent, and there
are no missing migrations under either settings module.

The reproductions in the review are now tests. Each one was re-run against the
fixed code, and a mutation pass (section F) shows all of them fail again the
moment the corresponding guard is removed.

---

## A. Structural invariant #1

### A1 - reverse related managers (`common.E004`) - DONE, and the residual caveat closed too

`common/checks.py` grew `E004`, which is enforced from both directions:

- every entry in `model._meta.related_objects` for a concrete `StoreScopedModel`
  is an error (Django already excludes hidden relations from that list, so
  anything left is traversable) - this is `sale.lines`;
- every *forward* relation out of a store-scoped model whose
  `field.remote_field.hidden` is False is an error, because the accessor it
  creates lives on the other model - this is `category.products`.

The test models now have the reviewer's shapes (`Category` org-level →
`Product` store-scoped; `Sale` → `SaleLine`, both store-scoped), and
`tests/test_common_bases.py` asserts `category.products`, `category.product_set`,
`sale.lines` and `sale.saleline_set` do not exist. `tests/test_common_checks.py`
builds rogue models with `related_name="products"` / `related_name="lines"` under
`isolate_apps` and asserts E004 fires for each.

**The documented caveat turned out to be exploitable enough to fix, so I fixed
it.** I verified the reviewer's note first:

```
Category.objects.filter(**{"+__name": "RIVAL"})                     -> compiles
Product.objects.for_store(s).filter(**{"category__+__name": "..."})  -> compiles
```

Both are existence oracles whenever a lookup key is built from request data. New
`common.managers.GuardedQuery.names_to_path()` refuses any path segment ending in
`+`, and it backs all three `common` managers (`objects`, `all_objects`,
`for_store`), so both forms now raise `UnscopedQueryError`. Three tests pin it.
Residual, documented rather than fixed: `User.objects` and `AuditLog.objects` are
not built on `GuardedQuery` (they are not store-scoped parents; their hidden
relations are the audit stamps). The standing rule stays in the docstring:
**never build an ORM lookup key from request data.**

### A2 - same-store FK validation in `save()` - DONE

`StoreScopedModel.save()` calls `_assert_related_stores_match()`, which walks
`self._meta.concrete_fields`, picks the FKs whose `related_model` is a
`StoreScopedModel` subclass, and compares the referenced row's `store_id` with
its own. It uses the field's cached instance when the caller passed an object
(no query) and a single indexed `values_list(...).first()` when the caller passed
a raw id - which is exactly the IDOR case. On a partial save it only checks the
relations named in `update_fields`, so `soft_delete()` adds no queries.
Violations raise `CrossStoreReferenceError` (a subclass of `UnscopedQueryError`,
so one `except` still covers every invariant-#1 violation).

`bulk_create` never calls `save()`, so `ScopedQuerySet.bulk_create` runs the same
check per object.

`common.E006` is the accompanying check: only a store-scoped model may hold a
foreign key into a store-scoped model, because nothing else has a store to be
compared against.

Tests cover the reproduction with objects, with raw ids, via `save()`, via
`bulk_create`, and the happy path.

### A3 - join traversal - DONE (closed by A1 + A2, with the regression tests asked for)

```python
Product.objects.for_store(store).values_list("category__products__name", flat=True)  # FieldError
Product.objects.for_store(store).filter(category__products__name="RIVAL...").exists()  # FieldError
Product.objects.for_store(store).aggregate(n=Count("category__products"))  # FieldError
```
All three are tests. The `+`-path variants of the same walk are covered above.

### A4 - `for_store()` now scopes writes - DONE

`for_store`/`for_stores` keep the pinned pk(s) on the query (`store_scope_pks`),
which survives cloning. `ScopedQuerySet` overrides:

- `create` - fills in the pinned store when none is given (this is code-review
  Minor 9: `for_store(s).create(name="x")` used to die on a NOT NULL), raises
  `CrossStoreReferenceError` when a different store is named, and raises
  `UnscopedQueryError` when an unpinned manager is asked to create with no store
  at all;
- `bulk_create` - same, per object;
- `get_or_create` / `update_or_create` - reject a conflicting store in `kwargs`,
  `defaults` or `create_defaults` before touching the database;
- `update` - already required a scope; now also refuses a `store=` that would
  move rows out of the pinned store.

### A5 - cross-organization integrity in the database - DONE (the real thing)

`StoreAccess` gained a denormalized non-null `org`, derived from its membership in
`clean_fields()` and `save()` so no caller has to pass it. `Store`, `Role` and
`Membership` gained `UniqueConstraint(fields=["id", "org"])` to serve as
composite-FK targets. The **uncommitted** `orgs/0001_initial` now installs, by
`RunSQL` with a matching `reverse_sql`:

```sql
orgs_membership  (role_id, org_id)       -> orgs_role (id, org_id)
orgs_storeaccess (membership_id, org_id) -> orgs_membership (id, org_id)
orgs_storeaccess (store_id, org_id)      -> orgs_store (id, org_id)
```

and `audit/0001_initial` installs:

```sql
audit_auditlog   (store_id, org_id)      -> orgs_store (id, org_id)
```

All `DEFERRABLE INITIALLY IMMEDIATE`: violations surface at the statement, where
the bug is, but a future service can still defer them inside a transaction if it
ever needs to insert out of order. `MATCH SIMPLE` (the default) is what makes the
nullable `AuditLog` pair work correctly - org-only and store-only rows stay
legal, a *disagreeing* pair does not.

Nothing had to fall back to the "service + `full_clean()`" floor. Tests assert
`IntegrityError` for all three reproductions plus the audit one, and a further
test reads `pg_constraint` to prove the four constraints exist in the schema.

Two notes for the database reviewer:

1. Deferrable-vs-immediate mattered for testability: with Django's own
   `INITIALLY DEFERRED` style, the violation would only surface at COMMIT, which
   never happens inside a test transaction, so the tests would have passed
   vacuously.
2. The composite-FK SQL is written literally in the migration rather than
   generated by a helper, deliberately: a migration should not change meaning
   when application code changes. (The append-only trigger in B2 is the opposite
   case - the reviewer asked for reuse there - and it carries a written stability
   contract.)

### A6 - `common.E005` - DONE

For every concrete store-scoped model the check now rejects: any `unique=True`
field, any `unique_together`, and any `UniqueConstraint` that does not reference
`store`/`store_id` **or** is not conditioned on `deleted_at IS NULL`. Expression
constraints are handled by walking the expression tree for `F`/function source
names, and the condition is inspected structurally (an AND-level
`("deleted_at__isnull", True)`, not a string match). Five check tests, including
one compliant constraint that must pass.

## B. Audit integrity and hard deletes

### B1 - forgery - DONE

```python
def save(self, **kwargs):
    if not self._state.adding or self.pk is not None:
        raise AppendOnlyError(...)
    kwargs["force_insert"] = True
    return super().save(**kwargs)
```

plus `Meta.base_manager_name = "objects"` (so `_base_manager` is the append-only
manager) and a `bulk_create` override that refuses `update_conflicts=True`. The
test constructs a row with an existing pk, saves, asserts `AppendOnlyError`, and
then compares a full column-by-column snapshot of the stored row taken before
the attempt - byte-identical, as required. `force_insert=True` from the caller is
refused too.

### B2 - database-level guard - DONE, as a reusable snippet

`common/db.py` holds the plpgsql function and a
`append_only_triggers(table) -> (forward, reverse)` helper;
`apps/audit/migrations/0002_append_only_trigger.py` calls it for
`audit_auditlog`. Slice 2's ledger tables install the identical guard with one
call. Function creation and trigger creation are separate `RunSQL` operations so
reversal drops the triggers first and the function second (and loudly fails
rather than silently cascading if another table still depends on it).

I did not have the reviewer's SQL text, so I wrote it to the stated shape:
plpgsql function + `BEFORE UPDATE OR DELETE ... FOR EACH ROW` + `BEFORE TRUNCATE
... FOR EACH STATEMENT`, `RAISE EXCEPTION ... USING ERRCODE = 'restrict_violation'`
(which psycopg surfaces as `IntegrityError`). One deviation worth your eyes:

**UPDATE and DELETE have no escape hatch at all.** TRUNCATE has two, because
Django's own `TransactionTestCase` teardown flushes tables with `TRUNCATE` and
would otherwise be unable to clean up (task 4 already plans a
`TransactionTestCase` race test): the guard exempts a database whose name starts
with `test_`, and a session that has explicitly set `raporo.allow_truncate='on'`
for a reviewed retention purge. A third setting,
`raporo.enforce_truncate_guard='on'`, turns both exemptions off - which is how
the test suite proves the TRUNCATE trigger really refuses, from inside a test
database. I considered the GUC-only design with an autouse fixture and rejected
it: the `SET` has to survive into pytest-django's teardown on the same
connection, which is fixture-ordering luck rather than a guarantee.
`REVOKE UPDATE, DELETE` stays rejected for the reason you gave.

Tests: the two triggers exist in `pg_trigger`; a raw `UPDATE` raises and the row
is unchanged; a raw `DELETE` raises and the row survives; `TRUNCATE` raises with
the exemption disabled. (The TRUNCATE test writes no row first - Postgres refuses
`TRUNCATE` outright in a transaction with pending trigger events on that table,
and that refusal would have masked the one under test.)

### B3 - `accounts.User` hard-delete guard - DONE

`User.delete()` raises `HardDeleteForbidden`; `UserManager` is built from
`NoHardDeleteQuerySet`; `Meta.base_manager_name = "objects"` closes the
`_base_manager` path. `is_active = False` remains the deactivation path, and no
erasure path was invented. Four tests. This also replaced an older test that
relied on `User.objects.delete()` raising `ProtectedError` - it now asserts the
PROTECT `on_delete` structurally instead.

### B4 - `all_objects` can no longer hard-delete - DONE

New `NoHardDeleteQuerySet` refuses `delete()`/`_raw_delete()` without filtering
`deleted_at`; `SoftDeleteQuerySet` extends it; `all_objects` is an
`AllObjectsManager` built on it. `SoftDeleteModel.Meta.base_manager_name` is now
`"all_objects"`, so `Model._base_manager.filter(...).delete()` is refused too -
that hole was the same shape as B1's and was not in the fix list.

## C. Checks, constraints, uploads

### C1 - `common.E002` is now a positive assertion - DONE

`if model._default_manager.name != "all_objects": Error(...)`. The check test
declares a model with an extra `recent = models.Manager()` and first asserts
Django really does hand it `_default_manager` (it does - `Options.default_manager`
skips the abstract-MRO fallback the moment a model has any local manager), then
asserts E002 fires. The false claim is corrected in the docstring and in section
D7 below.

### C2 - `Organization.slug` - DONE

`unique=True` dropped in favour of
`UniqueConstraint(fields=["slug"], condition=LIVE, name="orgs_organization_unique_live_slug")`
with a translated violation message. Tests: validation and database both reject a
duplicate live slug; a soft-deleted organization releases its slug. The
constraint is in the parametrized structural test alongside the other four, and
the migration docstring records that reversing on a database that has since
accumulated a soft-deleted-then-recreated duplicate slug will fail - correctly,
because it would mean losing real data.

### C3 - logo upload - DONE (model + settings layer)

- `models.ImageField` with three validators: extension allowlist
  (`png/jpg/jpeg/webp`), a 2 MB size cap, and Pillow content verification that
  decodes the bytes and checks the *detected* format, so `logo.svg` renamed to
  `logo.png` is rejected;
- `upload_to=organization_logo_path`, a module-level callable returning
  `org-logos/<uuid4 hex>.<ext>` - the attacker-chosen filename never reaches
  storage;
- `FILE_UPLOAD_MAX_MEMORY_SIZE`, `DATA_UPLOAD_MAX_MEMORY_SIZE`,
  `FILE_UPLOAD_PERMISSIONS = 0o644` in `base.py`;
- `MEDIA_ROOT` moved out of `BASE_DIR` (dev default `/var/tmp/raporo-media` via
  `DJANGO_MEDIA_ROOT`); `prod.py` reads `os.environ["DJANGO_MEDIA_ROOT"]` with no
  default, so production fails fast rather than writing to a temp directory;
- `Pillow==12.3.0` added to `requirements.txt` with the justification in a
  comment. Image rebuilt.

Six tests, including the disguised-SVG case and one asserting `MEDIA_ROOT` is not
under `BASE_DIR`. Serving media from a separate origin stays devops-engineer's,
as you parked it.

**One thing I could not do:** `.env.example` is inside a path my permission
settings deny, so I could not add the `DJANGO_MEDIA_ROOT` line to it. Please add:

```
# Where uploaded files (organization logos) are written. Outside the source
# tree, writable by the app user. Required in production.
DJANGO_MEDIA_ROOT=/var/lib/raporo/media
```

## D. The rest

| Item | Done | Note |
|---|---|---|
| D1 `\|` / `&` closure | yes | `__or__`/`__and__` require both operands pinned and merge the pinned sets (union for `\|`, intersection for `&`). `union()` was already safe - its combined queries each build their own compiler. 4 tests. |
| D2 `for_stores()` one org | yes | Resolves the pks through `Store.all_objects`, raises `ValueError` on unknown ids and `CrossStoreReferenceError` on more than one `org_id`. Costs one query on a rare call. |
| D3 username namespace | yes | `username_validator` (`^[\w.+-]+\Z`, no `@`) plus `validate_username_not_numeric` (also rejects `250-788-111-111`). 7 tests. |
| D4 store branding | yes | `Store.brand` (JSON, default `{}`) and `Store.use_own_branding` (default False), translated verbose names, folded into the uncommitted `orgs/0001_initial`. No resolution logic. **`resolve_branding(store)` is slice 4's job**; store *name* never inherits. |
| D5 unattributable tombstone | yes | `soft_delete(*, by, system=False)` on both model and queryset; `by=None` without `system=True` raises `ValueError`. Recorded here: **`soft_delete` writes no audit row** - task 4's services must. |
| D6 payload cap | yes | Strings over 1024 chars get `...[truncated]`; a payload over 16 KB serialised is replaced by `{"_truncated": "...[truncated]", "_reason": ..., "_original_bytes": ..., "_keys": [...]}`. Distinct from `[redacted]`. |
| D7 false docstrings | yes | `common/models.py` now says `common` *is* an installed app; `apps/orgs/models.py` says no store-scoped table carries an org pointer and names the models here that do; the `default_manager_name` paragraph now credits declaration order + E002 instead of a Meta inheritance that does not happen. The Meta-declaring-subclass test asserts both `_meta.default_manager_name is None` and `_default_manager.name == "all_objects"`. |
| D8 both unique indexes | yes | Meta comment records the `NON_FIELD_ERRORS` reason. |
| D9 `app_configs` | yes | Honoured, with a test that `manage.py check accounts` reports nothing. |
| D10 tautological tests | yes | The three are deleted. `test_no_store_scoped_model_carries_its_own_org_pointer` now walks every concrete `StoreScopedModel` in the registry and asserts none declares `org`/`organization`. |

---

## E. Evidence

### E.1 Migrations, from a wiped volume

```
$ docker compose down -v
$ docker compose run --rm web python manage.py migrate
Network raporo_default Creating 
 Network raporo_default Created 
 Volume raporo_pgdata Creating 
 Volume raporo_pgdata Created 
Operations to perform:
  Apply all migrations: accounts, admin, audit, auth, contenttypes, orgs, sessions
Running migrations:
  Applying contenttypes.0001_initial... OK
  Applying contenttypes.0002_remove_content_type_name... OK
  Applying auth.0001_initial... OK
  Applying auth.0002_alter_permission_name_max_length... OK
  Applying auth.0003_alter_user_email_max_length... OK
  Applying auth.0004_alter_user_username_opts... OK
  Applying auth.0005_alter_user_last_login_null... OK
  Applying auth.0006_require_contenttypes_0002... OK
  Applying auth.0007_alter_validators_add_error_messages... OK
  Applying auth.0008_alter_user_username_max_length... OK
  Applying auth.0009_alter_user_last_name_max_length... OK
  Applying auth.0010_alter_group_name_max_length... OK
  Applying auth.0011_update_proxy_permissions... OK
  Applying auth.0012_alter_user_first_name_max_length... OK
  Applying accounts.0001_initial... OK
  Applying admin.0001_initial... OK
  Applying admin.0002_logentry_remove_auto_add... OK
  Applying admin.0003_logentry_add_action_flag_choices... OK
  Applying orgs.0001_initial... OK
  Applying audit.0001_initial... OK
  Applying audit.0002_append_only_trigger... OK
  Applying sessions.0001_initial... OK
```

### E.2 `docker compose run --rm web pytest -v`

```
============================= test session starts ==============================
platform linux -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0 -- /usr/local/bin/python3.13
cachedir: .pytest_cache
django: version: 6.1, settings: config.settings.test (from option)
rootdir: /app
configfile: pytest.ini
plugins: django-4.14.0
collecting ... collected 254 items

tests/test_audit.py::test_record_writes_a_row PASSED                     [  0%]
tests/test_audit.py::test_record_without_a_target_or_actor_is_allowed PASSED [  0%]
tests/test_audit.py::test_record_derives_the_org_from_the_store PASSED   [  1%]
tests/test_audit.py::test_record_rejects_a_store_from_another_org PASSED [  1%]
tests/test_audit.py::test_record_rejects_a_malformed_action[] PASSED     [  1%]
tests/test_audit.py::test_record_rejects_a_malformed_action[   ] PASSED  [  2%]
tests/test_audit.py::test_record_rejects_a_malformed_action[User.Created] PASSED [  2%]
tests/test_audit.py::test_record_rejects_a_malformed_action[user created] PASSED [  3%]
tests/test_audit.py::test_record_rejects_a_malformed_action[user..created] PASSED [  3%]
tests/test_audit.py::test_record_rejects_a_malformed_action[user] PASSED [  3%]
tests/test_audit.py::test_record_rejects_a_malformed_action[xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx] PASSED [  4%]
tests/test_audit.py::test_record_rejects_a_malformed_action[None] PASSED [  4%]
tests/test_audit.py::test_record_rejects_an_unsaved_target PASSED        [  5%]
tests/test_audit.py::test_record_redacts_sensitive_values PASSED         [  5%]
tests/test_audit.py::test_record_rejects_changes_it_cannot_serialise PASSED [  5%]
tests/test_audit.py::test_record_requires_a_mapping_for_changes PASSED   [  6%]
tests/test_audit.py::test_record_serialises_decimals_and_dates PASSED    [  6%]
tests/test_audit.py::test_record_stores_the_ip PASSED                    [  7%]
tests/test_audit.py::test_record_rejects_a_bogus_ip PASSED               [  7%]
tests/test_audit.py::test_audit_rows_cannot_be_updated PASSED            [  7%]
tests/test_audit.py::test_audit_rows_cannot_be_bulk_updated PASSED       [  8%]
tests/test_audit.py::test_audit_rows_cannot_be_deleted PASSED            [  8%]
tests/test_audit.py::test_the_action_field_carries_the_validator PASSED  [  9%]
tests/test_audit.py::test_audit_log_has_no_soft_delete_columns PASSED    [  9%]
tests/test_audit.py::test_the_actor_reference_is_protected PASSED        [  9%]
tests/test_audit.py::test_newest_rows_come_first PASSED                  [ 10%]
tests/test_audit.py::test_an_audit_row_cannot_be_overwritten_through_an_explicit_pk PASSED [ 10%]
tests/test_audit.py::test_an_audit_row_cannot_be_overwritten_with_force_insert PASSED [ 11%]
tests/test_audit.py::test_the_base_manager_cannot_delete_audit_rows PASSED [ 11%]
tests/test_audit.py::test_bulk_create_cannot_be_turned_into_an_upsert PASSED [ 11%]
tests/test_audit.py::test_the_append_only_triggers_exist_in_postgres PASSED [ 12%]
tests/test_audit.py::test_the_database_refuses_a_raw_update PASSED       [ 12%]
tests/test_audit.py::test_the_database_refuses_a_raw_delete PASSED       [ 12%]
tests/test_audit.py::test_the_database_refuses_a_truncate PASSED         [ 13%]
tests/test_audit.py::test_audit_rows_cannot_mix_an_org_with_a_foreign_store PASSED [ 13%]
tests/test_audit.py::test_an_org_only_or_store_only_audit_row_is_still_legal PASSED [ 14%]
tests/test_audit.py::test_a_long_string_is_truncated_distinguishably PASSED [ 14%]
tests/test_audit.py::test_an_oversized_payload_is_replaced_by_a_marker PASSED [ 14%]
tests/test_common_bases.py::test_soft_delete_hides_from_default_manager PASSED [ 15%]
tests/test_common_bases.py::test_soft_delete_stamps_who_and_when PASSED  [ 15%]
tests/test_common_bases.py::test_soft_delete_is_idempotent PASSED        [ 16%]
tests/test_common_bases.py::test_soft_delete_requires_an_actor_keyword PASSED [ 16%]
tests/test_common_bases.py::test_hard_delete_is_forbidden_on_instances PASSED [ 16%]
tests/test_common_bases.py::test_hard_delete_is_forbidden_on_querysets PASSED [ 17%]
tests/test_common_bases.py::test_queryset_soft_delete_stamps_every_row PASSED [ 17%]
tests/test_common_bases.py::test_audited_model_stamps_timestamps PASSED  [ 18%]
tests/test_common_bases.py::test_audited_actor_fields_are_optional_but_protected PASSED [ 18%]
tests/test_common_bases.py::test_no_store_scoped_model_carries_its_own_org_pointer PASSED [ 18%]
tests/test_common_bases.py::test_store_fk_is_declared_by_the_base_not_by_consumers PASSED [ 19%]
tests/test_common_bases.py::test_unscoped_query_raises[list] PASSED      [ 19%]
tests/test_common_bases.py::test_unscoped_query_raises[len] PASSED       [ 20%]
tests/test_common_bases.py::test_unscoped_query_raises[bool] PASSED      [ 20%]
tests/test_common_bases.py::test_unscoped_query_raises[count] PASSED     [ 20%]
tests/test_common_bases.py::test_unscoped_query_raises[exists] PASSED    [ 21%]
tests/test_common_bases.py::test_unscoped_query_raises[first] PASSED     [ 21%]
tests/test_common_bases.py::test_unscoped_query_raises[last] PASSED      [ 22%]
tests/test_common_bases.py::test_unscoped_query_raises[get] PASSED       [ 22%]
tests/test_common_bases.py::test_unscoped_query_raises[aggregate] PASSED [ 22%]
tests/test_common_bases.py::test_unscoped_query_raises[iterator] PASSED  [ 23%]
tests/test_common_bases.py::test_unscoped_query_raises[values_list] PASSED [ 23%]
tests/test_common_bases.py::test_unscoped_query_raises[chained_filter] PASSED [ 24%]
tests/test_common_bases.py::test_unscoped_query_raises[in_bulk] PASSED   [ 24%]
tests/test_common_bases.py::test_unscoped_query_raises[explain] PASSED   [ 24%]
tests/test_common_bases.py::test_unscoped_query_raises[update] PASSED    [ 25%]
tests/test_common_bases.py::test_unscoped_query_raises_on_the_manager_itself PASSED [ 25%]
tests/test_common_bases.py::test_unscoped_subquery_raises PASSED         [ 25%]
tests/test_common_bases.py::test_raw_sql_is_refused_on_the_scoped_manager PASSED [ 26%]
tests/test_common_bases.py::test_own_meta_child_is_still_guarded PASSED  [ 26%]
tests/test_common_bases.py::test_for_store_returns_only_that_store PASSED [ 27%]
tests/test_common_bases.py::test_for_store_hides_soft_deleted_rows PASSED [ 27%]
tests/test_common_bases.py::test_for_store_survives_further_chaining PASSED [ 27%]
tests/test_common_bases.py::test_for_store_accepts_a_primary_key PASSED  [ 28%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[None] PASSED [ 28%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[] PASSED [ 29%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[0] PASSED [ 29%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[bad3] PASSED [ 29%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[not-a-store] PASSED [ 30%]
tests/test_common_bases.py::test_for_store_rejects_a_saved_instance_of_another_model PASSED [ 30%]
tests/test_common_bases.py::test_for_stores_rejects_a_saved_instance_of_another_model PASSED [ 31%]
tests/test_common_bases.py::test_for_store_rejects_an_unsaved_store PASSED [ 31%]
tests/test_common_bases.py::test_for_stores_covers_several_stores_and_nothing_else PASSED [ 31%]
tests/test_common_bases.py::test_for_stores_rejects_an_empty_collection PASSED [ 32%]
tests/test_common_bases.py::test_scoped_update_is_allowed_and_stays_scoped PASSED [ 32%]
tests/test_common_bases.py::test_scoped_queryset_still_refuses_hard_delete PASSED [ 33%]
tests/test_common_bases.py::test_creating_a_row_needs_a_store_named_or_pinned PASSED [ 33%]
tests/test_common_bases.py::test_creating_a_row_with_no_store_at_all_is_refused PASSED [ 33%]
tests/test_common_bases.py::test_for_store_fills_in_the_store_on_create PASSED [ 34%]
tests/test_common_bases.py::test_for_store_refuses_to_create_in_another_store PASSED [ 34%]
tests/test_common_bases.py::test_for_store_refuses_to_bulk_create_in_another_store PASSED [ 35%]
tests/test_common_bases.py::test_bulk_create_fills_in_the_pinned_store PASSED [ 35%]
tests/test_common_bases.py::test_update_cannot_move_a_row_to_another_store PASSED [ 35%]
tests/test_common_bases.py::test_get_or_create_cannot_reach_into_another_store PASSED [ 36%]
tests/test_common_bases.py::test_get_or_create_uses_the_pinned_store PASSED [ 36%]
tests/test_common_bases.py::test_all_objects_is_the_documented_escape_hatch PASSED [ 37%]
tests/test_common_bases.py::test_django_internals_use_an_unguarded_default_manager PASSED [ 37%]
tests/test_common_bases.py::test_store_scoped_models_have_no_reverse_accessor_from_store PASSED [ 37%]
tests/test_common_bases.py::test_validation_error_is_not_how_scope_violations_surface PASSED [ 38%]
tests/test_common_bases.py::test_an_org_level_parent_has_no_accessor_to_its_store_scoped_children PASSED [ 38%]
tests/test_common_bases.py::test_a_store_scoped_parent_has_no_accessor_to_its_children PASSED [ 38%]
tests/test_common_bases.py::test_children_are_read_through_for_store PASSED [ 39%]
tests/test_common_bases.py::test_a_cross_store_foreign_key_is_refused_on_create PASSED [ 39%]
tests/test_common_bases.py::test_a_cross_store_foreign_key_is_refused_on_a_plain_save PASSED [ 40%]
tests/test_common_bases.py::test_a_cross_store_foreign_key_is_refused_in_bulk_create PASSED [ 40%]
tests/test_common_bases.py::test_a_cross_store_foreign_key_is_refused_when_only_the_id_is_given PASSED [ 40%]
tests/test_common_bases.py::test_same_store_foreign_keys_are_fine PASSED [ 41%]
tests/test_common_bases.py::test_a_partial_save_skips_the_unrelated_relation_check PASSED [ 41%]
tests/test_common_bases.py::test_a_scoped_query_cannot_join_back_out_to_other_tenants PASSED [ 42%]
tests/test_common_bases.py::test_the_join_existence_oracle_is_gone PASSED [ 42%]
tests/test_common_bases.py::test_the_aggregate_shape_of_the_same_leak_is_gone PASSED [ 42%]
tests/test_common_bases.py::test_a_scoped_query_reads_its_own_store_only PASSED [ 43%]
tests/test_common_bases.py::test_all_objects_cannot_hard_delete PASSED   [ 43%]
tests/test_common_bases.py::test_the_base_manager_cannot_hard_delete_either PASSED [ 44%]
tests/test_common_bases.py::test_all_objects_still_sees_retired_rows PASSED [ 44%]
tests/test_common_bases.py::test_or_with_an_unscoped_queryset_is_refused PASSED [ 44%]
tests/test_common_bases.py::test_and_with_an_unscoped_queryset_is_refused PASSED [ 45%]
tests/test_common_bases.py::test_or_of_two_scoped_querysets_stays_scoped PASSED [ 45%]
tests/test_common_bases.py::test_and_of_two_scoped_querysets_stays_scoped PASSED [ 46%]
tests/test_common_bases.py::test_for_stores_refuses_a_mixed_organization_set PASSED [ 46%]
tests/test_common_bases.py::test_for_stores_refuses_unknown_store_ids PASSED [ 46%]
tests/test_common_bases.py::test_for_stores_accepts_several_stores_of_one_org PASSED [ 47%]
tests/test_common_bases.py::test_soft_delete_refuses_a_missing_actor PASSED [ 47%]
tests/test_common_bases.py::test_soft_delete_accepts_a_declared_system_action PASSED [ 48%]
tests/test_common_bases.py::test_queryset_soft_delete_refuses_a_missing_actor PASSED [ 48%]
tests/test_common_bases.py::test_the_hidden_relation_cannot_be_traversed_from_the_parent PASSED [ 48%]
tests/test_common_bases.py::test_the_hidden_relation_cannot_be_traversed_from_a_scoped_query PASSED [ 49%]
tests/test_common_bases.py::test_the_hidden_relation_is_refused_on_all_objects_too PASSED [ 49%]
tests/test_healthz.py::test_healthz_returns_ok PASSED                    [ 50%]
tests/test_orgs_models.py::test_organization_defaults_are_rwanda_first PASSED [ 50%]
tests/test_orgs_models.py::test_organization_slug_is_unique PASSED       [ 50%]
tests/test_orgs_models.py::test_organization_slug_uniqueness_is_enforced_by_the_database PASSED [ 51%]
tests/test_orgs_models.py::test_a_soft_deleted_organization_releases_its_slug PASSED [ 51%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[rwf] PASSED [ 51%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[RW] PASSED [ 52%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[RWFX] PASSED [ 52%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[R1F] PASSED [ 53%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[] PASSED [ 53%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[Africa/Kigaly] PASSED [ 53%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[CAT] PASSED [ 54%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[] PASSED [ 54%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[UTC+2] PASSED [ 55%]
tests/test_orgs_models.py::test_organization_accepts_another_real_timezone PASSED [ 55%]
tests/test_orgs_models.py::test_store_name_is_unique_within_an_org PASSED [ 55%]
tests/test_orgs_models.py::test_store_name_uniqueness_is_enforced_by_the_database PASSED [ 56%]
tests/test_orgs_models.py::test_the_same_store_name_is_fine_in_another_org PASSED [ 56%]
tests/test_orgs_models.py::test_a_soft_deleted_store_name_can_be_reused PASSED [ 57%]
tests/test_orgs_models.py::test_store_carries_the_only_org_pointer PASSED [ 57%]
tests/test_orgs_models.py::test_permission_catalog_is_exactly_the_agreed_set PASSED [ 57%]
tests/test_orgs_models.py::test_presets_are_owner_manager_seller PASSED  [ 58%]
tests/test_orgs_models.py::test_manager_preset_runs_a_store_but_does_not_own_the_org PASSED [ 58%]
tests/test_orgs_models.py::test_role_rejects_an_unknown_permission_code PASSED [ 59%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[sale.record] PASSED [ 59%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[bad1] PASSED [ 59%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[bad2] PASSED [ 60%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[bad3] PASSED [ 60%]
tests/test_orgs_models.py::test_role_accepts_catalog_codes_and_answers_has PASSED [ 61%]
tests/test_orgs_models.py::test_role_name_is_unique_within_an_org PASSED [ 61%]
tests/test_orgs_models.py::test_role_defaults_to_no_permissions_and_not_preset PASSED [ 61%]
tests/test_orgs_models.py::test_membership_is_unique_per_user_and_org PASSED [ 62%]
tests/test_orgs_models.py::test_membership_uniqueness_is_enforced_by_the_database PASSED [ 62%]
tests/test_orgs_models.py::test_membership_rejects_a_role_from_another_org PASSED [ 62%]
tests/test_orgs_models.py::test_store_access_rejects_a_store_from_another_org PASSED [ 63%]
tests/test_orgs_models.py::test_store_access_is_unique_per_membership_and_store PASSED [ 63%]
tests/test_orgs_models.py::test_store_access_accepts_a_store_in_the_same_org PASSED [ 64%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Organization.orgs_organization_unique_live_slug] PASSED [ 64%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Store.orgs_store_unique_live_name_per_org] PASSED [ 64%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Role.orgs_role_unique_live_name_per_org] PASSED [ 65%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Membership.orgs_membership_unique_live_user_per_org] PASSED [ 65%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[StoreAccess.orgs_storeaccess_unique_live_membership_store] PASSED [ 66%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Organization] PASSED [ 66%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Store] PASSED [ 66%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Role] PASSED [ 67%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Membership] PASSED [ 67%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[StoreAccess] PASSED [ 68%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Organization] PASSED [ 68%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Store] PASSED [ 68%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Role] PASSED [ 69%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Membership] PASSED [ 69%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[StoreAccess] PASSED [ 70%]
tests/test_orgs_models.py::test_orgs_models_are_not_store_scoped PASSED  [ 70%]
tests/test_orgs_models.py::test_store_branding_defaults_to_inheriting_the_org PASSED [ 70%]
tests/test_orgs_models.py::test_store_brand_must_be_a_mapping PASSED     [ 71%]
tests/test_orgs_models.py::test_membership_cannot_take_a_role_from_another_org PASSED [ 71%]
tests/test_orgs_models.py::test_store_access_cannot_reach_a_store_in_another_org PASSED [ 72%]
tests/test_orgs_models.py::test_store_access_derives_its_org_from_the_membership PASSED [ 72%]
tests/test_orgs_models.py::test_store_access_full_clean_does_not_demand_the_derived_org PASSED [ 72%]
tests/test_orgs_models.py::test_the_same_org_composite_keys_exist_in_postgres PASSED [ 73%]
tests/test_orgs_models.py::test_a_real_png_is_accepted PASSED            [ 73%]
tests/test_orgs_models.py::test_an_svg_disguised_as_a_png_is_rejected PASSED [ 74%]
tests/test_orgs_models.py::test_an_svg_extension_is_rejected PASSED      [ 74%]
tests/test_orgs_models.py::test_an_oversized_image_is_rejected PASSED    [ 74%]
tests/test_orgs_models.py::test_the_stored_logo_filename_is_random PASSED [ 75%]
tests/test_orgs_models.py::test_media_root_is_outside_the_source_tree PASSED [ 75%]
tests/test_user_model.py::test_the_project_user_model_is_ours PASSED     [ 75%]
tests/test_user_model.py::test_username_field_and_required_fields PASSED [ 76%]
tests/test_user_model.py::test_phone_rejects_bad_formats[+250788123456] PASSED [ 76%]
tests/test_user_model.py::test_phone_rejects_bad_formats[0788123456] PASSED [ 77%]
tests/test_user_model.py::test_phone_rejects_bad_formats[250 788 123 456] PASSED [ 77%]
tests/test_user_model.py::test_phone_rejects_bad_formats[250788] PASSED  [ 77%]
tests/test_user_model.py::test_phone_rejects_bad_formats[2507881234567890] PASSED [ 78%]
tests/test_user_model.py::test_phone_rejects_bad_formats[25078812345a] PASSED [ 78%]
tests/test_user_model.py::test_phone_rejects_bad_formats[] PASSED        [ 79%]
tests/test_user_model.py::test_phone_accepts_country_code_digits[250788123456] PASSED [ 79%]
tests/test_user_model.py::test_phone_accepts_country_code_digits[12345678] PASSED [ 79%]
tests/test_user_model.py::test_phone_accepts_country_code_digits[999999999999999] PASSED [ 80%]
tests/test_user_model.py::test_email_is_required PASSED                  [ 80%]
tests/test_user_model.py::test_email_must_be_unique_case_insensitively PASSED [ 81%]
tests/test_user_model.py::test_email_uniqueness_is_enforced_by_the_database_too PASSED [ 81%]
tests/test_user_model.py::test_username_must_be_unique_case_insensitively PASSED [ 81%]
tests/test_user_model.py::test_phone_must_be_unique PASSED               [ 82%]
tests/test_user_model.py::test_language_defaults_to_english PASSED       [ 82%]
tests/test_user_model.py::test_language_choices_match_the_configured_languages PASSED [ 83%]
tests/test_user_model.py::test_language_rejects_an_unconfigured_code PASSED [ 83%]
tests/test_user_model.py::test_password_is_argon2 PASSED                 [ 83%]
tests/test_user_model.py::test_create_user_validates_its_input PASSED    [ 84%]
tests/test_user_model.py::test_create_user_requires_identity_fields[username] PASSED [ 84%]
tests/test_user_model.py::test_create_user_requires_identity_fields[email] PASSED [ 85%]
tests/test_user_model.py::test_create_user_requires_identity_fields[phone] PASSED [ 85%]
tests/test_user_model.py::test_create_user_normalises_the_email_domain PASSED [ 85%]
tests/test_user_model.py::test_create_user_without_a_password_cannot_log_in PASSED [ 86%]
tests/test_user_model.py::test_create_superuser_is_staff_and_superuser PASSED [ 86%]
tests/test_user_model.py::test_create_superuser_refuses_to_be_downgraded PASSED [ 87%]
tests/test_user_model.py::test_new_users_are_active_and_not_staff PASSED [ 87%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[victim@example.com] PASSED [ 87%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[250788111111] PASSED [ 88%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[250-788-111-111] PASSED [ 88%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[eva mugisha] PASSED [ 88%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[eva@] PASSED [ 89%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva] PASSED [ 89%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva.mugisha] PASSED [ 90%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva_m1] PASSED [ 90%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva-m] PASSED [ 90%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva+shop] PASSED [ 91%]
tests/test_user_model.py::test_create_user_rejects_an_ambiguous_username PASSED [ 91%]
tests/test_user_model.py::test_a_user_cannot_be_hard_deleted PASSED      [ 92%]
tests/test_user_model.py::test_users_cannot_be_hard_deleted_in_bulk PASSED [ 92%]
tests/test_user_model.py::test_the_user_base_manager_cannot_hard_delete_either PASSED [ 92%]
tests/test_user_model.py::test_deactivation_is_the_supported_path PASSED [ 93%]
tests/test_common_checks.py::test_the_real_models_pass PASSED            [ 93%]
tests/test_common_checks.py::test_non_scoped_models_are_ignored PASSED   [ 94%]
tests/test_common_checks.py::test_the_check_honours_the_app_configs_it_is_given PASSED [ 94%]
tests/test_common_checks.py::test_a_model_that_overrides_objects_is_rejected PASSED [ 94%]
tests/test_common_checks.py::test_an_extra_manager_that_steals_the_default_is_rejected PASSED [ 95%]
tests/test_common_checks.py::test_a_model_that_makes_the_guarded_manager_the_default_is_rejected PASSED [ 95%]
tests/test_common_checks.py::test_the_real_models_keep_the_unguarded_default_manager PASSED [ 96%]
tests/test_common_checks.py::test_a_model_that_repoints_the_store_fk_is_rejected PASSED [ 96%]
tests/test_common_checks.py::test_a_model_that_makes_the_store_optional_is_rejected PASSED [ 96%]
tests/test_common_checks.py::test_a_forward_fk_that_creates_an_accessor_on_the_parent_is_rejected PASSED [ 97%]
tests/test_common_checks.py::test_a_reverse_accessor_into_a_store_scoped_model_is_rejected PASSED [ 97%]
tests/test_common_checks.py::test_a_global_unique_field_is_rejected PASSED [ 98%]
tests/test_common_checks.py::test_a_unique_constraint_without_store_is_rejected PASSED [ 98%]
tests/test_common_checks.py::test_a_unique_constraint_that_ignores_soft_delete_is_rejected PASSED [ 98%]
tests/test_common_checks.py::test_unique_together_is_rejected PASSED     [ 99%]
tests/test_common_checks.py::test_a_per_store_live_unique_constraint_passes PASSED [ 99%]
tests/test_common_checks.py::test_an_org_level_model_pointing_at_a_store_scoped_model_is_rejected PASSED [100%]

============================= 254 passed in 12.64s =============================
```

### E.3 `ruff check .`

```
$ docker compose run --rm web ruff check .
All checks passed!
```

### E.4 Missing migrations, both settings modules

```
$ docker compose run --rm web python manage.py makemigrations --check --dry-run
No changes detected

$ docker compose run --rm web python manage.py makemigrations --check --dry-run --settings=config.settings.test
No changes detected
```

### E.5 `manage.py check`

```
$ docker compose run --rm web python manage.py check
System check identified no issues (0 silenced).
```

---

## F. Mutation pass over the new guards

Same method as round 1: remove one guard, run the suite, restore. Harness deleted
afterwards.

```
BASELINE: 254 passed in 13.51s
CAUGHT    A1_no_e004                       2 failed, 252 passed in 12.67s
CAUGHT    A2_no_same_store_fk_check        3 failed, 251 passed in 12.16s
CAUGHT    A4_create_ignores_pin            4 failed, 250 passed in 13.59s
CAUGHT    A4_bulk_create_ignores_pin       1 failed, 253 passed in 12.44s
CAUGHT    A4_update_can_move_stores        1 failed, 253 passed in 11.58s
CAUGHT    A6_no_e005                       4 failed, 250 passed in 11.35s
CAUGHT    B1_pk_forgery_allowed            2 failed, 252 passed in 11.98s
CAUGHT    B1_no_force_insert               3 failed, 251 passed in 12.41s
CAUGHT    B1_base_manager_open             1 failed, 253 passed in 11.56s
CAUGHT    B1_upsert_allowed                1 failed, 253 passed in 12.55s
CAUGHT    B3_user_delete_allowed           1 failed, 253 passed in 12.89s
SURVIVED! B4_all_objects_can_delete        254 passed in 12.55s
CAUGHT    C1_e002_negative_only            1 failed, 253 passed in 11.92s
CAUGHT    C2_slug_unique_forever           1 failed, 253 passed in 12.06s
CAUGHT    D1_or_not_guarded                1 failed, 253 passed in 11.62s
CAUGHT    D2_for_stores_any_org            1 failed, 253 passed in 11.62s
CAUGHT    D3_username_wide_open            5 failed, 249 passed in 11.71s
CAUGHT    D5_unattributable_tombstone      2 failed, 252 passed in 11.46s
CAUGHT    D6_no_payload_cap                2 failed, 252 passed in 11.11s
CAUGHT    PLUS_path_traversable            3 failed, 251 passed in 11.84s
RESTORED: 254 passed in 11.66s
```

`B4_all_objects_can_delete` survived only because the mutation was ineffective -
it swapped the manager's base class while `get_queryset()` still names
`NoHardDeleteQuerySet` explicitly. Re-run against the real line:

```
$ python .mutate.py B4_all_objects_can_delete && pytest -q --reuse-db
FAILED tests/test_common_bases.py::test_all_objects_cannot_hard_delete - Fail...
FAILED tests/test_common_bases.py::test_the_base_manager_cannot_hard_delete_either
2 failed, 252 passed in 16.96s
```

So: 20 of 20 guards are covered by a test that fails when the guard goes.

---

## G. Files touched in this round

Created:
- `/home/elvis/projects/2026/personal/raporo/common/db.py`
- `/home/elvis/projects/2026/personal/raporo/apps/audit/migrations/0002_append_only_trigger.py`

Rewritten or edited:
- `/home/elvis/projects/2026/personal/raporo/common/managers.py`
- `/home/elvis/projects/2026/personal/raporo/common/models.py`
- `/home/elvis/projects/2026/personal/raporo/common/checks.py`
- `/home/elvis/projects/2026/personal/raporo/common/validators.py`
- `/home/elvis/projects/2026/personal/raporo/apps/accounts/models.py`
- `/home/elvis/projects/2026/personal/raporo/apps/accounts/managers.py`
- `/home/elvis/projects/2026/personal/raporo/apps/audit/models.py`
- `/home/elvis/projects/2026/personal/raporo/apps/audit/services.py`
- `/home/elvis/projects/2026/personal/raporo/apps/orgs/models.py`
- `/home/elvis/projects/2026/personal/raporo/config/settings/base.py`
- `/home/elvis/projects/2026/personal/raporo/config/settings/prod.py`
- `/home/elvis/projects/2026/personal/raporo/requirements.txt`
- `/home/elvis/projects/2026/personal/raporo/.gitignore`
- `/home/elvis/projects/2026/personal/raporo/tests/conftest.py`
- `/home/elvis/projects/2026/personal/raporo/tests/testapp/models.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_common_bases.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_common_checks.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_orgs_models.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_audit.py`
- `/home/elvis/projects/2026/personal/raporo/tests/test_user_model.py`

Regenerated (all three were uncommitted, so the fixes are folded in rather than
stacked):
- `/home/elvis/projects/2026/personal/raporo/apps/accounts/migrations/0001_initial.py`
- `/home/elvis/projects/2026/personal/raporo/apps/orgs/migrations/0001_initial.py`
- `/home/elvis/projects/2026/personal/raporo/apps/audit/migrations/0001_initial.py`
- `/home/elvis/projects/2026/personal/raporo/tests/testapp/migrations/0001_initial.py`

---

## H. Carry-forward (from the review, plus what this round added)

Kept verbatim from the fix list so it survives:

1. **Task 5 (auth backend):** do **not** use `__iexact` on `username`/`email`.
   Postgres compiles it to `UPPER(col::text) = UPPER(%s)`, which cannot use the
   `lower()` functional index and will sequential-scan. Match the constraint's own
   expression (`filter(username=Lower(identifier))`). `full_clean()` is fine as-is.
   Pick the field by identifier shape and use `.get()`, never `.first()`.
2. **Task 4 (services):** soft-deleting a parent leaves live children pointing at a
   dead store - PROTECT never fires because hard delete is forbidden, and
   `for_store(<soft-deleted store>)` still returns rows. Services need an explicit
   policy: cascade the soft delete, or refuse while live children exist.
3. **Tasks 4/10:** nothing auto-eager-loads. Use
   `Membership.objects.filter(org=org).select_related("user", "role").prefetch_related("store_access__store")`.
4. Signup/reset must never echo `violation_error_message` text; reset always
   reports success, signup notifies the existing address out-of-band.

Added by this round:

5. **`soft_delete()` writes no audit row.** Every service that retires a row must
   call `audit.services.record(...)` itself.
6. **`resolve_branding(store)` is slice 4's job** (D4). The fields exist; the
   three-level chain (store → org → Raporo default, per-field fallback, name never
   inherited) is not implemented anywhere yet.
7. **New store-scoped models must not declare their own manager.** `common.E002`
   turns it into a startup error rather than a silent change to what unique
   validation, forms and the admin see. Uniqueness must be per store and
   conditioned on `deleted_at IS NULL` (`common.E005`), and every relation touching
   a store-scoped model needs `related_name="+"` (`common.E004`).
8. **`_assert_related_stores_match` costs one indexed query per store-scoped FK
   that was passed as a raw id** (none when the caller passes the object, because
   the field's cached instance is used). Services that write many rows in a loop
   should pass objects, or use
   `objects.for_store(store).bulk_create(...)`, which checks each row once.
9. **`bulk_update` is only partly covered.** It routes through `update()`, so the
   scope guard applies, but a `store` change expressed as a `Case` expression is
   not detected. Nothing uses `bulk_update` yet; if a service needs it, move the
   store column out of the update or add a check there.
10. **`raporo.allow_truncate` / `raporo.enforce_truncate_guard`** are the two
    session settings the append-only trigger reads (B2). Any retention purge is a
    reviewed, human-run operation that sets the first one; nothing in the
    application should ever set it.
11. **`.env.example` still needs `DJANGO_MEDIA_ROOT`** (C3) - I am not permitted to
    edit that file.
12. **`User.objects` / `AuditLog.objects` are not `GuardedQuery`-backed**, so a
    hand-built `+` lookup key rooted at those two models still resolves. Never
    build ORM lookup keys from request data.

---

# FIX ROUND 2

Scope handed to me: **B1, B4, B5, B6**. A1, A2, B2 and B3 were already on disk
when I started; I verified they are present and did not touch them. Section C
(devops DB roles, the ops naming invariant, the username `CheckConstraint`),
`.env*`, `README.md` and `docs/SETUP.md` were out of scope and are untouched.

## B1 - the `common/db.py` stability contract, enforced

**Design chosen: (b) versioned constants *and* (c) a pinned hash - they are not
alternatives.** (b) is structural and (c) is the tripwire.

- (b) alone is a naming convention. It makes an in-place edit *look* wrong, but
  nothing stops it, and "looks wrong" is exactly what the previous docstring
  already tried and failed to be.
- (c) alone fires in CI, but leaves the engineer who tripped it with no sanctioned
  way forward: the natural response to "hash mismatch" is to update the hash.
- (a) - embedding the literal SQL in each migration - was rejected. It is the only
  option that is genuinely immune, but it deletes the reason `common/db.py` exists:
  slice 2 has four ledger tables (StockMovement, Payment, CapitalEntry, Payout) and
  four copy-pasted plpgsql bodies drift by hand within one review cycle. Freezing
  one shared copy is stronger than not sharing it.

Together: the name states which shipped migrations depend on the exact text, the
hash makes an edit fail, and the failure message names the sanctioned move
(add `_V2` + a new migration) rather than "update this hash".

Changed:

- `common/db.py:39-110` - `CREATE_APPEND_ONLY_FUNCTION` -> `CREATE_APPEND_ONLY_FUNCTION_V1`,
  `DROP_APPEND_ONLY_FUNCTION` -> `DROP_APPEND_ONLY_FUNCTION_V1`,
  `append_only_triggers` -> `append_only_triggers_v1`. **No unversioned alias is
  kept** - an alias re-opens the trap, because the next author imports the short
  name and a later V3 silently changes what their migration installs. The Postgres
  function name stays `raporo_append_only` and the frozen SQL spells it out
  literally instead of interpolating `APPEND_ONLY_FUNCTION`, so renaming the
  Python constant cannot rewrite frozen text. Module docstring (`common/db.py:1-33`)
  now documents the V2 procedure: `CREATE OR REPLACE` the same function so a fresh
  install (V1 then V2) and an already-migrated database (V2) converge on one body.
- `apps/audit/migrations/0002_append_only_trigger.py:20-34,42-43` - imports the `_V1`
  names. **The SQL it applies is byte-identical**; verified by diffing the strings
  produced by `HEAD:common/db.py` against the new module:
  `create IDENTICAL / drop IDENTICAL / fwd IDENTICAL / rev IDENTICAL`.
- `tests/test_db_stability.py` (new, 5 tests) - SHA-256 of all four frozen strings,
  plus the SQL the migration's `operations` actually carry (pinning the module is
  not enough: the migration could be edited to build its SQL some other way), plus
  two structural tests - every SQL string or SQL-emitting helper exported by
  `common/db.py` must carry a version suffix, and no migration may import a name
  from `common.db` that `PINNED_SQL` does not cover. The second one is what will
  stop slice 2 from re-creating an unenforced contract for its own tables.

Mutations run (all caught):

| mutation | result |
|---|---|
| add `  -- harmless tidy-up` inside `CREATE_APPEND_ONLY_FUNCTION_V1` | `test_the_frozen_function_sql_has_not_changed` **and** `test_the_shipped_migration_still_carries_exactly_the_pinned_sql` fail; message prints the V2 procedure, the expected/actual hashes and the full offending text |
| append `CREATE_APPEND_ONLY_FUNCTION = CREATE_APPEND_ONLY_FUNCTION_V1` | `test_every_sql_helper_in_common_db_is_versioned` fails: `common/db.py exports unversioned SQL: ['CREATE_APPEND_ONLY_FUNCTION']` |
| add `apps/audit/migrations/0003_probe.py` importing a new unpinned `APPEND_ONLY_FUNCTION_V2` | `test_no_migration_imports_an_unpinned_name_from_common_db` fails and names the file and the symbol |

## B4 - stale reversibility docstring

`apps/orgs/migrations/0001_initial.py:16-21`. The old paragraph described reversing
to "a plain `unique=True` slug", a scenario that requires the partial-unique
constraint to have arrived in a *later* migration. It arrives in this one
(`AddConstraint` at line 167), so `migrate orgs zero` drops the composite keys,
then the constraints, then the tables. Rewritten to say that: no step can fail on
existing data because nothing survives to conflict, and reversal destroys every
organization, store, role, membership and store-access row. No new caveats invented.
Docstring only - no test, as agreed.

## B5 - the `app_configs` test now earns its name

`tests/test_common_checks.py:45-93`. Two tests, one per direction, and both bite:

- `test_the_check_honours_the_app_configs_it_is_given` declares a rogue
  `unique=True` model in an `@isolate_apps("tests.testapp")` registry, asserts the
  premise (that model is the only model in the config being passed), then asserts
  the check **reports** `common.E005` when handed `testapp` and **stays silent**
  when handed `accounts`. The rogue model exists only in the isolated registry, so
  a check that ignored `app_configs` and fell back to the project registry could
  not see it.
- `test_the_check_stays_silent_about_apps_it_was_not_given` covers the other
  direction with the rogue model reachable from the registry the whole-project run
  walks (`monkeypatch.setattr(checks, "global_apps", ...)`), so a check that
  dropped the filter would report it while being asked only about `accounts`.

Mutation - `common/checks.py:check_store_scoped_models` reduced to
`models = global_apps.get_models()` (the `app_configs` branch deleted):

```
>       assert "common.E005" in ids(check_store_scoped_models([testapp_config]))
E       AssertionError: assert 'common.E005' in set()
>       assert check_store_scoped_models([global_apps.get_app_config("accounts")]) == []
E       AssertionError: assert [<Error: leve...common.E005'>] == []
FAILED tests/test_common_checks.py::test_the_check_honours_the_app_configs_it_is_given
FAILED tests/test_common_checks.py::test_the_check_stays_silent_about_apps_it_was_not_given
2 failed, 16 passed
```

Filter restored: `18 passed`.

## B6 - loaddata against the composite FKs, and a correction

**The premise this item was written on is false, and the test now says so.**

The brief (and the database-engineer's decision verdict) held that Django's
Postgres backend issues `SET CONSTRAINTS ALL DEFERRED` around fixture loading via
`disable_constraint_checking`, so `DEFERRABLE INITIALLY IMMEDIATE` would still
tolerate out-of-order fixtures. It does not. Verified against the installed
Django 6.1: `django.db.backends.postgresql.base.DatabaseWrapper` **never overrides**
`disable_constraint_checking`, so `BaseDatabaseWrapper.disable_constraint_checking`
runs, emits no SQL and returns `False`. `loaddata`'s
`with connection.constraint_checks_disabled():` is therefore a no-op on Postgres;
Django gets away with it because *its own* foreign keys are already
`DEFERRABLE INITIALLY DEFERRED`. Only the final `check_constraints()` touches
`SET CONSTRAINTS`, and by then every row is in.

My first draft of this test asserted the reviewer's expectation and failed:

```
E django.db.utils.IntegrityError: Problem installing fixture
  '/app/tests/fixtures/orgs_out_of_order.json': Could not load orgs.StoreAccess(pk=1):
  insert or update on table "orgs_storeaccess" violates foreign key constraint
  "orgs_storeaccess_membership_same_org_fk"
E DETAIL:  Key (membership_id, org_id)=(1, 1) is not present in table "orgs_membership".
```

Consequence, and why I did **not** change the schema: the only thing that breaks is
a *hand-written or hand-reordered* fixture that lists children before parents.
`dumpdata` emits models in registration order, and `orgs/models.py` defines
Organization -> Store -> Role -> Membership -> StoreAccess, so the real
backup/restore path works. Relaxing the keys to `INITIALLY DEFERRED` would buy
child-first fixtures at the cost of statement-time refusal, which is the entire
point of the design - and schema changes are the database-engineer's call, not
mine. **Flagged for the database-engineer re-review**: verdict (1) in the ledger
should be re-stated as "correct, and loaddata is not broken *for dependency-ordered
fixtures, which is what dumpdata produces*".

`tests/test_fixture_loading.py` (new, 5 tests) + three fixtures in
`tests/fixtures/`:

- `test_django_does_not_defer_constraints_for_loaddata` pins the premise
  (`connection.disable_constraint_checking() is False`), so a future Django that
  changes this tells us here instead of in production;
- `test_a_dependency_ordered_fixture_loads` - the ordinary case;
- `test_a_child_first_fixture_is_refused_by_the_composite_key` - the same six rows
  reversed; documents the real cost of `INITIALLY IMMEDIATE` in a test rather than
  in someone's memory, and asserts nothing is left half-loaded;
- `test_a_fixture_that_mixes_two_organizations_is_refused` - membership 2 in org 1
  holding org 2's role, in a dependency-correct file so the cross-org row is the
  only fault. Fixtures load through `save_base(raw=True)`: no model validation runs
  at all, so the composite key is the only thing that can refuse it;
- `test_a_dumpdata_round_trip_restores_every_row` - create, `dumpdata`, raw
  `DELETE` of all six tables, `loaddata`, assert every row is back. This is the
  path that actually matters, and it is the regression guard for slice 2: a ledger
  model declared above its parent would pass every other test in the suite and
  break restore.

Mutations run (all caught):

| mutation | result |
|---|---|
| the three orgs composite FKs changed to `DEFERRABLE INITIALLY DEFERRED` (`--create-db`) | `test_a_child_first_fixture_is_refused_by_the_composite_key` fails - `DID NOT RAISE IntegrityError`. The cross-org test still passes, which is the informative half: deferral changes *when* the guard fires, not whether |
| `orgs_membership_role_same_org_fk` block deleted from `orgs/0001` (`--create-db`) | `test_a_fixture_that_mixes_two_organizations_is_refused` fails - `DID NOT RAISE IntegrityError` |
| dumped JSON reversed before reload (mutating the test, to prove it depends on dump order) | `test_a_dumpdata_round_trip_restores_every_row` fails with `violates foreign key constraint "orgs_storeaccess_membership_same_org_fk"` |

## Files changed this round

- `common/db.py` - versioned + frozen constants, mechanism documented
- `apps/audit/migrations/0002_append_only_trigger.py` - imports `_V1` names (SQL byte-identical)
- `apps/orgs/migrations/0001_initial.py` - reversibility docstring
- `tests/test_common_checks.py` - B5
- `tests/test_db_stability.py` (new) - B1
- `tests/test_fixture_loading.py` (new) - B6
- `tests/fixtures/orgs_dependency_ordered.json`, `tests/fixtures/orgs_child_first.json`,
  `tests/fixtures/orgs_cross_org_membership.json` (new)

## Verbatim evidence

### `docker compose run --rm web pytest -v`

```
============================= test session starts ==============================
platform linux -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0 -- /usr/local/bin/python3.13
cachedir: .pytest_cache
django: version: 6.1, settings: config.settings.test (from option)
rootdir: /app
configfile: pytest.ini
plugins: django-4.14.0
collecting ... collected 268 items

tests/test_audit.py::test_record_writes_a_row PASSED                     [  0%]
tests/test_audit.py::test_record_without_a_target_or_actor_is_allowed PASSED [  0%]
tests/test_audit.py::test_record_derives_the_org_from_the_store PASSED   [  1%]
tests/test_audit.py::test_record_rejects_a_store_from_another_org PASSED [  1%]
tests/test_audit.py::test_record_rejects_a_malformed_action[] PASSED     [  1%]
tests/test_audit.py::test_record_rejects_a_malformed_action[   ] PASSED  [  2%]
tests/test_audit.py::test_record_rejects_a_malformed_action[User.Created] PASSED [  2%]
tests/test_audit.py::test_record_rejects_a_malformed_action[user created] PASSED [  2%]
tests/test_audit.py::test_record_rejects_a_malformed_action[user..created] PASSED [  3%]
tests/test_audit.py::test_record_rejects_a_malformed_action[user] PASSED [  3%]
tests/test_audit.py::test_record_rejects_a_malformed_action[xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx] PASSED [  4%]
tests/test_audit.py::test_record_rejects_a_malformed_action[None] PASSED [  4%]
tests/test_audit.py::test_record_rejects_an_unsaved_target PASSED        [  4%]
tests/test_audit.py::test_record_redacts_sensitive_values PASSED         [  5%]
tests/test_audit.py::test_record_rejects_changes_it_cannot_serialise PASSED [  5%]
tests/test_audit.py::test_record_requires_a_mapping_for_changes PASSED   [  5%]
tests/test_audit.py::test_record_serialises_decimals_and_dates PASSED    [  6%]
tests/test_audit.py::test_record_stores_the_ip PASSED                    [  6%]
tests/test_audit.py::test_record_rejects_a_bogus_ip PASSED               [  7%]
tests/test_audit.py::test_audit_rows_cannot_be_updated PASSED            [  7%]
tests/test_audit.py::test_audit_rows_cannot_be_bulk_updated PASSED       [  7%]
tests/test_audit.py::test_audit_rows_cannot_be_deleted PASSED            [  8%]
tests/test_audit.py::test_the_action_field_carries_the_validator PASSED  [  8%]
tests/test_audit.py::test_audit_log_has_no_soft_delete_columns PASSED    [  8%]
tests/test_audit.py::test_the_actor_reference_is_protected PASSED        [  9%]
tests/test_audit.py::test_newest_rows_come_first PASSED                  [  9%]
tests/test_audit.py::test_an_audit_row_cannot_be_overwritten_through_an_explicit_pk PASSED [ 10%]
tests/test_audit.py::test_an_audit_row_cannot_be_overwritten_with_force_insert PASSED [ 10%]
tests/test_audit.py::test_the_base_manager_cannot_delete_audit_rows PASSED [ 10%]
tests/test_audit.py::test_bulk_create_cannot_be_turned_into_an_upsert PASSED [ 11%]
tests/test_audit.py::test_the_append_only_triggers_exist_in_postgres PASSED [ 11%]
tests/test_audit.py::test_the_database_refuses_a_raw_update PASSED       [ 11%]
tests/test_audit.py::test_the_database_refuses_a_raw_delete PASSED       [ 12%]
tests/test_audit.py::test_the_database_refuses_a_truncate PASSED         [ 12%]
tests/test_audit.py::test_audit_rows_cannot_mix_an_org_with_a_foreign_store PASSED [ 13%]
tests/test_audit.py::test_an_org_only_or_store_only_audit_row_is_still_legal PASSED [ 13%]
tests/test_audit.py::test_a_long_string_is_truncated_distinguishably PASSED [ 13%]
tests/test_audit.py::test_an_oversized_payload_is_replaced_by_a_marker PASSED [ 14%]
tests/test_common_bases.py::test_soft_delete_hides_from_default_manager PASSED [ 14%]
tests/test_common_bases.py::test_soft_delete_stamps_who_and_when PASSED  [ 14%]
tests/test_common_bases.py::test_soft_delete_is_idempotent PASSED        [ 15%]
tests/test_common_bases.py::test_soft_delete_requires_an_actor_keyword PASSED [ 15%]
tests/test_common_bases.py::test_hard_delete_is_forbidden_on_instances PASSED [ 16%]
tests/test_common_bases.py::test_hard_delete_is_forbidden_on_querysets PASSED [ 16%]
tests/test_common_bases.py::test_queryset_soft_delete_stamps_every_row PASSED [ 16%]
tests/test_common_bases.py::test_audited_model_stamps_timestamps PASSED  [ 17%]
tests/test_common_bases.py::test_audited_actor_fields_are_optional_but_protected PASSED [ 17%]
tests/test_common_bases.py::test_no_store_scoped_model_carries_its_own_org_pointer PASSED [ 17%]
tests/test_common_bases.py::test_store_fk_is_declared_by_the_base_not_by_consumers PASSED [ 18%]
tests/test_common_bases.py::test_unscoped_query_raises[list] PASSED      [ 18%]
tests/test_common_bases.py::test_unscoped_query_raises[len] PASSED       [ 19%]
tests/test_common_bases.py::test_unscoped_query_raises[bool] PASSED      [ 19%]
tests/test_common_bases.py::test_unscoped_query_raises[count] PASSED     [ 19%]
tests/test_common_bases.py::test_unscoped_query_raises[exists] PASSED    [ 20%]
tests/test_common_bases.py::test_unscoped_query_raises[first] PASSED     [ 20%]
tests/test_common_bases.py::test_unscoped_query_raises[last] PASSED      [ 20%]
tests/test_common_bases.py::test_unscoped_query_raises[get] PASSED       [ 21%]
tests/test_common_bases.py::test_unscoped_query_raises[aggregate] PASSED [ 21%]
tests/test_common_bases.py::test_unscoped_query_raises[iterator] PASSED  [ 22%]
tests/test_common_bases.py::test_unscoped_query_raises[values_list] PASSED [ 22%]
tests/test_common_bases.py::test_unscoped_query_raises[chained_filter] PASSED [ 22%]
tests/test_common_bases.py::test_unscoped_query_raises[in_bulk] PASSED   [ 23%]
tests/test_common_bases.py::test_unscoped_query_raises[explain] PASSED   [ 23%]
tests/test_common_bases.py::test_unscoped_query_raises[update] PASSED    [ 23%]
tests/test_common_bases.py::test_unscoped_query_raises_on_the_manager_itself PASSED [ 24%]
tests/test_common_bases.py::test_unscoped_subquery_raises PASSED         [ 24%]
tests/test_common_bases.py::test_raw_sql_is_refused_on_the_scoped_manager PASSED [ 25%]
tests/test_common_bases.py::test_own_meta_child_is_still_guarded PASSED  [ 25%]
tests/test_common_bases.py::test_for_store_returns_only_that_store PASSED [ 25%]
tests/test_common_bases.py::test_for_store_hides_soft_deleted_rows PASSED [ 26%]
tests/test_common_bases.py::test_for_store_survives_further_chaining PASSED [ 26%]
tests/test_common_bases.py::test_for_store_accepts_a_primary_key PASSED  [ 26%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[None] PASSED [ 27%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[] PASSED [ 27%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[0] PASSED [ 27%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[bad3] PASSED [ 28%]
tests/test_common_bases.py::test_for_store_rejects_a_missing_or_bogus_store[not-a-store] PASSED [ 28%]
tests/test_common_bases.py::test_for_store_rejects_a_saved_instance_of_another_model PASSED [ 29%]
tests/test_common_bases.py::test_for_stores_rejects_a_saved_instance_of_another_model PASSED [ 29%]
tests/test_common_bases.py::test_for_store_rejects_an_unsaved_store PASSED [ 29%]
tests/test_common_bases.py::test_for_stores_covers_several_stores_and_nothing_else PASSED [ 30%]
tests/test_common_bases.py::test_for_stores_rejects_an_empty_collection PASSED [ 30%]
tests/test_common_bases.py::test_scoped_update_is_allowed_and_stays_scoped PASSED [ 30%]
tests/test_common_bases.py::test_scoped_queryset_still_refuses_hard_delete PASSED [ 31%]
tests/test_common_bases.py::test_creating_a_row_needs_a_store_named_or_pinned PASSED [ 31%]
tests/test_common_bases.py::test_creating_a_row_with_no_store_at_all_is_refused PASSED [ 32%]
tests/test_common_bases.py::test_for_store_fills_in_the_store_on_create PASSED [ 32%]
tests/test_common_bases.py::test_for_store_refuses_to_create_in_another_store PASSED [ 32%]
tests/test_common_bases.py::test_for_store_refuses_to_bulk_create_in_another_store PASSED [ 33%]
tests/test_common_bases.py::test_bulk_create_fills_in_the_pinned_store PASSED [ 33%]
tests/test_common_bases.py::test_update_cannot_move_a_row_to_another_store PASSED [ 33%]
tests/test_common_bases.py::test_update_cannot_repoint_a_foreign_key_into_another_store PASSED [ 34%]
tests/test_common_bases.py::test_update_refuses_to_reparent_a_row_even_within_scope PASSED [ 34%]
tests/test_common_bases.py::test_save_with_update_fields_store_revalidates_every_foreign_key PASSED [ 35%]
tests/test_common_bases.py::test_get_or_create_cannot_reach_into_another_store PASSED [ 35%]
tests/test_common_bases.py::test_get_or_create_uses_the_pinned_store PASSED [ 35%]
tests/test_common_bases.py::test_all_objects_is_the_documented_escape_hatch PASSED [ 36%]
tests/test_common_bases.py::test_django_internals_use_an_unguarded_default_manager PASSED [ 36%]
tests/test_common_bases.py::test_store_scoped_models_have_no_reverse_accessor_from_store PASSED [ 36%]
tests/test_common_bases.py::test_validation_error_is_not_how_scope_violations_surface PASSED [ 37%]
tests/test_common_bases.py::test_an_org_level_parent_has_no_accessor_to_its_store_scoped_children PASSED [ 37%]
tests/test_common_bases.py::test_a_store_scoped_parent_has_no_accessor_to_its_children PASSED [ 38%]
tests/test_common_bases.py::test_children_are_read_through_for_store PASSED [ 38%]
tests/test_common_bases.py::test_a_cross_store_foreign_key_is_refused_on_create PASSED [ 38%]
tests/test_common_bases.py::test_a_cross_store_foreign_key_is_refused_on_a_plain_save PASSED [ 39%]
tests/test_common_bases.py::test_a_cross_store_foreign_key_is_refused_in_bulk_create PASSED [ 39%]
tests/test_common_bases.py::test_a_cross_store_foreign_key_is_refused_when_only_the_id_is_given PASSED [ 39%]
tests/test_common_bases.py::test_same_store_foreign_keys_are_fine PASSED [ 40%]
tests/test_common_bases.py::test_a_partial_save_skips_the_unrelated_relation_check PASSED [ 40%]
tests/test_common_bases.py::test_a_scoped_query_cannot_join_back_out_to_other_tenants PASSED [ 41%]
tests/test_common_bases.py::test_the_join_existence_oracle_is_gone PASSED [ 41%]
tests/test_common_bases.py::test_the_aggregate_shape_of_the_same_leak_is_gone PASSED [ 41%]
tests/test_common_bases.py::test_a_scoped_query_reads_its_own_store_only PASSED [ 42%]
tests/test_common_bases.py::test_all_objects_cannot_hard_delete PASSED   [ 42%]
tests/test_common_bases.py::test_the_base_manager_cannot_hard_delete_either PASSED [ 42%]
tests/test_common_bases.py::test_all_objects_still_sees_retired_rows PASSED [ 43%]
tests/test_common_bases.py::test_or_with_an_unscoped_queryset_is_refused PASSED [ 43%]
tests/test_common_bases.py::test_and_with_an_unscoped_queryset_is_refused PASSED [ 44%]
tests/test_common_bases.py::test_or_of_two_scoped_querysets_stays_scoped PASSED [ 44%]
tests/test_common_bases.py::test_and_of_two_scoped_querysets_stays_scoped PASSED [ 44%]
tests/test_common_bases.py::test_for_stores_refuses_a_mixed_organization_set PASSED [ 45%]
tests/test_common_bases.py::test_for_stores_refuses_unknown_store_ids PASSED [ 45%]
tests/test_common_bases.py::test_for_stores_accepts_several_stores_of_one_org PASSED [ 45%]
tests/test_common_bases.py::test_soft_delete_refuses_a_missing_actor PASSED [ 46%]
tests/test_common_bases.py::test_soft_delete_accepts_a_declared_system_action PASSED [ 46%]
tests/test_common_bases.py::test_queryset_soft_delete_refuses_a_missing_actor PASSED [ 47%]
tests/test_common_bases.py::test_the_hidden_relation_cannot_be_traversed_from_the_parent PASSED [ 47%]
tests/test_common_bases.py::test_the_hidden_relation_cannot_be_traversed_from_a_scoped_query PASSED [ 47%]
tests/test_common_bases.py::test_the_hidden_relation_is_refused_on_all_objects_too PASSED [ 48%]
tests/test_fixture_loading.py::test_django_does_not_defer_constraints_for_loaddata PASSED [ 48%]
tests/test_fixture_loading.py::test_a_dependency_ordered_fixture_loads PASSED [ 48%]
tests/test_fixture_loading.py::test_a_child_first_fixture_is_refused_by_the_composite_key PASSED [ 49%]
tests/test_fixture_loading.py::test_a_fixture_that_mixes_two_organizations_is_refused PASSED [ 49%]
tests/test_fixture_loading.py::test_a_dumpdata_round_trip_restores_every_row PASSED [ 50%]
tests/test_healthz.py::test_healthz_returns_ok PASSED                    [ 50%]
tests/test_orgs_models.py::test_organization_defaults_are_rwanda_first PASSED [ 50%]
tests/test_orgs_models.py::test_organization_slug_is_unique PASSED       [ 51%]
tests/test_orgs_models.py::test_organization_slug_uniqueness_is_enforced_by_the_database PASSED [ 51%]
tests/test_orgs_models.py::test_a_soft_deleted_organization_releases_its_slug PASSED [ 51%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[rwf] PASSED [ 52%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[RW] PASSED [ 52%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[RWFX] PASSED [ 52%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[R1F] PASSED [ 53%]
tests/test_orgs_models.py::test_organization_rejects_a_bogus_currency_code[] PASSED [ 53%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[Africa/Kigaly] PASSED [ 54%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[CAT] PASSED [ 54%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[] PASSED [ 54%]
tests/test_orgs_models.py::test_organization_rejects_an_unknown_timezone[UTC+2] PASSED [ 55%]
tests/test_orgs_models.py::test_organization_accepts_another_real_timezone PASSED [ 55%]
tests/test_orgs_models.py::test_store_name_is_unique_within_an_org PASSED [ 55%]
tests/test_orgs_models.py::test_store_name_uniqueness_is_enforced_by_the_database PASSED [ 56%]
tests/test_orgs_models.py::test_the_same_store_name_is_fine_in_another_org PASSED [ 56%]
tests/test_orgs_models.py::test_a_soft_deleted_store_name_can_be_reused PASSED [ 57%]
tests/test_orgs_models.py::test_store_carries_the_only_org_pointer PASSED [ 57%]
tests/test_orgs_models.py::test_permission_catalog_is_exactly_the_agreed_set PASSED [ 57%]
tests/test_orgs_models.py::test_presets_are_owner_manager_seller PASSED  [ 58%]
tests/test_orgs_models.py::test_manager_preset_runs_a_store_but_does_not_own_the_org PASSED [ 58%]
tests/test_orgs_models.py::test_role_rejects_an_unknown_permission_code PASSED [ 58%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[sale.record] PASSED [ 59%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[bad1] PASSED [ 59%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[bad2] PASSED [ 60%]
tests/test_orgs_models.py::test_role_permissions_must_be_a_list_of_unique_codes[bad3] PASSED [ 60%]
tests/test_orgs_models.py::test_role_accepts_catalog_codes_and_answers_has PASSED [ 60%]
tests/test_orgs_models.py::test_role_name_is_unique_within_an_org PASSED [ 61%]
tests/test_orgs_models.py::test_role_defaults_to_no_permissions_and_not_preset PASSED [ 61%]
tests/test_orgs_models.py::test_membership_is_unique_per_user_and_org PASSED [ 61%]
tests/test_orgs_models.py::test_membership_uniqueness_is_enforced_by_the_database PASSED [ 62%]
tests/test_orgs_models.py::test_membership_rejects_a_role_from_another_org PASSED [ 62%]
tests/test_orgs_models.py::test_store_access_rejects_a_store_from_another_org PASSED [ 63%]
tests/test_orgs_models.py::test_store_access_is_unique_per_membership_and_store PASSED [ 63%]
tests/test_orgs_models.py::test_store_access_accepts_a_store_in_the_same_org PASSED [ 63%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Organization.orgs_organization_unique_live_slug] PASSED [ 64%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Store.orgs_store_unique_live_name_per_org] PASSED [ 64%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Role.orgs_role_unique_live_name_per_org] PASSED [ 64%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[Membership.orgs_membership_unique_live_user_per_org] PASSED [ 65%]
tests/test_orgs_models.py::test_unique_constraints_only_cover_live_rows[StoreAccess.orgs_storeaccess_unique_live_membership_store] PASSED [ 65%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Organization] PASSED [ 66%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Store] PASSED [ 66%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Role] PASSED [ 66%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[Membership] PASSED [ 67%]
tests/test_orgs_models.py::test_orgs_models_are_soft_deletable_and_audited[StoreAccess] PASSED [ 67%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Organization] PASSED [ 67%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Store] PASSED [ 68%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Role] PASSED [ 68%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[Membership] PASSED [ 69%]
tests/test_orgs_models.py::test_orgs_models_refuse_hard_delete[StoreAccess] PASSED [ 69%]
tests/test_orgs_models.py::test_orgs_models_are_not_store_scoped PASSED  [ 69%]
tests/test_orgs_models.py::test_store_branding_defaults_to_inheriting_the_org PASSED [ 70%]
tests/test_orgs_models.py::test_store_brand_must_be_a_mapping PASSED     [ 70%]
tests/test_orgs_models.py::test_membership_cannot_take_a_role_from_another_org PASSED [ 70%]
tests/test_orgs_models.py::test_store_access_cannot_reach_a_store_in_another_org PASSED [ 71%]
tests/test_orgs_models.py::test_store_access_derives_its_org_from_the_membership PASSED [ 71%]
tests/test_orgs_models.py::test_store_access_full_clean_does_not_demand_the_derived_org PASSED [ 72%]
tests/test_orgs_models.py::test_the_same_org_composite_keys_exist_in_postgres PASSED [ 72%]
tests/test_orgs_models.py::test_a_real_png_is_accepted PASSED            [ 72%]
tests/test_orgs_models.py::test_an_svg_disguised_as_a_png_is_rejected PASSED [ 73%]
tests/test_orgs_models.py::test_an_svg_extension_is_rejected PASSED      [ 73%]
tests/test_orgs_models.py::test_an_oversized_image_is_rejected PASSED    [ 73%]
tests/test_orgs_models.py::test_the_stored_logo_filename_is_random PASSED [ 74%]
tests/test_orgs_models.py::test_media_root_is_outside_the_source_tree PASSED [ 74%]
tests/test_user_model.py::test_the_project_user_model_is_ours PASSED     [ 75%]
tests/test_user_model.py::test_username_field_and_required_fields PASSED [ 75%]
tests/test_user_model.py::test_phone_rejects_bad_formats[+250788123456] PASSED [ 75%]
tests/test_user_model.py::test_phone_rejects_bad_formats[0788123456] PASSED [ 76%]
tests/test_user_model.py::test_phone_rejects_bad_formats[250 788 123 456] PASSED [ 76%]
tests/test_user_model.py::test_phone_rejects_bad_formats[250788] PASSED  [ 76%]
tests/test_user_model.py::test_phone_rejects_bad_formats[2507881234567890] PASSED [ 77%]
tests/test_user_model.py::test_phone_rejects_bad_formats[25078812345a] PASSED [ 77%]
tests/test_user_model.py::test_phone_rejects_bad_formats[] PASSED        [ 77%]
tests/test_user_model.py::test_phone_accepts_country_code_digits[250788123456] PASSED [ 78%]
tests/test_user_model.py::test_phone_accepts_country_code_digits[12345678] PASSED [ 78%]
tests/test_user_model.py::test_phone_accepts_country_code_digits[999999999999999] PASSED [ 79%]
tests/test_user_model.py::test_email_is_required PASSED                  [ 79%]
tests/test_user_model.py::test_email_must_be_unique_case_insensitively PASSED [ 79%]
tests/test_user_model.py::test_email_uniqueness_is_enforced_by_the_database_too PASSED [ 80%]
tests/test_user_model.py::test_username_must_be_unique_case_insensitively PASSED [ 80%]
tests/test_user_model.py::test_phone_must_be_unique PASSED               [ 80%]
tests/test_user_model.py::test_language_defaults_to_english PASSED       [ 81%]
tests/test_user_model.py::test_language_choices_match_the_configured_languages PASSED [ 81%]
tests/test_user_model.py::test_language_rejects_an_unconfigured_code PASSED [ 82%]
tests/test_user_model.py::test_password_is_argon2 PASSED                 [ 82%]
tests/test_user_model.py::test_create_user_validates_its_input PASSED    [ 82%]
tests/test_user_model.py::test_create_user_requires_identity_fields[username] PASSED [ 83%]
tests/test_user_model.py::test_create_user_requires_identity_fields[email] PASSED [ 83%]
tests/test_user_model.py::test_create_user_requires_identity_fields[phone] PASSED [ 83%]
tests/test_user_model.py::test_create_user_normalises_the_email_domain PASSED [ 84%]
tests/test_user_model.py::test_create_user_without_a_password_cannot_log_in PASSED [ 84%]
tests/test_user_model.py::test_create_superuser_is_staff_and_superuser PASSED [ 85%]
tests/test_user_model.py::test_create_superuser_refuses_to_be_downgraded PASSED [ 85%]
tests/test_user_model.py::test_new_users_are_active_and_not_staff PASSED [ 85%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[victim@example.com] PASSED [ 86%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[250788111111] PASSED [ 86%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[250-788-111-111] PASSED [ 86%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[eva mugisha] PASSED [ 87%]
tests/test_user_model.py::test_username_cannot_impersonate_another_identifier[eva@] PASSED [ 87%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva] PASSED [ 88%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva.mugisha] PASSED [ 88%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva_m1] PASSED [ 88%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva-m] PASSED [ 89%]
tests/test_user_model.py::test_ordinary_usernames_are_accepted[eva+shop] PASSED [ 89%]
tests/test_user_model.py::test_create_user_rejects_an_ambiguous_username PASSED [ 89%]
tests/test_user_model.py::test_a_user_cannot_be_hard_deleted PASSED      [ 90%]
tests/test_user_model.py::test_users_cannot_be_hard_deleted_in_bulk PASSED [ 90%]
tests/test_user_model.py::test_the_user_base_manager_cannot_hard_delete_either PASSED [ 91%]
tests/test_user_model.py::test_deactivation_is_the_supported_path PASSED [ 91%]
tests/test_common_checks.py::test_the_real_models_pass PASSED            [ 91%]
tests/test_common_checks.py::test_non_scoped_models_are_ignored PASSED   [ 92%]
tests/test_common_checks.py::test_the_check_honours_the_app_configs_it_is_given PASSED [ 92%]
tests/test_common_checks.py::test_the_check_stays_silent_about_apps_it_was_not_given PASSED [ 92%]
tests/test_common_checks.py::test_a_model_that_overrides_objects_is_rejected PASSED [ 93%]
tests/test_common_checks.py::test_an_extra_manager_that_steals_the_default_is_rejected PASSED [ 93%]
tests/test_common_checks.py::test_a_model_that_makes_the_guarded_manager_the_default_is_rejected PASSED [ 94%]
tests/test_common_checks.py::test_the_real_models_keep_the_unguarded_default_manager PASSED [ 94%]
tests/test_common_checks.py::test_a_model_that_repoints_the_store_fk_is_rejected PASSED [ 94%]
tests/test_common_checks.py::test_a_model_that_makes_the_store_optional_is_rejected PASSED [ 95%]
tests/test_common_checks.py::test_a_forward_fk_that_creates_an_accessor_on_the_parent_is_rejected PASSED [ 95%]
tests/test_common_checks.py::test_a_reverse_accessor_into_a_store_scoped_model_is_rejected PASSED [ 95%]
tests/test_common_checks.py::test_a_global_unique_field_is_rejected PASSED [ 96%]
tests/test_common_checks.py::test_a_unique_constraint_without_store_is_rejected PASSED [ 96%]
tests/test_common_checks.py::test_a_unique_constraint_that_ignores_soft_delete_is_rejected PASSED [ 97%]
tests/test_common_checks.py::test_unique_together_is_rejected PASSED     [ 97%]
tests/test_common_checks.py::test_a_per_store_live_unique_constraint_passes PASSED [ 97%]
tests/test_common_checks.py::test_an_org_level_model_pointing_at_a_store_scoped_model_is_rejected PASSED [ 98%]
tests/test_db_stability.py::test_the_frozen_function_sql_has_not_changed PASSED [ 98%]
tests/test_db_stability.py::test_the_frozen_trigger_sql_has_not_changed PASSED [ 98%]
tests/test_db_stability.py::test_the_shipped_migration_still_carries_exactly_the_pinned_sql PASSED [ 99%]
tests/test_db_stability.py::test_every_sql_helper_in_common_db_is_versioned PASSED [ 99%]
tests/test_db_stability.py::test_no_migration_imports_an_unpinned_name_from_common_db PASSED [100%]

======================= 268 passed in 433.34s (0:07:13) ========================
```

### `docker compose run --rm web ruff check .`

```
All checks passed!
```

### `docker compose run --rm web python manage.py check`

```
System check identified no issues (0 silenced).
```

### `docker compose run --rm web python manage.py makemigrations --check --dry-run`

```
No changes detected
```

### `docker compose run --rm web python manage.py makemigrations --check --dry-run --settings=config.settings.test`

```
No changes detected
```

---

# FIX ROUND 3

Scope: sections **A**, **B** and **D** of `task-123-fix-round-3.md`. Section C
(entrypoint, Dockerfile, `.gitignore`, `.dockerignore`) belongs to a devops
round running concurrently and was not touched; section E is carried forward.
**D8** lives in `docker/Dockerfile` and is therefore also devops', not this
round's — it is the one item in section D that did not land here.

Baseline at start: 268 passed. End state: **330 passed**, ruff clean,
`manage.py check` silent, no drift under either settings module.

## A1 — the whole combinator surface, guarded at the seam

The two seams, not a list of method names:

* `sql.Query.combine` — `|`, `&` and `^` all reach it. `GuardedQuery.combine`
  refuses a pinned/unpinned merge from *either* side (the unscoped base class
  carries the scope flags now, so it can see both). `ScopedQuery.combine` then
  re-resolves the merged pin set through `_store_pks()` — the same resolver that
  already refuses `for_stores([A, RIVAL])`, which is what `|` was a synonym for.
* `QuerySet._combinator_query` — `union()`, `intersection()` and `difference()`
  all reach it and never call `combine()`. Overridden on `NoHardDeleteQuerySet`
  (refusal) and on `ScopedQuerySet` (refusal + merged resolution).

Both seams are the *only* callers in Django 6.1 — verified, not assumed:

```
$ grep -rn '\.combine(' django/db/ | grep -v 'def combine'
django/db/models/query.py:500:        combined.query.combine(other.query, sql.AND)
django/db/models/query.py:519:        combined.query.combine(other.query, sql.OR)
django/db/models/query.py:538:        combined.query.combine(other.query, sql.XOR)

$ grep -rn '_combinator_query' django/db/
django/db/models/query.py:1700:    def _combinator_query(self, combinator, *other_qs, all=False):
django/db/models/query.py:1720:            return qs[0]._combinator_query("union", *qs[1:], all=all)
django/db/models/query.py:1723:        return self._combinator_query("union", *other_qs, all=all)
django/db/models/query.py:1732:        return self._combinator_query("intersection", *other_qs)
django/db/models/query.py:1738:        return self._combinator_query("difference", *other_qs)
```

`^` also needed queryset-level cover on the unscoped side, and while adding it
a latent bug surfaced: `NoHardDeleteQuerySet.__ror__` / `__rand__` called
`super().__ror__()` / `super().__rand__()`, **which do not exist** — Django's
`QuerySet` defines only `__or__`, `__and__` and `__xor__`
(`'__ror__' in QuerySet.__dict__` is `False`; the `hasattr` that presumably
justified them finds `type.__ror__` on the metaclass, i.e. PEP-604 union
syntax). Every reflected operator would have raised `AttributeError` the first
time one ran. They now delegate to the forward operator, which is the same set
operation.

### Before — the four leaking expressions from the brief, executed

```
Product.objects.for_store(A) | Product.objects.for_store(B)        -> ['RIVAL', 'mine']
Product.objects.for_store(A).union(Product.objects.for_store(B))   -> ['RIVAL', 'mine']
Product.objects.for_store(A) ^ Product.all_objects.all()           -> ['RIVAL', 'mine2']
Product.all_objects.all() ^ Product.objects.for_store(A)           -> ['RIVAL', 'mine2']
```

### After — the same four expressions, same fixtures, same probe

```
Product.objects.for_store(A) | Product.objects.for_store(B)        -> CrossStoreReferenceError
Product.objects.for_store(A).union(Product.objects.for_store(B))   -> CrossStoreReferenceError
Product.objects.for_store(A) ^ Product.all_objects.all()           -> UnscopedQueryError
Product.all_objects.all() ^ Product.objects.for_store(A)           -> UnscopedQueryError
```

### The matrix

`["__or__","__and__","__xor__","__ror__","__rand__","__rxor__","union",
"intersection","difference"]` × {unscoped right, unscoped left, cross-org,
cross-org reversed, same-org} — 45 cases, each materialised (`sorted(row.name
for row in ...)`), because a build-time refusal and a fetch-time leak look
identical until something iterates. Plus a "stays pinned to both stores" leg and
a structural test that both seams are still overridden and still exist upstream.

Before the fix, **30 of the new A1/A2 cases failed**:

```
FAILED tests/test_common_bases.py::test_a_multi_store_pin_refuses_to_update_a_store_scoped_foreign_key
FAILED tests/test_common_bases.py::test_a_multi_store_pin_refuses_even_an_in_scope_foreign_key
FAILED tests/test_common_bases.py::test_a_combinator_refuses_an_unscoped_right_hand_side[__xor__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_an_unscoped_right_hand_side[__rxor__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_an_unscoped_left_hand_side[__xor__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_an_unscoped_left_hand_side[__rxor__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[__or__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[__and__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[__xor__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[__ror__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[__rand__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[__rxor__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[union]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[intersection]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[difference]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[__or__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[__and__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[__xor__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[__ror__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[__rand__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[__rxor__]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[union]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[intersection]
FAILED tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[difference]
FAILED tests/test_common_bases.py::test_a_combinator_allows_two_stores_of_one_organization[__ror__]
FAILED tests/test_common_bases.py::test_a_combinator_allows_two_stores_of_one_organization[__rand__]
FAILED tests/test_common_bases.py::test_a_combinator_allows_two_stores_of_one_organization[__rxor__]
FAILED tests/test_common_bases.py::test_a_merged_queryset_stays_pinned_to_both_stores[__xor__]
FAILED tests/test_common_bases.py::test_a_merged_queryset_stays_pinned_to_both_stores[union]
FAILED tests/test_common_bases.py::test_the_combinator_guard_sits_on_the_query_seam_not_on_a_list_of_names
30 failed, 22 passed, 91 deselected
```

After: `52 passed, 91 deselected`.

Cost, stated plainly: every `|`, `&`, `^`, `union()`, `intersection()` and
`difference()` between two scoped querysets now issues one extra `SELECT id,
org_id FROM orgs_store WHERE id IN (...)`. That is the price of resolving
ownership instead of assuming it, and it is the same query `for_stores()`
already pays.

## A2 — a multi-store pin refuses store-scoped FK updates outright

`_check_update_fk_stores` tested membership in the *pinned set*; the invariant
`save()` enforces is equality with the *row's own* store. A multi-store pin
cannot know each row's store, so it now refuses rather than approximates. The
single-store path is unchanged (there, membership and equality are the same
test) and is covered by its own passing test so the fix is not just a ban.

### Before

```
SaleLine.objects.for_stores([A, A2]).update(product_id=<A2 product>) -> 1 row updated
  SaleLine(store=A).product now lives in store: 2 (row's own store is 1)
```

### After

```
SaleLine.objects.for_stores([A, A2]).update(product_id=<A2 product>) -> CrossStoreReferenceError
```

## B1 — `common.E100` runs again

`@register(Tags.database)` → `@register(Tags.security)`. The check does pure
`settings.DATABASES` string inspection and opens no connection, so the database
tag was simply wrong, and pointing the entrypoint at `check --database default`
would have made a connection-free guard depend on a reachable database at boot.
The two false statements in scope were corrected: `config/settings/prod.py:16-23`
(which now states the tag choice and its reason, and no longer asserts what
`docker/entrypoint.sh` does — that file is devops' this round) and the
`common/checks.py` docstring. `docker/entrypoint.sh` and `docs/DEVELOPMENT.md`
were routed elsewhere.

### Before — prod settings, `POSTGRES_DB=test_raporo`

```
$ python manage.py check
System check identified no issues (0 silenced).

$ python manage.py check --database default
SystemCheckError: System check identified some issues:

ERRORS:
?: (common.E100) Database 'default' is named 'test_raporo', which starts with 'test_'. The append-only TRUNCATE guard waives itself for such names, so this must never be a production database.
	HINT: Rename the database, or unset ENFORCE_NON_TEST_DATABASE.

System check identified 1 issue (0 silenced).
```

### After — same command, no `--database` argument

```
$ python manage.py check
SystemCheckError: System check identified some issues:

ERRORS:
?: (common.E100) Database 'default' is named 'test_raporo', which starts with 'test_'. The append-only TRUNCATE guard waives itself for such names, so this must never be a production database.
	HINT: Rename the database, or unset ENFORCE_NON_TEST_DATABASE.

System check identified 1 issue (0 silenced).
```

And it still passes a legitimately named database (`POSTGRES_DB=raporo`):

```
$ python manage.py check
System check identified no issues (0 silenced).
```

## B2 — six tests, all through the registry

Every one drives `django.core.checks.run_checks()` or `call_command("check")`;
none calls `check_database_is_not_test_named` directly. The suite's own database
is named `test_raporo`, so flipping `ENFORCE_NON_TEST_DATABASE` on inside a test
*is* the production misconfiguration, reproduced. Covered: fires through the
registry; fails `manage.py check` end to end with `SystemCheckError`; is not
tagged `database` and gives the same answer with and without a `databases`
argument; silent with the flag off; silent on a normally-named database; names
the alias and keeps its hint.

### Before — the same six tests against `@register(Tags.database)`

```
FAILED tests/test_common_checks.py::test_e100_fires_through_the_registry_on_a_test_named_database
FAILED tests/test_common_checks.py::test_manage_py_check_refuses_to_pass_on_a_test_named_database
FAILED tests/test_common_checks.py::test_e100_is_not_tagged_database_because_that_tag_is_skipped_by_default
FAILED tests/test_common_checks.py::test_e100_names_the_offending_alias_and_stays_actionable
4 failed, 2 passed, 18 deselected, 1 warning
```

(The two that passed on the broken code are the two that assert *absence* — the
tautology class the constraint on this fix existed to avoid.)

### After

```
6 passed
```

## D1 — one test, four escape hatches

`test_every_run_sql_statement_in_every_migration_is_pinned` imports every
`apps/*/migrations/*.py` module and hashes every statement of every `RunSQL`
operation's `sql` and `reverse_sql` (handling the string / list-of-strings /
list-of-`(sql, params)` forms, and `None` / `RunSQL.noop`). It does not care how
the text got there: inline, in a dict, in a class attribute, via
`import common.db as cdb`, or produced by a pinned helper called with a new
table name. Premise assertions keep it from going vacuous if the glob stops
matching.

That surfaced something the brief did not expect: `orgs/0001_initial` and
`audit/0001_initial` **already inline** eight statements (the four
`*_same_org_fk` keys, forward and reverse) that nothing pinned. They are now in
`PINNED_MIGRATION_SQL`, kept separate from `PINNED_SQL` so the import-name scan
is not polluted by path-shaped keys.

`_looks_like_sql` is gone, replaced by `_holds_text`: every module-level
non-underscore name in `common/db.py` that is — or contains, through a list,
tuple, set, dict or class attribute — a string must be versioned unless
allowlisted. `REVOKE`, `GRANT`, `INSERT`, `TRUNCATE`, `DO $$` and `WITH ...
UPDATE` no longer walk past it.

Mutation-checked, both directions at once (an unversioned `REVOKE_TRUNCATE`
constant in `common/db.py` and an extra inline `RunSQL` in `audit/0002`):

```
FAILED tests/test_db_stability.py::test_the_shipped_migration_still_carries_exactly_the_pinned_sql
FAILED tests/test_db_stability.py::test_every_run_sql_statement_in_every_migration_is_pinned
FAILED tests/test_db_stability.py::test_every_sql_helper_in_common_db_is_versioned
3 failed, 3 passed
```

Both mutations reverted; `git status` confirms neither file kept them.

## D2 / D3 — the V1→V2 path, corrected

`common/db.py`'s docstring now says, with reasons: a V2 in another app must
declare an explicit dependency on **every** migration installing an earlier
version (`("audit", "0002_append_only_trigger")` included) or Django's graph is
free to apply it first on a fresh install and leave that database on the V1
body; and a V2's `reverse_sql` is `CREATE_APPEND_ONLY_FUNCTION_V1`, never a
DROP, because `DROP FUNCTION raporo_append_only()` is refused by Postgres while
any guarded table's trigger depends on it. A worked "guard a new table" example
follows: depend on `audit/0002`, carry **only** the trigger operation, pin the
new table's forward and reverse text. `audit/0002`'s own docstring says the same
from the other end — it owns the function's lifecycle, and copying both of its
operations is what makes a second guarded table's migration irreversible.

## D4 — `migrate orgs zero` destroys the audit log

Measured, not reasoned:

```
$ python manage.py migrate orgs zero --plan | grep -iE 'audit|Planned'
Planned operations:
audit.0002_append_only_trigger
    Raw SQL operation -> DROP TRIGGER IF EXISTS audit_auditl…
audit.0001_initial
    Undo Create model AuditLog
    Raw SQL operation -> ALTER TABLE audit_auditlog DROP CON…
```

The migration's docstring now names that row-set: the one command that can erase
the append-only forensic record wholesale.

## D5 — the post-`loaddata` deferral window

Three tests and a prominent module docstring warning. The baseline (a cross-org
`UPDATE` refused at statement time), the landmine (the identical write
**accepted** after a `loaddata` in the same transaction, then proved still armed
by a `SET CONSTRAINTS ALL IMMEDIATE` that raises), and the remedy (issue
`SET CONSTRAINTS ALL IMMEDIATE` right after loading and the guard is back). The
landmine test restores the row before it ends — `TestCase._fixture_teardown`
runs the same check, and leaving it would fail the *next* test, which is exactly
the confusion being documented.

## D6 — `tests/test_fixture_loading.py`

* The ordered fixture now carries an `audit.auditlog` row, so all four
  `*_same_org_fk` keys are exercised — including the one that shares a table
  with the append-only trigger.
* The child-first test asserts the specific constraint name
  (`orgs_storeaccess_membership_same_org_fk`), matching the cross-org test.
* The round-trip dump is derived from `INSTALLED_APPS` and excludes only
  `contenttypes`, `auth.Permission`, `sessions` and `admin.LogEntry`, so a
  slice-2 app registered above `apps.orgs` will actually break it.
* `disable_constraint_checking()` is wrapped in `try/finally` with
  `enable_constraint_checking()`.

## D7 — `bulk_update` and the poisoned transaction

`ScopedQuerySet.update()` now carries the warning: `bulk_update` runs its
`update()` calls inside `transaction.atomic(savepoint=False)`, so a
`CrossStoreReferenceError` escapes an atomic block with no savepoint to roll
back to. A caller that catches it cannot continue in the same transaction.

## Not in this round

* **Section C** (C1–C5) and **D8** — `docker/`, `compose.yaml`, `.gitignore`,
  `.dockerignore`, `requirements.txt`. Concurrent devops round.
* **Section E** — carried forward unchanged.
* `README.md`, `docs/SETUP.md`, `docs/DEVELOPMENT.md` — tech-writer's, including
  the false E100 claim at `docs/DEVELOPMENT.md:189-192`.

## Files changed this round

```
common/managers.py                            A1, A2, D7
common/checks.py                              B1
config/settings/prod.py                       B1 (comment)
common/db.py                                  D2, D3
apps/audit/migrations/0002_append_only_trigger.py   D3 (docstring only)
apps/orgs/migrations/0001_initial.py          D4 (docstring only)
tests/test_common_bases.py                    A1, A2
tests/test_common_checks.py                   B2
tests/test_db_stability.py                    D1
tests/test_fixture_loading.py                 D5, D6
tests/fixtures/orgs_dependency_ordered.json   D6
```

No migration operation was altered — the two migration files changed docstrings
only, which is why `makemigrations --check` is clean and every pinned hash still
matches.

## Verbatim evidence

### `docker compose run --rm web pytest -v`

```
======================= 330 passed in 659.37s (0:10:59) ========================
```

(Full per-test listing omitted here for length; the run was clean, 330 of 330,
and the new test names appear in the list below.)

New this round:

```
tests/test_common_bases.py::test_a_multi_store_pin_refuses_to_update_a_store_scoped_foreign_key
tests/test_common_bases.py::test_a_multi_store_pin_refuses_even_an_in_scope_foreign_key
tests/test_common_bases.py::test_a_single_store_pin_still_updates_a_foreign_key_in_its_own_store
tests/test_common_bases.py::test_a_combinator_refuses_an_unscoped_right_hand_side[9 operators]
tests/test_common_bases.py::test_a_combinator_refuses_an_unscoped_left_hand_side[9 operators]
tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge[9 operators]
tests/test_common_bases.py::test_a_combinator_refuses_a_cross_organization_merge_in_the_other_order[9 operators]
tests/test_common_bases.py::test_a_combinator_allows_two_stores_of_one_organization[9 operators]
tests/test_common_bases.py::test_a_merged_queryset_stays_pinned_to_both_stores[__or__, __xor__, union]
tests/test_common_bases.py::test_the_combinator_guard_sits_on_the_query_seam_not_on_a_list_of_names
tests/test_common_checks.py::test_e100_fires_through_the_registry_on_a_test_named_database
tests/test_common_checks.py::test_manage_py_check_refuses_to_pass_on_a_test_named_database
tests/test_common_checks.py::test_e100_is_not_tagged_database_because_that_tag_is_skipped_by_default
tests/test_common_checks.py::test_e100_is_silent_when_the_flag_is_off
tests/test_common_checks.py::test_e100_is_silent_on_a_normally_named_database
tests/test_common_checks.py::test_e100_names_the_offending_alias_and_stays_actionable
tests/test_db_stability.py::test_every_run_sql_statement_in_every_migration_is_pinned
tests/test_fixture_loading.py::test_a_cross_organization_role_is_refused_at_statement_time
tests/test_fixture_loading.py::test_loaddata_leaves_the_composite_keys_deferred_for_the_rest_of_the_transaction
tests/test_fixture_loading.py::test_set_constraints_all_immediate_restores_the_guard_after_a_loaddata
```

### `docker compose run --rm web ruff check .`

```
All checks passed!
```

### `docker compose run --rm web python manage.py check`

```
System check identified no issues (0 silenced).
```

### `docker compose run --rm web python manage.py makemigrations --check --dry-run`

```
No changes detected
```

### `docker compose run --rm web python manage.py makemigrations --check --dry-run --settings=config.settings.test`

```
No changes detected
```
