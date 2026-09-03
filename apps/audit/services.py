"""The only sanctioned way to write the audit trail.

`record()` validates at the boundary (action shape, target saved, org/store
consistency, JSON-serialisable payload) and redacts before anything can land in
the database. Rows are append-only, so a retried caller producing two identical
rows is harmless - duplicates never corrupt state.

What you may put in `changes` (standing policy, privacy ruling C2)
------------------------------------------------------------------
**Field names and IDs for anything personal. Values for anything else.**

    # yes - a business fact, and the diff is the point
    record("product.repriced", changes={"price_before": "1500.00",
                                        "price_after": "1800.00"})
    record("role.updated", changes={"permissions_added": ["sale.create"]})
    record("plan.limit_hit", changes={"store_limit": 5, "stores_used": 5})

    # yes - personal data named, never quoted
    record("user.erased", target=user,
           changes={"reason": "closure", "fields_cleared": ["email", "phone"]})
    record("sale.recorded", changes={"customer_id": 41, "total": "9000.00"})

    # no - the value is personal, and this table refuses UPDATE and DELETE
    record("user.created", changes={"email": "eva@example.rw"})

The reason is structural, not stylistic. An audit row must stop identifying
anyone the moment the row it points at is anonymized: erasure operates on the
*referents* (`erase_user()` anonymizes the `User`), never on the trail. A row
holding foreign keys, a verb, a class label and a timestamp satisfies that. A
row holding a quoted email does not, and the append-only trigger means no
migration-free fix exists. Keeping this true is what lets the trigger carry
zero DELETE exemptions.

The denylists below are a backstop for mistakes, not the policy. They match on
the *key*, so they cannot see an address buried in a free-text `note` or
`reason`; those keys stay allowed because their legitimate use is enums and
short codes. Do not put prose in `changes`.

Other standing rules from the same ruling:

- C3 - a registration audit row may not echo the new user's identifiers.
- C4 - ordinary business services pass no `ip`.
- C5 - a permission denial logs `user_id`, `org_id`, `code`. Never a username
  or an email.
- C6 - never log an exception message together with its payload: `full_clean()`
  on a `User` embeds the email in the `ValidationError`.
- C7 - a soft-delete audit row may not echo the deleted row's personal fields.

Full reasoning: docs/superpowers/specs/2026-09-02-privacy-law-058-2021-ruling.md
"""

import json
import logging
import re
from collections.abc import Mapping

from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import validate_ipv46_address

from apps.audit.models import ACTION_MAX_LENGTH, AuditLog, action_validator

logger = logging.getLogger("raporo.audit")

REDACTED = "[redacted]"
TRUNCATED_SUFFIX = "...[truncated]"
MAX_NESTING = 8
#: Hard ceiling on one row's payload. Audit rows are written on every action, so
#: an attacker-influenced field must not be able to bloat the table.
MAX_CHANGES_BYTES = 16 * 1024
MAX_STRING_CHARS = 1024

# Two denylists, one mechanism, two different reasons. Keep them apart: a
# future reader must be able to tell which list a term is on and why it is
# there, because the two lists get different treatment below.

#: (1) Credentials. Storing one of these is a security incident whatever the
#: privacy law says. Matched as a *substring* of the lowered key, so
#: `recovery_code` catches `recovery_code_hash` and `token` catches
#: `refresh_token`. No exemptions: a handle to a secret (`session_id`) is a
#: secret, so this list is consulted before the reference carve-out.
CREDENTIAL_KEY_PARTS = (
    "password",
    "passphrase",
    "secret",
    "token",
    "totp",
    "otp",
    "recovery_code",
    "api_key",
    "authorization",
    "session",
    "cookie",
)

#: (2) Identifiers. Not secret - personal. Redacted so the trail holds nothing
#: that survives the anonymization of its referent. Substring-matched, which is
#: safe here only because each term is unambiguous wherever it appears inside a
#: key: any key containing `email` is an email address, any key containing
#: `phone` is a phone number.
PII_KEY_PARTS = (
    "email",
    "phone",
    "username",
    "first_name",
    "last_name",
    "full_name",
    "surname",
    "nickname",
    "contact",
    "address",
    "customer",
    "investor",
    "logo",
)

#: (3) Identifiers whose term is too short or too common to substring-match.
#: Matched against whole `_`/`-`/`.`-separated segments of the key instead.
#: `ip` is the reason this set exists: as a substring it would redact
#: `description`, `membership`, `receipt` and `relationship`.
PII_KEY_SEGMENTS = frozenset({"ip", "ips", "ipv4", "ipv6", "name", "names"})

#: Segments that make a following `name` a *person's* name. `name` is the one
#: identifier term that is genuinely ambiguous: `store_name`, `role_name`,
#: `permission_name` and `filename` are business facts whose values are the
#: whole value of a rename audit, while `owner_name` and `member_name` are
#: people. Substring-matching `name` would redact both and leave
#: `store.renamed` recording that something was renamed to something.
#:
#: So the rule is narrow: a `name` segment is personal when it is
#: person-qualified, or when it stands alone (`{"name": ...}` - the referent is
#: unknowable from the key, and ambiguity resolves to redact; a caller who
#: means the shop writes `store_name`, which is the better key anyway).
#:
#: The residual: `store_name` / `org_name` values persist, and for a sole
#: trader a shop name is often the owner's name. That is a documented, bounded
#: exposure - a public commercial identity, not a contact detail - accepted
#: because the alternative destroys the trail's evidentiary value and pushes
#: authors into renaming keys to dodge the redactor.
PERSON_NAME_QUALIFIERS = frozenset(
    {
        "first",
        "last",
        "middle",
        "full",
        "display",
        "given",
        "family",
        "owner",
        "user",
        "person",
        "member",
        "employee",
        "staff",
        "customer",
        "client",
        "investor",
        "contact",
        "actor",
        "payer",
        "payee",
        "recipient",
    }
)

#: Suffixes that mark a key as pointing *at* personal records rather than
#: quoting them: references (`customer_id`, `investor_ids`) and aggregates
#: (`customer_count`). C2 wants both. A foreign key stops identifying anyone
#: once its referent is anonymized - exactly like `AuditLog.actor_id` - and a
#: row count identifies nobody at all; an export audit row is worthless without
#: it. The leading underscore is load-bearing: it keeps `_count` from matching
#: `discount` or `bank_account`.
#:
#: Applies to the identifier lists only, never to credentials.
NON_CONTENT_KEY_SUFFIXES = ("_id", "_ids", "_pk", "_pks", "_uuid", "_uuids", "_count", "_counts")

_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")
#: `name` is deliberately excluded here: it is decided by `_is_person_name`,
#: which needs the qualifier in front of it, not a bare segment hit.
_PII_SEGMENTS_EXCEPT_NAME = PII_KEY_SEGMENTS - {"name", "names"}


def _segments(lowered: str) -> list[str]:
    return [segment for segment in _SEGMENT_SPLIT.split(lowered) if segment]


def _is_credential(lowered: str) -> bool:
    return any(part in lowered for part in CREDENTIAL_KEY_PARTS)


def _is_person_name(segments: list[str]) -> bool:
    if not segments or segments[-1] not in ("name", "names"):
        return False
    if len(segments) == 1:
        return True
    return segments[-2] in PERSON_NAME_QUALIFIERS


def _is_identifier(lowered: str, segments: list[str]) -> bool:
    if any(part in lowered for part in PII_KEY_PARTS):
        return True
    if any(segment in _PII_SEGMENTS_EXCEPT_NAME for segment in segments):
        return True
    return _is_person_name(segments)


def _is_sensitive(key) -> bool:
    """True when this key's *value* must never reach the table."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if _is_credential(lowered):
        return True
    if lowered.endswith(NON_CONTENT_KEY_SUFFIXES):
        return False
    return _is_identifier(lowered, _segments(lowered))


def _redact(value, depth: int = 0):
    if depth >= MAX_NESTING:
        return TRUNCATED_SUFFIX
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive(key) else _redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, str) and len(value) > MAX_STRING_CHARS:
        return value[:MAX_STRING_CHARS] + TRUNCATED_SUFFIX
    return value


def _clean_action(action) -> str:
    if not isinstance(action, str):
        raise TypeError(f"audit action must be a string, got {type(action).__name__}.")
    action = action.strip()
    if len(action) > ACTION_MAX_LENGTH:
        raise ValueError(f"audit action must be at most {ACTION_MAX_LENGTH} characters.")
    action_validator(action)
    return action


def _clean_changes(changes) -> dict:
    if changes is None:
        return {}
    if not isinstance(changes, Mapping):
        raise TypeError(f"audit changes must be a mapping, got {type(changes).__name__}.")
    redacted = _redact(dict(changes))
    try:
        serialised = json.dumps(redacted, cls=DjangoJSONEncoder)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"audit changes are not JSON-serialisable: {exc}") from exc
    if len(serialised.encode()) > MAX_CHANGES_BYTES:
        # Deliberately distinct from "[redacted]": one means "we refused to
        # store this", the other means "it did not fit".
        return {
            "_truncated": TRUNCATED_SUFFIX,
            "_reason": f"payload exceeded {MAX_CHANGES_BYTES} bytes",
            "_original_bytes": len(serialised.encode()),
            "_keys": sorted(str(key) for key in redacted)[:50],
        }
    return redacted


def _clean_target(target) -> tuple[str, int | None]:
    if target is None:
        return "", None
    meta = getattr(target, "_meta", None)
    if meta is None:
        raise TypeError(f"audit target must be a model instance, got {type(target).__name__}.")
    if target.pk is None:
        raise ValueError(
            f"audit target {meta.label} has no primary key: save it before recording."
        )
    return f"{meta.app_label}.{type(target).__name__}", int(target.pk)


def record(action, *, actor=None, org=None, store=None, target=None, changes=None, ip=None):
    """Append one row to the trail and return it.

    Args:
        action: dotted verb, e.g. `store.created`.
        actor: the user who acted, or None for system actions.
        org / store: where it happened. A store implies its org; passing both
            with a mismatch is a tenancy bug and raises.
        target: the model instance the action was about (must be saved).
        changes: JSON-serialisable mapping. Field names and IDs for
            anything personal, values for anything else - see the module
            docstring. Credential and identifier keys are redacted, but
            that is a backstop, not a licence to pass personal values.
        ip: request IP, validated. Ordinary business services pass none
            (privacy ruling C4); a security action may pass one.
    """
    action = _clean_action(action)
    changes = _clean_changes(changes)
    target_type, target_id = _clean_target(target)

    if store is not None:
        if org is None:
            org = store.org
        elif store.org_id != org.pk:
            raise ValueError(
                f"audit store {store.pk} belongs to organization {store.org_id}, "
                f"not {org.pk}: refusing to write a cross-organization row."
            )

    if ip is not None:
        validate_ipv46_address(ip)

    row = AuditLog(
        action=action,
        actor=actor,
        org=org,
        store=store,
        target_type=target_type,
        target_id=target_id,
        changes=changes,
        ip=ip,
    )
    row.save()

    logger.info(
        "audit.recorded",
        extra={
            "audit_id": row.pk,
            "audit_action": action,
            "actor_id": getattr(actor, "pk", None),
            "org_id": getattr(org, "pk", None),
            "store_id": getattr(store, "pk", None),
            "target_type": target_type,
            "target_id": target_id,
        },
    )
    return row


__all__ = ["record"]
