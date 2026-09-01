"""The only sanctioned way to write the audit trail.

`record()` validates at the boundary (action shape, target saved, org/store
consistency, JSON-serialisable payload) and redacts secrets before they can
land in the database. Rows are append-only, so a retried caller producing two
identical rows is harmless - duplicates never corrupt state.
"""

import json
import logging
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

#: Substrings that mark a key as unsafe to keep in the trail.
SENSITIVE_KEY_PARTS = (
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


def _is_sensitive(key) -> bool:
    return isinstance(key, str) and any(part in key.lower() for part in SENSITIVE_KEY_PARTS)


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
        changes: JSON-serialisable mapping; sensitive keys are redacted.
        ip: request IP, validated.
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
