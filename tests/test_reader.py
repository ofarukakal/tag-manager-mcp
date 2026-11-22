from __future__ import annotations

import re
from typing import Any

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response

from tag_manager_mcp.client import GTMClient, GTMError
from tag_manager_mcp.quota import QuotaGuard
from tag_manager_mcp.reader import GTMReader, summarize_version


class FakeRequest:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def execute(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakePagedEndpoint:
    def __init__(self, pages: dict[str | None, dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> FakeRequest:
        self.calls.append(kwargs)
        return FakeRequest(self._pages[kwargs.get("pageToken")])


class FakeSimpleEndpoint:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list(self, **kwargs: Any) -> FakeRequest:
        self.calls.append(("list", kwargs))
        return FakeRequest(self._responses["list"])

    def get(self, **kwargs: Any) -> FakeRequest:
        self.calls.append(("get", kwargs))
        return FakeRequest(self._responses["get"])

    def live(self, **kwargs: Any) -> FakeRequest:
        self.calls.append(("live", kwargs))
        key = kwargs.get("parent", "live")
        payload = self._responses.get(key, self._responses.get("live"))
        return FakeRequest(payload)

    def latest(self, **kwargs: Any) -> FakeRequest:
        self.calls.append(("latest", kwargs))
        return FakeRequest(self._responses["latest"])


def _make_client(tmp_path: Any, service: Any) -> GTMClient:
    guard = QuotaGuard(
        {
            "GTM_MIN_REQUEST_INTERVAL_SECONDS": "0",
            "GTM_DAILY_REQUEST_BUDGET": "100",
            "GTM_QUOTA_STATE_FILE": str(tmp_path / "quota.json"),
        }
    )
    client = GTMClient(service=service, quota_guard=guard)
    client.max_quota_retries = 0
    return client


def test_export_container_live_and_specific_version(tmp_path: Any) -> None:
    live_payload = {"containerVersionId": "5", "name": "Live v5", "tag": []}
    v3_payload = {"containerVersionId": "3", "name": "Old v3", "tag": [{"name": "T1"}]}
    versions_calls: list[tuple[str, dict[str, Any]]] = []

    class Versions:
        def live(self, **kwargs: Any) -> FakeRequest:
            versions_calls.append(("live", kwargs))
            return FakeRequest(live_payload)

        def get(self, **kwargs: Any) -> FakeRequest:
            versions_calls.append(("get", kwargs))
            return FakeRequest(v3_payload)

    class Containers:
        def versions(self) -> Versions:
            return Versions()

    class Accounts:
        def containers(self) -> Containers:
            return Containers()

    class Service:
        def accounts(self) -> Accounts:
            return Accounts()

    reader = GTMReader(_make_client(tmp_path, Service()))

    exported_live = reader.export_container("100", "200")
    assert set(exported_live.keys()) == {"exportFormatVersion", "exportTime", "containerVersion"}
    assert exported_live["exportFormatVersion"] == 2
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", exported_live["exportTime"])
    assert exported_live["containerVersion"] == live_payload

    exported_v3 = reader.export_container("100", "200", version_id="3")
    assert set(exported_v3.keys()) == {"exportFormatVersion", "exportTime", "containerVersion"}
    assert exported_v3["exportFormatVersion"] == 2
    assert exported_v3["containerVersion"] == v3_payload
    assert versions_calls == [
        ("live", {"parent": "accounts/100/containers/200"}),
        ("get", {"path": "accounts/100/containers/200/versions/3"}),
    ]


def test_latest_version_header_and_container_helpers(tmp_path: Any) -> None:
    headers_ep = FakeSimpleEndpoint({"latest": {"containerVersionId": "9", "name": "v9"}})
    envs_ep = FakePagedEndpoint({None: {"environment": [{"environmentId": "1", "name": "Live"}]}})
    dest_ep = FakeSimpleEndpoint({"list": {"destination": [{"destinationId": "AW-1"}]}})
    perm_ep = FakePagedEndpoint({None: {"userPermission": [{"emailAddress": "a@example.com"}]}})

    class Containers:
        def version_headers(self) -> FakeSimpleEndpoint:
            return headers_ep

        def environments(self) -> FakePagedEndpoint:
            return envs_ep

        def destinations(self) -> FakeSimpleEndpoint:
            return dest_ep

    class Accounts:
        def containers(self) -> Containers:
            return Containers()

        def user_permissions(self) -> FakePagedEndpoint:
            return perm_ep

    class Service:
        def accounts(self) -> Accounts:
            return Accounts()

    reader = GTMReader(_make_client(tmp_path, Service()))
    assert reader.latest_version_header("1", "2") == {"containerVersionId": "9", "name": "v9"}
    assert reader.list_environments("1", "2") == [{"environmentId": "1", "name": "Live"}]
    assert reader.list_destinations("1", "2") == [{"destinationId": "AW-1"}]
    assert reader.list_user_permissions("1") == [{"emailAddress": "a@example.com"}]


def test_list_workspace_entities_pagination_and_unknown_kind(tmp_path: Any) -> None:
    templates_ep = FakePagedEndpoint(
        {
            None: {"template": [{"templateId": "10", "name": "T1"}], "nextPageToken": "p2"},
            "p2": {"template": [{"templateId": "11", "name": "T2"}]},
        }
    )
    biv_ep = FakePagedEndpoint(
        {None: {"builtInVariable": [{"type": "pageUrl", "name": "Page URL"}]}}
    )
    gtag_ep = FakeSimpleEndpoint({"get": {"gtagConfigId": "1", "type": "AW-123"}})

    class Workspaces:
        def templates(self) -> FakePagedEndpoint:
            return templates_ep

        def built_in_variables(self) -> FakePagedEndpoint:
            return biv_ep

        def gtag_config(self) -> FakeSimpleEndpoint:
            return gtag_ep

    class Containers:
        def workspaces(self) -> Workspaces:
            return Workspaces()

    class Accounts:
        def containers(self) -> Containers:
            return Containers()

    class Service:
        def accounts(self) -> Accounts:
            return Accounts()

    reader = GTMReader(_make_client(tmp_path, Service()))

    templates = reader.list_workspace_entities("templates", "1", "2", "3")
    assert templates == [{"templateId": "10", "name": "T1"}, {"templateId": "11", "name": "T2"}]
    assert templates_ep.calls == [
        {"parent": "accounts/1/containers/2/workspaces/3"},
        {"parent": "accounts/1/containers/2/workspaces/3", "pageToken": "p2"},
    ]

    assert reader.list_workspace_entities("built_in_variables", "1", "2", "3") == [
        {"type": "pageUrl", "name": "Page URL"}
    ]
    assert reader.get_workspace_entity("gtag_config", "1", "2", "3", "1") == {
        "gtagConfigId": "1",
        "type": "AW-123",
    }

    with pytest.raises(ValueError, match="unsupported entity kind"):
        reader.list_workspace_entities("unknown_kind", "1", "2", "3")

    with pytest.raises(ValueError, match="does not support get"):
        reader.get_workspace_entity("built_in_variables", "1", "2", "3", "pageUrl")


def test_inventory_continues_past_failing_account(tmp_path: Any) -> None:
    forbidden = HttpError(Response({"status": "403"}), b'{"error":{"message":"Forbidden"}}')

    class Containers:
        def list(self, parent: str, pageToken: str | None = None) -> FakeRequest:
            if parent == "accounts/10":
                return FakeRequest(forbidden)
            return FakeRequest(
                {
                    "container": [
                        {
                            "containerId": "200",
                            "publicId": "GTM-OK",
                            "name": "Site",
                            "usageContext": ["web"],
                        }
                    ]
                }
            )

    class Accounts:
        def list(self, pageToken: str | None = None) -> FakeRequest:
            return FakeRequest(
                {
                    "account": [
                        {"accountId": "10", "name": "Blocked Account"},
                        {"accountId": "20", "name": "Accessible Account"},
                    ]
                }
            )

        def containers(self) -> Containers:
            return Containers()

    class Service:
        def accounts(self) -> Accounts:
            return Accounts()

    reader = GTMReader(_make_client(tmp_path, Service()))
    inv = reader.inventory()

    assert len(inv) == 2
    assert inv[0]["accountId"] == "10"
    assert inv[0]["containers"] == []
    assert inv[0]["error"] is not None
    assert "Google Tag Manager API error" in inv[0]["error"]

    assert inv[1] == {
        "accountId": "20",
        "name": "Accessible Account",
        "containers": [
            {
                "containerId": "200",
                "publicId": "GTM-OK",
                "name": "Site",
                "usageContext": ["web"],
            }
        ],
        "error": None,
    }


def test_fetch_all_live_filters_usage_context_and_records_missing_live(tmp_path: Any) -> None:
    not_found = HttpError(
        Response({"status": "404"}), b'{"error":{"message":"Published version not found"}}'
    )

    class Versions:
        def live(self, parent: str) -> FakeRequest:
            if parent == "accounts/1/containers/101":
                return FakeRequest({"containerVersionId": "7", "name": "Live 7"})
            return FakeRequest(not_found)

    class Containers:
        def list(self, parent: str, pageToken: str | None = None) -> FakeRequest:
            return FakeRequest(
                {
                    "container": [
                        {
                            "containerId": "101",
                            "publicId": "GTM-WEB1",
                            "name": "Web 1",
                            "usageContext": ["web"],
                        },
                        {
                            "containerId": "102",
                            "publicId": "GTM-SRV1",
                            "name": "Server 1",
                            "usageContext": ["server"],
                        },
                        {
                            "containerId": "103",
                            "publicId": "GTM-WEB2",
                            "name": "Web Unpublished",
                            "usageContext": ["web"],
                        },
                    ]
                }
            )

        def versions(self) -> Versions:
            return Versions()

    class Accounts:
        def list(self, pageToken: str | None = None) -> FakeRequest:
            return FakeRequest({"account": [{"accountId": "1", "name": "Main"}]})

        def containers(self) -> Containers:
            return Containers()

    class Service:
        def accounts(self) -> Accounts:
            return Accounts()

    reader = GTMReader(_make_client(tmp_path, Service()))
    live_web = reader.fetch_all_live(usage_context="web")

    assert len(live_web) == 2
    assert live_web[0] == {
        "accountId": "1",
        "containerId": "101",
        "publicId": "GTM-WEB1",
        "name": "Web 1",
        "version": {"containerVersionId": "7", "name": "Live 7"},
        "error": None,
    }
    assert live_web[1]["containerId"] == "103"
    assert live_web[1]["version"] is None
    assert live_web[1]["error"] is not None
    assert isinstance(GTMError(live_web[1]["error"]), GTMError)

    live_all = reader.fetch_all_live(usage_context=None)
    assert [item["containerId"] for item in live_all] == ["101", "102", "103"]


def test_summarize_version_counts_and_paused_tags() -> None:
    version = {
        "containerVersionId": "12",
        "name": "Release 12",
        "tag": [
            {"tagId": "1", "name": "GA4 Config", "paused": False},
            {"tagId": "2", "name": "Legacy Pixel", "paused": True},
            {"tagId": "3", "name": "Beta Tag", "paused": True},
        ],
        "trigger": [{"triggerId": "1"}, {"triggerId": "2"}],
        "variable": [{"variableId": "1"}],
        "folder": [],
        "builtInVariable": [{"type": "pageUrl"}],
        "customTemplate": [{"templateId": "5"}],
        "client": [],
        "zone": [{"zoneId": "9"}],
        "transformation": [{"transformationId": "4"}],
        "gtagConfig": [{"gtagConfigId": "8"}, {"gtagConfigId": "9"}],
    }

    summary = summarize_version(version)
    assert summary == {
        "containerVersionId": "12",
        "name": "Release 12",
        "counts": {
            "tag": 3,
            "trigger": 2,
            "variable": 1,
            "folder": 0,
            "builtInVariable": 1,
            "customTemplate": 1,
            "client": 0,
            "zone": 1,
            "transformation": 1,
            "gtagConfig": 2,
        },
        "pausedTags": ["Legacy Pixel", "Beta Tag"],
    }
