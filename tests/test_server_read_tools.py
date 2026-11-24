from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tag_manager_mcp import export_files, server
from tag_manager_mcp.access import AccessMode


class FakeReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.live_payload: dict[str, Any] = {
            "containerVersionId": "10",
            "name": "Live 10",
            "container": {"publicId": "GTM-TEST123"},
            "tag": [
                {"tagId": "1", "name": "GA4 Config", "paused": False},
                {"tagId": "2", "name": "Old Pixel", "paused": True},
            ],
            "trigger": [{"triggerId": "1", "name": "All Pages"}],
            "variable": [{"variableId": "1", "name": "Page URL"}],
            "customTemplate": [
                {"templateId": "1", "name": "Consent Banner", "templateData": "x" * 120},
                {"templateId": "2", "name": "Custom Helper", "templateData": "y" * 50},
            ],
        }
        self.versions_by_id: dict[str, dict[str, Any]] = {
            "8": {
                "containerVersionId": "8",
                "name": "Release 8",
                "container": {"publicId": "GTM-TEST123"},
                "tag": [{"tagId": "1", "name": "GA4 Config", "paused": False}],
                "trigger": [],
                "variable": [],
            }
        }
        self.fetch_all_live_items: list[dict[str, Any]] = [
            {
                "accountId": "1",
                "containerId": "100",
                "publicId": "GTM-WEB1",
                "name": "Site One",
                "version": {
                    "containerVersionId": "4",
                    "name": "v4",
                    "tag": [{"tagId": "1", "name": "T1", "paused": False}],
                },
                "error": None,
            },
            {
                "accountId": "1",
                "containerId": "101",
                "publicId": "GTM-WEB2",
                "name": "Site Two",
                "version": None,
                "error": "Published version not found",
            },
        ]

    def live_version(self, account_id: str, container_id: str) -> dict[str, Any]:
        self.calls.append(("live_version", (account_id, container_id), {}))
        return self.live_payload

    def get_version(self, account_id: str, container_id: str, version_id: str) -> dict[str, Any]:
        self.calls.append(("get_version", (account_id, container_id, version_id), {}))
        return self.versions_by_id[version_id]

    def export_container(
        self, account_id: str, container_id: str, version_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(
            ("export_container", (account_id, container_id), {"version_id": version_id})
        )
        version = (
            self.live_payload if version_id is None else self.versions_by_id[version_id]
        )
        return {
            "exportFormatVersion": 2,
            "exportTime": "2026-11-22 12:00:00",
            "containerVersion": version,
        }

    def list_environments(self, account_id: str, container_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_environments", (account_id, container_id), {}))
        return [{"environmentId": "1", "name": "Live"}]

    def list_destinations(self, account_id: str, container_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list_destinations", (account_id, container_id), {}))
        return [{"destinationId": "G-123"}]

    def inventory(self) -> list[dict[str, Any]]:
        self.calls.append(("inventory", (), {}))
        return [{"accountId": "1", "name": "Demo", "containers": [], "error": None}]

    def fetch_all_live(self, usage_context: str | None = "web") -> list[dict[str, Any]]:
        self.calls.append(("fetch_all_live", (), {"usage_context": usage_context}))
        return list(self.fetch_all_live_items)

    def list_workspace_entities(
        self, kind: str, account_id: str, container_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("list_workspace_entities", (kind, account_id, container_id, workspace_id), {})
        )
        return [{"templateId": "1", "name": "Tpl"}]

    def get_workspace_entity(
        self, kind: str, account_id: str, container_id: str, workspace_id: str, entity_id: str
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "get_workspace_entity",
                (kind, account_id, container_id, workspace_id, entity_id),
                {},
            )
        )
        return {"templateId": entity_id, "name": "Tpl"}


def test_get_version_summary_live_and_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    monkeypatch.setattr(server, "get_reader", lambda: reader)

    live_summary = server.get_version_summary("1", "100")
    assert live_summary["containerVersionId"] == "10"
    assert live_summary["counts"]["tag"] == 2
    assert live_summary["pausedTags"] == ["Old Pixel"]

    v8_summary = server.get_version_summary("1", "100", version_id="8")
    assert v8_summary["containerVersionId"] == "8"
    assert v8_summary["counts"]["tag"] == 1
    assert v8_summary["pausedTags"] == []


def test_get_version_entities_filtering_and_template_data_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FakeReader()
    monkeypatch.setattr(server, "get_reader", lambda: reader)

    tags = server.get_version_entities("1", "100", "tag", name_contains="pixel")
    assert tags == [{"tagId": "2", "name": "Old Pixel", "paused": True}]

    many_templates = server.get_version_entities("1", "100", "customTemplate")
    assert len(many_templates) == 2
    assert all("templateData" not in item for item in many_templates)
    assert [item["templateDataBytes"] for item in many_templates] == [120, 50]
    # Original cached payload was not mutated.
    assert "templateData" in reader.live_payload["customTemplate"][0]

    single_template = server.get_version_entities(
        "1", "100", "customTemplate", name_contains="consent"
    )
    assert single_template == [
        {"templateId": "1", "name": "Consent Banner", "templateData": "x" * 120}
    ]


def test_diff_container_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    monkeypatch.setattr(server, "get_reader", lambda: reader)

    diff = server.diff_container_versions("1", "100", "8")
    assert diff["from"] == "8"
    assert diff["to"] == "10"
    assert diff["changes"]["tag"]["added"] == [{"id": "2", "name": "Old Pixel", "fields": []}]
    assert diff["total"] >= 1


def test_export_container_to_file_writes_expected_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = FakeReader()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setenv("GTM_EXPORT_DIR", str(tmp_path / "exports"))

    result = server.export_container_to_file("1", "100")
    out_path = Path(result["path"])
    assert out_path.is_absolute()
    assert out_path.parent == (tmp_path / "exports").resolve()
    assert out_path.name == "GTM-TEST123_v10.json"
    assert result["summary"]["containerVersionId"] == "10"

    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(written.keys()) == {"exportFormatVersion", "exportTime", "containerVersion"}
    assert written["exportFormatVersion"] == 2
    assert written["containerVersion"]["containerVersionId"] == "10"


def test_export_sanitizes_traversal_public_id_and_rejects_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = FakeReader()
    reader.live_payload["container"]["publicId"] = "../../etc/passwd"
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    export_dir = (tmp_path / "exports").resolve()
    monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))

    result = server.export_container_to_file("1", "100")
    out_path = Path(result["path"]).resolve()
    assert out_path.is_relative_to(export_dir)
    assert out_path.parent == export_dir
    assert out_path.name == "______etc_passwd_v10.json"

    # Also verify defense-in-depth path containment check raises ValueError.
    class NoopPattern:
        def sub(self, _repl: str, value: str) -> str:
            return value

    monkeypatch.setattr(export_files, "_SAFE_FILENAME_RE", NoopPattern())
    with pytest.raises(ValueError, match="escapes export directory"):
        export_files.write_version_export(
            {"containerVersionId": "10", "container": {"publicId": "../../escape"}},
            export_dir=export_dir,
        )


def test_container_and_account_read_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    monkeypatch.setattr(server, "get_reader", lambda: reader)

    assert server.list_environments("1", "100") == [{"environmentId": "1", "name": "Live"}]
    assert server.list_destinations("1", "100") == [{"destinationId": "G-123"}]
    assert server.inventory() == [
        {"accountId": "1", "name": "Demo", "containers": [], "error": None}
    ]


def test_list_live_versions_without_and_with_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = FakeReader()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    export_dir = (tmp_path / "exports").resolve()
    monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))

    unsaved = server.list_live_versions(usage_context="web", save=False)
    assert len(unsaved) == 2
    assert "path" not in unsaved[0]
    assert unsaved[0]["summary"]["containerVersionId"] == "4"
    assert unsaved[1]["summary"] is None
    assert unsaved[1]["error"] == "Published version not found"

    saved = server.list_live_versions(usage_context="web", save=True)
    saved_path = Path(saved[0]["path"])
    assert saved_path.parent == export_dir
    assert saved_path.name == "GTM-WEB1_v4.json"
    assert saved[1]["path"] is None

    written = json.loads(saved_path.read_text(encoding="utf-8"))
    assert set(written.keys()) == {"exportFormatVersion", "exportTime", "containerVersion"}
    assert written["containerVersion"]["containerVersionId"] == "4"


def test_list_live_versions_save_continues_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = FakeReader()
    reader.fetch_all_live_items = [
        {
            "accountId": "1",
            "containerId": "100",
            "publicId": "GTM-OK1",
            "name": "Site One",
            "version": {"containerVersionId": "4", "name": "v4", "tag": []},
            "error": None,
        },
        {
            "accountId": "1",
            "containerId": "101",
            "publicId": "GTM-FAIL",
            "name": "Site Two",
            "version": {"containerVersionId": "7", "name": "v7", "tag": []},
            "error": None,
        },
        {
            "accountId": "1",
            "containerId": "102",
            "publicId": "GTM-OK2",
            "name": "Site Three",
            "version": {"containerVersionId": "9", "name": "v9", "tag": []},
            "error": None,
        },
    ]
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    export_dir = (tmp_path / "exports").resolve()
    monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))

    real_write = export_files.write_version_export

    def flaky_write(export_or_version: dict[str, Any], **kwargs: Any) -> Path:
        if kwargs.get("public_id") == "GTM-FAIL":
            raise OSError("disk full")
        return real_write(export_or_version, **kwargs)

    monkeypatch.setattr(server, "write_version_export", flaky_write)

    saved = server.list_live_versions(usage_context="web", save=True)
    assert len(saved) == 3
    assert Path(saved[0]["path"]).is_file()
    assert saved[0]["error"] is None
    assert saved[1]["path"] is None
    assert saved[1]["error"] == "disk full"
    assert Path(saved[2]["path"]).is_file()
    assert saved[2]["error"] is None


def test_list_and_get_entity_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = FakeReader()
    client_calls: list[tuple[str, tuple[Any, ...]]] = []

    class FakeClient:
        def list_entities(self, *args: Any) -> list[dict[str, Any]]:
            client_calls.append(("list_entities", args))
            return [{"tagId": "1"}]

        def get_entity(self, *args: Any) -> dict[str, Any]:
            client_calls.append(("get_entity", args))
            return {"tagId": "1"}

    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: FakeClient())

    assert server.list_entities("tags", "1", "2", "3") == [{"tagId": "1"}]
    assert server.get_entity("tags", "1", "2", "3", "9") == {"tagId": "1"}
    assert client_calls == [
        ("list_entities", ("tags", "1", "2", "3")),
        ("get_entity", ("tags", "1", "2", "3", "9")),
    ]

    assert server.list_entities("templates", "1", "2", "3") == [{"templateId": "1", "name": "Tpl"}]
    assert server.get_entity("gtag_config", "1", "2", "3", "5") == {
        "templateId": "5",
        "name": "Tpl",
    }
    assert reader.calls == [
        ("list_workspace_entities", ("templates", "1", "2", "3"), {}),
        ("get_workspace_entity", ("gtag_config", "1", "2", "3", "5"), {}),
    ]


def test_new_tools_registered_in_read_mode_and_write_publish_unchanged() -> None:
    new_read_tools = {
        "get_version_summary",
        "get_version_entities",
        "diff_container_versions",
        "export_container_to_file",
        "list_environments",
        "list_destinations",
        "inventory",
        "list_live_versions",
        "latest_version_header",
        "list_folder_entities",
        "get_workspace",
        "get_environment",
        "get_destination",
        "get_container_snippet",
        "get_account",
        "plan_container_import",
    }
    expected_write = {
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
    expected_publish = {
        "reauthorize_environment",
        "prepare_publish",
        "publish_version",
        "list_publish_requests",
        "set_latest_version",
        "publish_to_environment",
    }
    expected_admin = {
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

    read_srv = server.build_server(AccessMode.READ)
    read_names = {t.name for t in asyncio.run(read_srv.list_tools())}
    assert new_read_tools.issubset(read_names)
    assert read_names.isdisjoint(expected_write)
    assert read_names.isdisjoint(expected_publish)
    assert read_names.isdisjoint(expected_admin)

    by_level = {spec.name: spec.level for spec in server.tool_registry.all_tools}
    assert {name for name, lvl in by_level.items() if lvl is AccessMode.WRITE} == expected_write
    assert {
        name for name, lvl in by_level.items() if lvl is AccessMode.PUBLISH
    } == expected_publish
    assert {name for name, lvl in by_level.items() if lvl is AccessMode.ADMIN} == expected_admin
