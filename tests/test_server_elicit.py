from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)
from mcp.server.mcpserver.utilities.context_injection import find_context_parameter
from mcp_types import (
    ClientCapabilities,
    ElicitationCapability,
    FormElicitationCapability,
    UrlElicitationCapability,
)

from tag_manager_mcp import publish_guard, server


class FakePublishReader:
    def __init__(self) -> None:
        self.live_payload: dict[str, Any] = {
            "containerVersionId": "10",
            "name": "Live 10",
            "container": {"publicId": "GTM-TEST123", "name": "Prod Web"},
            "tag": [
                {"tagId": "1", "name": "GA4 Config", "type": "googtag"},
                {"tagId": "2", "name": "Legacy Pixel", "type": "html"},
            ],
            "trigger": [{"triggerId": "1", "name": "All Pages", "type": "pageview"}],
        }
        self.latest_header_payload: dict[str, Any] = {"containerVersionId": "10"}
        self.versions_by_id: dict[str, dict[str, Any]] = {
            "10": dict(self.live_payload),
            "11": {
                "containerVersionId": "11",
                "name": "Release 11",
                "container": {"publicId": "GTM-TEST123", "name": "Prod Web"},
                "tag": [
                    {"tagId": "1", "name": "GA4 Config", "type": "googtag", "paused": True},
                    {"tagId": "3", "name": "Lead Event", "type": "gaawe"},
                ],
                "trigger": [{"triggerId": "1", "name": "All Pages", "type": "pageview"}],
            },
        }

    def live_version(self, account_id: str, container_id: str) -> dict[str, Any]:
        return self.live_payload

    def get_version(self, account_id: str, container_id: str, version_id: str) -> dict[str, Any]:
        return self.versions_by_id[version_id]

    def latest_version_header(self, account_id: str, container_id: str) -> dict[str, Any]:
        return self.latest_header_payload


class FakePublishClient:
    def __init__(self) -> None:
        self.publish_calls: list[tuple[str, str, str]] = []

    def publish_version(
        self, account_id: str, container_id: str, version_id: str
    ) -> dict[str, Any]:
        self.publish_calls.append((account_id, container_id, version_id))
        return {
            "compilerError": False,
            "containerVersion": {
                "path": f"accounts/{account_id}/containers/{container_id}/versions/{version_id}",
                "containerVersionId": version_id,
            },
        }


class FakePublishWriter:
    def __init__(self) -> None:
        self.environments: dict[str, dict[str, Any]] = {
            "7": {
                "environmentId": "7",
                "type": "user",
                "name": "Staging",
                "description": "QA env",
                "url": "https://staging.example",
                "enableDebug": True,
                "containerVersionId": "10",
                "fingerprint": "fp-env-7",
                "authorizationCode": "secret-env-7",
            }
        }
        self.update_environment_calls: list[tuple[Any, ...]] = []
        self.set_latest_calls: list[tuple[str, str, str]] = []

    def get_environment(
        self, account_id: str, container_id: str, environment_id: str
    ) -> dict[str, Any]:
        return dict(self.environments[environment_id])

    def update_environment(
        self,
        account_id: str,
        container_id: str,
        environment_id: str,
        body: dict[str, Any],
        fingerprint: str | None = None,
    ) -> dict[str, Any]:
        self.update_environment_calls.append(
            (account_id, container_id, environment_id, dict(body), fingerprint)
        )
        updated = {**self.environments[environment_id], **body}
        self.environments[environment_id] = updated
        return dict(updated)

    def set_latest_version(
        self, account_id: str, container_id: str, version_id: str
    ) -> dict[str, Any]:
        self.set_latest_calls.append((account_id, container_id, version_id))
        return {"containerVersionId": version_id}


class FakeElicitContext:
    """Fake MCP Context with scripted elicit responses and configurable capability."""

    def __init__(
        self,
        responses: Iterable[Any] = (),
        *,
        supports_elicitation: bool = True,
        capabilities: ClientCapabilities | None = None,
        on_elicit_hook: Any = None,
    ) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, type[Any]]] = []
        self._on_elicit_hook = on_elicit_hook
        if capabilities is not None:
            self._capabilities: ClientCapabilities | None = capabilities
        elif supports_elicitation:
            self._capabilities = ClientCapabilities(
                elicitation=ElicitationCapability(form=FormElicitationCapability())
            )
        else:
            self._capabilities = ClientCapabilities()

    @property
    def client_capabilities(self) -> ClientCapabilities | None:
        return self._capabilities

    async def elicit(self, message: str, schema: type[Any]) -> Any:
        call_index = len(self.calls)
        self.calls.append((message, schema))
        if self._on_elicit_hook is not None:
            self._on_elicit_hook(call_index, message, schema)
        if not self._responses:
            raise AssertionError("FakeElicitContext ran out of scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(message, schema)
        return item


def _accept_typed(value: str) -> Any:
    return lambda _msg, schema: AcceptedElicitation(data=schema(container_id=value))


def _accept_click(value: bool) -> Any:
    return lambda _msg, schema: AcceptedElicitation(data=schema(publish=value))


def _setup_publish_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, FakePublishReader, FakePublishClient, FakePublishWriter]:
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    approval_file = tmp_path / "approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(approval_file))
    reader = FakePublishReader()
    client = FakePublishClient()
    writer = FakePublishWriter()
    monkeypatch.setattr(server, "get_reader", lambda: reader)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "get_writer", lambda: writer)
    return approval_file, reader, client, writer


def test_elicit_happy_path_publishes_once_and_refuses_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_file, _, client, _ = _setup_publish_env(tmp_path, monkeypatch)
    prepared = server.prepare_publish("1", "100", "11")
    approval_id = prepared["approval_id"]

    ctx = FakeElicitContext([_accept_typed("  GTM-TEST123 "), _accept_click(True)])
    res = asyncio.run(
        server.publish_version("1", "100", "11", approval_id, confirm=True, ctx=ctx)  # type: ignore[arg-type]
    )
    assert res["ok"] is True
    assert client.publish_calls == [("1", "100", "11")]
    assert len(ctx.calls) == 2

    typed_msg, typed_schema = ctx.calls[0]
    assert "Publishes to: LIVE" in typed_msg
    assert "GTM-TEST123 (Prod Web)" in typed_msg
    assert "v11 - Release 11" in typed_msg
    assert "v10" in typed_msg
    assert "Lead Event" in typed_msg
    assert set(typed_schema.model_fields) == {"container_id"}

    click_msg, click_schema = ctx.calls[1]
    assert click_msg == "Publish GTM-TEST123 v11 to LIVE now?"
    assert set(click_schema.model_fields) == {"publish"}
    assert click_schema.model_fields["publish"].default is False

    stored = publish_guard.get_request(approval_id, path=approval_file)
    assert stored is not None
    assert stored["status"] == publish_guard.STATUS_USED
    assert stored["approved_via"] == publish_guard.APPROVED_VIA_ELICITATION

    # Second call with the same approval_id is refused before any prompt.
    ctx2 = FakeElicitContext([])
    replay = asyncio.run(
        server.publish_version("1", "100", "11", approval_id, confirm=True, ctx=ctx2)  # type: ignore[arg-type]
    )
    assert replay["ok"] is False
    assert "already been used" in replay["message"]
    assert ctx2.calls == []
    assert client.publish_calls == [("1", "100", "11")]


@pytest.mark.parametrize(
    ("label", "responses", "expected_calls"),
    [
        ("wrong_id", [_accept_typed("GTM-WRONG99")], 1),
        ("case_mismatch", [_accept_typed("gtm-test123")], 1),
        ("empty_id", [_accept_typed("   ")], 1),
        ("typed_declined", [DeclinedElicitation()], 1),
        ("typed_cancelled", [CancelledElicitation()], 1),
        ("typed_raises", [RuntimeError("client disconnected")], 1),
        ("click_false", [_accept_typed("GTM-TEST123"), _accept_click(False)], 2),
        (
            "click_truthy_non_bool",
            [
                _accept_typed("GTM-TEST123"),
                SimpleNamespace(action="accept", data=SimpleNamespace(publish=1)),
            ],
            2,
        ),
        ("click_declined", [_accept_typed("GTM-TEST123"), DeclinedElicitation()], 2),
        ("click_cancelled", [_accept_typed("GTM-TEST123"), CancelledElicitation()], 2),
        ("click_raises", [_accept_typed("GTM-TEST123"), RuntimeError("boom")], 2),
    ],
)
def test_elicit_failures_publish_nothing_and_cancel_request(
    label: str,
    responses: list[Any],
    expected_calls: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_file, _, client, _ = _setup_publish_env(tmp_path, monkeypatch)
    prepared = server.prepare_publish("1", "100", "11")
    approval_id = prepared["approval_id"]

    ctx = FakeElicitContext(responses)
    res = asyncio.run(
        server.publish_version("1", "100", "11", approval_id, confirm=True, ctx=ctx)  # type: ignore[arg-type]
    )
    assert res["ok"] is False, f"expected refusal for {label}"
    assert len(ctx.calls) == expected_calls
    assert client.publish_calls == []

    stored = publish_guard.get_request(approval_id, path=approval_file)
    assert stored is not None
    assert stored["status"] == publish_guard.STATUS_CANCELLED

    # Attempting to reuse the now-cancelled request fails before prompting.
    ctx_retry = FakeElicitContext([_accept_typed("GTM-TEST123"), _accept_click(True)])
    retry_res = asyncio.run(
        server.publish_version("1", "100", "11", approval_id, confirm=True, ctx=ctx_retry)  # type: ignore[arg-type]
    )
    assert retry_res["ok"] is False
    assert "cancelled" in retry_res["message"]
    assert ctx_retry.calls == []
    assert client.publish_calls == []


@pytest.mark.parametrize(
    "ctx_factory",
    [
        lambda: None,
        lambda: FakeElicitContext([], supports_elicitation=False),
        lambda: FakeElicitContext(
            [], capabilities=ClientCapabilities(elicitation=None)
        ),
        lambda: FakeElicitContext(
            [],
            capabilities=ClientCapabilities(
                elicitation=ElicitationCapability(url=UrlElicitationCapability())
            ),
        ),
    ],
)
def test_client_without_elicitation_capability_refuses_and_publishes_nothing(
    ctx_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_file, _, client, _ = _setup_publish_env(tmp_path, monkeypatch)
    prepared = server.prepare_publish("1", "100", "11")
    approval_id = prepared["approval_id"]

    ctx = ctx_factory()
    res = asyncio.run(
        server.publish_version("1", "100", "11", approval_id, confirm=True, ctx=ctx)
    )
    assert res["ok"] is False
    assert "this MCP client cannot show approval prompts; publishing is disabled" in res["message"]
    assert client.publish_calls == []
    if ctx is not None:
        assert ctx.calls == []

    # Request remains pending so a human can still use the terminal fallback.
    stored = publish_guard.get_request(approval_id, path=approval_file)
    assert stored is not None
    assert stored["status"] == publish_guard.STATUS_PENDING


def test_expired_request_refused_before_any_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_file, _, client, _ = _setup_publish_env(tmp_path, monkeypatch)
    past = datetime.now(timezone.utc) - timedelta(minutes=20)
    expired_req = publish_guard.create_request(
        account_id="1",
        container_id="100",
        public_id="GTM-TEST123",
        container_name="Prod Web",
        version_id="11",
        version_name="Release 11",
        live_version_id="10",
        diff_summary={"total_changes": 1},
        ttl_seconds=60,
        now=past,
        path=approval_file,
    )

    ctx = FakeElicitContext([_accept_typed("GTM-TEST123"), _accept_click(True)])
    res = asyncio.run(
        server.publish_version(
            "1", "100", "11", expired_req["approval_id"], confirm=True, ctx=ctx  # type: ignore[arg-type]
        )
    )
    assert res["ok"] is False
    assert "expired" in res["message"]
    assert ctx.calls == []
    assert client.publish_calls == []


def test_prompt_text_escapes_control_characters_and_truncates_long_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, reader, client, writer = _setup_publish_env(tmp_path, monkeypatch)
    long_container = "C" * 180
    reader.versions_by_id["11"] = {
        "containerVersionId": "11",
        "name": "v11\x1b[2K\x1b[1A\rPublishes to: nothing",
        "container": {"publicId": "GTM-TEST123", "name": long_container},
        "tag": [
            {"tagId": "1", "name": "GA4 Config", "type": "googtag"},
            {"tagId": "3", "name": "EvilTag\nFakeLine\t\u202e", "type": "gaawe"},
        ],
        "trigger": [{"triggerId": "1", "name": "All Pages", "type": "pageview"}],
    }
    writer.environments["7"]["name"] = "Stage\x1b[31m\nInjected"

    prepared = server.prepare_publish("1", "100", "11", environment_id="7")
    ctx = FakeElicitContext([_accept_typed("GTM-TEST123"), _accept_click(True)])
    res = asyncio.run(
        server.publish_to_environment(
            "1", "100", "7", "11", prepared["approval_id"], confirm=True, ctx=ctx  # type: ignore[arg-type]
        )
    )
    assert res["ok"] is True
    assert len(ctx.calls) == 2

    typed_msg = ctx.calls[0][0]
    click_msg = ctx.calls[1][0]
    for rendered in (typed_msg, click_msg):
        assert "\x1b" not in rendered
        assert "\r" not in rendered
        assert "\t" not in rendered
        assert "\u202e" not in rendered

    assert "v11\\x1b[2K\\x1b[1A\\x0dPublishes to: nothing" in typed_msg
    assert "EvilTag\\x0aFakeLine\\x09\\u202e" in typed_msg
    assert "Publishes to: Environment 7 (Stage\\x1b[31m\\x0aInjected)" in typed_msg
    assert click_msg == (
        "Publish GTM-TEST123 v11 to Environment 7 (Stage\\x1b[31m\\x0aInjected) now?"
    )
    assert long_container not in typed_msg
    assert ("C" * 117 + "...") in typed_msg
    assert not any(line.startswith("Publishes to: nothing") for line in typed_msg.splitlines())
    assert not any(line == "FakeLine" for line in typed_msg.splitlines())
    assert client.publish_calls == []


def test_live_environment_and_latest_targets_go_through_both_prompts_and_enforce_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_file, _, client, writer = _setup_publish_env(tmp_path, monkeypatch)

    live_prep = server.prepare_publish("1", "100", "11")
    env_prep = server.prepare_publish("1", "100", "11", environment_id="7")
    latest_prep = server.prepare_publish("1", "100", "11", latest=True)

    # Cross-target attempt is refused before any prompt.
    ctx_mismatch = FakeElicitContext([])
    mismatch = asyncio.run(
        server.publish_version(
            "1", "100", "11", env_prep["approval_id"], confirm=True, ctx=ctx_mismatch  # type: ignore[arg-type]
        )
    )
    assert mismatch["ok"] is False
    assert "target" in mismatch["message"]
    assert ctx_mismatch.calls == []

    # 1. LIVE target
    ctx_live = FakeElicitContext([_accept_typed("GTM-TEST123"), _accept_click(True)])
    live_res = asyncio.run(
        server.publish_version(
            "1", "100", "11", live_prep["approval_id"], confirm=True, ctx=ctx_live  # type: ignore[arg-type]
        )
    )
    assert live_res["ok"] is True
    assert len(ctx_live.calls) == 2
    assert "Publishes to: LIVE" in ctx_live.calls[0][0]
    assert ctx_live.calls[1][0] == "Publish GTM-TEST123 v11 to LIVE now?"
    assert client.publish_calls == [("1", "100", "11")]
    assert publish_guard.get_request(live_prep["approval_id"], path=approval_file)["status"] == (
        publish_guard.STATUS_USED
    )

    # 2. ENVIRONMENT target
    ctx_env = FakeElicitContext([_accept_typed("GTM-TEST123"), _accept_click(True)])
    env_res = asyncio.run(
        server.publish_to_environment(
            "1", "100", "7", "11", env_prep["approval_id"], confirm=True, ctx=ctx_env  # type: ignore[arg-type]
        )
    )
    assert env_res["ok"] is True
    assert len(ctx_env.calls) == 2
    assert "Publishes to: Environment 7 (Staging)" in ctx_env.calls[0][0]
    assert ctx_env.calls[1][0] == "Publish GTM-TEST123 v11 to Environment 7 (Staging) now?"
    assert len(writer.update_environment_calls) == 1
    assert writer.update_environment_calls[0][3]["containerVersionId"] == "11"
    assert publish_guard.get_request(env_prep["approval_id"], path=approval_file)["status"] == (
        publish_guard.STATUS_USED
    )

    # 3. LATEST target
    ctx_latest = FakeElicitContext([_accept_typed("GTM-TEST123"), _accept_click(True)])
    latest_res = asyncio.run(
        server.set_latest_version(
            "1", "100", "11", latest_prep["approval_id"], confirm=True, ctx=ctx_latest  # type: ignore[arg-type]
        )
    )
    assert latest_res["ok"] is True
    assert len(ctx_latest.calls) == 2
    assert "Publishes to: LATEST" in ctx_latest.calls[0][0]
    assert ctx_latest.calls[1][0] == "Publish GTM-TEST123 v11 to LATEST now?"
    assert writer.set_latest_calls == [("1", "100", "11")]
    assert publish_guard.get_request(latest_prep["approval_id"], path=approval_file)["status"] == (
        publish_guard.STATUS_USED
    )


def test_terminal_fallback_publishes_without_prompts_but_pending_and_elicited_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_file, reader, client, _ = _setup_publish_env(tmp_path, monkeypatch)

    # Pending request cannot publish on a client without elicitation.
    pending_prep = server.prepare_publish("1", "100", "11")
    no_elicit_ctx = FakeElicitContext([], supports_elicitation=False)
    pending_res = asyncio.run(
        server.publish_version(
            "1", "100", "11", pending_prep["approval_id"], confirm=True, ctx=no_elicit_ctx  # type: ignore[arg-type]
        )
    )
    assert pending_res["ok"] is False
    assert no_elicit_ctx.calls == []
    assert client.publish_calls == []

    # Approve via terminal path -> publishes without prompts even when client lacks elicitation.
    publish_guard.approve_request(
        pending_prep["approval_id"], typed_public_id="GTM-TEST123", path=approval_file
    )
    term_res = asyncio.run(
        server.publish_version(
            "1", "100", "11", pending_prep["approval_id"], confirm=True, ctx=no_elicit_ctx  # type: ignore[arg-type]
        )
    )
    assert term_res["ok"] is True
    assert no_elicit_ctx.calls == []
    assert client.publish_calls == [("1", "100", "11")]

    # A request approved via elicitation (e.g., if live version changed before consume)
    # cannot be reused without prompting again.
    stale_prep = server.prepare_publish("1", "100", "11")

    def mutate_live_during_prompt(call_idx: int, _msg: str, _schema: type[Any]) -> None:
        if call_idx == 1:
            reader.live_payload = {
                "containerVersionId": "99",
                "name": "Concurrent Hotfix",
                "container": {"publicId": "GTM-TEST123"},
            }

    ctx_stale = FakeElicitContext(
        [_accept_typed("GTM-TEST123"), _accept_click(True)],
        on_elicit_hook=mutate_live_during_prompt,
    )
    stale_res = asyncio.run(
        server.publish_version(
            "1", "100", "11", stale_prep["approval_id"], confirm=True, ctx=ctx_stale  # type: ignore[arg-type]
        )
    )
    assert stale_res["ok"] is False
    assert "Live container version changed" in stale_res["message"]
    assert client.publish_calls == [("1", "100", "11")]
    stale_stored = publish_guard.get_request(stale_prep["approval_id"], path=approval_file)
    assert stale_stored is not None
    assert stale_stored["status"] == publish_guard.STATUS_CANCELLED
    assert "Live container version changed" in stale_stored["reason"]
    assert server.list_publish_requests() == []

    # Even if live version reverts, the cancelled request cannot be reused.
    reader.live_payload = dict(reader.versions_by_id["10"])
    retry_ctx = FakeElicitContext([], supports_elicitation=True)
    retry_res = asyncio.run(
        server.publish_version(
            "1", "100", "11", stale_prep["approval_id"], confirm=True, ctx=retry_ctx  # type: ignore[arg-type]
        )
    )
    assert retry_res["ok"] is False
    assert "cancelled" in retry_res["message"]
    assert retry_ctx.calls == []
    assert client.publish_calls == [("1", "100", "11")]


def test_served_version_changed_during_elicitation_cancels_request_across_all_three_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    approval_file, reader, client, writer = _setup_publish_env(tmp_path, monkeypatch)

    # 1. publish_version: both prompts accepted, live version changed -> cancelled
    live_prep = server.prepare_publish("1", "100", "11")

    def mutate_live(call_idx: int, _msg: str, _schema: type[Any]) -> None:
        if call_idx == 1:
            reader.live_payload = {
                "containerVersionId": "99",
                "name": "Concurrent Live",
                "container": {"publicId": "GTM-TEST123"},
            }

    live_ctx = FakeElicitContext(
        [_accept_typed("GTM-TEST123"), _accept_click(True)],
        on_elicit_hook=mutate_live,
    )
    live_res = asyncio.run(
        server.publish_version(
            "1", "100", "11", live_prep["approval_id"], confirm=True, ctx=live_ctx  # type: ignore[arg-type]
        )
    )
    assert live_res["ok"] is False
    assert client.publish_calls == []
    live_stored = publish_guard.get_request(live_prep["approval_id"], path=approval_file)
    assert live_stored is not None
    assert live_stored["status"] == publish_guard.STATUS_CANCELLED
    assert "Live container version changed" in live_stored["reason"]
    assert server.list_publish_requests() == []

    # 2. publish_to_environment: both prompts accepted, environment version changed -> cancelled
    env_prep = server.prepare_publish("1", "100", "11", environment_id="7")

    def mutate_env(call_idx: int, _msg: str, _schema: type[Any]) -> None:
        if call_idx == 1:
            writer.environments["7"]["containerVersionId"] = "99"

    env_ctx = FakeElicitContext(
        [_accept_typed("GTM-TEST123"), _accept_click(True)],
        on_elicit_hook=mutate_env,
    )
    env_res = asyncio.run(
        server.publish_to_environment(
            "1", "100", "7", "11", env_prep["approval_id"], confirm=True, ctx=env_ctx  # type: ignore[arg-type]
        )
    )
    assert env_res["ok"] is False
    assert writer.update_environment_calls == []
    env_stored = publish_guard.get_request(env_prep["approval_id"], path=approval_file)
    assert env_stored is not None
    assert env_stored["status"] == publish_guard.STATUS_CANCELLED
    assert "Live container version changed" in env_stored["reason"]
    assert server.list_publish_requests() == []

    # 3. set_latest_version: both prompts accepted, latest version changed -> cancelled
    latest_prep = server.prepare_publish("1", "100", "11", latest=True)

    def mutate_latest(call_idx: int, _msg: str, _schema: type[Any]) -> None:
        if call_idx == 1:
            reader.latest_header_payload = {"containerVersionId": "99"}

    latest_ctx = FakeElicitContext(
        [_accept_typed("GTM-TEST123"), _accept_click(True)],
        on_elicit_hook=mutate_latest,
    )
    latest_res = asyncio.run(
        server.set_latest_version(
            "1", "100", "11", latest_prep["approval_id"], confirm=True, ctx=latest_ctx  # type: ignore[arg-type]
        )
    )
    assert latest_res["ok"] is False
    assert writer.set_latest_calls == []
    latest_stored = publish_guard.get_request(latest_prep["approval_id"], path=approval_file)
    assert latest_stored is not None
    assert latest_stored["status"] == publish_guard.STATUS_CANCELLED
    assert "Live container version changed" in latest_stored["reason"]
    assert server.list_publish_requests() == []

    # 4. Terminal-approved request where consume_approval fails is also cancelled.
    term_prep = server.prepare_publish("1", "100", "11")
    publish_guard.approve_request(
        term_prep["approval_id"], typed_public_id="GTM-TEST123", path=approval_file
    )
    reader.live_payload = {
        "containerVersionId": "100",
        "name": "Concurrent Hotfix",
        "container": {"publicId": "GTM-TEST123"},
    }
    term_res = asyncio.run(
        server.publish_version(
            "1", "100", "11", term_prep["approval_id"], confirm=True
        )
    )
    assert term_res["ok"] is False
    assert client.publish_calls == []
    term_stored = publish_guard.get_request(term_prep["approval_id"], path=approval_file)
    assert term_stored is not None
    assert term_stored["status"] == publish_guard.STATUS_CANCELLED
    assert "Live container version changed" in term_stored["reason"]
    assert server.list_publish_requests() == []


def test_mcp_tool_schemas_hide_context_and_forbid_approval_args_and_inject_ctx_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, client, _ = _setup_publish_env(tmp_path, monkeypatch)

    for fn in (server.publish_version, server.publish_to_environment, server.set_latest_version):
        assert find_context_parameter(fn) == "ctx"

    srv = server.build_server("publish")
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    forbidden_props = {"ctx", "typed_public_id", "container_id_confirmation", "clicked", "publish"}
    for name, expected_props in (
        ("publish_version", {"account_id", "container_id", "version_id", "approval_id", "confirm"}),
        (
            "publish_to_environment",
            {
                "account_id",
                "container_id",
                "environment_id",
                "version_id",
                "approval_id",
                "confirm",
            },
        ),
        (
            "set_latest_version",
            {"account_id", "container_id", "version_id", "approval_id", "confirm"},
        ),
    ):
        props = set((tools[name].input_schema.get("properties") or {}).keys())
        assert props == expected_props
        assert props.isdisjoint(forbidden_props)

    # End-to-end call through MCPServer.call_tool injects ctx and runs both prompts.
    prepared = server.prepare_publish("1", "100", "11")
    ctx = FakeElicitContext([_accept_typed("GTM-TEST123"), _accept_click(True)])
    call_result = asyncio.run(
        srv.call_tool(
            "publish_version",
            {
                "account_id": "1",
                "container_id": "100",
                "version_id": "11",
                "approval_id": prepared["approval_id"],
                "confirm": True,
            },
            context=ctx,  # type: ignore[arg-type]
        )
    )
    assert call_result.is_error is False
    assert len(ctx.calls) == 2
    assert client.publish_calls == [("1", "100", "11")]
