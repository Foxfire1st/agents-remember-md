"""Command-line construction for the repository-owned Python quality gate."""

from __future__ import annotations

import argparse
from pathlib import Path

from agents_remember.kernel.primitives import memory_cap

from agents_remember_test_support.code_quality import crap_calculator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Agents Remember source quality suite over every tracked Python "
            "file. Enforcing steps: Ruff (lint, complexity rules "
            "C901/PLR0911/PLR0912/PLR0915 included), Ruff format (--check), Pyright "
            "(types), and pytest. CRAP and changed-lines coverage are diagnostic reports. The File Size Budget rail is wired here "
            "too; [tool.agents_remember] file_size_armed decides whether a violation "
            "fails the run (unarmed runs still report every band). Radon "
            "cyclomatic complexity and maintainability index are printed as a report "
            "only -- radon exits 0 whatever it finds, so it cannot fail this gate. Scope "
            "orders cheap deterministic subprocesses before pytest; CRAP and diff coverage "
            "then consume pytest's branch data. Dagger-owned exact/test-only retry proofs are "
            "content-addressed in an explicit locked Dagger cache and fail closed; "
            "AR_QUALITY_NO_RETRY is the explicit rollback trigger. Quality scope "
            "is derived from the index and configured roots, not from a flag: there is "
            "no way to narrow what the gate measures. Non-ignored untracked siblings are "
            "reported as outside that measurement. No baseline, allowlist or exemption "
            "file anywhere in it can excuse a finding."
        )
    )
    parser.add_argument(
        "--targeted",
        action="store_true",
        help=(
            "Run the leaf change-set contract instead of the full tree: ruff over the "
            "changed Python files, pyright over the changed files plus the reverse-import "
            "closure, pytest over the derived test subset, and coverage/CRAP scoped to the "
            "changed production modules. The derivation is printed for review."
        ),
    )
    parser.add_argument(
        "--memory-cap-bytes",
        type=int,
        help=(
            "Apply a POSIX address-space rlimit (RLIMIT_AS) to this process and every rail "
            "it spawns, so an over-cap run dies inside its own process instead of taking the "
            f"host down. Policy: {memory_cap.QUALITY_MEMORY_CAP_POLICY}."
        ),
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    _add_evidence_output_arguments(parser)
    parser.add_argument(
        "--threshold",
        type=float,
        default=crap_calculator.DEFAULT_CRAP_THRESHOLD,
        help="Report production functions at or above this review threshold; scores do not fail delivery.",
    )
    parser.add_argument("--top", type=int, default=crap_calculator.DEFAULT_TOP)
    parser.add_argument(
        "--diff-base",
        help=(
            "Revision the changed-lines coverage report diffs against. Defaults to the "
            "merge base with, in order, AR_GATE_DIFF_BASE, the pull request base, the "
            "branch's upstream, then the default branch. The base actually used is "
            "printed on every run."
        ),
    )
    return parser


def _add_evidence_output_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach report paths without mixing evidence plumbing into policy arguments."""

    parser.add_argument(
        "--coverage-json",
        type=Path,
        help="Optional path for the generated Coverage.py JSON report.",
    )
    parser.add_argument(
        "--pytest-report-log",
        type=Path,
        help=(
            "Replace this JSONL file with pytest collection and result events. The report "
            "is flushed per event so a lifecycle operation can project live progress."
        ),
    )
    parser.add_argument(
        "--pytest-phase-report",
        type=Path,
        help=(
            "Replace this JSON file with route-neutral pytest phase timestamps and node outcomes."
        ),
    )
    parser.add_argument(
        "--causal-failure-report",
        type=Path,
        help=(
            "Replace this JSON artifact with owner preflight causes, graph-proven blocked "
            "groups, and independent pytest failures."
        ),
    )
    parser.add_argument(
        "--coverage-data",
        type=Path,
        help="Keep the Coverage.py data file at this report-local path.",
    )
    parser.add_argument(
        "--progress-report",
        type=Path,
        help=(
            "Atomically replace this JSON file with the currently running rail and terminal "
            "wrapper result."
        ),
    )
