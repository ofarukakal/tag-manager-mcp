"""Two-step, out-of-band approval guard for publishing GTM container versions."""

from __future__ import annotations

import copy
import fcntl
import hmac
import json
import math
import os
import secrets
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

APPROVAL_FILE_ENV_VAR = "GTM_PUBLISH_APPROVAL_FILE"
DEFAULT_APPROVAL_FILE = Path.home() / ".config/gtm-mcp/publish-approvals.json"
DEFAULT_TTL_SECONDS = 600
MAX_TTL_SECONDS = 86400
MAX_CANCEL_REASON_LENGTH = 200
STATE_SCHEMA_VERSION = 1

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_USED = "used"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"

TARGET_LIVE = "live"
TARGET_LATEST = "latest"
TARGET_ENVIRONMENT_PREFIX = "environment:"

APPROVED_VIA_TERMINAL = "terminal"
APPROVED_VIA_ELICITATION = "elicitation"
VALID_APPROVED_VIA = frozenset({APPROVED_VIA_TERMINAL, APPROVED_VIA_ELICITATION})

VALID_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_APPROVED,
        STATUS_USED,
        STATUS_EXPIRED,
        STATUS_CANCELLED,
    }
)
OPEN_STATUSES = frozenset({STATUS_PENDING, STATUS_APPROVED})

NowInput = float | int | datetime | Callable[[], float | int | datetime] | None

_THREAD_LOCK = threading.Lock()


class PublishApprovalError(PermissionError):
    """Raised when a publish approval is missing, invalid, expired, or fails closed."""


class ApprovalStateError(PublishApprovalError):
    """Raised when the approval state file is corrupt, unreadable, or unwritable."""


def get_approval_file(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the publish approval state file path."""
    if path is not None:
        return Path(path).expanduser()
    source = os.environ if env is None else env
    configured = (source.get(APPROVAL_FILE_ENV_VAR) or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config/gtm-mcp/publish-approvals.json"


def _resolve_now(now: NowInput = None) -> datetime:
    """Resolve an injectable timestamp, datetime, or clock callable into a UTC datetime."""
    value = now() if callable(now) else now
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("Timestamp must be finite")
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    raise TypeError(f"Unsupported clock/timestamp value: {type(value).__name__}")


def _parse_iso_utc(raw: Any, field: str, state_file: Path) -> datetime:
    """Parse a persisted timezone-aware ISO-8601 timestamp or fail closed."""
    if not isinstance(raw, str) or not raw:
        raise ApprovalStateError(
            f"Corrupt publish approval state file {state_file}: invalid {field}"
        )
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApprovalStateError(
            f"Corrupt publish approval state file {state_file}: invalid {field}"
        ) from exc
    if parsed.tzinfo is None:
        raise ApprovalStateError(
            f"Corrupt publish approval state file {state_file}: naive timestamp in {field}"
        )
    return parsed.astimezone(timezone.utc)


def _is_expired(record: Mapping[str, Any], current_time: datetime, state_file: Path) -> bool:
    expires_at = _parse_iso_utc(record.get("expires_at"), "expires_at", state_file)
    return current_time >= expires_at


def is_request_expired(record: Mapping[str, Any], *, now: NowInput = None) -> bool:
    """Return True if ``record`` has reached or passed its expiry timestamp."""
    current_time = _resolve_now(now)
    raw = record.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return True
    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        return True
    return current_time >= expires_at.astimezone(timezone.utc)


def _validate_state(data: Any, state_file: Path) -> dict[str, Any]:
    """Validate persisted JSON schema and fail closed on any corruption."""
    if not isinstance(data, dict):
        raise ApprovalStateError(
            f"Corrupt publish approval state file {state_file}: root must be a JSON object"
        )
    version = data.get("version", STATE_SCHEMA_VERSION)
    if isinstance(version, bool) or version != STATE_SCHEMA_VERSION:
        raise ApprovalStateError(
            f"Corrupt publish approval state file {state_file}: unsupported schema version"
        )
    requests = data.get("requests")
    if not isinstance(requests, dict):
        raise ApprovalStateError(
            f"Corrupt publish approval state file {state_file}: 'requests' must be a JSON object"
        )

    for key, record in requests.items():
        if not isinstance(key, str) or not key:
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: invalid request key"
            )
        if not isinstance(record, dict):
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} must be a JSON object"
            )
        if record.get("approval_id") != key:
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"mismatched approval_id for {key!r}"
            )
        for req_field in ("account_id", "container_id", "public_id", "version_id"):
            val = record.get(req_field)
            if not isinstance(val, str) or not val.strip():
                raise ApprovalStateError(
                    f"Corrupt publish approval state file {state_file}: "
                    f"request {key!r} has invalid {req_field}"
                )
        if not isinstance(record.get("version_name"), str):
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} has invalid version_name"
            )
        live_ver = record.get("live_version_id")
        if live_ver is not None and not isinstance(live_ver, str):
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} has invalid live_version_id"
            )
        container_name = record.get("container_name")
        if container_name is not None and not isinstance(container_name, str):
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} has invalid container_name"
            )
        if "target" in record:
            target_val = record["target"]
            if not isinstance(target_val, str) or not target_val.strip():
                raise ApprovalStateError(
                    f"Corrupt publish approval state file {state_file}: "
                    f"request {key!r} has invalid target"
                )
        target_name = record.get("target_name")
        if target_name is not None and not isinstance(target_name, str):
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} has invalid target_name"
            )
        if not isinstance(record.get("diff_summary"), dict):
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} has invalid diff_summary"
            )
        status = record.get("status")
        if status not in VALID_STATUSES:
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} has unknown status {status!r}"
            )
        created_at = _parse_iso_utc(record.get("created_at"), "created_at", state_file)
        expires_at = _parse_iso_utc(record.get("expires_at"), "expires_at", state_file)
        if expires_at <= created_at:
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} has expires_at <= created_at"
            )
        if status == STATUS_APPROVED:
            _parse_iso_utc(record.get("approved_at"), "approved_at", state_file)
        if status == STATUS_USED:
            _parse_iso_utc(record.get("used_at"), "used_at", state_file)
        approved_via = record.get("approved_via")
        if approved_via is not None and approved_via not in VALID_APPROVED_VIA:
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} has invalid approved_via {approved_via!r}"
            )
        reason = record.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ApprovalStateError(
                f"Corrupt publish approval state file {state_file}: "
                f"request {key!r} has invalid reason"
            )

    return {"version": STATE_SCHEMA_VERSION, "requests": requests}


def _read_state(state_file: Path, *, allow_missing: bool) -> dict[str, Any]:
    """Read and validate the state file; fail closed on any I/O or format error."""
    try:
        raw = state_file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if allow_missing:
            return {"version": STATE_SCHEMA_VERSION, "requests": {}}
        raise ApprovalStateError(
            f"Publish approval state file not found: {state_file}"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ApprovalStateError(
            f"Unreadable publish approval state file {state_file}: {exc}"
        ) from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApprovalStateError(
            f"Corrupt publish approval state file {state_file}: {exc}"
        ) from exc

    return _validate_state(parsed, state_file)


def _write_state_atomic(state_file: Path, state: dict[str, Any]) -> None:
    """Persist state atomically with mode 0600 via temporary file + os.replace."""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    except (OSError, TypeError, ValueError) as exc:
        raise ApprovalStateError(
            f"Failed to prepare publish approval state file {state_file}: {exc}"
        ) from exc

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(state_file.parent),
            prefix=f".{state_file.name}.",
            suffix=".tmp",
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd != -1:
                os.close(fd)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, state_file)
        tmp_path = None
    except OSError as exc:
        raise ApprovalStateError(
            f"Failed to write publish approval state file {state_file}: {exc}"
        ) from exc
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                os.unlink(tmp_path)


@contextmanager
def _exclusive_state_lock(state_file: Path) -> Iterator[None]:
    """Serialize state-file mutations across threads and processes."""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ApprovalStateError(
            f"Cannot access publish approval directory {state_file.parent}: {exc}"
        ) from exc

    lock_path = state_file.with_suffix(state_file.suffix + ".lock")
    with _THREAD_LOCK:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise ApprovalStateError(
                f"Cannot open publish approval lock file {lock_path}: {exc}"
            ) from exc
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _require_nonempty_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _validate_target(target: Any) -> str:
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target must be a non-empty string")
    if target != target.strip() or "\n" in target or "\r" in target:
        raise ValueError("target must not contain leading/trailing whitespace or newlines")
    if target in {TARGET_LIVE, TARGET_LATEST}:
        return target
    if target.startswith(TARGET_ENVIRONMENT_PREFIX):
        env_id = target[len(TARGET_ENVIRONMENT_PREFIX) :]
        if env_id and env_id == env_id.strip():
            return target
    raise ValueError("target must be 'live', 'latest', or 'environment:<environment_id>'")


def _record_target(record: Mapping[str, Any]) -> str:
    if "target" in record:
        return str(record["target"])
    return TARGET_LIVE


def create_request(
    *,
    account_id: str,
    container_id: str,
    public_id: str,
    version_id: str,
    version_name: str,
    live_version_id: str | None,
    diff_summary: dict[str, Any],
    target: str = TARGET_LIVE,
    target_name: str | None = None,
    container_name: str | None = None,
    ttl_seconds: int | float = DEFAULT_TTL_SECONDS,
    now: NowInput = None,
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create and persist a pending publish approval request.

    Any older pending or approved requests for the same ``account_id``,
    ``container_id``, and ``target`` are cancelled (or marked expired if
    already past expiry). Requests created here are always stored with
    ``status='pending'``.
    """
    _require_nonempty_str(account_id, "account_id")
    _require_nonempty_str(container_id, "container_id")
    _require_nonempty_str(public_id, "public_id")
    if public_id != public_id.strip() or "\n" in public_id or "\r" in public_id:
        raise ValueError("public_id must not contain leading/trailing whitespace or newlines")
    _require_nonempty_str(version_id, "version_id")
    validated_target = _validate_target(target)
    if target_name is not None and not isinstance(target_name, str):
        raise TypeError("target_name must be a string or None")
    if version_name is None:
        normalized_version_name = ""
    elif isinstance(version_name, str):
        normalized_version_name = version_name
    else:
        raise TypeError("version_name must be a string")
    if live_version_id is not None and not isinstance(live_version_id, str):
        raise TypeError("live_version_id must be a string or None")
    if container_name is not None and not isinstance(container_name, str):
        raise TypeError("container_name must be a string or None")
    if not isinstance(diff_summary, dict):
        raise TypeError("diff_summary must be a dict")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not math.isfinite(float(ttl_seconds))
        or ttl_seconds <= 0
        or ttl_seconds > MAX_TTL_SECONDS
    ):
        raise ValueError(f"ttl_seconds must be between 0 and {MAX_TTL_SECONDS}")

    # Validate JSON serializability of diff_summary up front.
    try:
        diff_copy = json.loads(json.dumps(diff_summary))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"diff_summary must be JSON-serializable: {exc}") from exc

    state_file = get_approval_file(path, env=env)
    current_time = _resolve_now(now)
    expires_at = current_time + timedelta(seconds=float(ttl_seconds))

    with _exclusive_state_lock(state_file):
        state = _read_state(state_file, allow_missing=True)
        requests: dict[str, dict[str, Any]] = state["requests"]

        for existing in requests.values():
            if (
                existing["account_id"] == account_id
                and existing["container_id"] == container_id
                and _record_target(existing) == validated_target
                and existing["status"] in OPEN_STATUSES
            ):
                if _is_expired(existing, current_time, state_file):
                    existing["status"] = STATUS_EXPIRED
                else:
                    existing["status"] = STATUS_CANCELLED
                    existing["cancelled_at"] = current_time.isoformat()

        approval_id = secrets.token_hex(8)
        while approval_id in requests:
            approval_id = secrets.token_hex(8)

        record: dict[str, Any] = {
            "approval_id": approval_id,
            "account_id": account_id,
            "container_id": container_id,
            "public_id": public_id,
            "container_name": container_name,
            "target": validated_target,
            "target_name": target_name,
            "version_id": version_id,
            "version_name": normalized_version_name,
            "live_version_id": live_version_id,
            "diff_summary": diff_copy,
            "created_at": current_time.isoformat(),
            "expires_at": expires_at.isoformat(),
            "status": STATUS_PENDING,
            "approved_at": None,
            "approved_via": None,
            "used_at": None,
        }
        requests[approval_id] = record
        _write_state_atomic(state_file, state)
        return copy.deepcopy(record)


def get_request(
    approval_id: str,
    *,
    path: str | Path | None = None,
    now: NowInput = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return a copy of the stored approval request for ``approval_id``, or None."""
    state_file = get_approval_file(path, env=env)
    state = _read_state(state_file, allow_missing=True)
    if not isinstance(approval_id, str):
        return None
    record = state["requests"].get(approval_id)
    if record is None:
        return None
    result = copy.deepcopy(record)
    if now is not None and result["status"] in OPEN_STATUSES:
        current_time = _resolve_now(now)
        if _is_expired(result, current_time, state_file):
            result["status"] = STATUS_EXPIRED
    return result


def list_requests(
    *,
    include_closed: bool = False,
    path: str | Path | None = None,
    now: NowInput = None,
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """List approval requests, excluding closed and expired ones by default."""
    state_file = get_approval_file(path, env=env)
    state = _read_state(state_file, allow_missing=True)
    current_time = _resolve_now(now)

    results: list[dict[str, Any]] = []
    for record in state["requests"].values():
        item = copy.deepcopy(record)
        expired = item["status"] in OPEN_STATUSES and _is_expired(item, current_time, state_file)
        if expired:
            item["status"] = STATUS_EXPIRED
        if not include_closed and item["status"] not in OPEN_STATUSES:
            continue
        results.append(item)

    results.sort(key=lambda item: (item["created_at"], item["approval_id"]))
    return results


def _approve_record(
    approval_id: str,
    *,
    typed_public_id: str,
    approved_via: str,
    require_click: bool = False,
    clicked: bool = False,
    now: NowInput = None,
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    state_file = get_approval_file(path, env=env)
    current_time = _resolve_now(now)

    try:
        exists = state_file.exists()
    except OSError as exc:
        raise ApprovalStateError(
            f"Unreadable publish approval state file {state_file}: {exc}"
        ) from exc
    if not exists:
        raise PublishApprovalError(f"Unknown publish approval request: {approval_id!r}")

    with _exclusive_state_lock(state_file):
        state = _read_state(state_file, allow_missing=False)
        record = state["requests"].get(approval_id) if isinstance(approval_id, str) else None
        if record is None:
            raise PublishApprovalError(f"Unknown publish approval request: {approval_id!r}")

        status = record["status"]
        if status == STATUS_USED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has already been used."
            )
        if status == STATUS_APPROVED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} is already approved."
            )
        if status == STATUS_CANCELLED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} was cancelled."
            )
        if status == STATUS_EXPIRED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has expired."
            )
        if status != STATUS_PENDING:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} is not pending (status: {status!r})."
            )

        if _is_expired(record, current_time, state_file):
            record["status"] = STATUS_EXPIRED
            _write_state_atomic(state_file, state)
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has expired."
            )

        if not isinstance(typed_public_id, str):
            raise PublishApprovalError(
                f"Container public ID mismatch for request {approval_id!r}: "
                "typed confirmation must be a string."
            )
        cleaned = typed_public_id.strip()
        if not hmac.compare_digest(cleaned, record["public_id"]):
            raise PublishApprovalError(
                f"Container public ID mismatch for request {approval_id!r}: "
                f"expected {record['public_id']!r}, got {cleaned!r}."
            )
        if require_click and clicked is not True:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} requires explicit click confirmation."
            )

        record["status"] = STATUS_APPROVED
        record["approved_at"] = current_time.isoformat()
        record["approved_via"] = approved_via
        _write_state_atomic(state_file, state)
        return copy.deepcopy(record)


def approve_request(
    approval_id: str,
    *,
    typed_public_id: str,
    now: NowInput = None,
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Approve a pending, unexpired request via the terminal CLI when ``typed_public_id`` matches.

    Marks expired requests as ``expired``. On any other failure (unknown ID,
    wrong typed ID, or non-pending status), leaves the state file unchanged and
    raises ``PublishApprovalError``.
    """
    return _approve_record(
        approval_id,
        typed_public_id=typed_public_id,
        approved_via=APPROVED_VIA_TERMINAL,
        require_click=False,
        now=now,
        path=path,
        env=env,
    )


def approve_via_elicitation(
    approval_id: str,
    *,
    typed_public_id: str,
    clicked: bool,
    now: NowInput = None,
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Approve a pending, unexpired request via two-step MCP elicitation.

    Applies the same checks as ``approve_request`` (pending, unexpired, exact
    public ID match) and also requires ``clicked is True``. Records
    ``approved_via='elicitation'`` on success.
    """
    return _approve_record(
        approval_id,
        typed_public_id=typed_public_id,
        approved_via=APPROVED_VIA_ELICITATION,
        require_click=True,
        clicked=clicked,
        now=now,
        path=path,
        env=env,
    )


def _normalize_cancel_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise TypeError("reason must be a string or None")
    collapsed = " ".join(reason.split())
    if not collapsed:
        return None
    if len(collapsed) > MAX_CANCEL_REASON_LENGTH:
        return collapsed[: MAX_CANCEL_REASON_LENGTH - 3] + "..."
    return collapsed


def cancel_request(
    approval_id: str,
    *,
    reason: str | None = None,
    now: NowInput = None,
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Cancel a pending or approved publish approval request (or mark expired if past expiry).

    Refuses with ``PublishApprovalError`` for ``used``, ``expired``, or
    ``cancelled`` requests. Returns a copy of the record if cancelled, or None
    if unknown or no state file exists.
    """
    clean_reason = _normalize_cancel_reason(reason)
    state_file = get_approval_file(path, env=env)
    current_time = _resolve_now(now)

    try:
        exists = state_file.exists()
    except OSError as exc:
        raise ApprovalStateError(
            f"Unreadable publish approval state file {state_file}: {exc}"
        ) from exc
    if not exists:
        return None

    with _exclusive_state_lock(state_file):
        state = _read_state(state_file, allow_missing=False)
        record = state["requests"].get(approval_id) if isinstance(approval_id, str) else None
        if record is None:
            return None
        status = record["status"]
        if status == STATUS_USED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has already been used."
            )
        if status == STATUS_CANCELLED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} was already cancelled."
            )
        if status == STATUS_EXPIRED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has expired."
            )
        if status not in OPEN_STATUSES:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} cannot be cancelled "
                f"(status: {status!r})."
            )
        if _is_expired(record, current_time, state_file):
            record["status"] = STATUS_EXPIRED
            _write_state_atomic(state_file, state)
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has expired."
            )
        record["status"] = STATUS_CANCELLED
        record["cancelled_at"] = current_time.isoformat()
        if clean_reason is not None:
            record["reason"] = clean_reason
        _write_state_atomic(state_file, state)
        return copy.deepcopy(record)


def consume_approval(
    approval_id: str,
    *,
    account_id: str,
    container_id: str,
    version_id: str,
    target: str,
    current_live_version_id: str | None,
    now: NowInput = None,
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Atomically validate and consume a single-use publish approval.

    This is the sole function that authorises publishing. It succeeds only when
    the request exists, was approved via ``approve_request``, is unexpired, and
    matches ``account_id``, ``container_id``, ``version_id``, ``target``, and
    the original ``live_version_id``. The request is persisted as ``used``
    before returning.
    """
    state_file = get_approval_file(path, env=env)
    current_time = _resolve_now(now)

    try:
        exists = state_file.exists()
    except OSError as exc:
        raise ApprovalStateError(
            f"Unreadable publish approval state file {state_file}: {exc}"
        ) from exc
    if not exists:
        raise PublishApprovalError(f"Unknown publish approval request: {approval_id!r}")

    with _exclusive_state_lock(state_file):
        state = _read_state(state_file, allow_missing=False)
        record = state["requests"].get(approval_id) if isinstance(approval_id, str) else None
        if record is None:
            raise PublishApprovalError(f"Unknown publish approval request: {approval_id!r}")

        status = record["status"]
        if status == STATUS_PENDING:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has not been approved yet "
                f"(run `tag-manager-approve {approval_id}` in a terminal)."
            )
        if status == STATUS_USED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has already been used."
            )
        if status == STATUS_CANCELLED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} was cancelled."
            )
        if status == STATUS_EXPIRED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has expired."
            )
        if status != STATUS_APPROVED:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} is not approved (status: {status!r})."
            )

        # Defence in depth: an approved request must carry a valid approval timestamp.
        _parse_iso_utc(record.get("approved_at"), "approved_at", state_file)

        if _is_expired(record, current_time, state_file):
            record["status"] = STATUS_EXPIRED
            _write_state_atomic(state_file, state)
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} has expired."
            )

        if not isinstance(account_id, str) or record["account_id"] != account_id:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} does not match account_id "
                f"{account_id!r}."
            )
        if not isinstance(container_id, str) or record["container_id"] != container_id:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} does not match container_id "
                f"{container_id!r}."
            )
        if not isinstance(version_id, str) or record["version_id"] != version_id:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} does not match version_id "
                f"{version_id!r}."
            )
        expected_target = _record_target(record)
        if not isinstance(target, str) or expected_target != target:
            raise PublishApprovalError(
                f"Publish approval request {approval_id!r} does not match target "
                f"{target!r} (expected {expected_target!r})."
            )
        expected_live = record["live_version_id"]
        if (
            type(current_live_version_id) is not type(expected_live)
            or current_live_version_id != expected_live
        ):
            raise PublishApprovalError(
                f"Live container version changed since approval request {approval_id!r} "
                f"was created (expected live version {expected_live!r}, "
                f"current live version {current_live_version_id!r})."
            )

        record["status"] = STATUS_USED
        record["used_at"] = current_time.isoformat()
        _write_state_atomic(state_file, state)
        return copy.deepcopy(record)
