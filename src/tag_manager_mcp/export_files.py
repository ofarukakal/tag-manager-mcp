"""Helpers for resolving GTM export paths and writing container version JSON files."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_EXPORT_DIR = Path("~/.local/share/gtm-mcp/exports")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def get_export_dir(export_dir: str | Path | None = None) -> Path:
    """Resolve the GTM export directory from argument, GTM_EXPORT_DIR, or the default path."""
    raw = export_dir if export_dir is not None else os.getenv("GTM_EXPORT_DIR")
    target = Path(raw).expanduser() if raw else DEFAULT_EXPORT_DIR.expanduser()
    return target.resolve()


def write_version_export(
    export_or_version: dict[str, Any],
    *,
    public_id: str | None = None,
    fallback_public_id: str | None = None,
    export_dir: str | Path | None = None,
) -> Path:
    """Write a container version export JSON file into the export directory and return its path."""
    if (
        isinstance(export_or_version.get("containerVersion"), dict)
        and "exportFormatVersion" in export_or_version
    ):
        payload = export_or_version
        version = export_or_version["containerVersion"]
    else:
        version = export_or_version
        payload = {
            "exportFormatVersion": 2,
            "exportTime": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "containerVersion": version,
        }

    api_public_id = (
        (version.get("container") or {}).get("publicId")
        or public_id
        or version.get("publicId")
        or fallback_public_id
        or ""
    )
    api_version_id = version.get("containerVersionId") or ""
    safe_public_id = _SAFE_FILENAME_RE.sub("_", str(api_public_id))
    safe_version_id = _SAFE_FILENAME_RE.sub("_", str(api_version_id))
    filename = f"{safe_public_id}_v{safe_version_id}.json"

    base_dir = get_export_dir(export_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    resolved_dir = base_dir.resolve()
    target = (resolved_dir / filename).resolve()
    if not target.is_relative_to(resolved_dir) or target == resolved_dir:
        raise ValueError(f"Export path escapes export directory: {target}")

    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target
