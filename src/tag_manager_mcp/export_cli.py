"""Command-line bulk export for live Google Tag Manager container versions."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from .access import AccessMode, AccessModeError, get_access_mode
from .auth import AuthenticationError, CredentialProvider
from .client import GTMClient
from .export_files import write_version_export
from .reader import GTMReader


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tag-manager-export",
        description="Export live Google Tag Manager container versions as JSON files.",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help="Output directory (defaults to GTM_EXPORT_DIR or ~/.local/share/gtm-mcp/exports).",
    )
    parser.add_argument(
        "--usage-context",
        default="web",
        help="Container usageContext filter (default: web; use 'all' for no filter).",
    )
    parser.add_argument(
        "--container",
        action="append",
        default=None,
        metavar="PUBLIC_ID",
        help="Export only the given container public ID (repeatable).",
    )
    return parser


def _build_readonly_reader() -> GTMReader:
    env = {**os.environ, "GTM_ACCESS_MODE": AccessMode.READ.value}
    provider = CredentialProvider(env=env)
    return GTMReader(GTMClient(provider=provider))


def main(argv: Sequence[str] | None = None, reader: GTMReader | None = None) -> int:
    """Run the bulk GTM live-container export CLI and return an exit status code."""
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    usage_context = None if args.usage_context == "all" else args.usage_context
    container_filter = (
        {cid.strip().upper() for cid in args.container} if args.container else None
    )

    try:
        get_access_mode()
        active_reader = reader if reader is not None else _build_readonly_reader()
        items = active_reader.fetch_all_live(usage_context=usage_context)
    except AccessModeError as exc:
        msg = " ".join(str(exc).splitlines())
        print(f"Configuration error: {msg}", file=sys.stderr)
        return 2
    except AuthenticationError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        msg = " ".join(str(exc).splitlines())
        print(f"ERROR  {msg}", file=sys.stderr)
        return 1

    if container_filter is not None:
        items = [
            item
            for item in items
            if str(item.get("publicId", "")).strip().upper() in container_filter
        ]

    exported = 0
    failed = 0

    for item in items:
        public_id = str(item.get("publicId") or item.get("containerId") or "")
        version = item.get("version")
        error = item.get("error")
        if error or not isinstance(version, dict):
            print(f"{public_id}  ERROR  {error or 'No live version returned'}")
            failed += 1
            continue

        try:
            path = write_version_export(
                version,
                public_id=public_id,
                export_dir=args.out,
            )
        except Exception as exc:
            print(f"{public_id}  ERROR  {exc}")
            failed += 1
            continue

        version_id = str(version.get("containerVersionId") or "")
        tag_count = len(version.get("tag") or [])
        print(f"{public_id}  v{version_id}  {tag_count} tags  {path}")
        exported += 1

    if failed > 0 or exported == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
