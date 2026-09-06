"""Repository-owned executable adapters for certification-profile Python rails."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

from agents_remember.certification.repository_profiles.selection_results import (
    RepositorySelectionResult,
)

from agents_remember_test_support.code_quality import (
    causal_continuation,
    check,
    profile_selection,
)
from agents_remember_test_support.code_quality import scope as quality_scope
from agents_remember_test_support.code_quality.check_cli import build_parser as quality_parser
from agents_remember_test_support.testing.dagger_admission import (
    DaggerAdmission,
    DaggerAdmissionError,
    require_dagger_admission,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="rail", required=True)
    for name in ("quality-config", "selection-ownership"):
        command = commands.add_parser(name)
        _common_arguments(command, reports=False)
    for name in ("python-suite", "python-crap", "python-diff-coverage"):
        command = commands.add_parser(name)
        _common_arguments(command, reports=True)
    teardown = commands.add_parser("verify-teardown")
    teardown.add_argument("--summary", type=Path, required=True)
    teardown.add_argument("--proof", type=Path, required=True)
    return parser


def _common_arguments(parser: argparse.ArgumentParser, *, reports: bool) -> None:
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--mode", choices=("targeted", "full"), required=True)
    parser.add_argument("--diff-base", required=True)
    parser.add_argument("--scope", type=Path, required=True)
    if reports:
        parser.add_argument("--reports", type=Path, required=True)
        parser.add_argument("--memory-cap-bytes", type=int, default=0)


def _profile_config(args: argparse.Namespace, admission: DaggerAdmission) -> check.CheckConfig:
    reports = args.reports.resolve()
    argv = [
        "--project-root",
        args.project_root.as_posix(),
        "--coverage-json",
        (reports / "coverage.json").as_posix(),
        "--coverage-data",
        (reports / "coverage.data").as_posix(),
        "--pytest-report-log",
        (reports / "pytest-events.jsonl").as_posix(),
        "--pytest-phase-report",
        (reports / "pytest-phases.json").as_posix(),
        "--causal-failure-report",
        (reports / "causal-failures.json").as_posix(),
        "--progress-report",
        (reports / "quality-progress.json").as_posix(),
        "--diff-base",
        args.diff_base,
    ]
    if args.mode == "targeted":
        argv.append("--targeted")
    if args.memory_cap_bytes:
        argv.extend(("--memory-cap-bytes", str(args.memory_cap_bytes)))
    parsed = quality_parser().parse_args(argv)
    config = check.config_from_args(parsed, admission=admission)
    selection = _require_exact_scope(args, config)
    return replace(config, selection_digest=selection.selectionDigest)


def _require_exact_scope(
    args: argparse.Namespace,
    config: check.CheckConfig | None = None,
) -> RepositorySelectionResult:
    try:
        observed = json.loads(args.scope.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise quality_scope.ScopeError("profile selector published no valid scope") from error
    try:
        selection = RepositorySelectionResult.model_validate(observed)
    except ValueError as error:
        raise quality_scope.ScopeError("profile selector published an invalid contract") from error
    expected = profile_selection.selection_payload(
        args.project_root.resolve(),
        mode=args.mode,
        diff_base=args.diff_base,
    )
    if observed != expected:
        raise quality_scope.ScopeError(
            "profile selector result differs from the exact repository-owned derivation"
        )
    if config is None:
        return selection
    values = selection.output_values()
    actual = {
        "lint-paths": _paths(config.scope.lint_paths),
        "type-closure": _paths(config.scope.type_paths),
        "coverage-paths": _paths(config.scope.coverage_paths),
        "selected-tests": _paths(config.scope.test_paths),
        "size-paths": _paths(config.scope.size_paths),
    }
    for name, value in actual.items():
        if list(values.get(name, ())) != value:
            raise quality_scope.ScopeError(
                f"profile selector field {name} differs from the executable rail scope"
            )
    return selection


def _paths(paths: list[Path]) -> list[str]:
    # Selector contracts use canonical string order; Path orders path components,
    # which differs for siblings such as conversation/ and conversation-library/.
    return sorted(path.as_posix() for path in paths)


def _run_python_suite(config: check.CheckConfig) -> int:
    coverage_json = config.coverage_json
    if coverage_json is None:
        raise quality_scope.ScopeError("profile Python suite requires a coverage artifact")
    for path in (
        coverage_json,
        config.coverage_data,
        config.pytest_report_log,
        config.pytest_phase_report,
    ):
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
    if config.targeted and not config.scope.test_paths:
        print("result: pytest PASS (not applicable: no exact affected test population)")
        return 0
    retry_plan = check.initialized_retry_plan(
        config,
        coverage_json,
        config.project_root.resolve(),
        runner=check.run_subprocess,
        printer=check.print_line,
    )
    if retry_plan is None:
        return _run_without_retry(config, coverage_json)
    failures = 0
    try:
        causal_report = config.causal_failure_report
        if causal_report is None:
            raise quality_scope.ScopeError("profile Python suite lacks causal input authority")
        observation = causal_continuation.inspect_causal_report(causal_report)
        causal_failure = observation.state is causal_continuation.CausalReportState.FAILED
        report_unavailable = observation.state is causal_continuation.CausalReportState.UNAVAILABLE
        step = next(
            (
                item
                for item in check.quality_steps(config, coverage_json, retry_plan=retry_plan)
                if item.name == "pytest"
            ),
            None,
        )
        if step is None:
            check.print_line("result: pytest FAIL (profile derived no pytest rail)")
            return 1
        active = check._causal_continuation_step(
            step,
            config,
            coverage_json,
            causal_failure=causal_failure,
            causal_report_unavailable=report_unavailable,
        )
        if active is None:
            check.print_line("result: pytest FAIL (causal continuation derived no pytest rail)")
            return 1
        if retry_plan.exact and not causal_failure:
            failures = check.report_cached_pytest(coverage_json, check.print_line)
        else:
            runtime = check.RailRuntime(
                runner=check.run_subprocess,
                printer=check.print_line,
                retry_plan=retry_plan,
            )
            result = check.run_subprocess(
                active.name,
                active.command,
                config.project_root,
                check.subprocess_env(config),
            )
            failures = check._pytest_result_failures(
                active,
                result,
                config,
                coverage_json,
                runtime,
            )
        return failures
    finally:
        retry_plan.finish(coverage_json, quality_passed=False)


def _run_without_retry(config: check.CheckConfig, coverage_json: Path) -> int:
    step = next(
        (item for item in check.quality_steps(config, coverage_json) if item.name == "pytest"),
        None,
    )
    if step is None:
        check.print_line("result: pytest FAIL (profile derived no pytest rail)")
        return 1
    result = check.run_subprocess(
        step.name,
        step.command,
        config.project_root,
        check.subprocess_env(config),
    )
    return check.report_pytest_result(step, result, coverage_json, check.print_line)


def _run_post_coverage(config: check.CheckConfig, *, rail: str) -> int:
    coverage_json = config.coverage_json
    if coverage_json is None:
        raise quality_scope.ScopeError("post-coverage rail requires the exact suite artifact")
    if rail == "python-crap":
        return check.run_crap_calculator(
            config,
            coverage_json,
            config.project_root.resolve(),
            printer=check.print_line,
        )
    return check.run_diff_coverage(
        config,
        coverage_json,
        config.project_root.resolve(),
        printer=check.print_line,
    )


def _verify_teardown(summary: Path, proof: Path) -> int:
    try:
        summary_bytes = summary.read_bytes()
        payload = json.loads(summary_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("clean-room summary is unavailable or malformed") from error
    if not isinstance(payload, dict) or payload.get("schema") != (
        "ar-ambient-role-chat-e2e-summary/v1"
    ):
        raise RuntimeError("clean-room summary has the wrong schema")
    observations: list[dict[str, object]] = []
    if payload.get("status") == "skipped":
        _write_teardown_proof(summary_bytes, proof, "not-applicable", observations)
        print("result: teardown-process-cleanliness PASS (scenario not applicable)")
        return 0
    runs = payload.get("runs")
    if payload.get("status") != "passed" or not isinstance(runs, list) or not runs:
        raise RuntimeError("clean-room summary does not prove successful teardown")
    root = summary.parent
    for run in runs:
        if not isinstance(run, dict) or run.get("status") != "passed":
            raise RuntimeError("clean-room replication did not pass")
        reference = run.get("report")
        if not isinstance(reference, str) or Path(reference).name != reference:
            raise RuntimeError("clean-room replication reference is unsafe")
        report_bytes = (root / reference).read_bytes()
        detail = json.loads(report_bytes)
        checkpoints = detail.get("checkpoints") if isinstance(detail, dict) else None
        cleanup = (
            next(
                (
                    item
                    for item in checkpoints
                    if isinstance(item, dict) and item.get("id") == "L5-C10"
                ),
                None,
            )
            if isinstance(checkpoints, list)
            else None
        )
        if not isinstance(cleanup, dict) or cleanup.get("status") != "passed":
            raise RuntimeError("clean-room replication lacks a passing teardown checkpoint")
        observations.append(
            {
                "report": reference,
                "sha256": hashlib.sha256(report_bytes).hexdigest(),
                "checkpoint": cleanup,
            }
        )
    _write_teardown_proof(summary_bytes, proof, "passed", observations)
    print("result: teardown-process-cleanliness PASS")
    return 0


def _write_teardown_proof(
    summary_bytes: bytes, proof: Path, status: str, observations: list[dict[str, object]]
) -> None:
    payload = {
        "schemaVersion": "teardown-proof/v1",
        "status": status,
        "summarySha256": hashlib.sha256(summary_bytes).hexdigest(),
        "replications": observations,
    }
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    try:
        admission = require_dagger_admission(subject="repository certification rail")
        args = build_parser().parse_args(argv)
        if args.rail == "verify-teardown":
            result = _verify_teardown(args.summary, args.proof)
        elif args.rail == "quality-config":
            quality_scope.validate_quality_config(args.project_root.resolve())
            _require_exact_scope(args)
            print("result: quality-config-scope PASS")
            result = 0
        elif args.rail == "selection-ownership":
            _require_exact_scope(args)
            print("result: test-selection-ownership PASS")
            result = 0
        else:
            if args.memory_cap_bytes < 0:
                raise quality_scope.ScopeError("memory-cap bytes cannot be negative")
            if args.memory_cap_bytes and not check.apply_memory_cap(args.memory_cap_bytes):
                result = 1
            else:
                config = _profile_config(args, admission)
                result = (
                    _run_python_suite(config)
                    if args.rail == "python-suite"
                    else _run_post_coverage(config, rail=args.rail)
                )
        return result
    except (DaggerAdmissionError, MemoryError, OSError, RuntimeError, ValueError) as error:
        print(f"repository certification rail refused: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
