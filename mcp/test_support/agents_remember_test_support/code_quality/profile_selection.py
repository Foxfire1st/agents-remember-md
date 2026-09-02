"""Publish the exact repository-owned scope consumed by certification profile rails."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from agents_remember.kernel.atomic_write import atomic_write_text

from agents_remember_test_support.code_quality import scope as quality_scope
from agents_remember_test_support.code_quality import targeted
from agents_remember_test_support.code_quality.diff_coverage import resolve_base
from agents_remember_test_support.testing.dagger_admission import (
    DaggerAdmissionError,
    require_dagger_admission,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=("targeted", "full"), required=True)
    parser.add_argument("--diff-base", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def selection_payload(project_root: Path, *, mode: str, diff_base: str) -> dict[str, object]:
    """Derive one complete scope and refuse an ownership-incomplete targeted result."""

    root = project_root.resolve()
    full = quality_scope.derive_scope(root)
    if mode == "full":
        return {
            "schemaVersion": "repository-selector-result/v1",
            "mode": "full",
            "baseRevision": diff_base,
            "complete": True,
            "globalInvalidation": True,
            "changed-files": [],
            "lint-paths": _paths(full.lint_paths),
            "type-closure": _paths(full.type_paths),
            "coverage-paths": _paths(full.coverage_paths),
            "coverage-roots": _paths(full.coverage_paths),
            "selected-tests": _paths(full.test_paths),
            "size-paths": _paths(full.size_paths),
        }
    base = resolve_base(root, explicit_base=diff_base)
    derived = targeted.derive_targeted_scope(root, base.revision)
    if not derived.test_impact.complete:
        reason = derived.test_impact.fresh_rerun_reason
        detail = "unknown" if reason is None else reason.render()
        raise quality_scope.ScopeError(
            "targeted ownership is incomplete; repair the exact ownership declaration "
            f"before certification: {detail}"
        )
    return {
        "schemaVersion": "repository-selector-result/v1",
        "mode": "targeted",
        "baseRevision": base.revision,
        "complete": True,
        "globalInvalidation": derived.test_impact.global_invalidation,
        "changed-files": _paths(derived.changed_paths),
        "lint-paths": _paths(derived.lint_paths),
        "type-closure": _paths(derived.type_paths),
        "coverage-paths": _paths(derived.coverage_paths),
        "coverage-roots": list(derived.coverage_root_modules),
        "selected-tests": _paths(derived.test_paths),
        "size-paths": _paths(derived.lint_paths),
    }


def _paths(paths: Iterable[Path]) -> list[str]:
    return [path.as_posix() for path in paths]


def _confined_output(project_root: Path, requested: Path) -> Path:
    root = project_root.resolve()
    if requested.is_absolute():
        scratch_text = os.environ.get("AR_CERTIFICATION_SELECTOR_ROOT")
        if not scratch_text:
            raise quality_scope.ScopeError(
                "absolute selector output requires the admitted sandbox scratch root"
            )
        authority = Path(scratch_text).resolve()
        output = requested.resolve()
        try:
            output.relative_to(authority)
        except ValueError as error:
            raise quality_scope.ScopeError(
                "selector output escaped the admitted sandbox scratch root"
            ) from error
    else:
        output = (root / requested).resolve()
    try:
        output.relative_to(authority if requested.is_absolute() else root)
    except ValueError as error:
        raise quality_scope.ScopeError(
            "selector output must remain inside the candidate"
        ) from error
    if output.exists() and output.is_symlink():
        raise quality_scope.ScopeError("selector output cannot replace a symlink")
    return output


def main(argv: list[str] | None = None) -> int:
    try:
        require_dagger_admission(subject="repository certification selector")
        args = build_parser().parse_args(argv)
        root = args.project_root.resolve()
        payload = selection_payload(root, mode=args.mode, diff_base=args.diff_base)
        selected_tests = payload["selected-tests"]
        if not isinstance(selected_tests, list):
            raise quality_scope.ScopeError(
                "repository selector produced an invalid test population"
            )
        output = _confined_output(root, args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except (DaggerAdmissionError, OSError, RuntimeError, ValueError) as error:
        print(f"repository selector refused: {error}", file=sys.stderr, flush=True)
        return 1
    print(
        f"repository selector: {args.mode} complete; {len(selected_tests)} selected test inputs",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
