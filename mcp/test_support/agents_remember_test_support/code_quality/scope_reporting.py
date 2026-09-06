"""Render truthful scope, input, config, and unit provenance for quality rails."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agents_remember_test_support.code_quality.causal_preflight import preflight_scope_units
from agents_remember_test_support.code_quality.scope import (
    GateScope,
    ScopeError,
    coverage_json_file_count,
    dashboard_build_inputs,
    derive_scope,
    eslint_result_files,
    python_files_under,
    read_pyproject,
    toml_section,
    validate_quality_config,
)

if TYPE_CHECKING:
    from agents_remember_test_support.code_quality import diff_coverage, targeted

INVOCATION_ENV = "AR_QUALITY_INVOCATION"
PUSH_UPDATES_ENV = "AR_QUALITY_PUSH_UPDATES"
UNTRACKED_SAMPLE_LIMIT = 12
FIXED_STEP_SCOPE_NAMES = (
    "ruff",
    "ruff-format",
    "file-size",
    "layering",
    "pyright",
    "evidence-lifecycle",
    "causal-preflight",
    "radon-cc",
    "radon-mi",
    "pytest",
)


class ScopeReportingError(RuntimeError):
    """A provenance line could not truthfully describe its input."""


@dataclass(frozen=True)
class PushUpdate:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    def summary(self) -> str:
        local = "delete" if set(self.local_sha) == {"0"} else self.local_sha[:12]
        remote = "new" if set(self.remote_sha) == {"0"} else self.remote_sha[:12]
        return f"{self.local_ref}@{local}->{self.remote_ref}@{remote}"


def scope_line(name: str, looked_at: str, config: str, units: str) -> str:
    """The stable one-line output contract shared by wrapper, hooks, and CI."""
    return f"scope: {name} | input={looked_at} | config={config} | units={units}"


def parse_push_updates(raw: str) -> list[PushUpdate]:
    updates: list[PushUpdate] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 4:
            raise ScopeReportingError(
                f"pre-push update line {number} has {len(fields)} fields, expected 4"
            )
        updates.append(PushUpdate(*fields))
    return updates


# 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/test_support/agents_remember_test_support/code_quality/scope_reporting.py:72).
def validate_invocation_environment(
    environment: dict[str, str] | None = None,
) -> None:  # pragma: no cover
    env = os.environ if environment is None else environment
    if env.get(INVOCATION_ENV) != "pre-push":
        return
    updates = parse_push_updates(env.get(PUSH_UPDATES_ENV, ""))
    if not updates:
        raise ScopeReportingError(
            "pre-push received zero ref updates; run through Git's pre-push hook so it supplies "
            "the four-field ref-update stream, or invoke the full tier as a manual check without "
            "AR_QUALITY_INVOCATION=pre-push"
        )


def invocation_description(environment: dict[str, str] | None = None) -> str:
    env = os.environ if environment is None else environment
    invocation = env.get(INVOCATION_ENV, "manual")
    named = {
        "closeout-staged": (
            "closeout staged candidate; closeout synchronized index and working-tree bytes "
            "before this wrapper"
        ),
        "master-integration": "master integration tree; clean checkout at the commit about to land",
        "leaf-integration": "leaf integration tree; clean checkout at the leaf commit about to land",
        "ci": "CI checkout at HEAD; index and working-tree bytes are expected identical",
        "pre-commit-staged": "pre-commit staged/index candidate isolated into the working tree",
        "pre-commit-sequencer": (
            "pre-commit merge/rebase working tree; stash isolation is unsafe in this state"
        ),
    }
    if invocation in named:
        return named[invocation]
    if invocation == "pre-push":
        updates = parse_push_updates(env.get(PUSH_UPDATES_ENV, ""))
        summaries = ", ".join(update.summary() for update in updates)
        return (
            f"pre-push ref updates ({len(updates)}): {summaries}; fixed rails read current "
            "checkout bytes at index-known paths and do not mutate the index"
        )
    return "manual dirty tree; index-known paths read current working-tree bytes"


def pyright_config_description(project_root: Path, python_executable: str) -> str:
    try:
        _path, data = read_pyproject(project_root)
    except ScopeError:
        return (
            f"MISSING pyproject.toml [tool.pyright]; interpreter=--pythonpath {python_executable}"
        )
    pyright = toml_section(data, ("tool", "pyright"))
    venv_path = pyright.get("venvPath")
    venv = pyright.get("venv")
    if isinstance(venv_path, str) and isinstance(venv, str):
        resolved = (project_root / venv_path / venv).resolve()
        venv_state = f"venv={resolved.as_posix()}"
    else:
        venv_state = "no venv declaration"
    return (
        f"pyproject.toml [tool.pyright]; interpreter=--pythonpath {python_executable}; {venv_state}"
    )


def wrapper_scope_line(
    project_root: Path,
    scope: GateScope,
    environment: dict[str, str] | None = None,
    *,
    name: str = "quality-wrapper",
    targeted: bool = False,
) -> str:
    if targeted:
        name = "targeted-quality-wrapper"
    return scope_line(
        name,
        invocation_description(environment),
        f"{(project_root / 'pyproject.toml').name} validated quality sections",
        (
            f"{len(scope.lint_paths)} changed Python files; "
            f"{len(scope.type_paths)} pyright files (changed + reverse-import closure); "
            f"{len(scope.coverage_paths)} changed production modules; "
            f"{len(scope.test_paths)} derived test files; "
            f"{len(scope.size_paths)} size-scoped changed files"
            if targeted
            else f"{len(scope.lint_paths)} index-known Python files; "
            f"{len(scope.coverage_paths)} coverage roots; {len(scope.test_paths)} test roots; "
            f"{len(scope.size_paths)} size-scoped files"
        ),
    )


def fixed_step_scope_line(
    name: str,
    project_root: Path,
    scope: GateScope,
    *,
    python_executable: str = sys.executable,
    targeted: bool = False,
) -> str:
    production = python_files_under(project_root, scope.coverage_paths)
    tests = python_files_under(project_root, scope.test_paths)
    if name in {"ruff", "ruff-format"}:
        return scope_line(
            name,
            (
                "current checkout bytes at changed paths from git diff against the leaf base"
                if targeted
                else "current checkout bytes at paths enumerated by git ls-files '*.py'"
            ),
            "pyproject.toml [tool.ruff]",
            f"{len(scope.lint_paths)} changed Python files"
            if targeted
            else f"{len(scope.lint_paths)} index-known Python files",
        )
    if name == "pyright":
        return scope_line(
            name,
            (
                "current checkout bytes at changed paths plus the reverse-import closure"
                if targeted
                else "current checkout bytes at paths enumerated by git ls-files '*.py'"
            ),
            pyright_config_description(project_root, python_executable),
            f"{len(scope.type_paths)} files (changed + reverse-import closure)"
            if targeted
            else f"{len(scope.type_paths)} index-known Python files",
        )
    if name in {"evidence-lifecycle", "causal-preflight"}:
        return _evidence_scope_line(name)
    if name in {"radon-cc", "radon-mi"}:
        return scope_line(
            name,
            "on-disk Python files recursively consumed from product package roots",
            "pyproject.toml [tool.radon] plus explicit report thresholds",
            f"{len(production)} on-disk product Python files",
        )
    if name == "pytest":
        return scope_line(
            name,
            (
                "derived test subset covering changed modules"
                if targeted
                else "configured test roots on disk plus coverage over production package roots"
            ),
            "pyproject.toml [tool.pytest.ini_options] + [tool.coverage.run]",
            (
                f"{len(scope.test_paths)} derived test files; "
                f"{len(scope.coverage_paths)} changed production modules offered to Coverage.py"
                if targeted
                else f"{len(tests)} Python files present under test roots; "
                f"{len(production)} on-disk product Python files offered to Coverage.py"
            ),
        )
    if name in {"file-size", "layering"}:
        return _structural_scope_line(name, scope)
    raise ScopeReportingError(f"no scope contract is registered for wrapper step {name!r}")


def _evidence_scope_line(name: str) -> str:
    if name == "evidence-lifecycle":
        return scope_line(
            name,
            "repository-owned durable fixture and shared-support catalog",
            "mcp/tests/evidence-lifecycle.toml ar-test-evidence-lifecycle/v1",
            "1 complete catalog plus every governed fixture-data path",
        )
    return scope_line(
        name,
        "canonical high-fanout prerequisite owners plus graph-proven consumers",
        "code_quality.causal_preflight PREFLIGHTS + DependencyOwnershipGraph",
        preflight_scope_units(),
    )


def _structural_scope_line(name: str, scope: GateScope) -> str:
    if name == "file-size":
        return scope_line(
            name,
            "index-known Python files plus dashboard/src TypeScript files",
            "system/coding-guidelines.md File Size Budget (1,200 hard limit / 2,000 "
            "architectural failure); [tool.agents_remember] file_size_armed",
            f"{len(scope.size_paths)} size-scoped files",
        )
    return scope_line(
        name,
        "every Python module under mcp/src/agents_remember",
        "layers.toml strict package order (rank(imported) < rank(importer), no "
        "package-pair cycles, no stale present=false flags); armed with no baseline",
        "full tree",
    )


def targeted_scope_lines(
    base: diff_coverage.BaseResolution,
    result: targeted.TargetedScopeResult,
) -> list[str]:
    """The full derivation a targeted run prints, so the selection is reviewable."""
    changed = [path.as_posix() for path in result.changed_paths]
    closure = [path.as_posix() for path in result.reverse_import_closure]
    tests = [path.as_posix() for path in result.test_paths]
    ownership_reasons = sorted({reason.render() for reason in result.test_impact.reasons})
    lines = [
        scope_line(
            "targeted",
            "git diff --diff-filter=ACMRD against the leaf base plus non-ignored untracked paths",
            f"base: {base.revision} ({base.origin})",
            f"{len(changed)} changed repository paths",
        ),
        f"targeted changed files ({len(changed)}):",
        *(f"  {path}" for path in changed),
        (
            f"targeted reverse-import closure for pyright adds {len(closure)} file(s):"
            if closure
            else "targeted reverse-import closure: none"
        ),
        *(f"  {path}" for path in closure),
        f"targeted test subset ({len(tests)} file(s)):",
        *(f"  {path}" for path in tests),
        (
            "targeted ownership: complete"
            if result.test_impact.complete
            else "targeted ownership: incomplete; Gate 2 blocked without population expansion"
        ),
        f"targeted selection reasons ({len(ownership_reasons)}):",
        *(f"  {reason}" for reason in ownership_reasons),
    ]
    return lines


def coverage_result_scope_line(coverage_json: Path) -> str:
    records = coverage_json_file_count(coverage_json)
    return scope_line(
        "coverage-result",
        f"file records emitted by pytest-cov into {coverage_json.as_posix()}",
        "pyproject.toml [tool.coverage.run] + pytest --cov production roots",
        f"{records} Coverage.py file records",
    )


def randomized_pytest_scope_line(project_root: Path, seed: str) -> str:
    _path, data = read_pyproject(project_root)
    testpaths = toml_section(data, ("tool", "pytest", "ini_options")).get("testpaths")
    if not isinstance(testpaths, list) or not testpaths:
        raise ScopeReportingError("randomized pytest cannot resolve configured testpaths")
    roots = [Path(str(path)) for path in testpaths]
    tests = python_files_under(project_root, roots)
    if not tests:
        raise ScopeReportingError("randomized pytest resolves zero Python test files")
    return scope_line(
        "randomized-pytest",
        f"configured pytest roots on disk in deterministic randomized order; seed={seed}",
        "pyproject.toml [tool.pytest.ini_options] + --random-order-seed",
        f"{len(tests)} Python test files",
    )


def crap_scope_line(
    config_path: Path,
    coverage_json: Path,
    function_count: int,
    threshold: float,
) -> str:
    return scope_line(
        "CRAP-Calculator",
        f"production functions in coverage roots scored from {coverage_json.as_posix()}",
        f"{config_path.as_posix()} [tool.coverage.run]; review threshold={threshold:.1f}; "
        "scores are diagnostic only",
        f"{function_count} functions",
    )


def diff_input_description(
    base: diff_coverage.BaseResolution,
    environment: dict[str, str] | None = None,
) -> str:
    env = os.environ if environment is None else environment
    invocation = env.get(INVOCATION_ENV, "manual")
    if invocation == "closeout-staged":
        target = "staged candidate (working tree synchronized by closeout)"
    elif invocation == "ci":
        target = "CI checkout at HEAD"
    elif invocation == "master-integration":
        target = "master integration tree (clean checkout at the commit about to land)"
    elif invocation == "leaf-integration":
        target = "leaf integration tree (clean checkout at the leaf commit about to land)"
    elif invocation == "pre-push":
        target = "current checkout; pushed ref ranges are stated by the wrapper tier"
    else:
        target = "manual working tree"
    return (
        f"git diff --diff-filter=ACMR {base.revision} to {target}, intersected with "
        "Coverage.py JSON; untracked files excluded"
    )


def diff_scope_line(
    result: diff_coverage.DiffCoverage,
    coverage_json: Path,
    environment: dict[str, str] | None = None,
) -> str:
    return scope_line(
        "diff-coverage",
        diff_input_description(result.base, environment),
        f"coverage input={coverage_json.as_posix()}; diagnostic only",
        (
            f"{result.changed_files} changed Python files; "
            f"{result.total_units} measurable statements+branches"
        ),
    )


def untracked_scope_lines(
    scope: GateScope,
    *,
    sample_limit: int = UNTRACKED_SAMPLE_LIMIT,
) -> list[str]:
    roots = ", ".join(path.as_posix() for path in scope.scope_roots) or "<caller-supplied>"
    paths = scope.untracked_paths
    lines = [
        scope_line(
            "untracked-exposure",
            f"git ls-files --others --exclude-standard inside [{roots}]",
            "git ignore rules",
            f"{len(paths)} non-ignored untracked files",
        )
    ]
    if not paths:
        lines.append("untracked: 0 files are outside the index/diff measurement")
        return lines
    lines.append(
        f"untracked: {len(paths)} file(s) are NOT in this measurement "
        "(the wrapper's index/diff measurement; report only; no staging, stash, or index "
        "mutation):"
    )
    shown = paths[:sample_limit]
    lines.extend(f"  {path.as_posix()}" for path in shown)
    remainder = len(paths) - len(shown)
    if remainder:
        lines.append(f"  ... {remainder} more untracked file(s) not shown")
    return lines


def generated_scope_line(project_root: Path, name: str, script: Path) -> str:
    command = [sys.executable, script.as_posix(), "--list-targets"]
    completed = subprocess.run(
        command,
        cwd=project_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if completed.returncode != 0:
        raise ScopeReportingError(
            f"{script.as_posix()} --list-targets failed with exit {completed.returncode}: "
            f"{completed.stdout.strip()}"
        )
    targets = [
        line for line in completed.stdout.splitlines() if line and not line.startswith("canonical:")
    ]
    if not targets:
        raise ScopeReportingError(f"{script.as_posix()} declared zero generated targets")
    return scope_line(
        f"generated-{name}",
        f"source-to-target mappings emitted by {script.as_posix()} --list-targets",
        f"{script.as_posix()} --check",
        f"{len(targets)} generated targets",
    )


FRONTEND_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})


def frontend_files(dashboard: Path) -> list[Path]:
    ignored = frozenset({"node_modules", "dist", "coverage", ".cache"})
    return sorted(
        path
        for path in dashboard.rglob("*")
        if path.is_file()
        and path.suffix in FRONTEND_SUFFIXES
        and not any(part in ignored for part in path.relative_to(dashboard).parts)
    )


def read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScopeReportingError(f"could not read {label} {path}: {error}") from error
    if not isinstance(data, dict):
        raise ScopeReportingError(f"{label} {path} is not a JSON object; correct its shape")
    return data


def tsconfig_project_inputs(
    dashboard: Path,
    reference: object,
    index: int,
) -> tuple[set[Path], str | None]:
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        return set(), f"reference {index} is malformed; give it one string path to a tsconfig"
    project = (dashboard / reference["path"]).with_suffix(".json")
    if not project.is_file():
        project = dashboard / reference["path"]
    if project.is_dir():
        project = project / "tsconfig.json"
    if not project.is_file():
        return (
            set(),
            f"referenced project {project} is missing; restore it or remove the stale reference",
        )
    try:
        project_data = read_json_object(project, "TypeScript project")
    except ScopeReportingError as error:
        return set(), str(error)
    inputs = config_input_files(project.parent, project_data)
    if not inputs:
        return (
            set(),
            f"TypeScript project {project} resolves zero input files; add its files/include "
            "inputs or remove the inert project",
        )
    return inputs, None


def tsconfig_inputs(dashboard: Path) -> tuple[int, int]:
    root_config = dashboard / "tsconfig.json"
    data = read_json_object(root_config, "TypeScript root config")
    references = data.get("references")
    if not isinstance(references, list) or not references:
        raise ScopeReportingError(f"{root_config} declares zero TypeScript projects")
    inputs: set[Path] = set()
    findings: list[str] = []
    valid_projects = 0
    for index, reference in enumerate(references):
        project_inputs, finding = tsconfig_project_inputs(dashboard, reference, index)
        if finding is not None:
            findings.append(finding)
            continue
        valid_projects += 1
        inputs.update(project_inputs)
    if findings:
        rendered = "\n".join(f"  - {finding}" for finding in findings)
        raise ScopeReportingError(
            f"TypeScript project scope has {len(findings)} finding(s):\n{rendered}"
        )
    return valid_projects, len(inputs)


def config_input_files(dashboard: Path, config: dict[str, object]) -> set[Path]:
    entries: list[str] = []
    for key in ("files", "include"):
        value = config.get(key)
        if isinstance(value, list):
            entries.extend(str(item) for item in value)
    found: set[Path] = set()
    for entry in entries:
        candidate = dashboard / entry
        if candidate.is_file():
            found.add(candidate)
            continue
        if candidate.is_dir():
            found.update(path for path in candidate.rglob("*") if path.suffix in {".ts", ".tsx"})
            continue
        found.update(
            path
            for path in dashboard.glob(entry)
            if path.is_file() and path.suffix in {".ts", ".tsx"}
        )
    return found


def dashboard_lint_scope_line(dashboard: Path) -> str:
    config = dashboard / "eslint.config.js"
    findings = []
    if not config.is_file():
        findings.append("dashboard/eslint.config.js is missing; restore the lint config")
    if findings:
        raise ScopeReportingError("; ".join(findings))
    try:
        resolved = eslint_result_files(dashboard)
    except ScopeError as error:
        raise ScopeReportingError(str(error)) from error
    return scope_line(
        "dashboard-lint",
        "ESLint machine-readable result set after flat-config ignores",
        "dashboard/eslint.config.js + dashboard/package.json script lint (eslint .)",
        f"{len(resolved)} ESLint-resolved files",
    )


def dashboard_test_scope_line(dashboard: Path, all_frontend: list[Path]) -> str:
    tests = [
        path
        for path in all_frontend
        if path.is_relative_to(dashboard / "src")
        and (".test." in path.name or ".spec." in path.name)
    ]
    findings = []
    if not (dashboard / "vitest.config.ts").is_file():
        findings.append("dashboard/vitest.config.ts is missing; restore the unit-test config")
    if not tests:
        findings.append("dashboard unit tests resolve zero test files; add tests or fix the scope")
    if findings:
        raise ScopeReportingError("; ".join(findings))
    return scope_line(
        "dashboard-test",
        "unit-test files under dashboard/src",
        "dashboard/vitest.config.ts + dashboard/package.json script test",
        f"{len(tests)} test files",
    )


def dashboard_typecheck_scope_line(projects: int, inputs: int) -> str:
    return scope_line(
        "dashboard-typecheck",
        "TypeScript project references and their resolved input files",
        "dashboard/tsconfig.json + dashboard/package.json script typecheck",
        f"{projects} projects; {inputs} TypeScript inputs",
    )


def dashboard_build_scope_line(
    dashboard: Path,
    projects: int,
    inputs: int,
) -> str:
    try:
        resolved = dashboard_build_inputs(dashboard)
    except ScopeError as error:
        raise ScopeReportingError(str(error)) from error
    panda = ", ".join(resolved.panda_include)
    vite = ", ".join(path.relative_to(dashboard).as_posix() for path in resolved.vite_inputs)
    return scope_line(
        "dashboard-build",
        (
            f"Panda include [{panda}]; TypeScript project inputs; "
            f"Vite BUILD_INPUT_FILES [{vite}]; bundled module graph intentionally uncounted"
        ),
        (
            "dashboard/package.json build='panda codegen && tsc -b && vite build' + "
            "panda.config.ts + tsconfig.json + vite.config.ts"
        ),
        (
            f"{len(resolved.panda_include)} Panda source glob; {projects} TypeScript projects; "
            f"{inputs} TypeScript inputs; {len(resolved.vite_inputs)} explicit Vite inputs"
        ),
    )


def dashboard_scope_line(project_root: Path, step: str) -> str:
    dashboard = project_root / "dashboard"
    package_json = dashboard / "package.json"
    package = read_json_object(package_json, "dashboard package")
    scripts = package.get("scripts")
    script_step = {"coverage": "test:coverage", "diff-coverage": "coverage:diff"}.get(step, step)
    if not isinstance(scripts, dict) or script_step not in scripts:
        raise ScopeReportingError(
            f"dashboard package.json has no {script_step!r} script; restore the ordinary project command"
        )
    if step == "lint":
        return dashboard_lint_scope_line(dashboard)
    if step == "test":
        return dashboard_test_scope_line(dashboard, frontend_files(dashboard))
    if step in ("coverage", "diff-coverage"):
        all_frontend = frontend_files(dashboard)
        return scope_line(
            f"dashboard-{step}",
            (
                "Vitest v8 coverage over the dashboard source tree; "
                "diff-coverage intersects the changed-lines set with the coverage JSON"
            ),
            (
                "dashboard/package.json test:coverage / coverage:diff + "
                "vitest.config.ts coverage block"
            ),
            f"units={len(all_frontend)} frontend source files",
        )
    if step == "e2e":
        return scope_line(
            "dashboard-e2e",
            "Playwright primary config against the built dashboard (/dev/bench fixture gallery)",
            "dashboard/package.json e2e='playwright test' + playwright.config.ts",
            "1 Playwright config; built dashboard via 'npm run build'",
        )
    projects, inputs = tsconfig_inputs(dashboard)
    if step == "typecheck":
        return dashboard_typecheck_scope_line(projects, inputs)
    if step == "build":
        return dashboard_build_scope_line(dashboard, projects, inputs)
    raise ScopeReportingError(f"unsupported dashboard quality step {step!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)
    tier = subparsers.add_parser("hook-tier")
    tier.add_argument("--tier", choices=("fast", "targeted", "full"), required=True)
    fixed = subparsers.add_parser("fixed-step")
    fixed.add_argument(
        "--name",
        choices=FIXED_STEP_SCOPE_NAMES,
        required=True,
    )
    generated = subparsers.add_parser("generated")
    generated.add_argument("--name", required=True)
    generated.add_argument("--script", type=Path, required=True)
    dashboard = subparsers.add_parser("dashboard")
    dashboard.add_argument(
        "--step",
        choices=("lint", "typecheck", "test", "build", "coverage", "diff-coverage", "e2e"),
        required=True,
    )
    randomized = subparsers.add_parser("randomized-pytest")
    randomized.add_argument("--seed", required=True)
    subparsers.add_parser("untracked")
    return parser


def _generated_report(args: argparse.Namespace, project_root: Path) -> int:
    print(generated_scope_line(project_root, args.name, args.script))
    return 0


def _dashboard_report(args: argparse.Namespace, project_root: Path) -> int:
    print(dashboard_scope_line(project_root, args.step))
    return 0


def _randomized_report(args: argparse.Namespace, project_root: Path) -> int:
    print(randomized_pytest_scope_line(project_root, args.seed))
    return 0


def _quality_report(args: argparse.Namespace, project_root: Path) -> int:
    validate_quality_config(project_root)
    validate_invocation_environment()
    scope = derive_scope(project_root)
    if args.command == "untracked":
        print("\n".join(untracked_scope_lines(scope)))
        return 0
    if args.command == "hook-tier":
        print(wrapper_scope_line(project_root, scope, name=f"{args.tier}-tier"))
    else:
        print(fixed_step_scope_line(args.name, project_root, scope))
    return 0


_SIMPLE_COMMANDS: dict[str, Callable[[argparse.Namespace, Path], int]] = {
    "generated": _generated_report,
    "dashboard": _dashboard_report,
    "randomized-pytest": _randomized_report,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    try:
        handler = _SIMPLE_COMMANDS.get(args.command)
        if handler is not None:
            return handler(args, project_root)
        return _quality_report(args, project_root)
    except (ScopeError, ScopeReportingError) as error:
        print(f"scope reporting failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
