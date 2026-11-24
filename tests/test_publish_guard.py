from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tag_manager_mcp.publish_guard import (
    APPROVAL_FILE_ENV_VAR,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_USED,
    ApprovalStateError,
    PublishApprovalError,
    approve_request,
    consume_approval,
    create_request,
    get_approval_file,
    get_request,
    list_requests,
)

BASE_TIME = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


def _sample_diff() -> dict[str, Any]:
    return {
        "tag": {"added": ["GA4 Config"], "removed": [], "changed": ["Checkout Event"]},
        "trigger": {"added": [], "removed": ["Old Click"], "changed": []},
    }


def _create_sample(
    path: Path,
    *,
    account_id: str = "100",
    container_id: str = "200",
    public_id: str = "GTM-ABC123",
    version_id: str = "7",
    version_name: str = "Release 7",
    live_version_id: str | None = "6",
    target: str = "live",
    target_name: str | None = None,
    ttl_seconds: int = 600,
    now: Any = BASE_TIME,
    container_name: str | None = "Web Store",
) -> dict[str, Any]:
    return create_request(
        account_id=account_id,
        container_id=container_id,
        public_id=public_id,
        version_id=version_id,
        version_name=version_name,
        live_version_id=live_version_id,
        diff_summary=_sample_diff(),
        target=target,
        target_name=target_name,
        container_name=container_name,
        ttl_seconds=ttl_seconds,
        now=now,
        path=path,
    )


def test_exception_hierarchy() -> None:
    assert issubclass(PublishApprovalError, PermissionError)
    assert issubclass(ApprovalStateError, PublishApprovalError)


def test_get_approval_file_resolution(tmp_path: Path) -> None:
    assert get_approval_file(env={}) == Path.home() / ".config/gtm-mcp/publish-approvals.json"
    env_path = tmp_path / "env-approvals.json"
    assert get_approval_file(env={APPROVAL_FILE_ENV_VAR: str(env_path)}) == env_path
    explicit = tmp_path / "explicit.json"
    assert get_approval_file(explicit, env={APPROVAL_FILE_ENV_VAR: str(env_path)}) == explicit


def test_create_request_persists_pending_with_0600_mode(tmp_path: Path) -> None:
    state_file = tmp_path / "nested" / "approvals.json"
    req = _create_sample(state_file)

    assert len(req["approval_id"]) == 16
    assert int(req["approval_id"], 16) >= 0
    assert req["status"] == STATUS_PENDING
    assert req["approved_at"] is None
    assert req["used_at"] is None
    assert req["account_id"] == "100"
    assert req["container_id"] == "200"
    assert req["public_id"] == "GTM-ABC123"
    assert req["version_id"] == "7"
    assert req["version_name"] == "Release 7"
    assert req["live_version_id"] == "6"
    assert req["created_at"] == BASE_TIME.isoformat()
    assert req["expires_at"] == (BASE_TIME + timedelta(seconds=600)).isoformat()

    mode = stat.S_IMODE(os.stat(state_file).st_mode)
    assert mode == 0o600

    # Mutating the returned dict must not affect persisted state.
    req["status"] = STATUS_APPROVED
    loaded = get_request(req["approval_id"], path=state_file)
    assert loaded is not None
    assert loaded["status"] == STATUS_PENDING


def test_create_request_cannot_create_approved_or_invalid_ttl(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"

    with pytest.raises(TypeError):
        create_request(  # type: ignore[call-arg]
            account_id="100",
            container_id="200",
            public_id="GTM-ABC123",
            version_id="7",
            version_name="v7",
            live_version_id="6",
            diff_summary={},
            status="approved",
            path=state_file,
        )

    for bad_ttl in (0, -1, 10**9, float("inf"), True):
        with pytest.raises(ValueError):
            create_request(
                account_id="100",
                container_id="200",
                public_id="GTM-ABC123",
                version_id="7",
                version_name="v7",
                live_version_id="6",
                diff_summary={},
                ttl_seconds=bad_ttl,  # type: ignore[arg-type]
                path=state_file,
            )


def test_new_request_cancels_older_pending_and_approved_for_same_container(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    first = _create_sample(state_file, version_id="7", now=BASE_TIME)
    other_container = _create_sample(
        state_file,
        account_id="100",
        container_id="999",
        public_id="GTM-OTHER1",
        version_id="1",
        now=BASE_TIME + timedelta(seconds=1),
    )
    second = _create_sample(
        state_file, version_id="8", now=BASE_TIME + timedelta(seconds=2)
    )
    approve_request(
        second["approval_id"],
        typed_public_id="GTM-ABC123",
        now=BASE_TIME + timedelta(seconds=3),
        path=state_file,
    )
    third = _create_sample(
        state_file, version_id="9", now=BASE_TIME + timedelta(seconds=4)
    )

    assert get_request(first["approval_id"], path=state_file)["status"] == STATUS_CANCELLED
    assert get_request(second["approval_id"], path=state_file)["status"] == STATUS_CANCELLED
    assert get_request(third["approval_id"], path=state_file)["status"] == STATUS_PENDING
    assert get_request(other_container["approval_id"], path=state_file)["status"] == STATUS_PENDING

    # Cancelled approved request can no longer be consumed.
    with pytest.raises(PublishApprovalError, match="cancelled"):
        consume_approval(
            second["approval_id"],
            account_id="100",
            container_id="200",
            version_id="8",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=5),
            path=state_file,
        )


def test_get_and_list_requests(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    assert get_request("missing", path=state_file) is None
    assert list_requests(path=state_file, now=BASE_TIME) == []

    r1 = _create_sample(state_file, container_id="200", ttl_seconds=60, now=BASE_TIME)
    r2 = _create_sample(
        state_file,
        container_id="201",
        public_id="GTM-201",
        ttl_seconds=600,
        now=BASE_TIME + timedelta(seconds=10),
    )

    open_at_30 = list_requests(path=state_file, now=BASE_TIME + timedelta(seconds=30))
    assert [r["approval_id"] for r in open_at_30] == [r1["approval_id"], r2["approval_id"]]

    open_at_120 = list_requests(path=state_file, now=BASE_TIME + timedelta(seconds=120))
    assert [r["approval_id"] for r in open_at_120] == [r2["approval_id"]]

    all_at_120 = list_requests(
        include_closed=True, path=state_file, now=BASE_TIME + timedelta(seconds=120)
    )
    by_id = {r["approval_id"]: r for r in all_at_120}
    assert by_id[r1["approval_id"]]["status"] == STATUS_EXPIRED
    assert by_id[r2["approval_id"]]["status"] == STATUS_PENDING


def test_approve_request_exact_case_sensitive_match_and_strip(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_sample(state_file, public_id="GTM-AbC123", now=BASE_TIME)

    # Wrong case leaves request pending.
    with pytest.raises(PublishApprovalError, match="mismatch"):
        approve_request(
            req["approval_id"],
            typed_public_id="gtm-abc123",
            now=BASE_TIME + timedelta(seconds=5),
            path=state_file,
        )
    assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_PENDING

    # Exact match with surrounding whitespace succeeds.
    approved = approve_request(
        req["approval_id"],
        typed_public_id="  GTM-AbC123 \n",
        now=BASE_TIME + timedelta(seconds=10),
        path=state_file,
    )
    assert approved["status"] == STATUS_APPROVED
    assert approved["approved_at"] == (BASE_TIME + timedelta(seconds=10)).isoformat()

    # Re-approving an already approved request fails.
    with pytest.raises(PublishApprovalError, match="already approved"):
        approve_request(
            req["approval_id"],
            typed_public_id="GTM-AbC123",
            now=BASE_TIME + timedelta(seconds=11),
            path=state_file,
        )


def test_approve_request_unknown_and_expired_marks_expired(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    with pytest.raises(PublishApprovalError, match="Unknown"):
        approve_request("nonexistent", typed_public_id="GTM-ABC123", now=BASE_TIME, path=state_file)

    req = _create_sample(state_file, ttl_seconds=60, now=BASE_TIME)
    # Exact boundary (created_at + 60s) is expired (fail closed).
    with pytest.raises(PublishApprovalError, match="expired"):
        approve_request(
            req["approval_id"],
            typed_public_id="GTM-ABC123",
            now=BASE_TIME + timedelta(seconds=60),
            path=state_file,
        )
    persisted = get_request(req["approval_id"], path=state_file)
    assert persisted is not None
    assert persisted["status"] == STATUS_EXPIRED


def test_consume_approval_full_lifecycle_and_single_use(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_sample(state_file, now=BASE_TIME)

    # Cannot consume while still pending.
    with pytest.raises(PublishApprovalError, match="not been approved"):
        consume_approval(
            req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=5),
            path=state_file,
        )

    approve_request(
        req["approval_id"],
        typed_public_id="GTM-ABC123",
        now=BASE_TIME + timedelta(seconds=10),
        path=state_file,
    )

    consumed = consume_approval(
        req["approval_id"],
        account_id="100",
        container_id="200",
        version_id="7",
        target="live",
        current_live_version_id="6",
        now=BASE_TIME + timedelta(seconds=20),
        path=state_file,
    )
    assert consumed["status"] == STATUS_USED
    assert consumed["used_at"] == (BASE_TIME + timedelta(seconds=20)).isoformat()

    # Second consume of same id must fail.
    with pytest.raises(PublishApprovalError, match="already been used"):
        consume_approval(
            req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=21),
            path=state_file,
        )

    # Approving a used request also fails.
    with pytest.raises(PublishApprovalError, match="already been used"):
        approve_request(
            req["approval_id"],
            typed_public_id="GTM-ABC123",
            now=BASE_TIME + timedelta(seconds=22),
            path=state_file,
        )


def test_consume_approval_enforces_bindings_and_expiry(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_sample(state_file, ttl_seconds=120, now=BASE_TIME)
    approve_request(
        req["approval_id"],
        typed_public_id="GTM-ABC123",
        now=BASE_TIME + timedelta(seconds=5),
        path=state_file,
    )

    # Wrong account_id
    with pytest.raises(PublishApprovalError, match="account_id"):
        consume_approval(
            req["approval_id"],
            account_id="999",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=10),
            path=state_file,
        )

    # Wrong container_id
    with pytest.raises(PublishApprovalError, match="container_id"):
        consume_approval(
            req["approval_id"],
            account_id="100",
            container_id="999",
            version_id="7",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=10),
            path=state_file,
        )

    # Wrong version_id
    with pytest.raises(PublishApprovalError, match="version_id"):
        consume_approval(
            req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="8",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=10),
            path=state_file,
        )

    # Live version changed since request was created
    with pytest.raises(PublishApprovalError, match="Live container version changed"):
        consume_approval(
            req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id="7",
            now=BASE_TIME + timedelta(seconds=10),
            path=state_file,
        )

    # Live version None vs "6"
    with pytest.raises(PublishApprovalError, match="Live container version changed"):
        consume_approval(
            req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id=None,
            now=BASE_TIME + timedelta(seconds=10),
            path=state_file,
        )

    # Expired after approval
    with pytest.raises(PublishApprovalError, match="expired"):
        consume_approval(
            req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=120),
            path=state_file,
        )
    assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_EXPIRED


def test_consume_approval_first_publish_with_none_live_version(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_sample(state_file, live_version_id=None, now=BASE_TIME)
    approve_request(
        req["approval_id"],
        typed_public_id="GTM-ABC123",
        now=BASE_TIME + timedelta(seconds=1),
        path=state_file,
    )

    with pytest.raises(PublishApprovalError, match="Live container version changed"):
        consume_approval(
            req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id="",
            now=BASE_TIME + timedelta(seconds=2),
            path=state_file,
        )

    consumed = consume_approval(
        req["approval_id"],
        account_id="100",
        container_id="200",
        version_id="7",
        target="live",
        current_live_version_id=None,
        now=BASE_TIME + timedelta(seconds=2),
        path=state_file,
    )
    assert consumed["status"] == STATUS_USED


def test_concurrent_consume_allows_exactly_one_winner(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_sample(state_file, now=BASE_TIME)
    approve_request(
        req["approval_id"],
        typed_public_id="GTM-ABC123",
        now=BASE_TIME + timedelta(seconds=1),
        path=state_file,
    )

    def attempt(_: int) -> str:
        try:
            consume_approval(
                req["approval_id"],
                account_id="100",
                container_id="200",
                version_id="7",
                target="live",
                current_live_version_id="6",
                now=BASE_TIME + timedelta(seconds=2),
                path=state_file,
            )
            return "ok"
        except PublishApprovalError:
            return "denied"

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))

    assert outcomes.count("ok") == 1
    assert outcomes.count("denied") == 5


@pytest.mark.parametrize(
    "corrupt_content",
    [
        "",
        "{not valid json",
        "[]",
        "{}",
        json.dumps({"version": 99, "requests": {}}),
        json.dumps({"version": 1, "requests": []}),
        json.dumps({"version": 1, "requests": {"abc": "not-a-dict"}}),
        json.dumps(
            {
                "version": 1,
                "requests": {
                    "abc": {
                        "approval_id": "abc",
                        "account_id": "100",
                        "container_id": "200",
                        "public_id": "GTM-ABC123",
                        "version_id": "7",
                        "version_name": "v7",
                        "live_version_id": "6",
                        "diff_summary": {},
                        "created_at": BASE_TIME.isoformat(),
                        "expires_at": (BASE_TIME + timedelta(seconds=600)).isoformat(),
                        "status": "approved",
                        "approved_at": None,
                    }
                },
            }
        ),
    ],
)
def test_corrupt_state_file_fails_closed(tmp_path: Path, corrupt_content: str) -> None:
    state_file = tmp_path / "corrupt.json"
    state_file.write_text(corrupt_content, encoding="utf-8")

    with pytest.raises(PublishApprovalError):
        get_request("abc", path=state_file)
    with pytest.raises(PublishApprovalError):
        list_requests(path=state_file, now=BASE_TIME)
    with pytest.raises(PublishApprovalError):
        approve_request("abc", typed_public_id="GTM-ABC123", now=BASE_TIME, path=state_file)
    with pytest.raises(PublishApprovalError):
        consume_approval(
            "abc",
            account_id="100",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME,
            path=state_file,
        )


def test_unreadable_state_file_fails_closed(tmp_path: Path) -> None:
    # A directory where a file is expected is unreadable as a JSON state file.
    dir_as_file = tmp_path / "state-dir"
    dir_as_file.mkdir()

    with pytest.raises(PublishApprovalError):
        get_request("abc", path=dir_as_file)
    with pytest.raises(PublishApprovalError):
        consume_approval(
            "abc",
            account_id="100",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME,
            path=dir_as_file,
        )


def test_target_is_required_enforced_and_scoped_for_cancellation(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    live_req = _create_sample(state_file, target="live", now=BASE_TIME)
    env7_req = _create_sample(
        state_file,
        target="environment:7",
        target_name="Staging",
        now=BASE_TIME + timedelta(seconds=1),
    )
    env8_req = _create_sample(
        state_file,
        target="environment:8",
        target_name="QA",
        now=BASE_TIME + timedelta(seconds=2),
    )
    latest_req = _create_sample(
        state_file, target="latest", now=BASE_TIME + timedelta(seconds=3)
    )

    # Creating a new request for environment:7 cancels ONLY the previous environment:7 request.
    env7_req_2 = _create_sample(
        state_file,
        target="environment:7",
        target_name="Staging",
        version_id="8",
        now=BASE_TIME + timedelta(seconds=4),
    )
    assert get_request(env7_req["approval_id"], path=state_file)["status"] == STATUS_CANCELLED
    assert get_request(live_req["approval_id"], path=state_file)["status"] == STATUS_PENDING
    assert get_request(env8_req["approval_id"], path=state_file)["status"] == STATUS_PENDING
    assert get_request(latest_req["approval_id"], path=state_file)["status"] == STATUS_PENDING
    assert get_request(env7_req_2["approval_id"], path=state_file)["status"] == STATUS_PENDING

    for req in (live_req, env7_req_2, env8_req, latest_req):
        approve_request(
            req["approval_id"],
            typed_public_id="GTM-ABC123",
            now=BASE_TIME + timedelta(seconds=5),
            path=state_file,
        )

    # target keyword argument is required.
    with pytest.raises(TypeError):
        consume_approval(  # type: ignore[call-arg]
            live_req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="7",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=6),
            path=state_file,
        )

    # Cross-target consumption is refused.
    with pytest.raises(PublishApprovalError, match="target"):
        consume_approval(
            live_req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="7",
            target="environment:7",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=6),
            path=state_file,
        )
    with pytest.raises(PublishApprovalError, match="target"):
        consume_approval(
            env7_req_2["approval_id"],
            account_id="100",
            container_id="200",
            version_id="8",
            target="environment:8",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=6),
            path=state_file,
        )
    with pytest.raises(PublishApprovalError, match="target"):
        consume_approval(
            latest_req["approval_id"],
            account_id="100",
            container_id="200",
            version_id="7",
            target="live",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=6),
            path=state_file,
        )

    # Matching target succeeds.
    consumed = consume_approval(
        env7_req_2["approval_id"],
        account_id="100",
        container_id="200",
        version_id="8",
        target="environment:7",
        current_live_version_id="6",
        now=BASE_TIME + timedelta(seconds=6),
        path=state_file,
    )
    assert consumed["status"] == STATUS_USED
    assert consumed["target"] == "environment:7"


def test_legacy_request_without_target_field_is_treated_as_live(tmp_path: Path) -> None:
    state_file = tmp_path / "legacy-approvals.json"
    state_file.write_text(
        json.dumps(
            {
                "version": 1,
                "requests": {
                    "legacy1234567890": {
                        "approval_id": "legacy1234567890",
                        "account_id": "100",
                        "container_id": "200",
                        "public_id": "GTM-ABC123",
                        "container_name": "Web Store",
                        "version_id": "7",
                        "version_name": "v7",
                        "live_version_id": "6",
                        "diff_summary": {},
                        "created_at": BASE_TIME.isoformat(),
                        "expires_at": (BASE_TIME + timedelta(seconds=600)).isoformat(),
                        "status": STATUS_APPROVED,
                        "approved_at": (BASE_TIME + timedelta(seconds=1)).isoformat(),
                        "used_at": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PublishApprovalError, match="target"):
        consume_approval(
            "legacy1234567890",
            account_id="100",
            container_id="200",
            version_id="7",
            target="latest",
            current_live_version_id="6",
            now=BASE_TIME + timedelta(seconds=10),
            path=state_file,
        )

    consumed = consume_approval(
        "legacy1234567890",
        account_id="100",
        container_id="200",
        version_id="7",
        target="live",
        current_live_version_id="6",
        now=BASE_TIME + timedelta(seconds=10),
        path=state_file,
    )
    assert consumed["status"] == STATUS_USED
