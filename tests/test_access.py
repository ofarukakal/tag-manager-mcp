from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tag_manager_mcp import publish_guard, server
from tag_manager_mcp.access import (
    SCOPE_DELETE_CONTAINERS,
    SCOPE_EDIT_CONTAINERS,
    SCOPE_EDIT_CONTAINERVERSIONS,
    SCOPE_MANAGE_ACCOUNTS,
    SCOPE_MANAGE_USERS,
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
    "list_folder_entities",
    "get_workspace",
    "list_versions",
    "latest_version_header",
    "get_version_summary",
    "get_version_entities",
    "diff_container_versions",
    "export_container_to_file",
    "list_environments",
    "list_destinations",
    "inventory",
    "list_live_versions",
    "get_environment",
    "get_destination",
    "get_container_snippet",
    "get_account",
    "plan_container_import",
}

WRITE_TOOLS = {
    "create_workspace",
    "sync_workspace",
    "create_entity",
    "update_entity",
    "delete_entity",
    "revert_entity",
    "enable_builtin_variables",
    "disable_builtin_variables",
    "revert_builtin_variable",
    "import_template_from_gallery",
    "move_entities_to_folder",
    "update_workspace",
    "resolve_conflict",
    "quick_preview",
    "bulk_update",
    "create_version",
    "update_version",
    "delete_version",
    "undelete_version",
    "create_environment",
    "update_environment",
    "delete_environment",
    "link_destination",
    "apply_container_import",
}

PUBLISH_TOOLS = {
    "reauthorize_environment",
    "prepare_publish",
    "publish_version",
    "publish_to_environment",
    "set_latest_version",
    "list_publish_requests",
}

ADMIN_TOOLS = {
    "delete_workspace",
    "create_container",
    "update_container",
    "delete_container",
    "combine_containers",
    "move_tag_id",
    "update_account",
    "list_user_permissions",
    "get_user_permission",
    "create_user_permission",
    "update_user_permission",
    "delete_user_permission",
}


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
    assert scopes_for_mode("admin") == (
        SCOPE_READONLY,
        SCOPE_EDIT_CONTAINERS,
        SCOPE_EDIT_CONTAINERVERSIONS,
        SCOPE_PUBLISH,
        SCOPE_DELETE_CONTAINERS,
        SCOPE_MANAGE_ACCOUNTS,
        SCOPE_MANAGE_USERS,
    )
    admin_only = {
        SCOPE_DELETE_CONTAINERS,
        SCOPE_MANAGE_ACCOUNTS,
        SCOPE_MANAGE_USERS,
    }
    for mode in (AccessMode.READ, AccessMode.WRITE, AccessMode.PUBLISH):
        assert admin_only.isdisjoint(scopes_for_mode(mode))


@pytest.mark.parametrize(
    "bad_value", ["", "   ", "readonly", "superadmin", "all", "readwrite", "1"]
)
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
        registry.tool("superadmin")

    with pytest.raises(AccessModeError):
        # Applying @registry.tool without arguments passes the function as level.
        registry.tool(lambda: None)  # type: ignore[arg-type]


def test_admin_tool_registered_only_in_admin_mode() -> None:
    registry = ToolRegistry()

    @registry.tool("read")
    def read_tool() -> None:
        pass

    @registry.tool("publish")
    def publish_tool() -> None:
        pass

    @registry.tool("admin")
    def admin_tool() -> None:
        pass

    assert [spec.name for spec in registry.tools_for_mode("read")] == ["read_tool"]
    assert [spec.name for spec in registry.tools_for_mode("write")] == ["read_tool"]
    assert [spec.name for spec in registry.tools_for_mode("publish")] == [
        "read_tool",
        "publish_tool",
    ]
    assert [spec.name for spec in registry.tools_for_mode("admin")] == [
        "read_tool",
        "publish_tool",
        "admin_tool",
    ]

    class FakeServer:
        def __init__(self) -> None:
            self.added: list[str] = []

        def add_tool(self, func: Any) -> None:
            self.added.append(func.__name__)

    publish_server = FakeServer()
    assert registry.register(publish_server, "publish") == ("read_tool", "publish_tool")
    assert "admin_tool" not in publish_server.added

    admin_server = FakeServer()
    assert registry.register(admin_server, "admin") == (
        "read_tool",
        "publish_tool",
        "admin_tool",
    )
    assert "admin_tool" in admin_server.added


def test_registered_tools_per_mode() -> None:
    read_names = _registered_tool_names("read")
    assert read_names == READ_TOOLS
    assert read_names.isdisjoint(WRITE_TOOLS)
    assert read_names.isdisjoint(PUBLISH_TOOLS)
    assert read_names.isdisjoint(ADMIN_TOOLS)

    write_names = _registered_tool_names("write")
    assert write_names == READ_TOOLS | WRITE_TOOLS
    assert write_names.isdisjoint(PUBLISH_TOOLS)
    assert write_names.isdisjoint(ADMIN_TOOLS)
    assert "publish_version" not in write_names

    publish_names = _registered_tool_names("publish")
    assert publish_names == READ_TOOLS | WRITE_TOOLS | PUBLISH_TOOLS
    assert publish_names.isdisjoint(ADMIN_TOOLS)

    admin_names = _registered_tool_names("admin")
    assert admin_names == READ_TOOLS | WRITE_TOOLS | PUBLISH_TOOLS | ADMIN_TOOLS

    for tool_name in (
        "publish_version",
        "publish_to_environment",
        "set_latest_version",
        "prepare_publish",
        "list_publish_requests",
        "reauthorize_environment",
    ):
        assert tool_name not in read_names
        assert tool_name not in write_names
        assert tool_name in publish_names
        assert tool_name in admin_names

    for tool_name in ADMIN_TOOLS:
        assert tool_name not in read_names
        assert tool_name not in write_names
        assert tool_name not in publish_names
        assert tool_name in admin_names


def test_is_allowed_hierarchy() -> None:
    ordered = (AccessMode.READ, AccessMode.WRITE, AccessMode.PUBLISH, AccessMode.ADMIN)
    for req_idx, required in enumerate(ordered):
        for act_idx, active in enumerate(ordered):
            assert is_allowed(required, active) is (act_idx >= req_idx)


def test_require_refuses_insufficient_mode() -> None:
    assert require("read", env={}) is AccessMode.READ
    assert require("admin", env={"GTM_ACCESS_MODE": "admin"}) is AccessMode.ADMIN
    assert require("publish", env={"GTM_ACCESS_MODE": "admin"}) is AccessMode.ADMIN
    with pytest.raises(AccessDeniedError, match="'write'"):
        require("write", env={})
    with pytest.raises(AccessDeniedError, match="'publish'"):
        require("publish", env={"GTM_ACCESS_MODE": "write"})
    with pytest.raises(AccessDeniedError, match="'admin'"):
        require("admin", env={"GTM_ACCESS_MODE": "publish"})


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
        asyncio.run(server.publish_version("1", "2", "3", "approval-1", confirm=True))


def test_direct_call_guard_blocks_publish_in_write_mode_and_preserves_confirm(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeRequest:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def execute(self) -> dict[str, Any]:
            return self.payload

    class FakeVersions:
        def live(self, parent: str) -> FakeRequest:
            return FakeRequest({"containerVersionId": "2", "container": {"publicId": "GTM-DEMO"}})

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
    approval_file = tmp_path / "approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(approval_file))

    req = publish_guard.create_request(
        account_id="1",
        container_id="2",
        public_id="GTM-DEMO",
        version_id="3",
        version_name="v3",
        live_version_id="2",
        diff_summary={"total_changes": 1},
        path=approval_file,
    )
    publish_guard.approve_request(
        req["approval_id"], typed_public_id="GTM-DEMO", path=approval_file
    )

    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    with pytest.raises(AccessDeniedError):
        asyncio.run(server.publish_version("1", "2", "3", req["approval_id"], confirm=True))

    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    unconfirmed = asyncio.run(
        server.publish_version("1", "2", "3", req["approval_id"], confirm=False)
    )
    assert unconfirmed["ok"] is False
    assert "confirm=true" in unconfirmed["message"]

    confirmed = asyncio.run(
        server.publish_version("1", "2", "3", req["approval_id"], confirm=True)
    )
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
