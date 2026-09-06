"""Coverage-derived quality rails run after pytest produces branch data."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from agents_remember_test_support.code_quality import (
    crap_calculator,
    diff_coverage,
    scope_reporting,
)
from agents_remember_test_support.code_quality.scope import GateScope

Printer = Callable[[str], None]


class CoverageRailConfig(Protocol):
    @property
    def scope(self) -> GateScope: ...

    @property
    def threshold(self) -> float: ...

    @property
    def top(self) -> int: ...

    @property
    def diff_base(self) -> str | None: ...

    @property
    def targeted(self) -> bool: ...


def run_crap_calculator(
    config: CoverageRailConfig,
    coverage_json: Path,
    project_root: Path,
    *,
    printer: Printer,
) -> int:
    printer("\n## CRAP-Calculator")
    if getattr(config, "targeted", False) and not config.scope.coverage_paths:
        printer(
            "not applicable: targeted run changed no production modules, so there are no "
            "changed functions for a CRAP report to score"
        )
        printer("result: CRAP-Calculator PASS (not applicable)")
        return 0
    if not coverage_json.exists():
        printer(
            scope_reporting.crap_scope_line(
                project_root / "pyproject.toml",
                coverage_json,
                0,
                config.threshold,
            )
        )
        printer(f"coverage JSON was not created: {coverage_json}")
        printer("result: CRAP-Calculator FAIL")
        return 1
    try:
        scores = crap_calculator.calculate_scores(
            config.scope.coverage_paths,
            coverage_json=coverage_json,
            project_root=project_root,
        )
    except RuntimeError as error:
        printer(str(error))
        printer("result: CRAP-Calculator FAIL")
        return 1
    printer(
        scope_reporting.crap_scope_line(
            project_root / "pyproject.toml",
            coverage_json,
            len(scores),
            config.threshold,
        )
    )
    if not scores:
        printer(
            "CRAP scored zero production functions. Check the declared product roots and "
            "coverage artifact; an empty diagnostic report cannot describe this scope."
        )
        printer("result: CRAP-Calculator FAIL")
        return 1
    printer(crap_calculator.render_table(scores, project_root, config.threshold, config.top))
    over_threshold = [score for score in scores if score.crap >= config.threshold]
    if not over_threshold:
        printer("result: CRAP-Calculator PASS")
        return 0
    printer(
        f"\n{len(over_threshold)} production function(s) meet or exceed the CRAP review "
        f"threshold {config.threshold:.1f}. Consider simpler code, a meaningful behavioral "
        "test, or a concise justified acceptance. Scores do not block delivery."
    )
    for score in over_threshold:
        printer(crap_failure_line(score, project_root, config.threshold))
    printer("result: CRAP-Calculator PASS (diagnostic review findings)")
    return 0


def crap_failure_line(
    score: crap_calculator.FunctionScore, project_root: Path, threshold: float
) -> str:
    """Locate a review finding without prescribing coverage-driven tests."""
    location = f"{crap_calculator.display_path(score.path, project_root)}:{score.start_line}"
    return (
        f"  {score.crap:6.2f}  cc {score.complexity:>3}  "
        f"branch {score.coverage_ratio * 100:5.1f}%  {location} {score.function} "
        f"-- review threshold {threshold:.1f}"
    )


def run_diff_coverage(
    config: CoverageRailConfig,
    coverage_json: Path,
    project_root: Path,
    *,
    printer: Printer,
) -> int:
    """Report changed-line coverage without imposing a delivery percentage."""
    printer("\n## diff-coverage")
    if (
        getattr(config, "targeted", False)
        and not coverage_json.exists()
        and not config.scope.coverage_paths
    ):
        printer(
            "not applicable: targeted run changed no production modules, so no coverage "
            "report was produced and there is no changed production line to score"
        )
        printer("result: diff-coverage PASS (not applicable)")
        return 0
    if not coverage_json.exists():
        printer(
            scope_reporting.scope_line(
                "diff-coverage",
                "changed Python diff intersected with missing Coverage.py JSON",
                f"coverage input={coverage_json.as_posix()}; diagnostic only",
                "0 measurable statements+branches",
            )
        )
        printer(f"coverage JSON was not created: {coverage_json}")
        printer("result: diff-coverage FAIL")
        return 1
    try:
        base = diff_coverage.resolve_base(project_root, explicit_base=config.diff_base)
        result = diff_coverage.measure(project_root, coverage_json, base)
    except (RuntimeError, OSError) as error:
        printer(str(error))
        printer("result: diff-coverage FAIL")
        return 1
    printer(scope_reporting.diff_scope_line(result, coverage_json))
    for line in diff_coverage.render(result):
        printer(line)
    if result.state == "measured":
        printer("result: diff-coverage PASS")
    else:
        printer(f"result: diff-coverage PASS (not applicable: {result.state})")
    return 0
