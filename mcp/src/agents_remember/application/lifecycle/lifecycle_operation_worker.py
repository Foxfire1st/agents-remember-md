"""Application entry point for the canonical detached lifecycle runner."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from agents_remember.application.completion_cleanup import auto_complete_seats
from agents_remember.application.lifecycle.terminal_rail_failure import (
    terminal_worker_failure_result,
)
from agents_remember.application.worktree_services import (
    bind_worktree_services,
    build_default_worktree_services,
)
from agents_remember.kernel.authority import require_repo
from agents_remember.kernel.primitives.checkout_coordination import (
    declare_lifecycle_operation_process,
)
from agents_remember.kernel.primitives.gate_policy import (
    DecisionRole,
    GatePolicy,
    GatePolicyRule,
    make_gate_policy,
)
from agents_remember.kernel.primitives.gate_vocab import GateKind
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, load_config
from agents_remember.models.lifecycles.mutation_evidence import GitMutationEvidence
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    IntegrateOperationInput,
    IntegrationPublicationIntent,
    IntegrationQualityCertification,
    LifecycleOperationKind,
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
    OrganizationalCompletionRepairEvidence,
)
from agents_remember.worktrees.integration.closeout.ledger_recovery import (
    CloseoutLedgerRecoveryDecision,
)
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_generation_retained,
    derive_closeout_recovery_commits,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    located_lifecycle_operation_store,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.closeout import closeout_result
from agents_remember.worktrees.modules.integrate import integrate_result
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import load_contract

HEARTBEAT_SECONDS = 5.0
QUALITY_PROGRESS_REPORT = "quality-progress.json"


class OperationCancelled(RuntimeError):
    pass


def _stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _policy(operation_input: CloseoutOperationInput | IntegrateOperationInput) -> GatePolicy:
    rules = [
        GatePolicyRule(
            kind=cast(GateKind, rule.kind),
            delegated_role=cast(DecisionRole | None, rule.delegatedRole),
            require_reviewer_verdict=rule.requireReviewerVerdict,
        )
        for rule in operation_input.gatePolicy
    ]
    return make_gate_policy(rules)


class OperationRuntime:
    def __init__(self, store: LifecycleOperationStore, *, worker_lease: str | None = None) -> None:
        self.store = store
        self.stop = threading.Event()
        self.worker_lease = worker_lease

    def start(self) -> LifecycleOperationRecord:
        stamp = _stamp()
        worker_pid = os.getpid() if self.worker_lease is not None else None

        def running(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
            if self.worker_lease is None and record.workerPid is not None:
                raise RuntimeError(
                    "inline lifecycle execution cannot replace detached worker authority"
                )
            if self.worker_lease is not None:
                if record.workerLease != self.worker_lease:
                    raise RuntimeError("lifecycle worker lease does not match durable authority")
                if record.workerPid != worker_pid:
                    raise RuntimeError("lifecycle worker pid does not match durable authority")
            if record.status == "running":
                if self.worker_lease is not None and record.workerPid != worker_pid:
                    raise RuntimeError(
                        "lifecycle operation is already owned by another running worker"
                    )
                return record
            if record.status != "queued":
                return record
            if self.worker_lease is not None and record.workerPid != worker_pid:
                raise RuntimeError(
                    "queued lifecycle operation is reserved for another worker process"
                )
            recovering = (
                closeout_generation_retained(record)
                if record.operationKind == "closeout"
                else record.irreversibleBoundaryEntered
            )
            return record.model_copy(
                update={
                    "status": "running",
                    "phase": "recovering-after-claim" if recovering else "preflight",
                    "startedAt": record.startedAt or stamp,
                    "heartbeatAt": stamp,
                    "currentCommand": "recover task state"
                    if recovering
                    else "validate lifecycle operation",
                }
            )

        return self.store.update(running)

    def progress(self, phase: str, evidence: Mapping[str, object]) -> None:
        stamp = _stamp()
        recovery_value = evidence.get("recovery_commits")
        recovery_commits = (
            LifecycleOperationRecoveryCommits.model_validate(recovery_value)
            if recovery_value is not None
            else None
        )
        quality_value = evidence.get("quality_certification")
        quality_certification = (
            IntegrationQualityCertification.model_validate(quality_value)
            if quality_value is not None
            else None
        )
        publication_value = evidence.get("integration_publication")
        integration_publication = (
            IntegrationPublicationIntent.model_validate(publication_value)
            if publication_value is not None
            else None
        )
        repair_value = evidence.get("organizational_repair")
        organizational_repair = (
            OrganizationalCompletionRepairEvidence.model_validate(repair_value)
            if repair_value is not None
            else None
        )
        failure_value = evidence.get("organizational_failure")
        organizational_failure = (
            dict(failure_value)
            if isinstance(failure_value, Mapping)
            and failure_value.get("state") == "organizational-completion-gate-failed"
            else None
        )
        mutation_value = evidence.get("mutation_evidence")
        mutation_evidence = (
            GitMutationEvidence.model_validate(mutation_value)
            if mutation_value is not None
            else None
        )
        finalization_value = evidence.get("closeout_finalized_contract_sha256")
        if finalization_value is not None and not isinstance(finalization_value, str):
            raise RuntimeError("closeout finalized contract SHA-256 must be a string")

        def advance(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
            if record.cancelRequested or record.status == "cancelled":
                return record
            mutations = dict(record.mutationEvidence)
            if mutation_evidence is not None:
                mutations[mutation_evidence.leg] = mutation_evidence
            durable_recovery = (
                derive_closeout_recovery_commits(
                    record,
                    mutations=mutations,
                    reported=recovery_commits,
                )
                if record.operationKind == "closeout"
                else recovery_commits or record.recoveryCommits
            )
            return record.model_copy(
                update={
                    "status": "running",
                    "phase": phase,
                    "heartbeatAt": stamp,
                    "currentCommand": f"lifecycle stage: {phase}",
                    "irreversibleBoundaryEntered": (
                        record.irreversibleBoundaryEntered
                        or bool(evidence.get("irreversible_boundary"))
                        or (
                            mutation_evidence is not None
                            and mutation_evidence.state == "commit-proven"
                        )
                    ),
                    "approvalClaimed": (
                        record.approvalClaimed or bool(evidence.get("approval_claimed"))
                    ),
                    "mutationEvidence": mutations,
                    "recoveryCommits": durable_recovery,
                    "closeoutFinalizedContractSha256": (
                        record.closeoutFinalizedContractSha256
                        if finalization_value is None
                        else finalization_value
                    ),
                    "qualityCertification": (quality_certification or record.qualityCertification),
                    "integrationPublication": (
                        integration_publication or record.integrationPublication
                    ),
                    "organizationalRepair": (organizational_repair or record.organizationalRepair),
                    "result": organizational_failure or record.result,
                }
            )

        current = self.store.update(advance)
        if current.cancelRequested or current.status in {"cancelled", "termination-required"}:
            raise OperationCancelled("operation cancelled before irreversible boundary")
        print(f"phase={phase} command={current.currentCommand}", flush=True)

    def heartbeat(self) -> None:
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                current_command = self._quality_command()

                def beat(
                    record: LifecycleOperationRecord,
                    command_evidence: str | None = current_command,
                ) -> LifecycleOperationRecord:
                    if record.status != "running":
                        return record
                    return record.model_copy(
                        update={
                            "heartbeatAt": _stamp(),
                            "currentCommand": command_evidence or record.currentCommand,
                        }
                    )

                self.store.update(beat)
            except Exception as error:  # pragma: no cover - reported by terminal worker log
                print(f"heartbeat failed: {error}", flush=True)
                return

    def _quality_command(self) -> str | None:
        """Read the wrapper's atomic report without making it operation authority."""
        path = self.store.path.parent / QUALITY_PROGRESS_REPORT
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("status") != "running":
            return None
        step = payload.get("step")
        if step not in {"dagger", "complete", "failed"}:
            return None
        return f"quality stage: {step}"

    def finish(self, result: dict[str, object], *, ok: bool) -> None:
        stamp = _stamp()
        self.store.update(
            lambda record: terminal_operation_record(record, result, ok=ok, stamp=stamp)
        )

    def fail(self, error: Exception) -> None:
        current = self.store.read()
        pending = _organizational_repair_failure(current)
        if isinstance(error, CloseoutLedgerRecoveryDecision):
            decision = error.classification
            pending = {**decision.decision_payload(), "reason": decision.detail}
        if pending is None and current is not None:
            pending = terminal_worker_failure_result(
                operation_kind=current.operationKind,
                generation=current.generation,
                candidate_tree=current.candidateTree,
                error=error,
                reports_dir=self.store.path.parent.parent / "reports",
            )
        self.finish(
            pending
            or {
                "reason": "operation worker failed before publishing a typed domain result",
                "failure": public_failure_evidence(
                    stage="worker-execution",
                    side="operation",
                    name="accepted-generation",
                    error_type=type(error).__name__,
                    observed={"state": "failed"},
                ),
            },
            ok=False,
        )


def execute_operation(record: LifecycleOperationRecord, runtime: OperationRuntime) -> None:
    pending_repair = _organizational_repair_failure(record)
    if pending_repair is not None:
        runtime.finish(pending_repair, ok=False)
        return
    operation_input = record.input
    config = load_config(operation_input.configPath)
    current_contract = load_contract(Path(operation_input.contractPath))
    certification_profile = require_repo(
        config,
        current_contract.repo_name,
    ).certification_profile
    common = {
        "contract_path": Path(operation_input.contractPath),
        "certification_profile": certification_profile,
        "approved": True,
        "operation_key": record.operationKey,
        "operation_generation": record.generation,
        "candidate_tree": record.candidateTree,
        "approval_claimed": record.approvalClaimed,
        "recovery_commits": record.recoveryCommits,
        "quality_certification": record.qualityCertification,
        "integration_publication": record.integrationPublication,
        "operation_progress": runtime.progress,
    }
    if isinstance(operation_input, CloseoutOperationInput):
        args = WorktreeArgs(
            **common,
            gate_policy=_policy(operation_input),
            approval_note=operation_input.approvalNote,
            closeout_input=operation_input.effectiveInput,
        )
        result = closeout_result(args, current_contract)
        payload = {
            **result.payload,
            "ok": result.returncode == 0,
            "operation": "worktree_closeout_apply",
        }
    elif isinstance(operation_input, IntegrateOperationInput):
        args = WorktreeArgs(
            **common,
            gate_policy=_policy(operation_input),
            strategy=operation_input.strategy,
            ledger_commit_message=operation_input.ledgerCommitMessage,
        )
        result = integrate_result(args, current_contract)
        payload = integration_completion_payload(config, operation_input, result)
    else:
        raise RuntimeError("direct landing cannot execute through the detached worker")
    runtime.finish(payload, ok=result.returncode == 0)


def _organizational_repair_failure(
    record: LifecycleOperationRecord | None,
) -> dict[str, object] | None:
    if (
        record is None
        or record.organizationalRepair is None
        or not isinstance(record.result, dict)
        or record.result.get("state") != "organizational-completion-gate-failed"
    ):
        return None
    return dict(record.result)


def terminal_operation_record(
    record: LifecycleOperationRecord,
    result: dict[str, object],
    *,
    ok: bool,
    stamp: str,
) -> LifecycleOperationRecord:
    """Apply the terminal transition while preserving an accepted repair generation.

    Organizational repair is an already-published developer-decision contract. A later
    lower-level symptom cannot replace that contract with a payload that the durable schema
    rejects. Keeping this transition pure also gives the quality preflight one exact owner
    boundary to validate before its consumers execute.
    """

    if record.cancelRequested or record.status in {"cancelled", "termination-required"}:
        return record
    if ok:
        return record.model_copy(
            update={
                "status": "completed",
                "phase": "completed",
                "heartbeatAt": stamp,
                "finishedAt": stamp,
                "currentCommand": "operation completed",
                "result": result,
                "guidance": "Observe the task contract for the next lifecycle edge.",
                # The process is still executing this final stack frame. Status/control
                # reconciliation clears its binding only after observing actual exit.
            }
        )
    durable_result = _organizational_repair_failure(record) or result
    needs_recovery = (
        closeout_generation_retained(record)
        if record.operationKind == "closeout"
        else record.irreversibleBoundaryEntered and not bool(durable_result.get("safeToReplace"))
    )
    developer_decision = bool(durable_result.get("developerDecisionRequired"))
    needs_input = needs_recovery or developer_decision
    return record.model_copy(
        update={
            "status": "input-required" if needs_input else "failed",
            "phase": "contract-finalization" if needs_recovery else "failed",
            "heartbeatAt": stamp,
            "finishedAt": None if needs_input else stamp,
            "currentCommand": "reconcile the same operation"
            if needs_recovery
            else "operation failed",
            "result": durable_result,
            "failure": str(
                durable_result.get("reason") or durable_result.get("summary") or durable_result
            ),
            "guidance": (
                "Resolve the exact developer-decision evidence without replacing this "
                "generation; status will advertise recovery only after the accepted or "
                "intended state is restored."
                if developer_decision
                else (
                    "Restart this exact task operation; its consumed approval remains bound "
                    "to the same internal fingerprint and recovery will not replay a different "
                    "mutation."
                    if needs_recovery
                    else "Fix the reported preflight failure, then restart this task operation."
                )
            ),
            # Exit proof and Git proof are independent. Retain this process binding
            # until a task-addressed observer proves the process instance exited.
        }
    )


def integration_completion_payload(
    config: McpRuntimeConfig,
    operation_input: IntegrateOperationInput,
    result: WorktreeCommandResult,
) -> dict[str, object]:
    """Apply the integration edge's completion cleanup inside the detached owner."""
    payload: dict[str, object] = {
        **result.payload,
        "ok": result.returncode == 0,
        "operation": "worktree_integrate",
    }
    if result.returncode == 0 and operation_input.autoCompleteSeats:
        payload.update(
            auto_complete_seats(
                config,
                Path(operation_input.contractPath),
                reason="auto-close: leaf integrated into master",
                edge="leaf-integration",
            )
        )
    return payload


def run_worker(contract_path: Path, kind: LifecycleOperationKind, worker_lease: str) -> int:
    contract = load_contract(contract_path)
    store = located_lifecycle_operation_store(contract, kind)
    current = store.read()
    if current is None or current.operationKind != kind:
        raise RuntimeError(f"no {kind} operation is queued for {contract.task_name}")
    if current.status in {"cancelled", "completed"}:
        return 0
    deadline = datetime.now(UTC).timestamp() + 5.0
    while current.workerLease != worker_lease and datetime.now(UTC).timestamp() < deadline:
        threading.Event().wait(0.01)
        current = store.read() or current
        if current.status in {"cancelled", "completed"}:
            return 0
    runtime = OperationRuntime(store, worker_lease=worker_lease)
    current = runtime.start()
    if current.status in {"cancelled", "completed"}:
        return 0
    if current.status != "running":
        raise RuntimeError(f"{kind} worker cannot start from durable state {current.status!r}")
    heartbeat = threading.Thread(target=runtime.heartbeat, daemon=True)
    heartbeat.start()
    try:
        execute_operation(current, runtime)
    except OperationCancelled:
        return 0
    except Exception as error:
        print(f"operation failed: {error}", flush=True)
        runtime.fail(error)
        return 1
    finally:
        runtime.stop.set()
        heartbeat.join(timeout=HEARTBEAT_SECONDS)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resume one durable task lifecycle operation")
    parser.add_argument("--contract-path", type=Path, required=True)
    parser.add_argument("--kind", choices=("closeout", "integrate"), required=True)
    parser.add_argument("--worker-lease", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    declare_lifecycle_operation_process()
    bind_worktree_services(build_default_worktree_services())
    return run_worker(
        args.contract_path,
        cast(LifecycleOperationKind, args.kind),
        args.worker_lease,
    )


if __name__ == "__main__":
    sys.exit(main())
