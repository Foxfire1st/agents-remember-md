"""Command-line entry point for the pinned Dagger Ubuntu quality proof."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents_remember.worktrees.modules.quality.clean_executor import (
    CleanQualityRequest,
    run_clean_quality,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-worktree", type=Path, default=Path.cwd())
    parser.add_argument("--worktree-group", type=Path, required=True)
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--certification-profile", type=Path, required=True)
    parser.add_argument("--mode", choices=("targeted", "full"), default="full")
    parser.add_argument("--diff-base", default="")
    parser.add_argument("--memory-cap-bytes", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_clean_quality(
            CleanQualityRequest(
                code_worktree=args.code_worktree.resolve(),
                worktree_group=args.worktree_group.resolve(),
                repository_id=args.repository_id,
                profile_reference=args.certification_profile,
                mode=args.mode,
                diff_base=args.diff_base,
                memory_cap_bytes=args.memory_cap_bytes or None,
            )
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"clean quality refused: {error}", file=sys.stderr, flush=True)
        return 1
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
