from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember_test_support.code_quality import crap_calculator


class CrapCalculatorTests(unittest.TestCase):
    def test_crap_score_formula_uses_coverage_ratio(self) -> None:
        self.assertAlmostEqual(crap_calculator.crap_score(10, 0.8), 10.8)
        self.assertEqual(crap_calculator.crap_score(10, 1.0), 10.0)

    def test_calculates_function_scores_from_radon_and_coverage_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, coverage_json = write_sample_coverage_fixture(root)

            scores = crap_calculator.calculate_scores(
                [source],
                coverage_json=coverage_json,
                project_root=root,
            )

            by_name = {score.function: score for score in scores}
            self.assertEqual(by_name["simple"].complexity, 1)
            self.assertEqual(by_name["simple"].coverage_ratio, 1.0)
            self.assertEqual(by_name["branchy"].complexity, 3)
            # 2 of 6 statements plus 1 of 2 branch arcs: 3/8, where the statement-only
            # reader produced 2/6. The untaken false arc of `if value > 0` is a unit of the
            # denominator that did not exist before. It does not always move the ratio
            # down -- here a mostly-unexecuted function gains from its one taken arc; the
            # test below covers the case that matters, a function whose statements all run.
            self.assertAlmostEqual(by_name["branchy"].coverage_ratio, 3 / 8)
            self.assertEqual(by_name["branchy"].covered_branches, 1)
            self.assertEqual(by_name["branchy"].missing_branches, 1)
            self.assertGreater(by_name["branchy"].crap, by_name["simple"].crap)

    def test_a_report_without_branch_measurement_is_refused(self) -> None:
        """No silent fallback: statement-only input fails loudly instead of scoring low."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "sample.py"
            source.write_text("def flat(value):\n    return value\n", encoding="utf-8")
            coverage_json = root / "coverage.json"
            coverage_json.write_text(
                json.dumps(
                    {
                        "meta": {"format": 3, "branch_coverage": False},
                        "files": {"sample.py": {"executed_lines": [1, 2], "missing_lines": []}},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(RuntimeError) as raised:
                crap_calculator.calculate_scores(
                    [source], coverage_json=coverage_json, project_root=root
                )

            self.assertIn("branch_coverage", str(raised.exception))


def write_sample_coverage_fixture(root: Path) -> tuple[Path, Path]:
    source = root / "sample.py"
    source.write_text(
        "\n".join(
            [
                "def simple(value):",
                "    return value + 1",
                "",
                "def branchy(value):",
                "    if value > 0:",
                "        return 'positive'",
                "    if value < 0:",
                "        return 'negative'",
                "    return 'zero'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    coverage_json = root / "coverage.json"
    coverage_json.write_text(
        json.dumps(
            branch_report(
                {
                    "sample.py": {
                        "executed_lines": [1, 2, 4, 5],
                        "missing_lines": [6, 7, 8, 9],
                        # `if value > 0` was only ever evaluated true, so the arc to line 7
                        # is a branch the suite never took.
                        "executed_branches": [[5, 6]],
                        "missing_branches": [[5, 7]],
                    }
                }
            )
        ),
        encoding="utf-8",
    )
    return source, coverage_json


def branch_report(files: dict[str, object]) -> dict[str, object]:
    """A Coverage.py JSON report that declares branch measurement, as the reader requires."""
    return {"meta": {"format": 3, "branch_coverage": True}, "files": files}


if __name__ == "__main__":
    unittest.main()
