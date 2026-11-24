from __future__ import annotations

import copy
from typing import Any

import pytest

from tag_manager_mcp.importer import SERVER_ASSIGNED_FIELDS, apply_import, plan_import

ENTITY_ID_FIELDS: dict[str, str] = {
    "folders": "folderId",
    "templates": "templateId",
    "variables": "variableId",
    "triggers": "triggerId",
    "tags": "tagId",
}


class FakeImportOps:
    """In-memory fake ops recording call order, bodies, and optional failures."""

    def __init__(self, fail_on: dict[tuple[str, str], Exception] | None = None) -> None:
        self._next_id = 1000
        self._fail_on = fail_on or {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.created_bodies: list[tuple[str, dict[str, Any]]] = []
        self.updated_bodies: list[tuple[str, str, dict[str, Any]]] = []
        self.deleted_ids: list[tuple[str, str]] = []
        self.enabled_builtins: list[str] = []

    def _alloc_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def create(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "")
        self.calls.append(("create", (kind, name)))
        if ("create", name) in self._fail_on:
            raise self._fail_on[("create", name)]
        self.created_bodies.append((kind, copy.deepcopy(body)))
        new_id = self._alloc_id()
        result = copy.deepcopy(body)
        result[ENTITY_ID_FIELDS[kind]] = new_id
        return result

    def update(self, kind: str, entity_id: str, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "")
        self.calls.append(("update", (kind, entity_id, name)))
        if ("update", name) in self._fail_on:
            raise self._fail_on[("update", name)]
        self.updated_bodies.append((kind, entity_id, copy.deepcopy(body)))
        result = copy.deepcopy(body)
        result[ENTITY_ID_FIELDS[kind]] = entity_id
        return result

    def delete(self, kind: str, entity_id: str) -> dict[str, Any]:
        self.calls.append(("delete", (kind, entity_id)))
        if ("delete", entity_id) in self._fail_on:
            raise self._fail_on[("delete", entity_id)]
        self.deleted_ids.append((kind, entity_id))
        return {}

    def enable_builtins(self, types: list[str]) -> list[dict[str, str]]:
        self.calls.append(("enable_builtins", tuple(types)))
        self.enabled_builtins.extend(types)
        return [{"type": item} for item in types]


def _make_sample_export() -> dict[str, Any]:
    """Build a synthetic GTM UI export with folders, template, variables, triggers, and tags."""
    return {
        "exportFormatVersion": 2,
        "exportTime": "2026-11-24 10:00:00",
        "containerVersion": {
            "accountId": "111",
            "containerId": "999",
            "containerVersionId": "42",
            "fingerprint": "ver-fp",
            "path": "accounts/111/containers/999/versions/42",
            "tagManagerUrl": "https://tagmanager.google.com/#/versions/42",
            "folder": [
                {
                    "accountId": "111",
                    "containerId": "999",
                    "workspaceId": "0",
                    "folderId": "1",
                    "name": "Analytics",
                    "fingerprint": "f1-fp",
                    "path": "accounts/111/containers/999/folders/1",
                    "tagManagerUrl": "https://tagmanager.google.com/f/1",
                },
                {
                    "accountId": "111",
                    "containerId": "999",
                    "folderId": "2",
                    "name": "Marketing",
                    "fingerprint": "f2-fp",
                },
            ],
            "customTemplate": [
                {
                    "accountId": "111",
                    "containerId": "999",
                    "templateId": "10",
                    "name": "Custom Pixel",
                    "parentFolderId": "2",
                    "templateData": "const log = require('logToConsole');",
                    "fingerprint": "tpl-fp",
                    "path": "accounts/111/containers/999/templates/10",
                    "tagManagerUrl": "https://tagmanager.google.com/tpl/10",
                }
            ],
            "variable": [
                {
                    "accountId": "111",
                    "containerId": "999",
                    "variableId": "20",
                    "name": "User ID",
                    "type": "v",
                    "parentFolderId": "1",
                    "fingerprint": "v1-fp",
                    "parameter": [
                        {"type": "INTEGER", "key": "dataLayerVersion", "value": "2"},
                        {"type": "TEMPLATE", "key": "name", "value": "user_id"},
                    ],
                },
                {
                    "variableId": "21",
                    "name": "Env Name",
                    "type": "c",
                    "parentFolderId": "1",
                    "parameter": [{"type": "TEMPLATE", "key": "value", "value": "prod"}],
                },
                {
                    "variableId": "22",
                    "name": "Template Var",
                    "type": "cvt_999_10",
                    "parentFolderId": "2",
                    "parameter": [{"type": "BOOLEAN", "key": "enabled", "value": "true"}],
                },
            ],
            "trigger": [
                {
                    "accountId": "111",
                    "containerId": "999",
                    "triggerId": "30",
                    "name": "Pageview Home",
                    "type": "PAGEVIEW",
                    "parentFolderId": "1",
                    "fingerprint": "tr1-fp",
                    "filter": [
                        {
                            "type": "CONTAINS",
                            "parameter": [
                                {"type": "TEMPLATE", "key": "arg0", "value": "{{Page URL}}"},
                                {"type": "TEMPLATE", "key": "arg1", "value": "/home"},
                            ],
                        }
                    ],
                },
                {
                    "triggerId": "31",
                    "name": "Signup Click",
                    "type": "CLICK",
                    "parentFolderId": "2",
                    "autoEventFilter": [
                        {
                            "type": "EQUALS",
                            "parameter": [
                                {"type": "TEMPLATE", "key": "arg0", "value": "{{Click Element}}"}
                            ],
                        }
                    ],
                },
                {
                    "triggerId": "32",
                    "name": "All Actions Group",
                    "type": "TRIGGER_GROUP",
                    "parentFolderId": "2",
                    "parameter": [
                        {
                            "type": "LIST",
                            "key": "triggerIds",
                            "list": [
                                {"type": "TRIGGER_REFERENCE", "value": "30"},
                                {"type": "TRIGGER_REFERENCE", "value": "31"},
                            ],
                        }
                    ],
                },
            ],
            "tag": [
                {
                    "accountId": "111",
                    "containerId": "999",
                    "tagId": "40",
                    "name": "Setup Consent",
                    "type": "gaawe",
                    "tagFiringOption": "ONCE_PER_EVENT",
                    "parentFolderId": "1",
                    "firingTriggerId": ["2147479553"],
                    "consentSettings": {"consentStatus": "NOT_NEEDED"},
                    "parameter": [{"type": "TEMPLATE", "key": "eventName", "value": "init"}],
                    "fingerprint": "t1-fp",
                    "path": "accounts/111/containers/999/tags/40",
                    "tagManagerUrl": "https://tagmanager.google.com/tag/40",
                },
                {
                    "tagId": "41",
                    "name": "Main Pixel",
                    "type": "cvt_999_10",
                    "tagFiringOption": "ONCE_PER_LOAD",
                    "parentFolderId": "2",
                    "firingTriggerId": ["32"],
                    "blockingTriggerId": ["2147479572", "31"],
                    "setupTag": [{"tagName": "Setup Consent", "stopOnSetupFailure": True}],
                    "parameter": [{"type": "TEMPLATE", "key": "pixelId", "value": "PX-1"}],
                },
                {
                    "tagId": "42",
                    "name": "Gallery Tag",
                    "type": "cvt_GALLERY_PUBLIC_ID",
                    "tagFiringOption": "UNLIMITED",
                    "firingTriggerId": ["30"],
                    "blockingTriggerId": ["2147479573", "2147480000"],
                },
            ],
            "builtInVariable": [
                {"type": "PAGE_URL", "name": "Page URL"},
                {"type": "CLICK_ELEMENT", "name": "Click Element"},
            ],
        },
    }


def test_plan_import_empty_workspace_creates_everything() -> None:
    export = _make_sample_export()
    snapshot = copy.deepcopy(export)

    plan = plan_import(export, existing={})

    assert export == snapshot  # input not mutated
    assert [item["name"] for item in plan["create"]["folder"]] == ["Analytics", "Marketing"]
    assert [item["name"] for item in plan["create"]["customTemplate"]] == ["Custom Pixel"]
    assert [item["name"] for item in plan["create"]["variable"]] == [
        "Env Name",
        "Template Var",
        "User ID",
    ]
    assert [item["name"] for item in plan["create"]["trigger"]] == [
        "All Actions Group",
        "Pageview Home",
        "Signup Click",
    ]
    assert [item["name"] for item in plan["create"]["tag"]] == [
        "Gallery Tag",
        "Main Pixel",
        "Setup Consent",
    ]
    for kind in ("folder", "customTemplate", "variable", "trigger", "tag"):
        assert plan["update"][kind] == []
        assert plan["delete"][kind] == []
        assert plan["skip"][kind] == []
    assert plan["enable_builtins"] == ["clickElement", "pageUrl"]
    assert plan["warnings"] == []


def test_plan_import_bare_container_version_accepted() -> None:
    bare_version = _make_sample_export()["containerVersion"]
    plan = plan_import(bare_version, existing={})
    assert len(plan["create"]["tag"]) == 3
    assert plan["enable_builtins"] == ["clickElement", "pageUrl"]


def test_plan_import_merge_skip_overwrite_rename_and_overwrite_mode() -> None:
    export = _make_sample_export()
    existing = {
        "folder": [
            {"folderId": "501", "name": "Analytics"},
            {"folderId": "502", "name": "Legacy Folder"},
        ],
        "tag": [
            {"tagId": "601", "name": "Setup Consent"},
            {"tagId": "602", "name": "Setup Consent (imported)"},
            {"tagId": "603", "name": "Old Tag"},
        ],
        "builtInVariable": [{"type": "pageUrl", "name": "Page URL"}],
    }

    skip_plan = plan_import(export, existing, mode="merge", conflict="skip")
    assert skip_plan["skip"]["folder"] == [
        {"name": "Analytics", "source_id": "1", "target_id": "501"}
    ]
    assert skip_plan["create"]["folder"] == [{"name": "Marketing", "source_id": "2"}]
    assert skip_plan["skip"]["tag"] == [
        {"name": "Setup Consent", "source_id": "40", "target_id": "601"}
    ]
    assert skip_plan["enable_builtins"] == ["clickElement"]
    assert skip_plan["delete"]["tag"] == []

    overwrite_conflict_plan = plan_import(export, existing, mode="merge", conflict="overwrite")
    assert overwrite_conflict_plan["update"]["folder"] == [
        {"name": "Analytics", "source_id": "1", "target_id": "501"}
    ]
    assert overwrite_conflict_plan["update"]["tag"] == [
        {"name": "Setup Consent", "source_id": "40", "target_id": "601"}
    ]
    assert overwrite_conflict_plan["delete"]["folder"] == []
    assert overwrite_conflict_plan["delete"]["tag"] == []

    rename_plan = plan_import(export, existing, mode="merge", conflict="rename")
    assert rename_plan["create"]["folder"] == [
        {"name": "Analytics (imported)", "source_id": "1"},
        {"name": "Marketing", "source_id": "2"},
    ]
    assert [item["name"] for item in rename_plan["create"]["tag"]] == [
        "Gallery Tag",
        "Main Pixel",
        "Setup Consent (imported 2)",
    ]
    assert rename_plan["skip"]["tag"] == []
    assert rename_plan["update"]["tag"] == []

    full_overwrite_plan = plan_import(export, existing, mode="overwrite")
    assert full_overwrite_plan["update"]["folder"] == [
        {"name": "Analytics", "source_id": "1", "target_id": "501"}
    ]
    assert full_overwrite_plan["delete"]["folder"] == [
        {"name": "Legacy Folder", "source_id": None, "target_id": "502"}
    ]
    assert full_overwrite_plan["delete"]["tag"] == [
        {"name": "Old Tag", "source_id": None, "target_id": "603"},
        {"name": "Setup Consent (imported)", "source_id": None, "target_id": "602"},
    ]


def test_plan_import_warnings_for_dangling_refs_and_unhandled_kinds() -> None:
    export = _make_sample_export()
    export["containerVersion"]["client"] = [{"clientId": "1", "name": "GA4 Client"}]
    export["containerVersion"]["gtagConfig"] = [{"gtagConfigId": "2", "type": "googtag"}]
    export["containerVersion"]["tag"][0]["firingTriggerId"].append("888")

    plan = plan_import(export, existing={})
    joined = "\n".join(plan["warnings"])
    assert "client" in joined
    assert "gtagConfig" in joined
    assert "888" in joined


def test_plan_import_invalid_inputs_raise_value_error() -> None:
    export = _make_sample_export()
    with pytest.raises(ValueError, match="mode"):
        plan_import(export, {}, mode="replace")
    with pytest.raises(ValueError, match="conflict"):
        plan_import(export, {}, mode="merge", conflict="fail")
    with pytest.raises(ValueError):
        plan_import({}, {})
    with pytest.raises(ValueError):
        plan_import({"foo": "bar"}, {})
    with pytest.raises(ValueError):
        plan_import({"exportFormatVersion": 1, "containerVersion": {}}, {})


def test_apply_import_remaps_ids_and_preserves_builtin_triggers_and_api_format() -> None:
    export = _make_sample_export()
    plan = plan_import(export, existing={})
    ops = FakeImportOps()

    result = apply_import(
        plan, export, ops, account_id="222", container_id="777", workspace_id="5"
    )

    assert result["errors"] == []

    # Execution order: builtins -> folders -> templates -> triggers -> variables -> tags
    call_stages = [
        call[0] if call[0] == "enable_builtins" else call[1][0] for call in ops.calls
    ]
    assert call_stages == [
        "enable_builtins",
        "folders",
        "folders",
        "templates",
        "triggers",
        "triggers",
        "triggers",
        "variables",
        "variables",
        "variables",
        "tags",
        "tags",
        "tags",
    ]
    # TriggerGroup must be created after its member triggers despite alphabetical name
    trigger_names = [
        call[1][1] for call in ops.calls if call[0] == "create" and call[1][0] == "triggers"
    ]
    assert trigger_names.index("All Actions Group") > trigger_names.index("Pageview Home")
    assert trigger_names.index("All Actions Group") > trigger_names.index("Signup Click")

    created_ids = {
        kind: {item["name"]: item["id"] for item in items}
        for kind, items in result["created"].items()
    }
    analytics_fid = created_ids["folder"]["Analytics"]
    marketing_fid = created_ids["folder"]["Marketing"]
    tpl_id = created_ids["customTemplate"]["Custom Pixel"]
    pv_tid = created_ids["trigger"]["Pageview Home"]
    click_tid = created_ids["trigger"]["Signup Click"]
    group_tid = created_ids["trigger"]["All Actions Group"]

    bodies_by_name = {
        (kind, body["name"]): body for kind, body in ops.created_bodies
    }

    # No server-assigned fields or entity IDs on any body sent to ops
    for kind, body in ops.created_bodies:
        for forbidden in SERVER_ASSIGNED_FIELDS | {ENTITY_ID_FIELDS[kind]}:
            assert forbidden not in body

    # Folder & custom template type remapping on variable and tag
    tpl_body = bodies_by_name[("templates", "Custom Pixel")]
    assert tpl_body["parentFolderId"] == marketing_fid

    tpl_var_body = bodies_by_name[("variables", "Template Var")]
    assert tpl_var_body["parentFolderId"] == marketing_fid
    assert tpl_var_body["type"] == f"cvt_777_{tpl_id}"
    assert tpl_var_body["parameter"][0]["type"] == "boolean"

    # LowerCamelCase enums on trigger & trigger group reference remapping
    pv_body = bodies_by_name[("triggers", "Pageview Home")]
    assert pv_body["type"] == "pageview"
    assert pv_body["filter"][0]["type"] == "contains"

    group_body = bodies_by_name[("triggers", "All Actions Group")]
    assert group_body["type"] == "triggerGroup"
    assert group_body["parameter"][0]["type"] == "list"
    assert group_body["parameter"][0]["list"] == [
        {"type": "triggerReference", "value": pv_tid},
        {"type": "triggerReference", "value": click_tid},
    ]

    # Built-in trigger IDs untouched, custom triggers remapped, gallery cvt_ untouched
    setup_body = bodies_by_name[("tags", "Setup Consent")]
    assert setup_body["parentFolderId"] == analytics_fid
    assert setup_body["tagFiringOption"] == "oncePerEvent"
    assert setup_body["consentSettings"]["consentStatus"] == "notNeeded"
    assert setup_body["firingTriggerId"] == ["2147479553"]

    main_body = bodies_by_name[("tags", "Main Pixel")]
    assert main_body["type"] == f"cvt_777_{tpl_id}"
    assert main_body["tagFiringOption"] == "oncePerLoad"
    assert main_body["firingTriggerId"] == [group_tid]
    assert main_body["blockingTriggerId"] == ["2147479572", click_tid]
    assert main_body["setupTag"] == [{"tagName": "Setup Consent", "stopOnSetupFailure": True}]

    gallery_body = bodies_by_name[("tags", "Gallery Tag")]
    assert gallery_body["type"] == "cvt_GALLERY_PUBLIC_ID"
    assert gallery_body["tagFiringOption"] == "unlimited"
    assert gallery_body["firingTriggerId"] == [pv_tid]
    assert gallery_body["blockingTriggerId"] == ["2147479573", "2147480000"]


def test_apply_import_updates_setup_tag_name_on_rename_and_deletes_in_reverse_order() -> None:
    export = _make_sample_export()
    existing = {
        "folder": [{"folderId": "501", "name": "Analytics"}],
        "tag": [{"tagId": "601", "name": "Setup Consent"}],
    }
    rename_plan = plan_import(export, existing, mode="merge", conflict="rename")
    rename_ops = FakeImportOps()
    rename_result = apply_import(rename_plan, export, rename_ops, "1", "777", "5")
    assert rename_result["errors"] == []

    main_body = next(
        body
        for kind, body in rename_ops.created_bodies
        if kind == "tags" and body["name"] == "Main Pixel"
    )
    assert main_body["setupTag"] == [
        {"tagName": "Setup Consent (imported)", "stopOnSetupFailure": True}
    ]

    existing_extra = {
        "folder": [
            {"folderId": "501", "name": "Analytics"},
            {"folderId": "502", "name": "Extra Folder"},
        ],
        "trigger": [{"triggerId": "701", "name": "Extra Trigger"}],
        "tag": [
            {"tagId": "601", "name": "Setup Consent"},
            {"tagId": "602", "name": "Extra Tag"},
        ],
    }
    ow_plan = plan_import(export, existing_extra, mode="overwrite")
    ow_ops = FakeImportOps()
    ow_result = apply_import(ow_plan, export, ow_ops, "1", "777", "5")
    assert ow_result["errors"] == []
    assert ow_ops.deleted_ids == [
        ("tags", "602"),
        ("triggers", "701"),
        ("folders", "502"),
    ]
    assert [kind for kind, _, _ in ow_ops.updated_bodies] == ["folders", "tags"]
    for kind, _eid, body in ow_ops.updated_bodies:
        for forbidden in SERVER_ASSIGNED_FIELDS | {ENTITY_ID_FIELDS[kind]}:
            assert forbidden not in body


def test_apply_import_records_error_and_continues() -> None:
    export = _make_sample_export()
    plan = plan_import(export, existing={})
    ops = FakeImportOps(fail_on={("create", "Custom Pixel"): RuntimeError("template rejected")})

    result = apply_import(plan, export, ops, "1", "777", "5")

    assert result["errors"] == [
        {"kind": "customTemplate", "name": "Custom Pixel", "error": "template rejected"},
        {
            "kind": "variable",
            "name": "Template Var",
            "error": "depends on customTemplate 'Custom Pixel' which was not imported",
        },
        {
            "kind": "tag",
            "name": "Main Pixel",
            "error": "depends on customTemplate 'Custom Pixel' which was not imported",
        },
    ]
    # Dependent entities (Template Var, Main Pixel) were skipped; unrelated entities succeeded
    assert len(result["created"]["folder"]) == 2
    assert len(result["created"]["customTemplate"]) == 0
    assert [item["name"] for item in result["created"]["variable"]] == ["Env Name", "User ID"]
    assert len(result["created"]["trigger"]) == 3
    assert [item["name"] for item in result["created"]["tag"]] == ["Gallery Tag", "Setup Consent"]


def test_apply_import_remaps_variable_trigger_ids_and_trigger_references() -> None:
    export = {
        "exportFormatVersion": 2,
        "containerVersion": {
            "trigger": [
                {"triggerId": "5", "name": "T", "type": "PAGEVIEW"},
                {"triggerId": "6", "name": "D", "type": "CLICK"},
            ],
            "variable": [
                {
                    "variableId": "10",
                    "name": "CondVar",
                    "type": "v",
                    "enablingTriggerId": ["5"],
                    "disablingTriggerId": ["6", "2147479553"],
                    "parameter": [
                        {"type": "TRIGGER_REFERENCE", "key": "linkedTrigger", "value": "5"}
                    ],
                }
            ],
        },
    }
    existing = {"trigger": [{"triggerId": "5", "name": "Other", "type": "pageview"}]}
    plan = plan_import(export, existing)
    ops = FakeImportOps()

    result = apply_import(plan, export, ops, "1", "777", "2")

    assert result["errors"] == []
    created_triggers = {item["name"]: item["id"] for item in result["created"]["trigger"]}
    assert created_triggers["T"] != "5"
    assert created_triggers["D"] != "6"
    var_body = next(body for kind, body in ops.created_bodies if kind == "variables")
    assert var_body["enablingTriggerId"] == [created_triggers["T"]]
    assert var_body["disablingTriggerId"] == [created_triggers["D"], "2147479553"]
    assert var_body["parameter"] == [
        {"type": "triggerReference", "key": "linkedTrigger", "value": created_triggers["T"]}
    ]


def test_apply_import_skips_dependents_when_trigger_create_fails() -> None:
    export = {
        "exportFormatVersion": 2,
        "containerVersion": {
            "folder": [{"folderId": "1", "name": "F"}],
            "trigger": [
                {"triggerId": "5", "name": "T", "type": "PAGEVIEW"},
                {"triggerId": "6", "name": "T2", "type": "PAGEVIEW"},
            ],
            "variable": [
                {
                    "variableId": "10",
                    "name": "DepVar",
                    "type": "v",
                    "enablingTriggerId": ["5"],
                },
                {"variableId": "11", "name": "FreeVar", "type": "c", "parentFolderId": "1"},
            ],
            "tag": [
                {
                    "tagId": "20",
                    "name": "DepTag",
                    "type": "html",
                    "firingTriggerId": ["5"],
                },
                {
                    "tagId": "21",
                    "name": "FreeTag",
                    "type": "html",
                    "firingTriggerId": ["6"],
                },
            ],
        },
    }
    plan = plan_import(export, existing={})
    ops = FakeImportOps(fail_on={("create", "T"): RuntimeError("trigger rejected")})

    result = apply_import(plan, export, ops, "1", "777", "2")

    assert result["errors"] == [
        {"kind": "trigger", "name": "T", "error": "trigger rejected"},
        {
            "kind": "variable",
            "name": "DepVar",
            "error": "depends on trigger 'T' which was not imported",
        },
        {
            "kind": "tag",
            "name": "DepTag",
            "error": "depends on trigger 'T' which was not imported",
        },
    ]
    assert [item["name"] for item in result["created"]["folder"]] == ["F"]
    assert [item["name"] for item in result["created"]["trigger"]] == ["T2"]
    assert [item["name"] for item in result["created"]["variable"]] == ["FreeVar"]
    assert [item["name"] for item in result["created"]["tag"]] == ["FreeTag"]
    assert [name for _, (_, name) in ops.calls] == ["F", "T", "T2", "FreeVar", "FreeTag"]


def test_apply_import_blocks_update_and_deletes_all_kinds_in_reverse_order() -> None:
    export = {
        "exportFormatVersion": 2,
        "containerVersion": {
            "trigger": [{"triggerId": "5", "name": "NewTrig", "type": "PAGEVIEW"}],
            "variable": [
                {
                    "variableId": "10",
                    "name": "ExistingVar",
                    "type": "v",
                    "enablingTriggerId": ["5"],
                },
                {
                    "variableId": "11",
                    "name": "DanglingVar",
                    "type": "v",
                    "parentFolderId": "999",
                },
            ],
        },
    }
    existing = {
        "folder": [{"folderId": "101", "name": "OldFolder"}],
        "customTemplate": [{"templateId": "102", "name": "OldTpl"}],
        "trigger": [{"triggerId": "103", "name": "OldTrig"}],
        "variable": [
            {"variableId": "104", "name": "ExistingVar"},
            {"variableId": "106", "name": "OldVar"},
        ],
        "tag": [{"tagId": "105", "name": "OldTag"}],
    }
    plan = plan_import(export, existing, mode="overwrite")
    ops = FakeImportOps(fail_on={("create", "NewTrig"): RuntimeError("boom")})

    result = apply_import(plan, export, ops, "1", "777", "2")

    assert result["errors"] == [
        {"kind": "trigger", "name": "NewTrig", "error": "boom"},
        {
            "kind": "variable",
            "name": "DanglingVar",
            "error": "depends on folder '999' which was not imported",
        },
        {
            "kind": "variable",
            "name": "ExistingVar",
            "error": "depends on trigger 'NewTrig' which was not imported",
        },
    ]
    assert result["created"]["variable"] == []
    assert result["updated"]["variable"] == []
    assert ops.updated_bodies == []
    assert ops.deleted_ids == [
        ("tags", "105"),
        ("variables", "106"),
        ("triggers", "103"),
        ("templates", "102"),
        ("folders", "101"),
    ]


def test_apply_import_remaps_tag_reference_parameters_on_rename() -> None:
    export = {
        "exportFormatVersion": 2,
        "containerVersion": {
            "tag": [
                {"tagId": "1", "name": "Base Tag", "type": "html"},
                {"tagId": "2", "name": "Unchanged Tag", "type": "html"},
                {
                    "tagId": "3",
                    "name": "Dependent Tag",
                    "type": "html",
                    "parameter": [
                        {"type": "TAG_REFERENCE", "key": "primaryTag", "value": "Base Tag"},
                        {
                            "type": "TAG_REFERENCE",
                            "key": "fallbackTag",
                            "value": "Unchanged Tag",
                        },
                        {
                            "type": "LIST",
                            "key": "nestedTags",
                            "list": [
                                {
                                    "type": "MAP",
                                    "map": [
                                        {
                                            "type": "TAG_REFERENCE",
                                            "key": "target",
                                            "value": "Base Tag",
                                        },
                                        {
                                            "type": "TAG_REFERENCE",
                                            "key": "secondary",
                                            "value": "Unchanged Tag",
                                        },
                                    ],
                                }
                            ],
                        },
                    ],
                },
            ]
        },
    }
    existing = {"tag": [{"tagId": "900", "name": "Base Tag"}]}
    plan = plan_import(export, existing, mode="merge", conflict="rename")
    ops = FakeImportOps()

    result = apply_import(plan, export, ops, "1", "777", "2")

    assert result["errors"] == []
    dep_body = next(
        body
        for kind, body in ops.created_bodies
        if kind == "tags" and body["name"] == "Dependent Tag"
    )
    assert dep_body["parameter"] == [
        {"type": "tagReference", "key": "primaryTag", "value": "Base Tag (imported)"},
        {"type": "tagReference", "key": "fallbackTag", "value": "Unchanged Tag"},
        {
            "type": "list",
            "key": "nestedTags",
            "list": [
                {
                    "type": "map",
                    "map": [
                        {"type": "tagReference", "key": "target", "value": "Base Tag (imported)"},
                        {"type": "tagReference", "key": "secondary", "value": "Unchanged Tag"},
                    ],
                }
            ],
        },
    ]


def test_apply_import_remaps_nested_trigger_reference_in_tag_map_list() -> None:
    export = {
        "exportFormatVersion": 2,
        "containerVersion": {
            "trigger": [{"triggerId": "5", "name": "Custom Trig", "type": "PAGEVIEW"}],
            "tag": [
                {
                    "tagId": "10",
                    "name": "Tag With Nested TriggerRef",
                    "type": "html",
                    "parameter": [
                        {
                            "type": "MAP",
                            "key": "config",
                            "map": [
                                {
                                    "type": "LIST",
                                    "key": "triggers",
                                    "list": [{"type": "TRIGGER_REFERENCE", "value": "5"}],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    }
    existing = {"trigger": [{"triggerId": "5", "name": "Existing Trig", "type": "pageview"}]}
    plan = plan_import(export, existing)
    ops = FakeImportOps()

    result = apply_import(plan, export, ops, "1", "777", "2")

    assert result["errors"] == []
    new_trig_id = result["created"]["trigger"][0]["id"]
    assert new_trig_id != "5"
    tag_body = next(body for kind, body in ops.created_bodies if kind == "tags")
    assert tag_body["parameter"] == [
        {
            "type": "map",
            "key": "config",
            "map": [
                {
                    "type": "list",
                    "key": "triggers",
                    "list": [{"type": "triggerReference", "value": new_trig_id}],
                }
            ],
        }
    ]


def test_apply_import_orders_triggers_with_trigger_reference_parameters() -> None:
    export = {
        "exportFormatVersion": 2,
        "containerVersion": {
            "trigger": [
                {
                    "triggerId": "1",
                    "name": "Alpha Trigger",
                    "type": "CUSTOM_EVENT",
                    "parameter": [
                        {"type": "TRIGGER_REFERENCE", "key": "linkedTrigger", "value": "2"}
                    ],
                },
                {"triggerId": "2", "name": "Zebra Trigger", "type": "PAGEVIEW"},
            ]
        },
    }
    plan = plan_import(export, existing={})
    ops = FakeImportOps()

    result = apply_import(plan, export, ops, "1", "777", "2")

    assert result["errors"] == []
    assert [item["name"] for item in result["created"]["trigger"]] == [
        "Zebra Trigger",
        "Alpha Trigger",
    ]
    zebra_id = result["created"]["trigger"][0]["id"]
    alpha_body = next(
        body
        for kind, body in ops.created_bodies
        if kind == "triggers" and body["name"] == "Alpha Trigger"
    )
    assert alpha_body["parameter"] == [
        {"type": "triggerReference", "key": "linkedTrigger", "value": zebra_id}
    ]


def test_apply_import_trigger_reference_cycle_does_not_loop_and_reports_errors() -> None:
    export = {
        "exportFormatVersion": 2,
        "containerVersion": {
            "trigger": [
                {
                    "triggerId": "1",
                    "name": "Alpha Trigger",
                    "type": "CUSTOM_EVENT",
                    "parameter": [
                        {"type": "TRIGGER_REFERENCE", "key": "linkedTrigger", "value": "2"}
                    ],
                },
                {
                    "triggerId": "2",
                    "name": "Zebra Trigger",
                    "type": "CUSTOM_EVENT",
                    "parameter": [
                        {"type": "TRIGGER_REFERENCE", "key": "linkedTrigger", "value": "1"}
                    ],
                },
            ]
        },
    }
    plan = plan_import(export, existing={})
    ops = FakeImportOps()

    result = apply_import(plan, export, ops, "1", "777", "2")

    assert result["created"]["trigger"] == []
    assert result["errors"] == [
        {
            "kind": "trigger",
            "name": "Alpha Trigger",
            "error": "depends on trigger 'Zebra Trigger' which was not imported",
        },
        {
            "kind": "trigger",
            "name": "Zebra Trigger",
            "error": "depends on trigger 'Alpha Trigger' which was not imported",
        },
    ]


def test_import_treats_parent_folder_id_zero_as_no_folder() -> None:
    export = {
        "exportFormatVersion": 2,
        "containerVersion": {
            "variable": [
                {
                    "variableId": "10",
                    "name": "Root Var",
                    "type": "c",
                    "parentFolderId": "0",
                    "parameter": [{"type": "TEMPLATE", "key": "value", "value": "ok"}],
                }
            ],
            "trigger": [
                {
                    "triggerId": "20",
                    "name": "Root Trigger",
                    "type": "PAGEVIEW",
                    "parentFolderId": "0",
                }
            ],
            "tag": [
                {
                    "tagId": "30",
                    "name": "Root Tag",
                    "type": "html",
                    "parentFolderId": "0",
                    "firingTriggerId": ["20"],
                }
            ],
        },
    }
    plan = plan_import(export, existing={})
    assert plan["warnings"] == []

    ops = FakeImportOps()
    result = apply_import(plan, export, ops, "1", "777", "2")

    assert result["errors"] == []
    assert [item["name"] for item in result["created"]["trigger"]] == ["Root Trigger"]
    assert [item["name"] for item in result["created"]["variable"]] == ["Root Var"]
    assert [item["name"] for item in result["created"]["tag"]] == ["Root Tag"]
    assert len(ops.created_bodies) == 3
    for _kind, body in ops.created_bodies:
        assert "parentFolderId" not in body
