"""Typed configuration and deterministic subprocess plan for the quality wrapper."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agents_remember.kernel.atomic_write import atomic_write_text

from agents_remember_test_support.code_quality import diff_coverage, retry_proof, targeted
from agents_remember_test_support.code_quality import scope as quality_scope
from agents_remember_test_support.testing.dagger_admission import (
    DaggerAdmission,
    require_dagger_admission_capability,
)
from agents_remember_test_support.testing.evidence_lanes import (
    EvidenceTrigger,
    expression_for,
)

GateScope = quality_scope.GateScope
ScopeError = quality_scope.ScopeError
RADON_REPORT_NOTE = (
    "report only: radon exits 0 whatever it finds, so nothing below can fail the gate"
)


def git_ls_files(project_root: Path, *patterns: str) -> list[Path]:
    return quality_scope.git_ls_files(project_root, *patterns)


def top_level_packages(tracked: list[Path]) -> list[Path]:
    return quality_scope.top_level_packages(tracked)


def toml_section(data: Mapping[str, object], keys: tuple[str, ...]) -> Mapping[str, object]:
    return quality_scope.toml_section(data, keys)


def pytest_testpaths(project_root: Path) -> list[Path]:
    return quality_scope.pytest_testpaths(project_root)


def derive_scope(project_root: Path) -> GateScope:
    return quality_scope.derive_scope(project_root)


def _quality_stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class QualityProgress:
    """One atomic, self-overwriting view of the wrapper's current rail."""

    path: Path | None
    started_at: str
    completed: list[str]

    @classmethod
    def start(cls, path: Path | None) -> QualityProgress:
        progress = cls(path=path, started_at=_quality_stamp(), completed=[])
        progress.write(status="running", step="scope", detail="derive quality scope")
        return progress

    def write(self, *, status: str, step: str, detail: str) -> None:
        if self.path is None:
            return
        atomic_write_text(
            self.path,
            json.dumps(
                {
                    "status": status,
                    "step": step,
                    "detail": detail,
                    "startedAt": self.started_at,
                    "updatedAt": _quality_stamp(),
                    "completedSteps": self.completed,
                },
                indent=2,
            )
            + "\n",
        )

    def finish_step(self, step: str, *, passed: bool) -> None:
        if passed and step not in self.completed:
            self.completed.append(step)
        self.write(
            status="running" if passed else "failed",
            step=step,
            detail="passed" if passed else "failed",
        )


@dataclass(frozen=True)
class CheckConfig:
    """Complete immutable input to one quality-wrapper run."""

    project_root: Path
    scope: GateScope
    admission: DaggerAdmission
    coverage_json: Path | None
    threshold: float
    top: int
    diff_base: str | None = None
    diff_floor: float = diff_coverage.DEFAULT_DIFF_COVERAGE_FLOOR
    targeted: bool = False
    targeted_base: diff_coverage.BaseResolution | None = None
    targeted_scope: targeted.TargetedScopeResult | None = None
    file_size_armed: bool = False
    pytest_report_log: Path | None = None
    pytest_phase_report: Path | None = None
    causal_failure_report: Path | None = None
    coverage_data: Path | None = None
    progress_report: Path | None = None
    progress: QualityProgress | None = None
    selection_digest: str | None = None


@dataclass(frozen=True)
class Step:
    """One enforcing or explicitly report-only subprocess step."""

    name: str
    command: list[str]
    report_note: str | None = None

    @property
    def enforcing(self) -> bool:
        return self.report_note is None


def quality_steps(
    config: CheckConfig,
    coverage_json: Path,
    *,
    retry_plan: retry_proof.RetryPlan | None = None,
) -> list[Step]:
    """Build the ordered rail plan without executing it."""

    scope = config.scope
    if config.targeted and config.targeted_scope is not None:
        coverage_args = list(config.targeted_scope.coverage_root_modules)
    else:
        coverage_args = posix_args(scope.coverage_paths)
    radon_args = posix_args(scope.coverage_paths)
    steps = _fixed_steps(
        config,
        posix_args(scope.lint_paths),
        posix_args(scope.type_paths),
    )
    steps.insert(2, _file_size_step(config, posix_args(scope.size_paths)))
    steps.insert(3, _layering_step(config))
    if not config.targeted or scope.coverage_paths:
        steps += _radon_report_steps(radon_args)
    pytest = _pytest_step(
        config,
        coverage_json,
        posix_args(scope.test_paths),
        coverage_args,
        retry_plan,
    )
    if pytest is not None:
        steps.append(pytest)
    return steps


def _fixed_steps(
    config: CheckConfig,
    lint_args: list[str],
    type_args: list[str],
) -> list[Step]:
    steps = [
        Step("ruff", [sys.executable, "-m", "ruff", "check", *lint_args]),
        Step("ruff-format", [sys.executable, "-m", "ruff", "format", "--check", *lint_args]),
        Step(
            "pyright",
            [
                sys.executable,
                "-m",
                "pyright",
                "--project",
                ".",
                "--pythonpath",
                sys.executable,
                *type_args,
            ],
        ),
        Step(
            "evidence-lifecycle",
            [
                sys.executable,
                "-m",
                "agents_remember_test_support.testing.evidence_lifecycle",
                "--project-root",
                ".",
            ],
        ),
    ]
    if config.causal_failure_report is not None:
        steps.append(
            Step(
                "causal-preflight",
                [
                    sys.executable,
                    "-m",
                    "agents_remember_test_support.code_quality.causal_preflight",
                    "--project-root",
                    ".",
                    "--report",
                    config.causal_failure_report.as_posix(),
                ],
            )
        )
    return steps


def _radon_report_steps(radon_args: list[str]) -> list[Step]:
    return [
        Step(
            "radon-cc",
            [
                sys.executable,
                "-m",
                "radon",
                "cc",
                *radon_args,
                "-s",
                "-n",
                "B",
                "--order",
                "SCORE",
            ],
            report_note=RADON_REPORT_NOTE,
        ),
        Step(
            "radon-mi",
            [sys.executable, "-m", "radon", "mi", *radon_args, "-s", "-n", "B"],
            report_note=RADON_REPORT_NOTE,
        ),
    ]


def _pytest_step(
    config: CheckConfig,
    coverage_json: Path,
    test_args: list[str],
    coverage_args: list[str],
    retry_plan: retry_proof.RetryPlan | None,
) -> Step | None:
    require_dagger_admission_capability(config.admission)
    if config.targeted and not config.scope.test_paths:
        return None
    pytest_args = [sys.executable, "-m", "pytest", *test_args]
    marker_expression = expression_for(
        EvidenceTrigger.AFFECTED if config.targeted else EvidenceTrigger.RELEASE
    )
    if marker_expression is not None:
        pytest_args += ["-m", marker_expression]
    if config.pytest_report_log is not None:
        pytest_args.append(f"--report-log={config.pytest_report_log.as_posix()}")
    if config.pytest_phase_report is not None:
        pytest_args += [
            "-p",
            "agents_remember_test_support.testing.pytest_phase_reporter",
            "--ar-pytest-phase-report",
            config.pytest_phase_report.as_posix(),
        ]
    if config.causal_failure_report is not None:
        pytest_args += ["--ar-causal-failure-report", config.causal_failure_report.as_posix()]
    if retry_plan is not None and retry_plan.delta:
        pytest_args += [
            "-p",
            "agents_remember_test_support.testing.retry_selection",
            *(f"--ar-retry-execute-path={path.as_posix()}" for path in retry_plan.delta_tests),
        ]
    if config.scope.coverage_paths:
        pytest_args += [
            *(f"--cov={module}" for module in coverage_args),
            f"--cov-report=json:{coverage_json.as_posix()}",
            "--cov-report=term",
            *(["--cov-context=test"] if retry_plan is not None else []),
        ]
    return Step("pytest", pytest_args)


def _file_size_step(config: CheckConfig, size_args: list[str]) -> Step:
    return Step(
        "file-size",
        [
            sys.executable,
            "-m",
            "agents_remember_test_support.code_quality.file_size",
            "--project-root",
            str(config.project_root),
            *size_args,
            *(["--report"] if not config.file_size_armed else []),
        ],
    )


def _layering_step(config: CheckConfig) -> Step:
    return Step(
        "layering",
        [
            sys.executable,
            "-m",
            "agents_remember_test_support.code_quality.layering",
            "--project-root",
            str(config.project_root),
        ],
    )


def posix_args(paths: list[Path]) -> list[str]:
    return [path.as_posix() for path in paths]
