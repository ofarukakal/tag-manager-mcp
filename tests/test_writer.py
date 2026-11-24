from __future__ import annotations

import inspect
from typing import Any

import pytest

from tag_manager_mcp.client import GTMClient
from tag_manager_mcp.quota import QuotaGuard
from tag_manager_mcp.writer import (
    CLIENT_ENTITY_KINDS,
    OPERATION_SCOPES,
    REVERTIBLE_ENTITY_KINDS,
    WRITER_ENTITY_KINDS,
    GTMWriter,
)


class FakeRequest:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self) -> Any:
        return self._payload


class RecordingEndpoint:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, method_name: str) -> Any:
        def _method(**kwargs: Any) -> FakeRequest:
            self.calls.append((method_name, kwargs))
            payload = self._responses.get(method_name, {"ok": method_name})
            if callable(payload):
                payload = payload(kwargs)
            return FakeRequest(payload)

        return _method


def _make_writer(tmp_path: Any, service: Any) -> GTMWriter:
    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "100",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        }
    )
    client = GTMClient(service=service, quota_guard=guard)
    client.max_quota_retries = 0
    return GTMWriter(client)


def _build_full_fake_service() -> tuple[Any, dict[str, RecordingEndpoint]]:
    endpoints: dict[str, RecordingEndpoint] = {
        "accounts": RecordingEndpoint(),
        "user_permissions": RecordingEndpoint(
            {
                "list": lambda kwargs: (
                    {
                        "userPermission": [{"emailAddress": "b@example.com"}],
                    }
                    if kwargs.get("pageToken") == "p2"
                    else {
                        "userPermission": [{"emailAddress": "a@example.com"}],
                        "nextPageToken": "p2",
                    }
                )
            }
        ),
        "containers": RecordingEndpoint(),
        "destinations": RecordingEndpoint(),
        "environments": RecordingEndpoint(),
        "versions": RecordingEndpoint(),
        "workspaces": RecordingEndpoint(),
        "built_in_variables": RecordingEndpoint({"revert": {"enabled": True}}),
        "clients": RecordingEndpoint(),
        "folders": RecordingEndpoint(
            {
                "entities": lambda kwargs: (
                    {
                        "tag": [{"tagId": "2"}],
                        "variable": [{"variableId": "9"}],
                    }
                    if kwargs.get("pageToken") == "fp2"
                    else {
                        "tag": [{"tagId": "1"}],
                        "trigger": [{"triggerId": "5"}],
                        "nextPageToken": "fp2",
                    }
                )
            }
        ),
        "gtag_config": RecordingEndpoint(),
        "tags": RecordingEndpoint(),
        "templates": RecordingEndpoint(),
        "transformations": RecordingEndpoint(),
        "triggers": RecordingEndpoint(),
        "variables": RecordingEndpoint(),
        "zones": RecordingEndpoint(),
    }

    class Versions(RecordingEndpoint):
        def __init__(self) -> None:
            super().__init__()
            self.calls = endpoints["versions"].calls

        def __getattr__(self, method_name: str) -> Any:
            if method_name == "publish":
                raise AssertionError("versions.publish must never be accessed by GTMWriter")
            return getattr(endpoints["versions"], method_name)

    versions_proxy = Versions()

    class Workspaces:
        def __getattr__(self, method_name: str) -> Any:
            if method_name in endpoints:
                return lambda: endpoints[method_name]
            return getattr(endpoints["workspaces"], method_name)

    class Containers:
        def destinations(self) -> RecordingEndpoint:
            return endpoints["destinations"]

        def environments(self) -> RecordingEndpoint:
            return endpoints["environments"]

        def versions(self) -> Versions:
            return versions_proxy

        def workspaces(self) -> Workspaces:
            return Workspaces()

        def __getattr__(self, method_name: str) -> Any:
            return getattr(endpoints["containers"], method_name)

    class Accounts:
        def containers(self) -> Containers:
            return Containers()

        def user_permissions(self) -> RecordingEndpoint:
            return endpoints["user_permissions"]

        def __getattr__(self, method_name: str) -> Any:
            return getattr(endpoints["accounts"], method_name)

    class Service:
        def accounts(self) -> Accounts:
            return Accounts()

    return Service(), endpoints


@pytest.mark.parametrize("kind", WRITER_ENTITY_KINDS)
def test_workspace_entity_crud_and_fingerprint(tmp_path: Any, kind: str) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    created = writer.create_workspace_entity(kind, "1", "2", "3", {"name": "Entity"})
    assert created == {"ok": "create"}

    updated_no_fp = writer.update_workspace_entity(kind, "1", "2", "3", "9", {"name": "Updated"})
    assert updated_no_fp == {"ok": "update"}

    updated_fp = writer.update_workspace_entity(
        kind, "1", "2", "3", "9", {"name": "Updated"}, fingerprint="fp1"
    )
    assert updated_fp == {"ok": "update"}

    deleted = writer.delete_workspace_entity(kind, "1", "2", "3", "9")
    assert deleted == {"ok": "delete"}

    assert endpoints[kind].calls == [
        ("create", {"parent": "accounts/1/containers/2/workspaces/3", "body": {"name": "Entity"}}),
        (
            "update",
            {
                "path": f"accounts/1/containers/2/workspaces/3/{kind}/9",
                "body": {"name": "Updated"},
            },
        ),
        (
            "update",
            {
                "path": f"accounts/1/containers/2/workspaces/3/{kind}/9",
                "body": {"name": "Updated"},
                "fingerprint": "fp1",
            },
        ),
        ("delete", {"path": f"accounts/1/containers/2/workspaces/3/{kind}/9"}),
    ]


@pytest.mark.parametrize("kind", CLIENT_ENTITY_KINDS)
def test_workspace_entity_rejects_client_managed_kinds(tmp_path: Any, kind: str) -> None:
    service, _ = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    with pytest.raises(ValueError, match="GTMClient"):
        writer.create_workspace_entity(kind, "1", "2", "3", {"name": "X"})
    with pytest.raises(ValueError, match="GTMClient"):
        writer.update_workspace_entity(kind, "1", "2", "3", "4", {"name": "X"})
    with pytest.raises(ValueError, match="GTMClient"):
        writer.delete_workspace_entity(kind, "1", "2", "3", "4")


def test_workspace_entity_rejects_unknown_kind(tmp_path: Any) -> None:
    service, _ = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    with pytest.raises(ValueError, match="unsupported entity kind"):
        writer.create_workspace_entity("unknown", "1", "2", "3", {"name": "X"})
    with pytest.raises(ValueError, match="unsupported entity kind"):
        writer.update_workspace_entity("unknown", "1", "2", "3", "4", {"name": "X"})
    with pytest.raises(ValueError, match="unsupported entity kind"):
        writer.delete_workspace_entity("unknown", "1", "2", "3", "4")


@pytest.mark.parametrize("kind", REVERTIBLE_ENTITY_KINDS)
def test_revert_workspace_entity_all_kinds(tmp_path: Any, kind: str) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    writer.revert_workspace_entity(kind, "1", "2", "3", "10")
    writer.revert_workspace_entity(kind, "1", "2", "3", "10", fingerprint="fp2")

    assert endpoints[kind].calls == [
        ("revert", {"path": f"accounts/1/containers/2/workspaces/3/{kind}/10"}),
        (
            "revert",
            {
                "path": f"accounts/1/containers/2/workspaces/3/{kind}/10",
                "fingerprint": "fp2",
            },
        ),
    ]


def test_revert_workspace_entity_rejects_non_revertible_and_unknown(tmp_path: Any) -> None:
    service, _ = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    for bad_kind in ("gtag_config", "built_in_variables", "bogus"):
        with pytest.raises(ValueError, match="non-revertible"):
            writer.revert_workspace_entity(bad_kind, "1", "2", "3", "10")


def test_revert_builtin_variable_and_import_template(tmp_path: Any) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    res = writer.revert_builtin_variable("1", "2", "3", "pageUrl")
    assert res == {"enabled": True}
    assert endpoints["built_in_variables"].calls == [
        ("revert", {"path": "accounts/1/containers/2/workspaces/3", "type": "pageUrl"})
    ]

    with pytest.raises(ValueError, match="variable_type"):
        writer.revert_builtin_variable("1", "2", "3", "   ")

    writer.import_template_from_gallery("1", "2", "3", "owner", "repo")
    writer.import_template_from_gallery(
        "1", "2", "3", "owner", "repo", gallery_sha="abc123", acknowledge_permissions=True
    )
    assert endpoints["templates"].calls == [
        (
            "import_from_gallery",
            {
                "parent": "accounts/1/containers/2/workspaces/3",
                "galleryOwner": "owner",
                "galleryRepository": "repo",
                "acknowledgePermissions": False,
            },
        ),
        (
            "import_from_gallery",
            {
                "parent": "accounts/1/containers/2/workspaces/3",
                "galleryOwner": "owner",
                "galleryRepository": "repo",
                "gallerySha": "abc123",
                "acknowledgePermissions": True,
            },
        ),
    ]

    with pytest.raises(ValueError, match="gallery_owner"):
        writer.import_template_from_gallery("1", "2", "3", "", "repo")
    with pytest.raises(ValueError, match="gallery_sha"):
        writer.import_template_from_gallery("1", "2", "3", "owner", "repo", gallery_sha="  ")


def test_folder_entities_and_move(tmp_path: Any) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    entities = writer.list_folder_entities("1", "2", "3", "4")
    assert entities == {
        "tag": [{"tagId": "1"}, {"tagId": "2"}],
        "trigger": [{"triggerId": "5"}],
        "variable": [{"variableId": "9"}],
    }

    writer.move_entities_to_folder(
        "1",
        "2",
        "3",
        "0",
        tag_ids=["10", "11"],
        trigger_ids=["20"],
        variable_ids=None,
    )

    assert endpoints["folders"].calls == [
        ("entities", {"path": "accounts/1/containers/2/workspaces/3/folders/4"}),
        (
            "entities",
            {"path": "accounts/1/containers/2/workspaces/3/folders/4", "pageToken": "fp2"},
        ),
        (
            "move_entities_to_folder",
            {
                "path": "accounts/1/containers/2/workspaces/3/folders/0",
                "tagId": ["10", "11"],
                "triggerId": ["20"],
            },
        ),
    ]


def test_workspace_operations(tmp_path: Any) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    writer.get_workspace("1", "2", "3")
    writer.update_workspace("1", "2", "3", {"name": "WS"})
    writer.update_workspace("1", "2", "3", {"name": "WS"}, fingerprint="wfp")
    writer.delete_workspace("1", "2", "3")
    writer.resolve_conflict("1", "2", "3", {"tag": {"name": "T"}, "changeStatus": "updated"})
    writer.resolve_conflict(
        "1", "2", "3", {"tag": {"name": "T"}, "changeStatus": "updated"}, fingerprint="cfp"
    )
    writer.quick_preview("1", "2", "3")
    writer.bulk_update("1", "2", "3", {"changes": [{"changeStatus": "added"}]})

    ws_path = "accounts/1/containers/2/workspaces/3"
    assert endpoints["workspaces"].calls == [
        ("get", {"path": ws_path}),
        ("update", {"path": ws_path, "body": {"name": "WS"}}),
        ("update", {"path": ws_path, "body": {"name": "WS"}, "fingerprint": "wfp"}),
        ("delete", {"path": ws_path}),
        (
            "resolve_conflict",
            {"path": ws_path, "body": {"tag": {"name": "T"}, "changeStatus": "updated"}},
        ),
        (
            "resolve_conflict",
            {
                "path": ws_path,
                "body": {"tag": {"name": "T"}, "changeStatus": "updated"},
                "fingerprint": "cfp",
            },
        ),
        ("quick_preview", {"path": ws_path}),
        ("bulk_update", {"path": ws_path, "body": {"changes": [{"changeStatus": "added"}]}}),
    ]


def test_version_operations_and_no_publish(tmp_path: Any) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    writer.update_version("1", "2", "7", {"name": "v7"})
    writer.update_version("1", "2", "7", {"name": "v7"}, fingerprint="vfp")
    writer.delete_version("1", "2", "7")
    writer.undelete_version("1", "2", "7")
    writer.set_latest_version("1", "2", "7")

    ver_path = "accounts/1/containers/2/versions/7"
    assert endpoints["versions"].calls == [
        ("update", {"path": ver_path, "body": {"name": "v7"}}),
        ("update", {"path": ver_path, "body": {"name": "v7"}, "fingerprint": "vfp"}),
        ("delete", {"path": ver_path}),
        ("undelete", {"path": ver_path}),
        ("set_latest", {"path": ver_path}),
    ]


def test_environment_operations(tmp_path: Any) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    writer.get_environment("1", "2", "5")
    writer.create_environment("1", "2", {"name": "Staging", "type": "user"})
    writer.update_environment("1", "2", "5", {"enableDebug": True})
    writer.update_environment("1", "2", "5", {"enableDebug": True}, fingerprint="efp")
    writer.delete_environment("1", "2", "5")
    writer.reauthorize_environment("1", "2", "5")
    writer.reauthorize_environment("1", "2", "5", body={"name": "Staging"})

    env_path = "accounts/1/containers/2/environments/5"
    assert endpoints["environments"].calls == [
        ("get", {"path": env_path}),
        (
            "create",
            {"parent": "accounts/1/containers/2", "body": {"name": "Staging", "type": "user"}},
        ),
        ("update", {"path": env_path, "body": {"enableDebug": True}}),
        ("update", {"path": env_path, "body": {"enableDebug": True}, "fingerprint": "efp"}),
        ("delete", {"path": env_path}),
        ("reauthorize", {"path": env_path}),
        ("reauthorize", {"path": env_path, "body": {"name": "Staging"}}),
    ]


def test_destination_operations(tmp_path: Any) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    writer.get_destination("1", "2", "AW-123")
    writer.link_destination("1", "2", "AW-123", allow_user_permission_feature_update=True)

    assert endpoints["destinations"].calls == [
        ("get", {"path": "accounts/1/containers/2/destinations/AW-123"}),
        (
            "link",
            {
                "parent": "accounts/1/containers/2",
                "destinationId": "AW-123",
                "allowUserPermissionFeatureUpdate": True,
            },
        ),
    ]


def test_container_operations(tmp_path: Any) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    writer.get_container_snippet("1", "2")
    writer.create_container("1", {"name": "Site", "usageContext": ["web"]})
    writer.update_container("1", "2", {"name": "Site2"})
    writer.update_container("1", "2", {"name": "Site2"}, fingerprint="cfp")
    writer.delete_container("1", "2")
    writer.combine_containers(
        "1", "2", "3", setting_source="current", allow_user_permission_feature_update=True
    )
    writer.move_tag_id(
        "1",
        "2",
        "G-123",
        tag_name="Moved Tag",
        copy_settings=True,
        copy_terms_of_service=True,
        copy_users=True,
        allow_user_permission_feature_update=True,
    )

    c_path = "accounts/1/containers/2"
    assert endpoints["containers"].calls == [
        ("snippet", {"path": c_path}),
        ("create", {"parent": "accounts/1", "body": {"name": "Site", "usageContext": ["web"]}}),
        ("update", {"path": c_path, "body": {"name": "Site2"}}),
        ("update", {"path": c_path, "body": {"name": "Site2"}, "fingerprint": "cfp"}),
        ("delete", {"path": c_path}),
        (
            "combine",
            {
                "path": c_path,
                "containerId": "3",
                "allowUserPermissionFeatureUpdate": True,
                "settingSource": "current",
            },
        ),
        (
            "move_tag_id",
            {
                "path": c_path,
                "tagId": "G-123",
                "copySettings": True,
                "copyTermsOfService": True,
                "copyUsers": True,
                "allowUserPermissionFeatureUpdate": True,
                "tagName": "Moved Tag",
            },
        ),
    ]

    with pytest.raises(ValueError, match="setting_source"):
        writer.combine_containers("1", "2", "3", setting_source="invalid")


def test_account_operations(tmp_path: Any) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    writer.get_account("1")
    writer.update_account("1", {"name": "Acc"})
    writer.update_account("1", {"name": "Acc"}, fingerprint="afp")

    assert endpoints["accounts"].calls == [
        ("get", {"path": "accounts/1"}),
        ("update", {"path": "accounts/1", "body": {"name": "Acc"}}),
        ("update", {"path": "accounts/1", "body": {"name": "Acc"}, "fingerprint": "afp"}),
    ]


def test_user_permission_operations_and_pagination(tmp_path: Any) -> None:
    service, endpoints = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    listed = writer.list_user_permissions("1")
    assert listed == [{"emailAddress": "a@example.com"}, {"emailAddress": "b@example.com"}]

    writer.get_user_permission("1", "perm1")
    writer.create_user_permission("1", {"emailAddress": "c@example.com"})
    writer.update_user_permission("1", "perm1", {"accountAccess": {"permission": "admin"}})
    writer.delete_user_permission("1", "perm1")

    assert endpoints["user_permissions"].calls == [
        ("list", {"parent": "accounts/1"}),
        ("list", {"parent": "accounts/1", "pageToken": "p2"}),
        ("get", {"path": "accounts/1/user_permissions/perm1"}),
        ("create", {"parent": "accounts/1", "body": {"emailAddress": "c@example.com"}}),
        (
            "update",
            {
                "path": "accounts/1/user_permissions/perm1",
                "body": {"accountAccess": {"permission": "admin"}},
            },
        ),
        ("delete", {"path": "accounts/1/user_permissions/perm1"}),
    ]


def test_rejects_ids_with_slash_across_families(tmp_path: Any) -> None:
    service, _ = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    bad_calls = [
        lambda: writer.create_workspace_entity("templates", "1/2", "2", "3", {"name": "T"}),
        lambda: writer.update_workspace_entity("clients", "1", "2", "3", "4/5", {"name": "C"}),
        lambda: writer.revert_workspace_entity("tags", "1", "2", "3", "4/5"),
        lambda: writer.list_folder_entities("1", "2", "3", "folders/4"),
        lambda: writer.move_entities_to_folder("1", "2", "3", "4", tag_ids=["tags/1"]),
        lambda: writer.get_workspace("1", "2", "workspaces/3"),
        lambda: writer.delete_version("1", "2", "versions/7"),
        lambda: writer.get_environment("1", "2", "environments/5"),
        lambda: writer.link_destination("1", "2", "destinations/AW-1"),
        lambda: writer.combine_containers("1", "2", "containers/3"),
        lambda: writer.move_tag_id("1", "2", "tags/G-1"),
        lambda: writer.get_account("accounts/1"),
        lambda: writer.get_user_permission("1", "user_permissions/9"),
    ]
    for call in bad_calls:
        with pytest.raises(ValueError, match="not a path"):
            call()


def test_rejects_empty_body_across_families(tmp_path: Any) -> None:
    service, _ = _build_full_fake_service()
    writer = _make_writer(tmp_path, service)

    for empty in ({}, None, []):  # type: ignore[var-annotated]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.create_workspace_entity("templates", "1", "2", "3", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.update_workspace_entity("zones", "1", "2", "3", "9", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.update_workspace("1", "2", "3", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.resolve_conflict("1", "2", "3", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.bulk_update("1", "2", "3", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.update_version("1", "2", "7", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.create_environment("1", "2", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.update_environment("1", "2", "5", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.create_container("1", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.update_container("1", "2", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.update_account("1", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.create_user_permission("1", empty)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="non-empty JSON object"):
            writer.update_user_permission("1", "p1", empty)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="non-empty JSON object"):
        writer.reauthorize_environment("1", "2", "5", body={})


def test_operation_scopes_covers_every_public_method() -> None:
    public_methods = {
        name
        for name, _ in inspect.getmembers(GTMWriter, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert set(OPERATION_SCOPES.keys()) == public_methods

    allowed_scopes = {
        "readonly",
        "edit.containers",
        "edit.containerversions",
        "publish",
        "delete.containers",
        "manage.accounts",
        "manage.users",
    }
    assert set(OPERATION_SCOPES.values()).issubset(allowed_scopes)


def test_gtm_writer_has_no_publish_method() -> None:
    public_methods = {
        name
        for name, _ in inspect.getmembers(GTMWriter, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert not any("publish" in name for name in public_methods)
    source = inspect.getsource(GTMWriter)
    assert ".publish(" not in source
