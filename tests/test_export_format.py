from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from tag_manager_mcp import export_format
from tag_manager_mcp.export_format import to_api_form, to_ui_export


def test_to_ui_export_converts_all_observed_enum_pairs_and_nested_parameters() -> None:
    raw: dict[str, Any] = {
        "containerVersionId": "10",
        "container": {
            "publicId": "GTM-TEST",
            "usageContext": ["web"],
        },
        "tag": [
            {
                "name": "GA4 Event",
                "type": "gaawe",
                "tagFiringOption": "oncePerEvent",
                "priority": {"type": "integer", "value": "10"},
                "monitoringMetadata": {
                    "type": "map",
                    "map": [
                        {"type": "template", "key": "k", "value": "v"},
                    ],
                },
                "consentSettings": {
                    "consentStatus": "notNeeded",
                    "consentType": {
                        "type": "list",
                        "list": [
                            {"type": "template", "value": "analytics_storage"},
                        ],
                    },
                },
                "parameter": [
                    {"type": "template", "key": "eventName", "value": "purchase"},
                    {"type": "boolean", "key": "sendEcommerceData", "value": "true"},
                    {"type": "integer", "key": "maxItems", "value": "5"},
                    {
                        "type": "list",
                        "key": "eventParameters",
                        "list": [
                            {
                                "type": "map",
                                "map": [
                                    {"type": "template", "key": "name", "value": "currency"},
                                    {"type": "boolean", "key": "enabled", "value": "true"},
                                ],
                            }
                        ],
                    },
                ],
            },
            {
                "name": "Custom HTML",
                "type": "html",
                "tagFiringOption": "oncePerLoad",
                "consentSettings": {"consentStatus": "notSet"},
            },
            {
                "name": "Template Tag",
                "type": "cvt_123_45",
                "consentSettings": {"consentStatus": "needed"},
            },
        ],
        "trigger": [
            {
                "name": "Custom Event",
                "type": "customEvent",
                "waitForTags": {"type": "boolean", "value": "true"},
                "checkValidation": {"type": "boolean", "value": "false"},
                "uniqueTriggerId": {"type": "template", "value": "trig-1"},
                "waitForTagsTimeout": {"type": "integer", "value": "2000"},
                "parameter": [{"type": "template", "key": "event", "value": "signup"}],
                "customEventFilter": [
                    {
                        "type": "equals",
                        "parameter": [{"type": "template", "key": "arg0", "value": "{{_event}}"}],
                    }
                ],
                "filter": [
                    {
                        "type": "contains",
                        "parameter": [{"type": "template", "key": "arg0", "value": "{{Page URL}}"}],
                    },
                    {
                        "type": "matchRegex",
                        "parameter": [
                            {"type": "template", "key": "arg0", "value": "{{Page Path}}"}
                        ],
                    },
                ],
                "autoEventFilter": [
                    {
                        "type": "cssSelector",
                        "parameter": [
                            {"type": "template", "key": "arg0", "value": "{{Click Element}}"}
                        ],
                    },
                    {
                        "type": "startsWith",
                        "parameter": [
                            {"type": "template", "key": "arg0", "value": "{{Click URL}}"}
                        ],
                    },
                ],
            },
            {"name": "Visibility", "type": "elementVisibility"},
            {"name": "All Pages", "type": "pageview"},
            {"name": "Clicks", "type": "click"},
            {"name": "Link Clicks", "type": "linkClick"},
            {"name": "JS Error", "type": "jsError"},
            {"name": "YouTube", "type": "youTubeVideo"},
        ],
        "variable": [
            {
                "name": "DLV",
                "type": "v",
                "parameter": [{"type": "integer", "key": "dataLayerVersion", "value": "2"}],
            },
            {"name": "JS", "type": "jsm"},
            {"name": "Const", "type": "c"},
        ],
        "builtInVariable": [
            {"type": "pageUrl", "name": "Page URL"},
            {"type": "clickElement", "name": "Click Element"},
            {"type": "formId", "name": "Form ID"},
        ],
        "client": [{"name": "GA4 Client", "type": "ga4_client"}],
        "transformation": [{"name": "Allow", "type": "allow_params"}],
        "customTemplate": [
            {
                "templateId": "1",
                "name": "Custom",
                "templateData": '{"type":"template"}',
            }
        ],
    }
    snapshot = copy.deepcopy(raw)

    ui = to_ui_export(raw)

    # Original input must not be mutated
    assert raw == snapshot

    # Container.usageContext
    assert ui["container"]["usageContext"] == ["WEB"]

    # Tag fields & Parameter.type everywhere
    t0, t1, t2 = ui["tag"]
    assert t0["type"] == "gaawe"
    assert t0["tagFiringOption"] == "ONCE_PER_EVENT"
    assert t0["priority"]["type"] == "INTEGER"
    assert t0["monitoringMetadata"]["type"] == "MAP"
    assert t0["monitoringMetadata"]["map"][0]["type"] == "TEMPLATE"
    assert t0["consentSettings"]["consentStatus"] == "NOT_NEEDED"
    assert t0["consentSettings"]["consentType"]["type"] == "LIST"
    assert t0["consentSettings"]["consentType"]["list"][0]["type"] == "TEMPLATE"
    assert [p["type"] for p in t0["parameter"][:3]] == ["TEMPLATE", "BOOLEAN", "INTEGER"]
    assert t0["parameter"][3]["type"] == "LIST"
    assert t0["parameter"][3]["list"][0]["type"] == "MAP"
    assert [p["type"] for p in t0["parameter"][3]["list"][0]["map"]] == ["TEMPLATE", "BOOLEAN"]

    assert t1["type"] == "html"
    assert t1["tagFiringOption"] == "ONCE_PER_LOAD"
    assert t1["consentSettings"]["consentStatus"] == "NOT_SET"
    assert t2["type"] == "cvt_123_45"
    assert t2["consentSettings"]["consentStatus"] == "NEEDED"

    # Trigger.type, trigger Parameter fields, and Condition.type
    tr0 = ui["trigger"][0]
    assert tr0["type"] == "CUSTOM_EVENT"
    assert tr0["waitForTags"]["type"] == "BOOLEAN"
    assert tr0["checkValidation"]["type"] == "BOOLEAN"
    assert tr0["uniqueTriggerId"]["type"] == "TEMPLATE"
    assert tr0["waitForTagsTimeout"]["type"] == "INTEGER"
    assert tr0["parameter"][0]["type"] == "TEMPLATE"
    assert tr0["customEventFilter"][0]["type"] == "EQUALS"
    assert tr0["customEventFilter"][0]["parameter"][0]["type"] == "TEMPLATE"
    assert [c["type"] for c in tr0["filter"]] == ["CONTAINS", "MATCH_REGEX"]
    assert [c["type"] for c in tr0["autoEventFilter"]] == ["CSS_SELECTOR", "STARTS_WITH"]
    assert [tr["type"] for tr in ui["trigger"][1:]] == [
        "ELEMENT_VISIBILITY",
        "PAGEVIEW",
        "CLICK",
        "LINK_CLICK",
        "JS_ERROR",
        "YOU_TUBE_VIDEO",
    ]

    # Variable, BuiltInVariable, Client, Transformation, CustomTemplate
    assert ui["variable"][0]["type"] == "v"
    assert ui["variable"][0]["parameter"][0]["type"] == "INTEGER"
    assert ui["variable"][1]["type"] == "jsm"
    assert ui["variable"][2]["type"] == "c"
    assert [b["type"] for b in ui["builtInVariable"]] == ["PAGE_URL", "CLICK_ELEMENT", "FORM_ID"]
    assert ui["client"][0]["type"] == "ga4_client"
    assert ui["transformation"][0]["type"] == "allow_params"
    assert ui["customTemplate"][0]["templateData"] == '{"type":"template"}'

    # Round-trip back to API form
    assert to_api_form(ui) == raw


def test_digit_overrides_and_unknown_enum_values() -> None:
    version = {
        "container": {
            "usageContext": ["androidSdk5", "iosSdk5", "unknownContext"],
        },
        "builtInVariable": [
            {"type": "firebaseEventParameterCampaignCp1"},
            {"type": "futureCustomBuiltIn"},
        ],
        "trigger": [
            {"type": "someFutureTriggerType"},
        ],
        "tag": [
            {
                "type": "gaawe",
                "parameter": [{"type": "futureParamType", "key": "k", "value": "v"}],
            }
        ],
    }

    ui = to_ui_export(version)
    assert ui["container"]["usageContext"] == [
        "ANDROID_SDK_5",
        "IOS_SDK_5",
        "unknownContext",
    ]
    assert ui["builtInVariable"] == [
        {"type": "FIREBASE_EVENT_PARAMETER_CAMPAIGN_CP1"},
        {"type": "futureCustomBuiltIn"},
    ]
    assert ui["trigger"] == [{"type": "someFutureTriggerType"}]
    assert ui["tag"][0]["parameter"][0]["type"] == "futureParamType"

    assert to_api_form(ui) == version

    # Unknown UI spelling is also preserved when converting back to API form
    ui_with_unknown = {
        "trigger": [{"type": "CUSTOM_EVENT"}, {"type": "FUTURE_UI_ENUM"}],
    }
    assert to_api_form(ui_with_unknown) == {
        "trigger": [{"type": "customEvent"}, {"type": "FUTURE_UI_ENUM"}],
    }


def test_missing_or_invalid_discovery_doc_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_format._load_schemas.cache_clear()
    try:
        missing = tmp_path / "missing.json"
        monkeypatch.setattr(export_format, "_discovery_doc_path", lambda: missing)
        with pytest.raises(RuntimeError, match="not found"):
            to_ui_export({"tag": []})

        export_format._load_schemas.cache_clear()
        invalid = tmp_path / "invalid.json"
        invalid.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(export_format, "_discovery_doc_path", lambda: invalid)
        with pytest.raises(RuntimeError, match="Failed to parse"):
            to_api_form({"tag": []})
    finally:
        export_format._load_schemas.cache_clear()
