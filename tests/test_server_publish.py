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
                "type": "user",
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
                "type": "user",
                "name": "Preview",
                "containerVersionId": "10",
                "fingerprint": "fp-env-8",
                "authorizationCode": "secret-env-8",
            },
        }
        self.update_environment_calls: list[tuple[Any, ...]] = []
        self.delete_environment_calls: list[tuple[str, str, str]] = []
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

    def delete_environment(
        self, account_id: str, container_id: str, environment_id: str
    ) -> dict[str, Any]:
        self.delete_environment_calls.append((account_id, container_id, environment_id))
        return {"deleted": environment_id}

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
        line
        for line in source.splitlines()
        if not line.lstrip().startswith(("def publish_version(", "async def publish_version("))
    ]
    occurrences = sum(line.count(".publish_version(") for line in non_def_lines)
    assert occurrences == 1

    # Verify consume_approval appears before the single .publish_version( call.
    func_start = source.index("async def publish_version(")
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
    assert prepared["next_step"] == (
        "Call publish_version (or publish_to_environment / set_latest_version) with this "
        "approval_id; your MCP client will ask you to type the container ID and then click "
        "to confirm."
    )
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
    published = asyncio.run(
        server.publish_version("1", "100", "1", prepared["approval_id"], confirm=True)
    )
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
    no_id_omitted = asyncio.run(server.publish_version("1", "100", "11", confirm=True))
    assert no_id_omitted["ok"] is False
    no_id_empty = asyncio.run(server.publish_version("1", "100", "11", "", confirm=True))
    assert no_id_empty["ok"] is False
    assert client.publish_calls == []

    # 2. Unknown approval id
    unknown = asyncio.run(
        server.publish_version("1", "100", "11", "deadbeef00000000", confirm=True)
    )
    assert unknown["ok"] is False
    assert client.publish_calls == []

    # 3. Pending (unapproved) request without elicitation capability
    prepared = server.prepare_publish("1", "100", "11")
    approval_id = prepared["approval_id"]
    pending_res = asyncio.run(
        server.publish_version("1", "100", "11", approval_id, confirm=True)
    )
    assert pending_res["ok"] is False
    assert "cannot show approval prompts" in pending_res["message"]
    assert client.publish_calls == []

    # Approve out-of-band (simulating `tag-manager-approve`)
    publish_guard.approve_request(approval_id, typed_public_id="GTM-TEST123", path=approval_file)

    # 4. Approved request for another version / container / account
    wrong_version = asyncio.run(
        server.publish_version("1", "100", "12", approval_id, confirm=True)
    )
    assert wrong_version["ok"] is False
    assert "version_id" in wrong_version["message"]
    wrong_container = asyncio.run(
        server.publish_version("1", "999", "11", approval_id, confirm=True)
    )
    assert wrong_container["ok"] is False
    assert client.publish_calls == []

    # 5. Approved, but live version changed since prepare_publish
    reader.live_payload = {
        "containerVersionId": "10b",
        "name": "Hotfix",
        "container": {"publicId": "GTM-TEST123"},
    }
    stale_live = asyncio.run(
        server.publish_version("1", "100", "11", approval_id, confirm=True)
    )
    assert stale_live["ok"] is False
    assert "Live container version changed" in stale_live["message"]
    assert client.publish_calls == []

    # The approval refused at consume time is cancelled and no longer listed
    assert publish_guard.get_request(approval_id, path=approval_file)["status"] == "cancelled"
    assert server.list_publish_requests() == []

    # Restore original live version -> a fresh, approved request succeeds once
    reader.live_payload = {
        "containerVersionId": "10",
        "name": "Live 10",
        "container": {"publicId": "GTM-TEST123"},
    }
    approval_id = server.prepare_publish("1", "100", "11")["approval_id"]
    publish_guard.approve_request(approval_id, typed_public_id="GTM-TEST123", path=approval_file)
    ok_res = asyncio.run(server.publish_version("1", "100", "11", approval_id, confirm=True))
    assert ok_res["ok"] is True
    assert client.publish_calls == [("1", "100", "11")]

    # 6. Second call with the same already-used approval id is refused
    replay = asyncio.run(server.publish_version("1", "100", "11", approval_id, confirm=True))
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
        asyncio.run(server.publish_version("1", "100", "11", "abc", confirm=True))
    with pytest.raises(AccessDeniedError):
        asyncio.run(server.publish_to_environment("1", "100", "7", "11", "abc", confirm=True))
    with pytest.raises(AccessDeniedError):
        asyncio.run(server.set_latest_version("1", "100", "11", "abc", confirm=True))


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
        asyncio.run(
            server.publish_to_environment(
                "1", "100", "7", "11", live_prep["approval_id"], confirm=True
            )
        )["ok"]
        is False
    )
    assert (
        asyncio.run(
            server.set_latest_version("1", "100", "11", live_prep["approval_id"], confirm=True)
        )["ok"]
        is False
    )
    # Environment 7 approval cannot be consumed by environment 8, live, or latest.
    assert (
        asyncio.run(
            server.publish_to_environment(
                "1", "100", "8", "11", env7_prep["approval_id"], confirm=True
            )
        )["ok"]
        is False
    )
    assert (
        asyncio.run(
            server.publish_version("1", "100", "11", env7_prep["approval_id"], confirm=True)
        )["ok"]
        is False
    )
    assert (
        asyncio.run(
            server.set_latest_version("1", "100", "11", env7_prep["approval_id"], confirm=True)
        )["ok"]
        is False
    )
    # Latest approval cannot be consumed by live or publish_to_environment.
    assert (
        asyncio.run(
            server.publish_version("1", "100", "11", latest_prep["approval_id"], confirm=True)
        )["ok"]
        is False
    )
    assert (
        asyncio.run(
            server.publish_to_environment(
                "1", "100", "7", "11", latest_prep["approval_id"], confirm=True
            )
        )["ok"]
        is False
    )
    assert client.publish_calls == []
    assert writer.update_environment_calls == []
    assert writer.set_latest_calls == []

    # Stale environment version check refuses without updating.
    writer.environments["7"]["containerVersionId"] = "99"
    stale_env = asyncio.run(
        server.publish_to_environment(
            "1", "100", "7", "11", env7_prep["approval_id"], confirm=True
        )
    )
    assert stale_env["ok"] is False
    assert writer.update_environment_calls == []
    assert (
        publish_guard.get_request(env7_prep["approval_id"], path=approval_file)["status"]
        == "cancelled"
    )
    writer.environments["7"]["containerVersionId"] = "10"
    env7_prep = server.prepare_publish("1", "100", "11", environment_id="7")
    publish_guard.approve_request(
        env7_prep["approval_id"], typed_public_id="GTM-TEST123", path=approval_file
    )

    # Matching publish_to_environment succeeds, sends full resource (minus auth code) + fingerprint.
    env_res = asyncio.run(
        server.publish_to_environment(
            "1", "100", "7", "11", env7_prep["approval_id"], confirm=True
        )
    )
    assert env_res["ok"] is True
    assert env_res["result"]["authorizationCode"] == "[redacted]"
    assert writer.update_environment_calls == [
        (
            "1",
            "100",
            "7",
            {
                "environmentId": "7",
                "type": "user",
                "name": "Staging",
                "description": "QA env",
                "enableDebug": True,
                "url": "https://staging.example",
                "containerVersionId": "11",
                "fingerprint": "fp-env-7",
            },
            "fp-env-7",
        )
    ]

    # Matching set_latest_version succeeds once and cannot be replayed.
    latest_res = asyncio.run(
        server.set_latest_version("1", "100", "11", latest_prep["approval_id"], confirm=True)
    )
    assert latest_res == {"ok": True, "result": {"containerVersionId": "11"}}
    assert writer.set_latest_calls == [("1", "100", "11")]
    assert (
        asyncio.run(
            server.set_latest_version("1", "100", "11", latest_prep["approval_id"], confirm=True)
        )["ok"]
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

    # Metadata-only environment update preserves served containerVersionId and omits auth code.
    ok_update = server.update_environment("1", "100", "7", {"name": "Staging Renamed"})
    assert ok_update["name"] == "Staging Renamed"
    assert len(writer.update_environment_calls) == 1
    sent_body = writer.update_environment_calls[0][3]
    assert sent_body["containerVersionId"] == "10"
    assert "authorizationCode" not in sent_body


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
    assert writer.update_environment_calls == [
        (
            "1",
            "100",
            "7",
            {
                "environmentId": "7",
                "type": "user",
                "containerVersionId": "10",
                "fingerprint": "fp-env-7",
                **allowed_body,
            },
            "fp-env-7",
        )
    ]


def test_update_environment_preserves_served_version_and_workspace_and_omits_auth_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    writer = FakePublishWriter()
    writer.environments["12"] = {
        "path": "accounts/1/containers/100/environments/12",
        "accountId": "1",
        "containerId": "100",
        "environmentId": "12",
        "type": "user",
        "name": "Staging",
        "description": "QA env",
        "url": "https://staging.example",
        "enableDebug": True,
        "containerVersionId": "12",
        "fingerprint": "fp-12",
        "authorizationCode": "secret-12",
        "authorizationTimestamp": "2026-01-01T00:00:00Z",
        "tagManagerUrl": "https://tagmanager.google.com/#/env/12",
    }
    writer.environments["13"] = {
        "path": "accounts/1/containers/100/environments/13",
        "accountId": "1",
        "containerId": "100",
        "environmentId": "13",
        "type": "user",
        "name": "Workspace Preview",
        "url": "https://preview.example",
        "enableDebug": False,
        "workspaceId": "3",
        "fingerprint": "fp-ws-3",
        "authorizationCode": "secret-ws-3",
    }
    writer.environments["14"] = {
        "environmentId": "14",
        "type": "user",
        "name": "Empty",
        "fingerprint": "fp-14",
        "authorizationCode": "secret-14",
    }
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    # 1. Environment serving containerVersionId "12" keeps it and uses current fingerprint.
    res_ver = server.update_environment("1", "100", "12", {"name": "New"})
    assert res_ver["name"] == "New"
    assert res_ver["authorizationCode"] == "[redacted]"
    _, _, env_id, sent_ver_body, sent_ver_fp = writer.update_environment_calls[-1]
    assert env_id == "12"
    assert sent_ver_fp == "fp-12"
    assert sent_ver_body["containerVersionId"] == "12"
    assert "workspaceId" not in sent_ver_body
    assert sent_ver_body["name"] == "New"
    assert sent_ver_body["type"] == "user"
    assert sent_ver_body["description"] == "QA env"
    assert sent_ver_body["url"] == "https://staging.example"
    assert sent_ver_body["enableDebug"] is True
    assert "authorizationCode" not in sent_ver_body

    # 2. Environment linked to workspaceId "3" keeps it and uses current fingerprint.
    res_ws = server.update_environment("1", "100", "13", {"name": "New"})
    assert res_ws["name"] == "New"
    assert res_ws["authorizationCode"] == "[redacted]"
    _, _, env_id, sent_ws_body, sent_ws_fp = writer.update_environment_calls[-1]
    assert env_id == "13"
    assert sent_ws_fp == "fp-ws-3"
    assert sent_ws_body["workspaceId"] == "3"
    assert "containerVersionId" not in sent_ws_body
    assert sent_ws_body["name"] == "New"
    assert sent_ws_body["type"] == "user"
    assert "authorizationCode" not in sent_ws_body

    # 3. Environment with neither containerVersionId nor workspaceId keeps both absent.
    server.update_environment("1", "100", "14", {"name": "Still Empty"})
    _, _, _, sent_empty_body, sent_empty_fp = writer.update_environment_calls[-1]
    assert sent_empty_fp == "fp-14"
    assert "containerVersionId" not in sent_empty_body
    assert "workspaceId" not in sent_empty_body
    assert "authorizationCode" not in sent_empty_body

    # 4. Caller-supplied fingerprint takes precedence over current resource fingerprint.
    server.update_environment("1", "100", "12", {"name": "Renamed"}, fingerprint="fp-caller")
    _, _, _, sent_fp_body, sent_caller_fp = writer.update_environment_calls[-1]
    assert sent_caller_fp == "fp-caller"
    assert sent_fp_body["containerVersionId"] == "12"
    assert "authorizationCode" not in sent_fp_body


def test_publish_to_environment_sends_full_current_resource_with_only_version_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    approval_file = tmp_path / "approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(approval_file))

    reader = FakePublishReader()
    reader.versions_by_id["10"] = dict(reader.live_payload or {})
    client = FakePublishClient()
    writer = FakePublishWriter()
    writer.environments["7"] = {
        "path": "accounts/1/containers/100/environments/7",
        "accountId": "1",
        "containerId": "100",
        "environmentId": "7",
        "type": "user",
        "name": "Staging",
        "description": "QA env",
        "url": "https://staging.example",
        "enableDebug": True,
        "containerVersionId": "10",
        "fingerprint": "fp-env-7",
        "authorizationCode": "secret-env-7",
        "authorizationTimestamp": "2026-01-01T00:00:00Z",
        "tagManagerUrl": "https://tagmanager.google.com/#/env/7",
    }
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    prep = server.prepare_publish("1", "100", "11", environment_id="7")
    publish_guard.approve_request(
        prep["approval_id"], typed_public_id="GTM-TEST123", path=approval_file
    )
    res = asyncio.run(
        server.publish_to_environment("1", "100", "7", "11", prep["approval_id"], confirm=True)
    )
    assert res["ok"] is True
    assert res["result"]["authorizationCode"] == "[redacted]"
    assert writer.update_environment_calls == [
        (
            "1",
            "100",
            "7",
            {
                "path": "accounts/1/containers/100/environments/7",
                "accountId": "1",
                "containerId": "100",
                "environmentId": "7",
                "type": "user",
                "name": "Staging",
                "description": "QA env",
                "url": "https://staging.example",
                "enableDebug": True,
                "containerVersionId": "11",
                "fingerprint": "fp-env-7",
                "authorizationTimestamp": "2026-01-01T00:00:00Z",
                "tagManagerUrl": "https://tagmanager.google.com/#/env/7",
            },
            "fp-env-7",
        )
    ]
    assert "authorizationCode" not in writer.update_environment_calls[0][3]


@pytest.mark.parametrize("env_type", ["live", "latest", "workspace", "other"])
def test_non_user_environments_are_refused_for_update_publish_prepare_and_delete(
    env_type: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    approval_file = tmp_path / "approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(approval_file))

    reader = FakePublishReader()
    reader.versions_by_id["10"] = dict(reader.live_payload or {})
    client = FakePublishClient()
    writer = FakePublishWriter()
    writer.environments["20"] = {
        "environmentId": "20",
        "type": env_type,
        "name": "Managed Env",
        "containerVersionId": "10",
        "fingerprint": "fp-20",
        "authorizationCode": "secret-20",
    }

    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    expected_msg = (
        f"Environment 20 is of type '{env_type}'; "
        "only user-created environments can be changed here."
    )

    update_res = server.update_environment("1", "100", "20", {"name": "Renamed"})
    assert update_res == {"ok": False, "message": expected_msg}

    prep_res = server.prepare_publish("1", "100", "11", environment_id="20")
    assert prep_res == {"ok": False, "message": expected_msg}
    assert publish_guard.list_requests(include_closed=True, path=approval_file) == []

    publish_res = asyncio.run(
        server.publish_to_environment("1", "100", "20", "11", "some-approval-id", confirm=True)
    )
    assert publish_res == {"ok": False, "message": expected_msg}

    delete_res = server.delete_environment("1", "100", "20", confirm=True)
    assert delete_res == {"ok": False, "message": expected_msg}

    assert writer.update_environment_calls == []
    assert writer.delete_environment_calls == []


@pytest.mark.parametrize("env_type", ["absent", None, "", "user"])
def test_user_and_untyped_environments_are_allowed_for_update_publish_prepare_and_delete(
    env_type: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    approval_file = tmp_path / "approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(approval_file))

    reader = FakePublishReader()
    reader.versions_by_id["10"] = dict(reader.live_payload or {})
    client = FakePublishClient()
    writer = FakePublishWriter()
    env_payload: dict[str, Any] = {
        "environmentId": "20",
        "name": "User Env",
        "containerVersionId": "10",
        "fingerprint": "fp-20",
        "authorizationCode": "secret-20",
    }
    if env_type != "absent":
        env_payload["type"] = env_type
    writer.environments["20"] = env_payload

    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    update_res = server.update_environment("1", "100", "20", {"name": "Renamed"})
    assert update_res["name"] == "Renamed"

    prep_res = server.prepare_publish("1", "100", "11", environment_id="20")
    assert "approval_id" in prep_res
    publish_guard.approve_request(
        prep_res["approval_id"], typed_public_id="GTM-TEST123", path=approval_file
    )

    publish_res = asyncio.run(
        server.publish_to_environment(
            "1", "100", "20", "11", prep_res["approval_id"], confirm=True
        )
    )
    assert publish_res["ok"] is True

    delete_res = server.delete_environment("1", "100", "20", confirm=True)
    assert delete_res == {"ok": True, "result": {"deleted": "20"}}


def test_user_environment_delete_succeeds_with_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    writer = FakePublishWriter()
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    res = server.delete_environment("1", "100", "7", confirm=True)
    assert res == {"ok": True, "result": {"deleted": "7"}}
    assert writer.delete_environment_calls == [("1", "100", "7")]


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
