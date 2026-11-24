from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tag_manager_mcp import export_cli
from tag_manager_mcp.access import AccessMode
from tag_manager_mcp.auth import AuthenticationError
from tag_manager_mcp.client import GTMError


class FakeExportReader:
    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        *,
        for_items: list[dict[str, Any]] | None = None,
        auth_error: Exception | None = None,
    ) -> None:
        self.items = items or []
        self.for_items = for_items
        self.auth_error = auth_error
        self.contexts: list[str | None] = []
        self.requested_containers: list[list[str]] = []

    def fetch_all_live(self, usage_context: str | None = "web") -> list[dict[str, Any]]:
        self.contexts.append(usage_context)
        if self.auth_error is not None:
            raise self.auth_error
        return list(self.items)

    def fetch_live_for(self, public_ids: list[str]) -> list[dict[str, Any]]:
        self.requested_containers.append(list(public_ids))
        if self.auth_error is not None:
            raise self.auth_error
        if self.for_items is not None:
            return list(self.for_items)
        by_id = {
            str(item.get("publicId", "")).strip().upper(): item
            for item in self.items
        }
        return [
            by_id.get(
                cid.strip().upper(),
                {
                    "accountId": "",
                    "containerId": "",
                    "publicId": cid,
                    "name": "",
                    "version": None,
                    "error": f"Container {cid} not found",
                },
            )
            for cid in public_ids
        ]


def test_export_cli_exits_0_and_writes_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = FakeExportReader(
        [
            {
                "accountId": "1",
                "containerId": "10",
                "publicId": "GTM-AAA111",
                "name": "Site A",
                "version": {
                    "containerVersionId": "12",
                    "tag": [{"tagId": "1"}, {"tagId": "2"}],
                },
                "error": None,
            },
            {
                "accountId": "1",
                "containerId": "20",
                "publicId": "GTM-BBB222",
                "name": "Site B",
                "version": {"containerVersionId": "3", "tag": []},
                "error": None,
            },
        ]
    )

    code = export_cli.main(["--out", str(tmp_path)], reader=reader)
    assert code == 0
    assert reader.contexts == ["web"]

    file_a = tmp_path / "GTM-AAA111_v12.json"
    file_b = tmp_path / "GTM-BBB222_v3.json"
    assert file_a.is_file()
    assert file_b.is_file()

    payload_a = json.loads(file_a.read_text(encoding="utf-8"))
    assert set(payload_a.keys()) == {"exportFormatVersion", "exportTime", "containerVersion"}
    assert payload_a["containerVersion"]["containerVersionId"] == "12"

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        f"GTM-AAA111  v12  2 tags  {file_a.resolve()}",
        f"GTM-BBB222  v3  0 tags  {file_b.resolve()}",
    ]


def test_export_cli_container_uses_fetch_live_for_and_ignores_usage_context(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = FakeExportReader(
        for_items=[
            {
                "accountId": "1",
                "containerId": "10",
                "publicId": "A",
                "name": "Site A",
                "version": {"containerVersionId": "12", "tag": []},
                "error": None,
            },
            {
                "accountId": "1",
                "containerId": "20",
                "publicId": "B",
                "name": "Site B",
                "version": {"containerVersionId": "3", "tag": []},
                "error": None,
            },
        ]
    )

    code = export_cli.main(
        [
            "--out",
            str(tmp_path),
            "--usage-context",
            "server",
            "--container",
            "A",
            "--container",
            "B",
        ],
        reader=reader,
    )
    assert code == 0
    assert reader.contexts == []
    assert reader.requested_containers == [["A", "B"]]
    assert (tmp_path / "A_v12.json").is_file()
    assert (tmp_path / "B_v3.json").is_file()
    assert capsys.readouterr().out.strip().splitlines() == [
        f"A  v12  0 tags  {(tmp_path / 'A_v12.json').resolve()}",
        f"B  v3  0 tags  {(tmp_path / 'B_v3.json').resolve()}",
    ]


def test_export_cli_container_partial_failure_exits_1_and_writes_good(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = FakeExportReader(
        for_items=[
            {
                "accountId": "1",
                "containerId": "10",
                "publicId": "GTM-OK1",
                "name": "OK",
                "version": {"containerVersionId": "5", "tag": []},
                "error": None,
            },
            {
                "accountId": "",
                "containerId": "",
                "publicId": "GTM-BAD1",
                "name": "",
                "version": None,
                "error": "Google Tag Manager API error: Not Found",
            },
        ]
    )

    code = export_cli.main(
        [
            "--out",
            str(tmp_path),
            "--container",
            "GTM-OK1",
            "--container",
            "GTM-BAD1",
        ],
        reader=reader,
    )
    assert code == 1
    assert reader.contexts == []
    assert reader.requested_containers == [["GTM-OK1", "GTM-BAD1"]]
    assert (tmp_path / "GTM-OK1_v5.json").is_file()
    assert capsys.readouterr().out.strip().splitlines() == [
        f"GTM-OK1  v5  0 tags  {(tmp_path / 'GTM-OK1_v5.json').resolve()}",
        "GTM-BAD1  ERROR  Google Tag Manager API error: Not Found",
    ]


def test_export_cli_usage_context_all_without_container(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = FakeExportReader(
        [
            {
                "accountId": "1",
                "containerId": "20",
                "publicId": "GTM-BBB222",
                "name": "Site B",
                "version": {"containerVersionId": "3", "tag": []},
                "error": None,
            },
        ]
    )

    code = export_cli.main(
        ["--out", str(tmp_path), "--usage-context", "all"],
        reader=reader,
    )
    assert code == 0
    assert reader.contexts == [None]
    assert reader.requested_containers == []
    assert (tmp_path / "GTM-BBB222_v3.json").is_file()
    assert capsys.readouterr().out.strip() == (
        f"GTM-BBB222  v3  0 tags  {(tmp_path / 'GTM-BBB222_v3.json').resolve()}"
    )


def test_export_cli_help_notes_usage_context_ignored_with_container() -> None:
    help_text = export_cli._build_parser().format_help()
    assert "ignored when --container is given" in help_text


def test_export_cli_exits_1_on_container_error_or_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = FakeExportReader(
        [
            {
                "accountId": "1",
                "containerId": "10",
                "publicId": "GTM-OK1",
                "name": "OK",
                "version": {"containerVersionId": "5", "tag": []},
                "error": None,
            },
            {
                "accountId": "1",
                "containerId": "20",
                "publicId": "GTM-FAIL1",
                "name": "Fail",
                "version": None,
                "error": "Published version not found",
            },
        ]
    )

    code = export_cli.main(["--out", str(tmp_path)], reader=reader)
    assert code == 1
    assert (tmp_path / "GTM-OK1_v5.json").is_file()
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        f"GTM-OK1  v5  0 tags  {(tmp_path / 'GTM-OK1_v5.json').resolve()}",
        "GTM-FAIL1  ERROR  Published version not found",
    ]

    empty_code = export_cli.main(
        ["--out", str(tmp_path), "--container", "GTM-MISSING"],
        reader=reader,
    )
    assert empty_code == 1


def test_export_cli_exits_2_on_authentication_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = FakeExportReader(auth_error=AuthenticationError("no valid cached user token"))

    code = export_cli.main(["--out", str(tmp_path)], reader=reader)
    assert code == 2
    assert "Authentication failed" in capsys.readouterr().err


def test_build_readonly_reader_forces_read_access_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GTM_ACCESS_MODE", "publish")
    reader = export_cli._build_readonly_reader()
    assert reader.client._provider.access_mode is AccessMode.READ


def test_export_cli_does_not_import_server_and_exits_2_on_invalid_access_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, tag_manager_mcp.export_cli as cli; "
                "assert 'tag_manager_mcp.server' not in sys.modules; "
                "raise SystemExit(cli.main([]))"
            ),
        ],
        env={"PATH": "", "GTM_ACCESS_MODE": "bogus"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    err_lines = proc.stderr.strip().splitlines()
    assert len(err_lines) == 1
    assert "GTM_ACCESS_MODE" in err_lines[0]

    monkeypatch.setenv("GTM_ACCESS_MODE", "bogus")
    code = export_cli.main([], reader=FakeExportReader())
    assert code == 2
    assert "GTM_ACCESS_MODE" in capsys.readouterr().err


def test_export_cli_exits_1_on_fetch_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = FakeExportReader(auth_error=GTMError("API 503 backend error"))

    code = export_cli.main(["--out", str(tmp_path)], reader=reader)
    assert code == 1
    err_lines = capsys.readouterr().err.strip().splitlines()
    assert err_lines == ["ERROR  API 503 backend error"]


def test_export_cli_continues_past_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    reader = FakeExportReader(
        [
            {
                "accountId": "1",
                "containerId": "10",
                "publicId": "GTM-OK1",
                "name": "OK1",
                "version": {"containerVersionId": "1", "tag": []},
                "error": None,
            },
            {
                "accountId": "1",
                "containerId": "20",
                "publicId": "GTM-FAIL",
                "name": "Fail",
                "version": {"containerVersionId": "2", "tag": []},
                "error": None,
            },
            {
                "accountId": "1",
                "containerId": "30",
                "publicId": "GTM-OK2",
                "name": "OK2",
                "version": {"containerVersionId": "3", "tag": []},
                "error": None,
            },
        ]
    )
    real_write = export_cli.write_version_export

    def flaky_write(version: dict[str, Any], **kwargs: Any) -> Path:
        if kwargs.get("public_id") == "GTM-FAIL":
            raise OSError("permission denied")
        return real_write(version, **kwargs)

    monkeypatch.setattr(export_cli, "write_version_export", flaky_write)

    code = export_cli.main(["--out", str(tmp_path)], reader=reader)
    assert code == 1
    assert (tmp_path / "GTM-OK1_v1.json").is_file()
    assert not (tmp_path / "GTM-FAIL_v2.json").exists()
    assert (tmp_path / "GTM-OK2_v3.json").is_file()
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == [
        f"GTM-OK1  v1  0 tags  {(tmp_path / 'GTM-OK1_v1.json').resolve()}",
        "GTM-FAIL  ERROR  permission denied",
        f"GTM-OK2  v3  0 tags  {(tmp_path / 'GTM-OK2_v3.json').resolve()}",
    ]
