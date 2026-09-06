"""Metric observations remain useful without demanding metric-driven tests."""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from agents_remember_test_support.code_quality import crap_calculator, diff_coverage, post_coverage
from agents_remember_test_support.code_quality.scope import GateScope


@dataclass
class DiagnosticConfig:
    scope: GateScope
    threshold: float = 20.0
    top: int = 10
    diff_base: str | None = "abc"
    targeted: bool = False


def test_low_coverage_is_reported_without_failing_delivery(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.json"
    coverage.write_text("{}")
    result = diff_coverage.DiffCoverage(
        base=diff_coverage.BaseResolution("abc", "explicit"),
        state="measured",
        covered_units=0,
        total_units=2,
        uncovered_lines=("module.py:1",),
        untaken_branches=("module.py:1->2",),
        unmeasured_files=(),
        changed_files=1,
    )
    config = DiagnosticConfig(GateScope([], [], [], []))
    output: list[str] = []
    with (
        patch.object(diff_coverage, "resolve_base", return_value=result.base),
        patch.object(
            diff_coverage,
            "measure",
            return_value=result,
        ),
    ):
        assert (
            post_coverage.run_diff_coverage(config, coverage, tmp_path, printer=output.append) == 0
        )
    report = "\n".join(output)
    assert "0/2 units = 0.00%" in report
    assert "module.py:1" in report
    assert "diagnostic only" in report


def test_high_crap_requests_review_without_failing_delivery(tmp_path: Path) -> None:
    coverage = tmp_path / "coverage.json"
    coverage.write_text("{}")
    source = tmp_path / "module.py"
    score = crap_calculator.FunctionScore(
        source, "operation", "function", 1, 8, 8, 0, 8, 8, 0, 2, 0.0, 72.0
    )
    config = DiagnosticConfig(
        scope=GateScope([], [], [source], []),
        threshold=20.0,
        top=10,
        targeted=False,
    )
    output: list[str] = []
    with patch.object(crap_calculator, "calculate_scores", return_value=[score]):
        assert (
            post_coverage.run_crap_calculator(config, coverage, tmp_path, printer=output.append)
            == 0
        )
    report = "\n".join(output)
    assert "72.00" in report
    assert "review threshold 20.0" in report
    assert "justified acceptance" in report
    assert "needs branch coverage" not in report
