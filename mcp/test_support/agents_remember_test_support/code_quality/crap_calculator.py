"""CRAP-Calculator: combine function complexity with branch coverage data.

``crap = cc**2 * (1 - coverage)**3 + cc`` is defined over *branch* coverage, and the
coverage term is the only thing a test can move. This module reads the branch fields
Coverage.py emits under ``[tool.coverage.run] branch = true`` -- ``executed_branches``
and ``missing_branches`` -- and refuses to score a report produced without them, so the
formula cannot quietly be fed the statement coverage it is not defined over.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IGNORED_PATH_PARTS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
# A score at or above 20 fails without a baseline or exemption. Cover missing branch
# arcs or split the function. Known measurement boundary: Radon counts boolean
# short-circuits in complexity while Coverage.py may emit no corresponding arc, so a
# score can sometimes be reduced only by simplifying the function.
DEFAULT_CRAP_THRESHOLD = 20.0
DEFAULT_TOP = 25


@dataclass(frozen=True)
class FileCoverage:
    """One file's coverage, statements and branch arcs alike.

    A branch arc is Coverage.py's ``[source_line, destination_line]`` pair. Arcs are
    attributed to a function by their *source* line -- the branching statement -- because
    a destination is frequently outside the span (a ``return`` arc leaves the function,
    and Coverage.py writes the exit destination as a negative number).
    """

    executed_lines: frozenset[int]
    missing_lines: frozenset[int]
    executed_branches: frozenset[tuple[int, int]]
    missing_branches: frozenset[tuple[int, int]]
    has_data: bool = True

    @property
    def executable_lines(self) -> frozenset[int]:
        return self.executed_lines | self.missing_lines


@dataclass(frozen=True)
class FunctionScore:
    path: Path
    function: str
    kind: str
    start_line: int
    end_line: int
    complexity: int
    covered_lines: int
    missing_lines: int
    executable_lines: int
    covered_branches: int
    missing_branches: int
    coverage_ratio: float
    crap: float
    missing_coverage_data: bool = False


@dataclass(frozen=True)
class FileRollup:
    path: Path
    function_count: int
    max_crap: float
    average_crap: float
    over_threshold: int


def crap_score(complexity: int, coverage_ratio: float) -> float:
    """CRAP for one function. ``coverage_ratio`` is branch coverage, never statements."""
    bounded_coverage = max(0.0, min(coverage_ratio, 1.0))
    return complexity**2 * (1.0 - bounded_coverage) ** 3 + complexity


def read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return data


def load_coverage_by_path(coverage_json: Path, project_root: Path) -> dict[str, FileCoverage]:
    data = read_json(coverage_json)
    require_branch_measurement(data, coverage_json)
    files = data.get("files")
    if not isinstance(files, dict):
        raise RuntimeError(f"coverage JSON is missing a files object: {coverage_json}")

    coverage_by_path: dict[str, FileCoverage] = {}
    for raw_path, raw_data in files.items():
        if not isinstance(raw_path, str) or not isinstance(raw_data, dict):
            continue
        coverage = FileCoverage(
            executed_lines=frozenset(parse_line_numbers(raw_data.get("executed_lines", []))),
            missing_lines=frozenset(parse_line_numbers(raw_data.get("missing_lines", []))),
            executed_branches=frozenset(parse_branch_arcs(raw_data.get("executed_branches"))),
            missing_branches=frozenset(parse_branch_arcs(raw_data.get("missing_branches"))),
        )
        for key in coverage_keys(Path(raw_path), project_root):
            coverage_by_path[key] = coverage
    return coverage_by_path


def require_branch_measurement(data: dict[str, Any], coverage_json: Path) -> None:
    """Refuse coverage reports that do not carry branch measurement."""
    meta = data.get("meta")
    branch = meta.get("branch_coverage") if isinstance(meta, dict) else None
    if branch is not True:
        raise RuntimeError(
            f"{coverage_json}: meta.branch_coverage is {branch!r}, so this report carries no "
            "branch data. CRAP is defined over branch coverage; re-run coverage with "
            "[tool.coverage.run] branch = true."
        )


def parse_line_numbers(raw: object) -> set[int]:
    if not isinstance(raw, list):
        return set()
    return {int(value) for value in raw if isinstance(value, int) and value > 0}


def parse_branch_arcs(raw: object) -> set[tuple[int, int]]:
    """``[[source, destination], ...]`` as pairs.

    A malformed entry raises rather than being dropped: a silently skipped arc is a
    branch that reads as taken, which moves a score in the forgiving direction.
    """
    if not isinstance(raw, list):
        raise RuntimeError(f"expected a list of branch arcs, got {type(raw).__name__}")
    arcs: set[tuple[int, int]] = set()
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 2:
            raise RuntimeError(f"expected a [source, destination] branch arc, got {entry!r}")
        source, destination = entry
        if not isinstance(source, int) or not isinstance(destination, int):
            raise RuntimeError(f"branch arc endpoints must be integers, got {entry!r}")
        arcs.add((source, destination))
    return arcs


def coverage_keys(path: Path, project_root: Path) -> set[str]:
    keys = {path.as_posix().casefold(), str(path).casefold()}
    resolved = path.resolve() if path.is_absolute() else (project_root / path).resolve()
    keys.add(resolved.as_posix().casefold())
    keys.add(str(resolved).casefold())
    try:
        relative = resolved.relative_to(project_root)
    except ValueError:
        pass
    else:
        keys.add(relative.as_posix().casefold())
        keys.add(str(relative).casefold())
    return keys


def iter_python_files(paths: list[Path], project_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        resolved = path if path.is_absolute() else project_root / path
        if resolved.is_file() and resolved.suffix == ".py":
            files.append(resolved.resolve())
        elif resolved.is_dir():
            files.extend(
                child.resolve()
                for child in sorted(resolved.rglob("*.py"))
                if not has_ignored_part(child)
            )
    return sorted(dict.fromkeys(files))


def has_ignored_part(path: Path) -> bool:
    return any(part in IGNORED_PATH_PARTS for part in path.parts)


def analyze_file(
    path: Path,
    coverage_by_path: dict[str, FileCoverage],
    project_root: Path,
) -> list[FunctionScore]:
    coverage = find_file_coverage(path, coverage_by_path, project_root)
    scores: list[FunctionScore] = []
    for block in complexity_blocks(path):
        if not is_function_block(block):
            continue
        scores.append(score_block(path, block, coverage))
    return scores


def find_file_coverage(
    path: Path,
    coverage_by_path: dict[str, FileCoverage],
    project_root: Path,
) -> FileCoverage:
    for key in coverage_keys(path, project_root):
        coverage = coverage_by_path.get(key)
        if coverage is not None:
            return coverage
    return FileCoverage(frozenset(), frozenset(), frozenset(), frozenset(), has_data=False)


def complexity_blocks(path: Path) -> list[Any]:
    try:
        radon_complexity = importlib.import_module("radon.complexity")
    except ImportError as error:
        raise RuntimeError(
            "CRAP-Calculator requires radon. Install the development requirements first."
        ) from error
    return list(radon_complexity.cc_visit(path.read_text(encoding="utf-8")))


def is_function_block(block: Any) -> bool:
    return hasattr(block, "is_method")


def score_block(path: Path, block: Any, coverage: FileCoverage) -> FunctionScore:
    span = set(range(int(block.lineno), int(block.endline) + 1))
    executable = coverage.executable_lines & span
    covered = coverage.executed_lines & span
    missing = coverage.missing_lines & span
    taken = arcs_from(coverage.executed_branches, span)
    untaken = arcs_from(coverage.missing_branches, span)
    coverage_ratio = coverage_ratio_for_function(
        len(covered) + taken,
        len(executable) + taken + untaken,
        coverage.has_data,
    )
    complexity = int(block.complexity)
    return FunctionScore(
        path=path,
        function=str(getattr(block, "fullname", block.name)),
        kind="method" if bool(block.is_method) else "function",
        start_line=int(block.lineno),
        end_line=int(block.endline),
        complexity=complexity,
        covered_lines=len(covered),
        missing_lines=len(missing),
        executable_lines=len(executable),
        covered_branches=taken,
        missing_branches=untaken,
        coverage_ratio=coverage_ratio,
        crap=crap_score(complexity, coverage_ratio),
        missing_coverage_data=not coverage.has_data,
    )


def arcs_from(arcs: frozenset[tuple[int, int]], span: set[int]) -> int:
    return sum(1 for source, _destination in arcs if source in span)


def coverage_ratio_for_function(covered_units: int, total_units: int, has_data: bool) -> float:
    """Coverage.py statement-plus-branch ratio over one function span.

    An absent file is wholly uncovered. A measured span with no executable units is
    treated as covered, matching an explicit coverage exclusion.
    """
    if not has_data:
        return 0.0
    if not total_units:
        return 1.0
    return covered_units / total_units


def calculate_scores(
    paths: list[Path],
    *,
    coverage_json: Path,
    project_root: Path,
) -> list[FunctionScore]:
    resolved_root = project_root.resolve()
    coverage = load_coverage_by_path(coverage_json, resolved_root)
    scores: list[FunctionScore] = []
    for path in iter_python_files(paths, resolved_root):
        scores.extend(analyze_file(path, coverage, resolved_root))
    return sorted(scores, key=lambda score: (-score.crap, -score.complexity, score.path.as_posix()))


def rollup_by_file(scores: list[FunctionScore], threshold: float) -> list[FileRollup]:
    grouped: dict[Path, list[FunctionScore]] = {}
    for score in scores:
        grouped.setdefault(score.path, []).append(score)
    rollups = [
        FileRollup(
            path=path,
            function_count=len(file_scores),
            max_crap=max(score.crap for score in file_scores),
            average_crap=sum(score.crap for score in file_scores) / len(file_scores),
            over_threshold=sum(1 for score in file_scores if score.crap >= threshold),
        )
        for path, file_scores in grouped.items()
    ]
    return sorted(rollups, key=lambda item: (-item.max_crap, item.path.as_posix()))


def score_to_mapping(score: FunctionScore, project_root: Path) -> dict[str, Any]:
    return {
        "path": display_path(score.path, project_root),
        "function": score.function,
        "kind": score.kind,
        "startLine": score.start_line,
        "endLine": score.end_line,
        "complexity": score.complexity,
        "coverageRatio": round(score.coverage_ratio, 4),
        "coveredLines": score.covered_lines,
        "missingLines": score.missing_lines,
        "executableLines": score.executable_lines,
        "coveredBranches": score.covered_branches,
        "missingBranches": score.missing_branches,
        "crap": round(score.crap, 2),
        "missingCoverageData": score.missing_coverage_data,
    }


def rollup_to_mapping(rollup: FileRollup, project_root: Path) -> dict[str, Any]:
    return {
        "path": display_path(rollup.path, project_root),
        "functionCount": rollup.function_count,
        "maxCrap": round(rollup.max_crap, 2),
        "averageCrap": round(rollup.average_crap, 2),
        "overThreshold": rollup.over_threshold,
    }


def display_path(path: Path, project_root: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def render_table(
    scores: list[FunctionScore], project_root: Path, threshold: float, top: int
) -> str:
    selected = scores[:top] if top > 0 else scores
    rollups = rollup_by_file(scores, threshold)
    lines = [
        "# CRAP-Calculator",
        "",
        f"Review threshold: {threshold:.1f} (diagnostic only)",
        "",
        "## Function Scores",
        "",
        "| CRAP | CC | Branch Cov | Exec Lines | Branches | Function | Location |",
        "| ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    lines.extend(function_row(score, project_root) for score in selected)
    lines.extend(
        [
            "",
            "## File Rollup",
            "",
            "| Max CRAP | Avg CRAP | Functions | Over Threshold | File |",
            "| ---: | ---: | ---: | ---: | --- |",
        ]
    )
    lines.extend(rollup_row(rollup, project_root) for rollup in rollups)
    return "\n".join(lines)


def function_row(score: FunctionScore, project_root: Path) -> str:
    coverage = f"{score.coverage_ratio * 100:.1f}%"
    branches = f"{score.covered_branches}/{score.covered_branches + score.missing_branches}"
    location = f"{display_path(score.path, project_root)}:{score.start_line}"
    return (
        f"| {score.crap:.2f} | {score.complexity} | {coverage} | "
        f"{score.executable_lines} | {branches} | `{score.function}` | `{location}` |"
    )


def rollup_row(rollup: FileRollup, project_root: Path) -> str:
    return (
        f"| {rollup.max_crap:.2f} | {rollup.average_crap:.2f} | "
        f"{rollup.function_count} | {rollup.over_threshold} | "
        f"`{display_path(rollup.path, project_root)}` |"
    )


def render_json(scores: list[FunctionScore], project_root: Path, threshold: float) -> str:
    payload = {
        "tool": "CRAP-Calculator",
        "threshold": threshold,
        "functions": [score_to_mapping(score, project_root) for score in scores],
        "files": [
            rollup_to_mapping(rollup, project_root) for rollup in rollup_by_file(scores, threshold)
        ],
    }
    return json.dumps(payload, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "CRAP-Calculator: combine Radon cyclomatic complexity with coverage JSON "
            "to report function-level CRAP scores."
        )
    )
    parser.add_argument("paths", nargs="*", type=Path, default=[Path(".")])
    parser.add_argument("--coverage-json", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_CRAP_THRESHOLD,
        help="Review threshold for diagnostic findings; scores do not fail delivery.",
    )
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        project_root = args.project_root.resolve()
        scores = calculate_scores(
            args.paths,
            coverage_json=args.coverage_json.resolve(),
            project_root=project_root,
        )
        if args.format == "json":
            print(render_json(scores, project_root, args.threshold))
        else:
            print(render_table(scores, project_root, args.threshold, args.top))
    except RuntimeError as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    sys.exit(main())
