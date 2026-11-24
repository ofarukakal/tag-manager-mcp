from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

import pytest

from tag_manager_mcp import server
from tag_manager_mcp.debug_log import DebugCallLogger, sanitize, sanitize_uri


def test_sanitize_redacts_nested_secrets() -> None:
    value = sanitize(
        {
            "access_token": "secret",
            "nested": {"private_key": "key", "name": "safe"},
        }
    )
    assert value == {
        "access_token": "[REDACTED]",
        "nested": {"private_key": "[REDACTED]", "name": "safe"},
    }
    assert "secret" not in sanitize_uri("https://example.test/path?access_token=secret&x=1")


def test_sqlite_logger_records_and_reads_events(tmp_path) -> None:
    debug = DebugCallLogger(
        {
            "GTM_DEBUG_LOG_ENABLED": "true",
            "GTM_DEBUG_DB_PATH": str(tmp_path / "debug.db"),
            "GTM_DEBUG_RETENTION_DAYS": "30",
            "GTM_DEBUG_MAX_PAYLOAD_BYTES": "10000",
        }
    )
    debug.log(
        kind="mcp_tool",
        name="test_tool",
        status="success",
        request={"client_secret": "hidden", "value": 1},
        response={"ok": True},
        call_id="abc",
    )

    status = debug.status()
    assert status["total_events"] == 1
    assert status["events_by_kind"] == {"mcp_tool": 1}
    row = debug.recent(limit=1)[0]
    assert row["call_id"] == "abc"
    assert row["request"] == {"client_secret": "[REDACTED]", "value": 1}
    assert row["response"] == {"ok": True}


def test_sanitize_and_debug_log_redact_environment_authorization_codes(tmp_path) -> None:
    payload = {
        "environment": [
            {
                "environmentId": "7",
                "name": "Staging",
                "authorizationCode": "gtm-auth-secret-1",
            },
            {
                "environmentId": "8",
                "name": "QA",
                "AUTHORIZATION_CODE": "gtm-auth-secret-2",
                "authorization_code": "gtm-auth-secret-3",
            },
        ]
    }
    cleaned = sanitize(payload)
    assert cleaned["environment"][0]["authorizationCode"] == "[REDACTED]"
    assert cleaned["environment"][1]["AUTHORIZATION_CODE"] == "[REDACTED]"
    assert cleaned["environment"][1]["authorization_code"] == "[REDACTED]"
    assert "gtm-auth-secret" not in sanitize_uri(
        "https://example.test/env?authorizationCode=gtm-auth-secret&authorization_code=sec2"
    )

    debug = DebugCallLogger(
        {
            "GTM_DEBUG_LOG_ENABLED": "true",
            "GTM_DEBUG_DB_PATH": str(tmp_path / "debug.db"),
            "GTM_DEBUG_RETENTION_DAYS": "30",
            "GTM_DEBUG_MAX_PAYLOAD_BYTES": "10000",
        }
    )
    debug.log(
        kind="gtm_api",
        name="reauthorize_environment",
        status="success",
        response={"environmentId": "7", "authorizationCode": "super-secret-auth"},
        call_id="env1",
    )
    row = debug.recent(limit=1)[0]
    assert row["response"]["authorizationCode"] == "[REDACTED]"
    assert "super-secret-auth" not in str(row)


def test_sanitize_redacts_sensitive_keys_inside_json_like_strings() -> None:
    samples = [
        ('{"name": "Staging", "authorizationCode": "ROTATED-SECRET"}', "ROTATED-SECRET"),
        ("{'name': 'Staging', 'refresh_token': 'rt-single-secret'}", "rt-single-secret"),
        (
            r'{"text": "{\"AUTHORIZATIONCODE\": \"ESCAPED-SECRET\", \"ok\": true}"}',
            "ESCAPED-SECRET",
        ),
        (
            r"{'text': '\'Refresh_Token\': \'escaped-rt-secret\''}",
            "escaped-rt-secret",
        ),
    ]
    for raw, secret in samples:
        cleaned = sanitize(raw)
        assert secret not in cleaned
        assert "[REDACTED]" in cleaned

    # Valid JSON string stays valid JSON after sanitization.
    parsed = json.loads(sanitize('{"name": "Staging", "authorizationCode": "ROTATED-SECRET"}'))
    assert parsed == {"name": "Staging", "authorizationCode": "[REDACTED]"}


def test_logging_fastmcp_call_tool_redacts_secrets_in_json_string_payloads(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "debug.db"
    debug = DebugCallLogger(
        {
            "GTM_DEBUG_LOG_ENABLED": "true",
            "GTM_DEBUG_DB_PATH": str(db_path),
            "GTM_DEBUG_RETENTION_DAYS": "30",
            "GTM_DEBUG_MAX_PAYLOAD_BYTES": "10000",
        }
    )
    monkeypatch.setattr(server, "get_debug_logger", lambda: debug)
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")

    class FakeWriter:
        def reauthorize_environment(
            self, account_id: str, container_id: str, environment_id: str
        ) -> dict[str, Any]:
            return {
                "environmentId": environment_id,
                "name": "Staging",
                "authorizationCode": "ROTATED-SECRET",
            }

    monkeypatch.setattr(server, "get_writer", lambda: FakeWriter())

    srv = server.build_server("publish")

    @srv.tool()
    def _echo_token() -> dict[str, Any]:
        return {"ok": True, "refresh_token": "REFRESH-SECRET-XYZ"}

    reauth_result = asyncio.run(
        srv.call_tool(
            "reauthorize_environment",
            {"account_id": "1", "container_id": "2", "environment_id": "7"},
        )
    )
    # The authorized caller still receives the rotated authorization code.
    assert "ROTATED-SECRET" in str(reauth_result)

    asyncio.run(srv.call_tool("_echo_token", {}))

    # Also test a custom object (without model_dump) serialized via default=str.
    class _OpaquePayload:
        def __str__(self) -> str:
            return (
                '{"authorizationCode": "OPAQUE-AUTH-SECRET", '
                '"refresh_token": "OPAQUE-RT-SECRET"}'
            )

    debug.log(kind="mcp_tool", name="opaque", status="success", response=_OpaquePayload())

    # Raw SQLite rows must never contain any secret.
    with sqlite3.connect(db_path) as conn:
        raw_rows = conn.execute(
            "SELECT request_json, response_json, error_message FROM call_log"
        ).fetchall()
    assert len(raw_rows) == 3
    raw_dump = repr(raw_rows)
    for secret in (
        "ROTATED-SECRET",
        "REFRESH-SECRET-XYZ",
        "OPAQUE-AUTH-SECRET",
        "OPAQUE-RT-SECRET",
    ):
        assert secret not in raw_dump

    # Read-level debug_log_recent must never expose any secret either.
    monkeypatch.setenv("GTM_ACCESS_MODE", "read")
    recent = server.debug_log_recent(limit=10)
    recent_dump = json.dumps(recent)
    for secret in (
        "ROTATED-SECRET",
        "REFRESH-SECRET-XYZ",
        "OPAQUE-AUTH-SECRET",
        "OPAQUE-RT-SECRET",
    ):
        assert secret not in recent_dump
    assert "[REDACTED]" in recent_dump
