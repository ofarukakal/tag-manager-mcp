"""Schema-driven conversion between GTM API v2 camelCase enums and UI export UPPER_SNAKE_CASE."""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_LOWER_TO_UPPER_RE = re.compile(r"(?<!^)(?=[A-Z])")
_UI_ENUM_OVERRIDES: dict[str, str] = {
    "androidSdk5": "ANDROID_SDK_5",
    "iosSdk5": "IOS_SDK_5",
}


def _discovery_doc_path() -> Path:
    try:
        import googleapiclient
    except Exception as exc:
        raise RuntimeError("googleapiclient is not installed; cannot load GTM schema") from exc

    package_file = getattr(googleapiclient, "__file__", None)
    if not package_file:
        raise RuntimeError("Cannot locate googleapiclient package directory for GTM schema")

    return Path(package_file).parent / "discovery_cache" / "documents" / "tagmanager.v2.json"


@lru_cache(maxsize=1)
def _load_schemas() -> dict[str, Any]:
    path = _discovery_doc_path()
    if not path.is_file():
        raise RuntimeError(f"GTM discovery document not found: {path}")

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Failed to parse GTM discovery document {path}: {exc}") from exc

    schemas = doc.get("schemas") if isinstance(doc, dict) else None
    if not isinstance(schemas, dict) or "ContainerVersion" not in schemas:
        raise RuntimeError(f"GTM discovery document {path} is missing ContainerVersion schema")
    return schemas


def _enum_to_ui(value: str) -> str:
    if value in _UI_ENUM_OVERRIDES:
        return _UI_ENUM_OVERRIDES[value]
    return _LOWER_TO_UPPER_RE.sub("_", value).upper()


@lru_cache(maxsize=64)
def _ui_to_api_map(enum_values: tuple[str, ...]) -> dict[str, str]:
    return {_enum_to_ui(item): item for item in enum_values}


def _resolve_schema(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    current = schema
    while "$ref" in current:
        ref_name = current["$ref"]
        if not isinstance(ref_name, str) or ref_name in seen:
            return current
        seen.add(ref_name)
        target = schemas.get(ref_name)
        if not isinstance(target, dict):
            return current
        current = target
    return current


def _convert_value(
    value: Any, schema: dict[str, Any], schemas: dict[str, Any], *, to_ui: bool
) -> Any:
    resolved = _resolve_schema(schema, schemas)

    enum_values = resolved.get("enum")
    if isinstance(enum_values, list) and isinstance(value, str):
        if to_ui:
            return _enum_to_ui(value) if value in enum_values else value
        reverse = _ui_to_api_map(tuple(str(item) for item in enum_values))
        return reverse.get(value, value)

    if isinstance(value, list):
        items_schema = resolved.get("items")
        if isinstance(items_schema, dict):
            return [
                _convert_value(item, items_schema, schemas, to_ui=to_ui)
                for item in value
            ]
        return value

    if isinstance(value, dict):
        properties = resolved.get("properties")
        if isinstance(properties, dict):
            for key, prop_schema in properties.items():
                if key in value and isinstance(prop_schema, dict):
                    value[key] = _convert_value(
                        value[key], prop_schema, schemas, to_ui=to_ui
                    )
        return value

    return value


def to_ui_export(version: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a ContainerVersion with enum fields in GTM UI UPPER_SNAKE_CASE."""
    schemas = _load_schemas()
    copied = copy.deepcopy(version)
    if not isinstance(copied, dict):
        return copied
    return _convert_value(copied, schemas["ContainerVersion"], schemas, to_ui=True)


def to_api_form(version: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of a ContainerVersion with UI enum fields converted back to API form."""
    schemas = _load_schemas()
    copied = copy.deepcopy(version)
    if not isinstance(copied, dict):
        return copied
    return _convert_value(copied, schemas["ContainerVersion"], schemas, to_ui=False)
