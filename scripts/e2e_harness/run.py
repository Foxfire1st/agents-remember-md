#!/usr/bin/env python3
"""Run the ambient-role clean-room scenario twice, with no retry semantics."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from agents_remember.kernel.primitives.checkout_coordination import declare_test_process
from agents_remember.worktrees.modules.git import worktree_candidate_tree
from agents_remember_test_support.testing.dagger_admission import require_dagger_admission
from reporting import CheckpointDefinition, CheckpointRecorder, write_json
from scenario import run_scenario
from selection import admitted_selection

RUN_COUNT = 2
C10 = CheckpointDefinition(
    "L5-C10",
    requirement="L5-R5",
    expected="fresh run leaves no hosted tmux session or cleanup failure behind",
    owner="E2E fixture teardown",
)


@dataclass(frozen=True)
class _RunContext:
    reports: Path
    args: argparse.Namespace
    repository: Path
    selected: tuple[str, ...]
    candidate: dict[str, str]
    command: str
    invocation: str


def main() -> int:
    _admit_execution()
    args = _arguments()
    repository = args.repository_root.resolve()
    reports = args.reports.resolve() / "ambient-role-chat-e2e"
    reports.mkdir(parents=True, exist_ok=True)
    candidate = _candidate_identity(repository)
    decision = admitted_selection(
        args.source_selection,
        repository,
        candidate["tree"],
        mode=args.mode,
        diff_base=args.diff_base,
    )
    changed = decision.sourceSelection.changedPaths
    selected = decision.selectedPaths
    command = _invocation_command(args)
    context = _RunContext(
        reports=reports,
        args=args,
        repository=repository,
        selected=selected,
        candidate=candidate,
        command=command,
        invocation=uuid.uuid4().hex,
    )
    run_reports = _run_replications(context)
    passed = all(report["status"] == "passed" for report in run_reports)
    _write_run_summary(
        context,
        passed=passed,
        changed=changed,
        run_reports=run_reports,
    )
    return 0 if passed else 1


def _admit_execution() -> None:
    """Refuse every host invocation before any fixture or tmux side effect."""

    require_dagger_admission(subject="Ambient role-chat E2E")
    # The controller is explicit test infrastructure. Declaring that boundary lets it
    # inspect its disposable authority fixture while every spawned MCP process still
    # enters through the normal daemon declaration and public stdio surface.
    declare_test_process()


def _invocation_command(args: argparse.Namespace) -> str:
    return (
        f"{sys.executable} scripts/e2e_harness/run.py --mode {args.mode} "
        f"--diff-base {args.diff_base} --reports {args.reports} "
        f"--source-selection {args.source_selection}"
    )


def _run_replications(context: _RunContext) -> list[dict[str, object]]:
    return [_run_once(context, index=index) for index in range(1, RUN_COUNT + 1)]


def _run_once(
    context: _RunContext,
    *,
    index: int,
) -> dict[str, object]:
    root = _fresh_run_root(context.invocation, index)
    recorder = CheckpointRecorder(scenario=f"ambient-role-chat/run-{index}")
    status = "passed"
    result: dict[str, object] = {}
    error: dict[str, str] | None = None
    try:
        result = run_scenario(root, repository_root=context.repository, recorder=recorder)
        residual = _residual_tmux(result)
        recorder.check(
            C10,
            actual={"residualSessions": residual, "cleanup": result.get("cleanup")},
            passed=not residual and _cleanup_passed(result),
        )
    except Exception as exc:
        status = "failed"
        error = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        root_cleanup_error = _remove_run_root(root, recorder)
        if root_cleanup_error is not None and error is None:
            status = "failed"
            error = root_cleanup_error
    report = recorder.report(
        schema="ar-ambient-role-chat-e2e-run/v1",
        status=status,
        run=index,
        plannedReplications=RUN_COUNT,
        retry=False,
        mode=context.args.mode,
        diffBase=context.args.diff_base,
        selectedPaths=context.selected,
        candidate=context.candidate,
        command=context.command,
        fixtureInvocation=context.invocation,
        result=result,
        error=error,
    )
    write_json(context.reports / f"run-{index}.json", report)
    return report


def _fresh_run_root(invocation: str, index: int) -> Path:
    # Hosted control uses a Unix socket below the fixture workspace. Keep the
    # disposable root deliberately short so the real control endpoint remains
    # below Linux's 103-byte portable path ceiling.
    root = Path("/tmp") / f"arx-{invocation[:12]}-{index}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root


def _write_run_summary(
    context: _RunContext,
    *,
    passed: bool,
    changed: tuple[str, ...],
    run_reports: list[dict[str, object]],
) -> None:
    args = context.args
    write_json(
        context.reports / "summary.json",
        {
            "schema": "ar-ambient-role-chat-e2e-summary/v1",
            "status": "passed" if passed else "failed",
            "mode": args.mode,
            "diffBase": args.diff_base,
            "changedPaths": changed,
            "selectedPaths": context.selected,
            "candidate": context.candidate,
            "command": context.command,
            "fixtureInvocation": context.invocation,
            "runCount": RUN_COUNT,
            "retryCount": 0,
            "runs": [
                {
                    "run": report["run"],
                    "status": report["status"],
                    "report": f"run-{report['run']}.json",
                }
                for report in run_reports
            ],
        },
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("targeted", "full"), required=True)
    parser.add_argument("--diff-base", required=True)
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--source-selection", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    return parser.parse_args()


def _candidate_identity(repository: Path) -> dict[str, str]:
    repository = repository.resolve()
    with TemporaryDirectory(prefix="arspawn-e2e-candidate-index-") as temporary:
        scratch_root = Path(temporary).resolve()
        if scratch_root == repository or scratch_root.is_relative_to(repository):
            raise RuntimeError(
                "ambient-role candidate identity scratch must be outside the repository"
            )
        tree = worktree_candidate_tree(repository, scratch_root / "candidate.index")
    return {
        "commit": _git(repository, "rev-parse", "HEAD"),
        "tree": tree,
    }


def _residual_tmux(result: dict[str, object]) -> list[str]:
    raw = result.get("tmuxNames")
    names = raw if isinstance(raw, list) else []
    residual: list[str] = []
    for name in names:
        if not isinstance(name, str):
            continue
        probe = subprocess.run(
            ["tmux", "has-session", "-t", name],
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            residual.append(name)
    return residual


def _cleanup_passed(result: dict[str, object]) -> bool:
    cleanup = result.get("cleanup")
    return bool(isinstance(cleanup, dict) and cleanup.get("status") == "clean")


def _remove_run_root(
    root: Path,
    recorder: CheckpointRecorder,
) -> dict[str, str] | None:
    try:
        shutil.rmtree(root)
    except FileNotFoundError:
        return None
    except OSError as exc:
        error = {
            "type": type(exc).__name__,
            "message": f"disposable fixture root cleanup failed: {exc}",
            "traceback": traceback.format_exc(),
        }
        recorder.diagnostic("root-cleanup", error)
        return error
    return None


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
