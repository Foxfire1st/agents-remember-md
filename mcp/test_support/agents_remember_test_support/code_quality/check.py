"""Run the repository-owned Python quality rails with explicit provenance.

Ruff, Ruff format, file size, Pyright, pytest, CRAP, and changed-lines coverage enforce.
Cheap deterministic rails precede pytest, which is the final subprocess; CRAP and diff
coverage then score its artifact. Radon CC and MI are labelled reports because Radon
findings do not change its exit status. Scope
comes from ``code_quality.scope`` for a full run; ``--targeted`` derives the leaf
change-set scope from ``code_quality.targeted`` (changed files, reverse-import closure,
and the derived test subset). Full runs may additionally run under a settings-owned
memory cap (``--memory-cap-bytes`` / ``orchestration.qualityGate.memoryCapBytes``).
The wrapper reports relevant untracked files separately because the index and diff
omit them.

Nonce-attested Dagger retries can reuse a content-addressed successful pytest proof when the exact
tree is unchanged or only selected test modules changed. Test-delta reuse strips their prior
Coverage.py contexts, writes fresh data separately, and merges both explicitly before scoring;
any ambiguity runs fresh. The wrapper itself
refuses before scope or retry planning outside the graph.

Each rail prints its actual input, config, nonzero population or result denominator, and
explicit result. Missing/vacuous inputs and tool failures refuse. Findings are remediated
in source or tests; baselines, allowlists, and exemptions are not supported.
"""

from __future__ import annotations

import argparse
import os
import resource
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from agents_remember.kernel.platform_subprocess import native_subprocess_environment
from agents_remember.kernel.primitives import memory_cap

from agents_remember_test_support.code_quality import (
    causal_continuation,
    diff_coverage,
    post_coverage,
    quality_subprocess_environment,
    retry_proof,
    scope_reporting,
    targeted,
)
from agents_remember_test_support.code_quality import scope as quality_scope
from agents_remember_test_support.code_quality.check_cli import build_parser
from agents_remember_test_support.code_quality.quality_plan import (
    RADON_REPORT_NOTE,
    CheckConfig,
    GateScope,
    QualityProgress,
    ScopeError,
    Step,
    derive_scope,
    git_ls_files,
    posix_args,
    pytest_testpaths,
    quality_steps,
    toml_section,
    top_level_packages,
)
from agents_remember_test_support.testing.dagger_admission import (
    DaggerAdmission,
    DaggerAdmissionError,
    require_dagger_admission,
    require_dagger_admission_capability,
)
from agents_remember_test_support.testing.evidence_lanes import (
    EVIDENCE_LANES,
    EvidenceTrigger,
)
from agents_remember_test_support.testing.lane_manifest import (
    LaneManifestError,
    load_lane_manifest,
)

__all__ = [
    "RADON_REPORT_NOTE",
    "CheckConfig",
    "GateScope",
    "QualityProgress",
    "ScopeError",
    "Step",
    "derive_scope",
    "git_ls_files",
    "posix_args",
    "pytest_testpaths",
    "quality_steps",
    "toml_section",
    "top_level_packages",
]

crap_failure_line = post_coverage.crap_failure_line
run_crap_calculator = post_coverage.run_crap_calculator
run_diff_coverage = post_coverage.run_diff_coverage

QUALITY_PROGRESS_REPORT_ENV = "AR_QUALITY_PROGRESS_REPORT"
QUALITY_TEMP_ROOT = Path("/tmp/arq")


@dataclass(frozen=True)
class StepResult:
    name: str
    return_code: int
    command: list[str]


CommandRunner = Callable[[str, list[str], Path, Mapping[str, str]], StepResult]
Printer = Callable[[str], None]


@dataclass(frozen=True)
class RailRuntime:
    runner: CommandRunner
    printer: Printer
    retry_plan: retry_proof.RetryPlan | None


# 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/test_support/agents_remember_test_support/code_quality/check.py:106).
def print_line(message: str) -> None:  # pragma: no cover
    print(message, flush=True)


def apply_memory_cap(cap_bytes: int, printer: Printer | None = None) -> bool:
    """Apply the shared self-cap policy for any repository-owned quality entrypoint."""

    printer = printer or print_line
    try:
        resource.setrlimit(resource.RLIMIT_AS, (cap_bytes, cap_bytes))
    except (ValueError, OSError) as error:
        printer(
            f"memory-cap could not be applied (policy={memory_cap.QUALITY_MEMORY_CAP_POLICY}; "
            f"mechanism={memory_cap.RLIMIT_MECHANISM}; cap={cap_bytes} bytes): {error}"
        )
        return False
    os.environ[memory_cap.MEMORY_CAP_ENV] = str(cap_bytes)
    printer(
        f"memory-cap: policy={memory_cap.QUALITY_MEMORY_CAP_POLICY}; "
        f"mechanism={memory_cap.RLIMIT_MECHANISM}; cap={cap_bytes} bytes"
    )
    return True


# 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/test_support/agents_remember_test_support/code_quality/check.py:110).
def run_subprocess(
    name: str, command: list[str], cwd: Path, env: Mapping[str, str]
) -> StepResult:  # pragma: no cover
    completed = subprocess.run(
        command, cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, check=False
    )
    return StepResult(name=name, return_code=completed.returncode, command=command)


def subprocess_env(config: CheckConfig) -> dict[str, str]:  # pragma: no cover
    """Subprocess environment with this checkout's source roots first on PYTHONPATH."""
    return quality_subprocess_environment.build(
        os.environ,
        project_root=config.project_root,
        coverage_paths=config.scope.coverage_paths,
        coverage_data=config.coverage_data,
    )


def run_quality_check(
    config: CheckConfig,
    *,
    runner: CommandRunner = run_subprocess,
    printer: Printer = print_line,
) -> int:
    require_dagger_admission_capability(config.admission)
    project_root = config.project_root.resolve()
    progress = QualityProgress.start(config.progress_report)
    targeted = getattr(config, "targeted", False)
    if targeted and config.targeted_scope is not None and config.targeted_base is not None:
        for line in scope_reporting.targeted_scope_lines(
            config.targeted_base, config.targeted_scope
        ):
            printer(line)
        if not config.scope.lint_paths:
            printer(
                "targeted: no Python files changed against the leaf base; there is nothing "
                "for the leaf rails to certify"
            )
            progress.write(status="completed", step="complete", detail="no Python changes")
            printer("result: quality-wrapper PASS")
            return 0
        if not config.scope.coverage_paths:
            printer(
                "targeted: radon report and CRAP rails are not applicable -- no changed "
                "production modules"
            )
        if not config.scope.test_paths:
            printer(
                "targeted: pytest rail is not applicable -- no test subset was derived "
                "(no changed production modules and no changed tests)"
            )
    printer(scope_reporting.wrapper_scope_line(project_root, config.scope, targeted=targeted))
    for line in scope_reporting.untracked_scope_lines(config.scope):
        printer(line)
    with coverage_path_context(config.coverage_json, project_root) as coverage_json:
        failed_steps = execute_quality_rails(
            replace(config, progress=progress),
            coverage_json,
            project_root,
            runner=runner,
            printer=printer,
        )
    if failed_steps:
        progress.write(status="failed", step="complete", detail=f"{failed_steps} failed rails")
        printer(f"result: quality-wrapper FAIL ({failed_steps} failed rails)")
        return 1
    progress.write(status="completed", step="complete", detail="all quality rails passed")
    printer("result: quality-wrapper PASS")
    return 0


def execute_quality_rails(
    config: CheckConfig,
    coverage_json: Path,
    project_root: Path,
    *,
    runner: CommandRunner,
    printer: Printer,
) -> int:
    # An explicit output path may contain a report from an earlier tree. Never let a
    # pre-pytest refusal feed stale coverage into CRAP or diff-coverage.
    coverage_json.unlink(missing_ok=True)
    if config.coverage_data is not None:
        config.coverage_data.parent.mkdir(parents=True, exist_ok=True)
        config.coverage_data.unlink(missing_ok=True)
    if config.pytest_report_log is not None:
        config.pytest_report_log.parent.mkdir(parents=True, exist_ok=True)
        config.pytest_report_log.unlink(missing_ok=True)
    if config.pytest_phase_report is not None:
        config.pytest_phase_report.parent.mkdir(parents=True, exist_ok=True)
        config.pytest_phase_report.unlink(missing_ok=True)
    if config.causal_failure_report is not None:
        config.causal_failure_report.parent.mkdir(parents=True, exist_ok=True)
        config.causal_failure_report.unlink(missing_ok=True)
        config.causal_failure_report.with_suffix(".md").unlink(missing_ok=True)
    retry_plan = initialized_retry_plan(
        config,
        coverage_json,
        project_root,
        runner=runner,
        printer=printer,
    )
    failed_steps = run_fixed_checks(
        config,
        coverage_json,
        runner=runner,
        printer=printer,
        retry_plan=retry_plan,
    )
    if failed_steps and not coverage_json.is_file():
        report_missing_coverage_failure(printer)
    else:
        failed_steps = complete_coverage_rails(
            config,
            coverage_json,
            fixed_failures=failed_steps,
            runtime=RailRuntime(runner=runner, printer=printer, retry_plan=retry_plan),
        )
    if retry_plan is not None:
        retry_plan.finish(coverage_json, quality_passed=failed_steps == 0)
    return failed_steps


def complete_coverage_rails(
    config: CheckConfig,
    coverage_json: Path,
    *,
    fixed_failures: int,
    runtime: RailRuntime,
) -> int:
    project_root = config.project_root.resolve()
    coverage_failures = run_coverage_rails(
        config,
        coverage_json,
        project_root,
        printer=runtime.printer,
    )
    if not (
        runtime.retry_plan is not None
        and runtime.retry_plan.delta
        and runtime.retry_plan.pytest_passed
        and coverage_failures
        and not fixed_failures
    ):
        return fixed_failures + coverage_failures
    runtime.printer(
        "retry-proof: conservative delta coverage did not clear every post-pytest rail; "
        "running the full pytest selection once for a conclusive verdict"
    )
    runtime.retry_plan.prepare_fresh_rerun(coverage_json)
    pytest_failures = run_pytest_only(
        config,
        coverage_json,
        runtime.retry_plan,
        runner=runtime.runner,
        printer=runtime.printer,
    )
    if pytest_failures and not coverage_json.is_file():
        report_missing_coverage_failure(runtime.printer)
        return pytest_failures
    return pytest_failures + run_coverage_rails(
        config,
        coverage_json,
        project_root,
        printer=runtime.printer,
    )


def run_pytest_only(
    config: CheckConfig,
    coverage_json: Path,
    retry_plan: retry_proof.RetryPlan,
    *,
    runner: CommandRunner,
    printer: Printer,
) -> int:
    progress = config.progress or QualityProgress.start(None)
    step = next(
        (
            candidate
            for candidate in quality_steps(config, coverage_json, retry_plan=retry_plan)
            if candidate.name == "pytest"
        ),
        None,
    )
    if step is None:
        printer("result: pytest FAIL (fresh full rerun derived no pytest rail)")
        return 1
    printer(step_header(step))
    printer(
        scope_reporting.fixed_step_scope_line(
            step.name,
            config.project_root,
            config.scope,
            targeted=config.targeted,
        )
    )
    env = subprocess_env(config)
    env["COVERAGE_FILE"] = str(retry_plan.active_data_path)
    result = runner(step.name, step.command, config.project_root, env)
    retry_plan.record_pytest(result.return_code)
    failures = report_pytest_result(step, result, coverage_json, printer)
    progress.finish_step(step.name, passed=failures == 0)
    return failures


def initialized_retry_plan(
    config: CheckConfig,
    coverage_json: Path,
    project_root: Path,
    *,
    runner: CommandRunner,
    printer: Printer,
) -> retry_proof.RetryPlan | None:
    plan = prepare_retry_plan(
        config,
        project_root,
        runner=runner,
        printer=printer,
    )
    if plan is None:
        return None
    try:
        plan.prepare_artifacts(coverage_json)
    except (OSError, RuntimeError) as error:
        printer(f"retry-proof: artifact preparation failed ({error}); running fresh")
        coverage_json.unlink(missing_ok=True)
        return None
    return plan


def report_missing_coverage_failure(printer: Printer) -> None:
    printer("\n## CRAP-Calculator")
    printer("result: CRAP-Calculator SKIPPED (pytest coverage was not produced)")
    printer("\n## diff-coverage")
    printer("result: diff-coverage SKIPPED (pytest coverage was not produced)")


def run_coverage_rails(
    config: CheckConfig,
    coverage_json: Path,
    project_root: Path,
    *,
    printer: Printer,
) -> int:
    progress = config.progress or QualityProgress.start(None)
    progress.write(status="running", step="CRAP-Calculator", detail="score covered functions")
    crap_failures = run_crap_calculator(
        config,
        coverage_json,
        project_root,
        printer=printer,
    )
    progress.finish_step("CRAP-Calculator", passed=crap_failures == 0)
    progress.write(status="running", step="diff-coverage", detail="score changed statements")
    diff_failures = run_diff_coverage(
        config,
        coverage_json,
        project_root,
        printer=printer,
    )
    progress.finish_step("diff-coverage", passed=diff_failures == 0)
    return crap_failures + diff_failures


def run_fixed_checks(
    config: CheckConfig,
    coverage_json: Path,
    *,
    runner: CommandRunner,
    printer: Printer,
    retry_plan: retry_proof.RetryPlan | None = None,
) -> int:
    progress = config.progress or QualityProgress.start(None)
    env = subprocess_env(config)
    if retry_plan is not None:
        env["COVERAGE_FILE"] = str(retry_plan.active_data_path)
    targeted = getattr(config, "targeted", False)
    failed_steps = 0
    pytest_blocking_failures = 0
    causal_decision: causal_continuation.CausalPreflightDecision | None = None
    runtime = RailRuntime(runner=runner, printer=printer, retry_plan=retry_plan)
    for step in quality_steps(config, coverage_json, retry_plan=retry_plan):
        progress.write(status="running", step=step.name, detail="run quality rail")
        printer(step_header(step))
        printer(
            scope_reporting.fixed_step_scope_line(
                step.name,
                config.project_root,
                config.scope,
                targeted=targeted,
            )
        )
        if _skip_pytest_after_prior_failure(
            step,
            pytest_blocking_failures=pytest_blocking_failures,
            coverage_json=coverage_json,
            progress=progress,
            printer=printer,
        ):
            continue
        active_step = _causal_continuation_step(
            step,
            config,
            coverage_json,
            causal_failure=bool(causal_decision and causal_decision.causal_failure),
            causal_report_unavailable=bool(causal_decision and causal_decision.report_unavailable),
        )
        if _report_missing_causal_continuation(
            step,
            active_step=active_step,
            progress=progress,
            printer=printer,
        ):
            failed_steps += 1
            pytest_blocking_failures += 1
            continue
        assert active_step is not None
        if (
            active_step.name == "pytest"
            and retry_plan is not None
            and retry_plan.exact
            and not (causal_decision and causal_decision.causal_failure)
        ):
            cached_failures = report_cached_pytest(coverage_json, printer)
            failed_steps += cached_failures
            progress.finish_step(step.name, passed=cached_failures == 0)
            continue
        result = runner(active_step.name, active_step.command, config.project_root, env)
        if active_step.name == "causal-preflight":
            report_path = config.causal_failure_report
            assert report_path is not None
            causal_decision = causal_continuation.evaluate_preflight_result(
                result.return_code,
                report_path,
                printer=printer,
            )
            failed_steps += int(not causal_decision.passed)
            progress.finish_step(active_step.name, passed=causal_decision.passed)
            if not causal_decision.passed:
                report_memory_cap_failure(active_step.name, printer)
            continue
        if active_step.name == "pytest":
            pytest_failures = _pytest_result_failures(
                active_step,
                result,
                config,
                coverage_json,
                runtime,
            )
            failed_steps += pytest_failures
            progress.finish_step(active_step.name, passed=pytest_failures == 0)
            continue
        if result.return_code == 0:
            printer(step_success(active_step))
            progress.finish_step(active_step.name, passed=True)
            continue
        failed_steps += 1
        pytest_blocking_failures += 1
        printer(step_failure(active_step, result.return_code))
        progress.finish_step(active_step.name, passed=False)
        report_memory_cap_failure(active_step.name, printer)
    return failed_steps


def _skip_pytest_after_prior_failure(
    step: Step,
    *,
    pytest_blocking_failures: int,
    coverage_json: Path,
    progress: QualityProgress,
    printer: Printer,
) -> bool:
    if step.name != "pytest" or not pytest_blocking_failures:
        return False
    # Never let a cached earlier-tree artifact survive when pytest cannot run.
    coverage_json.unlink(missing_ok=True)
    printer("result: pytest SKIPPED (an earlier quality rail failed)")
    progress.finish_step(step.name, passed=False)
    return True


def _report_missing_causal_continuation(
    step: Step,
    *,
    active_step: Step | None,
    progress: QualityProgress,
    printer: Printer,
) -> bool:
    if active_step is not None:
        return False
    printer("result: pytest FAIL (causal continuation derived no pytest rail)")
    progress.finish_step(step.name, passed=False)
    return True


def _pytest_result_failures(
    step: Step,
    result: StepResult,
    config: CheckConfig,
    coverage_json: Path,
    runtime: RailRuntime,
) -> int:
    retry_plan = runtime.retry_plan
    if retry_plan is not None:
        retry_plan.record_pytest(result.return_code)
        if (
            retry_plan.delta
            and retry_plan.pytest_passed
            and not _merge_retry_coverage(
                retry_plan,
                coverage_json,
                config.project_root,
                runtime.printer,
            )
        ):
            return 1
    return report_pytest_result(step, result, coverage_json, runtime.printer)


def _merge_retry_coverage(
    retry_plan: retry_proof.RetryPlan,
    coverage_json: Path,
    project_root: Path,
    printer: Printer,
) -> bool:
    try:
        retry_plan.merge_delta_artifacts(coverage_json, project_root)
    except (OSError, RuntimeError) as error:
        retry_plan.reject_pytest_proof()
        coverage_json.unlink(missing_ok=True)
        printer(f"retry-proof: delta coverage merge failed ({error})")
        printer("result: pytest FAIL (delta coverage proof could not be finalized)")
        return False
    printer("retry-proof: retained and delta Coverage.py contexts merged explicitly")
    return True


def _causal_continuation_step(
    step: Step,
    config: CheckConfig,
    coverage_json: Path,
    *,
    causal_failure: bool,
    causal_report_unavailable: bool,
) -> Step | None:
    if step.name != "pytest" or not (causal_failure or causal_report_unavailable):
        return step
    coverage_json.unlink(missing_ok=True)
    if config.coverage_data is not None:
        config.coverage_data.unlink(missing_ok=True)
    continuation_config = (
        replace(config, causal_failure_report=None) if causal_report_unavailable else config
    )
    return next(
        (
            candidate
            for candidate in quality_steps(
                continuation_config,
                coverage_json,
                retry_plan=None,
            )
            if candidate.name == "pytest"
        ),
        None,
    )


def report_cached_pytest(coverage_json: Path, printer: Printer) -> int:
    try:
        printer(scope_reporting.coverage_result_scope_line(coverage_json))
    except ScopeError as error:
        printer(f"coverage result reporting failed: {error}")
        printer("result: pytest FAIL (cached Coverage.py result scope unavailable)")
        return 1
    printer("result: pytest PASS (exact content-addressed proof reused)")
    return 0


def report_pytest_result(
    step: Step,
    result: StepResult,
    coverage_json: Path,
    printer: Printer,
) -> int:
    if coverage_json.is_file():
        try:
            printer(scope_reporting.coverage_result_scope_line(coverage_json))
        except ScopeError as error:
            printer(f"coverage result reporting failed: {error}")
            printer("result: pytest FAIL (Coverage.py result scope unavailable)")
            return 1
    if result.return_code == 0:
        printer(step_success(step))
        return 0
    printer(step_failure(step, result.return_code))
    report_memory_cap_failure(step.name, printer)
    return 1


def report_memory_cap_failure(step_name: str, printer: Printer) -> None:
    cap = os.environ.get(memory_cap.MEMORY_CAP_ENV)
    if cap:
        printer(
            f"{step_name} may have died from the quality memory cap "
            f"(policy={memory_cap.QUALITY_MEMORY_CAP_POLICY}; "
            f"mechanism={memory_cap.RLIMIT_MECHANISM}; cap={cap} bytes; "
            "see the memory-cap line at the top of this run)"
        )


def prepare_retry_plan(
    config: CheckConfig,
    project_root: Path,
    *,
    runner: CommandRunner,
    printer: Printer,
) -> retry_proof.RetryPlan | None:
    """Enable proof reuse only for the real wrapper subprocess pipeline."""
    if runner is not run_subprocess:
        return None
    try:
        base = (
            config.targeted_base
            if config.targeted_base is not None
            else diff_coverage.resolve_base(project_root, explicit_base=config.diff_base)
        )
        manifest = load_lane_manifest(project_root)
        trigger = EvidenceTrigger.AFFECTED if config.targeted else EvidenceTrigger.RELEASE
        accepting = frozenset(lane.category for lane in EVIDENCE_LANES if trigger in lane.triggers)
        cache_root_text = os.environ.get(retry_proof.CACHE_ROOT_ENV)
        if not cache_root_text:
            raise ScopeError(
                f"{retry_proof.CACHE_ROOT_ENV} is required for Dagger-owned retry persistence"
            )
        return retry_proof.prepare(
            retry_proof.RetryInputs(
                project_root=project_root,
                targeted=config.targeted,
                base_revision=base.revision,
                threshold=config.threshold,
                top=config.top,
                diff_floor=config.diff_floor,
                coverage_paths=tuple(config.scope.coverage_paths),
                test_arguments=tuple(config.scope.test_paths),
                untracked_paths=tuple(config.scope.untracked_paths),
                cache_root=Path(cache_root_text),
                lane_digest=manifest.digest,
                lane_trigger=trigger.value,
                lane_population=manifest.compatibility_population(accepting),
                selection_digest=config.selection_digest or "",
            ),
            admission=config.admission,
            printer=printer,
        )
    except (LaneManifestError, OSError, RuntimeError, ScopeError) as error:
        printer(f"retry-proof: unavailable ({error}); running fresh")
        return None


def step_header(step: Step) -> str:
    if step.report_note is None:
        return f"\n## {step.name}"
    return f"\n## {step.name} -- {step.report_note}"


def step_failure(step: Step, return_code: int) -> str:
    if step.report_note is None:
        return f"result: {step.name} FAIL (exit code {return_code})"
    return (
        f"result: {step.name} FAIL; {step.name} could not run (exit code {return_code}). "
        "This is a report step, "
        "so this is the tool breaking rather than a finding -- fix the tool."
    )


def step_success(step: Step) -> str:
    if step.report_note is None:
        return f"result: {step.name} PASS"
    return f"result: {step.name} REPORT COMPLETE (non-enforcing)"


class coverage_path_context:
    def __init__(self, requested_path: Path | None, project_root: Path) -> None:
        self.requested_path = requested_path
        self.project_root = project_root
        self.temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None

    # 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/test_support/agents_remember_test_support/code_quality/check.py:468).
    def __enter__(self) -> Path:  # pragma: no cover
        if self.requested_path is not None:
            self.path = resolve_under_root(self.requested_path, self.project_root)
            return self.path
        self.temp_dir = tempfile.TemporaryDirectory(prefix="agents-remember-quality-")
        self.path = Path(self.temp_dir.name) / "coverage.json"
        return self.path

    # 260731-EFA-L7 R10: verbatim L7 split; unchanged branch, out of this leaf's behavior scope (mcp/test_support/agents_remember_test_support/code_quality/check.py:476).
    def __exit__(self, *exc_info: object) -> None:  # pragma: no cover
        if self.temp_dir is not None:
            self.temp_dir.cleanup()


def resolve_under_root(path: Path, project_root: Path) -> Path:
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def config_from_args(
    args: argparse.Namespace,
    *,
    admission: DaggerAdmission,
) -> CheckConfig:
    require_dagger_admission_capability(admission)
    project_root = args.project_root.resolve()
    configured_progress = getattr(args, "progress_report", None)
    if configured_progress is None and (
        progress_env := os.environ.get(QUALITY_PROGRESS_REPORT_ENV)
    ):
        configured_progress = Path(progress_env)
    configured_coverage_data = getattr(args, "coverage_data", None)
    if configured_coverage_data is None and (coverage_env := os.environ.get("COVERAGE_FILE")):
        configured_coverage_data = Path(coverage_env)
    configured_causal_report = getattr(args, "causal_failure_report", None)
    if configured_causal_report is None:
        configured_causal_report = QUALITY_TEMP_ROOT / str(os.getpid()) / "causal-failures.json"
    quality_scope.validate_quality_config(project_root)
    try:
        scope_reporting.validate_invocation_environment()
    except scope_reporting.ScopeReportingError as error:
        raise ScopeError(str(error)) from error
    if args.targeted:
        base = diff_coverage.resolve_base(project_root, explicit_base=args.diff_base)
        derived = targeted.derive_targeted_scope(project_root, base.revision)
        if not derived.test_impact.complete:
            details = "; ".join(reason.render() for reason in derived.test_impact.unresolved_inputs)
            raise ScopeError("test-selection-ownership-incomplete: " + (details or "unknown input"))
        full_scope = derive_scope(project_root)
        return CheckConfig(
            project_root=project_root,
            admission=admission,
            scope=derived.to_gate_scope(full_scope),
            coverage_json=args.coverage_json,
            threshold=args.threshold,
            top=args.top,
            diff_base=args.diff_base,
            diff_floor=args.diff_floor,
            targeted=True,
            targeted_base=base,
            targeted_scope=derived,
            file_size_armed=quality_scope.file_size_armed(project_root),
            pytest_report_log=getattr(args, "pytest_report_log", None),
            pytest_phase_report=getattr(args, "pytest_phase_report", None),
            causal_failure_report=configured_causal_report,
            coverage_data=configured_coverage_data,
            progress_report=configured_progress,
        )
    return CheckConfig(
        project_root=project_root,
        admission=admission,
        scope=derive_scope(project_root),
        coverage_json=args.coverage_json,
        threshold=args.threshold,
        top=args.top,
        diff_base=args.diff_base,
        diff_floor=args.diff_floor,
        file_size_armed=quality_scope.file_size_armed(project_root),
        pytest_report_log=getattr(args, "pytest_report_log", None),
        pytest_phase_report=getattr(args, "pytest_phase_report", None),
        causal_failure_report=configured_causal_report,
        coverage_data=configured_coverage_data,
        progress_report=configured_progress,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        admission = require_dagger_admission(subject="Agents Remember quality wrapper")
    except DaggerAdmissionError as error:
        print_line(str(error))
        print_line("result: quality-wrapper FAIL")
        return 1
    parser = build_parser()
    args = parser.parse_args(argv)
    native_environment = native_subprocess_environment(os.environ, temp_root=QUALITY_TEMP_ROOT)
    os.environ.clear()
    os.environ.update(native_environment)
    # ``tempfile`` caches its chosen directory process-wide. Reset it after sanitising the
    # environment so a module imported before ``main`` cannot preserve a Windows/long-path root.
    tempfile.tempdir = QUALITY_TEMP_ROOT.as_posix()
    if args.memory_cap_bytes is not None and args.memory_cap_bytes <= 0:
        print_line(
            "--memory-cap-bytes must be a positive integer "
            f"(policy={memory_cap.QUALITY_MEMORY_CAP_POLICY})"
        )
        print_line("result: quality-wrapper FAIL")
        return 1
    if args.memory_cap_bytes is not None and not apply_memory_cap(
        args.memory_cap_bytes,
        print_line,
    ):
        print_line("result: quality-wrapper FAIL")
        return 1
    try:
        config = config_from_args(args, admission=admission)
        return run_quality_check(config)
    except ScopeError as error:
        print_line(f"gate scope could not be derived: {error}")
        print_line("result: quality-wrapper FAIL")
        return 1
    except MemoryError:
        if args.memory_cap_bytes is not None:
            print_line(
                "result: quality-wrapper FAIL (memory cap exceeded; "
                f"policy={memory_cap.QUALITY_MEMORY_CAP_POLICY}; "
                f"mechanism={memory_cap.RLIMIT_MECHANISM}; cap={args.memory_cap_bytes} bytes)"
            )
        else:
            print_line("result: quality-wrapper FAIL (out of memory)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
