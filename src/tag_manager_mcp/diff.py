"""Deterministic diffing between two GTM ContainerVersion dictionaries."""

from __future__ import annotations

from typing import Any

ENTITY_ID_KEYS: tuple[tuple[str, str], ...] = (
    ("tag", "tagId"),
    ("trigger", "triggerId"),
    ("variable", "variableId"),
    ("folder", "folderId"),
    ("customTemplate", "templateId"),
    ("client", "clientId"),
    ("zone", "zoneId"),
    ("transformation", "transformationId"),
    ("builtInVariable", "type"),
    ("gtagConfig", "gtagConfigId"),
)

IGNORED_KEYS: frozenset[str] = frozenset(
    {
        "fingerprint",
        "accountId",
        "containerId",
        "path",
        "tagManagerUrl",
        "workspaceId",
    }
)

_MISSING = object()


def diff_versions(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Compare two ContainerVersion dicts and return added, removed, and changed entities."""
    changes: dict[str, dict[str, list[dict[str, Any]]]] = {}
    total = 0

    for entity_key, id_key in ENTITY_ID_KEYS:
        old_map = _index_entities(old.get(entity_key) or [], id_key)
        new_map = _index_entities(new.get(entity_key) or [], id_key)

        added = [
            _entry(entity_id, new_map[entity_id])
            for entity_id in new_map
            if entity_id not in old_map
        ]
        removed = [
            _entry(entity_id, old_map[entity_id])
            for entity_id in old_map
            if entity_id not in new_map
        ]
        changed: list[dict[str, Any]] = []
        for entity_id in old_map:
            if entity_id not in new_map:
                continue
            fields = _changed_fields(old_map[entity_id], new_map[entity_id])
            if fields:
                changed.append(_entry(entity_id, new_map[entity_id], fields))

        added.sort(key=_sort_key)
        removed.sort(key=_sort_key)
        changed.sort(key=_sort_key)

        changes[entity_key] = {
            "added": added,
            "removed": removed,
            "changed": changed,
        }
        total += len(added) + len(removed) + len(changed)

    return {
        "from": old.get("containerVersionId"),
        "to": new.get("containerVersionId"),
        "changes": changes,
        "total": total,
    }


def _index_entities(entities: list[dict[str, Any]], id_key: str) -> dict[str, dict[str, Any]]:
    return {str(item.get(id_key) or ""): item for item in entities}


def _changed_fields(old_item: dict[str, Any], new_item: dict[str, Any]) -> list[str]:
    keys = (set(old_item) | set(new_item)) - IGNORED_KEYS
    return sorted(
        key for key in keys if old_item.get(key, _MISSING) != new_item.get(key, _MISSING)
    )


def _entry(
    entity_id: str, item: dict[str, Any], fields: list[str] | None = None
) -> dict[str, Any]:
    return {
        "id": entity_id,
        "name": str(item.get("name") or ""),
        "fields": fields if fields is not None else [],
    }


def _sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (entry["name"], entry["id"])
