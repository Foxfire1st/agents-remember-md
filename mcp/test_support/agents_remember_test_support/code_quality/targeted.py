"""Dependency-owned change-set scope for leaf-edge quality gates."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from agents_remember.kernel import git_command

from agents_remember_test_support.code_quality.dependency_ownership import (
    DependencyOwnershipGraph,
    TestImpact,
    coverage_root_modules,
    reverse_import_closure,
)
from agents_remember_test_support.code_quality.scope import (
    GateScope,
    ScopeError,
    configured_package_authority,
)
from agents_remember_test_support.testing.dependency_facts import import_roots_for, within_any


@dataclass(frozen=True)
class TargetedScopeResult:
    """Concrete rail scope plus the auditable test-consumer decision."""

    base_revision: str
    changed_paths: tuple[Path, ...]
    lint_paths: tuple[Path, ...]
    type_paths: tuple[Path, ...]
    coverage_paths: tuple[Path, ...]
    coverage_root_modules: tuple[str, ...]
    test_paths: tuple[Path, ...]
    reverse_import_closure: tuple[Path, ...]
    test_impact: TestImpact

    def to_gate_scope(self, full_scope: GateScope) -> GateScope:
        return GateScope(
            lint_paths=list(self.lint_paths),
            type_paths=list(self.type_paths),
            coverage_paths=list(self.coverage_paths),
            test_paths=list(self.test_paths),
            size_paths=list(self.lint_paths),
            scope_roots=full_scope.scope_roots,
            untracked_paths=full_scope.untracked_paths,
        )


def _git(project_root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return git_command.run_git(project_root, ["-c", "core.quotePath=false", *arguments])
    except (OSError, subprocess.SubprocessError) as error:
        raise ScopeError(
            f"targeted scope could not run git (git {' '.join(arguments)}): {error}"
        ) from error


def changed_paths(project_root: Path, base_revision: str) -> list[Path]:
    """All current and deleted paths in the complete base-to-working-tree delta."""

    completed = _git(
        project_root,
        [
            "diff",
            "-z",
            "--name-only",
            "--diff-filter=ACMRD",
            base_revision,
            "--",
        ],
    )
    if completed.returncode != 0:
        raise ScopeError(
            "targeted scope could not diff the change set against "
            f"{base_revision}: exit {completed.returncode}: {completed.stderr.strip()}"
        )
    changed = {Path(entry) for entry in completed.stdout.split("\0") if entry}
    untracked = _git(
        project_root,
        ["ls-files", "-z", "--others", "--exclude-standard", "--"],
    )
    if untracked.returncode != 0:
        raise ScopeError(
            "targeted scope could not enumerate untracked candidate paths: "
            f"exit {untracked.returncode}: {untracked.stderr.strip()}"
        )
    changed.update(Path(entry) for entry in untracked.stdout.split("\0") if entry)
    return sorted(changed, key=Path.as_posix)


def changed_python_paths(project_root: Path, base_revision: str) -> list[Path]:
    """Current Python files in the complete candidate delta."""

    return [
        path
        for path in changed_paths(project_root, base_revision)
        if path.suffix == ".py" and (project_root / path).is_file()
    ]


def derive_targeted_scope(project_root: Path, base_revision: str) -> TargetedScopeResult:
    """Derive static rails and affected tests from the one ownership graph."""

    root = project_root.resolve()
    graph = DependencyOwnershipGraph(root)
    changed = changed_paths(root, base_revision)
    changed_python = tuple(
        path for path in changed if path.suffix == ".py" and (root / path).is_file()
    )
    changed_set = set(changed_python)
    changed_modules = sorted(
        {graph.modules[path] for path in changed_python if path in graph.modules}
    )
    closure = reverse_import_closure(
        changed_set,
        changed_modules,
        graph.modules,
        graph.importers,
    )
    impact = graph.resolve(changed, base_revision=base_revision)
    package_authority = configured_package_authority(root, list(graph.tracked))
    product_python = tuple(
        path for path in graph.python_paths if within_any(path, package_authority.product)
    )
    product_import_roots = import_roots_for(product_python, ())
    coverage_paths = tuple(
        sorted(
            (path for path in changed_python if within_any(path, package_authority.product)),
            key=Path.as_posix,
        )
    )
    return TargetedScopeResult(
        base_revision=base_revision,
        changed_paths=tuple(changed),
        lint_paths=changed_python,
        type_paths=tuple(sorted(closure, key=Path.as_posix)),
        coverage_paths=coverage_paths,
        coverage_root_modules=coverage_root_modules(
            product_python,
            product_import_roots,
        ),
        test_paths=impact.tests,
        reverse_import_closure=tuple(sorted(closure - changed_set, key=Path.as_posix)),
        test_impact=impact,
    )
