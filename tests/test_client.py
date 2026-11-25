from __future__ import annotations

from pathlib import Path
from typing import Any

import googleapiclient
import pytest
from google.auth.credentials import AnonymousCredentials
from googleapiclient.discovery import build_from_document
from googleapiclient.errors import HttpError
from httplib2 import Response

from tag_manager_mcp.client import (
    GTMClient,
    _validate_body,
    container_path,
    parse_container_identifier,
    workspace_path,
)
from tag_manager_mcp.quota import QuotaGuard


def test_resource_paths() -> None:
    assert container_path("123", "456") == "accounts/123/containers/456"
    assert workspace_path("123", "456", "7") == "accounts/123/containers/456/workspaces/7"


@pytest.mark.parametrize("value", ["", "accounts/123"])
def test_resource_ids_reject_empty_or_paths(value: str) -> None:
    with pytest.raises(ValueError):
        container_path(value, "456")


def test_create_tag_body_requires_name_and_type() -> None:
    with pytest.raises(ValueError, match="name"):
        _validate_body("tags", {"type": "gaawe"})
    with pytest.raises(ValueError, match="type"):
        _validate_body("tags", {"name": "GA4 event"})
    _validate_body("tags", {"name": "GA4 event", "type": "gaawe"})


def test_parse_container_identifier() -> None:
    assert parse_container_identifier(" gtm-w525xw4 ") == ("tagId", "GTM-W525XW4")
    assert parse_container_identifier("AW-123456789") == (
        "destinationId",
        "AW-123456789",
    )
    with pytest.raises(ValueError, match="public tag"):
        parse_container_identifier("W525XW4")


def test_list_all_follows_page_tokens(tmp_path) -> None:
    class Request:
        def __init__(self, response: dict) -> None:
            self.response = response

        def execute(self) -> dict:
            return self.response

    seen: list[str | None] = []

    def request(token: str | None) -> Request:
        seen.append(token)
        if token is None:
            return Request({"tag": [{"tagId": "1"}], "nextPageToken": "next"})
        return Request({"tag": [{"tagId": "2"}]})

    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "10",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        }
    )
    client = GTMClient(service=object(), quota_guard=guard)
    assert client._list_all(request, "tag") == [{"tagId": "1"}, {"tagId": "2"}]
    assert seen == [None, "next"]


def test_execute_retries_quota_response(tmp_path) -> None:
    class Request:
        attempts = 0

        def execute(self) -> dict:
            self.attempts += 1
            if self.attempts == 1:
                response = Response({"status": "429", "retry-after": "0"})
                raise HttpError(response, b'{"error":{"message":"Quota exceeded"}}')
            return {"ok": True}

    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "10",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        }
    )
    client = GTMClient(service=object(), quota_guard=guard)
    client.max_quota_retries = 1
    request = Request()

    assert client._execute(lambda: request) == {"ok": True}
    assert request.attempts == 2
    assert guard.status()["request_count"] == 2


class _RecordingEndpoint:
    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or {}

    def __getattr__(self, method: str) -> Any:
        def _call(**kwargs: Any) -> Any:
            self.calls.append((method, kwargs))
            payload = self._responses.get(method, {})

            class _Req:
                def execute(self) -> dict[str, Any]:
                    return payload

            return _Req()

        return _call


def _make_client_with_fake_service(
    tmp_path: Path,
) -> tuple[GTMClient, dict[str, _RecordingEndpoint]]:
    endpoints = {
        "accounts": _RecordingEndpoint({"list": {"account": []}}),
        "containers": _RecordingEndpoint({"list": {"container": []}}),
        "workspaces": _RecordingEndpoint({"list": {"workspace": []}}),
        "version_headers": _RecordingEndpoint({"list": {"containerVersionHeader": []}}),
        "versions": _RecordingEndpoint(),
        "built_in_variables": _RecordingEndpoint(
            {"list": {"builtInVariable": []}, "create": {"builtInVariable": []}}
        ),
        "tags": _RecordingEndpoint({"list": {"tag": []}}),
        "triggers": _RecordingEndpoint({"list": {"trigger": []}}),
        "variables": _RecordingEndpoint({"list": {"variable": []}}),
        "folders": _RecordingEndpoint({"list": {"folder": []}}),
    }

    class _WorkspacesResource(_RecordingEndpoint):
        def built_in_variables(self) -> _RecordingEndpoint:
            return endpoints["built_in_variables"]

        def tags(self) -> _RecordingEndpoint:
            return endpoints["tags"]

        def triggers(self) -> _RecordingEndpoint:
            return endpoints["triggers"]

        def variables(self) -> _RecordingEndpoint:
            return endpoints["variables"]

        def folders(self) -> _RecordingEndpoint:
            return endpoints["folders"]

    class _ContainersResource(_RecordingEndpoint):
        def workspaces(self) -> _WorkspacesResource:
            return workspaces_res

        def version_headers(self) -> _RecordingEndpoint:
            return endpoints["version_headers"]

        def versions(self) -> _RecordingEndpoint:
            return endpoints["versions"]

    class _AccountsResource(_RecordingEndpoint):
        def containers(self) -> _ContainersResource:
            return containers_res

    workspaces_res = _WorkspacesResource({"list": {"workspace": []}})
    containers_res = _ContainersResource({"list": {"container": []}})
    accounts_res = _AccountsResource({"list": {"account": []}})
    endpoints["workspaces"] = workspaces_res
    endpoints["containers"] = containers_res
    endpoints["accounts"] = accounts_res

    class _Service:
        def accounts(self) -> _AccountsResource:
            return accounts_res

    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "100",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        }
    )
    return GTMClient(service=_Service(), quota_guard=guard), endpoints


def test_builtin_variables_paths_and_parents(tmp_path: Path) -> None:
    client, endpoints = _make_client_with_fake_service(tmp_path)

    client.list_builtin_variables("1", "2", "3")
    client.enable_builtin_variables("1", "2", "3", ["pageUrl"])
    client.disable_builtin_variables("1", "2", "3", ["pageUrl"])

    assert endpoints["built_in_variables"].calls == [
        ("list", {"parent": "accounts/1/containers/2/workspaces/3"}),
        ("create", {"parent": "accounts/1/containers/2/workspaces/3", "type": ["pageUrl"]}),
        (
            "delete",
            {
                "path": "accounts/1/containers/2/workspaces/3/built_in_variables",
                "type": ["pageUrl"],
            },
        ),
    ]


def test_client_methods_exact_paths_and_parents_match_discovery_doc(tmp_path: Path) -> None:
    client, endpoints = _make_client_with_fake_service(tmp_path)

    client.list_containers("1")
    client.get_container("1", "2")
    client.list_workspaces("1", "2")
    client.create_workspace("1", "2", "WS")
    client.workspace_status("1", "2", "3")
    client.sync_workspace("1", "2", "3")
    client.list_entities("tags", "1", "2", "3")
    client.get_entity("tags", "1", "2", "3", "9")
    client.create_entity("tags", "1", "2", "3", {"name": "T", "type": "html"})
    client.update_entity("tags", "1", "2", "3", "9", {"name": "T2"}, fingerprint="fp")
    client.delete_entity("tags", "1", "2", "3", "9")
    client.list_versions("1", "2")
    client.create_version("1", "2", "3", name="v1")
    client.publish_version("1", "2", "4")

    assert endpoints["containers"].calls == [
        ("list", {"parent": "accounts/1"}),
        ("get", {"path": "accounts/1/containers/2"}),
    ]
    assert endpoints["workspaces"].calls == [
        ("list", {"parent": "accounts/1/containers/2"}),
        (
            "create",
            {"parent": "accounts/1/containers/2", "body": {"name": "WS", "description": ""}},
        ),
        ("getStatus", {"path": "accounts/1/containers/2/workspaces/3"}),
        ("sync", {"path": "accounts/1/containers/2/workspaces/3"}),
        (
            "create_version",
            {"path": "accounts/1/containers/2/workspaces/3", "body": {"name": "v1"}},
        ),
    ]
    assert endpoints["tags"].calls == [
        ("list", {"parent": "accounts/1/containers/2/workspaces/3"}),
        ("get", {"path": "accounts/1/containers/2/workspaces/3/tags/9"}),
        (
            "create",
            {
                "parent": "accounts/1/containers/2/workspaces/3",
                "body": {"name": "T", "type": "html"},
            },
        ),
        (
            "update",
            {
                "path": "accounts/1/containers/2/workspaces/3/tags/9",
                "body": {"name": "T2"},
                "fingerprint": "fp",
            },
        ),
        ("delete", {"path": "accounts/1/containers/2/workspaces/3/tags/9"}),
    ]
    assert endpoints["version_headers"].calls == [
        ("list", {"parent": "accounts/1/containers/2"}),
    ]
    assert endpoints["versions"].calls == [
        ("publish", {"path": "accounts/1/containers/2/versions/4"}),
    ]

    # Also validate built_in_variables requests directly against the bundled v2 discovery doc.
    doc_path = (
        Path(googleapiclient.__file__).resolve().parent
        / "discovery_cache"
        / "documents"
        / "tagmanager.v2.json"
    )
    discovery_service = build_from_document(
        doc_path.read_text(encoding="utf-8"),
        credentials=AnonymousCredentials(),
    )
    built_uris: list[tuple[str, str]] = []

    class _DiscoveryValidatingClient(GTMClient):
        def _execute(self, request_factory: Any) -> dict[str, Any]:
            req = request_factory()
            built_uris.append((req.method, req.uri))
            return {}

    validating = _DiscoveryValidatingClient(service=discovery_service)
    validating.list_builtin_variables("1", "2", "3")
    validating.enable_builtin_variables("1", "2", "3", ["pageUrl"])
    validating.disable_builtin_variables("1", "2", "3", ["pageUrl"])

    assert built_uris == [
        (
            "GET",
            "https://tagmanager.googleapis.com/tagmanager/v2/accounts/1/containers/2/workspaces/3/built_in_variables?alt=json",
        ),
        (
            "POST",
            "https://tagmanager.googleapis.com/tagmanager/v2/accounts/1/containers/2/workspaces/3/built_in_variables?type=pageUrl&alt=json",
        ),
        (
            "DELETE",
            "https://tagmanager.googleapis.com/tagmanager/v2/accounts/1/containers/2/workspaces/3/built_in_variables?type=pageUrl",
        ),
    ]
