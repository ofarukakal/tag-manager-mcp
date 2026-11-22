from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tag_manager_mcp import auth
from tag_manager_mcp.access import (
    SCOPE_EDIT_CONTAINERS,
    SCOPE_EDIT_CONTAINERVERSIONS,
    SCOPE_PUBLISH,
    SCOPE_READONLY,
    AccessModeError,
    scopes_for_mode,
)
from tag_manager_mcp.auth import SCOPES, AuthenticationError, CredentialProvider


def _write_token(path: Path, scopes: list[str] | None, *, valid: bool = True) -> None:
    expiry = (
        datetime.now(timezone.utc) + timedelta(hours=1)
        if valid
        else datetime.now(timezone.utc) - timedelta(hours=1)
    )
    payload: dict[str, Any] = {
        "token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "fake-client-id",
        "client_secret": "fake-client-secret",
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if scopes is not None:
        payload["scopes"] = scopes
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_invalid_auth_mode() -> None:
    with pytest.raises(AuthenticationError, match="GTM_AUTH_MODE"):
        CredentialProvider({"GTM_AUTH_MODE": "magic"})


def test_auto_mode_reports_both_missing_methods(tmp_path) -> None:
    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "auto",
            "GTM_OAUTH_TOKEN_FILE": str(tmp_path / "missing-token.json"),
        }
    )
    with pytest.raises(AuthenticationError) as exc:
        provider.get_credentials(interactive=False)
    message = str(exc.value)
    assert "user OAuth" in message
    assert "service account" in message


def test_module_scopes_constant_stays_importable_and_readonly() -> None:
    assert SCOPES == (SCOPE_READONLY,)


def test_invalid_access_mode_fails_closed() -> None:
    with pytest.raises(AccessModeError, match="GTM_ACCESS_MODE"):
        CredentialProvider({"GTM_ACCESS_MODE": "superadmin"})


def test_cached_token_lacking_required_scope_rejected_non_interactively(tmp_path) -> None:
    token_file = tmp_path / "token.json"
    _write_token(token_file, [SCOPE_READONLY])

    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "user",
            "GTM_ACCESS_MODE": "write",
            "GTM_OAUTH_TOKEN_FILE": str(token_file),
        }
    )
    with pytest.raises(AuthenticationError, match="tag-manager-auth"):
        provider.get_credentials(interactive=False)


@pytest.mark.parametrize("scopes", [None, []])
def test_cached_token_without_scopes_field_rejected_non_interactively(
    tmp_path: Path, scopes: list[str] | None
) -> None:
    token_file = tmp_path / "token.json"
    _write_token(token_file, scopes)

    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "user",
            "GTM_ACCESS_MODE": "read",
            "GTM_OAUTH_TOKEN_FILE": str(token_file),
        }
    )
    with pytest.raises(AuthenticationError, match="tag-manager-auth"):
        provider.get_credentials(interactive=False)


def test_cached_token_with_broader_scopes_rejected_in_read_mode(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    _write_token(token_file, list(scopes_for_mode("publish")))

    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "user",
            "GTM_ACCESS_MODE": "read",
            "GTM_OAUTH_TOKEN_FILE": str(token_file),
        }
    )
    with pytest.raises(AuthenticationError) as exc:
        provider.get_credentials(interactive=False)
    message = str(exc.value)
    assert "'read' mode" in message
    assert "different scopes" in message
    assert "tag-manager-auth" in message
    assert "GTM_ACCESS_MODE=read" in message


def test_cached_token_with_exact_scopes_in_different_order_accepted(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    reordered = [
        SCOPE_EDIT_CONTAINERVERSIONS,
        SCOPE_EDIT_CONTAINERS,
        SCOPE_READONLY,
    ]
    _write_token(token_file, reordered)

    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "user",
            "GTM_ACCESS_MODE": "write",
            "GTM_OAUTH_TOKEN_FILE": str(token_file),
        }
    )
    result = provider.get_credentials(interactive=False)
    assert result.method == "user-oauth"
    assert set(result.credentials.scopes or ()) == set(scopes_for_mode("write"))


@pytest.mark.parametrize(
    ("env", "expected_name"),
    [
        ({}, "token-read.json"),
        ({"GTM_ACCESS_MODE": "read"}, "token-read.json"),
        ({"GTM_ACCESS_MODE": "write"}, "token-write.json"),
        ({"GTM_ACCESS_MODE": "publish"}, "token-publish.json"),
    ],
)
def test_default_token_file_path_is_scoped_per_mode(
    env: dict[str, str], expected_name: str
) -> None:
    provider = CredentialProvider(env)
    assert provider.token_file == Path.home() / ".config/gtm-mcp" / expected_name


def test_explicit_oauth_token_file_overrides_default(tmp_path: Path) -> None:
    custom = tmp_path / "custom-token.json"
    provider = CredentialProvider(
        {
            "GTM_ACCESS_MODE": "publish",
            "GTM_OAUTH_TOKEN_FILE": str(custom),
        }
    )
    assert provider.token_file == custom


def test_cached_token_with_broader_scopes_reruns_consent_flow_interactively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token.json"
    secrets_file = tmp_path / "client_secret.json"
    secrets_file.write_text("{}", encoding="utf-8")
    _write_token(token_file, list(scopes_for_mode("publish")))

    requested: list[tuple[str, tuple[str, ...]]] = []

    class FakeCreds:
        def __init__(self, scopes: tuple[str, ...]) -> None:
            self.scopes = list(scopes)
            self.valid = True
            self.expired = False
            self.refresh_token = "r"

        def to_json(self) -> str:
            return json.dumps({"scopes": self.scopes, "token": "new"})

    class FakeFlow:
        def __init__(self, scopes: tuple[str, ...]) -> None:
            self.scopes = scopes

        def run_local_server(self, **kwargs: Any) -> FakeCreds:
            return FakeCreds(self.scopes)

    def fake_from_secrets(path: str, scopes: tuple[str, ...]) -> FakeFlow:
        requested.append((path, tuple(scopes)))
        return FakeFlow(tuple(scopes))

    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", fake_from_secrets)

    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "user",
            "GTM_ACCESS_MODE": "read",
            "GTM_OAUTH_TOKEN_FILE": str(token_file),
            "GTM_OAUTH_CLIENT_SECRETS": str(secrets_file),
        }
    )
    result = provider.get_credentials(interactive=True)
    assert result.method == "user-oauth"
    assert requested == [(str(secrets_file), (SCOPE_READONLY,))]
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["scopes"] == [SCOPE_READONLY]


def test_refreshed_token_with_broader_scopes_rejected_and_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token.json"
    _write_token(token_file, [SCOPE_READONLY], valid=False)
    original_content = token_file.read_text(encoding="utf-8")

    def fake_refresh(self: Any, request: Any) -> None:
        self._granted_scopes = list(scopes_for_mode("publish"))
        self.token = "refreshed-access-token"
        self.expiry = datetime.now(timezone.utc) + timedelta(hours=1)

    monkeypatch.setattr(auth.UserCredentials, "refresh", fake_refresh)

    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "user",
            "GTM_ACCESS_MODE": "read",
            "GTM_OAUTH_TOKEN_FILE": str(token_file),
        }
    )
    with pytest.raises(AuthenticationError) as exc:
        provider.get_credentials(interactive=False)
    assert "'read' mode" in str(exc.value)
    assert "GTM_ACCESS_MODE=read" in str(exc.value)
    assert token_file.read_text(encoding="utf-8") == original_content


def test_consent_flow_returning_broader_scopes_rejected_and_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token.json"
    secrets_file = tmp_path / "client_secret.json"
    secrets_file.write_text("{}", encoding="utf-8")

    class BroaderFakeCreds:
        def __init__(self) -> None:
            self.scopes = list(scopes_for_mode("publish"))
            self.valid = True
            self.expired = False
            self.refresh_token = "r"

        def to_json(self) -> str:
            return json.dumps({"scopes": self.scopes, "token": "new"})

    class FakeFlow:
        def run_local_server(self, **kwargs: Any) -> BroaderFakeCreds:
            return BroaderFakeCreds()

    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_secrets_file",
        lambda path, scopes: FakeFlow(),
    )

    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "user",
            "GTM_ACCESS_MODE": "read",
            "GTM_OAUTH_TOKEN_FILE": str(token_file),
            "GTM_OAUTH_CLIENT_SECRETS": str(secrets_file),
        }
    )
    with pytest.raises(AuthenticationError) as exc:
        provider.get_credentials(interactive=True)
    assert "'read' mode" in str(exc.value)
    assert "GTM_ACCESS_MODE=read" in str(exc.value)
    assert not token_file.exists()


def test_cached_token_lacking_scope_reruns_consent_flow_interactively(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "token.json"
    secrets_file = tmp_path / "client_secret.json"
    secrets_file.write_text("{}", encoding="utf-8")
    _write_token(token_file, [SCOPE_READONLY])

    requested: list[tuple[str, tuple[str, ...]]] = []

    class FakeCreds:
        def __init__(self, scopes: tuple[str, ...]) -> None:
            self.scopes = list(scopes)
            self.valid = True
            self.expired = False
            self.refresh_token = "r"

        def to_json(self) -> str:
            return json.dumps({"scopes": self.scopes, "token": "new"})

    class FakeFlow:
        def __init__(self, scopes: tuple[str, ...]) -> None:
            self.scopes = scopes

        def run_local_server(self, **kwargs: Any) -> FakeCreds:
            return FakeCreds(self.scopes)

    def fake_from_secrets(path: str, scopes: tuple[str, ...]) -> FakeFlow:
        requested.append((path, tuple(scopes)))
        return FakeFlow(tuple(scopes))

    monkeypatch.setattr(auth.InstalledAppFlow, "from_client_secrets_file", fake_from_secrets)

    provider = CredentialProvider(
        {
            "GTM_AUTH_MODE": "user",
            "GTM_ACCESS_MODE": "write",
            "GTM_OAUTH_TOKEN_FILE": str(token_file),
            "GTM_OAUTH_CLIENT_SECRETS": str(secrets_file),
        }
    )
    result = provider.get_credentials(interactive=True)
    assert result.method == "user-oauth"
    assert requested == [
        (
            str(secrets_file),
            (SCOPE_READONLY, SCOPE_EDIT_CONTAINERS, SCOPE_EDIT_CONTAINERVERSIONS),
        )
    ]
    saved = json.loads(token_file.read_text(encoding="utf-8"))
    assert saved["scopes"] == [
        SCOPE_READONLY,
        SCOPE_EDIT_CONTAINERS,
        SCOPE_EDIT_CONTAINERVERSIONS,
    ]


@pytest.mark.parametrize(
    ("access_mode", "expected_scopes"),
    [
        ("read", (SCOPE_READONLY,)),
        ("write", (SCOPE_READONLY, SCOPE_EDIT_CONTAINERS, SCOPE_EDIT_CONTAINERVERSIONS)),
        (
            "publish",
            (SCOPE_READONLY, SCOPE_EDIT_CONTAINERS, SCOPE_EDIT_CONTAINERVERSIONS, SCOPE_PUBLISH),
        ),
    ],
)
def test_service_account_and_adc_request_mode_scopes(
    access_mode: str,
    expected_scopes: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sa_calls: list[tuple[str, tuple[str, ...]]] = []
    adc_calls: list[tuple[str, ...]] = []

    def fake_sa_info(info: dict[str, Any], scopes: tuple[str, ...]) -> object:
        sa_calls.append(("info", tuple(scopes)))
        return object()

    def fake_sa_file(path: str, scopes: tuple[str, ...]) -> object:
        sa_calls.append(("file", tuple(scopes)))
        return object()

    def fake_adc(scopes: tuple[str, ...]) -> tuple[object, str]:
        adc_calls.append(tuple(scopes))
        return object(), "proj"

    monkeypatch.setattr(auth.ServiceAccountCredentials, "from_service_account_info", fake_sa_info)
    monkeypatch.setattr(auth.ServiceAccountCredentials, "from_service_account_file", fake_sa_file)
    monkeypatch.setattr(auth, "default_credentials", fake_adc)

    sa_file = tmp_path / "sa.json"
    sa_file.write_text('{"type": "service_account"}', encoding="utf-8")

    CredentialProvider(
        {
            "GTM_AUTH_MODE": "service-account",
            "GTM_ACCESS_MODE": access_mode,
            "GTM_SERVICE_ACCOUNT_JSON": '{"type": "service_account"}',
        }
    ).get_credentials(interactive=False)

    CredentialProvider(
        {
            "GTM_AUTH_MODE": "service-account",
            "GTM_ACCESS_MODE": access_mode,
            "GTM_SERVICE_ACCOUNT_FILE": str(sa_file),
        }
    ).get_credentials(interactive=False)

    CredentialProvider(
        {
            "GTM_AUTH_MODE": "adc",
            "GTM_ACCESS_MODE": access_mode,
        }
    ).get_credentials(interactive=False)

    assert sa_calls == [("info", expected_scopes), ("file", expected_scopes)]
    assert adc_calls == [expected_scopes]

