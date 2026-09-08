from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from agents_remember.controlplane.enforcement import (
    CLOSEOUT_GATE_KIND,
    CloseoutGuard,
    evaluate_closeout_gate,
)
from agents_remember.controlplane.store import GateStore
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.primitives.observer_paths import observer_logs_root
from agents_remember.models.closeout.input import EffectiveCloseoutInput
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.observer.events import now_iso
from agents_remember.worktrees.closeout_input import (
    effective_message_arguments,
    require_effective_closeout_plan,
)
from agents_remember.worktrees.integration.closeout.curator_coherence import (
    CuratorCoherenceNoImpact,
)
from agents_remember.worktrees.integration.closeout.integration_reopen import (
    completed_integration_reopen,
    preview_integration_reopen,
)
from agents_remember.worktrees.integration.closeout.memory_candidate_pairing import (
    accepted_closeout_memory_pair,
    memory_candidate_pair_payload,
    resolve_closeout_memory_pair,
)
from agents_remember.worktrees.integration.integration_branch_authority import (
    require_ordinary_worktree,
    require_series_contract_authority,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    require_closeout_mutation_authority,
)
from agents_remember.worktrees.modules.args import WorktreeArgs, report_operation_progress
from agents_remember.worktrees.modules.closeout_external import (
    ExternalCloseoutEvidence,
    external_closeout_commits,
)
from agents_remember.worktrees.modules.context import contract_context
from agents_remember.worktrees.modules.git import (
    branch_commit,
    changed_worktree_paths,
    commit_date,
    committed_changed_paths,
    worktree_dirty,
)
from agents_remember.worktrees.modules.guidance import (
    contract_next_args,
    recovery_guidance,
    status_payload,
)
from agents_remember.worktrees.modules.models import (
    PATH_SAMPLE_LIMIT,
    EntityFingerprintRefreshPlan,
    OnboardingRefreshPlan,
    RouteOverviewBodyClassification,
    RouteOverviewRefreshPlan,
    SidecarBodyClassification,
    VerifiedChange,
    WorktreeCommandResult,
)
from agents_remember.worktrees.modules.onboarding import (
    classify_route_overview_updates,
    classify_sidecar_updates,
    contract_memory_verified_commit,
    entity_fingerprint_refresh_plan,
    onboarding_refresh_plan,
    route_index_refresh_plan_for_context,
    route_overview_metadata_refresh_plan,
    validate_onboarding_refresh_plan,
    validate_route_overview_refresh_plan,
)
from agents_remember.worktrees.modules.onboarding_acceptance import (
    apply_route_no_impact,
    apply_sidecar_no_impact,
)
from agents_remember.worktrees.modules.quality.closeout_memory import run_memory_quality_phase
from agents_remember.worktrees.modules.quality.gate import (
    QualityGatePlan,
    QualityGateTarget,
    closeout_profile_purpose,
    code_quality_gate_preview,
    requires_strict_code_quality,
)
from agents_remember.worktrees.queue.closeout_preview import (
    closeout_order,
    closeout_summary,
    proposed_closeout_commits,
)
from agents_remember.worktrees.queue.closeout_recovery import (
    MemoryCloseoutOutcome,
    accepted_code_commit,
    prove_closeout_recovery_commits,
)
from agents_remember.worktrees.queue.closeout_staged_quality import (
    gate_staged_code as _gate_staged_code,
)
from agents_remember.worktrees.route_review import (
    code_candidate_tree,
    code_change_present,
    require_current_route_review,
)
from agents_remember.worktrees.series_closeout import (
    publish_closeout_under_authority,
    refuse_series_workbench_commit,
)
from agents_remember.worktrees.services import worktree_services
from agents_remember.worktrees.source_lineage import require_current_source_lineage
from agents_remember.worktrees.worktree_contract import (
    ContractCells,
    WorktreeContract,
    amend_contract,
    load_contract,
    write_contract,
)


def closeout_changed_paths(contract) -> dict[str, list[str]]:
    """Closeout worklist: working-tree changes plus the unverified committed range.

    ``committed`` covers commits the task transports (merges, pre-committed
    slices) that no previous closeout verified; ``working`` keeps the strict
    dirty-tree tier. A path in both tiers counts as working.
    """
    if contract.kind == "series":
        return {"all": [], "working": [], "committed": []}
    working = changed_worktree_paths(contract.code_worktree)
    committed = committed_changed_paths(
        contract.code_worktree, contract.code_base_commit, contract.code_commit
    )
    committed_only = sorted(set(committed) - set(working))
    return {
        "all": sorted({*working, *committed_only}),
        "working": working,
        "committed": committed_only,
    }


def _quality_gate_target(contract, args: WorktreeArgs) -> QualityGateTarget:
    return QualityGateTarget(
        code_worktree=contract.code_worktree,
        worktree_group=contract.worktree_group,
        repository_id=contract.repo_name,
        profile_reference=args.certification_profile,
        purpose=closeout_profile_purpose(contract),
    )


def _bounded_paths(paths: list[str]) -> dict[str, object]:
    """Count plus capped sample so committed-range lists never flood the payload."""
    return {"count": len(paths), "sample": paths[:PATH_SAMPLE_LIMIT]}


def _bounded_refresh_plan_view(plan: OnboardingRefreshPlan) -> dict[str, object]:
    """Payload view of the sidecar plan: blockers stay full, scaling lists bounded."""
    return {
        "required": _bounded_paths([item["source_path"] for item in plan["required"]]),
        "missing": plan["missing"],
        "unsupported": plan["unsupported"],
        "unonboarded": _bounded_paths(plan["unonboarded"]),
    }


def _bounded_classification_view(
    classification: SidecarBodyClassification,
) -> dict[str, object]:
    return {
        "stale": _bounded_paths(classification["stale"]),
        "untraced": _bounded_paths(classification["untraced"]),
        "attested_no_impact": _bounded_paths(classification["attested_no_impact"]),
    }


def _refresh_plans_have_work(
    metadata_refresh: OnboardingRefreshPlan,
    entity_refresh: EntityFingerprintRefreshPlan,
    route_overview_refresh: RouteOverviewRefreshPlan,
    route_index_refresh: dict[str, Any],
) -> bool:
    """True when any onboarding/entity/route refresh would change memory content."""
    return (
        bool(metadata_refresh["required"])
        or bool(entity_refresh["required"])
        or bool(route_overview_refresh["required"])
        or route_index_refresh["written"] > 0
    )


def _completed_integration_source_heads(contract, base: str, integrated: str) -> set[str]:
    expected = {base}
    if contract.integration_status == "completed" and integrated:
        expected.add(integrated)
    return expected


def _format_expected_heads(expected: set[str]) -> str:
    return ", ".join(sorted(expected))


@dataclass(frozen=True)
class _MemoryRefreshPreview:
    """What external-memory closeout would refresh, and what the body gates make of it.

    Related values computed by one step and consumed by one caller. They are grouped rather
    than returned as a tuple because the caller reads them by name, and grouped rather
    than left inline because the six ``contract.memory_mode == "external"`` conditionals
    that produce them were 66 of ``closeout_preview_payload``'s 153 lines.
    """

    metadata: OnboardingRefreshPlan
    entities: EntityFingerprintRefreshPlan
    route_overviews: RouteOverviewRefreshPlan
    route_indexes: dict[str, Any]
    sidecar_body_gate: SidecarBodyClassification
    route_overview_body_gate: RouteOverviewBodyClassification
    pair_identity: MemoryCandidatePairIdentity | None


def _memory_refresh_preview(contract, worklist: dict[str, list[str]]) -> _MemoryRefreshPreview:
    """Plan the external-memory refresh and classify it, without touching anything.

    Every field is the same conditional: plan it for real when the task carries external
    memory, and answer with the empty plan when it does not -- internal-memory closeout has
    no onboarding tree to refresh.
    """
    changed_paths = worklist["all"]
    external_leaf = contract.memory_mode == "external" and contract.kind == "leaf"
    pair_evidence = accepted_closeout_memory_pair(contract)
    coherence_no_impact = pair_evidence.no_impact
    metadata_refresh: OnboardingRefreshPlan = (
        onboarding_refresh_plan(contract, changed_paths, working_paths=worklist["working"])
        if external_leaf
        else {
            "required": [],
            "missing": [],
            "unsupported": [],
            "unonboarded": [],
        }
    )
    entity_refresh: EntityFingerprintRefreshPlan = (
        entity_fingerprint_refresh_plan(contract, changed_paths)
        if external_leaf
        else {
            "required": [],
            "unsupported": [],
        }
    )
    route_overview_refresh: RouteOverviewRefreshPlan = (
        route_overview_metadata_refresh_plan(contract, changed_paths)
        if external_leaf
        else {
            "required": [],
            "missing_metadata": [],
        }
    )
    route_index_refresh: dict[str, Any] = (
        route_index_refresh_plan_for_context(_closeout_contract_context(contract))
        if external_leaf
        else {
            "routes": 0,
            "written": 0,
            "unchanged": 0,
            "indexes": [],
        }
    )
    sidecar_body_gate: SidecarBodyClassification = (
        apply_sidecar_no_impact(
            classify_sidecar_updates(
                _closeout_contract_context(contract),
                metadata_refresh,
                memory_tree=contract.memory_worktree,
                memory_verified_commit=contract_memory_verified_commit(contract),
            ),
            coherence_no_impact.content_sources,
        )
        if external_leaf
        else {
            "stale": [],
            "untraced": [],
            "attested_no_impact": [],
        }
    )
    route_overview_body_gate: RouteOverviewBodyClassification = (
        apply_route_no_impact(
            classify_route_overview_updates(
                _closeout_contract_context(contract),
                route_overview_refresh,
                changed_paths,
                memory_tree=contract.memory_worktree,
                memory_verified_commit=contract_memory_verified_commit(contract),
            ),
            coherence_no_impact.source_routes,
        )
        if external_leaf
        else {
            "stale": [],
            "untraced": [],
            "attested_no_impact": [],
            "stamped_without_body_review": [],
        }
    )
    return _MemoryRefreshPreview(
        metadata=metadata_refresh,
        entities=entity_refresh,
        route_overviews=route_overview_refresh,
        route_indexes=route_index_refresh,
        sidecar_body_gate=sidecar_body_gate,
        route_overview_body_gate=route_overview_body_gate,
        pair_identity=pair_evidence.pair_identity,
    )


def closeout_preview_payload(contract, args: WorktreeArgs) -> dict[str, object]:
    """Answer what closeout would do, having done none of it."""
    refuse_series_workbench_commit(contract)
    code_dirty = contract.kind == "leaf" and worktree_dirty(contract.code_worktree)
    code_changed = code_change_present(contract)
    route_review = require_current_route_review(contract)
    memory_dirty = (
        contract.kind == "leaf"
        and contract.memory_mode == "external"
        and worktree_dirty(contract.memory_worktree)
    )
    worklist = closeout_changed_paths(contract)
    changed_paths = worklist["all"]
    refresh = _memory_refresh_preview(contract, worklist)
    memory_would_commit = memory_dirty or _refresh_plans_have_work(
        refresh.metadata, refresh.entities, refresh.route_overviews, refresh.route_indexes
    )
    code_quality_gate = _closeout_quality_gate_preview(
        contract,
        args,
        code_would_commit=code_changed,
    )
    return {
        "state": "would-closeout",
        **status_payload(contract),
        "phase": "commit-approval-pending",
        "summary": closeout_summary(contract),
        **recovery_guidance(
            "request_commit_approval",
            tool="worktree_closeout_apply",
            args=contract_next_args(
                contract,
                **effective_message_arguments(_effective_closeout_input(args)),
            ),
            required_args=["intent_note"],
        ),
        "commit_approval_required": True,
        "route_review": route_review,
        "approval_question": (
            "Approve recording the exact existing series commits in the closeout contract?"
            if contract.kind == "series"
            else "Approve creating the code, memory, and ledger commits with these messages?"
        ),
        "closeout_order": closeout_order(contract),
        "changed_code_paths": _bounded_paths(changed_paths),
        "changed_code_paths_committed": _bounded_paths(worklist["committed"]),
        "onboarding_metadata_refresh": _bounded_refresh_plan_view(refresh.metadata),
        "sidecar_body_gate": _bounded_classification_view(refresh.sidecar_body_gate),
        "sidecars_attested_no_impact": _bounded_paths(
            refresh.sidecar_body_gate["attested_no_impact"]
        ),
        "entity_fingerprint_refresh": refresh.entities,
        "route_overview_metadata_refresh": refresh.route_overviews,
        "route_overview_body_gate": refresh.route_overview_body_gate,
        "route_overviews_attested_no_impact": refresh.route_overview_body_gate[
            "attested_no_impact"
        ],
        "route_index_refresh": refresh.route_indexes,
        **memory_candidate_pair_payload(refresh.pair_identity),
        "code_quality_gate": code_quality_gate,
        "integration_reopen": preview_integration_reopen(
            contract, code_dirty=code_dirty, memory_would_commit=memory_would_commit
        ),
        "closeout_gate": _closeout_gate_payload(_closeout_gate_guard(contract, args)),
        "proposed_commits": proposed_closeout_commits(
            contract, args, code_dirty, memory_would_commit, code_quality_gate
        ),
    }


def _validate_closeout_source_heads(contract) -> None:
    current_code_source = branch_commit(contract.code_repo_path, contract.code_source_branch)
    expected_code_heads = _completed_integration_source_heads(
        contract, contract.code_base_commit, contract.integrated_code_commit
    )
    if current_code_source not in expected_code_heads:
        raise RuntimeError(
            "code source branch moved since task start: "
            f"{contract.code_source_branch} is {current_code_source}, "
            f"expected {_format_expected_heads(expected_code_heads)}"
        )
    if (
        contract.memory_mode == "external"
        and contract.memory_repo_path is not None
        and contract.memory_base_commit
    ):
        current_memory_source = branch_commit(
            contract.memory_repo_path, contract.memory_source_branch
        )
        expected_memory_heads = _completed_integration_source_heads(
            contract, contract.memory_base_commit, contract.integrated_ledger_commit
        )
        if current_memory_source not in expected_memory_heads:
            raise RuntimeError(
                "memory source branch moved since task start: "
                f"{contract.memory_source_branch} is {current_memory_source}, "
                f"expected {_format_expected_heads(expected_memory_heads)}"
            )


def _validate_closeout_source_state(contract) -> None:
    """Prove immediate source heads and the full super -> master -> leaf chain."""
    _validate_closeout_source_heads(contract)
    require_current_source_lineage(contract, operation="closeout")


def _closeout_approval_note(args: WorktreeArgs) -> str:
    if not args.approved:
        raise RuntimeError("closeout requires journaled explicit commit approval")
    approval_note = args.approval_note.replace("\n", " ").strip()
    if not approval_note:
        raise RuntimeError(
            "closeout requires a journaled note describing the developer's explicit commit approval"
        )
    return approval_note


def _closeout_gate_guard(contract, args: WorktreeArgs) -> CloseoutGuard | None:
    """The lifecycle's closeout-gate verdict, or ``None`` when the lifecycle is gateless.

    Reads the same gate log the dashboard writes -- the observer root under the
    contract's coordination root, keyed by ``contract.lifecycle_id``. A pure read;
    whether an unsatisfied verdict raises is the caller's choice, so the preview can
    surface the verdict without failing.
    """
    if not contract.lifecycle_id:
        return None
    store = GateStore(observer_logs_root(contract.coordination_root))
    return evaluate_closeout_gate(
        store.current(contract.lifecycle_id),
        policy=args.gate_policy,
        operation_key=args.operation_key or None,
    )


def _refuse_unsatisfied_closeout_gate(contract, args: WorktreeArgs) -> None:
    """Refuse early, on a pure read, when the gate is visibly unsatisfied. DECIDES NOTHING.

    Server-side gate enforcement (slice 6b): a dashboard-opened ``closeout-approval`` gate is
    binding; a gateless lifecycle uses the approval and note persisted by the canonical
    journaled closeout operation. The agent cannot satisfy the gate itself: its own
    ``gate_decide`` is
    ``decidedBy="model"``, which :func:`evaluate_closeout_gate` rejects.

    THIS IS NOT THE ENFORCEMENT ANY MORE, and reading it as such is the check-then-act mistake
    leaf 260731-EFA-L5 R2 was called in to remove. The approval is consumed by
    :func:`_claim_closeout_gate`, under the gate log's lock, immediately before the first
    irreversible act. This call exists only so an unapproved closeout is refused BEFORE it stages
    the worktree and spends a minute in the strict code-quality gate.

    It is safe to keep precisely because it can only DENY. Its read is unlocked and therefore
    already stale by the time it returns, but a stale read here has exactly two outcomes: it
    refuses a gate that has since been approved (the operator reruns, and nothing was consumed),
    or it permits and the claim re-evaluates the same policy under the lock and refuses there. It
    can never be the reason an approval is spent, because it never writes.
    """
    guard = _closeout_gate_guard(contract, args)
    if guard is not None and not guard.permitted:
        raise RuntimeError(f"closeout blocked by gate enforcement: {guard.reason}")


def _claim_closeout_gate(contract, args: WorktreeArgs) -> CloseoutGuard | None:
    """Spend the lifecycle's closeout approval, atomically, BEFORE anything irreversible happens.

    Two properties, and both are the point (leaf 260731-EFA-L5 R2/R3):

    ATOMIC. :meth:`GateStore.claim_approval` folds the log, applies the policy and appends the
    ``applied`` snapshot inside one held lock, so two closeouts racing this line resolve to exactly
    one spend. The old shape checked here and marked applied ~100 lines later, with every commit in
    between; two real processes and a 0.4s body were enough to have both permitted and two
    ``applied`` snapshots on disk.

    CLAIMED BEFORE THE SPEND, WHICH IS A DELIBERATE SEMANTIC CHANGE: an approval now authorises ONE
    ATTEMPT, not one success. A closeout that dies after this line -- a crashed process, a failed
    memory quality gate, a git error, an ENOSPC -- leaves the approval consumed, and the next
    closeout needs a fresh gate. ``controlplane/enforcement.py`` already words the remedy: "was
    already applied; open a fresh gate for a new mutation".

    That is the correct trade and the alternative is not a milder version of it. Marking applied at
    the END means the marker is attempted only once the code commit, the memory commit, the ledger
    commit and the contract rewrite have all happened -- so every way that write can fail to land
    (the process dies; the append raises) leaves a live approval sitting on top of an unknown
    amount of completed, irreversible work. Both were reproduced. Fail-closed costs a re-approval
    after a failure the operator can see; fail-open silently hands the next closeout an approval
    the human granted for work that is already done.

    A two-phase claim (a ``claimed`` state, finalised to ``applied`` on success and released back
    to ``approved`` on a clean failure) was considered and rejected: the release is exactly the
    step that cannot be guaranteed -- it is the same write, at the same late position, with the
    same failure modes -- so it would need a reaper to age a stuck ``claimed`` gate back to
    spendable, which re-opens this window on a timer and cannot tell a died-mid-commit closeout
    from a died-before-commit one.

    The call site is one statement above the first irreversible act. Everything upstream of it --
    source-head validation, the onboarding/route plans, the mixed reset and staging, the strict
    code-quality gate -- either only reads or only touches the index of the task's own disposable
    worktree, so a refusal there changes nothing and must not cost the developer their approval.
    ``mcp/tests/test_gate_replay_window.py`` pins both halves: the gate is already ``applied`` by
    the time ``commit_if_dirty`` runs, and a gate failure leaves it ``approved``.
    """
    if not contract.lifecycle_id:
        return None
    store = GateStore(observer_logs_root(contract.coordination_root))
    guard = store.claim_approval(
        contract.lifecycle_id,
        kind=CLOSEOUT_GATE_KIND,
        now=now_iso(),
        policy=args.gate_policy,
        operation_key=args.operation_key or None,
    )
    if not guard.permitted:
        raise RuntimeError(f"closeout blocked by gate enforcement: {guard.reason}")
    return guard


def _closeout_gate_payload(guard: CloseoutGuard | None) -> dict[str, object]:
    """How the preview/apply response reports gate enforcement to the commit-approval relay."""
    if guard is None:
        return {"enforced": False, "reason": "gateless lifecycle; chat commit approval governs"}
    return {
        "enforced": True,
        "permitted": guard.permitted,
        "gateId": guard.gate_id,
        "reason": guard.reason,
    }


@dataclass(frozen=True)
class _CloseoutAttestations:
    """What the onboarding body gates found, read before anything is committed.

    Validating rather than merely planning: ``validate_onboarding_refresh_plan`` and
    ``validate_route_overview_refresh_plan`` raise on a plan closeout must not proceed
    with, so this step is also the last refusal before the approval is claimed.
    """

    attested_sidecars: list[str] = field(default_factory=list)
    attested_overviews: list[str] = field(default_factory=list)
    stamped_overviews: list[str] = field(default_factory=list)
    unonboarded_paths: list[str] = field(default_factory=list)


def _closeout_attestations(
    contract,
    worklist: dict[str, list[str]],
    coherence_no_impact: CuratorCoherenceNoImpact,
) -> _CloseoutAttestations:
    """Validate and classify the onboarding refresh for a closeout that is about to run."""
    if contract.memory_mode != "external" or contract.kind != "leaf":
        return _CloseoutAttestations()
    changed_paths = worklist["all"]
    sidecar_plan = validate_onboarding_refresh_plan(
        contract,
        changed_paths,
        working_paths=worklist["working"],
        accepted_no_impact=coherence_no_impact.content_sources,
    )
    attested_sidecars = apply_sidecar_no_impact(
        classify_sidecar_updates(
            contract_context(contract),
            sidecar_plan,
            memory_tree=contract.memory_worktree,
            memory_verified_commit=contract_memory_verified_commit(contract),
        ),
        coherence_no_impact.content_sources,
    )["attested_no_impact"]
    overview_plan = validate_route_overview_refresh_plan(
        contract,
        changed_paths,
        accepted_no_impact=coherence_no_impact.source_routes,
    )
    overview_gate = apply_route_no_impact(
        classify_route_overview_updates(
            contract_context(contract),
            overview_plan,
            changed_paths,
            memory_tree=contract.memory_worktree,
            memory_verified_commit=contract_memory_verified_commit(contract),
        ),
        coherence_no_impact.source_routes,
    )
    return _CloseoutAttestations(
        attested_sidecars=attested_sidecars,
        attested_overviews=overview_gate["attested_no_impact"],
        stamped_overviews=overview_gate["stamped_without_body_review"],
        unonboarded_paths=sidecar_plan["unonboarded"],
    )


def _amended_closeout_contract(
    contract,
    approval_note: str,
    code_commit: str,
    memory: MemoryCloseoutOutcome,
    reopened: bool,
):
    """The contract as closeout leaves it: approved, committed, and possibly reopened.

    ``reopened`` means the closeout produced a commit that is not on the recorded source
    branch, so every integration cell it had earned is cleared and cleanup goes back to
    pending -- the task is not integrated any more, whatever it said a moment ago.
    """
    return amend_contract(
        replace(
            contract,
            approved_for_commit=True,
            commit_approval_note=approval_note,
            code_commit=code_commit,
            memory_content_commit=memory.memory_commit,
            ledger_commit=memory.ledger_commit,
            integration_strategy="" if reopened else contract.integration_strategy,
            integrated_code_commit="" if reopened else contract.integrated_code_commit,
            integrated_memory_content_commit=""
            if reopened
            else contract.integrated_memory_content_commit,
            integrated_ledger_commit="" if reopened else contract.integrated_ledger_commit,
        ),
        # The vocabulary cells go through the typed record; `replace` above carries only the
        # free-text commits and notes, which have no vocabulary to check them against.
        ContractCells(
            human_review_status="approved",
            closeout_status="completed",
            integration_status="not-started" if reopened else contract.integration_status,
            cleanup="pending" if reopened else contract.cleanup,
        ),
    )


def _memory_quality_before_refresh(contract) -> dict[str, Any]:
    """Run the external-memory citation preflight before the expensive code gate."""
    if contract.memory_mode != "external" or contract.kind != "leaf":
        return {}
    pair_evidence = accepted_closeout_memory_pair(contract)
    before_checks, _ = worktree_services().memory_quality.check_groups()
    result = run_memory_quality_phase(
        _closeout_contract_context(contract),
        before_checks,
        unstamped_code_commit=contract.code_base_commit,
    )
    result["curatorCoherence"] = {
        "state": "valid",
        "recordDigest": pair_evidence.coherence_record_digest,
        "deliveryAttempt": pair_evidence.delivery_attempt,
        **memory_candidate_pair_payload(pair_evidence.pair_identity),
    }
    return result


@dataclass(frozen=True)
class _CloseoutResultFacts:
    code_commit: str
    memory: MemoryCloseoutOutcome
    attestations: _CloseoutAttestations
    code_quality_gate: dict[str, Any]
    integration_reopen: dict[str, Any]
    gate_guard: CloseoutGuard | None
    pair_identity: MemoryCandidatePairIdentity | None


@dataclass(frozen=True)
class _CloseoutQualityFacts:
    attestations: _CloseoutAttestations
    code_quality_gate: dict[str, Any]
    memory_quality_before_refresh: dict[str, Any]
    strict_code_quality_required: bool
    coherence_no_impact: CuratorCoherenceNoImpact
    pair_identity: MemoryCandidatePairIdentity | None


def _recover_closeout_finalization(contract, args: WorktreeArgs) -> WorktreeCommandResult | None:
    """Finalize an already-committed detached closeout exactly once."""
    commits = args.recovery_commits
    if commits is None or (
        contract.memory_mode == "external"
        and (not commits.memoryContentCommit or not commits.ledgerCommit)
    ):
        return None
    pair_identity = resolve_closeout_memory_pair(contract)
    if contract.closeout_status == "completed":
        if (
            contract.code_commit != commits.codeCommit
            or contract.memory_content_commit != commits.memoryContentCommit
            or contract.ledger_commit != commits.ledgerCommit
        ):
            raise RuntimeError(
                "completed closeout contract does not match its recorded recovery commits"
            )
        return WorktreeCommandResult(
            0,
            {
                "state": "already-closed",
                "recovered": True,
                **status_payload(contract),
                **memory_candidate_pair_payload(pair_identity),
            },
        )
    approval_note = _closeout_approval_note(args)

    def publication():
        current = load_contract(contract.contract_path)
        if current != contract:
            raise RuntimeError("closeout contract changed before recovery finalization")
        if current.kind == "series":
            require_series_contract_authority(current, operation="worktree_closeout")
        else:
            require_ordinary_worktree(current, operation="worktree_closeout")
        memory = prove_closeout_recovery_commits(current, commits)
        integration_reopen = completed_integration_reopen(
            current,
            code_commit=commits.codeCommit,
            memory_content_commit=commits.memoryContentCommit,
            ledger_commit=commits.ledgerCommit,
        )
        updated = _amended_closeout_contract(
            current,
            approval_note,
            commits.codeCommit,
            memory,
            bool(integration_reopen["reopened"]),
        )
        report_operation_progress(
            args,
            "contract-finalization",
            current_command="finalize recovered closeout contract edge",
            recovery_commits=commits.model_dump(mode="json"),
            closeout_finalized_contract_sha256=closeout_contract_sha256(updated),
        )
        write_contract(current.contract_path, updated)
        return memory, integration_reopen, updated

    memory, integration_reopen, updated = publish_closeout_under_authority(contract, publication)
    payload = _closed_result_payload(
        updated,
        _CloseoutResultFacts(
            code_commit=commits.codeCommit,
            memory=memory,
            attestations=_CloseoutAttestations(),
            code_quality_gate={
                "status": "recovered-contract-finalization",
                "passed": True,
                "reason": "the exact accepted commit set was proven from Git and the ledger",
            },
            integration_reopen=integration_reopen,
            gate_guard=_closeout_gate_guard(contract, args),
            pair_identity=pair_identity,
        ),
    )
    payload["recovered"] = True
    return WorktreeCommandResult(0, payload)


def _closed_result_payload(updated, facts: _CloseoutResultFacts) -> dict[str, Any]:
    """Build the completed-closeout response after all durable writes finish."""
    memory = facts.memory
    attestations = facts.attestations
    return {
        "state": "closed",
        **status_payload(updated),
        "summary": "Closeout completed; integrate the task branches back into their source branches.",
        "code_commit": facts.code_commit,
        "memory_content_commit": memory.memory_commit,
        "ledger_commit": memory.ledger_commit,
        "refreshed_onboarding": _bounded_paths(
            [item["source_path"] for item in memory.refreshed_onboarding]
        ),
        "sidecars_attested_no_impact": _bounded_paths(attestations.attested_sidecars),
        "unonboarded_changed_paths": _bounded_paths(attestations.unonboarded_paths),
        "refreshed_entities": memory.refreshed_entities,
        "refreshed_route_overviews": memory.refreshed_route_overviews,
        "route_overviews_attested_no_impact": attestations.attested_overviews,
        "route_overviews_stamped_without_body_review": attestations.stamped_overviews,
        "route_index_refresh": memory.route_index_refresh,
        "memory_quality": memory.memory_quality,
        **memory_candidate_pair_payload(facts.pair_identity),
        "code_quality_gate": facts.code_quality_gate,
        "integration_reopen": facts.integration_reopen,
        "closeout_gate": _closeout_gate_payload(facts.gate_guard),
    }


def _closeout_quality_preflight(
    contract, args: WorktreeArgs, *, code_would_commit: bool
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Run the reversible code and memory gates in Gate-5 order before approval.

    CCR-R12 orders the closeout gates: the Dagger-backed code gate (Gates 1-4 rails)
    runs first and its red verdict blocks everything after it; the memory-quality
    preflight is Gate-5-domain work and is neither started nor prefetched before the
    code gate is green or not required.
    """
    code_quality_gate = _closeout_quality_gate_preview(
        contract,
        args,
        code_would_commit=code_would_commit,
    )
    strict_required = contract.kind == "leaf" and requires_strict_code_quality(
        _quality_gate_target(contract, args),
        code_would_commit=code_would_commit,
    )
    if strict_required:
        report_operation_progress(
            args, "quality", current_command="run targeted leaf quality contract"
        )
        code_quality_gate = _gate_staged_code(
            _quality_gate_target(contract, args),
            diff_base=contract.code_base_commit,
            candidate_tree=args.candidate_tree,
        )
        if not code_quality_gate.get("passed", False):
            raise RuntimeError(
                "closeout code-quality gate is red; Gate-5 memory preflight is blocked "
                "and no later gate may start"
            )
    memory_quality: dict[str, Any] = {}
    if contract.kind == "leaf" and contract.memory_mode == "external":
        report_operation_progress(
            args, "memory-preflight", current_command="run pre-refresh memory quality"
        )
        memory_quality = _memory_quality_before_refresh(contract)
    return code_quality_gate, memory_quality, strict_required


def _closeout_quality_gate_preview(
    contract,
    args: WorktreeArgs,
    *,
    code_would_commit: bool,
) -> dict[str, object]:
    if contract.kind != "leaf":
        return {
            "required": False,
            "status": "not-required-master-altitude",
            "command": "",
            "reason": (
                "series/master closeout records landed commits without rerunning acceptance; "
                "the master integration owns the single full acceptance"
            ),
        }
    return code_quality_gate_preview(
        _quality_gate_target(contract, args),
        code_would_commit=code_would_commit,
        diff_base=contract.code_base_commit,
        plan=QualityGatePlan(mode="targeted"),
    )


def _revalidate_reviewed_candidate(
    contract, route_review: dict[str, Any], accepted_candidate_tree: str
) -> None:
    """Re-prove source lineage and review identity at the last reversible boundary."""
    _validate_closeout_source_state(contract)
    if code_candidate_tree(contract) != accepted_candidate_tree:
        raise RuntimeError(
            "closeout candidate changed after quality; restart from the current candidate"
        )
    if not route_review.get("required", False):
        return
    current_review = require_current_route_review(contract)
    if route_review.get("candidateTree") != current_review.get("candidateTree"):
        raise RuntimeError(
            "closeout candidate changed after route review and quality; rerun independent review"
        )


@dataclass(frozen=True)
class _CloseoutCommitPhase:
    code_commit: str
    memory: MemoryCloseoutOutcome
    integration_reopen: dict[str, Any]
    gate_guard: CloseoutGuard | None


def _closeout_commit_phase(
    contract,
    args: WorktreeArgs,
    effective_input: EffectiveCloseoutInput,
    *,
    worklist: dict[str, list[str]],
    quality: _CloseoutQualityFacts,
) -> _CloseoutCommitPhase:
    resuming = args.approval_claimed or args.recovery_commits is not None
    report_operation_progress(
        args,
        "approval-claim",
        current_command="resume claimed closeout" if resuming else "claim closeout approval",
        approval_claimed=resuming,
    )
    gate_guard = (
        _closeout_gate_guard(contract, args) if resuming else _claim_closeout_gate(contract, args)
    )
    report_operation_progress(
        args,
        "approval-claim",
        current_command="closeout approval claimed",
        approval_claimed=True,
    )
    report_operation_progress(args, "code-commit", current_command="commit verified code")
    code_commit = accepted_code_commit(
        contract,
        args,
        effective_input,
        strict_code_quality_required=quality.strict_code_quality_required,
    )
    code_repository = (
        contract.code_repo_path if contract.kind == "series" else contract.code_worktree
    )
    code_commit_date = commit_date(code_repository, code_commit)
    memory = MemoryCloseoutOutcome()
    if contract.memory_mode == "external":
        memory = external_closeout_commits(
            contract,
            args,
            effective_input,
            VerifiedChange(
                commit=code_commit,
                commit_date=code_commit_date,
                changed_paths=worklist["all"],
                working_paths=worklist["working"],
            ),
            ExternalCloseoutEvidence(
                memory_quality_before_refresh=quality.memory_quality_before_refresh,
                coherence_no_impact=quality.coherence_no_impact,
            ),
        )
    integration_reopen = completed_integration_reopen(
        contract,
        code_commit=code_commit,
        memory_content_commit=memory.memory_commit,
        ledger_commit=memory.ledger_commit,
    )
    return _CloseoutCommitPhase(code_commit, memory, integration_reopen, gate_guard)


def _closeout_contract(
    args: WorktreeArgs,
    current_contract: WorktreeContract,
) -> tuple[Path, WorktreeContract]:
    contract_path = args.contract_path
    assert contract_path is not None
    contract = current_contract
    if contract_path.resolve() != contract.contract_path.resolve():
        raise RuntimeError(
            "closeout contract path does not match the path claimed by the contract document"
        )
    if contract.kind == "series":
        require_series_contract_authority(contract, operation="worktree_closeout")
    else:
        require_ordinary_worktree(contract, operation="worktree_closeout")
    return contract_path, contract


def _effective_closeout_input(args: WorktreeArgs) -> EffectiveCloseoutInput:
    effective = args.closeout_input
    if effective is None:
        raise RuntimeError("closeout execution requires normalized effective input")
    return effective


def _closeout_contract_context(contract):
    context = contract_context(contract)
    return replace(context, code_repository_root=contract.code_worktree)


def _closeout_quality_facts(
    contract: WorktreeContract,
    args: WorktreeArgs,
    *,
    resuming: bool,
    code_would_commit: bool,
    worklist: dict[str, list[str]],
) -> _CloseoutQualityFacts:
    """The attestations and reversible memory/code gate facts for one closeout."""

    pair_evidence = accepted_closeout_memory_pair(contract)
    coherence_no_impact = pair_evidence.no_impact

    if resuming:
        attestations = _CloseoutAttestations()
        code_quality_gate: dict[str, Any] = {
            "status": "recovered-post-claim",
            "passed": True,
            "reason": "the accepted candidate resumes after its durable approval claim",
        }
        memory_quality_before_refresh: dict[str, Any] = {}
        strict_code_quality_required = contract.kind == "leaf" and requires_strict_code_quality(
            _quality_gate_target(contract, args),
            code_would_commit=code_would_commit,
        )
    else:
        attestations = _closeout_attestations(contract, worklist, coherence_no_impact)
        code_quality_gate, memory_quality_before_refresh, strict_code_quality_required = (
            _closeout_quality_preflight(contract, args, code_would_commit=code_would_commit)
        )
    return _CloseoutQualityFacts(
        attestations=attestations,
        code_quality_gate=code_quality_gate,
        memory_quality_before_refresh=memory_quality_before_refresh,
        strict_code_quality_required=strict_code_quality_required,
        coherence_no_impact=coherence_no_impact,
        pair_identity=pair_evidence.pair_identity,
    )


def closeout_result(
    args: WorktreeArgs,
    current_contract: WorktreeContract,
) -> WorktreeCommandResult:
    """Run closeout for real, in the order the preview promised.

    Nothing moved across the claim on line "THE CLAIM" below, and nothing may: the ordering
    it enforces is the whole point of 260731-EFA-L5 R3.
    """
    contract, effective_input, early_result = _closeout_entry(args, current_contract)
    if early_result is not None:
        return early_result
    report_operation_progress(args, "preflight", current_command="validate closeout eligibility")
    if args.operation_key and not args.candidate_tree:
        raise RuntimeError("closeout operation is missing its accepted candidate tree")
    if not args.candidate_tree:
        args = replace(args, candidate_tree=code_candidate_tree(contract))
    route_review = require_current_route_review(contract)
    approval_note = _closeout_approval_note(args)
    resuming = args.approval_claimed or args.recovery_commits is not None
    if not resuming:
        _refuse_unsatisfied_closeout_gate(contract, args)
    worklist = closeout_changed_paths(contract)
    code_would_commit = code_change_present(contract)
    quality = _closeout_quality_facts(
        contract,
        args,
        resuming=resuming,
        code_would_commit=code_would_commit,
        worklist=worklist,
    )
    accepted_candidate_tree = cast(str, args.candidate_tree)
    refuse_series_workbench_commit(contract)
    _revalidate_reviewed_candidate(contract, route_review, accepted_candidate_tree)

    return _publish_closeout_candidate(
        contract,
        _CloseoutPublicationFacts(
            args=args,
            effective_input=effective_input,
            worklist=worklist,
            quality=quality,
            route_review=route_review,
            approval_note=approval_note,
        ),
    )


@dataclass(frozen=True)
class _CloseoutPublicationFacts:
    """Inputs already checked before the existing closeout writer claims approval."""

    args: WorktreeArgs
    effective_input: EffectiveCloseoutInput
    worklist: dict[str, list[str]]
    quality: _CloseoutQualityFacts
    route_review: dict[str, Any]
    approval_note: str


def _publish_closeout_candidate(
    contract: WorktreeContract,
    facts: _CloseoutPublicationFacts,
) -> WorktreeCommandResult:
    """Publish under one owner with a final contract and candidate check before claiming."""
    args = facts.args
    effective_input = facts.effective_input
    worklist = facts.worklist
    quality = facts.quality
    route_review = facts.route_review
    approval_note = facts.approval_note
    accepted_candidate_tree = cast(str, args.candidate_tree)

    def publication() -> tuple[_CloseoutCommitPhase, Any]:
        current = load_contract(contract.contract_path)
        if current != contract:
            raise RuntimeError("closeout contract changed before candidate commit")
        if current.kind == "series":
            require_series_contract_authority(current, operation="worktree_closeout")
        else:
            require_ordinary_worktree(current, operation="worktree_closeout")
        refuse_series_workbench_commit(current)
        _revalidate_reviewed_candidate(current, route_review, accepted_candidate_tree)
        committed = _closeout_commit_phase(
            current,
            args,
            effective_input,
            worklist=worklist,
            quality=quality,
        )
        updated = _amended_closeout_contract(
            current,
            approval_note,
            committed.code_commit,
            committed.memory,
            bool(committed.integration_reopen["reopened"]),
        )
        report_operation_progress(
            args,
            "contract-finalization",
            current_command="finalize closeout contract edge",
            recovery_commits={
                "codeCommit": committed.code_commit,
                "memoryContentCommit": committed.memory.memory_commit,
                "ledgerCommit": committed.memory.ledger_commit,
            },
            closeout_finalized_contract_sha256=closeout_contract_sha256(updated),
        )
        write_contract(current.contract_path, updated)
        return committed, updated

    committed, updated = publish_closeout_under_authority(contract, publication)
    return WorktreeCommandResult(
        0,
        _closed_result_payload(
            updated,
            _CloseoutResultFacts(
                code_commit=committed.code_commit,
                memory=committed.memory,
                attestations=quality.attestations,
                code_quality_gate=quality.code_quality_gate,
                integration_reopen=committed.integration_reopen,
                gate_guard=committed.gate_guard,
                pair_identity=quality.pair_identity,
            ),
        ),
    )


def _closeout_entry(
    args: WorktreeArgs,
    current_contract: WorktreeContract,
) -> tuple[WorktreeContract, EffectiveCloseoutInput, WorktreeCommandResult | None]:
    if current_contract.kind == "leaf" and not args.dry_run:
        raise CertificationContractError(
            "leaf closeout requires its journal-selected certification operation",
            (
                {
                    "code": "selected-closeout-operation-required",
                    "path": str(current_contract.contract_path),
                    "gateStarts": 0,
                },
            ),
        )
    if not args.dry_run:
        require_closeout_mutation_authority(args)
    _contract_path, contract = _closeout_contract(args, current_contract)
    effective_input = _effective_closeout_input(args)
    if args.recovery_commits is None:
        require_effective_closeout_plan(contract, effective_input, route="worktree")
    recovered = _recover_closeout_finalization(contract, args)
    if recovered is not None:
        return contract, effective_input, recovered
    _validate_closeout_source_state(contract)
    refuse_series_workbench_commit(contract)
    if args.dry_run:
        return (
            contract,
            effective_input,
            WorktreeCommandResult(0, closeout_preview_payload(contract, args)),
        )
    return contract, effective_input, None
