"""Plan and apply GTM container export imports into a target workspace."""

from __future__ import annotations

import copy
import heapq
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from .export_format import to_api_form

PLAN_KINDS: tuple[str, ...] = (
    "folder",
    "customTemplate",
    "trigger",
    "variable",
    "tag",
)

ENTITY_ID_FIELDS: dict[str, str] = {
    "folder": "folderId",
    "customTemplate": "templateId",
    "variable": "variableId",
    "trigger": "triggerId",
    "tag": "tagId",
}

KIND_TO_OPS_KIND: dict[str, str] = {
    "folder": "folders",
    "customTemplate": "templates",
    "variable": "variables",
    "trigger": "triggers",
    "tag": "tags",
}

UNHANDLED_KINDS: tuple[str, ...] = (
    "client",
    "transformation",
    "zone",
    "gtagConfig",
)

SERVER_ASSIGNED_FIELDS: frozenset[str] = frozenset(
    {
        "accountId",
        "containerId",
        "workspaceId",
        "path",
        "fingerprint",
        "tagManagerUrl",
    }
)

VALID_MODES: frozenset[str] = frozenset({"merge", "overwrite"})
VALID_CONFLICTS: frozenset[str] = frozenset({"skip", "overwrite", "rename"})

BUILTIN_TRIGGER_MIN_ID = 2147479553
BUILTIN_TRIGGER_IDS: frozenset[str] = frozenset({"2147479553", "2147479572", "2147479573"})

_CVT_TYPE_RE = re.compile(r"^cvt_([0-9]+)_([0-9]+)$")
_IMPORTED_SUFFIX_RE = re.compile(r" \(imported(?: \d+)?\)$")

_CONTAINER_VERSION_KEYS: frozenset[str] = frozenset(
    {
        "accountId",
        "builtInVariable",
        "client",
        "container",
        "containerId",
        "containerVersionId",
        "customTemplate",
        "deleted",
        "description",
        "fingerprint",
        "folder",
        "gtagConfig",
        "name",
        "path",
        "tag",
        "tagManagerUrl",
        "transformation",
        "trigger",
        "variable",
        "zone",
    }
)

_VERSION_LIST_KEYS: tuple[str, ...] = (
    *PLAN_KINDS,
    "builtInVariable",
    *UNHANDLED_KINDS,
)


class ImportOps(Protocol):
    """Workspace write callbacks bound to the target workspace."""

    def create(self, kind: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def update(self, kind: str, entity_id: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def delete(self, kind: str, entity_id: str) -> dict[str, Any]: ...

    def enable_builtins(self, types: list[str]) -> Any: ...


@dataclass(frozen=True)
class _EntitySource:
    """Lookup tables for source entities in a container version."""

    by_id: dict[str, dict[str, dict[str, Any]]]
    by_name: dict[str, dict[str, dict[str, Any]]]


def _extract_api_version(export: Any) -> dict[str, Any]:
    """Validate an export or bare ContainerVersion dict and convert enums to API form."""
    if not isinstance(export, dict):
        raise ValueError("Container export must be a JSON object.")

    if "containerVersion" in export:
        if "exportFormatVersion" in export and export["exportFormatVersion"] != 2:
            raise ValueError(
                f"Unsupported exportFormatVersion: {export['exportFormatVersion']!r}"
            )
        raw_version = export["containerVersion"]
        if not isinstance(raw_version, dict):
            raise ValueError("export['containerVersion'] must be a JSON object.")
    else:
        raw_version = export
        if not any(key in _CONTAINER_VERSION_KEYS for key in raw_version):
            raise ValueError("Input dict is not a GTM container export or ContainerVersion.")

    for list_key in _VERSION_LIST_KEYS:
        if (
            list_key in raw_version
            and raw_version[list_key] is not None
            and not isinstance(raw_version[list_key], list)
        ):
            raise ValueError(f"ContainerVersion field '{list_key}' must be a list.")

    return to_api_form(raw_version)


def _is_builtin_trigger_id(trigger_id: Any) -> bool:
    """Return True for built-in GTM trigger IDs that must never be remapped."""
    text = str(trigger_id)
    if text in BUILTIN_TRIGGER_IDS:
        return True
    return text.isdigit() and int(text) >= BUILTIN_TRIGGER_MIN_ID


def _unique_import_name(base_name: str, used_names: set[str]) -> str:
    """Return a non-colliding entity name with ' (imported)' / ' (imported N)' suffix."""
    candidate = f"{base_name} (imported)"
    if candidate not in used_names:
        return candidate
    index = 2
    while True:
        candidate = f"{base_name} (imported {index})"
        if candidate not in used_names:
            return candidate
        index += 1


def _plan_entry_sort_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("name") or ""),
        str(entry.get("source_id") or ""),
        str(entry.get("target_id") or ""),
    )


def _extract_trigger_group_ids(trigger: dict[str, Any]) -> list[str]:
    """Return trigger IDs referenced by a triggerGroup's triggerIds parameter."""
    refs: list[str] = []
    for param in trigger.get("parameter") or []:
        if not isinstance(param, dict) or param.get("key") != "triggerIds":
            continue
        for item in param.get("list") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") in (None, "triggerReference") and item.get("value") is not None:
                refs.append(str(item["value"]))
    return refs


def _collect_warnings(version: dict[str, Any]) -> list[str]:
    """Collect deterministic warnings for unhandled kinds and dangling export references."""
    warnings: list[str] = []

    for kind in UNHANDLED_KINDS:
        items = version.get(kind) or []
        if items:
            warnings.append(
                f"Unhandled entity kind '{kind}' ({len(items)} item(s)) is present in the "
                "export and will not be imported."
            )

    folder_ids = {
        str(item["folderId"])
        for item in (version.get("folder") or [])
        if isinstance(item, dict) and item.get("folderId") is not None
    }
    template_ids = {
        str(item["templateId"])
        for item in (version.get("customTemplate") or [])
        if isinstance(item, dict) and item.get("templateId") is not None
    }
    trigger_ids = {
        str(item["triggerId"])
        for item in (version.get("trigger") or [])
        if isinstance(item, dict) and item.get("triggerId") is not None
    }
    tag_names = {
        str(item["name"])
        for item in (version.get("tag") or [])
        if isinstance(item, dict) and item.get("name")
    }

    for kind in ("customTemplate", "variable", "trigger", "tag"):
        for item in version.get(kind) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            parent_folder_id = item.get("parentFolderId")
            if (
                parent_folder_id is not None
                and str(parent_folder_id) not in ("", "0")
                and str(parent_folder_id) not in folder_ids
            ):
                warnings.append(
                    f"{kind} '{name}' references parentFolderId '{parent_folder_id}' "
                    "which is not in the export."
                )

    for var in version.get("variable") or []:
        if not isinstance(var, dict):
            continue
        name = str(var.get("name") or "")
        _warn_missing_cvt_template(warnings, "variable", name, var.get("type"), template_ids)
        for field in ("enablingTriggerId", "disablingTriggerId"):
            for tid in var.get(field) or []:
                if not _is_builtin_trigger_id(tid) and str(tid) not in trigger_ids:
                    warnings.append(
                        f"variable '{name}' {field} references trigger '{tid}' "
                        "which is not in the export."
                    )

    for trig in version.get("trigger") or []:
        if not isinstance(trig, dict):
            continue
        name = str(trig.get("name") or "")
        for tid in _extract_trigger_group_ids(trig):
            if not _is_builtin_trigger_id(tid) and str(tid) not in trigger_ids:
                warnings.append(
                    f"trigger '{name}' triggerGroup references trigger '{tid}' "
                    "which is not in the export."
                )

    for tag in version.get("tag") or []:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name") or "")
        _warn_missing_cvt_template(warnings, "tag", name, tag.get("type"), template_ids)
        for field in ("firingTriggerId", "blockingTriggerId"):
            for tid in tag.get(field) or []:
                if not _is_builtin_trigger_id(tid) and str(tid) not in trigger_ids:
                    warnings.append(
                        f"tag '{name}' {field} references trigger '{tid}' "
                        "which is not in the export."
                    )
        for field in ("setupTag", "teardownTag"):
            for ref in tag.get(field) or []:
                if not isinstance(ref, dict):
                    continue
                ref_name = ref.get("tagName")
                if ref_name and str(ref_name) not in tag_names:
                    warnings.append(
                        f"tag '{name}' {field} references tag '{ref_name}' "
                        "which is not in the export."
                    )

    return warnings


def _warn_missing_cvt_template(
    warnings: list[str],
    kind: str,
    name: str,
    entity_type: Any,
    template_ids: set[str],
) -> None:
    if not isinstance(entity_type, str):
        return
    match = _CVT_TYPE_RE.match(entity_type)
    if match and match.group(2) not in template_ids:
        warnings.append(
            f"{kind} '{name}' uses custom template '{entity_type}' whose templateId "
            f"'{match.group(2)}' is not in the export."
        )


def plan_import(
    export: dict[str, Any],
    existing: dict[str, Any],
    mode: str = "merge",
    conflict: str = "skip",
) -> dict[str, Any]:
    """Compute a deterministic import plan comparing a GTM export against workspace entities."""
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode {mode!r}; expected one of {sorted(VALID_MODES)}.")
    if conflict not in VALID_CONFLICTS:
        raise ValueError(
            f"Invalid conflict {conflict!r}; expected one of {sorted(VALID_CONFLICTS)}."
        )
    if not isinstance(existing, dict):
        raise ValueError("existing must be a dictionary of workspace entity lists.")

    version = _extract_api_version(export)
    effective_conflict = "overwrite" if mode == "overwrite" else conflict

    create: dict[str, list[dict[str, Any]]] = {kind: [] for kind in PLAN_KINDS}
    update: dict[str, list[dict[str, Any]]] = {kind: [] for kind in PLAN_KINDS}
    delete: dict[str, list[dict[str, Any]]] = {kind: [] for kind in PLAN_KINDS}
    skip: dict[str, list[dict[str, Any]]] = {kind: [] for kind in PLAN_KINDS}

    export_builtins = version.get("builtInVariable") or []
    existing_builtins = existing.get("builtInVariable") or []
    existing_builtin_types = {
        str(item["type"])
        for item in existing_builtins
        if isinstance(item, dict) and item.get("type")
    }
    needed_builtin_types = {
        str(item["type"])
        for item in export_builtins
        if isinstance(item, dict) and item.get("type")
    }
    enable_builtins = sorted(needed_builtin_types - existing_builtin_types)
    extra_warnings: list[str] = []

    for kind in PLAN_KINDS:
        id_field = ENTITY_ID_FIELDS[kind]
        export_items = sorted(
            (item for item in (version.get(kind) or []) if isinstance(item, dict)),
            key=lambda item: (str(item.get("name") or ""), str(item.get(id_field) or "")),
        )
        existing_items = [
            item for item in (existing.get(kind) or []) if isinstance(item, dict)
        ]

        existing_by_name: dict[str, dict[str, Any]] = {}
        for item in existing_items:
            item_name = str(item.get("name") or "")
            if item_name not in existing_by_name:
                existing_by_name[item_name] = item

        used_names = {str(item.get("name") or "") for item in existing_items}
        imported_names: set[str] = set()

        for item in export_items:
            name = str(item.get("name") or "")
            raw_source_id = item.get(id_field)
            source_id = str(raw_source_id) if raw_source_id is not None else None
            imported_names.add(name)

            existing_match = existing_by_name.get(name)
            if existing_match is None:
                chosen_name = (
                    name if name not in used_names else _unique_import_name(name, used_names)
                )
                used_names.add(chosen_name)
                create[kind].append({"name": chosen_name, "source_id": source_id})
                _check_renamed_template_warning(kind, item, name, chosen_name, extra_warnings)
                _check_gallery_template_warning(kind, item, chosen_name, extra_warnings)
                continue

            raw_target_id = existing_match.get(id_field)
            target_id = str(raw_target_id) if raw_target_id is not None else None

            if effective_conflict == "skip":
                skip[kind].append(
                    {"name": name, "source_id": source_id, "target_id": target_id}
                )
            elif effective_conflict == "overwrite":
                update[kind].append(
                    {"name": name, "source_id": source_id, "target_id": target_id}
                )
                _check_gallery_template_warning(kind, item, name, extra_warnings)
            else:
                new_name = _unique_import_name(name, used_names)
                used_names.add(new_name)
                create[kind].append({"name": new_name, "source_id": source_id})
                _check_renamed_template_warning(kind, item, name, new_name, extra_warnings)
                _check_gallery_template_warning(kind, item, new_name, extra_warnings)

        if mode == "overwrite":
            for item in existing_items:
                item_name = str(item.get("name") or "")
                if item_name in imported_names:
                    continue
                raw_target_id = item.get(id_field)
                target_id = str(raw_target_id) if raw_target_id is not None else None
                delete[kind].append(
                    {"name": item_name, "source_id": None, "target_id": target_id}
                )

        create[kind].sort(key=_plan_entry_sort_key)
        update[kind].sort(key=_plan_entry_sort_key)
        delete[kind].sort(key=_plan_entry_sort_key)
        delete[kind] = _order_deletes(kind, delete[kind], existing_items)
        skip[kind].sort(key=_plan_entry_sort_key)

    return {
        "create": create,
        "update": update,
        "delete": delete,
        "skip": skip,
        "enable_builtins": enable_builtins,
        "warnings": [*_collect_warnings(version), *extra_warnings],
    }


def _index_source_entities(version: dict[str, Any]) -> _EntitySource:
    by_id: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in PLAN_KINDS}
    by_name: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in PLAN_KINDS}
    for kind in PLAN_KINDS:
        id_field = ENTITY_ID_FIELDS[kind]
        for item in version.get(kind) or []:
            if not isinstance(item, dict):
                continue
            raw_id = item.get(id_field)
            if raw_id is not None:
                by_id[kind][str(raw_id)] = item
            name = str(item.get("name") or "")
            if name and name not in by_name[kind]:
                by_name[kind][name] = item
    return _EntitySource(by_id=by_id, by_name=by_name)


def _find_source_item(
    entry: dict[str, Any], kind: str, source: _EntitySource
) -> dict[str, Any] | None:
    source_id = entry.get("source_id")
    if source_id is not None and str(source_id) in source.by_id[kind]:
        return source.by_id[kind][str(source_id)]
    name = str(entry.get("name") or "")
    if name in source.by_name[kind]:
        return source.by_name[kind][name]
    base_name = _IMPORTED_SUFFIX_RE.sub("", name)
    if base_name in source.by_name[kind]:
        return source.by_name[kind][base_name]
    return None


def _order_entries_topologically(
    entries: list[dict[str, Any]],
    key_of: Callable[[dict[str, Any]], str | None],
    deps_of: Callable[[dict[str, Any]], list[str]],
) -> list[dict[str, Any]]:
    """Order entries so dependencies appear first, preserving original order for ties/cycles."""
    if len(entries) <= 1:
        return list(entries)

    key_to_idx: dict[str, int] = {}
    for idx, entry in enumerate(entries):
        key = key_of(entry)
        if key is not None and key not in key_to_idx:
            key_to_idx[key] = idx

    in_degree = [0] * len(entries)
    dependents: list[list[int]] = [[] for _ in entries]
    for idx, entry in enumerate(entries):
        seen_deps: set[int] = set()
        for dep_key in deps_of(entry):
            dep_idx = key_to_idx.get(dep_key)
            if dep_idx is not None and dep_idx != idx and dep_idx not in seen_deps:
                seen_deps.add(dep_idx)
                dependents[dep_idx].append(idx)
                in_degree[idx] += 1

    ready = [idx for idx, deg in enumerate(in_degree) if deg == 0]
    heapq.heapify(ready)
    ordered: list[dict[str, Any]] = []
    emitted = [False] * len(entries)
    while ready:
        idx = heapq.heappop(ready)
        emitted[idx] = True
        ordered.append(entries[idx])
        for dependent_idx in dependents[idx]:
            in_degree[dependent_idx] -= 1
            if in_degree[dependent_idx] == 0:
                heapq.heappush(ready, dependent_idx)

    for idx, entry in enumerate(entries):
        if not emitted[idx]:
            ordered.append(entry)
    return ordered


def _update_template_data_display_name(template_data: Any, new_name: str) -> str | None:
    """Update displayName in templateData's ___INFO___ JSON block, or return None if invalid."""
    if not isinstance(template_data, str):
        return None
    lines = template_data.splitlines(keepends=True)
    info_line_idx: int | None = None
    offset = 0
    block_start = 0
    block_end = len(template_data)
    for idx, line in enumerate(lines):
        if info_line_idx is None:
            if line.rstrip("\r\n") == "___INFO___":
                info_line_idx = idx
                block_start = offset + len(line)
        elif line.startswith("___"):
            block_end = offset
            break
        offset += len(line)

    if info_line_idx is None:
        return None

    section = template_data[block_start:block_end]
    if not section.strip():
        return None
    lead_ws = len(section) - len(section.lstrip())
    trail_ws = len(section) - len(section.rstrip())
    json_start = block_start + lead_ws
    json_end = block_end - trail_ws

    try:
        info = json.loads(template_data[json_start:json_end])
    except (ValueError, TypeError):
        return None
    if not isinstance(info, dict):
        return None

    info["displayName"] = new_name
    serialized = json.dumps(info, indent=2, ensure_ascii=False)
    return template_data[:json_start] + serialized + template_data[json_end:]


def _check_renamed_template_warning(
    kind: str,
    item: dict[str, Any],
    orig_name: str,
    planned_name: str,
    warnings: list[str],
) -> None:
    if (
        kind == "customTemplate"
        and planned_name != orig_name
        and _update_template_data_display_name(item.get("templateData"), planned_name) is None
    ):
        warnings.append(
            f"customTemplate '{planned_name}' templateData has no parsable "
            "___INFO___ JSON block; displayName could not be updated."
        )


def _check_gallery_template_warning(
    kind: str,
    item: dict[str, Any],
    planned_name: str,
    warnings: list[str],
) -> None:
    if kind == "customTemplate" and "galleryReference" in item:
        warnings.append(
            f"customTemplate '{planned_name}': gallery link is not preserved; "
            "imported as a local copy."
        )


def _tag_referenced_names(item: dict[str, Any]) -> list[str]:
    """Return tag names referenced via setupTag, teardownTag, or tagReference parameters."""
    refs: list[str] = []
    for field in ("setupTag", "teardownTag"):
        for ref in item.get(field) or []:
            if isinstance(ref, dict) and ref.get("tagName"):
                refs.append(str(ref["tagName"]))
    for ref in _iter_entity_tag_refs("tag", item):
        if ref.get("value"):
            refs.append(str(ref["value"]))
    return refs


def _order_deletes(
    kind: str, entries: list[dict[str, Any]], existing_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Order delete entries so referrers are deleted before the entities they reference."""
    if len(entries) <= 1 or kind not in ("trigger", "tag"):
        return list(entries)

    id_field = ENTITY_ID_FIELDS[kind]
    by_id: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    for item in existing_items:
        raw_id = item.get(id_field)
        if raw_id is not None:
            by_id[str(raw_id)] = item
        name = str(item.get("name") or "")
        if name and name not in by_name:
            by_name[name] = item

    def _find_existing(entry: dict[str, Any]) -> dict[str, Any]:
        target_id = entry.get("target_id")
        if target_id is not None and str(target_id) in by_id:
            return by_id[str(target_id)]
        return by_name.get(str(entry.get("name") or ""), {})

    if kind == "trigger":

        def _key(entry: dict[str, Any]) -> str | None:
            tid = entry.get("target_id")
            return str(tid) if tid is not None else None

        def _refs(item: dict[str, Any]) -> list[str]:
            return [
                str(ref["value"])
                for ref in _iter_entity_trigger_refs("trigger", item)
                if ref.get("value") is not None
            ]

    else:

        def _key(entry: dict[str, Any]) -> str | None:
            return str(entry.get("name") or "") or None

        def _refs(item: dict[str, Any]) -> list[str]:
            return _tag_referenced_names(item)

    prereqs: dict[str, list[str]] = {}
    for entry in entries:
        referrer_key = _key(entry)
        if referrer_key is None:
            continue
        for target_key in _refs(_find_existing(entry)):
            prereqs.setdefault(target_key, []).append(referrer_key)

    return _order_entries_topologically(
        entries,
        key_of=_key,
        deps_of=lambda e: prereqs.get(_key(e) or "", []),
    )


def _order_creates(
    kind: str, entries: list[dict[str, Any]], source: _EntitySource
) -> list[dict[str, Any]]:
    if kind == "trigger":
        return _order_entries_topologically(
            entries,
            key_of=lambda e: str(e["source_id"]) if e.get("source_id") is not None else None,
            deps_of=lambda e: [
                str(ref["value"])
                for ref in _iter_entity_trigger_refs(
                    "trigger", _find_source_item(e, "trigger", source) or {}
                )
            ],
        )
    if kind == "tag":

        def _tag_deps(entry: dict[str, Any]) -> list[str]:
            return _tag_referenced_names(_find_source_item(entry, "tag", source) or {})

        def _tag_orig_name(entry: dict[str, Any]) -> str | None:
            item = _find_source_item(entry, "tag", source)
            if item and item.get("name"):
                return str(item["name"])
            return str(entry.get("name") or "") or None

        return _order_entries_topologically(entries, key_of=_tag_orig_name, deps_of=_tag_deps)
    return list(entries)


def _remap_trigger_id_list(ids: list[Any], trigger_map: dict[str, str]) -> list[str]:
    remapped: list[str] = []
    for raw_id in ids:
        sid = str(raw_id)
        if _is_builtin_trigger_id(sid):
            remapped.append(sid)
        else:
            remapped.append(trigger_map.get(sid, sid))
    return remapped


def _remap_cvt_type(
    entity_type: str, target_container_id: str, template_map: dict[str, str]
) -> str:
    match = _CVT_TYPE_RE.match(entity_type)
    if not match:
        return entity_type
    old_template_id = match.group(2)
    new_template_id = template_map.get(old_template_id)
    if new_template_id is None:
        return entity_type
    return f"cvt_{target_container_id}_{new_template_id}"


def _iter_param_trigger_refs(param: Any) -> Iterator[dict[str, Any]]:
    """Yield Parameter dicts that hold a trigger ID reference."""
    if not isinstance(param, dict):
        return
    if (
        param.get("type") in ("triggerReference", "TRIGGER_REFERENCE")
        and param.get("value") is not None
    ):
        yield param
    if param.get("key") == "triggerIds" and isinstance(param.get("list"), list):
        for item in param["list"]:
            if (
                isinstance(item, dict)
                and item.get("type") in (None, "triggerReference", "TRIGGER_REFERENCE")
                and item.get("value") is not None
            ):
                yield item
        return
    for nested_key in ("list", "map"):
        nested = param.get(nested_key)
        if isinstance(nested, list):
            for child in nested:
                yield from _iter_param_trigger_refs(child)


def _iter_entity_params(kind: str, item: dict[str, Any]) -> Iterator[Any]:
    """Yield top-level Parameter objects anywhere on a trigger, variable, or tag."""
    if kind not in ("trigger", "variable", "tag"):
        return
    yield from item.get("parameter") or []
    if kind == "trigger":
        for cond_field in ("filter", "autoEventFilter", "customEventFilter"):
            for cond in item.get(cond_field) or []:
                if isinstance(cond, dict):
                    yield from cond.get("parameter") or []
        for param_field in (
            "checkValidation",
            "continuousTimeMinMilliseconds",
            "eventName",
            "horizontalScrollPercentageList",
            "interval",
            "intervalSeconds",
            "limit",
            "maxTimerLengthSeconds",
            "selector",
            "totalTimeMinMilliseconds",
            "uniqueTriggerId",
            "verticalScrollPercentageList",
            "visibilitySelector",
            "visiblePercentageMax",
            "visiblePercentageMin",
            "waitForTags",
            "waitForTagsTimeout",
        ):
            yield item.get(param_field)
    elif kind == "variable":
        fmt = item.get("formatValue")
        if isinstance(fmt, dict):
            for param_field in (
                "convertFalseToValue",
                "convertNullToValue",
                "convertTrueToValue",
                "convertUndefinedToValue",
            ):
                yield fmt.get(param_field)
    elif kind == "tag":
        for param_field in ("priority", "monitoringMetadata"):
            yield item.get(param_field)
        consent = item.get("consentSettings")
        if isinstance(consent, dict):
            yield consent.get("consentType")


def _iter_entity_trigger_refs(kind: str, item: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield triggerReference Parameter dicts anywhere on a trigger, variable, or tag."""
    for param in _iter_entity_params(kind, item):
        yield from _iter_param_trigger_refs(param)


def _iter_param_tag_refs(param: Any) -> Iterator[dict[str, Any]]:
    """Yield Parameter dicts that hold a tag name reference."""
    if not isinstance(param, dict):
        return
    if (
        param.get("type") in ("tagReference", "TAG_REFERENCE")
        and param.get("value") is not None
    ):
        yield param
    for nested_key in ("list", "map"):
        nested = param.get(nested_key)
        if isinstance(nested, list):
            for child in nested:
                yield from _iter_param_tag_refs(child)


def _iter_entity_tag_refs(kind: str, item: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield tagReference Parameter dicts anywhere on a trigger, variable, or tag."""
    for param in _iter_entity_params(kind, item):
        yield from _iter_param_tag_refs(param)


def _dep_label(dep_kind: str, dep_id: str, source: _EntitySource) -> str:
    item = source.by_id[dep_kind].get(dep_id)
    if item and item.get("name"):
        return str(item["name"])
    return dep_id


def _find_unmapped_dependency(
    kind: str,
    source_item: dict[str, Any],
    id_map: dict[str, dict[str, str]],
    source: _EntitySource,
) -> tuple[str, str] | None:
    """Return (dep_kind, label) for the first unmapped ID dependency, or None."""
    if kind in ("customTemplate", "variable", "trigger", "tag"):
        parent_folder_id = source_item.get("parentFolderId")
        if parent_folder_id is not None and str(parent_folder_id) not in ("", "0"):
            old_folder_id = str(parent_folder_id)
            if old_folder_id not in id_map["folder"]:
                return ("folder", _dep_label("folder", old_folder_id, source))

    if kind in ("variable", "tag") and isinstance(source_item.get("type"), str):
        match = _CVT_TYPE_RE.match(source_item["type"])
        if match:
            old_template_id = match.group(2)
            if old_template_id not in id_map["customTemplate"]:
                return ("customTemplate", _dep_label("customTemplate", old_template_id, source))

    trigger_fields: tuple[str, ...] = ()
    if kind == "variable":
        trigger_fields = ("enablingTriggerId", "disablingTriggerId")
    elif kind == "tag":
        trigger_fields = ("firingTriggerId", "blockingTriggerId")

    for field in trigger_fields:
        raw_ids = source_item.get(field)
        if isinstance(raw_ids, list):
            for raw_tid in raw_ids:
                old_tid = str(raw_tid)
                if not _is_builtin_trigger_id(old_tid) and old_tid not in id_map["trigger"]:
                    return ("trigger", _dep_label("trigger", old_tid, source))

    for ref in _iter_entity_trigger_refs(kind, source_item):
        old_tid = str(ref["value"])
        if not _is_builtin_trigger_id(old_tid) and old_tid not in id_map["trigger"]:
            return ("trigger", _dep_label("trigger", old_tid, source))

    return None


def _prepare_body(
    kind: str,
    source_item: dict[str, Any],
    planned_name: str,
    container_id: str,
    id_map: dict[str, dict[str, str]],
    tag_name_map: dict[str, str],
) -> dict[str, Any]:
    """Deep-copy an entity body, strip server fields, apply name, and remap references."""
    body = copy.deepcopy(source_item)
    id_field = ENTITY_ID_FIELDS[kind]
    for field in SERVER_ASSIGNED_FIELDS:
        body.pop(field, None)
    body.pop(id_field, None)
    body["name"] = planned_name
    if kind == "customTemplate":
        body.pop("galleryReference", None)

    if kind in ("customTemplate", "variable", "trigger", "tag"):
        parent_folder_id = body.get("parentFolderId")
        if parent_folder_id is not None:
            old_folder_id = str(parent_folder_id)
            if old_folder_id in ("", "0"):
                body.pop("parentFolderId", None)
            elif old_folder_id in id_map["folder"]:
                body["parentFolderId"] = id_map["folder"][old_folder_id]

    if kind in ("variable", "tag") and isinstance(body.get("type"), str):
        body["type"] = _remap_cvt_type(
            body["type"], str(container_id), id_map["customTemplate"]
        )

    if kind == "variable":
        for field in ("enablingTriggerId", "disablingTriggerId"):
            if isinstance(body.get(field), list):
                body[field] = _remap_trigger_id_list(body[field], id_map["trigger"])

    for ref in _iter_entity_trigger_refs(kind, body):
        old_tid = str(ref["value"])
        if not _is_builtin_trigger_id(old_tid):
            ref["value"] = id_map["trigger"].get(old_tid, old_tid)

    for ref in _iter_entity_tag_refs(kind, body):
        old_tag_name = str(ref["value"])
        if old_tag_name in tag_name_map:
            ref["value"] = tag_name_map[old_tag_name]

    if kind == "tag":
        for field in ("firingTriggerId", "blockingTriggerId"):
            if isinstance(body.get(field), list):
                body[field] = _remap_trigger_id_list(body[field], id_map["trigger"])
        for field in ("setupTag", "teardownTag"):
            if isinstance(body.get(field), list):
                for ref in body[field]:
                    if isinstance(ref, dict) and ref.get("tagName"):
                        old_tag_name = str(ref["tagName"])
                        if old_tag_name in tag_name_map:
                            ref["tagName"] = tag_name_map[old_tag_name]

    return body


def _build_import_body(
    kind: str,
    entry: dict[str, Any],
    planned_name: str,
    container_id: str,
    source: _EntitySource,
    id_map: dict[str, dict[str, str]],
    tag_name_map: dict[str, str],
) -> dict[str, Any]:
    source_item = _find_source_item(entry, kind, source)
    if source_item is None:
        raise ValueError(f"Source entity not found in export for {kind} '{planned_name}'.")
    missing = _find_unmapped_dependency(kind, source_item, id_map, source)
    if missing is not None:
        dep_kind, dep_label = missing
        raise ValueError(f"depends on {dep_kind} '{dep_label}' which was not imported")
    return _prepare_body(
        kind, source_item, planned_name, container_id, id_map, tag_name_map
    )


def apply_import(
    plan: dict[str, Any],
    export: dict[str, Any],
    ops: ImportOps,
    account_id: str,
    container_id: str,
    workspace_id: str,
) -> dict[str, Any]:
    """Execute a planned import against a workspace using the provided ops callbacks."""
    _ = (account_id, workspace_id)  # Workspace is already bound on ops.
    version = _extract_api_version(export)
    source = _index_source_entities(version)

    id_map: dict[str, dict[str, str]] = {kind: {} for kind in PLAN_KINDS}
    tag_name_map: dict[str, str] = {}

    for kind in PLAN_KINDS:
        for bucket in ("skip", "update"):
            for entry in (plan.get(bucket) or {}).get(kind) or []:
                source_id = entry.get("source_id")
                target_id = entry.get("target_id")
                if source_id is not None and target_id is not None:
                    id_map[kind][str(source_id)] = str(target_id)

    for entry in (plan.get("create") or {}).get("tag") or []:
        item = _find_source_item(entry, "tag", source)
        if item and item.get("name") and entry.get("name"):
            orig_name = str(item["name"])
            new_name = str(entry["name"])
            if orig_name != new_name:
                tag_name_map[orig_name] = new_name

    created: dict[str, list[dict[str, str]]] = {kind: [] for kind in PLAN_KINDS}
    updated: dict[str, list[dict[str, str]]] = {kind: [] for kind in PLAN_KINDS}
    deleted: dict[str, list[dict[str, str]]] = {kind: [] for kind in PLAN_KINDS}
    errors: list[dict[str, str]] = []

    builtin_types = list(plan.get("enable_builtins") or [])
    if builtin_types:
        try:
            ops.enable_builtins(builtin_types)
        except Exception as exc:
            errors.append(
                {
                    "kind": "builtInVariable",
                    "name": ", ".join(str(t) for t in builtin_types),
                    "error": str(exc),
                }
            )

    for kind in PLAN_KINDS:
        ops_kind = KIND_TO_OPS_KIND[kind]
        id_field = ENTITY_ID_FIELDS[kind]

        create_entries = _order_creates(
            kind, list((plan.get("create") or {}).get(kind) or []), source
        )
        for entry in create_entries:
            planned_name = str(entry.get("name") or "")
            raw_source_id = entry.get("source_id")
            source_id = str(raw_source_id) if raw_source_id is not None else None
            try:
                body = _build_import_body(
                    kind,
                    entry,
                    planned_name,
                    str(container_id),
                    source,
                    id_map,
                    tag_name_map,
                )
                if kind == "customTemplate":
                    source_item = _find_source_item(entry, kind, source)
                    orig_name = (
                        str(source_item.get("name") or "") if source_item else planned_name
                    )
                    if planned_name != orig_name:
                        updated_tpl = _update_template_data_display_name(
                            body.get("templateData"), planned_name
                        )
                        if updated_tpl is not None:
                            body["templateData"] = updated_tpl
                        else:
                            errors.append(
                                {
                                    "kind": kind,
                                    "name": planned_name,
                                    "error": (
                                        f"customTemplate '{planned_name}' templateData has no "
                                        "parsable ___INFO___ JSON block; displayName could not "
                                        "be updated."
                                    ),
                                }
                            )
                response = ops.create(ops_kind, body)
                new_id = str((response or {}).get(id_field) or "")
                if source_id is not None and new_id:
                    id_map[kind][source_id] = new_id
                created[kind].append({"name": planned_name, "id": new_id})
            except Exception as exc:
                errors.append({"kind": kind, "name": planned_name, "error": str(exc)})

        for entry in (plan.get("update") or {}).get(kind) or []:
            planned_name = str(entry.get("name") or "")
            raw_source_id = entry.get("source_id")
            source_id = str(raw_source_id) if raw_source_id is not None else None
            target_id = str(entry.get("target_id") or "")
            try:
                body = _build_import_body(
                    kind,
                    entry,
                    planned_name,
                    str(container_id),
                    source,
                    id_map,
                    tag_name_map,
                )
                response = ops.update(ops_kind, target_id, body)
                updated_id = str((response or {}).get(id_field) or target_id)
                if source_id is not None and updated_id:
                    id_map[kind][source_id] = updated_id
                updated[kind].append({"name": planned_name, "id": updated_id})
            except Exception as exc:
                errors.append({"kind": kind, "name": planned_name, "error": str(exc)})

    for kind in reversed(PLAN_KINDS):
        ops_kind = KIND_TO_OPS_KIND[kind]
        for entry in (plan.get("delete") or {}).get(kind) or []:
            name = str(entry.get("name") or "")
            target_id = str(entry.get("target_id") or "")
            try:
                ops.delete(ops_kind, target_id)
                deleted[kind].append({"name": name, "id": target_id})
            except Exception as exc:
                errors.append({"kind": kind, "name": name, "error": str(exc)})

    return {
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "errors": errors,
    }
