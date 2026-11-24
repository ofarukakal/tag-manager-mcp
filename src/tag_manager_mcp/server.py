"""FastMCP tools for Google Tag Manager."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal

from googleapiclient.errors import HttpError

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP

from . import publish_guard
from .access import (
    AccessMode,
    ToolRegistry,
    get_access_mode,
    parse_access_mode,
    require,
    scopes_for_mode,
)
from .client import GTMClient, GTMError
from .debug_log import call_id_context, get_debug_logger
from .diff import diff_versions
from .export_files import get_export_dir, write_version_export
from .importer import PLAN_KINDS, apply_import, plan_import
from .reader import VERSION_ENTITY_KEYS, GTMReader, summarize_version
from .writer import GTMWriter

logging.basicConfig(
    level=os.getenv("GTM_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class LoggingFastMCP(FastMCP):
    async def call_tool(
        self, name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any
    ) -> Any:
        debug_log = get_debug_logger()
        call_id = debug_log.new_call_id()
        token = call_id_context.set(call_id)
        started = time.monotonic()
        try:
            result = await super().call_tool(name, arguments, *args, **kwargs)
            debug_log.log(
                kind="mcp_tool",
                name=name,
                request=arguments,
                response=result,
                status="success",
                duration_ms=(time.monotonic() - started) * 1000,
                call_id=call_id,
            )
            return result
        except Exception as exc:
            debug_log.log(
                kind="mcp_tool",
                name=name,
                request=arguments,
                status="error",
                duration_ms=(time.monotonic() - started) * 1000,
                error=exc,
                call_id=call_id,
            )
            raise
        finally:
            call_id_context.reset(token)


SERVER_INSTRUCTIONS = """
When a user identifies a container with a public ID such as GTM-W525XW4, always call
resolve_container_identifier first. Use the returned account_id and container_id for subsequent
tools. Never scan every account to locate a public identifier. Call list_workspaces before any
workspace-scoped operation; never assume workspace ID 1. Prefer get_version_summary and
get_version_entities over exporting when answering questions about a container version. Use
export_container_to_file only when you need the full GTM export JSON written to disk for other
tools to read. To publish a container version (to live, a user environment, or latest), call
prepare_publish first, wait for a human to run tag-manager-approve in a terminal, and then call
publish_version, publish_to_environment, or set_latest_version with the approval_id and
confirm=true.
"""

tool_registry = ToolRegistry()
tool = tool_registry.tool
_client: GTMClient | None = None
_reader: GTMReader | None = None
_writer: GTMWriter | None = None
EntityKind = Literal[
    "tags",
    "triggers",
    "variables",
    "folders",
    "templates",
    "clients",
    "transformations",
    "zones",
    "gtag_config",
]
ReadEntityKind = EntityKind
RevertibleEntityKind = Literal[
    "tags",
    "triggers",
    "variables",
    "folders",
    "templates",
    "clients",
    "transformations",
    "zones",
]
VersionEntityKind = Literal[
    "tag",
    "trigger",
    "variable",
    "folder",
    "builtInVariable",
    "customTemplate",
    "client",
    "zone",
    "transformation",
    "gtagConfig",
]
ImportMode = Literal["merge", "overwrite"]
ImportConflict = Literal["skip", "overwrite", "rename"]
CombineSettingSource = Literal["settingSourceUnspecified", "current", "other"]
_EXISTING_ENTITY_KINDS = frozenset({"tags", "triggers", "variables", "folders"})
_IMPORT_EXISTING_KINDS: tuple[tuple[str, str], ...] = (
    ("folders", "folder"),
    ("templates", "customTemplate"),
    ("variables", "variable"),
    ("triggers", "trigger"),
    ("tags", "tag"),
    ("built_in_variables", "builtInVariable"),
)


def get_client() -> GTMClient:
    global _client
    if _client is None:
        _client = GTMClient()
    return _client


def get_reader() -> GTMReader:
    """Return a cached GTMReader bound to the active GTMClient."""
    global _reader
    client = get_client()
    if _reader is None or _reader.client is not client:
        _reader = GTMReader(client)
    return _reader


def get_writer() -> GTMWriter:
    """Return a cached GTMWriter bound to the active GTMClient."""
    global _writer
    client = get_client()
    if _writer is None or _writer.client is not client:
        _writer = GTMWriter(client)
    return _writer


def _load_export_file(file_name: str) -> dict[str, Any]:
    """Load a container export JSON file strictly from inside the configured export directory."""
    if not isinstance(file_name, str) or not file_name or not file_name.strip():
        raise ValueError("file_name must be a non-empty bare file name.")
    if "\x00" in file_name or "/" in file_name or "\\" in file_name:
        raise ValueError("file_name must be a bare file name without path separators.")
    if os.sep in file_name or (os.altsep and os.altsep in file_name):
        raise ValueError("file_name must be a bare file name without path separators.")
    if (
        file_name in {".", ".."}
        or Path(file_name).is_absolute()
        or Path(file_name).name != file_name
    ):
        raise ValueError("file_name must be a bare file name inside the export directory.")

    export_dir = get_export_dir().resolve()
    resolved = (export_dir / file_name).resolve()
    if not resolved.is_relative_to(export_dir) or resolved == export_dir:
        raise ValueError(f"Export file path escapes export directory: {file_name!r}")
    if not resolved.is_file():
        raise FileNotFoundError(f"Export file not found in export directory: {file_name!r}")

    data = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Container export must be a JSON object.")
    return data


def _fetch_existing_workspace_entities(
    account_id: str, container_id: str, workspace_id: str
) -> dict[str, list[dict[str, Any]]]:
    reader = get_reader()
    return {
        plan_key: reader.list_workspace_entities(
            reader_kind, account_id, container_id, workspace_id
        )
        for reader_kind, plan_key in _IMPORT_EXISTING_KINDS
    }


def _plan_counts(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "create": sum(len(items) for items in plan["create"].values()),
        "update": sum(len(items) for items in plan["update"].values()),
        "delete": sum(len(items) for items in plan["delete"].values()),
        "skip": sum(len(items) for items in plan["skip"].values()),
        "enable_builtins": len(plan["enable_builtins"]),
        "by_kind": {
            kind: {
                "create": len(plan["create"][kind]),
                "update": len(plan["update"][kind]),
                "delete": len(plan["delete"][kind]),
                "skip": len(plan["skip"][kind]),
            }
            for kind in PLAN_KINDS
        },
    }


class _WorkspaceImportOps:
    """ImportOps adapter over GTMClient and GTMWriter for a single target workspace."""

    def __init__(
        self,
        client: GTMClient,
        writer: GTMWriter,
        account_id: str,
        container_id: str,
        workspace_id: str,
    ) -> None:
        self._client = client
        self._writer = writer
        self._account_id = account_id
        self._container_id = container_id
        self._workspace_id = workspace_id

    def create(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind in _EXISTING_ENTITY_KINDS:
            return self._client.create_entity(
                kind, self._account_id, self._container_id, self._workspace_id, body
            )
        return self._writer.create_workspace_entity(
            kind, self._account_id, self._container_id, self._workspace_id, body
        )

    def update(self, kind: str, entity_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind in _EXISTING_ENTITY_KINDS:
            return self._client.update_entity(
                kind, self._account_id, self._container_id, self._workspace_id, entity_id, body
            )
        return self._writer.update_workspace_entity(
            kind, self._account_id, self._container_id, self._workspace_id, entity_id, body
        )

    def delete(self, kind: str, entity_id: str) -> dict[str, Any]:
        if kind in _EXISTING_ENTITY_KINDS:
            return self._client.delete_entity(
                kind, self._account_id, self._container_id, self._workspace_id, entity_id
            )
        return self._writer.delete_workspace_entity(
            kind, self._account_id, self._container_id, self._workspace_id, entity_id
        )

    def enable_builtins(self, types: list[str]) -> Any:
        return self._client.enable_builtin_variables(
            self._account_id, self._container_id, self._workspace_id, types
        )


_ENV_MUTABLE_METADATA_FIELDS: tuple[str, ...] = ("name", "description", "enableDebug", "url")
_ENV_ALLOWED_BODY_KEYS: frozenset[str] = frozenset(_ENV_MUTABLE_METADATA_FIELDS)


def _validate_environment_body(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    disallowed = sorted(str(key) for key in body if key not in _ENV_ALLOWED_BODY_KEYS)
    if not disallowed:
        return None
    rejected = ", ".join(disallowed)
    allowed = ", ".join(_ENV_MUTABLE_METADATA_FIELDS)
    return {
        "ok": False,
        "message": (
            f"Disallowed environment body key(s): {rejected}. "
            f"Only {allowed} are permitted; pointing an environment at a version is publishing "
            "and requires prepare_publish with environment_id and publish_to_environment."
        ),
    }


def _redact_environment(env: dict[str, Any]) -> dict[str, Any]:
    if isinstance(env, dict) and "authorizationCode" in env:
        return {**env, "authorizationCode": "[redacted]"}
    return env


def _http_status(exc: BaseException | None) -> int | None:
    if exc is None:
        return None
    for candidate in (exc, exc.__cause__):
        if candidate is None:
            continue
        status = getattr(candidate, "status_code", None)
        if status is None:
            status = getattr(getattr(candidate, "resp", None), "status", None)
        if status is not None:
            try:
                return int(status)
            except (TypeError, ValueError):
                continue
    return None


def _is_no_live_version_error(exc: Exception) -> bool:
    status = _http_status(exc)
    if status is not None:
        return status == 404
    lowered = str(exc).lower()
    return any(
        marker in lowered for marker in ("not found", "404", "no live", "never published")
    )


def _fetch_live_version_optional(
    reader: GTMReader, account_id: str, container_id: str
) -> dict[str, Any] | None:
    try:
        live = reader.live_version(account_id, container_id)
    except LookupError:
        return None
    except (GTMError, HttpError) as exc:
        if _is_no_live_version_error(exc):
            return None
        raise
    if not live or not live.get("containerVersionId"):
        return None
    return live


def _fetch_latest_version_header_optional(
    reader: GTMReader, account_id: str, container_id: str
) -> dict[str, Any] | None:
    try:
        header = reader.latest_version_header(account_id, container_id)
    except LookupError:
        return None
    except (GTMError, HttpError) as exc:
        if _is_no_live_version_error(exc):
            return None
        raise
    if not header or not header.get("containerVersionId"):
        return None
    return header


def _summarize_publish_diff(
    diff: dict[str, Any],
    *,
    has_live_version: bool,
    target: str = publish_guard.TARGET_LIVE,
) -> dict[str, Any]:
    summary: dict[str, Any] = {"total_changes": diff.get("total", 0)}
    if not has_live_version:
        if target == publish_guard.TARGET_LIVE:
            summary["note"] = "Container has never been published (no current live version)."
        elif target == publish_guard.TARGET_LATEST:
            summary["note"] = "Container has no current latest version."
        else:
            summary["note"] = "Environment currently has no container version."
    for kind, buckets in (diff.get("changes") or {}).items():
        added = [str(item.get("name") or item.get("id") or "") for item in buckets.get("added", [])]
        removed = [
            str(item.get("name") or item.get("id") or "") for item in buckets.get("removed", [])
        ]
        changed = [
            str(item.get("name") or item.get("id") or "") for item in buckets.get("changed", [])
        ]
        if not (added or removed or changed):
            continue
        summary[kind] = {
            "added": added,
            "added_count": len(added),
            "removed": removed,
            "removed_count": len(removed),
            "changed": changed,
            "changed_count": len(changed),
        }
    return summary


@tool("read")
def auth_status() -> dict[str, Any]:
    """Verify configured credentials and return the accessible GTM accounts."""
    client = get_client()
    accounts = client.list_accounts()
    mode = get_access_mode()
    return {
        "ok": True,
        "auth_method": client.auth_method,
        "access_mode": mode.value,
        "scopes": list(scopes_for_mode(mode)),
        "quota": client.quota_guard.status(),
        "accounts": accounts,
    }


@tool("read")
def quota_status() -> dict[str, Any]:
    """Return the local GTM request budget and rate-limit settings without using API quota."""
    return get_client().quota_guard.status()


@tool("read")
def debug_log_status() -> dict[str, Any]:
    """Return SQLite debug-log status and event counts."""
    return get_debug_logger().status()


@tool("read")
def debug_log_recent(
    limit: int = 50, kind: Literal["mcp_tool", "gtm_api"] | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    """Return recent sanitized debug events. Limit is constrained to 1-500 rows."""
    return get_debug_logger().recent(limit=limit, kind=kind, status=status)


@tool("read")
def list_accounts() -> list[dict[str, Any]]:
    """List Google Tag Manager accounts accessible to the authenticated identity."""
    return get_client().list_accounts()


@tool("read")
def list_containers(account_id: str) -> list[dict[str, Any]]:
    """List containers in a GTM account."""
    return get_client().list_containers(account_id)


@tool("read")
def get_container(account_id: str, container_id: str) -> dict[str, Any]:
    """Get one GTM container."""
    return get_client().get_container(account_id, container_id)


@tool("read")
def resolve_container_identifier(identifier: str) -> dict[str, Any]:
    """Resolve GTM-* or destination IDs to the owning account and internal container IDs."""
    container = get_client().resolve_container_identifier(identifier)
    return {
        "identifier": identifier.strip().upper(),
        "account_id": container.get("accountId"),
        "container_id": container.get("containerId"),
        "container": container,
        "next_step": "Call list_workspaces with account_id and container_id before editing.",
    }


@tool("read")
def list_workspaces(account_id: str, container_id: str) -> list[dict[str, Any]]:
    """List workspaces in a GTM container. Never assume workspace ID 1."""
    return get_client().list_workspaces(account_id, container_id)


@tool("write")
def create_workspace(
    account_id: str, container_id: str, name: str, description: str = ""
) -> dict[str, Any]:
    """Create an isolated GTM workspace for changes."""
    require("write")
    return get_client().create_workspace(account_id, container_id, name, description)


@tool("read")
def get_workspace_status(
    account_id: str, container_id: str, workspace_id: str
) -> dict[str, Any]:
    """Return pending changes and merge conflicts for a workspace."""
    return get_client().workspace_status(account_id, container_id, workspace_id)


@tool("write")
def sync_workspace(
    account_id: str, container_id: str, workspace_id: str
) -> dict[str, Any]:
    """Synchronize a workspace with the latest container version and report conflicts."""
    require("write")
    return get_client().sync_workspace(account_id, container_id, workspace_id)


@tool("read")
def list_entities(
    kind: ReadEntityKind, account_id: str, container_id: str, workspace_id: str
) -> list[dict[str, Any]]:
    """List workspace entities (tags, triggers, variables, folders, templates, etc.)."""
    if kind in _EXISTING_ENTITY_KINDS:
        return get_client().list_entities(kind, account_id, container_id, workspace_id)
    return get_reader().list_workspace_entities(kind, account_id, container_id, workspace_id)


@tool("read")
def get_entity(
    kind: ReadEntityKind,
    account_id: str,
    container_id: str,
    workspace_id: str,
    entity_id: str,
) -> dict[str, Any]:
    """Get one workspace entity by ID."""
    if kind in _EXISTING_ENTITY_KINDS:
        return get_client().get_entity(kind, account_id, container_id, workspace_id, entity_id)
    return get_reader().get_workspace_entity(
        kind, account_id, container_id, workspace_id, entity_id
    )


@tool("write")
def create_entity(
    kind: EntityKind,
    account_id: str,
    container_id: str,
    workspace_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Create a workspace entity (tag, trigger, variable, folder, template, client, etc.)."""
    require("write")
    if kind in _EXISTING_ENTITY_KINDS:
        return get_client().create_entity(kind, account_id, container_id, workspace_id, body)
    return get_writer().create_workspace_entity(kind, account_id, container_id, workspace_id, body)


@tool("write")
def update_entity(
    kind: EntityKind,
    account_id: str,
    container_id: str,
    workspace_id: str,
    entity_id: str,
    body: dict[str, Any],
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Update a workspace entity. Supply fingerprint for optimistic locking."""
    require("write")
    if kind in _EXISTING_ENTITY_KINDS:
        return get_client().update_entity(
            kind, account_id, container_id, workspace_id, entity_id, body, fingerprint
        )
    return get_writer().update_workspace_entity(
        kind, account_id, container_id, workspace_id, entity_id, body, fingerprint
    )


@tool("write")
def delete_entity(
    kind: EntityKind,
    account_id: str,
    container_id: str,
    workspace_id: str,
    entity_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a workspace entity. Requires confirm=true because this operation is destructive."""
    require("write")
    if not confirm:
        return {"ok": False, "message": "Deletion requires confirm=true."}
    if kind in _EXISTING_ENTITY_KINDS:
        result = get_client().delete_entity(kind, account_id, container_id, workspace_id, entity_id)
    else:
        result = get_writer().delete_workspace_entity(
            kind, account_id, container_id, workspace_id, entity_id
        )
    return {"ok": True, "result": result}


@tool("write")
def revert_entity(
    kind: RevertibleEntityKind,
    account_id: str,
    container_id: str,
    workspace_id: str,
    entity_id: str,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Revert uncommitted workspace changes to an entity."""
    require("write")
    return get_writer().revert_workspace_entity(
        kind, account_id, container_id, workspace_id, entity_id, fingerprint
    )


@tool("read")
def list_builtin_variables(
    account_id: str, container_id: str, workspace_id: str
) -> list[dict[str, Any]]:
    """List enabled built-in variables in a workspace."""
    return get_client().list_builtin_variables(account_id, container_id, workspace_id)


@tool("write")
def enable_builtin_variables(
    account_id: str,
    container_id: str,
    workspace_id: str,
    variable_types: list[str],
) -> list[dict[str, Any]]:
    """Enable one or more GTM built-in variable types."""
    require("write")
    if not variable_types:
        raise ValueError("variable_types cannot be empty")
    return get_client().enable_builtin_variables(
        account_id, container_id, workspace_id, variable_types
    )


@tool("write")
def disable_builtin_variables(
    account_id: str,
    container_id: str,
    workspace_id: str,
    variable_types: list[str],
    confirm: bool = False,
) -> dict[str, Any]:
    """Disable built-in variable types. Requires confirm=true."""
    require("write")
    if not confirm:
        return {"ok": False, "message": "Disabling variables requires confirm=true."}
    if not variable_types:
        raise ValueError("variable_types cannot be empty")
    result = get_client().disable_builtin_variables(
        account_id, container_id, workspace_id, variable_types
    )
    return {"ok": True, "result": result}


@tool("write")
def revert_builtin_variable(
    account_id: str,
    container_id: str,
    workspace_id: str,
    variable_type: str,
) -> dict[str, Any]:
    """Revert uncommitted changes to a built-in variable in a workspace."""
    require("write")
    return get_writer().revert_builtin_variable(
        account_id, container_id, workspace_id, variable_type
    )


@tool("write")
def import_template_from_gallery(
    account_id: str,
    container_id: str,
    workspace_id: str,
    gallery_owner: str,
    gallery_repository: str,
    gallery_sha: str | None = None,
    acknowledge_permissions: bool = False,
) -> dict[str, Any]:
    """Import a Community Template Gallery custom template into a workspace."""
    require("write")
    return get_writer().import_template_from_gallery(
        account_id,
        container_id,
        workspace_id,
        gallery_owner,
        gallery_repository,
        gallery_sha=gallery_sha,
        acknowledge_permissions=acknowledge_permissions,
    )


@tool("read")
def list_folder_entities(
    account_id: str, container_id: str, workspace_id: str, folder_id: str
) -> dict[str, Any]:
    """List tags, triggers, and variables inside a workspace folder."""
    return get_writer().list_folder_entities(account_id, container_id, workspace_id, folder_id)


@tool("write")
def move_entities_to_folder(
    account_id: str,
    container_id: str,
    workspace_id: str,
    folder_id: str,
    tag_ids: list[str] | None = None,
    trigger_ids: list[str] | None = None,
    variable_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Move tags, triggers, and/or variables to a folder (folder_id='0' removes from folder)."""
    require("write")
    return get_writer().move_entities_to_folder(
        account_id,
        container_id,
        workspace_id,
        folder_id,
        tag_ids=tag_ids,
        trigger_ids=trigger_ids,
        variable_ids=variable_ids,
    )


@tool("read")
def get_workspace(account_id: str, container_id: str, workspace_id: str) -> dict[str, Any]:
    """Get one GTM workspace by ID."""
    return get_writer().get_workspace(account_id, container_id, workspace_id)


@tool("write")
def update_workspace(
    account_id: str,
    container_id: str,
    workspace_id: str,
    body: dict[str, Any],
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Update a workspace. Supply fingerprint for optimistic locking."""
    require("write")
    return get_writer().update_workspace(account_id, container_id, workspace_id, body, fingerprint)


@tool("admin")
def delete_workspace(
    account_id: str, container_id: str, workspace_id: str, confirm: bool = False
) -> dict[str, Any]:
    """Delete a workspace. Requires admin access (delete.containers scope) and confirm=true."""
    require("admin")
    if not confirm:
        return {"ok": False, "message": "Deleting a workspace requires confirm=true."}
    result = get_writer().delete_workspace(account_id, container_id, workspace_id)
    return {"ok": True, "result": result}


@tool("write")
def resolve_conflict(
    account_id: str,
    container_id: str,
    workspace_id: str,
    body: dict[str, Any],
    fingerprint: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Resolve a workspace merge conflict with an Entity payload. Requires confirm=true."""
    require("write")
    if not confirm:
        return {"ok": False, "message": "Resolving a merge conflict requires confirm=true."}
    result = get_writer().resolve_conflict(
        account_id, container_id, workspace_id, body, fingerprint
    )
    return {"ok": True, "result": result}


@tool("write")
def quick_preview(account_id: str, container_id: str, workspace_id: str) -> dict[str, Any]:
    """Quick-preview the current workspace."""
    require("write")
    return get_writer().quick_preview(account_id, container_id, workspace_id)


@tool("write")
def bulk_update(
    account_id: str, container_id: str, workspace_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Apply multiple ProposedChange entity updates to a workspace in one request."""
    require("write")
    return get_writer().bulk_update(account_id, container_id, workspace_id, body)


@tool("read")
def list_versions(account_id: str, container_id: str) -> list[dict[str, Any]]:
    """List version headers for a GTM container."""
    return get_client().list_versions(account_id, container_id)


@tool("read")
def latest_version_header(account_id: str, container_id: str) -> dict[str, Any]:
    """Fetch the latest ContainerVersionHeader for a container."""
    return get_reader().latest_version_header(account_id, container_id)


@tool("read")
def get_version_summary(
    account_id: str, container_id: str, version_id: str | None = None
) -> dict[str, Any]:
    """Return entity counts and paused tags for the live or specified container version."""
    reader = get_reader()
    version = (
        reader.live_version(account_id, container_id)
        if version_id is None
        else reader.get_version(account_id, container_id, version_id)
    )
    return summarize_version(version)


@tool("read")
def get_version_entities(
    account_id: str,
    container_id: str,
    kind: VersionEntityKind,
    version_id: str | None = None,
    name_contains: str | None = None,
) -> list[dict[str, Any]]:
    """Return entities of a given kind from the live or specified container version."""
    if kind not in VERSION_ENTITY_KEYS:
        raise ValueError(f"unsupported version entity kind: {kind}")
    reader = get_reader()
    version = (
        reader.live_version(account_id, container_id)
        if version_id is None
        else reader.get_version(account_id, container_id, version_id)
    )
    entities = list(version.get(kind) or [])
    if name_contains is not None:
        needle = name_contains.lower()
        entities = [item for item in entities if needle in str(item.get("name", "")).lower()]

    if kind == "customTemplate" and len(entities) != 1:
        sanitized: list[dict[str, Any]] = []
        for item in entities:
            copy = dict(item)
            if "templateData" in copy:
                template_data = copy.pop("templateData")
                if isinstance(template_data, str):
                    copy["templateDataBytes"] = len(template_data.encode("utf-8"))
                else:
                    copy["templateDataBytes"] = len(template_data or b"")
            sanitized.append(copy)
        return sanitized

    return entities


@tool("read")
def diff_container_versions(
    account_id: str,
    container_id: str,
    from_version_id: str,
    to_version_id: str | None = None,
) -> dict[str, Any]:
    """Compare two container versions (or from_version_id against the live version)."""
    reader = get_reader()
    old_version = reader.get_version(account_id, container_id, from_version_id)
    new_version = (
        reader.live_version(account_id, container_id)
        if to_version_id is None
        else reader.get_version(account_id, container_id, to_version_id)
    )
    return diff_versions(old_version, new_version)


@tool("read")
def export_container_to_file(
    account_id: str, container_id: str, version_id: str | None = None
) -> dict[str, Any]:
    """Export a container version as JSON into GTM_EXPORT_DIR and return its path and summary."""
    exported = get_reader().export_container(account_id, container_id, version_id=version_id)
    version = exported.get("containerVersion") or {}
    path = write_version_export(exported, fallback_public_id=container_id)
    return {"path": str(path), "summary": summarize_version(version)}


@tool("read")
def list_environments(account_id: str, container_id: str) -> list[dict[str, Any]]:
    """List environments in a GTM container (with authorizationCode redacted)."""
    return [
        _redact_environment(env) for env in get_reader().list_environments(account_id, container_id)
    ]


@tool("read")
def list_destinations(account_id: str, container_id: str) -> list[dict[str, Any]]:
    """List destinations linked to a GTM container."""
    return get_reader().list_destinations(account_id, container_id)


@tool("read")
def inventory() -> list[dict[str, Any]]:
    """Inventory every accessible GTM account and its containers without aborting on errors."""
    return get_reader().inventory()


@tool("read")
def list_live_versions(
    usage_context: str | None = "web", save: bool = False
) -> list[dict[str, Any]]:
    """Summarize live versions across accessible containers, optionally saving export files."""
    items = get_reader().fetch_all_live(usage_context=usage_context)
    results: list[dict[str, Any]] = []
    for item in items:
        version = item.get("version")
        entry: dict[str, Any] = {
            "accountId": item.get("accountId", ""),
            "containerId": item.get("containerId", ""),
            "publicId": item.get("publicId", ""),
            "name": item.get("name", ""),
            "error": item.get("error"),
            "summary": summarize_version(version) if isinstance(version, dict) else None,
        }
        if save:
            if isinstance(version, dict):
                try:
                    path = write_version_export(
                        version,
                        public_id=item.get("publicId"),
                        fallback_public_id=item.get("containerId"),
                    )
                    entry["path"] = str(path)
                except Exception as exc:
                    entry["path"] = None
                    entry["error"] = str(exc)
            else:
                entry["path"] = None
        results.append(entry)
    return results


@tool("write")
def create_version(
    account_id: str,
    container_id: str,
    workspace_id: str,
    name: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Create an unpublished version after checking the workspace for changes and conflicts."""
    require("write")
    client = get_client()
    status = client.workspace_status(account_id, container_id, workspace_id)
    if status.get("mergeConflict"):
        return {"ok": False, "message": "Workspace has merge conflicts.", "status": status}
    if not status.get("workspaceChange"):
        return {"ok": False, "message": "Workspace has no pending changes.", "status": status}
    result = client.create_version(account_id, container_id, workspace_id, name, notes)
    return {"ok": True, "result": result}


@tool("write")
def update_version(
    account_id: str,
    container_id: str,
    version_id: str,
    body: dict[str, Any],
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Update a container version's metadata. Supply fingerprint for optimistic locking."""
    require("write")
    return get_writer().update_version(account_id, container_id, version_id, body, fingerprint)


@tool("write")
def delete_version(
    account_id: str, container_id: str, version_id: str, confirm: bool = False
) -> dict[str, Any]:
    """Delete a container version. Requires confirm=true."""
    require("write")
    if not confirm:
        return {"ok": False, "message": "Deleting a version requires confirm=true."}
    result = get_writer().delete_version(account_id, container_id, version_id)
    return {"ok": True, "result": result}


@tool("write")
def undelete_version(account_id: str, container_id: str, version_id: str) -> dict[str, Any]:
    """Restore a deleted container version."""
    require("write")
    return get_writer().undelete_version(account_id, container_id, version_id)


@tool("publish")
def set_latest_version(
    account_id: str,
    container_id: str,
    version_id: str,
    approval_id: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """Set a container version as latest after out-of-band terminal approval and confirm=true."""
    require("publish")
    if not confirm:
        return {
            "ok": False,
            "message": (
                "Setting the latest version changes what the latest environment serves "
                "and requires confirm=true."
            ),
        }
    if not isinstance(approval_id, str) or not approval_id.strip():
        return {
            "ok": False,
            "message": (
                "Setting the latest version requires an approval_id from prepare_publish "
                "(with latest=true), approved via `tag-manager-approve`."
            ),
        }
    header = _fetch_latest_version_header_optional(get_reader(), account_id, container_id)
    current_latest_version_id = str(header.get("containerVersionId") or "") if header else ""
    try:
        publish_guard.consume_approval(
            approval_id.strip(),
            account_id=account_id,
            container_id=container_id,
            version_id=version_id,
            target=publish_guard.TARGET_LATEST,
            current_live_version_id=current_latest_version_id,
        )
    except publish_guard.PublishApprovalError as exc:
        return {"ok": False, "message": str(exc)}
    result = get_writer().set_latest_version(account_id, container_id, version_id)
    return {"ok": True, "result": result}


@tool("read")
def get_environment(account_id: str, container_id: str, environment_id: str) -> dict[str, Any]:
    """Get one environment by ID (with authorizationCode redacted)."""
    return _redact_environment(
        get_writer().get_environment(account_id, container_id, environment_id)
    )


@tool("write")
def create_environment(
    account_id: str, container_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Create an environment in a container (accepts only name, description, url, enableDebug)."""
    require("write")
    error = _validate_environment_body(body)
    if error is not None:
        return error
    return _redact_environment(get_writer().create_environment(account_id, container_id, body))


@tool("write")
def update_environment(
    account_id: str,
    container_id: str,
    environment_id: str,
    body: dict[str, Any],
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Update an environment's metadata (accepts only name, description, url, enableDebug)."""
    require("write")
    error = _validate_environment_body(body)
    if error is not None:
        return error
    return _redact_environment(
        get_writer().update_environment(
            account_id, container_id, environment_id, body, fingerprint
        )
    )


@tool("write")
def delete_environment(
    account_id: str, container_id: str, environment_id: str, confirm: bool = False
) -> dict[str, Any]:
    """Delete an environment. Requires confirm=true."""
    require("write")
    if not confirm:
        return {"ok": False, "message": "Deleting an environment requires confirm=true."}
    result = get_writer().delete_environment(account_id, container_id, environment_id)
    return {"ok": True, "result": result}


@tool("publish")
def reauthorize_environment(
    account_id: str,
    container_id: str,
    environment_id: str,
) -> dict[str, Any]:
    """Re-generate the authorization code for an environment."""
    require("publish")
    return get_writer().reauthorize_environment(account_id, container_id, environment_id)


@tool("read")
def get_destination(account_id: str, container_id: str, destination_id: str) -> dict[str, Any]:
    """Get a destination linked to a container."""
    return get_writer().get_destination(account_id, container_id, destination_id)


@tool("write")
def link_destination(
    account_id: str,
    container_id: str,
    destination_id: str,
    allow_user_permission_feature_update: bool = False,
) -> dict[str, Any]:
    """Link a destination to a container."""
    require("write")
    return get_writer().link_destination(
        account_id,
        container_id,
        destination_id,
        allow_user_permission_feature_update=allow_user_permission_feature_update,
    )


@tool("read")
def get_container_snippet(account_id: str, container_id: str) -> dict[str, Any]:
    """Fetch the tagging snippet for a container."""
    return get_writer().get_container_snippet(account_id, container_id)


@tool("read")
def get_account(account_id: str) -> dict[str, Any]:
    """Get a GTM account by ID."""
    return get_writer().get_account(account_id)


@tool("admin")
def create_container(account_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Create a container in an account. Requires admin access."""
    require("admin")
    return get_writer().create_container(account_id, body)


@tool("admin")
def update_container(
    account_id: str,
    container_id: str,
    body: dict[str, Any],
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Update a container. Requires admin access."""
    require("admin")
    return get_writer().update_container(account_id, container_id, body, fingerprint)


@tool("admin")
def delete_container(
    account_id: str, container_id: str, confirm: bool = False
) -> dict[str, Any]:
    """Delete a container. Requires admin access and confirm=true."""
    require("admin")
    if not confirm:
        return {"ok": False, "message": "Deleting a container requires confirm=true."}
    result = get_writer().delete_container(account_id, container_id)
    return {"ok": True, "result": result}


@tool("admin")
def combine_containers(
    account_id: str,
    container_id: str,
    other_container_id: str,
    setting_source: CombineSettingSource | None = None,
    allow_user_permission_feature_update: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Combine another container into the target container. Requires admin and confirm=true."""
    require("admin")
    if not confirm:
        return {"ok": False, "message": "Combining containers requires confirm=true."}
    result = get_writer().combine_containers(
        account_id,
        container_id,
        other_container_id,
        setting_source=setting_source,
        allow_user_permission_feature_update=allow_user_permission_feature_update,
    )
    return {"ok": True, "result": result}


@tool("admin")
def move_tag_id(
    account_id: str,
    container_id: str,
    tag_id: str,
    tag_name: str | None = None,
    copy_settings: bool = False,
    copy_terms_of_service: bool = False,
    copy_users: bool = False,
    allow_user_permission_feature_update: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Move a tag ID into a new container. Requires admin access and confirm=true."""
    require("admin")
    if not confirm:
        return {"ok": False, "message": "Moving a tag ID requires confirm=true."}
    result = get_writer().move_tag_id(
        account_id,
        container_id,
        tag_id,
        tag_name=tag_name,
        copy_settings=copy_settings,
        copy_terms_of_service=copy_terms_of_service,
        copy_users=copy_users,
        allow_user_permission_feature_update=allow_user_permission_feature_update,
    )
    return {"ok": True, "result": result}


@tool("admin")
def update_account(
    account_id: str,
    body: dict[str, Any],
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Update a GTM account. Requires admin access."""
    require("admin")
    return get_writer().update_account(account_id, body, fingerprint)


@tool("admin")
def list_user_permissions(account_id: str) -> list[dict[str, Any]]:
    """List user permissions on a GTM account. Requires admin access."""
    require("admin")
    return get_writer().list_user_permissions(account_id)


@tool("admin")
def get_user_permission(account_id: str, permission_id: str) -> dict[str, Any]:
    """Get a user permission by ID. Requires admin access."""
    require("admin")
    return get_writer().get_user_permission(account_id, permission_id)


@tool("admin")
def create_user_permission(account_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Create a user permission on an account. Requires admin access."""
    require("admin")
    return get_writer().create_user_permission(account_id, body)


@tool("admin")
def update_user_permission(
    account_id: str, permission_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    """Update an existing user permission on an account. Requires admin access."""
    require("admin")
    return get_writer().update_user_permission(account_id, permission_id, body)


@tool("admin")
def delete_user_permission(
    account_id: str, permission_id: str, confirm: bool = False
) -> dict[str, Any]:
    """Delete a user permission from an account. Requires admin access and confirm=true."""
    require("admin")
    if not confirm:
        return {"ok": False, "message": "Deleting a user permission requires confirm=true."}
    result = get_writer().delete_user_permission(account_id, permission_id)
    return {"ok": True, "result": result}


@tool("read")
def plan_container_import(
    account_id: str,
    container_id: str,
    workspace_id: str,
    file_name: str,
    mode: ImportMode = "merge",
    conflict: ImportConflict = "skip",
) -> dict[str, Any]:
    """Plan importing a GTM export JSON file from GTM_EXPORT_DIR into a workspace."""
    export = _load_export_file(file_name)
    existing = _fetch_existing_workspace_entities(account_id, container_id, workspace_id)
    plan = plan_import(export, existing, mode=mode, conflict=conflict)
    return {**plan, "counts": _plan_counts(plan)}


@tool("write")
def apply_container_import(
    account_id: str,
    container_id: str,
    workspace_id: str,
    file_name: str,
    mode: ImportMode = "merge",
    conflict: ImportConflict = "skip",
    confirm: bool = False,
) -> dict[str, Any]:
    """Apply a GTM export JSON file from GTM_EXPORT_DIR to a workspace.

    Requires confirm=true whenever the computed plan updates or deletes any entity.
    """
    require("write")
    export = _load_export_file(file_name)
    existing = _fetch_existing_workspace_entities(account_id, container_id, workspace_id)
    plan = plan_import(export, existing, mode=mode, conflict=conflict)
    counts = _plan_counts(plan)
    if not confirm and (counts["update"] > 0 or counts["delete"] > 0):
        return {
            "ok": False,
            "message": (
                f"Import would update {counts['update']} and delete {counts['delete']} "
                "workspace entities; re-run with confirm=true to proceed."
            ),
            "counts": counts,
        }
    ops = _WorkspaceImportOps(get_client(), get_writer(), account_id, container_id, workspace_id)
    return apply_import(plan, export, ops, account_id, container_id, workspace_id)


@tool("publish")
def prepare_publish(
    account_id: str,
    container_id: str,
    version_id: str,
    environment_id: str | None = None,
    latest: bool = False,
) -> dict[str, Any]:
    """Create a pending publish approval request with a diff summary against the target version."""
    require("publish")
    if environment_id is not None and latest:
        raise ValueError("environment_id and latest are mutually exclusive.")

    reader = get_reader()
    version = reader.get_version(account_id, container_id, version_id)

    target_name: str | None = None
    if environment_id is not None:
        env_id = str(environment_id).strip()
        if not env_id or "/" in env_id:
            raise ValueError("environment_id must be a non-empty resource ID, not a path")
        target = f"{publish_guard.TARGET_ENVIRONMENT_PREFIX}{env_id}"
        env = get_writer().get_environment(account_id, container_id, env_id)
        target_name = str(env["name"]) if env.get("name") else None
        live_version_id = str(env.get("containerVersionId") or "")
        baseline = (
            reader.get_version(account_id, container_id, live_version_id)
            if live_version_id
            else None
        )
        next_tool = "publish_to_environment"
    elif latest:
        target = publish_guard.TARGET_LATEST
        header = _fetch_latest_version_header_optional(reader, account_id, container_id)
        live_version_id = str(header.get("containerVersionId") or "") if header else ""
        baseline = (
            reader.get_version(account_id, container_id, live_version_id)
            if live_version_id
            else None
        )
        next_tool = "set_latest_version"
    else:
        target = publish_guard.TARGET_LIVE
        baseline = _fetch_live_version_optional(reader, account_id, container_id)
        live_version_id = str(baseline.get("containerVersionId") or "") if baseline else ""
        next_tool = "publish_version"

    has_baseline = baseline is not None
    version_container = version.get("container") or {}
    baseline_container = (baseline.get("container") or {}) if baseline else {}
    public_id = str(
        version_container.get("publicId") or baseline_container.get("publicId") or ""
    ).strip()
    container_name = version_container.get("name") or baseline_container.get("name")
    if not public_id:
        container = get_client().get_container(account_id, container_id)
        public_id = str(container.get("publicId") or "").strip()
        if not container_name:
            container_name = container.get("name")
    if not public_id:
        raise ValueError(
            f"Could not determine publicId for account {account_id}, container {container_id}."
        )

    diff = diff_versions(baseline or {}, version)
    summary = _summarize_publish_diff(diff, has_live_version=has_baseline, target=target)
    version_name = str(version.get("name") or "")

    record = publish_guard.create_request(
        account_id=account_id,
        container_id=container_id,
        public_id=public_id,
        version_id=version_id,
        version_name=version_name,
        live_version_id=live_version_id,
        diff_summary=summary,
        target=target,
        target_name=target_name,
        container_name=str(container_name) if container_name else None,
    )
    approval_id = record["approval_id"]
    response: dict[str, Any] = {
        "approval_id": approval_id,
        "public_id": record["public_id"],
        "target": record["target"],
        "version_id": record["version_id"],
        "version_name": record["version_name"],
        "replaces_live_version_id": record["live_version_id"],
        "summary": summary,
        "expires_at": record["expires_at"],
        "next_step": (
            f"A human must run `tag-manager-approve {approval_id}` in a terminal "
            f"and type the container ID. Then call {next_tool} with this approval_id."
        ),
    }
    if target_name is not None:
        response["target_name"] = target_name
    return response


@tool("publish")
def publish_version(
    account_id: str,
    container_id: str,
    version_id: str,
    approval_id: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """Publish a container version live after out-of-band terminal approval and confirm=true."""
    require("publish")
    if not confirm:
        return {
            "ok": False,
            "message": "Publishing makes changes live and requires confirm=true.",
        }
    if not isinstance(approval_id, str) or not approval_id.strip():
        return {
            "ok": False,
            "message": (
                "Publishing requires an approval_id from prepare_publish, "
                "approved via `tag-manager-approve`."
            ),
        }
    live = _fetch_live_version_optional(get_reader(), account_id, container_id)
    current_live_version_id = str(live.get("containerVersionId") or "") if live else ""
    try:
        publish_guard.consume_approval(
            approval_id.strip(),
            account_id=account_id,
            container_id=container_id,
            version_id=version_id,
            target=publish_guard.TARGET_LIVE,
            current_live_version_id=current_live_version_id,
        )
    except publish_guard.PublishApprovalError as exc:
        return {"ok": False, "message": str(exc)}
    result = get_client().publish_version(account_id, container_id, version_id)
    return {"ok": True, "result": result}


@tool("publish")
def publish_to_environment(
    account_id: str,
    container_id: str,
    environment_id: str,
    version_id: str,
    approval_id: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """Point a user environment at a container version after terminal approval and confirm=true."""
    require("publish")
    if not confirm:
        return {
            "ok": False,
            "message": "Publishing to an environment requires confirm=true.",
        }
    if not isinstance(approval_id, str) or not approval_id.strip():
        return {
            "ok": False,
            "message": (
                "Publishing to an environment requires an approval_id from prepare_publish "
                "(with environment_id), approved via `tag-manager-approve`."
            ),
        }
    env_id = str(environment_id).strip()
    if not env_id or "/" in env_id:
        raise ValueError("environment_id must be a non-empty resource ID, not a path")
    writer = get_writer()
    env = writer.get_environment(account_id, container_id, env_id)
    current_version_id = str(env.get("containerVersionId") or "")
    try:
        publish_guard.consume_approval(
            approval_id.strip(),
            account_id=account_id,
            container_id=container_id,
            version_id=version_id,
            target=f"{publish_guard.TARGET_ENVIRONMENT_PREFIX}{env_id}",
            current_live_version_id=current_version_id,
        )
    except publish_guard.PublishApprovalError as exc:
        return {"ok": False, "message": str(exc)}
    update_body: dict[str, Any] = {
        field: env[field] for field in _ENV_MUTABLE_METADATA_FIELDS if field in env
    }
    update_body["containerVersionId"] = version_id
    fingerprint = env.get("fingerprint")
    result = writer.update_environment(
        account_id,
        container_id,
        env_id,
        update_body,
        fingerprint=fingerprint if isinstance(fingerprint, str) and fingerprint else None,
    )
    return {"ok": True, "result": _redact_environment(result)}


@tool("publish")
def list_publish_requests() -> list[dict[str, Any]]:
    """List pending and approved publish requests (read-only view)."""
    require("publish")
    return publish_guard.list_requests(include_closed=False)


def build_server(mode: AccessMode | str | None = None) -> LoggingFastMCP:
    """Create a server instance registering only tools permitted by the access mode."""
    active_mode = get_access_mode() if mode is None else parse_access_mode(mode)
    server = LoggingFastMCP("Google Tag Manager", instructions=SERVER_INSTRUCTIONS)
    server.access_mode = active_mode  # type: ignore[attr-defined]
    tool_registry.register(server, active_mode)
    return server


mcp = build_server()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
