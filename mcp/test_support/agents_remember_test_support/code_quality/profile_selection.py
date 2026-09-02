"""Publish the exact repository-owned scope consumed by certification profile rails."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.selection_results import (
    RepositorySelectionDraft,
    RepositorySelectionReason,
    RepositorySelectionResult,
    build_repository_selection_result,
)
from agents_remember.kernel import git_command
from agents_remember.kernel.atomic_write import atomic_write_text

from agents_remember_test_support.code_quality import scope as quality_scope
from agents_remember_test_support.code_quality import targeted
from agents_remember_test_support.code_quality.dependency_ownership import (
    DASHBOARD_TEST_PATTERNS,
    OWNERSHIP_AUTHORITY_VERSION,
    SelectionReason,
    ownership_configuration_digest,
)
from agents_remember_test_support.code_quality.diff_coverage import resolve_base
from agents_remember_test_support.testing.dagger_admission import (
    DaggerAdmissionError,
    require_dagger_admission,
)

SELECTOR_ID = "agents-remember-test-selection"
SELECTOR_VERSION = OWNERSHIP_AUTHORITY_VERSION
SELECTOR_OUTPUTS = (
    "changed-files",
    "coverage-paths",
    "coverage-roots",
    "dashboard-tests",
    "lint-paths",
    "selected-tests",
    "size-paths",
    "type-closure",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=("targeted", "full"), required=True)
    parser.add_argument("--diff-base", required=True)
    parser.add_argument("--candidate-kind", required=True)
    parser.add_argument("--candidate-value", required=True)
    parser.add_argument("--selector-id", required=True)
    parser.add_argument("--selector-version", required=True)
    parser.add_argument("--selector-configuration-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def selection_result(
    project_root: Path,
    *,
    mode: str,
    diff_base: str,
    candidate_identity: CandidateIdentity | None = None,
) -> RepositorySelectionResult:
    """Derive one content-addressed complete or typed-incomplete repository scope."""

    root = project_root.resolve()
    candidate = candidate_identity or _candidate_identity(root)
    full = quality_scope.derive_scope(root)
    dashboard_tests = _dashboard_tests(root)
    if mode == "full":
        outputs = {
            "changed-files": (),
            "lint-paths": _paths(full.lint_paths),
            "type-closure": _paths(full.type_paths),
            "coverage-paths": _paths(full.coverage_paths),
            "coverage-roots": _paths(full.coverage_paths),
            "dashboard-tests": dashboard_tests,
            "selected-tests": _paths(full.test_paths),
            "size-paths": _paths(full.size_paths),
        }
        return build_repository_selection_result(
            RepositorySelectionDraft(
                selector_id=SELECTOR_ID,
                selector_version=SELECTOR_VERSION,
                configuration_digest=ownership_configuration_digest(),
                candidate_identity=candidate,
                mode="full",
                base_revision=diff_base,
                population="full",
                complete=True,
                global_invalidators=("declared-full-mode",),
                dependency_reasons=_declared_full_reasons(outputs),
                unresolved_inputs=(),
                outputs=outputs,
            )
        )
    base = resolve_base(root, explicit_base=diff_base)
    derived = targeted.derive_targeted_scope(root, base.revision)
    outputs = {
        "changed-files": _paths(derived.changed_paths),
        "lint-paths": _paths(derived.lint_paths),
        "type-closure": _paths(derived.type_paths),
        "coverage-paths": _paths(derived.coverage_paths),
        "coverage-roots": list(derived.coverage_root_modules),
        "dashboard-tests": dashboard_tests,
        "selected-tests": _paths(derived.test_paths),
        "size-paths": _paths(derived.lint_paths),
    }
    dashboard_invalidators = _dashboard_invalidators(derived.changed_paths)
    reasons = [
        *_scope_reasons(derived),
        *_dashboard_reasons(dashboard_tests),
        *dashboard_invalidators,
    ]
    unresolved = tuple(
        _unresolved_reason(reason) for reason in derived.test_impact.unresolved_inputs
    )
    return build_repository_selection_result(
        RepositorySelectionDraft(
            selector_id=SELECTOR_ID,
            selector_version=SELECTOR_VERSION,
            configuration_digest=ownership_configuration_digest(),
            candidate_identity=candidate,
            mode="targeted",
            base_revision=base.revision,
            population=(
                "targeted" if outputs["selected-tests"] or outputs["dashboard-tests"] else "empty"
            ),
            complete=derived.test_impact.complete,
            global_invalidators=(
                *(reason.render() for reason in derived.test_impact.global_invalidators),
                *(
                    f"{reason.kind}:{reason.input}:{reason.detail}"
                    for reason in dashboard_invalidators
                ),
            ),
            dependency_reasons=reasons,
            unresolved_inputs=unresolved,
            outputs=outputs,
        )
    )


def selection_payload(
    project_root: Path,
    *,
    mode: str,
    diff_base: str,
    candidate_identity: CandidateIdentity | None = None,
) -> dict[str, object]:
    """Return the canonical JSON-shaped selector result for rail comparison."""

    return selection_result(
        project_root,
        mode=mode,
        diff_base=diff_base,
        candidate_identity=candidate_identity,
    ).model_dump(mode="json")


def _candidate_identity(root: Path) -> CandidateIdentity:
    completed = git_command.run_git(root, ["write-tree"])
    if completed.returncode != 0:
        raise quality_scope.ScopeError(
            "repository selector could not derive the exact candidate tree: "
            f"{completed.stderr.strip()}"
        )
    tree = completed.stdout.strip()
    if len(tree) != 40 or any(character not in "0123456789abcdef" for character in tree):
        raise quality_scope.ScopeError("repository selector produced an invalid candidate tree")
    return CandidateIdentity(kind="git-tree", value=tree)


def _dashboard_tests(root: Path) -> list[str]:
    existing_roots = tuple(
        (relative, suffixes)
        for relative, suffixes in DASHBOARD_TEST_PATTERNS
        if (root / relative).is_dir()
    )
    if not existing_roots:
        return []
    return sorted(
        path.as_posix()
        for path in quality_scope.git_ls_files(
            root,
            *(relative.as_posix() for relative, _suffixes in existing_roots),
        )
        if any(
            path.is_relative_to(relative) and path.name.endswith(suffixes)
            for relative, suffixes in existing_roots
        )
    )


def _declared_full_reasons(
    outputs: dict[str, Sequence[str]],
) -> tuple[RepositorySelectionReason, ...]:
    return tuple(
        RepositorySelectionReason(
            input="profile://agents-remember/declared-full-mode",
            kind="declared-full-population",
            effect="select",
            outputArtifact=artifact,
            outputValue=value,
            detail="profile-authorized-full-population",
        )
        for artifact, values in sorted(outputs.items())
        for value in values
    )


def _scope_reasons(
    derived: targeted.TargetedScopeResult,
) -> tuple[RepositorySelectionReason, ...]:
    reasons: list[RepositorySelectionReason] = []
    for owned in derived.test_impact.ownership:
        reasons.extend(
            _selected_test_reason(reason, owned.path.as_posix()) for reason in owned.reasons
        )
    reasons.extend(
        _resolved_input_reason(reason)
        for reason in derived.test_impact.input_decisions
        if not _is_dashboard_path(reason.source)
    )
    reasons.extend(
        _global_invalidator_reason(reason) for reason in derived.test_impact.global_invalidators
    )
    reasons.extend(_path_output_reasons(derived.changed_paths, "changed-files", "changed-input"))
    reasons.extend(_path_output_reasons(derived.lint_paths, "lint-paths", "changed-python"))
    reasons.extend(_path_output_reasons(derived.lint_paths, "size-paths", "changed-python"))
    reasons.extend(
        _path_output_reasons(derived.type_paths, "type-closure", "reverse-import-closure")
    )
    reasons.extend(
        _path_output_reasons(derived.coverage_paths, "coverage-paths", "changed-production")
    )
    reasons.extend(
        RepositorySelectionReason(
            input="profile://agents-remember/python-coverage-roots",
            kind="configured-coverage-root",
            effect="select",
            outputArtifact="coverage-roots",
            outputValue=value,
            detail="configured-product-import-root",
        )
        for value in derived.coverage_root_modules
    )
    return tuple(reasons)


def _path_output_reasons(
    paths: Sequence[Path], artifact: str, detail: str
) -> tuple[RepositorySelectionReason, ...]:
    return tuple(
        RepositorySelectionReason(
            input=path.as_posix(),
            kind="derived-scope",
            effect="select",
            outputArtifact=artifact,
            outputValue=path.as_posix(),
            detail=detail,
        )
        for path in paths
    )


def _selected_test_reason(
    reason: SelectionReason,
    output: str,
) -> RepositorySelectionReason:
    return RepositorySelectionReason(
        input=reason.source.as_posix(),
        kind=reason.kind.value,
        effect="select",
        outputArtifact="selected-tests",
        outputValue=output,
        detail=reason.detail,
    )


def _resolved_input_reason(reason: SelectionReason) -> RepositorySelectionReason:
    return RepositorySelectionReason(
        input=reason.source.as_posix(),
        kind=reason.kind.value,
        effect="irrelevant",
        detail=reason.detail,
    )


def _global_invalidator_reason(reason: SelectionReason) -> RepositorySelectionReason:
    return RepositorySelectionReason(
        input=reason.source.as_posix(),
        kind=reason.kind.value,
        effect="global-invalidate",
        detail=reason.detail,
    )


def _unresolved_reason(reason: SelectionReason) -> RepositorySelectionReason:
    return RepositorySelectionReason(
        input=reason.source.as_posix(),
        kind=reason.kind.value,
        effect="unresolved",
        detail=reason.detail,
    )


def _dashboard_reasons(paths: Sequence[str]) -> tuple[RepositorySelectionReason, ...]:
    return tuple(
        RepositorySelectionReason(
            input="profile://agents-remember/rails/dashboard-suite",
            kind="declared-full-population",
            effect="select",
            outputArtifact="dashboard-tests",
            outputValue=path,
            detail="profile-declared-dashboard-suite-population",
        )
        for path in paths
    )


def _dashboard_invalidators(
    changed_paths: Sequence[Path],
) -> tuple[RepositorySelectionReason, ...]:
    return tuple(
        RepositorySelectionReason(
            input=path.as_posix(),
            kind="profile-declared-suite",
            effect="global-invalidate",
            detail="dashboard-input-selects-declared-dashboard-suite",
        )
        for path in changed_paths
        if _is_dashboard_path(path)
    )


def _is_dashboard_path(path: Path) -> bool:
    return bool(path.parts and path.parts[0] == "dashboard")


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


def _verify_admitted_identity(args: argparse.Namespace, root: Path) -> CandidateIdentity:
    expected = (SELECTOR_ID, SELECTOR_VERSION, ownership_configuration_digest())
    observed = (args.selector_id, args.selector_version, args.selector_configuration_digest)
    if observed != expected:
        raise quality_scope.ScopeError(
            "repository selector identity differs from its versioned ownership authority"
        )
    candidate = CandidateIdentity(kind=args.candidate_kind, value=args.candidate_value)
    if candidate != _candidate_identity(root):
        raise quality_scope.ScopeError("repository selector candidate differs from the Git index")
    return candidate


def main(argv: list[str] | None = None) -> int:
    try:
        require_dagger_admission(subject="repository certification selector")
        args = build_parser().parse_args(argv)
        root = args.project_root.resolve()
        candidate = _verify_admitted_identity(args, root)
        result = selection_result(
            root,
            mode=args.mode,
            diff_base=args.diff_base,
            candidate_identity=candidate,
        )
        output = _confined_output(root, args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output, result.model_dump_json(indent=2) + "\n")
    except (DaggerAdmissionError, OSError, RuntimeError, ValueError) as error:
        print(f"repository selector refused: {error}", file=sys.stderr, flush=True)
        return 1
    if result.complete:
        selected_tests = result.output_values()["selected-tests"]
        print(
            f"repository selector: {args.mode} complete; "
            f"{len(selected_tests)} selected test inputs; digest={result.selectionDigest}",
            flush=True,
        )
    else:
        print(
            "repository selector: test-selection-ownership-incomplete; "
            f"{len(result.unresolvedInputs)} unresolved inputs; digest={result.selectionDigest}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
