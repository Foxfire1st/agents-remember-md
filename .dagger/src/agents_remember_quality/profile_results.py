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
    selection_results: dict[str, dict[str, object]] = dataclass_field(default_factory=dict)
    gate_catalog: list[dict[str, object]] = dataclass_field(default_factory=list)


@dataclass(frozen=True)
class GateOutcomes:
    """One gate's terminal facts packed behind a single argument."""

    applicability: str
    started: bool
    disposition: str
    rails: tuple
    rail_outcomes: dict[str, dict[str, object]] = dataclass_field(default_factory=dict)
    selector_outcomes: dict[str, dict[str, object]] = dataclass_field(default_factory=dict)


def gate_catalog_payload(
    gate_number: int,
    outcomes: GateOutcomes,
) -> dict[str, object]:
    """One typed aggregate gate entry with the complete terminal rail catalog.

    Every planned rail of a started gate gets one terminal record (pass, skipped,
    fail, or blocked); a gate that never started records zero starts and no rails.
    """
    catalog_rails: list[dict[str, object]] = []
    for rail in outcomes.rails:
        outcome = outcomes.rail_outcomes.get(rail.identity_key)
        if outcome is None:
            if not outcomes.started:
                continue
            raise ValueError(
                f"gate {gate_number} rail {rail.identity_key} lacks a terminal outcome"
            )
        catalog_rails.append(
            {
                "identity": rail.identity,
                "key": rail.identity_key,
                "gate": gate_number,
                "posture": rail.posture,
                **outcome,
            }
        )
    selectors = [
        {
            "selectorId": selector_id,
            **outcome,
        }
        for selector_id, outcome in sorted(outcomes.selector_outcomes.items())
    ]
    return {
        "gate": gate_number,
        "applicability": outcomes.applicability,
        "started": outcomes.started,
        "disposition": outcomes.disposition if outcomes.started else "not-run",
        "laterGatesZeroStart": outcomes.started and outcomes.disposition == "red",
        "zeroStart": not outcomes.started,
        "rails": catalog_rails,
        "selectors": selectors,
    }


def profile_result_payload(
    progress: QualityProgress,
    *,
    started_at: str,
    mode: str,
    attempt_nonce: str,
    gates: tuple[dict[str, object], ...] | None = None,
) -> dict[str, object]:
    """Build the repository-neutral terminal facts for one exact profile run.

    gates carries the complete typed per-gate rail catalog when the interpreter
    executed gate-by-gate: every applicable sibling rail reached a terminal status,
    and every later gate that never started is recorded with a zero-start fact.
    """

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
    if gates is not None:
        result["gates"] = list(gates)
    if progress.failure_details:
        result["failureDetails"] = progress.failure_details
    if progress.selection_results:
        result["selectionResults"] = [
            progress.selection_results[key] for key in sorted(progress.selection_results)
        ]
    return result


def quality_result_payload(
    progress: QualityProgress,
    *,
    started_at: str,
    mode: str,
    attempt_nonce: str,
    gates: tuple[dict[str, object], ...] | None = None,
) -> dict[str, object]:
    """Extend the neutral terminal result with Agents Remember profile evidence."""

    result = profile_result_payload(
        progress,
        started_at=started_at,
        mode=mode,
        attempt_nonce=attempt_nonce,
        gates=gates,
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
    "gate_catalog_payload",
    "profile_result_payload",
    "quality_result_payload",
]
