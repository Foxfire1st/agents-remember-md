"""Progress and terminal-result rendering for the repository Dagger adapter."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime

import dagger

BASELINE_CODEX_PROTOCOL = "initialize -> initialized -> thread/list"
AMBIENT_CODEX_PROTOCOL = (
    f"{BASELINE_CODEX_PROTOCOL}; real app-server MCP connected -> "
    "turn/start -> normally discovered MCP function calls"
)


@dataclass
class QualityProgress:
    """Mutable execution facts shared by the generic interpreter and result renderer."""

    container: dagger.Container
    exit_code: int
    attempted: list[str] = dataclass_field(default_factory=list)
    completed: list[str] = dataclass_field(default_factory=list)
    skipped: list[str] = dataclass_field(default_factory=list)
    step_exit_codes: dict[str, int] = dataclass_field(default_factory=dict)
    failure_details: dict[str, str] = dataclass_field(default_factory=dict)


def profile_result_payload(
    progress: QualityProgress,
    *,
    started_at: str,
    mode: str,
    attempt_nonce: str,
) -> dict[str, object]:
    """Build the repository-neutral terminal facts for one exact profile run."""

    failed_step = next(
        (
            step
            for step in reversed(progress.attempted)
            if progress.step_exit_codes.get(step, 0) != 0 and step not in progress.skipped
        ),
        None,
    )
    result: dict[str, object] = {
        "startedAt": started_at,
        "finishedAt": datetime.now(UTC).isoformat(),
        "mode": mode,
        "credentialsMounted": False,
        "containerSocketMounted": False,
        "attemptedSteps": progress.attempted,
        "completedSteps": progress.completed,
        "skippedSteps": progress.skipped,
        "failedStep": failed_step,
        "stepExitCodes": progress.step_exit_codes,
        "attemptNonce": attempt_nonce,
    }
    if progress.failure_details:
        result["failureDetails"] = progress.failure_details
    return result


def quality_result_payload(
    progress: QualityProgress,
    *,
    started_at: str,
    mode: str,
    attempt_nonce: str,
) -> dict[str, object]:
    """Extend the neutral terminal result with Agents Remember profile evidence."""

    result = profile_result_payload(
        progress,
        started_at=started_at,
        mode=mode,
        attempt_nonce=attempt_nonce,
    )
    e2e_completed = "ambient-role-chat-e2e" in progress.completed
    e2e_attempted = "ambient-role-chat-e2e" in progress.attempted
    e2e_skipped = "ambient-role-chat-e2e" in progress.skipped
    codex_attempted = e2e_attempted or "codex-read-only-probe" in progress.attempted
    result.update(
        {
            "codexProtocol": (
                AMBIENT_CODEX_PROTOCOL
                if e2e_completed
                else BASELINE_CODEX_PROTOCOL
                if "codex-read-only-probe" in progress.completed
                else None
            ),
            "promptSubmitted": (
                True if e2e_completed else None if e2e_attempted and not e2e_skipped else False
            ),
        }
    )
    if codex_attempted:
        result["codexMode"] = "real"
    if "causal-preflight" in progress.completed:
        result["causalFailureReport"] = "causal-failures.json"
        result["causalFailureSummary"] = "causal-failures.md"
    ambient_evidence = _ambient_evidence(
        e2e_completed=e2e_completed,
        e2e_skipped=e2e_skipped,
    )
    if ambient_evidence is not None:
        result["ambientRoleChatEvidence"] = ambient_evidence
    return result


def _ambient_evidence(
    *,
    e2e_completed: bool,
    e2e_skipped: bool,
) -> dict[str, object] | None:
    if e2e_completed:
        return {
            "status": "passed",
            "summary": "ambient-role-chat-e2e/summary.json",
            "runs": [
                "ambient-role-chat-e2e/run-1.json",
                "ambient-role-chat-e2e/run-2.json",
            ],
        }
    if e2e_skipped:
        return {
            "status": "skipped",
            "summary": "ambient-role-chat-e2e/summary.json",
        }
    return None


__all__ = [
    "AMBIENT_CODEX_PROTOCOL",
    "BASELINE_CODEX_PROTOCOL",
    "QualityProgress",
    "profile_result_payload",
    "quality_result_payload",
]
