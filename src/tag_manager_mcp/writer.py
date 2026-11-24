"""Write and administrative Google Tag Manager API v2 operations."""

from __future__ import annotations

from typing import Any

from .client import GTMClient, _id, _name, account_path, container_path, workspace_path

WRITER_ENTITY_KINDS: tuple[str, ...] = (
    "templates",
    "clients",
    "transformations",
    "zones",
    "gtag_config",
)

CLIENT_ENTITY_KINDS: tuple[str, ...] = (
    "tags",
    "triggers",
    "variables",
    "folders",
)

REVERTIBLE_ENTITY_KINDS: tuple[str, ...] = (
    "tags",
    "triggers",
    "variables",
    "folders",
    "templates",
    "clients",
    "transformations",
    "zones",
)

_COMBINE_SETTING_SOURCES: frozenset[str] = frozenset(
    {"settingSourceUnspecified", "current", "other"}
)

OPERATION_SCOPES: dict[str, str] = {
    "create_workspace_entity": "edit.containers",
    "update_workspace_entity": "edit.containers",
    "delete_workspace_entity": "edit.containers",
    "revert_workspace_entity": "edit.containers",
    "revert_builtin_variable": "edit.containers",
    "import_template_from_gallery": "edit.containers",
    "list_folder_entities": "readonly",
    "move_entities_to_folder": "edit.containers",
    "get_workspace": "readonly",
    "update_workspace": "edit.containers",
    "delete_workspace": "delete.containers",
    "resolve_conflict": "edit.containers",
    "quick_preview": "edit.containerversions",
    "bulk_update": "edit.containers",
    "update_version": "edit.containerversions",
    "delete_version": "edit.containerversions",
    "undelete_version": "edit.containerversions",
    "set_latest_version": "edit.containers",
    "get_environment": "readonly",
    "create_environment": "edit.containers",
    "update_environment": "edit.containers",
    "delete_environment": "edit.containers",
    "reauthorize_environment": "publish",
    "get_destination": "readonly",
    "link_destination": "edit.containers",
    "get_container_snippet": "readonly",
    "create_container": "edit.containers",
    "update_container": "edit.containers",
    "delete_container": "delete.containers",
    "combine_containers": "edit.containers",
    "move_tag_id": "edit.containers",
    "get_account": "readonly",
    "update_account": "manage.accounts",
    "list_user_permissions": "manage.users",
    "get_user_permission": "manage.users",
    "create_user_permission": "manage.users",
    "update_user_permission": "manage.users",
    "delete_user_permission": "manage.users",
}


class GTMWriter:
    """Facade over GTMClient covering remaining GTM API v2 write and admin operations."""

    def __init__(self, client: GTMClient) -> None:
        self.client = client

    def _containers(self) -> Any:
        return self.client.service.accounts().containers()

    def _workspaces(self) -> Any:
        return self._containers().workspaces()

    def create_workspace_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a workspace entity for a kind handled by GTMWriter."""
        _validate_writer_entity_kind(kind)
        _validate_body(body)
        parent = workspace_path(account_id, container_id, workspace_id)
        return self.client._execute(
            lambda: getattr(self._workspaces(), kind)().create(parent=parent, body=body)
        )

    def update_workspace_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        entity_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing workspace entity for a kind handled by GTMWriter."""
        _validate_writer_entity_kind(kind)
        _validate_body(body)
        path = _workspace_entity_path(kind, account_id, container_id, workspace_id, entity_id)
        kwargs = _with_fingerprint({"path": path, "body": body}, fingerprint)
        return self.client._execute(lambda: getattr(self._workspaces(), kind)().update(**kwargs))

    def delete_workspace_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        entity_id: str,
    ) -> dict[str, Any]:
        """Delete a workspace entity for a kind handled by GTMWriter."""
        _validate_writer_entity_kind(kind)
        path = _workspace_entity_path(kind, account_id, container_id, workspace_id, entity_id)
        return self.client._execute(lambda: getattr(self._workspaces(), kind)().delete(path=path))

    def revert_workspace_entity(
        self,
        kind: str,
        account_id: str,
        container_id: str,
        workspace_id: str,
        entity_id: str,
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Revert workspace changes to an entity for any revertible workspace entity kind."""
        if kind not in REVERTIBLE_ENTITY_KINDS:
            raise ValueError(f"unsupported or non-revertible entity kind: {kind}")
        path = _workspace_entity_path(kind, account_id, container_id, workspace_id, entity_id)
        kwargs = _with_fingerprint({"path": path}, fingerprint)
        return self.client._execute(lambda: getattr(self._workspaces(), kind)().revert(**kwargs))

    def revert_builtin_variable(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        variable_type: str,
    ) -> dict[str, Any]:
        """Revert changes to a built-in variable in a workspace."""
        normalized_type = _non_empty(variable_type, "variable_type")
        path = workspace_path(account_id, container_id, workspace_id)
        return self.client._execute(
            lambda: self._workspaces().built_in_variables().revert(path=path, type=normalized_type)
        )

    def import_template_from_gallery(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        gallery_owner: str,
        gallery_repository: str,
        gallery_sha: str | None = None,
        acknowledge_permissions: bool = False,
    ) -> dict[str, Any]:
        """Import a Community Template Gallery custom template into a workspace."""
        parent = workspace_path(account_id, container_id, workspace_id)
        kwargs: dict[str, Any] = {
            "parent": parent,
            "galleryOwner": _non_empty(gallery_owner, "gallery_owner"),
            "galleryRepository": _non_empty(gallery_repository, "gallery_repository"),
            "acknowledgePermissions": acknowledge_permissions,
        }
        if gallery_sha is not None:
            kwargs["gallerySha"] = _non_empty(gallery_sha, "gallery_sha")
        return self.client._execute(
            lambda: self._workspaces().templates().import_from_gallery(**kwargs)
        )

    def list_folder_entities(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        folder_id: str,
    ) -> dict[str, Any]:
        """List all tags, triggers, and variables inside a folder across all pages."""
        path = _workspace_entity_path("folders", account_id, container_id, workspace_id, folder_id)
        folders = self._workspaces().folders()
        tags: list[dict[str, Any]] = []
        triggers: list[dict[str, Any]] = []
        variables: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            response = self.client._execute(
                lambda token=page_token: folders.entities(path=path, pageToken=token)
                if token
                else folders.entities(path=path)
            )
            tags.extend(response.get("tag", []))
            triggers.extend(response.get("trigger", []))
            variables.extend(response.get("variable", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return {"tag": tags, "trigger": triggers, "variable": variables}

    def move_entities_to_folder(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        folder_id: str,
        tag_ids: list[str] | None = None,
        trigger_ids: list[str] | None = None,
        variable_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Move tags, triggers, and/or variables to a folder (folder_id='0' removes from folder)."""
        path = _workspace_entity_path("folders", account_id, container_id, workspace_id, folder_id)
        kwargs: dict[str, Any] = {"path": path}
        if tag_ids:
            kwargs["tagId"] = [_id(tid, "tag_id") for tid in tag_ids]
        if trigger_ids:
            kwargs["triggerId"] = [_id(tid, "trigger_id") for tid in trigger_ids]
        if variable_ids:
            kwargs["variableId"] = [_id(vid, "variable_id") for vid in variable_ids]
        return self.client._execute(
            lambda: self._workspaces().folders().move_entities_to_folder(**kwargs)
        )

    def get_workspace(
        self, account_id: str, container_id: str, workspace_id: str
    ) -> dict[str, Any]:
        """Fetch a workspace by ID."""
        path = workspace_path(account_id, container_id, workspace_id)
        return self.client._execute(lambda: self._workspaces().get(path=path))

    def update_workspace(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Update a workspace."""
        _validate_body(body)
        path = workspace_path(account_id, container_id, workspace_id)
        kwargs = _with_fingerprint({"path": path, "body": body}, fingerprint)
        return self.client._execute(lambda: self._workspaces().update(**kwargs))

    def delete_workspace(
        self, account_id: str, container_id: str, workspace_id: str
    ) -> dict[str, Any]:
        """Delete a workspace."""
        path = workspace_path(account_id, container_id, workspace_id)
        return self.client._execute(lambda: self._workspaces().delete(path=path))

    def resolve_conflict(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a workspace merge conflict with the given Entity payload."""
        _validate_body(body)
        path = workspace_path(account_id, container_id, workspace_id)
        kwargs = _with_fingerprint({"path": path, "body": body}, fingerprint)
        return self.client._execute(lambda: self._workspaces().resolve_conflict(**kwargs))

    def quick_preview(
        self, account_id: str, container_id: str, workspace_id: str
    ) -> dict[str, Any]:
        """Quick-preview the current workspace."""
        path = workspace_path(account_id, container_id, workspace_id)
        return self.client._execute(lambda: self._workspaces().quick_preview(path=path))

    def bulk_update(
        self,
        account_id: str,
        container_id: str,
        workspace_id: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply multiple ProposedChange entity updates to a workspace in one request."""
        _validate_body(body)
        path = workspace_path(account_id, container_id, workspace_id)
        return self.client._execute(lambda: self._workspaces().bulk_update(path=path, body=body))

    def update_version(
        self,
        account_id: str,
        container_id: str,
        version_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Update a container version."""
        _validate_body(body)
        path = _version_path(account_id, container_id, version_id)
        kwargs = _with_fingerprint({"path": path, "body": body}, fingerprint)
        return self.client._execute(lambda: self._containers().versions().update(**kwargs))

    def delete_version(
        self, account_id: str, container_id: str, version_id: str
    ) -> dict[str, Any]:
        """Delete a container version."""
        path = _version_path(account_id, container_id, version_id)
        return self.client._execute(lambda: self._containers().versions().delete(path=path))

    def undelete_version(
        self, account_id: str, container_id: str, version_id: str
    ) -> dict[str, Any]:
        """Restore a deleted container version."""
        path = _version_path(account_id, container_id, version_id)
        return self.client._execute(lambda: self._containers().versions().undelete(path=path))

    def set_latest_version(
        self, account_id: str, container_id: str, version_id: str
    ) -> dict[str, Any]:
        """Set a container version as the latest version."""
        path = _version_path(account_id, container_id, version_id)
        return self.client._execute(lambda: self._containers().versions().set_latest(path=path))

    def get_environment(
        self, account_id: str, container_id: str, environment_id: str
    ) -> dict[str, Any]:
        """Fetch an environment by ID."""
        path = _environment_path(account_id, container_id, environment_id)
        return self.client._execute(lambda: self._containers().environments().get(path=path))

    def create_environment(
        self, account_id: str, container_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Create an environment in a container."""
        _validate_body(body)
        parent = container_path(account_id, container_id)
        return self.client._execute(
            lambda: self._containers().environments().create(parent=parent, body=body)
        )

    def update_environment(
        self,
        account_id: str,
        container_id: str,
        environment_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Update an environment."""
        _validate_body(body)
        path = _environment_path(account_id, container_id, environment_id)
        kwargs = _with_fingerprint({"path": path, "body": body}, fingerprint)
        return self.client._execute(lambda: self._containers().environments().update(**kwargs))

    def delete_environment(
        self, account_id: str, container_id: str, environment_id: str
    ) -> dict[str, Any]:
        """Delete an environment."""
        path = _environment_path(account_id, container_id, environment_id)
        return self.client._execute(lambda: self._containers().environments().delete(path=path))

    def reauthorize_environment(
        self,
        account_id: str,
        container_id: str,
        environment_id: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Re-generate the authorization code for an environment."""
        path = _environment_path(account_id, container_id, environment_id)
        kwargs: dict[str, Any] = {"path": path}
        if body is not None:
            _validate_body(body)
            kwargs["body"] = body
        return self.client._execute(
            lambda: self._containers().environments().reauthorize(**kwargs)
        )

    def get_destination(
        self, account_id: str, container_id: str, destination_id: str
    ) -> dict[str, Any]:
        """Fetch a destination linked to a container."""
        path = _destination_path(account_id, container_id, destination_id)
        return self.client._execute(lambda: self._containers().destinations().get(path=path))

    def link_destination(
        self,
        account_id: str,
        container_id: str,
        destination_id: str,
        allow_user_permission_feature_update: bool = False,
    ) -> dict[str, Any]:
        """Link a destination to a container."""
        parent = container_path(account_id, container_id)
        dest_id = _id(destination_id, "destination_id")
        return self.client._execute(
            lambda: self._containers().destinations().link(
                parent=parent,
                destinationId=dest_id,
                allowUserPermissionFeatureUpdate=allow_user_permission_feature_update,
            )
        )

    def get_container_snippet(self, account_id: str, container_id: str) -> dict[str, Any]:
        """Fetch the tagging snippet for a container."""
        path = container_path(account_id, container_id)
        return self.client._execute(lambda: self._containers().snippet(path=path))

    def create_container(self, account_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create a container in an account."""
        _validate_body(body)
        parent = account_path(account_id)
        return self.client._execute(lambda: self._containers().create(parent=parent, body=body))

    def update_container(
        self,
        account_id: str,
        container_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Update a container."""
        _validate_body(body)
        path = container_path(account_id, container_id)
        kwargs = _with_fingerprint({"path": path, "body": body}, fingerprint)
        return self.client._execute(lambda: self._containers().update(**kwargs))

    def delete_container(self, account_id: str, container_id: str) -> dict[str, Any]:
        """Delete a container."""
        path = container_path(account_id, container_id)
        return self.client._execute(lambda: self._containers().delete(path=path))

    def combine_containers(
        self,
        account_id: str,
        container_id: str,
        other_container_id: str,
        setting_source: str | None = None,
        allow_user_permission_feature_update: bool = False,
    ) -> dict[str, Any]:
        """Combine another container into the target container."""
        path = container_path(account_id, container_id)
        merged_id = _id(other_container_id, "other_container_id")
        kwargs: dict[str, Any] = {
            "path": path,
            "containerId": merged_id,
            "allowUserPermissionFeatureUpdate": allow_user_permission_feature_update,
        }
        if setting_source is not None:
            if setting_source not in _COMBINE_SETTING_SOURCES:
                allowed = ", ".join(sorted(_COMBINE_SETTING_SOURCES))
                raise ValueError(f"setting_source must be one of: {allowed}")
            kwargs["settingSource"] = setting_source
        return self.client._execute(lambda: self._containers().combine(**kwargs))

    def move_tag_id(
        self,
        account_id: str,
        container_id: str,
        tag_id: str,
        tag_name: str | None = None,
        copy_settings: bool = False,
        copy_terms_of_service: bool = False,
        copy_users: bool = False,
        allow_user_permission_feature_update: bool = False,
    ) -> dict[str, Any]:
        """Move a tag ID out of a container into a newly created container."""
        path = container_path(account_id, container_id)
        kwargs: dict[str, Any] = {
            "path": path,
            "tagId": _id(tag_id, "tag_id"),
            "copySettings": copy_settings,
            "copyTermsOfService": copy_terms_of_service,
            "copyUsers": copy_users,
            "allowUserPermissionFeatureUpdate": allow_user_permission_feature_update,
        }
        if tag_name is not None:
            kwargs["tagName"] = _name(tag_name)
        return self.client._execute(lambda: self._containers().move_tag_id(**kwargs))

    def get_account(self, account_id: str) -> dict[str, Any]:
        """Fetch an account by ID."""
        path = account_path(account_id)
        return self.client._execute(lambda: self.client.service.accounts().get(path=path))

    def update_account(
        self,
        account_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Update an account."""
        _validate_body(body)
        path = account_path(account_id)
        kwargs = _with_fingerprint({"path": path, "body": body}, fingerprint)
        return self.client._execute(lambda: self.client.service.accounts().update(**kwargs))

    def list_user_permissions(self, account_id: str) -> list[dict[str, Any]]:
        """List all user permissions on an account across all pages."""
        parent = account_path(account_id)
        permissions = self.client.service.accounts().user_permissions()
        return self.client._list_all(
            lambda token: permissions.list(parent=parent, pageToken=token)
            if token
            else permissions.list(parent=parent),
            "userPermission",
        )

    def get_user_permission(self, account_id: str, permission_id: str) -> dict[str, Any]:
        """Fetch a user permission by ID."""
        path = _user_permission_path(account_id, permission_id)
        return self.client._execute(
            lambda: self.client.service.accounts().user_permissions().get(path=path)
        )

    def create_user_permission(self, account_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create a user permission on an account."""
        _validate_body(body)
        parent = account_path(account_id)
        return self.client._execute(
            lambda: self.client.service.accounts().user_permissions().create(
                parent=parent, body=body
            )
        )

    def update_user_permission(
        self, account_id: str, permission_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Update an existing user permission on an account."""
        _validate_body(body)
        path = _user_permission_path(account_id, permission_id)
        return self.client._execute(
            lambda: self.client.service.accounts().user_permissions().update(path=path, body=body)
        )

    def delete_user_permission(self, account_id: str, permission_id: str) -> dict[str, Any]:
        """Delete a user permission from an account."""
        path = _user_permission_path(account_id, permission_id)
        return self.client._execute(
            lambda: self.client.service.accounts().user_permissions().delete(path=path)
        )


def _validate_writer_entity_kind(kind: str) -> None:
    if kind in CLIENT_ENTITY_KINDS:
        raise ValueError(f"entity kind '{kind}' is managed by GTMClient; use GTMClient instead")
    if kind not in WRITER_ENTITY_KINDS:
        raise ValueError(f"unsupported entity kind: {kind}")


def _validate_body(body: Any) -> None:
    if not isinstance(body, dict) or not body:
        raise ValueError("body must be a non-empty JSON object")


def _non_empty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _with_fingerprint(kwargs: dict[str, Any], fingerprint: str | None) -> dict[str, Any]:
    if fingerprint:
        kwargs["fingerprint"] = fingerprint
    return kwargs


def _workspace_entity_path(
    kind: str, account_id: str, container_id: str, workspace_id: str, entity_id: str
) -> str:
    clean_id = _id(entity_id, f"{kind}_id")
    return f"{workspace_path(account_id, container_id, workspace_id)}/{kind}/{clean_id}"


def _version_path(account_id: str, container_id: str, version_id: str) -> str:
    clean_id = _id(version_id, "version_id")
    return f"{container_path(account_id, container_id)}/versions/{clean_id}"


def _environment_path(account_id: str, container_id: str, environment_id: str) -> str:
    clean_id = _id(environment_id, "environment_id")
    return f"{container_path(account_id, container_id)}/environments/{clean_id}"


def _destination_path(account_id: str, container_id: str, destination_id: str) -> str:
    clean_id = _id(destination_id, "destination_id")
    return f"{container_path(account_id, container_id)}/destinations/{clean_id}"


def _user_permission_path(account_id: str, permission_id: str) -> str:
    clean_id = _id(permission_id, "permission_id")
    return f"{account_path(account_id)}/user_permissions/{clean_id}"
