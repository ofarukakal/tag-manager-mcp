"""Access mode configuration, OAuth scope selection, and tool gating."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

ACCESS_MODE_ENV_VAR = "GTM_ACCESS_MODE"

SCOPE_READONLY = "https://www.googleapis.com/auth/tagmanager.readonly"
SCOPE_EDIT_CONTAINERS = "https://www.googleapis.com/auth/tagmanager.edit.containers"
SCOPE_EDIT_CONTAINERVERSIONS = (
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions"
)
SCOPE_PUBLISH = "https://www.googleapis.com/auth/tagmanager.publish"
SCOPE_DELETE_CONTAINERS = "https://www.googleapis.com/auth/tagmanager.delete.containers"
SCOPE_MANAGE_ACCOUNTS = "https://www.googleapis.com/auth/tagmanager.manage.accounts"
SCOPE_MANAGE_USERS = "https://www.googleapis.com/auth/tagmanager.manage.users"


class AccessMode(str, Enum):
    """Allowed GTM server access levels, ordered from least to most privileged."""

    READ = "read"
    WRITE = "write"
    PUBLISH = "publish"
    ADMIN = "admin"

    def __str__(self) -> str:
        return self.value


SCOPES_BY_MODE: dict[AccessMode, tuple[str, ...]] = {
    AccessMode.READ: (SCOPE_READONLY,),
    AccessMode.WRITE: (
        SCOPE_READONLY,
        SCOPE_EDIT_CONTAINERS,
        SCOPE_EDIT_CONTAINERVERSIONS,
    ),
    AccessMode.PUBLISH: (
        SCOPE_READONLY,
        SCOPE_EDIT_CONTAINERS,
        SCOPE_EDIT_CONTAINERVERSIONS,
        SCOPE_PUBLISH,
    ),
    AccessMode.ADMIN: (
        SCOPE_READONLY,
        SCOPE_EDIT_CONTAINERS,
        SCOPE_EDIT_CONTAINERVERSIONS,
        SCOPE_PUBLISH,
        SCOPE_DELETE_CONTAINERS,
        SCOPE_MANAGE_ACCOUNTS,
        SCOPE_MANAGE_USERS,
    ),
}

_MODE_RANK: dict[AccessMode, int] = {
    AccessMode.READ: 0,
    AccessMode.WRITE: 1,
    AccessMode.PUBLISH: 2,
    AccessMode.ADMIN: 3,
}

F = TypeVar("F", bound=Callable[..., Any])


class AccessModeError(ValueError):
    """Raised when an invalid access mode is configured or declared."""


class AccessDeniedError(PermissionError):
    """Raised when an operation exceeds the active access mode."""


def parse_access_mode(value: AccessMode | str) -> AccessMode:
    """Validate and normalize an access mode value, failing closed on invalid input."""
    if isinstance(value, AccessMode):
        return value
    if not isinstance(value, str):
        allowed = ", ".join(mode.value for mode in AccessMode)
        raise AccessModeError(
            f"Invalid {ACCESS_MODE_ENV_VAR}={value!r}; must be one of: {allowed}"
        )
    normalized = value.strip().lower()
    try:
        return AccessMode(normalized)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in AccessMode)
        raise AccessModeError(
            f"Invalid {ACCESS_MODE_ENV_VAR}={value!r}; must be one of: {allowed}"
        ) from exc


def get_access_mode(env: Mapping[str, str] | None = None) -> AccessMode:
    """Read GTM_ACCESS_MODE from env, defaulting to 'read' only when unset."""
    source = os.environ if env is None else env
    raw = source.get(ACCESS_MODE_ENV_VAR)
    if raw is None:
        return AccessMode.READ
    return parse_access_mode(raw)


def scopes_for_mode(mode: AccessMode | str) -> tuple[str, ...]:
    """Return the exact tuple of GTM OAuth scopes required for ``mode``."""
    return SCOPES_BY_MODE[parse_access_mode(mode)]


def is_allowed(required: AccessMode | str, active: AccessMode | str) -> bool:
    """Return True when ``active`` mode is at least as permissive as ``required``."""
    return _MODE_RANK[parse_access_mode(active)] >= _MODE_RANK[parse_access_mode(required)]


def require(
    level: AccessMode | str,
    *,
    mode: AccessMode | str | None = None,
    env: Mapping[str, str] | None = None,
) -> AccessMode:
    """Defence-in-depth runtime guard that refuses calls exceeding the active mode."""
    required = parse_access_mode(level)
    active = get_access_mode(env) if mode is None else parse_access_mode(mode)
    if not is_allowed(required, active):
        raise AccessDeniedError(
            f"Operation requires {required.value!r} access, "
            f"but active {ACCESS_MODE_ENV_VAR} is {active.value!r}."
        )
    return active


@dataclass(frozen=True)
class ToolSpec:
    """A registered tool function paired with its mandatory access level."""

    func: Callable[..., Any]
    level: AccessMode
    name: str


class ToolRegistry:
    """Collects MCP tool functions with explicit access levels and registers allowed ones."""

    def __init__(self) -> None:
        self._tools: list[ToolSpec] = []
        self._names: set[str] = set()

    def tool(self, level: AccessMode | str) -> Callable[[F], F]:
        """Decorator declaring a tool's required access level (no default permitted)."""
        required = parse_access_mode(level)

        def decorator(func: F) -> F:
            if not callable(func):
                raise TypeError(f"Expected callable tool, got {type(func).__name__}")
            name = func.__name__
            if name in self._names:
                raise ValueError(f"Duplicate tool registration: {name}")
            self._names.add(name)
            self._tools.append(ToolSpec(func=func, level=required, name=name))
            func._access_level = required  # type: ignore[attr-defined]
            return func

        return decorator

    @property
    def all_tools(self) -> tuple[ToolSpec, ...]:
        """Return all declared tools regardless of access mode."""
        return tuple(self._tools)

    def tools_for_mode(self, mode: AccessMode | str) -> tuple[ToolSpec, ...]:
        """Return only the tools permitted by ``mode``."""
        active = parse_access_mode(mode)
        return tuple(spec for spec in self._tools if is_allowed(spec.level, active))

    def register(self, server: Any, mode: AccessMode | str) -> tuple[str, ...]:
        """Register all tools allowed by ``mode`` onto ``server`` and return their names."""
        registered: list[str] = []
        for spec in self.tools_for_mode(mode):
            server.add_tool(spec.func)
            registered.append(spec.name)
        return tuple(registered)
