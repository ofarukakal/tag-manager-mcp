"""Interactive terminal CLI for human out-of-band GTM publish approval."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .publish_guard import (
    STATUS_PENDING,
    TARGET_ENVIRONMENT_PREFIX,
    TARGET_LATEST,
    TARGET_LIVE,
    ApprovalStateError,
    NowInput,
    PublishApprovalError,
    approve_request,
    get_request,
    is_request_expired,
    list_requests,
)

MAX_DISPLAY_LENGTH = 120


def sanitize_display(value: Any, *, max_length: int = MAX_DISPLAY_LENGTH) -> str:
    """Escape non-printable characters and truncate long display values safely."""
    text = str(value)
    parts: list[str] = []
    for ch in text:
        if ch.isprintable():
            parts.append(ch)
        else:
            code = ord(ch)
            if code <= 0xFF:
                parts.append(f"\\x{code:02x}")
            elif code <= 0xFFFF:
                parts.append(f"\\u{code:04x}")
            else:
                parts.append(f"\\U{code:08x}")
    escaped = "".join(parts)
    if len(escaped) > max_length:
        if max_length <= 3:
            return escaped[:max_length]
        return escaped[: max_length - 3] + "..."
    return escaped


def _format_target(record: Mapping[str, Any]) -> str:
    target = record.get("target") if "target" in record else TARGET_LIVE
    if target == TARGET_LIVE:
        return "LIVE"
    if target == TARGET_LATEST:
        return "LATEST"
    if isinstance(target, str) and target.startswith(TARGET_ENVIRONMENT_PREFIX):
        env_id = sanitize_display(target[len(TARGET_ENVIRONMENT_PREFIX) :])
        target_name = record.get("target_name")
        if target_name:
            return f"Environment {env_id} ({sanitize_display(target_name)})"
        return f"Environment {env_id}"
    return sanitize_display(target)


def _resolve_display_time(now: NowInput) -> datetime:
    value = now() if callable(now) else now
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _remaining_seconds(expires_at_iso: str, current_time: datetime) -> int:
    expires_at = datetime.fromisoformat(expires_at_iso).astimezone(timezone.utc)
    return max(0, int((expires_at - current_time).total_seconds()))


def _format_diff_summary(diff_summary: dict[str, Any]) -> list[str]:
    """Render a compact human-readable view of the diff summary."""
    if not diff_summary:
        return ["  (no changes reported)"]

    lines: list[str] = []
    for key, value in diff_summary.items():
        safe_key = sanitize_display(key)
        if isinstance(value, dict):
            non_empty_parts: list[str] = []
            for action in ("added", "removed", "changed"):
                items = value.get(action)
                if isinstance(items, list) and items:
                    names = [
                        sanitize_display(
                            entry.get("name") or entry.get("id") or entry
                            if isinstance(entry, dict)
                            else entry
                        )
                        for entry in items
                    ]
                    non_empty_parts.append(f"{action} ({len(items)}): {', '.join(names)}")
                elif isinstance(items, int) and items > 0:
                    non_empty_parts.append(f"{action}: {items}")
            if non_empty_parts:
                lines.append(f"  {safe_key}: " + "; ".join(non_empty_parts))
            elif not any(k in value for k in ("added", "removed", "changed")):
                lines.append(f"  {safe_key}: {sanitize_display(json.dumps(value, sort_keys=True))}")
        elif isinstance(value, list):
            if value:
                rendered = ", ".join(sanitize_display(item) for item in value)
                lines.append(f"  {safe_key} ({len(value)}): {rendered}")
            else:
                lines.append(f"  {safe_key}: 0")
        else:
            lines.append(f"  {safe_key}: {sanitize_display(value)}")
    return lines or ["  (no entity changes)"]


def _print_request_details(
    record: dict[str, Any],
    current_time: datetime,
    out: TextIO,
) -> None:
    approval_id = sanitize_display(record["approval_id"])
    target_label = _format_target(record)
    public_id = sanitize_display(record["public_id"])
    container_name = record.get("container_name")
    container_label = (
        f"{public_id} ({sanitize_display(container_name)})" if container_name else public_id
    )
    account_id = sanitize_display(record["account_id"])
    container_id = sanitize_display(record["container_id"])
    version_id = sanitize_display(record["version_id"])
    version_name = (
        sanitize_display(record["version_name"]) if record.get("version_name") else "(unnamed)"
    )
    live_version = record.get("live_version_id")
    live_label = f"v{sanitize_display(live_version)}" if live_version else "(none)"
    expires_at = sanitize_display(record["expires_at"])
    remaining = _remaining_seconds(record["expires_at"], current_time)
    target = record.get("target") if "target" in record else TARGET_LIVE
    replaces_heading = "Replaces live:   " if target == TARGET_LIVE else "Replaces current:"

    print(f"Publish Approval Request: {approval_id}", file=out)
    print(f"Publishes to: {target_label}", file=out)
    print(f"Container:        {container_label}", file=out)
    print(
        f"Account/ID:       account {account_id}, container {container_id}",
        file=out,
    )
    print(f"Version:          v{version_id} - {version_name}", file=out)
    print(f"{replaces_heading} {live_label}", file=out)
    print(f"Expires at:       {expires_at} (in {remaining}s)", file=out)
    print("Diff summary:", file=out)
    for line in _format_diff_summary(record["diff_summary"]):
        print(line, file=out)


def _list_pending(out: TextIO, *, path: str | Path | None, now: NowInput) -> int:
    current_time = _resolve_display_time(now)
    try:
        requests = list_requests(include_closed=False, path=path, now=current_time)
    except ApprovalStateError as exc:
        print(f"Error: {exc}", file=out)
        return 2
    except PublishApprovalError as exc:
        print(f"Error: {exc}", file=out)
        return 1

    pending = [req for req in requests if req.get("status") == STATUS_PENDING]
    if not pending:
        print("No pending publish approval requests.", file=out)
        return 0

    for req in pending:
        remaining = _remaining_seconds(req["expires_at"], current_time)
        approval_id = sanitize_display(req["approval_id"])
        public_id = sanitize_display(req["public_id"])
        target_label = _format_target(req)
        version_label = f"v{sanitize_display(req['version_id'])}"
        if req.get("version_name"):
            version_label = f"{version_label} ({sanitize_display(req['version_name'])})"
        print(
            f"{approval_id}  {public_id}  [{target_label}]  {version_label}  "
            f"expires in {remaining}s",
            file=out,
        )
    return 0


def _approve_one(
    approval_id: str,
    *,
    in_stream: TextIO,
    out: TextIO,
    check_tty: Callable[[], bool],
    path: str | Path | None,
    now: NowInput,
) -> int:
    if not check_tty():
        print(
            "Refusing to run non-interactively: stdin is not a TTY. "
            "Run `tag-manager-approve` directly in an interactive terminal.",
            file=out,
        )
        return 2

    current_time = _resolve_display_time(now)
    try:
        record = get_request(approval_id, path=path)
    except ApprovalStateError as exc:
        print(f"Error: {exc}", file=out)
        return 2
    except PublishApprovalError as exc:
        print(f"Error: {exc}", file=out)
        return 1

    if record is None:
        print(f"Unknown publish approval request: {sanitize_display(approval_id)}", file=out)
        return 1

    if record["status"] != STATUS_PENDING:
        print(
            f"Publish approval request {sanitize_display(approval_id)} cannot be approved "
            f"(status: {sanitize_display(record['status'])}).",
            file=out,
        )
        return 1

    if is_request_expired(record, now=current_time):
        try:
            approve_request(approval_id, typed_public_id="", now=current_time, path=path)
        except ApprovalStateError as exc:
            print(f"Error: {exc}", file=out)
            return 2
        except PublishApprovalError as exc:
            print(f"Cannot approve: {exc}", file=out)
            return 1

    _print_request_details(record, current_time, out)

    public_id = sanitize_display(record["public_id"])
    out.write(
        f"Type the container ID ({public_id}) to approve publishing, "
        "or anything else to cancel: "
    )
    out.flush()
    typed = in_stream.readline()
    if not typed or not typed.strip():
        print("Cancelled.", file=out)
        return 1

    try:
        approved = approve_request(
            approval_id,
            typed_public_id=typed,
            now=now,
            path=path,
        )
    except ApprovalStateError as exc:
        print(f"Error: {exc}", file=out)
        return 2
    except PublishApprovalError as exc:
        print(f"Cancelled: {exc}", file=out)
        return 1

    print(
        f"Approved publish request {sanitize_display(approved['approval_id'])} for "
        f"{sanitize_display(approved['public_id'])} "
        f"(version {sanitize_display(approved['version_id'])}, target {_format_target(approved)}).",
        file=out,
    )
    return 0


def _stream_is_tty(stream: Any) -> bool:
    """Return True only when stream reports itself as a TTY; fail closed otherwise."""
    isatty_fn = getattr(stream, "isatty", None)
    if not callable(isatty_fn):
        return False
    try:
        return bool(isatty_fn())
    except Exception:
        return False


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    isatty: Callable[[], bool] | None = None,
    path: str | Path | None = None,
    now: NowInput = None,
) -> int:
    """Run the interactive out-of-band GTM publish approval CLI."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    in_stream = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    check_tty = (lambda: _stream_is_tty(in_stream)) if isatty is None else isatty

    if raw_args in (["-h"], ["--help"]):
        print(
            "Usage: tag-manager-approve [<approval_id>]\n"
            "  No arguments: list pending publish approval requests.\n"
            "  <approval_id>: interactively review and approve a pending request in a TTY.",
            file=out,
        )
        return 0

    if len(raw_args) > 1 or any(arg.startswith("-") for arg in raw_args):
        print(
            "Usage: tag-manager-approve [<approval_id>]\n"
            "Non-interactive flags and confirmation arguments are not supported.",
            file=out,
        )
        return 2

    if not raw_args:
        return _list_pending(out, path=path, now=now)

    return _approve_one(
        raw_args[0],
        in_stream=in_stream,
        out=out,
        check_tty=check_tty,
        path=path,
        now=now,
    )


if __name__ == "__main__":
    raise SystemExit(main())
