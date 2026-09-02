from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

from agents_remember.controlplane.enforcement import GateGuard, evaluate_gate
from agents_remember.controlplane.integration_authority_lock import integration_authority_lock
from agents_remember.controlplane.records import GateRecord
from agents_remember.controlplane.store import GateStore
from agents_remember.kernel.primitives.gate_policy import (
    GatePolicy,
)
from agents_remember.kernel.primitives.observer_paths import observer_logs_root
from agents_remember.models.lifecycles.operation import (
    IntegrationOperationAuthority,
    IntegrationPublicationIntent,
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.worktrees.integration.atomic_series_landing import (
    AtomicLandingBlocked,
    require_atomic_landing_authority,
)
from agents_remember.worktrees.integration.integration_branch_authority import (
    integration_targets,
    require_ordinary_worktree,
    require_series_contract_authority,
)
from agents_remember.worktrees.integration.integration_claim_transfer import (
    prove_recovery_publication_authority,
    transfer_and_publish_integration_claim,
)
from agents_remember.worktrees.integration.integration_operation_authority import (
    require_current_integration_sources,
    require_plane_integration_operation,
)
from agents_remember.worktrees.integration.integration_publication_fence import (
    IntegrationDoorAuthorityConflict,
    classify_integration_door_authority,
    integration_door_decision_payload,
)
from agents_remember.worktrees.integration.integration_quality import (
    INTEGRATION_QUALITY_DECISION_SURFACE,
    IntegrationQualityFailure,
    organizational_quality_failure_payload,
    run_integration_quality_gate,
)
from agents_remember.worktrees.integration.integration_quality import (
    quality_gate_preview as _quality_gate_preview,
)
from agents_remember.worktrees.integration.integration_ref_state import (
    IntegrationRefDecisionError,
    IntegrationRefPublicationInterrupted,
    IntegrationRefState,
    classify_integration_authority_refs,
)
from agents_remember.worktrees.integration.integration_ref_transaction import (
    CheckoutRefresh,
    IntegratedCommits,
    IntegrationRefRace,
    IntegrationSources,
    merge_integrated_commits,
    prepare_integration_ref_move,
    recover_integration_ref,
    refresh_recovered_checkout,
    require_integrated_ledger_mapping,
)
from agents_remember.worktrees.integration.integration_resolution_handoff import (
    integration_resolution_required,
)
from agents_remember.worktrees.integration.organizational_completion_integration import (
    IntegrationBoundaryFacts,
    prepare_integration_publication_intent,
    preview_integration_boundary,
    recorded_organizational_quality_certification,
)
from agents_remember.worktrees.integration.organizational_completion_repair import (
    record_organizational_completion_repair,
)
from agents_remember.worktrees.modules.args import WorktreeArgs, report_operation_progress
from agents_remember.worktrees.modules.git import (
    branch_commit,
    current_branch,
    head_commit,
    is_ancestor,
    require_clean,
)
from agents_remember.worktrees.modules.guidance import (
    contract_next_args,
    next_guidance,
    status_payload,
)
from agents_remember.worktrees.modules.integration_preflight_results import (
    atomic_landing_blocked_result,
    prepared_integration_recovery,
)
from agents_remember.worktrees.modules.integration_publication import (
    IntegratePreview,
    IntegrationPublication,
    protected_integration_decision,
    publish_journaled_organizational_completion,
)
from agents_remember.worktrees.modules.integration_recovery import (
    classify_convergent_recovery_refs,
    prove_external_memory_recovery,
)
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.series_closeout import (
    atomic_series_ledger_prefix,
    publish_series_integration_under_authority,
)
from agents_remember.worktrees.source_lineage import (
    lineage_block_payload,
    lineage_refusal,
    source_lineage_for_contract,
)
from agents_remember.worktrees.worktree_contract import (
    ContractCells,
    WorktreeContract,
    amend_contract,
    load_contract,
    write_contract,
)


@dataclass(frozen=True)
class _ExternalRefRecovery:
    recovered_commits: IntegratedCommits
    authority: IntegrationOperationAuthority
    commits: LifecycleOperationRecoveryCommits


HANDOVER_GATE_KIND = "master-handover-approval"


def handover_gate_guard(
    gates: Mapping[str, GateRecord],
    *,
    task_name: str,
    parent_task_name: str,
    policy: GatePolicy,
) -> GateGuard:
    """The master-exit seam verdict for one integrating contract. Pure.

    The handover gate carries the MASTER identity: the manager raises it with
    ``enclosure=<master task name>`` on its own (worktree-less) lifecycle, while
    the master -> super integration runs on the orchestrator's integration
    worktree -- a different lifecycle -- so the fold must be cross-lifecycle
    (:meth:`GateStore.all_current`) and the address is the contract's master or
    series name, never ``contract.lifecycle_id``. Only gates whose ``enclosure``
    matches the contract's ``task_name`` or ``parent_task_name`` govern; the
    latest matching snapshot decides via :func:`evaluate_gate` (open or
    policy-invalid blocks). Gateless stays additive: with no matching gate the
    existing approval channel governs.
    """
    addresses = {name for name in (task_name, parent_task_name) if name}
    matching = {
        gate_id: gate
        for gate_id, gate in gates.items()
        if gate.kind == HANDOVER_GATE_KIND and gate.enclosure in addresses
    }
    return evaluate_gate(matching, kind=HANDOVER_GATE_KIND, policy=policy)


def unmatched_handover_gate_warning(
    gates: Mapping[str, GateRecord],
    *,
    task_name: str,
    parent_task_name: str,
) -> dict[str, object] | None:
    """The enclosure spelling-check for a gateless integrate. Pure.

    The seam address is an exact-string convention (``enclosure`` = master task
    name), so a mis-spelled address yields a gate :func:`handover_gate_guard`
    can never match -- gateless-permitted, silently. When NO gate in the fold
    addresses this contract but open master-handover-approval gates do exist,
    integration still proceeds (gateless stays additive -- another master's
    open gate is legitimate) and the result payload carries this warning, so a
    mis-addressed gate is loud at the exact moment it would have mattered.
    With a matching gate (any state) the address worked and other masters'
    in-flight gates are not worth a warning: ``None``.
    """
    addresses = {name for name in (task_name, parent_task_name) if name}
    handover_gates = [gate for gate in gates.values() if gate.kind == HANDOVER_GATE_KIND]
    if any(gate.enclosure in addresses for gate in handover_gates):
        return None
    unmatched = sorted(
        (gate for gate in handover_gates if gate.state == "open"),
        key=lambda gate: gate.id,
    )
    if not unmatched:
        return None
    return {
        "unmatched_open_gates": [
            {"gateId": gate.id, "enclosure": gate.enclosure} for gate in unmatched
        ],
        "note": (
            "open master-handover-approval gates exist but none address this master "
            "(task_name/parent_task_name); verify the enclosure spelling"
        ),
    }


def blocked_integration_payload(
    contract: WorktreeContract,
    state: str,
    reason: str,
    persist: bool = True,
    developer_decision_required: bool = True,
    **extra: object,
) -> dict[str, object]:
    blocked = amend_contract(contract, ContractCells(integration_status="blocked"))
    if persist:
        write_contract(blocked.contract_path, blocked)
    next_step: dict[str, object] = {"summary": reason}
    for key in ("nextOperation", "nextTool", "nextArgs"):
        if key in extra:
            next_step[key] = extra[key]
    return {
        "state": state,
        **status_payload(blocked),
        "reason": reason,
        "summary": reason,
        "developerDecisionRequired": developer_decision_required,
        "nextStep": next_step,
        **extra,
    }


def validate_integrate_contract(contract: WorktreeContract) -> None:
    if contract.closeout_status != "completed":
        raise RuntimeError("integration requires closeout.status completed")
    if not contract.approved_for_commit:
        raise RuntimeError("integration requires approved closeout")
    if not contract.code_commit:
        raise RuntimeError("integration requires closeout code_commit")
    if contract.kind == "series":
        if (
            branch_commit(contract.code_repo_path, contract.code_work_branch)
            != contract.code_commit
        ):
            raise RuntimeError("atomic code ref does not match closeout code_commit")
    else:
        if not contract.code_worktree.exists():
            raise RuntimeError(f"code worktree does not exist: {contract.code_worktree}")
        if current_branch(contract.code_worktree) != contract.code_work_branch:
            raise RuntimeError(f"code worktree must have {contract.code_work_branch} checked out")
        require_clean(contract.code_worktree, "code worktree")
        if head_commit(contract.code_worktree) != contract.code_commit:
            raise RuntimeError("code worktree HEAD does not match closeout code_commit")
    if contract.memory_mode == "external":
        validate_integrate_memory_contract(contract)


def validate_integrate_memory_contract(contract: WorktreeContract) -> None:
    if contract.memory_repo_path is None or contract.ledger_path is None:
        raise RuntimeError("external-memory integration requires memory repo and ledger path")
    if not contract.memory_content_commit or not contract.ledger_commit:
        raise RuntimeError(
            "external-memory integration requires closeout memory_content_commit and ledger_commit"
        )
    if contract.kind == "series":
        if (
            branch_commit(contract.memory_repo_path, contract.memory_work_branch)
            != contract.ledger_commit
        ):
            raise RuntimeError("atomic memory ref does not match closeout ledger_commit")
        return
    if contract.memory_worktree is None:
        raise RuntimeError("external-memory leaf integration requires a memory worktree")
    if current_branch(contract.memory_worktree) != contract.memory_work_branch:
        raise RuntimeError(f"memory worktree must have {contract.memory_work_branch} checked out")
    require_clean(contract.memory_worktree, "memory worktree")
    if head_commit(contract.memory_worktree) != contract.ledger_commit:
        raise RuntimeError("memory worktree HEAD does not match closeout ledger_commit")


def _integration_lineage_block(
    contract: WorktreeContract, *, persist: bool
) -> WorktreeCommandResult | None:
    projection = source_lineage_for_contract(contract)
    refusal = lineage_refusal(projection)
    if refusal is None:
        return None
    assert projection is not None
    status, reason = refusal
    recovery = lineage_block_payload(projection)
    recovery.pop("state", None)
    recovery.pop("summary", None)
    return WorktreeCommandResult(
        2,
        blocked_integration_payload(
            contract,
            status,
            f"integration requires current transitive source lineage: {reason}",
            persist=persist,
            developer_decision_required=False,
            **recovery,
        ),
    )


def _integration_sources_moved_block(
    contract: WorktreeContract, sources: IntegrationSources
) -> WorktreeCommandResult | None:
    current_code = branch_commit(contract.code_repo_path, contract.code_source_branch)
    moved = current_code != sources.current_code_source
    current_memory = ""
    if contract.memory_mode == "external":
        assert contract.memory_repo_path is not None
        current_memory = branch_commit(contract.memory_repo_path, contract.memory_source_branch)
        moved = moved or current_memory != sources.current_memory_source
    if not moved:
        return None
    return WorktreeCommandResult(
        2,
        blocked_integration_payload(
            contract,
            "source-moved-during-quality",
            "integration source branches moved while the quality gate ran; retry from "
            "preflight so the combined candidate is certified against the new tips",
            developer_decision_required=False,
            nextOperation="request_integration_decision",
            nextTool="worktree_integrate",
            nextArgs={"contract_path": contract.contract_path.as_posix(), "dry_run": True},
        ),
    )


def _integration_source_state_block(
    contract: WorktreeContract, sources: IntegrationSources
) -> WorktreeCommandResult | None:
    """Re-prove transitive ancestry and the exact source-tip snapshot."""
    return _integration_sources_moved_block(contract, sources) or _integration_lineage_block(
        contract, persist=True
    )


def _integration_replay_requirements(contract: WorktreeContract) -> IntegrationSources:
    current_code_source = branch_commit(contract.code_repo_path, contract.code_source_branch)
    current_memory_source = ""
    code_replay_required = not is_ancestor(
        contract.code_repo_path, current_code_source, contract.code_commit
    )
    memory_replay_required = False
    if contract.memory_mode == "external":
        assert contract.memory_repo_path is not None
        current_memory_source = branch_commit(
            contract.memory_repo_path, contract.memory_source_branch
        )
        memory_replay_required = not is_ancestor(
            contract.memory_repo_path, current_memory_source, contract.ledger_commit
        )
    return IntegrationSources(
        current_code_source=current_code_source,
        current_memory_source=current_memory_source,
        code_replay_required=code_replay_required,
        memory_replay_required=memory_replay_required,
    )


def _blocked_non_ff_result(
    contract: WorktreeContract,
    args: WorktreeArgs,
    sources: IntegrationSources,
) -> WorktreeCommandResult:
    return WorktreeCommandResult(
        2,
        blocked_integration_payload(
            contract,
            "blocked-non-ff",
            "source branch moved; rerun with --strategy replay after reviewing parallel changes",
            persist=not args.dry_run,
            code_replay_required=sources.code_replay_required,
            memory_replay_required=sources.memory_replay_required,
        ),
    )


def _dry_run_result(
    contract: WorktreeContract,
    args: WorktreeArgs,
    sources: IntegrationSources,
    *,
    preview: IntegratePreview,
) -> WorktreeCommandResult:
    # The preview EVALUATES (never enforces) the seam guard, so the c-09-mandated
    # dry_run preflight cannot promise "would-integrate" and then have the real run
    # refuse with handover-gate-blocked. Nothing on this path persists a contract
    # mutation.
    summary = (
        "Dry run completed; integration preflight can proceed with the selected strategy."
        if preview.guard.permitted
        else "Dry run completed; the real run would refuse with handover-gate-blocked — "
        "decide the addressed master-handover-approval gate first."
    )
    payload: dict[str, object] = {
        "state": "would-integrate",
        **status_payload(contract),
        "summary": summary,
        **next_guidance(
            "request_integration_decision",
            tool="worktree_integrate",
            args=contract_next_args(
                contract,
                strategy=args.strategy,
                ledger_commit_message=args.ledger_commit_message,
                dry_run=False,
            ),
        ),
        "strategy": args.strategy,
        "code_replay_required": sources.code_replay_required,
        "memory_replay_required": sources.memory_replay_required,
        "handover_gate": {
            "permitted": preview.guard.permitted,
            "gateId": preview.guard.gate_id,
            "reason": preview.guard.reason,
        },
        "quality_gate": preview.quality_gate,
        "cleanup_question": "After successful integration, ask whether to remove the code and memory worktrees plus merged local task branches.",
    }
    if preview.handover_warning is not None:
        payload["handover_gate_warning"] = preview.handover_warning
    return WorktreeCommandResult(0, payload)


def _integrated_code_commit(
    contract: WorktreeContract, current_code_source: str
) -> tuple[str, dict[str, object] | None]:
    integrated_code_commit = contract.code_commit
    if not is_ancestor(contract.code_repo_path, current_code_source, integrated_code_commit):
        raise RuntimeError(
            "integrated code commit is not a fast-forward from the current code source branch"
        )
    return integrated_code_commit, None


def _integrated_memory_commits(
    contract: WorktreeContract,
    current_memory_source: str,
) -> tuple[str, str, dict[str, object] | None]:
    integrated_memory_content_commit = contract.memory_content_commit
    integrated_ledger_commit = contract.ledger_commit
    if contract.memory_mode == "external":
        assert contract.memory_repo_path is not None
        if not is_ancestor(
            contract.memory_repo_path, current_memory_source, integrated_ledger_commit
        ):
            raise RuntimeError(
                "integrated memory ledger commit is not a fast-forward from the current memory source branch"
            )
    return integrated_memory_content_commit, integrated_ledger_commit, None


def _integrated_result(
    contract: WorktreeContract,
    args: WorktreeArgs,
    commits: IntegratedCommits,
    *,
    handover_warning: dict[str, object] | None,
    quality_gate: dict[str, object],
) -> WorktreeCommandResult:
    updated = amend_contract(
        replace(
            contract,
            integration_strategy=args.strategy,
            integrated_code_commit=commits.code,
            integrated_memory_content_commit=commits.memory_content,
            integrated_ledger_commit=commits.ledger,
        ),
        ContractCells(integration_status="completed", cleanup="pending"),
    )
    write_contract(contract.contract_path, updated)
    payload: dict[str, object] = {
        "state": "integrated",
        **status_payload(updated),
        "summary": "Integration completed; ask the developer whether to clean up worktrees and merged local branches.",
        "strategy": args.strategy,
        "integrated_code_commit": commits.code,
        "integrated_memory_content_commit": commits.memory_content,
        "integrated_ledger_commit": commits.ledger,
        "quality_gate": quality_gate,
        "cleanup_question": "Integration completed. Remove the code and memory worktrees plus merged local task branches now?",
    }
    if handover_warning is not None:
        payload["handover_gate_warning"] = handover_warning
    return WorktreeCommandResult(0, payload)


def _recover_landed_refs(
    contract: WorktreeContract,
    args: WorktreeArgs,
    commits: LifecycleOperationRecoveryCommits,
    authority: IntegrationOperationAuthority,
) -> bool:
    """Finish or prove the exact named-ref transaction after an abrupt worker death."""

    if contract.memory_mode != "external" and (commits.memoryContentCommit or commits.ledgerCommit):
        raise RuntimeError(
            "integration recovery recorded external-memory commits for an internal-memory contract"
        )
    facts = classify_integration_authority_refs(authority, commits)
    if facts.state == "unchanged":
        return False
    if facts.state == "conflict":
        raise IntegrationRefDecisionError(facts)
    code_source = facts.object_id("codeRef")
    if code_source is None:
        raise IntegrationRefDecisionError(facts)
    code_before = authority.codeSourceCommit
    code_after = commits.codeCommit
    if contract.memory_mode != "external":
        if code_source != code_after:
            raise RuntimeError(
                "integration recovery found an unowned code ref value: "
                f"{code_source} (expected {code_before} or {code_after})"
            )
        refresh_recovered_checkout(
            contract,
            args,
            IntegratedCommits(code=code_after, memory_content="", ledger=""),
            CheckoutRefresh(side="code", old=code_before, new=code_after),
        )
        return True

    return _recover_external_landed_refs(
        contract,
        args,
        commits,
        authority,
        facts,
    )


def _recover_external_landed_refs(
    contract: WorktreeContract,
    args: WorktreeArgs,
    commits: LifecycleOperationRecoveryCommits,
    authority: IntegrationOperationAuthority,
    facts: IntegrationRefState,
) -> bool:
    code_source = facts.object_id("codeRef")
    memory_source = facts.object_id("memoryRef")
    if code_source is None or memory_source is None:
        raise IntegrationRefDecisionError(facts)
    code_before = authority.codeSourceCommit
    code_after = commits.codeCommit
    if contract.memory_repo_path is None:
        raise RuntimeError("external-memory integration recovery requires a memory repo")
    memory_before = authority.memorySourceCommit
    memory_after = commits.ledgerCommit
    recovered_commits = IntegratedCommits(
        code=code_after,
        memory_content=commits.memoryContentCommit,
        ledger=memory_after,
    )
    recovery = _ExternalRefRecovery(recovered_commits, authority, commits)
    if code_source == code_after and memory_source in {memory_before, memory_after}:
        require_integrated_ledger_mapping(
            contract,
            recovered_commits,
            memory_source_commit=authority.memorySourceCommit,
            expected_series_prefix=(
                atomic_series_ledger_prefix(contract)
                if contract.kind == "series" and contract.memory_mode == "external"
                else ()
            ),
        )
    if code_source == code_after and memory_source == memory_before:
        memory_source = _recover_external_ref(
            contract,
            args,
            recovery,
            side="memory",
            intended=memory_after,
        )
    if code_source == code_before and memory_source == memory_after:
        code_source = _recover_external_ref(
            contract,
            args,
            recovery,
            side="code",
            intended=code_after,
        )
    live = classify_convergent_recovery_refs(authority, commits)
    if live.object_id("codeRef") != code_after or live.object_id("memoryRef") != memory_after:
        raise IntegrationRefPublicationInterrupted(live)
    refresh_recovered_checkout(
        contract,
        args,
        recovered_commits,
        CheckoutRefresh(side="code", old=code_before, new=code_after),
    )
    refresh_recovered_checkout(
        contract,
        args,
        recovered_commits,
        CheckoutRefresh(side="memory", old=memory_before, new=memory_after),
    )
    return True


def _recover_external_ref(
    contract: WorktreeContract,
    args: WorktreeArgs,
    recovery: _ExternalRefRecovery,
    *,
    side: Literal["code", "memory"],
    intended: str,
) -> str:
    if recover_integration_ref(contract, args, recovery.recovered_commits, side=side):
        return intended
    live = classify_convergent_recovery_refs(recovery.authority, recovery.commits)
    if live.object_id(f"{side}Ref") != intended:
        raise IntegrationRefPublicationInterrupted(live)
    return intended


def _prove_integration_recovery_commits(
    contract: WorktreeContract,
    args: WorktreeArgs,
    commits: LifecycleOperationRecoveryCommits,
    authority: IntegrationOperationAuthority,
) -> IntegratedCommits | WorktreeCommandResult | None:
    """Prove a wholly landed source pair, or permit an untouched retry."""
    try:
        moved = _recover_landed_refs(contract, args, commits, authority)
    except IntegrationRefDecisionError as exc:
        return WorktreeCommandResult(2, exc.classification.decision_payload())
    except IntegrationRefPublicationInterrupted as exc:
        return WorktreeCommandResult(2, exc.classification.interruption_payload())
    if not moved:
        return None

    if contract.kind == "series":
        task_code_head = branch_commit(contract.code_repo_path, contract.code_work_branch)
    else:
        require_clean(contract.code_worktree, "recovering integration code worktree")
        task_code_head = head_commit(contract.code_worktree)
    if task_code_head != commits.codeCommit:
        raise RuntimeError(
            "integration contract-finalization recovery requires manual reconciliation: "
            f"recorded code commit {commits.codeCommit}, found task HEAD {task_code_head}"
        )
    if contract.memory_mode == "external":
        prove_external_memory_recovery(contract, commits)
    return IntegratedCommits(
        code=commits.codeCommit,
        memory_content=commits.memoryContentCommit,
        ledger=commits.ledgerCommit,
    )


def _recover_integration_finalization(
    contract: WorktreeContract,
    args: WorktreeArgs,
    authority: IntegrationOperationAuthority,
) -> WorktreeCommandResult | None:
    commits = args.recovery_commits
    if commits is None:
        return None
    if contract.integration_status == "completed":
        _prove_completed_integration_descendant(contract, commits, authority)
        return WorktreeCommandResult(
            0,
            {"state": "already-integrated", "recovered": True, **status_payload(contract)},
        )
    proven = _prove_integration_recovery_commits(contract, args, commits, authority)
    if proven is None:
        return None
    if isinstance(proven, WorktreeCommandResult):
        return proven
    result = _integrated_result(
        contract,
        args,
        proven,
        handover_warning=None,
        quality_gate={
            "status": "recovered-contract-finalization",
            "passed": True,
            "reason": "the exact accepted commit set was proven from Git and the ledger",
        },
    )
    result.payload["recovered"] = True
    return result


def _prove_completed_integration_descendant(
    contract: WorktreeContract,
    commits: LifecycleOperationRecoveryCommits,
    authority: IntegrationOperationAuthority,
) -> None:
    expected = (commits.codeCommit, commits.memoryContentCommit, commits.ledgerCommit)
    contract_commits = (
        contract.integrated_code_commit,
        contract.integrated_memory_content_commit,
        contract.integrated_ledger_commit,
    )
    authority_commits = (
        authority.codeCandidateCommit,
        authority.memoryContentCommit,
        authority.ledgerCommit,
    )
    if contract_commits != expected or authority_commits != expected:
        raise RuntimeError(
            "completed integration contract does not match its recorded recovery authority"
        )
    targets = {target.side: target for target in integration_targets(contract)}
    code_target = targets["code"]
    code_tip = branch_commit(contract.code_repo_path, code_target.branch)
    if not is_ancestor(contract.code_repo_path, commits.codeCommit, code_tip):
        raise RuntimeError("completed integration commit is not reachable from the current target")
    if contract.memory_mode != "external":
        return
    assert contract.memory_repo_path is not None
    memory_target = targets["memory"]
    memory_tip = branch_commit(contract.memory_repo_path, memory_target.branch)
    if not is_ancestor(contract.memory_repo_path, commits.ledgerCommit, memory_tip):
        raise RuntimeError(
            "completed integration ledger is not reachable from the current memory target"
        )
    require_integrated_ledger_mapping(
        contract,
        IntegratedCommits(
            code=commits.codeCommit,
            memory_content=commits.memoryContentCommit,
            ledger=commits.ledgerCommit,
        ),
        memory_source_commit=authority.memorySourceCommit,
        expected_series_prefix=(
            atomic_series_ledger_prefix(contract)
            if contract.kind == "series" and contract.memory_mode == "external"
            else ()
        ),
    )


def integrate_result(
    args: WorktreeArgs,
    current_contract: WorktreeContract,
) -> WorktreeCommandResult:
    report_operation_progress(args, "preflight", current_command="validate integration eligibility")
    if not args.approved and not args.dry_run:
        raise RuntimeError("integration requires --approved after human review")
    assert args.contract_path is not None
    contract = current_contract
    if args.contract_path.resolve() != contract.contract_path.resolve():
        raise RuntimeError("integration contract path does not match the passed current contract")
    if contract.kind == "series":
        require_series_contract_authority(contract, operation="worktree_integrate")
    else:
        require_ordinary_worktree(contract, operation="worktree_integrate")
    integration_targets(contract)
    operation = None
    if not args.dry_run:
        operation = require_plane_integration_operation(contract, args)
    completed = _completed_integration_result(contract, args, operation)
    if completed is not None:
        return completed
    door_block = _integration_door_block(
        contract,
        operation.integrationPublication if operation is not None else None,
    )
    if door_block is not None:
        return door_block
    validate_integrate_contract(contract)
    sources = _integration_replay_requirements(contract)
    operation = None
    if not args.dry_run:
        operation = require_current_integration_sources(
            contract,
            args,
            code_source_commit=sources.current_code_source,
            memory_source_commit=sources.current_memory_source,
        )
    return _continue_integration(contract, args, sources, operation)


def _completed_integration_result(
    contract: WorktreeContract,
    args: WorktreeArgs,
    operation: LifecycleOperationRecord | None,
) -> WorktreeCommandResult | None:
    recovered = None
    if operation is not None and operation.recoveryCommits is not None:
        if operation.integrationAuthority is None:
            raise RuntimeError(
                "completed integration recovery has no immutable integration authority"
            )
        if args.recovery_commits != operation.recoveryCommits:
            raise RuntimeError(
                "integration recovery input does not match the durable operation record"
            )
        recovered = _recover_integration_under_authority(
            contract,
            args,
            operation.integrationAuthority,
        )
    completed = recovered
    if completed is None and contract.integration_status == "completed":
        if not args.dry_run:
            raise RuntimeError(
                "completed integration requires exact durable recovery evidence before "
                "journal finalization"
            )
        completed = WorktreeCommandResult(
            0,
            {"state": "already-integrated", **status_payload(contract)},
        )
    return completed


def _recover_integration_under_authority(
    contract: WorktreeContract,
    args: WorktreeArgs,
    authority: IntegrationOperationAuthority,
) -> WorktreeCommandResult | None:
    intent = args.integration_publication
    if intent is None:
        raise RuntimeError("integration recovery has no journaled publication intent")
    commits = (
        authority.codeCandidateCommit,
        authority.memoryContentCommit,
        authority.ledgerCommit,
    )
    try:
        intent = prove_recovery_publication_authority(
            contract,
            args,
            intent,
            commits=commits,
        )
    except IntegrationDoorAuthorityConflict as error:
        return WorktreeCommandResult(2, integration_door_decision_payload(error.evidence))

    def publication() -> WorktreeCommandResult | None:
        current = load_contract(contract.contract_path)
        if current != contract and contract.integration_status != "completed":
            raise RuntimeError("integration contract changed before recovery finalization")
        decision = protected_integration_decision(current, args)
        if decision is not None:
            return decision
        if current.integration_status != "completed":
            require_atomic_landing_authority(current)
        result = _recover_integration_finalization(current, args, authority)
        return publish_journaled_organizational_completion(result, intent)

    try:
        if contract.kind == "series":
            if contract.integration_status != "completed":
                return publish_series_integration_under_authority(contract, publication)
            with integration_authority_lock(contract.coordination_root, contract.repo_name):
                return publication()
        with integration_authority_lock(contract.coordination_root, contract.repo_name):
            return publication()
    except AtomicLandingBlocked as error:
        return atomic_landing_blocked_result(contract, error)


def _continue_integration(
    contract: WorktreeContract,
    args: WorktreeArgs,
    sources: IntegrationSources,
    operation: LifecycleOperationRecord | None,
) -> WorktreeCommandResult:
    if args.strategy == "ff-only" and sources.replay_required:
        return _blocked_non_ff_result(contract, args, sources)
    if args.strategy == "replay" and sources.replay_required:
        return integration_resolution_required(contract, args, sources, operation)
    lineage_block = _integration_lineage_block(contract, persist=not args.dry_run)
    if lineage_block is not None:
        return lineage_block
    return _handover_or_apply_integration(contract, args, sources)


def _handover_or_apply_integration(
    contract: WorktreeContract,
    args: WorktreeArgs,
    sources: IntegrationSources,
) -> WorktreeCommandResult:
    # The master-exit seam consumer (mirror of the closeout gate): when a
    # master-handover-approval gate is addressed to this contract's master or
    # series (its `enclosure`), only a policy-valid approval lets the
    # integration proceed. The fold is cross-lifecycle because the raiser
    # (the manager) and the integrator anchor different lifecycles.
    # Gateless stays additive. The guard is EVALUATED for both runs — the
    # dry-run preview reports it instead of enforcing it — and the
    # unmatched-open-gate warning keeps a mis-addressed enclosure (an exact
    # string that would otherwise fail open) loud on the result payload.
    gate_store = GateStore(observer_logs_root(contract.coordination_root))
    gate_fold = gate_store.all_current()
    guard = handover_gate_guard(
        gate_fold,
        task_name=contract.task_name,
        parent_task_name=contract.parent_task_name,
        policy=args.gate_policy,
    )
    handover_warning = unmatched_handover_gate_warning(
        gate_fold,
        task_name=contract.task_name,
        parent_task_name=contract.parent_task_name,
    )
    if not args.dry_run and not guard.permitted:
        summary = (
            "Integration is blocked by the addressed master-handover-approval gate; "
            "inspect the structural gate and decide it before rerunning integration."
        )
        guidance = {
            "nextOperation": "review_handover_gate",
            "nextTool": "gate_list",
            "nextArgs": {},
        }
        return WorktreeCommandResult(
            2,
            {
                "state": "handover-gate-blocked",
                "gateId": guard.gate_id,
                "reason": guard.reason,
                **status_payload(contract),
                "summary": summary,
                **guidance,
                "nextStep": {"summary": summary, **guidance},
            },
        )

    if args.dry_run:
        return _dry_run_result(
            contract,
            args,
            sources,
            preview=IntegratePreview(
                guard=guard,
                handover_warning=handover_warning,
                quality_gate=_quality_gate_preview(
                    contract,
                    profile_reference=args.certification_profile,
                ),
            ),
        )

    return _apply_integration(
        contract,
        args,
        sources,
        handover_warning=handover_warning,
    )


def _apply_integration(
    contract: WorktreeContract,
    args: WorktreeArgs,
    sources: IntegrationSources,
    *,
    handover_warning: dict[str, object] | None,
) -> WorktreeCommandResult:
    """Land the code commit, then the memory commits, then merge both into their sources."""
    prepared = _prepare_integration_commits(contract, args, sources)
    if isinstance(prepared, WorktreeCommandResult):
        return prepared
    commits, quality_gate, quality_certification, boundary_facts = prepared
    intent = args.integration_publication or prepare_integration_publication_intent(
        contract,
        operation_key=args.operation_key,
        generation=args.operation_generation,
        facts=boundary_facts,
        certification=quality_certification,
    )
    commit_tuple = (commits.code, commits.memory_content, commits.ledger)
    try:
        intent = transfer_and_publish_integration_claim(
            contract, args, intent, commits=commit_tuple
        )
    except IntegrationDoorAuthorityConflict as error:
        return WorktreeCommandResult(2, integration_door_decision_payload(error.evidence))
    locked_args = replace(
        args,
        quality_certification=quality_certification or args.quality_certification,
        integration_publication=intent,
    )
    publication = IntegrationPublication(
        contract=contract,
        args=args,
        locked_args=locked_args,
        sources=sources,
        commits=commits,
        intent=intent,
        quality_gate=quality_gate,
        handover_warning=handover_warning,
    )

    try:
        if contract.kind == "series":
            result = publish_series_integration_under_authority(
                contract,
                lambda: _publish_integration_edge(publication),
            )
            completed = publish_journaled_organizational_completion(result, intent)
        else:
            with integration_authority_lock(contract.coordination_root, contract.repo_name):
                result = _publish_integration_edge(publication)
                completed = publish_journaled_organizational_completion(result, intent)
    except AtomicLandingBlocked as error:
        return atomic_landing_blocked_result(contract, error)
    assert completed is not None
    return completed


def _publish_integration_edge(
    publication: IntegrationPublication,
) -> WorktreeCommandResult:
    current = load_contract(publication.contract.contract_path)
    door_block = _integration_door_block(current, publication.intent)
    if door_block is not None:
        return door_block
    if current != publication.contract:
        raise RuntimeError("integration contract changed before protected-ref movement")
    require_atomic_landing_authority(current)
    if current.kind == "series":
        require_series_contract_authority(current, operation="worktree_integrate")
    else:
        require_ordinary_worktree(current, operation="worktree_integrate")
    decision = protected_integration_decision(current, publication.locked_args)
    if decision is not None:
        return decision
    blocked = _integration_source_state_block(current, publication.sources)
    if blocked is not None:
        return blocked
    quality_gate = publication.quality_gate
    snapshot = prepare_integration_ref_move(
        current,
        publication.commits,
        publication.locked_args,
        publication.sources,
        expected_series_ledger_prefix=(
            atomic_series_ledger_prefix(current)
            if current.kind == "series" and current.memory_mode == "external"
            else ()
        ),
    )
    report_operation_progress(
        publication.args,
        "source-merge",
        current_command="compare-and-swap exact code and memory integration refs",
        irreversible_boundary=True,
        recovery_commits={
            "codeCommit": publication.commits.code,
            "memoryContentCommit": publication.commits.memory_content,
            "ledgerCommit": publication.commits.ledger,
        },
    )
    try:
        merge_integrated_commits(current, publication.commits, snapshot)
    except IntegrationRefRace:
        operation = require_plane_integration_operation(current, publication.locked_args)
        authority = operation.integrationAuthority
        assert authority is not None
        classification = classify_integration_authority_refs(
            authority,
            operation.recoveryCommits,
        )
        return WorktreeCommandResult(2, classification.public_payload())
    report_operation_progress(
        publication.locked_args,
        "contract-finalization",
        current_command="finalize integration contract edge",
    )
    return _integrated_result(
        current,
        publication.locked_args,
        publication.commits,
        handover_warning=publication.handover_warning,
        quality_gate=quality_gate,
    )


def _prepare_integration_commits(
    contract: WorktreeContract,
    args: WorktreeArgs,
    sources: IntegrationSources,
):
    recovered = prepared_integration_recovery(args)
    return recovered or _prepare_fresh_integration_commits(contract, args, sources)


def _prepare_fresh_integration_commits(
    contract: WorktreeContract,
    args: WorktreeArgs,
    sources: IntegrationSources,
):
    integrated_code_commit, blocked = _integrated_code_commit(contract, sources.current_code_source)
    if blocked is not None:
        return WorktreeCommandResult(2, blocked)
    report_operation_progress(
        args, "integration-quality", current_command="run altitude-routed quality contract"
    )
    quality_certification = args.quality_certification
    boundary_facts = IntegrationBoundaryFacts(None, None, None)
    if contract.kind == "series":
        quality_gate, blocked = _run_integration_quality_gate(contract, args=args)
        if blocked is not None:
            return WorktreeCommandResult(2, blocked)
    else:
        quality_gate = _quality_gate_preview(
            contract,
            profile_reference=args.certification_profile,
        )
    blocked = _integration_source_state_block(contract, sources)
    if blocked is not None:
        return blocked
    integrated_memory_content_commit, integrated_ledger_commit, blocked = (
        _integrated_memory_commits(contract, sources.current_memory_source)
    )
    if blocked is not None:
        return WorktreeCommandResult(2, blocked)
    commits = IntegratedCommits(
        code=integrated_code_commit,
        memory_content=integrated_memory_content_commit,
        ledger=integrated_ledger_commit,
    )
    boundary_facts = preview_integration_boundary(contract)
    if contract.kind == "leaf":
        completion = boundary_facts.organizational_completion
        if completion is not None:
            quality_gate, blocked = _run_integration_quality_gate(
                contract,
                completion=completion,
                args=args,
            )
            if blocked is not None:
                return WorktreeCommandResult(2, blocked)
            quality_certification = recorded_organizational_quality_certification(
                contract,
                operation_key=args.operation_key,
            )
    return commits, quality_gate, quality_certification, boundary_facts


def _integration_door_block(
    contract: WorktreeContract,
    publication: IntegrationPublicationIntent | None,
) -> WorktreeCommandResult | None:
    authority = classify_integration_door_authority(contract, publication)
    if authority.valid:
        return None
    return WorktreeCommandResult(2, integration_door_decision_payload(authority))


def _run_integration_quality_gate(
    contract: WorktreeContract,
    *,
    completion=None,
    args: WorktreeArgs,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Run the altitude-owned gate and translate failure into lifecycle guidance."""

    try:
        outcome = run_integration_quality_gate(
            contract,
            completion=completion,
            certification=args.quality_certification,
            certification_sink=(
                lambda certification: report_operation_progress(
                    args,
                    "integration-quality",
                    current_command="persist exact organizational full-gate certification",
                    quality_certification=certification.model_dump(mode="json"),
                )
            )
            if completion is not None
            else None,
            profile_reference=args.certification_profile,
        )
    except IntegrationQualityFailure as error:
        if error.organizational_completion:
            failure = organizational_quality_failure_payload(
                contract,
                error,
                expected_generation=args.operation_generation,
            )
            if args.operation_progress is not None:
                record_organizational_completion_repair(
                    contract,
                    operation_key=args.operation_key,
                    failure=failure,
                    progress=args.operation_progress,
                )
            return {}, failure
        return {}, blocked_integration_payload(
            contract,
            "blocked-quality-gate",
            "integration refused by the required quality gate",
            failureEvidence=error.evidence,
            decisionSurface=INTEGRATION_QUALITY_DECISION_SURFACE,
        )
    return outcome.result, None
