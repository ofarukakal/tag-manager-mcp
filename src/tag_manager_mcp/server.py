"""FastMCP tools for Google Tag Manager."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP

from .access import (
    AccessMode,
    ToolRegistry,
    get_access_mode,
    parse_access_mode,
    require,
    scopes_for_mode,
)
from .client import GTMClient
from .debug_log import call_id_context, get_debug_logger
from .diff import diff_versions
from .export_files import write_version_export
from .reader import VERSION_ENTITY_KEYS, GTMReader, summarize_version

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
tools to read.
"""

tool_registry = ToolRegistry()
tool = tool_registry.tool
_client: GTMClient | None = None
_reader: GTMReader | None = None
EntityKind = Literal["tags", "triggers", "variables", "folders"]
ReadEntityKind = Literal[
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
_EXISTING_ENTITY_KINDS = frozenset({"tags", "triggers", "variables", "folders"})


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
    """Create a tag, trigger, variable, or folder from a GTM API v2 JSON body."""
    require("write")
    return get_client().create_entity(kind, account_id, container_id, workspace_id, body)


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
    """Update a tag, trigger, variable, or folder. Supply fingerprint for optimistic locking."""
    require("write")
    return get_client().update_entity(
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
    """Delete an entity. Requires confirm=true because this operation is destructive."""
    require("write")
    if not confirm:
        return {"ok": False, "message": "Deletion requires confirm=true."}
    result = get_client().delete_entity(kind, account_id, container_id, workspace_id, entity_id)
    return {"ok": True, "result": result}


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


@tool("read")
def list_versions(account_id: str, container_id: str) -> list[dict[str, Any]]:
    """List version headers for a GTM container."""
    return get_client().list_versions(account_id, container_id)


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
    """List environments in a GTM container."""
    return get_reader().list_environments(account_id, container_id)


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


@tool("publish")
def publish_version(
    account_id: str, container_id: str, version_id: str, confirm: bool = False
) -> dict[str, Any]:
    """Publish a container version live. Requires confirm=true."""
    require("publish")
    if not confirm:
        return {
            "ok": False,
            "message": "Publishing makes changes live and requires confirm=true.",
        }
    result = get_client().publish_version(account_id, container_id, version_id)
    return {"ok": True, "result": result}


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
