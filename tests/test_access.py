from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tag_manager_mcp import server
from tag_manager_mcp.access import (
    SCOPE_EDIT_CONTAINERS,
    SCOPE_EDIT_CONTAINERVERSIONS,
    SCOPE_PUBLISH,
    SCOPE_READONLY,
    AccessDeniedError,
    AccessMode,
    AccessModeError,
    ToolRegistry,
    get_access_mode,
    is_allowed,
    require,
    scopes_for_mode,
)
from tag_manager_mcp.client import GTMClient
from tag_manager_mcp.quota import QuotaGuard

READ_TOOLS = {
    "auth_status",
    "quota_status",
    "debug_log_status",
    "debug_log_recent",
    "list_accounts",
    "list_containers",
    "get_container",
    "resolve_container_identifier",
    "list_workspaces",
    "get_workspace_status",
    "list_entities",
    "get_entity",
    "list_builtin_variables",
    "list_versions",
    "get_version_summary",
    "get_version_entities",
    "diff_container_versions",
    "export_container_to_file",
    "list_environments",
    "list_destinations",
    "inventory",
    "list_live_versions",
}

WRITE_TOOLS = {
    "create_workspace",
    "sync_workspace",
    "create_entity",
    "update_entity",
    "delete_entity",
    "enable_builtin_variables",
    "disable_builtin_variables",
    "create_version",
}

PUBLISH_TOOLS = {"publish_version"}


def _registered_tool_names(mode: str | AccessMode) -> set[str]:
    srv = server.build_server(mode)
    return {tool.name for tool in asyncio.run(srv.list_tools())}


def test_default_access_mode_is_read() -> None:
    assert get_access_mode({}) is AccessMode.READ


def test_scopes_per_mode_are_exact() -> None:
    assert scopes_for_mode("read") == (SCOPE_READONLY,)
    assert scopes_for_mode("write") == (
        SCOPE_READONLY,
        SCOPE_EDIT_CONTAINERS,
        SCOPE_EDIT_CONTAINERVERSIONS,
    )
    assert scopes_for_mode("publish") == (
        SCOPE_READONLY,
        SCOPE_EDIT_CONTAINERS,
        SCOPE_EDIT_CONTAINERVERSIONS,
        SCOPE_PUBLISH,
    )
    forbidden = {
        "https://www.googleapis.com/auth/tagmanager.manage.accounts",
        "https://www.googleapis.com/auth/tagmanager.manage.users",
    }
    for mode in AccessMode:
        assert forbidden.isdisjoint(scopes_for_mode(mode))


@pytest.mark.parametrize("bad_value", ["", "   ", "readonly", "admin", "all", "readwrite", "1"])
def test_invalid_access_mode_raises(bad_value: str) -> None:
    with pytest.raises(AccessModeError, match="GTM_ACCESS_MODE"):
        get_access_mode({"GTM_ACCESS_MODE": bad_value})
    with pytest.raises(AccessModeError):
        server.build_server(bad_value)


def test_tool_decorator_requires_explicit_valid_level() -> None:
    registry = ToolRegistry()

    with pytest.raises(TypeError):
        registry.tool()  # type: ignore[call-arg]

    with pytest.raises(AccessModeError):
        registry.tool("admin")

    with pytest.raises(AccessModeError):
        # Applying @registry.tool without arguments passes the function as level.
        registry.tool(lambda: None)  # type: ignore[arg-type]


def test_registered_tools_per_mode() -> None:
    read_names = _registered_tool_names("read")
    assert read_names == READ_TOOLS
    assert read_names.isdisjoint(WRITE_TOOLS)
    assert read_names.isdisjoint(PUBLISH_TOOLS)

    write_names = _registered_tool_names("write")
    assert write_names == READ_TOOLS | WRITE_TOOLS
    assert "publish_version" not in write_names

    publish_names = _registered_tool_names("publish")
    assert publish_names == READ_TOOLS | WRITE_TOOLS | PUBLISH_TOOLS

    for mode in AccessMode:
        assert "list_user_permissions" not in _registered_tool_names(mode)


def test_is_allowed_hierarchy() -> None:
    assert is_allowed("read", "read") is True
    assert is_allowed("write", "read") is False
    assert is_allowed("publish", "read") is False
    assert is_allowed("read", "write") is True
    assert is_allowed("write", "write") is True
    assert is_allowed("publish", "write") is False
    assert is_allowed("publish", "publish") is True


def test_require_refuses_insufficient_mode() -> None:
    assert require("read", env={}) is AccessMode.READ
    with pytest.raises(AccessDeniedError, match="'write'"):
        require("write", env={})
    with pytest.raises(AccessDeniedError, match="'publish'"):
        require("publish", env={"GTM_ACCESS_MODE": "write"})


def test_direct_call_guard_refuses_all_write_and_publish_tools_in_read_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GTM_ACCESS_MODE", raising=False)
    monkeypatch.setattr(
        server,
        "get_client",
        lambda: (_ for _ in ()).throw(AssertionError("client should not be called")),
    )

    with pytest.raises(AccessDeniedError):
        server.create_workspace("1", "2", "ws")
    with pytest.raises(AccessDeniedError):
        server.sync_workspace("1", "2", "3")
    with pytest.raises(AccessDeniedError):
        server.create_entity("tags", "1", "2", "3", {"name": "t", "type": "html"})
    with pytest.raises(AccessDeniedError):
        server.update_entity("tags", "1", "2", "3", "4", {"name": "t"})
    with pytest.raises(AccessDeniedError):
        server.delete_entity("tags", "1", "2", "3", "4", confirm=True)
    with pytest.raises(AccessDeniedError):
        server.enable_builtin_variables("1", "2", "3", ["pageUrl"])
    with pytest.raises(AccessDeniedError):
        server.disable_builtin_variables("1", "2", "3", ["pageUrl"], confirm=True)
    with pytest.raises(AccessDeniedError):
        server.create_version("1", "2", "3", name="v1")
    with pytest.raises(AccessDeniedError):
        server.publish_version("1", "2", "3", confirm=True)


def test_direct_call_guard_blocks_publish_in_write_mode_and_preserves_confirm(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeRequest:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def execute(self) -> dict[str, Any]:
            return self.payload

    class FakeVersions:
        def publish(self, path: str) -> FakeRequest:
            return FakeRequest({"containerVersion": {"path": path}})

    class FakeContainers:
        def versions(self) -> FakeVersions:
            return FakeVersions()

    class FakeAccounts:
        def list(self) -> FakeRequest:
            return FakeRequest({"account": [{"accountId": "1", "name": "Demo"}]})

        def containers(self) -> FakeContainers:
            return FakeContainers()

    class FakeService:
        def accounts(self) -> FakeAccounts:
            return FakeAccounts()

    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "10",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        }
    )
    fake_client = GTMClient(service=FakeService(), quota_guard=guard)
    fake_client.auth_method = "user-oauth"
    monkeypatch.setattr(server, "get_client", lambda: fake_client)

    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    with pytest.raises(AccessDeniedError):
        server.publish_version("1", "2", "3", confirm=True)

    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    unconfirmed = server.publish_version("1", "2", "3", confirm=False)
    assert unconfirmed["ok"] is False
    assert "confirm=true" in unconfirmed["message"]

    confirmed = server.publish_version("1", "2", "3", confirm=True)
    assert confirmed == {
        "ok": True,
        "result": {"containerVersion": {"path": "accounts/1/containers/2/versions/3"}},
    }


def test_auth_status_reports_access_mode_and_scopes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeRequest:
        def execute(self) -> dict[str, Any]:
            return {"account": [{"accountId": "1", "name": "Client"}]}

    class FakeAccounts:
        def list(self) -> FakeRequest:
            return FakeRequest()

    class FakeService:
        def accounts(self) -> FakeAccounts:
            return FakeAccounts()

    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "10",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        }
    )
    fake_client = GTMClient(service=FakeService(), quota_guard=guard)
    fake_client.auth_method = "user-oauth"
    monkeypatch.setattr(server, "get_client", lambda: fake_client)

    monkeypatch.delenv("GTM_ACCESS_MODE", raising=False)
    status = server.auth_status()
    assert status["ok"] is True
    assert status["access_mode"] == "read"
    assert status["scopes"] == [SCOPE_READONLY]
    assert status["accounts"] == [{"accountId": "1", "name": "Client"}]

    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    write_status = server.auth_status()
    assert write_status["access_mode"] == "write"
    assert write_status["scopes"] == [
        SCOPE_READONLY,
        SCOPE_EDIT_CONTAINERS,
        SCOPE_EDIT_CONTAINERVERSIONS,
    ]
