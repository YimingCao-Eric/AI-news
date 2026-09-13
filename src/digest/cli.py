"""`digest` command line entry point.

Subcommands are separate from the start so ranking can be iterated on without re-fetching
(PLAN.md section 7, phase 0). Every one of them is a stub in phase 0: it loads and prints the
config, then says what it does not yet do.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from digest.config import Config, ConfigError, load_config, summarise_config

STAGES = {
    "fetch": "pull items from every enabled source and normalise them into Item objects",
    "rank": "score stored items against the interest profile and apply per-topic quotas",
    "render": "write the selected items to digests/YYYY-MM-DD.md",
    "run": "fetch, rank, summarise and render in one pass -- the scheduled entry point",
}


def _not_implemented(command: str, config: Config, args: argparse.Namespace) -> int:
    print(f"digest {command}: not implemented")
    print(f"  will: {STAGES[command]}")
    if getattr(args, "dry_run", False):
        print("  (--dry-run requested; nothing would be written)")
    print()
    print(summarise_config(config))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="digest",
        description="Personal daily AI/tech news digest.",
    )

    # Shared by every subcommand. On a parent parser rather than the top level so that
    # `digest fetch --config-dir X` works, which is the order people actually type.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Directory holding sources.yaml and interests.yaml (default: current directory).",
    )
    common.add_argument(
        "--dry-run",
        action="store_true",
        help="Do the work but write nothing.",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="{fetch,rank,render,run}")
    for name, description in STAGES.items():
        sub = subparsers.add_parser(
            name,
            parents=[common],
            help=description,
            description=description,
        )
        sub.set_defaults(command=name)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    try:
        config = load_config(args.config_dir)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return _not_implemented(args.command, config, args)


if __name__ == "__main__":
    raise SystemExit(main())
