from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from googleapiclient.errors import HttpError

from tag_manager_mcp import publish_guard, server
from tag_manager_mcp.access import AccessDeniedError
from tag_manager_mcp.client import GTMError

SERVER_SOURCE_PATH = Path(server.__file__).resolve()


class FakePublishReader:
    def __init__(self) -> None:
        self.live_payload: dict[str, Any] | None = {
            "containerVersionId": "10",
            "name": "Live 10",
            "container": {"publicId": "GTM-TEST123", "name": "Prod Web"},
            "tag": [
                {"tagId": "1", "name": "GA4 Config", "type": "googtag"},
                {"tagId": "2", "name": "Legacy Pixel", "type": "html"},
            ],
            "trigger": [{"triggerId": "1", "name": "All Pages", "type": "pageview"}],
        }
        self.live_error: Exception | None = None
        self.versions_by_id: dict[str, dict[str, Any]] = {
            "11": {
                "containerVersionId": "11",
                "name": "Release 11",
                "container": {"publicId": "GTM-TEST123", "name": "Prod Web"},
                "tag": [
                    {"tagId": "1", "name": "GA4 Config", "type": "googtag", "paused": True},
                    {"tagId": "3", "name": "Lead Event", "type": "gaawe"},
                ],
                "trigger": [{"triggerId": "1", "name": "All Pages", "type": "pageview"}],
            }
        }

    def live_version(self, account_id: str, container_id: str) -> dict[str, Any]:
        if self.live_error is not None:
            raise self.live_error
        assert self.live_payload is not None
        return self.live_payload

    def get_version(self, account_id: str, container_id: str, version_id: str) -> dict[str, Any]:
        return self.versions_by_id[version_id]

    def latest_version_header(self, account_id: str, container_id: str) -> dict[str, Any]:
        if hasattr(self, "latest_error") and self.latest_error is not None:
            raise self.latest_error
        return getattr(self, "latest_header_payload", {"containerVersionId": "10"})


class FakePublishWriter:
    def __init__(self) -> None:
        self.environments: dict[str, dict[str, Any]] = {
            "7": {
                "environmentId": "7",
                "name": "Staging",
                "description": "QA env",
                "url": "https://staging.example",
                "enableDebug": True,
                "containerVersionId": "10",
                "fingerprint": "fp-env-7",
                "authorizationCode": "secret-env-7",
            },
            "8": {
                "environmentId": "8",
                "name": "Preview",
                "containerVersionId": "10",
                "fingerprint": "fp-env-8",
                "authorizationCode": "secret-env-8",
            },
        }
        self.update_environment_calls: list[tuple[Any, ...]] = []
        self.create_environment_calls: list[tuple[Any, ...]] = []
        self.reauthorize_environment_calls: list[tuple[Any, ...]] = []
        self.set_latest_calls: list[tuple[str, str, str]] = []

    def get_environment(
        self, account_id: str, container_id: str, environment_id: str
    ) -> dict[str, Any]:
        return dict(self.environments[environment_id])

    def create_environment(
        self, account_id: str, container_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.create_environment_calls.append((account_id, container_id, dict(body)))
        return dict(body)

    def update_environment(
        self,
        account_id: str,
        container_id: str,
        environment_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        self.update_environment_calls.append(
            (account_id, container_id, environment_id, dict(body), fingerprint)
        )
        updated = {**self.environments[environment_id], **body}
        self.environments[environment_id] = updated
        return dict(updated)

    def reauthorize_environment(
        self,
        account_id: str,
        container_id: str,
        environment_id: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.reauthorize_environment_calls.append(
            (account_id, container_id, environment_id, body)
        )
        return {
            **self.environments[environment_id],
            "authorizationCode": "rotated-secret-code",
        }

    def set_latest_version(
        self, account_id: str, container_id: str, version_id: str
    ) -> dict[str, Any]:
        self.set_latest_calls.append((account_id, container_id, version_id))
        return {"containerVersionId": version_id}


class FakePublishClient:
    def __init__(self) -> None:
        self.publish_calls: list[tuple[str, str, str]] = []
        self.container_payload: dict[str, Any] = {
            "accountId": "1",
            "containerId": "100",
            "publicId": "GTM-FALLBACK",
            "name": "Fallback Web",
        }

    def get_container(self, account_id: str, container_id: str) -> dict[str, Any]:
        return self.container_payload

    def publish_version(
        self, account_id: str, container_id: str, version_id: str
    ) -> dict[str, Any]:
        self.publish_calls.append((account_id, container_id, version_id))
        return {
            "compilerError": False,
            "containerVersion": {
                "path": f"accounts/{account_id}/containers/{container_id}/versions/{version_id}",
                "containerVersionId": version_id,
            },
        }


def test_invariant_i1_single_publish_version_call_site_in_server_source() -> None:
    """Invariant I1: `.publish_version(` occurs exactly once in server.py outside def line."""
    source = SERVER_SOURCE_PATH.read_text(encoding="utf-8")
    non_def_lines = [
        line for line in source.splitlines() if not line.lstrip().startswith("def publish_version(")
    ]
    occurrences = sum(line.count(".publish_version(") for line in non_def_lines)
    assert occurrences == 1

    # Verify consume_approval appears before the single .publish_version( call.
    func_start = source.index("def publish_version(")
    func_body = source[func_start:]
    consume_pos = func_body.index("consume_approval(")
    publish_pos = func_body.index(".publish_version(")
    assert consume_pos < publish_pos


def test_invariant_i2_no_approve_request_in_server_source() -> None:
    """Invariant I2: `approve_request` must not appear anywhere in server.py."""
    source = SERVER_SOURCE_PATH.read_text(encoding="utf-8")
    assert "approve_request" not in source


def test_prepare_publish_creates_pending_request_with_diff_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    approval_file = tmp_path / "approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(approval_file))
    reader = FakePublishReader()
    client = FakePublishClient()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)

    prepared = server.prepare_publish("1", "100", "11")
    approval_id = prepared["approval_id"]

    assert prepared["public_id"] == "GTM-TEST123"
    assert prepared["version_id"] == "11"
    assert prepared["version_name"] == "Release 11"
    assert prepared["replaces_live_version_id"] == "10"
    assert prepared["expires_at"]
    assert f"tag-manager-approve {approval_id}" in prepared["next_step"]
    assert prepared["summary"]["total_changes"] == 3
    assert prepared["summary"]["tag"] == {
        "added": ["Lead Event"],
        "added_count": 1,
        "removed": ["Legacy Pixel"],
        "removed_count": 1,
        "changed": ["GA4 Config"],
        "changed_count": 1,
    }
    assert "trigger" not in prepared["summary"]

    listed = server.list_publish_requests()
    assert len(listed) == 1
    assert listed[0]["approval_id"] == approval_id
    assert listed[0]["status"] == "pending"
    assert client.publish_calls == []


def test_prepare_publish_handles_never_published_container_and_fallback_public_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    approval_file = tmp_path / "approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(approval_file))

    reader = FakePublishReader()
    reader.live_error = GTMError("Google Tag Manager API error: Published version not found")
    reader.versions_by_id["1"] = {
        "containerVersionId": "1",
        "name": "Initial Release",
        "tag": [{"tagId": "1", "name": "First Tag", "type": "html"}],
    }
    client = FakePublishClient()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)

    prepared = server.prepare_publish("1", "100", "1")
    assert prepared["replaces_live_version_id"] == ""
    assert prepared["public_id"] == "GTM-FALLBACK"
    assert "never been published" in prepared["summary"]["note"].lower()
    assert prepared["summary"]["tag"]["added"] == ["First Tag"]

    # Out-of-band terminal approval and publish when still never published succeeds.
    publish_guard.approve_request(
        prepared["approval_id"], typed_public_id="GTM-FALLBACK", path=approval_file
    )
    published = server.publish_version("1", "100", "1", prepared["approval_id"], confirm=True)
    assert published["ok"] is True
    assert client.publish_calls == [("1", "100", "1")]


def test_invariant_i3_publish_version_refusals_and_single_use_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant I3: invalid/unapproved/mismatched/stale/reused cases refuse without publishing."""
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    approval_file = tmp_path / "approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(approval_file))

    reader = FakePublishReader()
    client = FakePublishClient()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)

    # 1. No approval id
    no_id_omitted = server.publish_version("1", "100", "11", confirm=True)
    assert no_id_omitted["ok"] is False
    no_id_empty = server.publish_version("1", "100", "11", "", confirm=True)
    assert no_id_empty["ok"] is False
    assert client.publish_calls == []

    # 2. Unknown approval id
    unknown = server.publish_version("1", "100", "11", "deadbeef00000000", confirm=True)
    assert unknown["ok"] is False
    assert client.publish_calls == []

    # 3. Pending (unapproved) request
    prepared = server.prepare_publish("1", "100", "11")
    approval_id = prepared["approval_id"]
    pending_res = server.publish_version("1", "100", "11", approval_id, confirm=True)
    assert pending_res["ok"] is False
    assert "not been approved" in pending_res["message"]
    assert client.publish_calls == []

    # Approve out-of-band (simulating `tag-manager-approve`)
    publish_guard.approve_request(approval_id, typed_public_id="GTM-TEST123", path=approval_file)

    # 4. Approved request for another version / container / account
    wrong_version = server.publish_version("1", "100", "12", approval_id, confirm=True)
    assert wrong_version["ok"] is False
    assert "version_id" in wrong_version["message"]
    wrong_container = server.publish_version("1", "999", "11", approval_id, confirm=True)
    assert wrong_container["ok"] is False
    assert client.publish_calls == []

    # 5. Approved, but live version changed since prepare_publish
    reader.live_payload = {
        "containerVersionId": "10b",
        "name": "Hotfix",
        "container": {"publicId": "GTM-TEST123"},
    }
    stale_live = server.publish_version("1", "100", "11", approval_id, confirm=True)
    assert stale_live["ok"] is False
    assert "Live container version changed" in stale_live["message"]
    assert client.publish_calls == []

    # Restore original live version -> matching approved request succeeds once
    reader.live_payload = {
        "containerVersionId": "10",
        "name": "Live 10",
        "container": {"publicId": "GTM-TEST123"},
    }
    ok_res = server.publish_version("1", "100", "11", approval_id, confirm=True)
    assert ok_res["ok"] is True
    assert client.publish_calls == [("1", "100", "11")]

    # 6. Second call with the same already-used approval id is refused
    replay = server.publish_version("1", "100", "11", approval_id, confirm=True)
    assert replay["ok"] is False
    assert "already been used" in replay["message"]
    assert client.publish_calls == [("1", "100", "11")]
    assert server.list_publish_requests() == []


def test_prepare_and_list_publish_require_publish_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    with pytest.raises(AccessDeniedError):
        server.prepare_publish("1", "100", "11")
    with pytest.raises(AccessDeniedError):
        server.list_publish_requests()
    with pytest.raises(AccessDeniedError):
        server.publish_version("1", "100", "11", "abc", confirm=True)
    with pytest.raises(AccessDeniedError):
        server.publish_to_environment("1", "100", "7", "11", "abc", confirm=True)
    with pytest.raises(AccessDeniedError):
        server.set_latest_version("1", "100", "11", "abc", confirm=True)


def test_prepare_publish_environment_and_latest_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    with pytest.raises(ValueError, match="mutually exclusive"):
        server.prepare_publish("1", "100", "11", environment_id="7", latest=True)


def test_publish_to_environment_and_set_latest_version_flows_and_target_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    approval_file = tmp_path / "approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(approval_file))

    reader = FakePublishReader()
    reader.versions_by_id["10"] = dict(reader.live_payload or {})
    client = FakePublishClient()
    writer = FakePublishWriter()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    # Prepare one approval for each target: live, environment:7, latest.
    live_prep = server.prepare_publish("1", "100", "11")
    env7_prep = server.prepare_publish("1", "100", "11", environment_id="7")
    latest_prep = server.prepare_publish("1", "100", "11", latest=True)

    assert live_prep["target"] == "live"
    assert env7_prep["target"] == "environment:7"
    assert env7_prep["target_name"] == "Staging"
    assert env7_prep["replaces_live_version_id"] == "10"
    assert env7_prep["summary"]["total_changes"] == 3
    assert latest_prep["target"] == "latest"
    assert latest_prep["replaces_live_version_id"] == "10"

    # All three stay open because cancellation is per account+container+target.
    for prep in (live_prep, env7_prep, latest_prep):
        publish_guard.approve_request(
            prep["approval_id"], typed_public_id="GTM-TEST123", path=approval_file
        )

    # Live approval cannot be consumed by publish_to_environment or set_latest_version.
    assert (
        server.publish_to_environment(
            "1", "100", "7", "11", live_prep["approval_id"], confirm=True
        )["ok"]
        is False
    )
    assert (
        server.set_latest_version("1", "100", "11", live_prep["approval_id"], confirm=True)["ok"]
        is False
    )
    # Environment 7 approval cannot be consumed by environment 8, live, or latest.
    assert (
        server.publish_to_environment(
            "1", "100", "8", "11", env7_prep["approval_id"], confirm=True
        )["ok"]
        is False
    )
    assert (
        server.publish_version("1", "100", "11", env7_prep["approval_id"], confirm=True)["ok"]
        is False
    )
    assert (
        server.set_latest_version("1", "100", "11", env7_prep["approval_id"], confirm=True)["ok"]
        is False
    )
    # Latest approval cannot be consumed by live or publish_to_environment.
    assert (
        server.publish_version("1", "100", "11", latest_prep["approval_id"], confirm=True)["ok"]
        is False
    )
    assert (
        server.publish_to_environment(
            "1", "100", "7", "11", latest_prep["approval_id"], confirm=True
        )["ok"]
        is False
    )
    assert client.publish_calls == []
    assert writer.update_environment_calls == []
    assert writer.set_latest_calls == []

    # Stale environment version check refuses without updating.
    writer.environments["7"]["containerVersionId"] = "99"
    stale_env = server.publish_to_environment(
        "1", "100", "7", "11", env7_prep["approval_id"], confirm=True
    )
    assert stale_env["ok"] is False
    assert writer.update_environment_calls == []
    writer.environments["7"]["containerVersionId"] = "10"

    # Matching publish_to_environment succeeds, sends minimal fields + fingerprint, redacts auth.
    env_res = server.publish_to_environment(
        "1", "100", "7", "11", env7_prep["approval_id"], confirm=True
    )
    assert env_res["ok"] is True
    assert env_res["result"]["authorizationCode"] == "[redacted]"
    assert writer.update_environment_calls == [
        (
            "1",
            "100",
            "7",
            {
                "name": "Staging",
                "description": "QA env",
                "enableDebug": True,
                "url": "https://staging.example",
                "containerVersionId": "11",
            },
            "fp-env-7",
        )
    ]

    # Matching set_latest_version succeeds once and cannot be replayed.
    latest_res = server.set_latest_version(
        "1", "100", "11", latest_prep["approval_id"], confirm=True
    )
    assert latest_res == {"ok": True, "result": {"containerVersionId": "11"}}
    assert writer.set_latest_calls == [("1", "100", "11")]
    assert (
        server.set_latest_version("1", "100", "11", latest_prep["approval_id"], confirm=True)["ok"]
        is False
    )


def test_invariant_write_mode_cannot_change_served_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In write mode, no registered tool can set containerVersionId or call set_latest_version."""
    write_srv = server.build_server("write")
    write_tool_names = {t.name for t in asyncio.run(write_srv.list_tools())}
    assert "set_latest_version" not in write_tool_names
    assert "publish_to_environment" not in write_tool_names
    assert "publish_version" not in write_tool_names

    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    writer = FakePublishWriter()
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    res_create = server.create_environment(
        "1", "100", {"name": "Staging", "containerVersionId": "11"}
    )
    res_update = server.update_environment(
        "1", "100", "7", {"name": "Staging", "containerVersionId": "11"}
    )
    assert res_create["ok"] is False
    assert res_update["ok"] is False
    assert writer.create_environment_calls == []
    assert writer.update_environment_calls == []

    # Metadata-only environment update still works in write mode.
    ok_update = server.update_environment("1", "100", "7", {"name": "Staging Renamed"})
    assert ok_update["name"] == "Staging Renamed"
    assert all("containerVersionId" not in call[3] for call in writer.update_environment_calls)


@pytest.mark.parametrize(
    ("bad_body", "expected_key"),
    [
        ({"containerVersionId": "9"}, "containerVersionId"),
        ({"container_version_id": "9"}, "container_version_id"),
        ({"workspaceId": "3"}, "workspaceId"),
        ({"workspace_id": "3"}, "workspace_id"),
        ({"name": "x", "authorizationCode": "z"}, "authorizationCode"),
        ({"name": "x", "enable_debug": True}, "enable_debug"),
    ],
)
def test_invariant_environment_allow_list_rejects_disallowed_keys(
    bad_body: dict[str, Any],
    expected_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    writer = FakePublishWriter()
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    create_res = server.create_environment("1", "100", bad_body)
    update_res = server.update_environment("1", "100", "7", bad_body)

    assert create_res["ok"] is False
    assert expected_key in create_res["message"]
    assert update_res["ok"] is False
    assert expected_key in update_res["message"]
    assert writer.create_environment_calls == []
    assert writer.update_environment_calls == []


def test_invariant_environment_allow_list_accepts_allowed_camel_case_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    writer = FakePublishWriter()
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    allowed_body = {
        "name": "x",
        "description": "d",
        "url": "https://e",
        "enableDebug": True,
    }
    created = server.create_environment("1", "100", allowed_body)
    updated = server.update_environment("1", "100", "7", allowed_body, fingerprint="fp-env-7")

    assert created["name"] == "x"
    assert updated["name"] == "x"
    assert writer.create_environment_calls == [("1", "100", allowed_body)]
    assert writer.update_environment_calls == [("1", "100", "7", allowed_body, "fp-env-7")]


def test_invariant_reauthorize_environment_has_no_body_param_and_sends_no_version_or_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    params = list(inspect.signature(server.reauthorize_environment).parameters)
    assert "body" not in params
    assert params == ["account_id", "container_id", "environment_id"]

    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    writer = FakePublishWriter()
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    result = server.reauthorize_environment("1", "100", "7")
    assert result["authorizationCode"] == "rotated-secret-code"
    assert writer.reauthorize_environment_calls == [("1", "100", "7", None)]
    for _, _, _, body in writer.reauthorize_environment_calls:
        assert body is None or (
            "containerVersionId" not in body
            and "container_version_id" not in body
            and "workspaceId" not in body
            and "workspace_id" not in body
        )


def test_is_no_live_version_error_prefers_http_status_over_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _http_err(status: int, body: bytes) -> HttpError:
        return HttpError(SimpleNamespace(status=status, reason="err"), body)

    err_404 = _http_err(404, b'{"error": {"message": "Not found"}}')
    err_403_not_found = _http_err(403, b'{"error": {"message": "Resource not found"}}')
    err_500_with_404 = _http_err(500, b'{"error": {"message": "Internal error 404"}}')

    assert server._is_no_live_version_error(err_404) is True
    assert server._is_no_live_version_error(err_403_not_found) is False
    assert server._is_no_live_version_error(err_500_with_404) is False

    wrapped_404 = GTMError("API error")
    wrapped_404.__cause__ = err_404
    wrapped_403 = GTMError("Resource not found")
    wrapped_403.__cause__ = err_403_not_found
    assert server._is_no_live_version_error(wrapped_404) is True
    assert server._is_no_live_version_error(wrapped_403) is False

    # Fallback to text matching only when no HTTP status is available.
    assert server._is_no_live_version_error(GTMError("Published version not found")) is True
    assert server._is_no_live_version_error(GTMError("Permission denied")) is False

    # _fetch_live_version_optional re-raises 403 even if its text mentions "not found".
    reader = FakePublishReader()
    reader.live_error = wrapped_403
    with pytest.raises(GTMError):
        server._fetch_live_version_optional(reader, "1", "100")
