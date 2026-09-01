# Fix round 1 — merged Tasks 1+2+3 (consolidated from three gates)

Three gates reviewed your work: code-reviewer (spec ❌ / REQUEST CHANGES), database-engineer (REQUEST CHANGES), security-engineer (**BLOCK**). All three praised the `get_compiler` guard replacement and the mutation pass as genuine — that judgement was right and is not in question.

**The core problem, stated plainly:** the layer's stated job is to make invariant #1 *structurally* true, and it does not yet. The security review built a throwaway harness with the model shapes slice 2 will have (`Category` org-level → `Product` store-scoped; `Sale` → `SaleLine` both store-scoped), ran the real Django 6.1 ORM against your real `common/` bases, and **reproduced** cross-tenant reads and writes that the guard permits while `manage.py check` stays silent. Every one of them fires on the first store-scoped model slice 2 adds. Fixing it after slice 2 means re-auditing every query written in between.

Nothing below is theoretical: each item marked (reproduced) was executed.

---

## A. BLOCKING — structural invariant #1 (do these first, in this order)

**A1 · Reverse related managers are unguarded and soft-delete-blind** — `common/models.py:121-127`, no compensating check in `common/checks.py`.
Django builds reverse managers from `related_model._default_manager.__class__`, which you deliberately made the unguarded `all_objects`. Reproduced: `category.products.all()` returned products from two different orgs; `sale_a.lines.all()` returned cross-store children; after `line_b.soft_delete()` the deleted row was still returned; `manage.py check` said "no issues".
**Fix:** add check `common.E004` — for every concrete `StoreScopedModel`, iterate `model._meta.related_objects` and error if any `rel.get_accessor_name()` is not `None`. (Verified by the reviewer: `related_name="+"` suppresses the accessor, `prefetch_related`, and the `parent__child` join lookup.) Document the residual caveat: the literal query name `+` still joins via `filter(**{"+__name": ...})`, so ORM filter keys must never be built from user input.

**A2 · Same-store FK validation, enforced in `save()` not `clean()`** — `common/models.py:95-127`.
Reproduced: `SaleLine.objects.create(store=store_B, sale=<a store_A sale>, product=<a store_B product>)` was accepted, and `saleline.product` resolved a store-B product from a store-A object graph via `_base_manager`. That is textbook IDOR the moment a form accepts `product_id`.
**Fix:** in `StoreScopedModel.save()` (so `objects.create()` cannot skip it), loop `self._meta.concrete_fields`; for each FK whose `related_model` is a `StoreScopedModel` subclass, assert the referenced row's `store_id == self.store_id` and raise otherwise. Add a system check that such FKs exist only between store-scoped models. A1 + A2 together close the join-traversal leak below.

**A3 · A correctly scoped query still reads other tenants through a relation join** — `common/managers.py:92-105`. (No separate fix; A1 + A2 close it — but add the regression tests.)
Reproduced with a fully scoped root: `Product.objects.for_store(my_store).values_list("category__products__name", flat=True)` returned `['my-secret-product', 'RIVAL-SECRET-PRODUCT']`; the same shape via `aggregate(Count(...))` counted a rival org's rows; and `.filter(category__products__name="RIVAL-SECRET-PRODUCT").exists()` returned `True` — a clean oracle for probing a competitor's catalogue.
**Required tests:** assert that both the `category__products__name` join and the `exists()` oracle now fail.

**A4 · `for_store()` does not scope writes** — `common/managers.py:132-154` guards `update()` only.
Reproduced: `Product.objects.for_store(store_A).create(store=store_B, ...)` landed a row in store 2; same for `bulk_create`.
**Fix:** have `for_store`/`for_stores` retain the pinned pk(s) on the queryset, and override `create`, `get_or_create`, `update_or_create`, `bulk_create` to default `store_id` to the single pinned store and raise `UnscopedQueryError` when a different one is supplied. (This also closes code-review Minor 9: `for_store(s).create(name="x")` currently fails with a NOT NULL IntegrityError.)

**A5 · `Membership.role` and `StoreAccess.store` can cross organizations** — `apps/orgs/models.py:190-196` and `:230-236`.
Reproduced: `Membership.objects.create(org=org_A, role=<org_B role>)` and `StoreAccess.objects.create(membership=<org_A membership>, store=<org_B store>)` both succeeded — `clean()` exists but the ORM write path never calls it. One such row hands an org-A member a legitimate-looking `for_store()` handle on org B's store.
**Fix (do the real thing now — the tables are empty and the migration is still uncommitted, so it is free today and expensive later):** denormalize `org` onto `Role`, `Membership`, `StoreAccess` where not already present; add `UniqueConstraint(fields=["id", "org"])` on `Store`/`Role`/`Membership`; add composite FKs via `RunSQL` (e.g. `FOREIGN KEY (role_id, org_id) REFERENCES orgs_role(id, org_id)`), reversible. Apply the same consistency guarantee to `AuditLog`'s `org`/`store` pair (the database reviewer flagged that `AuditLog.objects.create(org=org_A, store=<org_B store>)` bypasses the check that only lives in `audit.services.record()`). If any part genuinely cannot be done in-migration, the floor is: every write goes through a service calling `full_clean()`, plus explicit rejection tests — and say so in your report.

**A6 · `common.E005` — unique constraints on store-scoped models** (required by security in place of documentation; also code-review I5a).
Reproduced existence oracle: because `_default_manager` is `all_objects`, `full_clean()` queries globally, so tenant B validating `code="ABC-1"` got *"Coded with this Code already exists."* for a row owned by tenant A.
**Fix:** system check `common.E005` — every `unique=True` field and every `UniqueConstraint` on a store-scoped model must include `store` **and** be conditioned on `deleted_at IS NULL`; otherwise startup error. (Your report's concern 11 got the soft-delete half and missed the `store` half.)

## B. BLOCKING — audit integrity and hard deletes

**B1 · The audit log is forgeable in-process** — `apps/audit/models.py:97-102`. Both reviewers found this independently.
Reproduced: `AuditLog(pk=1, action="user.login", actor=alice, org=org, changes={}, at=timezone.now()).save()` **overwrote** an existing row — Mallory's `sale.below_floor_override` entry became a login by Alice. `_state.adding` is `True` even with an explicit pk, and Django 6.1's `_save_table` takes the UPDATE branch when `pk_set and not force_insert` (the skip-UPDATE shortcut needs every pk field to have a default, which `BigAutoField` does not). Without an explicit `at` it currently fails only on a NOT NULL — the guard is held up by an accident of `auto_now_add`, not design. Separately, `AuditLog._base_manager.filter(...).delete()` hard-deleted a row, because your `AppendOnlyQuerySet` protections live on `objects` only.
**Fix:** `if not self._state.adding or self.pk is not None: raise AppendOnlyError(...)`, pass `force_insert=True` to `super().save()`, set `Meta.base_manager_name = "objects"`, and reject `update_conflicts` in a `bulk_create` override. Test by constructing with an existing pk, saving, asserting the exception **and** that the stored row is byte-identical afterwards.

**B2 · Database-level append-only guard on `audit_auditlog`** — the database reviewer did not bless Python-only enforcement, and provided the complete migration. Use it as given (plpgsql function + `BEFORE UPDATE OR DELETE` row trigger + `BEFORE TRUNCATE` statement trigger, because TRUNCATE bypasses row triggers), as `apps/audit/migrations/0002_append_only_trigger.py`, fully reversible via DROP TRIGGER/DROP FUNCTION. It is in the database review verbatim — ask me if you need it re-pasted. `REVOKE UPDATE, DELETE` was considered and rejected: a single DB role would block Django's own migrations. **This is the pattern slice 2's ledger tables will copy, so write it as a reusable snippet, not bespoke.**

**B3 · `accounts.User` has no hard-delete guard** — `apps/accounts/models.py:26-101`. Violates the binding global constraint "No hard deletes anywhere", and was not declared in your §6/§7. `user.delete()` and `User.objects.filter(...).delete()` are live; PROTECT only saves users who have already acted, so a fresh account can be erased permanently.
**Fix:** override `User.delete()` to raise `HardDeleteForbidden`, give `UserManager` a queryset whose `delete()`/`_raw_delete()` raise, keep `is_active=False` as the deactivation path, and add tests mirroring `test_orgs_models_refuse_hard_delete`. Do NOT invent an erasure path — that is a privacy decision I am carrying forward separately.

**B4 · `all_objects` can hard-delete** — `common/models.py:68` and `:121` (reproduced). Fix: `models.Manager.from_queryset(NoHardDeleteQuerySet)` where the queryset refuses `delete()`/`_raw_delete()` but does **not** filter `deleted_at` — it must stay valid as `base_manager`.

## C. BLOCKING — checks, constraints, uploads

**C1 · `common.E002` is negative-only and misses the actual hole** — `common/checks.py:38-52`. Django's `Options.default_manager` skips the abstract-MRO fallback the moment a subclass declares **any** local manager (`if not default_manager_name and not self.local_managers`), and `Options.managers` sorts depth-0 first. So a perfectly plausible slice-2 model with `recent = RecentManager()` silently gets `recent` as `_default_manager`, no error. Your report's claim that the hole is "a concrete model that declares its own `objects`" is wrong.
**Fix:** assert positively — `if model._default_manager.name != "all_objects": Error(..., id="common.E002")`. Add a check test declaring an extra non-`objects` manager, and correct the claim in your report/docstring.

**C2 · `Organization.slug` breaks the partial-unique pattern the rest of the file gets right** — `apps/orgs/models.py:32`. `Store`/`Role`/`Membership`/`StoreAccess` all correctly use `condition=LIVE`; `slug` is plain `unique=True`, so soft-deleting an org reserves its slug forever and a re-signup gets an IntegrityError/500. Free to fix now while the table is empty.
**Fix:** drop `unique=True`, add `UniqueConstraint(fields=["slug"], condition=LIVE, name="orgs_organization_unique_live_slug", violation_error_message=_("That URL slug is taken."))`. Note in the migration docstring that reversing on a database that has since accumulated a soft-deleted-then-recreated duplicate slug will fail the down-migration.

**C3 · `Organization.logo` upload is unrestricted** — `apps/orgs/models.py:33` + `MEDIA_ROOT = BASE_DIR / "media"`. An org owner uploading `logo.svg` carrying script gets stored XSS served from the app's own origin, and the flat directory is readable by URL.
**Fix now (model + settings layer):** `ImageField` + `FileExtensionValidator(["png","jpg","jpeg","webp"])` + Pillow content verification + an explicit max-size validator; set `DATA_UPLOAD_MAX_MEMORY_SIZE`/`FILE_UPLOAD_MAX_MEMORY_SIZE`; randomize the stored filename; move `MEDIA_ROOT` outside `BASE_DIR`. Add Pillow to requirements (it is now justified — content verification needs it). **Deferred with an owner:** serving media from a separate origin/bucket with fixed safe `Content-Type` + nosniff is devops-engineer's, at deploy.

## D. Also in this round (cheap, converged, or my rulings)

**D1 · Guard is not closed under `|`** — `common/managers.py:92-105` (reproduced): `Product.objects.for_store(A) | Product.all_objects.all()` returned every store's rows, because the combined query inherits `store_scoped = True` from the left operand. Fix: override `__or__`/`__and__` to require both operands scoped and merge their scope sets.

**D2 · `for_stores()` accepts stores from different organizations** — `common/managers.py:68-76` (reproduced). Fix: resolve the distinct `org_id` set for the pks and raise unless it is exactly one.

**D3 · Username namespace collision** — `apps/accounts/models.py:27-45` (reproduced): `UnicodeUsernameValidator` permits `@` and all-digit values, so a user can register `username="victim@example.com"` or `username="250788111111"`, making the planned username-OR-email-OR-phone resolution ambiguous — at worst a password reset routed to the wrong row. Fix now while it is one validator: reject `@` and all-digit usernames.

**D4 · MY RULING — add the two branding fields now.** Elvis approved a three-level branding chain (store → org → Raporo default) after your dispatch began. Add to `Store`: `brand = models.JSONField(default=dict, blank=True)` and `use_own_branding = models.BooleanField(default=False)`, both with translated verbose names, folded into the **uncommitted** `orgs/0001_initial` so no extra migration is spent. Semantics (implemented later, in slice 4 — do NOT build resolution logic now): toggle False → inherit org branding entirely; toggle True → store's values apply with per-field fallback to org, then to a Raporo default. Store *name* never inherits. Record in your report that `resolve_branding(store)` is slice 4's job.

**D5 · `soft_delete(by=None)` produces an unattributable tombstone and writes no audit row** — `common/models.py:73-87`. Fix: reject `by=None` unless an explicit `system=True` flag is passed, and note in your report that `soft_delete` does not audit itself (Task 4's services must).

**D6 · Cap the audit `changes` payload** — `apps/audit/services.py:66-76`: no size cap, so an attacker-influenced payload can bloat the table. Cap the serialized length (~16 KB) and truncate with a distinguishable marker (also make truncation distinguishable from `[redacted]`).

**D7 · Docstrings that are actively false** (three reviewers, three catches): `common/models.py:1-6` says `common` is "not an installed app" (it is, via `CommonConfig`); `apps/orgs/models.py:3-5` says `Store.org` is "the only pointer to an organization in the whole schema" (`Role.org`, `Membership.org`, `AuditLog.org` exist — and this claim is load-bearing for A5); `common/models.py:104-108` claims Django inherits `default_manager_name` from the abstract Meta even when a subclass declares its own Meta — verified false (`_meta.default_manager_name` is `None` for a Meta-declaring subclass), so the real protection is declaration order plus C1's check. Fix all three, and add the test asserting `_default_manager.name == "all_objects"` for a Meta-declaring subclass.

**D8 · Keep both unique indexes on username/email, and say why** — `apps/accounts/models.py` Meta. The database reviewer traced `Model.validate_constraints()`: an expression-only `UniqueConstraint` raises a **non-field** `ValidationError` under `NON_FIELD_ERRORS`, so the plain `unique=True` is what produces the per-field form error. Add a one-line Meta comment recording that reason, or a future reviewer will "clean it up".

**D9 · `common/checks.py:86` ignores `app_configs`**, so `manage.py check <app>` reports errors for unrelated apps. Filter when provided.

**D10 · Delete the three tautological tests** or make them earn their names: `test_removing_the_scope_filter_would_be_caught` (asserts `"store_id" in sql` — a tautology given `filter(store_id=...)`), `test_hard_delete_forbidden_is_a_notimplementederror`, `test_store_limit_constant_is_five`. Also make `test_store_carries_the_only_org_pointer` assert what its name claims: that no `StoreScopedModel` subclass declares an `org` field.

---

## Explicitly NOT in this round (parked by me, with owners — do not do these)

- Third `live_objects` manager as `default_manager_name` (code-review I5b, non-blocking): **parked.** A1's E004 closes the reverse-manager leak structurally and A6's E005 closes the unique-validation oracle, which were the two concrete drivers. Revisit only if a real need appears.
- `User` erasure/anonymisation pathway (both reviewers): needs a `privacy-compliance` decision under Law 058/2021 before any account-deletion UI. B3 only refuses hard delete.
- Prod-settings gaps — CSP, `SECURE_PROXY_SSL_HEADER`, `CSRF_TRUSTED_ORIGINS`, `CSRF_COOKIE_SAMESITE`: must land with the first rendered page (tasks 8-9), not here.
- `Role.permissions` DB `CheckConstraint` + service whitelist, and the Manager-escalation rule (`PRESETS["Manager"]` holds `member.manage` but not `role.manage`, so a Manager could reassign themselves to Owner): Task 4's services own both — a member must never be able to grant a permission they do not hold, nor edit their own membership.
- `AuditLog (org, action, at)` composite index: when the audit-view screen is built.
- `FileField` never deletes the old file on replace: the logo-upload flow (slice 4/10) needs explicit `pre_save` cleanup.
- Serving media from a separate origin: devops-engineer, at deploy.
- CI step `makemigrations --check --settings=config.settings.test`: task 11 owns CI.

## Carry-forward requirements for later tasks (put these in your report so they survive)

- **Task 5 (auth backend), from the database review:** do **not** use `__iexact` against `username`/`email`. Django's Postgres backend compiles `iexact` to `UPPER(col::text) = UPPER(%s)`, which cannot use your `lower()` functional index — the lookup would silently sequential-scan. Match the constraint's own expression instead (`filter(username=Lower(identifier))`). `UniqueConstraint.validate()` builds the `Lower(...)` comparison correctly, so `full_clean()` is fine as-is; only ad-hoc lookup code needs this discipline. Also: pick the field by identifier shape and use `.get()`, never `.first()`.
- **Task 4 (services):** soft-deleting a parent leaves live children pointing at a dead store — PROTECT never fires because hard delete is forbidden, and `for_store(<soft-deleted store>)` happily returns rows. Services need an explicit policy (cascade soft-delete, or refuse while live children exist).
- **Tasks 4/10 (member list, permission checks):** nothing auto-eager-loads. Use `Membership.objects.filter(org=org).select_related("user", "role").prefetch_related("store_access__store")` — two queries, no N+1.
- Signup/reset flows must not echo `violation_error_message` text ("A user with that email address already exists.") — reset always reports success; signup notifies the existing address out-of-band.

## Evidence required before I re-review

Re-run and paste verbatim: `docker compose run --rm web pytest -v` (full suite), `ruff check .`, `python manage.py makemigrations --check --dry-run` under both dev and test settings, and `python manage.py check`. Append to `task-123-report.md` with each item above marked done/deviated. New regression tests are required for A1, A2, A3, A4, A5, A6, B1, B3, B4, C2, D1, D2, D3 — the reproductions above are your test cases.
