from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tag_manager_mcp import server
from tag_manager_mcp.access import AccessDeniedError


def _sample_export() -> dict[str, Any]:
    return {
        "exportFormatVersion": 2,
        "exportTime": "2026-11-24 12:00:00",
        "containerVersion": {
            "containerVersionId": "5",
            "folder": [{"folderId": "10", "name": "Analytics"}],
            "customTemplate": [
                {"templateId": "20", "name": "Consent Tpl", "templateData": "___INFO___"}
            ],
            "variable": [
                {"variableId": "30", "name": "App Env", "type": "c", "parentFolderId": "10"}
            ],
            "trigger": [{"triggerId": "40", "name": "Page View", "type": "PAGEVIEW"}],
            "tag": [
                {
                    "tagId": "50",
                    "name": "GA4 Tag",
                    "type": "cvt_999_20",
                    "parentFolderId": "10",
                    "firingTriggerId": ["40"],
                }
            ],
            "builtInVariable": [{"type": "PAGE_URL", "name": "Page URL"}],
        },
    }


class FakeImportReader:
    def __init__(self, entities_by_kind: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.entities_by_kind = entities_by_kind or {}

    def list_workspace_entities(
        self, kind: str, account_id: str, container_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        self.calls.append((kind, account_id, container_id, workspace_id))
        return list(self.entities_by_kind.get(kind, []))


class FakeImportClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._next_id = 100

    def _alloc(self, kind: str) -> dict[str, Any]:
        self._next_id += 1
        id_field = {
            "folders": "folderId",
            "variables": "variableId",
            "triggers": "triggerId",
            "tags": "tagId",
        }[kind]
        return {id_field: str(self._next_id)}

    def create_entity(
        self, kind: str, account_id: str, container_id: str, workspace_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("create_entity", (kind, account_id, container_id, workspace_id, body)))
        return {**body, **self._alloc(kind)}

    def update_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        entity_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            ("update_entity", (kind, account_id, container_id, workspace_id, entity_id, body))
        )
        id_field = {
            "folders": "folderId",
            "variables": "variableId",
            "triggers": "triggerId",
            "tags": "tagId",
        }[kind]
        return {**body, id_field: entity_id}

    def delete_entity(
        self, kind: str, account_id: str, container_id: str, workspace_id: str, entity_id: str
    ) -> dict[str, Any]:
        self.calls.append(
            ("delete_entity", (kind, account_id, container_id, workspace_id, entity_id))
        )
        return {}

    def enable_builtin_variables(
        self, account_id: str, container_id: str, workspace_id: str, variable_types: list[str]
    ) -> list[dict[str, Any]]:
        self.calls.append(
            ("enable_builtin_variables", (account_id, container_id, workspace_id, variable_types))
        )
        return [{"type": t} for t in variable_types]


class FakeImportWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._next_id = 500

    def create_workspace_entity(
        self, kind: str, account_id: str, container_id: str, workspace_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        self._next_id += 1
        self.calls.append(
            ("create_workspace_entity", (kind, account_id, container_id, workspace_id, body))
        )
        return {**body, "templateId": str(self._next_id)}

    def update_workspace_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        entity_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "update_workspace_entity",
                (kind, account_id, container_id, workspace_id, entity_id, body),
            )
        )
        return {**body, "templateId": entity_id}

    def delete_workspace_entity(
        self, kind: str, account_id: str, container_id: str, workspace_id: str, entity_id: str
    ) -> dict[str, Any]:
        self.calls.append(
            ("delete_workspace_entity", (kind, account_id, container_id, workspace_id, entity_id))
        )
        return {}


def test_invariant_i6_import_rejects_traversal_absolute_and_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant I6: ../x.json, absolute paths, and symlinks outside export dir are rejected."""
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_sample_export()), encoding="utf-8")
    symlink_escape = export_dir / "escape.json"
    symlink_escape.symlink_to(outside)

    monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")

    def _boom() -> Any:
        raise AssertionError("API should not be called for invalid import file paths")

    monkeypatch.setattr(server, "get_reader", _boom)
    monkeypatch.setattr(server, "get_client", _boom)
    monkeypatch.setattr(server, "get_writer", _boom)

    bad_names = [
        "../outside.json",
        "../x.json",
        str(outside),
        "/etc/passwd",
        "sub/export.json",
        "..\\x.json",
        "escape.json",
        "",
        "..",
    ]
    for bad in bad_names:
        with pytest.raises(ValueError):
            server.plan_container_import("1", "2", "3", bad)
        with pytest.raises(ValueError):
            server.apply_container_import("1", "2", "3", bad, mode="merge")


def test_plan_container_import_in_read_mode_returns_plan_with_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "GTM-SRC_v5.json").write_text(json.dumps(_sample_export()), encoding="utf-8")
    monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))
    monkeypatch.setenv("GTM_ACCESS_MODE", "read")

    reader = FakeImportReader(
        {
            "folders": [{"folderId": "1", "name": "Analytics"}],
            "templates": [],
            "variables": [],
            "triggers": [],
            "tags": [],
            "built_in_variables": [],
        }
    )
    monkeypatch.setattr(server, "get_reader", lambda: reader)

    result = server.plan_container_import("1", "2", "3", "GTM-SRC_v5.json")
    assert [c[0] for c in reader.calls] == [
        "folders",
        "templates",
        "variables",
        "triggers",
        "tags",
        "built_in_variables",
    ]
    assert result["skip"]["folder"] == [{"name": "Analytics", "source_id": "10", "target_id": "1"}]
    assert result["enable_builtins"] == ["pageUrl"]
    assert result["counts"]["create"] == 4
    assert result["counts"]["skip"] == 1
    assert result["counts"]["update"] == 0
    assert result["counts"]["delete"] == 0
    assert result["counts"]["enable_builtins"] == 1
    assert result["counts"]["by_kind"]["customTemplate"]["create"] == 1


def test_apply_container_import_merge_routes_client_and_writer_and_remaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "GTM-SRC_v5.json").write_text(json.dumps(_sample_export()), encoding="utf-8")
    monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))

    reader = FakeImportReader()
    client = FakeImportClient()
    writer = FakeImportWriter()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    monkeypatch.setenv("GTM_ACCESS_MODE", "read")
    with pytest.raises(AccessDeniedError):
        server.apply_container_import("1", "2", "3", "GTM-SRC_v5.json")

    monkeypatch.setenv("GTM_ACCESS_MODE", "write")
    result = server.apply_container_import("1", "2", "3", "GTM-SRC_v5.json", mode="merge")

    assert result["errors"] == []
    assert len(result["created"]["folder"]) == 1
    assert len(result["created"]["customTemplate"]) == 1
    assert len(result["created"]["variable"]) == 1
    assert len(result["created"]["trigger"]) == 1
    assert len(result["created"]["tag"]) == 1

    # Templates went through GTMWriter, other entities & builtins through GTMClient.
    assert [c[0] for c in writer.calls] == ["create_workspace_entity"]
    assert writer.calls[0][1][0] == "templates"
    assert [c[0] for c in client.calls] == [
        "enable_builtin_variables",
        "create_entity",  # folders
        "create_entity",  # variables
        "create_entity",  # triggers
        "create_entity",  # tags
    ]
    tag_body = client.calls[-1][1][4]
    # Remapped to target container_id=2 and new templateId=501.
    assert tag_body["type"] == "cvt_2_501"


def test_apply_container_import_overwrite_requires_confirm_and_deletes_extras(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "GTM-SRC_v5.json").write_text(json.dumps(_sample_export()), encoding="utf-8")
    monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")

    reader = FakeImportReader(
        {
            "folders": [{"folderId": "1", "name": "Analytics"}],
            "templates": [
                {"templateId": "9", "name": "Consent Tpl"},
                {"templateId": "10", "name": "Obsolete Tpl"},
            ],
            "variables": [],
            "triggers": [],
            "tags": [{"tagId": "88", "name": "Old Tag"}],
            "built_in_variables": [{"type": "pageUrl"}],
        }
    )
    client = FakeImportClient()
    writer = FakeImportWriter()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    refused = server.apply_container_import(
        "1", "2", "3", "GTM-SRC_v5.json", mode="overwrite", confirm=False
    )
    assert refused["ok"] is False
    assert "confirm=true" in refused["message"]
    assert refused["counts"]["update"] == 2
    assert refused["counts"]["delete"] == 2
    assert len(reader.calls) == 6
    assert client.calls == []
    assert writer.calls == []

    applied = server.apply_container_import(
        "1", "2", "3", "GTM-SRC_v5.json", mode="overwrite", confirm=True
    )
    assert applied["errors"] == []
    assert [item["name"] for item in applied["updated"]["folder"]] == ["Analytics"]
    assert [item["name"] for item in applied["updated"]["customTemplate"]] == ["Consent Tpl"]
    assert [item["name"] for item in applied["deleted"]["tag"]] == ["Old Tag"]
    assert [item["name"] for item in applied["deleted"]["customTemplate"]] == ["Obsolete Tpl"]
    assert [c[0] for c in writer.calls] == [
        "update_workspace_entity",
        "delete_workspace_entity",
    ]


def test_apply_container_import_merge_conflict_overwrite_requires_confirm_on_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "GTM-SRC_v5.json").write_text(json.dumps(_sample_export()), encoding="utf-8")
    monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")

    reader = FakeImportReader(
        {
            "folders": [{"folderId": "1", "name": "Analytics"}],
            "templates": [],
            "variables": [],
            "triggers": [],
            "tags": [{"tagId": "88", "name": "GA4 Tag"}],
            "built_in_variables": [],
        }
    )
    client = FakeImportClient()
    writer = FakeImportWriter()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    refused = server.apply_container_import(
        "1",
        "2",
        "3",
        "GTM-SRC_v5.json",
        mode="merge",
        conflict="overwrite",
        confirm=False,
    )
    assert refused["ok"] is False
    assert "confirm=true" in refused["message"]
    assert refused["counts"]["update"] == 2
    assert refused["counts"]["delete"] == 0
    assert client.calls == []
    assert writer.calls == []

    applied = server.apply_container_import(
        "1",
        "2",
        "3",
        "GTM-SRC_v5.json",
        mode="merge",
        conflict="overwrite",
        confirm=True,
    )
    assert applied["errors"] == []
    assert [item["name"] for item in applied["updated"]["folder"]] == ["Analytics"]
    assert [item["name"] for item in applied["updated"]["tag"]] == ["GA4 Tag"]


def test_apply_container_import_creates_only_needs_no_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "GTM-SRC_v5.json").write_text(json.dumps(_sample_export()), encoding="utf-8")
    monkeypatch.setenv("GTM_EXPORT_DIR", str(export_dir))
    monkeypatch.setenv("GTM_ACCESS_MODE", "write")

    reader = FakeImportReader()
    client = FakeImportClient()
    writer = FakeImportWriter()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)

    result = server.apply_container_import(
        "1",
        "2",
        "3",
        "GTM-SRC_v5.json",
        mode="merge",
        conflict="overwrite",
        confirm=False,
    )
    assert result["errors"] == []
    assert len(result["created"]["tag"]) == 1
    assert any(call[0] == "create_entity" for call in client.calls)
