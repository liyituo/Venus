#!/usr/bin/env python3
"""Preview or apply the conservative legacy team-project access migration.

The data directory is mandatory so a preview cannot silently trigger the
repository's default data-directory migration. Apply requires a pre-existing
backup made while the Hub is stopped.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", required=True, type=Path,
        help="absolute path to the existing Hub data directory (normally .venus)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="apply the previewed owner-only migration; stop Hub first",
    )
    parser.add_argument(
        "--backup-path", type=Path,
        help="existing off-data-directory backup archive; required with --apply",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.data_dir.is_absolute():
        parser.error("--data-dir must be absolute")
    data_dir = args.data_dir.resolve(strict=True)
    if not data_dir.is_dir():
        parser.error("--data-dir must be an existing directory")

    if args.apply:
        if args.backup_path is None:
            parser.error("--apply requires --backup-path")
        backup = args.backup_path.resolve(strict=True)
        if not backup.is_file() or backup.stat().st_size == 0:
            parser.error("--backup-path must be a nonempty existing file")
        if backup.is_relative_to(data_dir):
            parser.error("backup must be outside the Hub data directory")
    elif args.backup_path is not None:
        parser.error("--backup-path is only used with --apply")

    os.environ["VENUS_DATA_DIR"] = str(data_dir)
    src_dir = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(src_dir))
    import project_access  # noqa: E402

    # The service function must keep dry_run read-only, including schema setup.
    report = project_access.migrate_legacy_projects(dry_run=not args.apply)
    print(json.dumps({"mode": "apply" if args.apply else "dry-run",
                      "data_dir": str(data_dir), "report": report},
                     ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
