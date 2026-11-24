from __future__ import annotations

import ast
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tag_manager_mcp import approve_cli
from tag_manager_mcp.publish_guard import (
    STATUS_APPROVED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    create_request,
    get_request,
)

BASE_TIME = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


def _create_pending(
    path: Path,
    *,
    public_id: str = "GTM-ABC123",
    container_id: str = "200",
    version_id: str = "7",
    version_name: str = "Checkout fix",
    live_version_id: str | None = "6",
    ttl_seconds: int = 600,
    now: Any = BASE_TIME,
) -> dict[str, Any]:
    return create_request(
        account_id="100",
        container_id=container_id,
        public_id=public_id,
        container_name="Main Web",
        version_id=version_id,
        version_name=version_name,
        live_version_id=live_version_id,
        diff_summary={
            "tag": {
                "added": [{"id": "1", "name": "GA4 Purchase"}],
                "removed": [],
                "changed": [{"id": "2", "name": "GA4 Config"}],
            }
        },
        ttl_seconds=ttl_seconds,
        now=now,
        path=path,
    )


def test_non_tty_refuses_without_reading_stdin_and_leaves_pending(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_pending(state_file)

    class ExplodingStdin(io.StringIO):
        def readline(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("stdin must not be read when not a TTY")

    out = io.StringIO()
    rc = approve_cli.main(
        [req["approval_id"]],
        stdin=ExplodingStdin("GTM-ABC123\n"),
        stdout=out,
        isatty=lambda: False,
        path=state_file,
        now=BASE_TIME + timedelta(seconds=5),
    )

    assert rc == 2
    assert "TTY" in out.getvalue()
    assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_PENDING


def test_correct_container_id_approves_request(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_pending(state_file)

    out = io.StringIO()
    rc = approve_cli.main(
        [req["approval_id"]],
        stdin=io.StringIO("GTM-ABC123\n"),
        stdout=out,
        isatty=lambda: True,
        path=state_file,
        now=BASE_TIME + timedelta(seconds=15),
    )

    output = out.getvalue()
    assert rc == 0
    assert "GTM-ABC123 (Main Web)" in output
    assert "v7 - Checkout fix" in output
    assert "v6" in output
    assert "GA4 Purchase" in output
    assert (
        "Type the container ID (GTM-ABC123) to approve publishing, or anything else to cancel: "
        in output
    )
    assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_APPROVED


def test_wrong_container_id_leaves_pending_and_returns_1(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_pending(state_file)

    out = io.StringIO()
    rc = approve_cli.main(
        [req["approval_id"]],
        stdin=io.StringIO("gtm-abc123\n"),
        stdout=out,
        isatty=lambda: True,
        path=state_file,
        now=BASE_TIME + timedelta(seconds=15),
    )

    assert rc == 1
    assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_PENDING


def test_empty_input_cancels_and_leaves_pending(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_pending(state_file)

    out = io.StringIO()
    rc = approve_cli.main(
        [req["approval_id"]],
        stdin=io.StringIO("\n"),
        stdout=out,
        isatty=lambda: True,
        path=state_file,
        now=BASE_TIME + timedelta(seconds=15),
    )

    assert rc == 1
    assert "Cancelled" in out.getvalue()
    assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_PENDING


def test_expired_request_cannot_be_approved(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_pending(state_file, ttl_seconds=60)

    out = io.StringIO()
    rc = approve_cli.main(
        [req["approval_id"]],
        stdin=io.StringIO("GTM-ABC123\n"),
        stdout=out,
        isatty=lambda: True,
        path=state_file,
        now=BASE_TIME + timedelta(seconds=61),
    )

    assert rc == 1
    assert "expired" in out.getvalue().lower()
    assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_EXPIRED


def test_unknown_request_returns_1(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    out = io.StringIO()
    rc = approve_cli.main(
        ["deadbeef12345678"],
        stdin=io.StringIO("GTM-ABC123\n"),
        stdout=out,
        isatty=lambda: True,
        path=state_file,
        now=BASE_TIME,
    )
    assert rc == 1
    assert "Unknown" in out.getvalue()


def test_no_argument_lists_pending_requests(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req1 = _create_pending(
        state_file,
        public_id="GTM-AAA111",
        container_id="201",
        version_id="3",
        version_name="Launch",
        ttl_seconds=600,
    )
    req2 = _create_pending(
        state_file,
        public_id="GTM-BBB222",
        container_id="202",
        version_id="9",
        version_name="Hotfix",
        ttl_seconds=30,
    )

    out = io.StringIO()
    rc = approve_cli.main(
        [],
        stdout=out,
        path=state_file,
        now=BASE_TIME + timedelta(seconds=45),
    )

    output = out.getvalue()
    assert rc == 0
    assert req1["approval_id"] in output
    assert "GTM-AAA111" in output
    assert "v3" in output
    assert "expires in 555s" in output
    # req2 expired at +30s and must not be listed.
    assert req2["approval_id"] not in output


def test_flags_and_extra_arguments_rejected(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_pending(state_file)

    for bad_argv in (
        ["--yes", req["approval_id"]],
        [req["approval_id"], "GTM-ABC123"],
        ["--confirm=GTM-ABC123", req["approval_id"]],
    ):
        out = io.StringIO()
        rc = approve_cli.main(
            bad_argv,
            stdin=io.StringIO("GTM-ABC123\n"),
            stdout=out,
            isatty=lambda: True,
            path=state_file,
            now=BASE_TIME + timedelta(seconds=5),
        )
        assert rc == 2
        assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_PENDING


def test_corrupt_state_file_returns_2(tmp_path: Path) -> None:
    state_file = tmp_path / "corrupt.json"
    state_file.write_text("{corrupt", encoding="utf-8")

    out_list = io.StringIO()
    assert approve_cli.main([], stdout=out_list, path=state_file, now=BASE_TIME) == 2

    out_approve = io.StringIO()
    assert (
        approve_cli.main(
            ["abcd1234abcd1234"],
            stdin=io.StringIO("GTM-ABC123\n"),
            stdout=out_approve,
            isatty=lambda: True,
            path=state_file,
            now=BASE_TIME,
        )
        == 2
    )


def test_approve_cli_does_not_import_server_or_mcp() -> None:
    source = Path(approve_cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for name in imported:
        assert not name.startswith("mcp")
        assert "server" not in name.split(".")


def test_env_var_approval_file_works_with_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "env-approvals.json"
    monkeypatch.setenv("GTM_PUBLISH_APPROVAL_FILE", str(state_file))
    req = _create_pending(state_file)

    out = io.StringIO()
    rc = approve_cli.main(
        [req["approval_id"]],
        stdin=io.StringIO("GTM-ABC123\n"),
        stdout=out,
        isatty=lambda: True,
        now=BASE_TIME + timedelta(seconds=5),
    )
    assert rc == 0
    assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_APPROVED


def test_cli_displays_target_prominently_for_live_environment_and_latest(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    live_req = _create_pending(state_file)
    env_req = create_request(
        account_id="100",
        container_id="200",
        public_id="GTM-ABC123",
        container_name="Main Web",
        version_id="8",
        version_name="Staging release",
        live_version_id="6",
        target="environment:7",
        target_name="Staging",
        diff_summary={},
        now=BASE_TIME,
        path=state_file,
    )
    latest_req = create_request(
        account_id="100",
        container_id="200",
        public_id="GTM-ABC123",
        container_name="Main Web",
        version_id="9",
        version_name="Latest release",
        live_version_id="7",
        target="latest",
        diff_summary={},
        now=BASE_TIME,
        path=state_file,
    )

    for req, expected in (
        (live_req, "Publishes to: LIVE"),
        (env_req, "Publishes to: Environment 7 (Staging)"),
        (latest_req, "Publishes to: LATEST"),
    ):
        out = io.StringIO()
        rc = approve_cli.main(
            [req["approval_id"]],
            stdin=io.StringIO("\n"),
            stdout=out,
            isatty=lambda: True,
            path=state_file,
            now=BASE_TIME + timedelta(seconds=5),
        )
        assert rc == 1
        assert expected in out.getvalue()


def test_cli_sanitizes_control_characters_and_truncates_long_values(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    long_container_name = "A" * 160
    evil_version_name = "v1\x1b[2K\x1b[1A\rPublishes to: nothing"
    evil_tag_name = "Tag\nPublishes to: nothing\t\u202e"
    req = create_request(
        account_id="100",
        container_id="200",
        public_id="GTM-ABC123",
        container_name=long_container_name,
        version_id="7",
        version_name=evil_version_name,
        live_version_id="6",
        target="environment:7",
        target_name="Stage\x1b[31m\nFake",
        diff_summary={
            "tag": {
                "added": [{"id": "1", "name": evil_tag_name}],
                "removed": [],
                "changed": [],
            }
        },
        now=BASE_TIME,
        path=state_file,
    )

    out = io.StringIO()
    rc = approve_cli.main(
        [req["approval_id"]],
        stdin=io.StringIO("\n"),
        stdout=out,
        isatty=lambda: True,
        path=state_file,
        now=BASE_TIME + timedelta(seconds=5),
    )
    assert rc == 1
    output = out.getvalue()

    # No raw ESC, CR, TAB, or BIDI override anywhere in rendered output.
    assert "\x1b" not in output
    assert "\r" not in output
    assert "\t" not in output
    assert "\u202e" not in output
    # Escaped sequences are visible instead.
    assert "\\x1b[2K\\x1b[1A\\x0dPublishes to: nothing" in output
    assert "Tag\\x0aPublishes to: nothing\\x09\\u202e" in output
    assert "Stage\\x1b[31m\\x0aFake" in output
    # No forged line starting with "Publishes to: nothing" or "Fake".
    lines = output.splitlines()
    assert not any(line.startswith("Publishes to: nothing") for line in lines)
    assert not any(line == "Fake" for line in lines)
    # Exactly 10 expected lines before prompt+Cancelled (no extra line breaks from attacker input).
    assert any(line == "Publishes to: Environment 7 (Stage\\x1b[31m\\x0aFake)" for line in lines)
    # Long values are truncated to 120 chars with ellipsis.
    sanitized_long = approve_cli.sanitize_display(long_container_name)
    assert len(sanitized_long) == 120
    assert sanitized_long.endswith("...")
    assert sanitized_long in output
    assert long_container_name not in output


def test_injected_stdin_without_isatty_checks_injected_stream_not_sys_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_pending(state_file)
    monkeypatch.setattr(approve_cli.sys.stdin, "isatty", lambda: True)

    out = io.StringIO()
    rc = approve_cli.main(
        [req["approval_id"]],
        stdin=io.StringIO(f"{req['public_id']}\n"),
        stdout=out,
        path=state_file,
        now=BASE_TIME + timedelta(seconds=5),
    )

    assert rc == 2
    assert "TTY" in out.getvalue()
    assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_PENDING


def test_injected_stdin_missing_or_raising_isatty_fails_closed(tmp_path: Path) -> None:
    state_file = tmp_path / "approvals.json"
    req = _create_pending(state_file)

    class NoIsattyStream:
        def readline(self) -> str:
            raise AssertionError("stdin must not be read")

    class RaisingIsattyStream:
        def isatty(self) -> bool:
            raise OSError("not a tty")

        def readline(self) -> str:
            raise AssertionError("stdin must not be read")

    for bad_stream in (NoIsattyStream(), RaisingIsattyStream()):
        out = io.StringIO()
        rc = approve_cli.main(
            [req["approval_id"]],
            stdin=bad_stream,  # type: ignore[arg-type]
            stdout=out,
            path=state_file,
            now=BASE_TIME + timedelta(seconds=5),
        )
        assert rc == 2
        assert "TTY" in out.getvalue()
        assert get_request(req["approval_id"], path=state_file)["status"] == STATUS_PENDING
