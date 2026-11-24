"""Read-only Google Tag Manager container, workspace, and inventory helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .auth import AuthenticationError
from .client import GTMClient, _id, account_path, container_path, workspace_path
from .export_format import to_ui_export

WORKSPACE_ENTITY_SPECS: dict[str, tuple[str, bool]] = {
    "tags": ("tag", True),
    "triggers": ("trigger", True),
    "variables": ("variable", True),
    "folders": ("folder", True),
    "templates": ("template", True),
    "clients": ("client", True),
    "transformations": ("transformation", True),
    "zones": ("zone", True),
    "gtag_config": ("gtagConfig", True),
    "built_in_variables": ("builtInVariable", False),
}

VERSION_ENTITY_KEYS: tuple[str, ...] = (
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
)


class GTMReader:
    """Read-only facade over GTMClient for container versions, entities, and inventories."""

    def __init__(self, client: GTMClient) -> None:
        self.client = client

    def live_version(self, account_id: str, container_id: str) -> dict[str, Any]:
        """Fetch the currently published (live) ContainerVersion for a container."""
        parent = container_path(account_id, container_id)
        return self.client._execute(
            lambda: self.client.service.accounts().containers().versions().live(parent=parent)
        )

    def get_version(self, account_id: str, container_id: str, version_id: str) -> dict[str, Any]:
        """Fetch a specific ContainerVersion by version ID."""
        version_id = _id(version_id, "version_id")
        path = f"{container_path(account_id, container_id)}/versions/{version_id}"
        return self.client._execute(
            lambda: self.client.service.accounts().containers().versions().get(path=path)
        )

    def latest_version_header(self, account_id: str, container_id: str) -> dict[str, Any]:
        """Fetch the latest ContainerVersionHeader for a container."""
        parent = container_path(account_id, container_id)
        return self.client._execute(
            lambda: self.client.service.accounts()
            .containers()
            .version_headers()
            .latest(parent=parent)
        )

    def export_container(
        self, account_id: str, container_id: str, version_id: str | None = None
    ) -> dict[str, Any]:
        """Return a container version wrapped in the GTM UI Export Container JSON structure."""
        version = (
            self.live_version(account_id, container_id)
            if version_id is None
            else self.get_version(account_id, container_id, version_id)
        )
        return {
            "exportFormatVersion": 2,
            "exportTime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "containerVersion": to_ui_export(version),
        }

    def list_workspace_entities(
        self, kind: str, account_id: str, container_id: str, workspace_id: str
    ) -> list[dict[str, Any]]:
        """List all entities of a given kind in a workspace."""
        response_key, _ = _workspace_entity_spec(kind)
        parent = workspace_path(account_id, container_id, workspace_id)
        api = getattr(self.client.service.accounts().containers().workspaces(), kind)()
        return self.client._list_all(
            lambda token: api.list(parent=parent, pageToken=token)
            if token
            else api.list(parent=parent),
            response_key,
        )

    def get_workspace_entity(
        self, kind: str, account_id: str, container_id: str, workspace_id: str, entity_id: str
    ) -> dict[str, Any]:
        """Fetch a single workspace entity by ID for kinds supporting get."""
        _, has_get = _workspace_entity_spec(kind)
        if not has_get:
            raise ValueError(f"entity kind '{kind}' does not support get")
        entity_id = _id(entity_id, f"{kind}_id")
        path = f"{workspace_path(account_id, container_id, workspace_id)}/{kind}/{entity_id}"
        api = getattr(self.client.service.accounts().containers().workspaces(), kind)()
        return self.client._execute(lambda: api.get(path=path))

    def list_environments(self, account_id: str, container_id: str) -> list[dict[str, Any]]:
        """List all environments in a container."""
        parent = container_path(account_id, container_id)
        environments = self.client.service.accounts().containers().environments()
        return self.client._list_all(
            lambda token: environments.list(parent=parent, pageToken=token)
            if token
            else environments.list(parent=parent),
            "environment",
        )

    def list_destinations(self, account_id: str, container_id: str) -> list[dict[str, Any]]:
        """List all destinations linked to a container."""
        parent = container_path(account_id, container_id)
        destinations = self.client.service.accounts().containers().destinations()
        response = self.client._execute(lambda: destinations.list(parent=parent))
        return self.client._items(response, "destination")

    def list_user_permissions(self, account_id: str) -> list[dict[str, Any]]:
        """List all user permissions on a GTM account."""
        parent = account_path(account_id)
        permissions = self.client.service.accounts().user_permissions()
        return self.client._list_all(
            lambda token: permissions.list(parent=parent, pageToken=token)
            if token
            else permissions.list(parent=parent),
            "userPermission",
        )

    def inventory(self) -> list[dict[str, Any]]:
        """Inventory every accessible account and its containers without aborting on errors."""
        results: list[dict[str, Any]] = []
        for account in self.client.list_accounts():
            account_id = str(account.get("accountId", ""))
            entry: dict[str, Any] = {
                "accountId": account_id,
                "name": account.get("name", ""),
                "containers": [],
                "error": None,
            }
            try:
                raw_containers = self.client.list_containers(account_id)
                entry["containers"] = [
                    {
                        "containerId": container.get("containerId", ""),
                        "publicId": container.get("publicId", ""),
                        "name": container.get("name", ""),
                        "usageContext": list(container.get("usageContext") or []),
                    }
                    for container in raw_containers
                ]
            except Exception as exc:
                entry["error"] = str(exc)
            results.append(entry)
        return results

    def fetch_all_live(self, usage_context: str | None = "web") -> list[dict[str, Any]]:
        """Fetch the live version for every accessible container matching usage_context."""
        results: list[dict[str, Any]] = []
        for account in self.inventory():
            account_id = account["accountId"]
            for container in account["containers"]:
                contexts = container.get("usageContext") or []
                if usage_context is not None and usage_context not in contexts:
                    continue
                container_id = str(container.get("containerId", ""))
                version: dict[str, Any] | None = None
                error: str | None = None
                try:
                    version = self.live_version(account_id, container_id)
                except Exception as exc:
                    error = str(exc)
                results.append(
                    {
                        "accountId": account_id,
                        "containerId": container_id,
                        "publicId": container.get("publicId", ""),
                        "name": container.get("name", ""),
                        "version": version,
                        "error": error,
                    }
                )
        return results

    def fetch_live_for(self, public_ids: list[str]) -> list[dict[str, Any]]:
        """Resolve each public container ID directly and fetch only its live version."""
        results: list[dict[str, Any]] = []
        for raw_id in public_ids:
            identifier = str(raw_id).strip().upper()
            account_id = ""
            container_id = ""
            public_id = identifier
            name = ""
            version: dict[str, Any] | None = None
            error: str | None = None
            try:
                container = self.client.resolve_container_identifier(identifier)
                account_id = str(container.get("accountId", ""))
                container_id = str(container.get("containerId", ""))
                public_id = str(container.get("publicId") or identifier)
                name = str(container.get("name", ""))
                version = self.live_version(account_id, container_id)
            except AuthenticationError:
                raise
            except Exception as exc:
                error = str(exc)
            results.append(
                {
                    "accountId": account_id,
                    "containerId": container_id,
                    "publicId": public_id,
                    "name": name,
                    "version": version,
                    "error": error,
                }
            )
        return results


def summarize_version(version: dict[str, Any]) -> dict[str, Any]:
    """Return entity counts and paused tag names for a ContainerVersion dictionary."""
    tags = version.get("tag") or []
    paused_tags = [
        str(tag.get("name", ""))
        for tag in tags
        if isinstance(tag, dict) and bool(tag.get("paused"))
    ]
    counts = {key: len(version.get(key) or []) for key in VERSION_ENTITY_KEYS}
    return {
        "containerVersionId": version.get("containerVersionId"),
        "name": version.get("name"),
        "counts": counts,
        "pausedTags": paused_tags,
    }


def _workspace_entity_spec(kind: str) -> tuple[str, bool]:
    if kind not in WORKSPACE_ENTITY_SPECS:
        raise ValueError(f"unsupported entity kind: {kind}")
    return WORKSPACE_ENTITY_SPECS[kind]
