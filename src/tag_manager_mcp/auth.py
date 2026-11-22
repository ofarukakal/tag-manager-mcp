"""Local Google OAuth and service-account authentication for GTM."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from google.auth import default as default_credentials
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .access import AccessMode, get_access_mode, scopes_for_mode

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=False)

SCOPES = scopes_for_mode(AccessMode.READ)


class AuthenticationError(RuntimeError):
    """Raised when no configured Google authentication method succeeds."""


@dataclass(frozen=True)
class AuthResult:
    credentials: Credentials
    method: str


class CredentialProvider:
    """Resolve credentials with user OAuth first and service account second."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self.env = env if env is not None else os.environ
        self.mode = self.env.get("GTM_AUTH_MODE", "auto").strip().lower()
        if self.mode not in {"auto", "user", "service-account", "adc"}:
            raise AuthenticationError(
                "GTM_AUTH_MODE must be one of: auto, user, service-account, adc"
            )
        self.access_mode = get_access_mode(self.env)
        self.scopes = scopes_for_mode(self.access_mode)

    @property
    def token_file(self) -> Path:
        configured = self.env.get("GTM_OAUTH_TOKEN_FILE")
        if configured:
            return Path(configured).expanduser()
        return Path.home() / f".config/gtm-mcp/token-{self.access_mode.value}.json"

    @property
    def client_secrets_file(self) -> Path | None:
        configured = self.env.get("GTM_OAUTH_CLIENT_SECRETS")
        return Path(configured).expanduser() if configured else None

    def get_credentials(self, *, interactive: bool = True) -> AuthResult:
        errors: list[str] = []

        if self.mode in {"auto", "user"}:
            try:
                return AuthResult(self._user_credentials(interactive=interactive), "user-oauth")
            except Exception as exc:  # preserve fallback behavior and report every attempt
                errors.append(f"user OAuth: {exc}")
                if self.mode == "user":
                    raise AuthenticationError(errors[-1]) from exc

        if self.mode in {"auto", "service-account"}:
            try:
                return AuthResult(self._service_account_credentials(), "service-account")
            except Exception as exc:
                errors.append(f"service account: {exc}")
                if self.mode == "service-account":
                    raise AuthenticationError(errors[-1]) from exc

        if self.mode == "adc":
            try:
                credentials, _ = default_credentials(scopes=self.scopes)
                return AuthResult(credentials, "application-default-credentials")
            except Exception as exc:
                errors.append(f"application default credentials: {exc}")

        hint = (
            "Configure GTM_OAUTH_CLIENT_SECRETS and run `tag-manager-auth`, or configure "
            "GTM_SERVICE_ACCOUNT_FILE as fallback."
        )
        raise AuthenticationError("; ".join(errors) + f". {hint}")

    def _scopes_match(self, credentials: UserCredentials) -> bool:
        expected = set(self.scopes)
        if set(credentials.scopes or ()) != expected:
            return False
        granted = getattr(credentials, "granted_scopes", None)
        return granted is None or set(granted) == expected

    def _scope_mismatch_error(
        self, source: str, credentials: UserCredentials
    ) -> AuthenticationError:
        expected = set(self.scopes)
        granted_attr = getattr(credentials, "granted_scopes", None)
        if granted_attr is not None and set(granted_attr) != expected:
            granted = set(granted_attr)
        else:
            granted = set(credentials.scopes or ())
        details: list[str] = []
        missing = [scope for scope in self.scopes if scope not in granted]
        extra = sorted(granted - expected)
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected: {', '.join(extra)}")
        detail_text = f" ({'; '.join(details)})" if details else ""
        return AuthenticationError(
            f"{source} was granted for different scopes than required for "
            f"{self.access_mode.value!r} mode{detail_text}; "
            f"re-run `tag-manager-auth` with GTM_ACCESS_MODE={self.access_mode.value}"
        )

    def _user_credentials(self, *, interactive: bool) -> UserCredentials:
        credentials: UserCredentials | None = None
        if self.token_file.exists():
            try:
                # Load without overriding scopes so we verify the token's actual granted scopes.
                credentials = UserCredentials.from_authorized_user_file(str(self.token_file))
            except Exception:
                if not interactive:
                    raise

        if credentials is not None and not self._scopes_match(credentials):
            if not interactive:
                raise self._scope_mismatch_error("cached user token", credentials)
            credentials = None

        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception:
                if not interactive:
                    raise
                credentials = None

            if credentials is not None:
                if not self._scopes_match(credentials):
                    if not interactive:
                        raise self._scope_mismatch_error("refreshed user token", credentials)
                    credentials = None
                else:
                    self._save_user_credentials(credentials)

        if credentials and credentials.valid:
            return credentials

        if not interactive:
            raise AuthenticationError("no valid cached user token; run `tag-manager-auth`")

        secrets = self.client_secrets_file
        if secrets is None:
            raise AuthenticationError("GTM_OAUTH_CLIENT_SECRETS is not configured")
        if not secrets.is_file():
            raise AuthenticationError(f"OAuth client secrets file not found: {secrets}")

        flow = InstalledAppFlow.from_client_secrets_file(str(secrets), scopes=self.scopes)
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            access_type="offline",
            prompt="consent",
            open_browser=True,
        )
        if not self._scopes_match(credentials):
            raise self._scope_mismatch_error("granted OAuth token", credentials)
        self._save_user_credentials(credentials)
        return credentials

    def _save_user_credentials(self, credentials: UserCredentials) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(credentials.to_json(), encoding="utf-8")
        self.token_file.chmod(0o600)

    def _service_account_credentials(self) -> ServiceAccountCredentials:
        raw_json = self.env.get("GTM_SERVICE_ACCOUNT_JSON")
        filename = self.env.get("GTM_SERVICE_ACCOUNT_FILE")
        # GOOGLE_APPLICATION_CREDENTIALS is accepted as the conventional fallback path.
        filename = filename or self.env.get("GOOGLE_APPLICATION_CREDENTIALS")

        if raw_json:
            info = json.loads(raw_json)
            return ServiceAccountCredentials.from_service_account_info(info, scopes=self.scopes)
        if filename:
            path = Path(filename).expanduser()
            if not path.is_file():
                raise AuthenticationError(f"service-account file not found: {path}")
            return ServiceAccountCredentials.from_service_account_file(
                str(path), scopes=self.scopes
            )
        raise AuthenticationError("no service-account credentials configured")
