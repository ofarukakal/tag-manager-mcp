from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from tag_manager_mcp import server
from tag_manager_mcp.access import AccessDeniedError


class RecordingFake:
    """Records every method call without touching the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def _method(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, args, kwargs))
            return {"called": name, "args": list(args), "kwargs": kwargs}

        return _method


def test_get_writer_caches_and_rebinds_on_client_change(monkeypatch: pytest.MonkeyPatch) -> None:
    c1 = object()
    c2 = object()
    monkeypatch.setattr(server, "_writer", None)
    monkeypatch.setattr(server, "get_client", lambda: c1)

    w1 = server.get_writer()
    assert w1.client is c1
    assert server.get_writer() is w1

    monkeypatch.setattr(server, "get_client", lambda: c2)
    w2 = server.get_writer()
    assert w2 is not w1
    assert w2.client is c2


@pytest.mark.parametrize("kind", ["tags", "triggers", "variables", "folders"])
def test_entity_crud_existing_kinds_route_to_client(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    client = RecordingFake()
    writer = RecordingFake()
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    created = server.create_entity(kind, "1", "2", "3", {"name": "N", "type": "t"})  # type: ignore[arg-type]
    updated = server.update_entity(
        kind, "1", "2", "3", "9", {"name": "N2"}, fingerprint="fp1"  # type: ignore[arg-type]
    )
    deleted = server.delete_entity(kind, "1", "2", "3", "9", confirm=True)  # type: ignore[arg-type]

    assert created["called"] == "create_entity"
    assert updated["called"] == "update_entity"
    assert deleted["ok"] is True
    assert deleted["result"]["called"] == "delete_entity"
    assert [c[0] for c in client.calls] == ["create_entity", "update_entity", "delete_entity"]
    assert writer.calls == []


@pytest.mark.parametrize(
    "kind", ["templates", "clients", "transformations", "zones", "gtag_config"]
)
def test_entity_crud_new_kinds_route_to_writer(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    client = RecordingFake()
    writer = RecordingFake()
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    created = server.create_entity(kind, "1", "2", "3", {"name": "N"})  # type: ignore[arg-type]
    updated = server.update_entity(
        kind, "1", "2", "3", "9", {"name": "N2"}, fingerprint="fp2"  # type: ignore[arg-type]
    )
    deleted = server.delete_entity(kind, "1", "2", "3", "9", confirm=True)  # type: ignore[arg-type]

    assert created["called"] == "create_workspace_entity"
    assert updated["called"] == "update_workspace_entity"
    assert deleted["ok"] is True
    assert deleted["result"]["called"] == "delete_workspace_entity"
    assert [c[0] for c in writer.calls] == [
        "create_workspace_entity",
        "update_workspace_entity",
        "delete_workspace_entity",
    ]
    assert client.calls == []


def test_read_mode_writer_and_reader_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "read")
    writer = RecordingFake()
    reader = RecordingFake()
    monkeypatch.setattr(server, "get_writer", lambda: writer)
    monkeypatch.setattr(server, "get_reader", lambda: reader)

    assert server.latest_version_header("1", "2")["called"] == "latest_version_header"
    assert server.list_folder_entities("1", "2", "3", "4")["called"] == "list_folder_entities"
    assert server.get_workspace("1", "2", "3")["called"] == "get_workspace"
    assert server.get_environment("1", "2", "5")["called"] == "get_environment"
    assert server.get_destination("1", "2", "6")["called"] == "get_destination"
    assert server.get_container_snippet("1", "2")["called"] == "get_container_snippet"
    assert server.get_account("1")["called"] == "get_account"

    assert [c[0] for c in reader.calls] == ["latest_version_header"]
    assert [c[0] for c in writer.calls] == [
        "list_folder_entities",
        "get_workspace",
        "get_environment",
        "get_destination",
        "get_container_snippet",
        "get_account",
    ]


def test_write_mode_tools_pass_through_and_block_in_read_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = RecordingFake()
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    calls: list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]] = [
        (
            "revert_workspace_entity",
            server.revert_entity,
            ("tags", "1", "2", "3", "9"),
            {"fingerprint": "fp"},
        ),
        ("revert_builtin_variable", server.revert_builtin_variable, ("1", "2", "3", "pageUrl"), {}),
        (
            "import_template_from_gallery",
            server.import_template_from_gallery,
            ("1", "2", "3", "owner", "repo"),
            {"gallery_sha": "abc", "acknowledge_permissions": True},
        ),
        (
            "move_entities_to_folder",
            server.move_entities_to_folder,
            ("1", "2", "3", "0"),
            {"tag_ids": ["1"], "trigger_ids": ["2"], "variable_ids": ["3"]},
        ),
        (
            "update_workspace",
            server.update_workspace,
            ("1", "2", "3", {"name": "W"}),
            {"fingerprint": "fp"},
        ),
        (
            "resolve_conflict",
            server.resolve_conflict,
            ("1", "2", "3", {"tag": {"name": "T"}}),
            {"fingerprint": "fp", "confirm": True},
        ),
        ("quick_preview", server.quick_preview, ("1", "2", "3"), {}),
        ("bulk_update", server.bulk_update, ("1", "2", "3", {"proposedChange": []}), {}),
        (
            "update_version",
            server.update_version,
            ("1", "2", "7", {"name": "v7"}),
            {"fingerprint": "fp"},
        ),
        ("delete_version", server.delete_version, ("1", "2", "7"), {"confirm": True}),
        ("undelete_version", server.undelete_version, ("1", "2", "7"), {}),
        ("create_environment", server.create_environment, ("1", "2", {"name": "Staging"}), {}),
        (
            "update_environment",
            server.update_environment,
            ("1", "2", "5", {"name": "QA"}),
            {"fingerprint": "fp"},
        ),
        ("delete_environment", server.delete_environment, ("1", "2", "5"), {"confirm": True}),
        (
            "link_destination",
            server.link_destination,
            ("1", "2", "10"),
            {"allow_user_permission_feature_update": True},
        ),
    ]

    monkeypatch.setenv("GTM_ACCESS_MODE", "read")
    for _, func, args, kwargs in calls:
        with pytest.raises(AccessDeniedError):
            func(*args, **kwargs)
    assert writer.calls == []

    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    for expected_method, func, args, kwargs in calls:
        res = func(*args, **kwargs)
        if kwargs.get("confirm"):
            assert res["ok"] is True
            assert res["result"]["called"] == expected_method
        else:
            assert res["called"] == expected_method
    assert [c[0] for c in writer.calls] == [expected for expected, _, _, _ in calls]


def test_reauthorize_environment_requires_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = RecordingFake()
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    with pytest.raises(AccessDeniedError):
        server.reauthorize_environment("1", "2", "5")
    assert writer.calls == []

    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    res = server.reauthorize_environment("1", "2", "5")
    assert res["called"] == "reauthorize_environment"
    assert writer.calls == [("reauthorize_environment", ("1", "2", "5"), {})]


def test_admin_tools_require_admin_and_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = RecordingFake()
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    admin_calls: list[tuple[str, Any, tuple[Any, ...], dict[str, Any]]] = [
        ("delete_workspace", server.delete_workspace, ("1", "2", "3"), {"confirm": True}),
        ("create_container", server.create_container, ("1", {"name": "Web"}), {}),
        (
            "update_container",
            server.update_container,
            ("1", "2", {"name": "Web2"}),
            {"fingerprint": "fp"},
        ),
        ("delete_container", server.delete_container, ("1", "2"), {"confirm": True}),
        (
            "combine_containers",
            server.combine_containers,
            ("1", "2", "3"),
            {
                "setting_source": "current",
                "allow_user_permission_feature_update": True,
                "confirm": True,
            },
        ),
        (
            "move_tag_id",
            server.move_tag_id,
            ("1", "2", "GTM-AAA"),
            {"tag_name": "New", "copy_settings": True, "confirm": True},
        ),
        ("update_account", server.update_account, ("1", {"name": "Acct"}), {"fingerprint": "fp"}),
        ("list_user_permissions", server.list_user_permissions, ("1",), {}),
        ("get_user_permission", server.get_user_permission, ("1", "99"), {}),
        (
            "create_user_permission",
            server.create_user_permission,
            ("1", {"emailAddress": "a@b.com"}),
            {},
        ),
        (
            "update_user_permission",
            server.update_user_permission,
            ("1", "99", {"emailAddress": "a@b.com"}),
            {},
        ),
        ("delete_user_permission", server.delete_user_permission, ("1", "99"), {"confirm": True}),
    ]

    for denied_mode in ("read", "write", "publish"):
        monkeypatch.setenv("GTM_ACCESS_MODE", denied_mode)
        for _, func, args, kwargs in admin_calls:
            with pytest.raises(AccessDeniedError):
                func(*args, **kwargs)
    assert writer.calls == []

    monkeypatch.setenv("GTM_ACCESS_MODE", "admin")
    for expected_method, func, args, kwargs in admin_calls:
        res = func(*args, **kwargs)
        if kwargs.get("confirm"):
            assert res["ok"] is True
            assert res["result"]["called"] == expected_method
        else:
            assert res["called"] == expected_method
    assert [c[0] for c in writer.calls] == [expected for expected, _, _, _ in admin_calls]


CONFIRM_GUARDED_CALLS: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
    ("delete_entity", ("tags", "1", "2", "3", "9"), {}),
    ("delete_entity", ("templates", "1", "2", "3", "9"), {}),
    ("disable_builtin_variables", ("1", "2", "3", ["pageUrl"]), {}),
    ("delete_version", ("1", "2", "7"), {}),
    ("delete_environment", ("1", "2", "5"), {}),
    ("delete_workspace", ("1", "2", "3"), {}),
    ("delete_container", ("1", "2"), {}),
    ("delete_user_permission", ("1", "99"), {}),
    ("combine_containers", ("1", "2", "3"), {}),
    ("move_tag_id", ("1", "2", "GTM-AAA"), {}),
    ("resolve_conflict", ("1", "2", "3", {"tag": {"name": "T"}}), {}),
    ("apply_container_import", ("1", "2", "3", "export.json"), {"mode": "overwrite"}),
    ("publish_version", ("1", "2", "7", "approval-1"), {}),
    ("publish_to_environment", ("1", "2", "5", "7", "approval-1"), {}),
    ("set_latest_version", ("1", "2", "7", "approval-1"), {}),
]


def test_every_confirm_tool_is_covered_in_confirm_matrix() -> None:
    """Invariant I5 completeness: every registered tool taking `confirm` is tested."""
    tools_with_confirm = {
        spec.name
        for spec in server.tool_registry.all_tools
        if "confirm" in inspect.signature(spec.func).parameters
    }
    tested_names = {name for name, _, _ in CONFIRM_GUARDED_CALLS}
    assert tools_with_confirm == tested_names


@pytest.mark.parametrize(("tool_name", "args", "kwargs"), CONFIRM_GUARDED_CALLS)
def test_confirm_guarded_tools_refuse_without_confirm_and_do_not_call_api(
    tool_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant I5: every tool taking `confirm` refuses without it and touches no write API."""
    monkeypatch.setenv("GTM_ACCESS_MODE", "admin")

    def _boom() -> Any:
        raise AssertionError(f"{tool_name} must not call client/reader/writer without confirm=true")

    monkeypatch.setattr(server, "get_client", _boom)
    monkeypatch.setattr(server, "get_writer", _boom)
    if tool_name == "apply_container_import":
        export_dir = tmp_path / "exports"
        export_dir.mkdir()
        (export_dir / "export.json").write_text(
            json.dumps(
                {
                    "exportFormatVersion": 2,
                    "containerVersion": {"tag": [{"tagId": "1", "name": "T1", "type": "html"}]},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))

        class _PlanReader:
            def list_workspace_entities(
                self, kind: str, account_id: str, container_id: str, workspace_id: str
            ) -> list[dict[str, Any]]:
                if kind == "tags":
                    return [{"tagId": "9", "name": "Existing Tag"}]
                return []

        monkeypatch.setattr(server, "get_reader", lambda: _PlanReader())
    else:
        monkeypatch.setattr(server, "get_reader", _boom)

    func = getattr(server, tool_name)
    result = func(*args, **kwargs, confirm=False)
    assert result["ok"] is False
    assert "confirm=true" in result["message"]


def test_environment_write_tools_reject_container_version_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    writer = RecordingFake()
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    for bad_value in ("123", "", None):
        create_res = server.create_environment(
            "1", "2", {"name": "Staging", "containerVersionId": bad_value}
        )
        assert create_res["ok"] is False
        assert "containerVersionId" in create_res["message"]
        update_res = server.update_environment(
            "1", "2", "5", {"name": "Staging", "containerVersionId": bad_value}
        )
        assert update_res["ok"] is False
        assert "containerVersionId" in update_res["message"]

    assert writer.calls == []


def test_environment_authorization_code_redacted_except_reauthorize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEnvReader:
        def list_environments(self, account_id: str, container_id: str) -> list[dict[str, Any]]:
            return [
                {
                    "environmentId": "1",
                    "name": "Live",
                    "authorizationCode": "live-secret",
                },
                {
                    "environmentId": "7",
                    "name": "Staging",
                    "authorizationCode": "staging-secret",
                },
            ]

    class FakeEnvWriter:
        def get_environment(
            self, account_id: str, container_id: str, environment_id: str
        ) -> dict[str, Any]:
            return {
                "environmentId": environment_id,
                "name": "Staging",
                "authorizationCode": "staging-secret",
            }

        def reauthorize_environment(
            self,
            account_id: str,
            container_id: str,
            environment_id: str,
            body: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            return {
                "environmentId": environment_id,
                "name": "Staging",
                "authorizationCode": "fresh-rotated-secret",
            }

    monkeypatch.setattr(server, "get_reader", lambda: FakeEnvReader())
    monkeypatch.setattr(server, "get_writer", lambda: FakeEnvWriter())

    monkeypatch.setenv("GTM_ACCESS_MODE", "read")
    listed = server.list_environments("1", "2")
    assert [env["authorizationCode"] for env in listed] == ["[redacted]", "[redacted]"]

    got = server.get_environment("1", "2", "7")
    assert got["authorizationCode"] == "[redacted]"

    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    reauthorized = server.reauthorize_environment("1", "2", "7")
    assert reauthorized["authorizationCode"] == "fresh-rotated-secret"
