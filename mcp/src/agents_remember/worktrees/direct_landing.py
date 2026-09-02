"""Direct landing: code-commit verification + memory commit + ledger row.

The direct landing is the branch-addressed counterpart of the worktree closeout
commit phase for sanctioned direct execution. Where the worktree path stages a
leaf worktree candidate, this operation binds the task-root series contract and
verifies the exact code commit on the series branch, then commits external-
memory content and prepends the code-to-memory ledger row with the same ledger
semantics as the worktree path. Input is normalized before the integration
authority lock. Apply records a durable direct-landing generation before the
first memory or ledger mutation.

This is specifically the delivery route for a leaf implemented without its own
worktree enclosure. It is not master/series closeout and it is not the ordinary
``worktree_integrate`` edge that lands an already closed master into its parent
branch. Those lifecycle edges do not become direct execution merely because
their contract kind is ``series``.

The gate stays strictly pre-commit: pass the staged ``candidate_tree`` that the
owner already gated through the Dagger module's ``--source``/``--repository-bundle``
contract, and the landing verifies the branch HEAD tree equals it before any
memory or ledger commit. Commit-then-gate is the accepted-risk exception only
where the developer rules it.

The operation is policy-gated (``directExecutionEnabled``) and deliberately
synchronous: direct mode does not use the ``start_or_observe_operation`` detached
worker. The lane lock serializes execution; the canonical lifecycle journal owns
crash recovery across memory and ledger outputs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from agents_remember.controlplane.integration_authority_lock import integration_authority_lock
from agents_remember.controlplane.task_publication_lock import task_publication_lock
from agents_remember.errors import TaskIntentError
from agents_remember.kernel.memory_ledger import LedgerError, load_ledger
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.models.closeout.input import CloseoutCorrectedCall, EffectiveCloseoutInput
from agents_remember.models.lifecycles.direct_landing import DirectLandingOperationInput
from agents_remember.models.lifecycles.door import CloseoutDoorGeneration
from agents_remember.models.lifecycles.operation import (
    GatePolicyRuleSnapshot,
    LifecycleOperationRecord,
    lifecycle_operation_dependencies,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.closeout_input import (
    corrected_closeout_arguments,
    normalize_closeout_input,
    raw_closeout_messages,
)
from agents_remember.worktrees.integration.closeout.door import (
    DoorPublicationError,
    door_generation_for_operation,
    prepare_door_publication,
    publish_door_intent,
)
from agents_remember.worktrees.integration.closeout.task_intent_identity import (
    current_door_task_intent,
)
from agents_remember.worktrees.integration.configured_contract_authority import (
    reread_configured_contract,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_errors import (
    DirectLandingError,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_execution import (
    direct_landing_input,
    execute_or_require_direct_landing_recovery,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_operation import (
    DirectLandingRuntime,
    direct_landing_record,
    direct_landing_store,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    LifecycleOperationCandidate,
    LifecycleOperationCandidateBinding,
    lifecycle_operation_candidate,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    operation_state_fingerprint,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_lease import (
    contract_lifecycle_lease,
    require_lifecycle_operation_compatible,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    operation_projection,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)
from agents_remember.worktrees.integration.mutation_evidence import git_mutation_snapshot
from agents_remember.worktrees.modules.git import (
    branch_commit,
    current_branch,
    require_git,
)
from agents_remember.worktrees.queue.closeout_projection_publication import (
    projection_refresh_failure_effect,
    refresh_closeout_projection,
)
from agents_remember.worktrees.queue.closeout_queue import require_first_ready_generation
from agents_remember.worktrees.queue.closeout_queue_errors import CloseoutQueueError
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract


@dataclass(frozen=True)
class DirectLandingRequest:
    """One branch-addressed direct landing of an exact series code commit.

    ``candidate_tree`` is the staged candidate tree the owner gated pre-commit
    through the Dagger ``--source``/``--repository-bundle`` contract; when given,
    the landing verifies the branch HEAD tree equals it before committing.
    """

    contract_path: str
    code_commit: str
    memory_commit_message: str | None = None
    ledger_commit_message: str | None = None
    intent_note: str = ""
    candidate_tree: str | None = None
    dry_run: bool = False


@dataclass(frozen=True)
class _DirectRequestIdentity:
    config: McpRuntimeConfig
    contract: WorktreeContract
    request: DirectLandingRequest
    effective_input: EffectiveCloseoutInput
    code_commit: str
    candidate_tree: str


def direct_landing(
    config: McpRuntimeConfig,
    request: DirectLandingRequest,
    admitted_contract: WorktreeContract,
) -> dict[str, object]:
    """Run the direct landing under the integration authority lock.

    Validates the effective message plan before lane authority or Git. The code
    commit itself is verified, never created: the developer has already
    committed the candidate on the series branch in direct mode.
    """
    require_direct_landing_enabled(config)
    return _direct_landing_after_policy(config, request, admitted_contract)


def require_direct_landing_enabled(config: McpRuntimeConfig) -> None:
    """Refuse the branch-addressed leaf route before inspecting its contract."""

    if not config.direct_execution_enabled:
        raise DirectLandingError(
            "direct-landing-policy-disabled",
            "direct landing is disabled by policy; enable directExecutionEnabled "
            "in the MCP authority settings for sanctioned direct execution",
        )


def _direct_landing_after_policy(
    config: McpRuntimeConfig,
    request: DirectLandingRequest,
    admitted_contract: WorktreeContract,
) -> dict[str, object]:
    contract = admitted_contract
    contract_path = contract.contract_path
    if contract.kind != "series":
        raise DirectLandingError(
            "direct-landing-series-required",
            "direct landing binds the task-root series contract "
            f"(series-contract.md); {contract_path} is a {contract.kind} contract",
        )
    corrected_arguments = corrected_closeout_arguments(
        contract_path.as_posix(),
        code_commit="<exact series code commit>",
        intent_note="<developer intent>",
    )
    if request.dry_run:
        corrected_arguments["dry_run"] = True
    effective_input = normalize_closeout_input(
        contract,
        raw_closeout_messages(
            code=None,
            memory=request.memory_commit_message,
            ledger=request.ledger_commit_message,
        ),
        route="direct-landing",
        corrected_call=CloseoutCorrectedCall(
            tool="direct_landing",
            arguments=corrected_arguments,
        ),
    )
    if not request.intent_note.strip():
        raise DirectLandingError(
            "direct-landing-intent-required",
            "direct landing requires a non-empty intent note (the commit approval)",
        )
    code_commit = request.code_commit.strip()
    if not code_commit:
        raise DirectLandingError(
            "direct-landing-code-commit-required",
            "direct landing requires the exact series code commit to verify",
        )
    with contract_lifecycle_lease(contract):
        current, _location = reread_configured_contract(
            contract,
            config.config_path.as_posix(),
        )
        if current != contract:
            raise DirectLandingError(
                "direct-landing-contract-changed",
                "series contract changed before direct landing",
            )
        if request.dry_run:
            return _direct_landing_preview(current, request, effective_input, code_commit)
        require_lifecycle_operation_compatible(current, operation_kind="direct-landing")
        return _start_or_observe_direct_landing(
            config, current, request, effective_input, code_commit
        )


def _verify_code_commit(contract, code_commit: str, candidate_tree: str | None) -> str:
    """Verify the exact code commit is the current series branch HEAD.

    When ``candidate_tree`` is given (the gated staged candidate), the branch
    HEAD tree must equal it: the Dagger ``--source``/``--repository-bundle``
    gate ran over that exact tree before this landing, so a moved branch after
    the gate is refused pre-commit.
    """
    try:
        series_head = branch_commit(contract.code_repo_path, contract.code_work_branch)
    except RuntimeError as exc:
        raise DirectLandingError(
            "direct-landing-code-git-unreadable",
            "direct landing cannot read the accepted code ref",
            observed=public_failure_evidence(
                stage="direct-code-proof",
                side="code",
                name=contract.code_work_branch,
                error_type=type(exc).__name__,
                observed={"state": "unreadable"},
            ),
        ) from exc
    if code_commit != series_head:
        raise DirectLandingError(
            "direct-landing-code-commit-mismatch",
            f"code commit {code_commit} is not the current series branch HEAD "
            f"({series_head}); direct landing verifies the branch HEAD commit, "
            "it does not create one",
        )
    try:
        committed_tree = require_git(
            contract.code_repo_path, ["rev-parse", f"{code_commit}^{{tree}}"]
        )
    except RuntimeError as exc:
        raise DirectLandingError(
            "direct-landing-code-git-unreadable",
            "direct landing cannot read the accepted code tree",
            observed=public_failure_evidence(
                stage="direct-code-proof",
                side="code",
                name=contract.code_work_branch,
                error_type=type(exc).__name__,
                observed={"state": "unreadable"},
            ),
        ) from exc
    if not committed_tree:
        raise DirectLandingError(
            "direct-landing-code-commit-invalid",
            f"cannot resolve the tree of code commit {code_commit}",
        )
    if candidate_tree and committed_tree != candidate_tree:
        raise DirectLandingError(
            "direct-landing-candidate-tree-moved",
            "the series branch HEAD tree moved after the staged candidate was "
            "gated: the Dagger --source/--repository-bundle gate certified "
            f"{candidate_tree}, the branch now carries {committed_tree}; "
            "re-gate the new candidate before landing",
        )
    return committed_tree


def _memory_facts(contract) -> dict[str, object]:
    """Read the external-memory repository and ledger facts for the landing."""
    if contract.memory_mode != "external":
        return {"memoryMode": contract.memory_mode}
    if contract.memory_repo_path is None or contract.ledger_path is None:
        raise DirectLandingError(
            "direct-landing-memory-authority-missing",
            "external-memory direct landing requires the configured memory "
            "repository and ledger path",
        )
    try:
        memory_head = branch_commit(contract.memory_repo_path, contract.memory_work_branch)
    except (OSError, RuntimeError) as exc:
        raise DirectLandingError(
            "direct-landing-memory-evidence-unreadable",
            "direct landing cannot read the accepted memory ref",
            observed=public_failure_evidence(
                stage="direct-memory-proof",
                side="memory",
                name=contract.memory_work_branch,
                error_type=type(exc).__name__,
                observed={"state": "unreadable"},
            ),
        ) from exc
    _load_direct_ledger(contract.ledger_path)
    return {
        "memoryMode": "external",
        "memoryBranch": contract.memory_work_branch,
        "memoryHead": memory_head,
        "ledgerParsed": True,
    }


def _direct_landing_preview(
    contract,
    request: DirectLandingRequest,
    effective_input: EffectiveCloseoutInput,
    code_commit: str,
) -> dict[str, object]:
    candidate_tree = _verify_code_commit(contract, code_commit, request.candidate_tree)
    door = contract.closeout_door
    if door is None or door.disposition != "waiting":
        raise DirectLandingError(
            "direct-landing-door-not-waiting",
            "direct landing preview requires one current waiting series door generation",
            expected={"disposition": "waiting"},
            observed={"disposition": door.disposition if door is not None else "absent"},
        )
    if door.candidateTree != candidate_tree:
        raise DirectLandingError(
            "direct-landing-door-candidate-moved",
            "the waiting series door does not bind the gated direct candidate tree",
            expected={"candidateTree": door.candidateTree},
            observed={"candidateTree": candidate_tree},
        )
    try:
        require_first_ready_generation(
            contract.coordination_root,
            sprint_ref=door.sprintTaskDocumentRef,
            generation_id=door.generationId,
        )
    except CloseoutQueueError as exc:
        raise DirectLandingError(exc.status, str(exc)) from exc
    memory = _memory_facts(contract)
    return {
        "ok": True,
        "operation": "direct_landing",
        "state": "would-land",
        "summary": "Direct landing preview: code commit verified; memory and ledger "
        "commits would be created.",
        "contractPath": contract.contract_path.as_posix(),
        "codeCommit": code_commit,
        "memoryContentCommit": "",
        "ledgerCommit": "",
        "dryRun": True,
        "doorGenerationId": door.generationId,
        "memory": memory,
        "effectiveInput": effective_input.model_dump(mode="json"),
    }


def _direct_memory_admission_snapshot(contract: WorktreeContract):
    memory_repo = contract.memory_repo_path
    assert memory_repo is not None
    try:
        observed_branch = current_branch(memory_repo)
    except RuntimeError as exc:
        raise DirectLandingError(
            "direct-landing-memory-git-unreadable",
            "direct landing cannot read the accepted memory branch",
            observed=public_failure_evidence(
                stage="direct-memory-admission",
                side="memory",
                name=contract.memory_work_branch,
                error_type=type(exc).__name__,
                observed={"state": "unreadable"},
            ),
        ) from exc
    if contract.memory_work_branch and observed_branch != contract.memory_work_branch:
        raise DirectLandingError(
            "direct-landing-memory-branch-mismatch",
            "the memory repository checkout is not on the accepted memory branch",
            expected={"branch": contract.memory_work_branch},
            observed={"branch": observed_branch},
        )
    try:
        return git_mutation_snapshot(
            memory_repo,
            contract.worktree_group / "reports" / ".direct-admission.index",
        )
    except (OSError, RuntimeError) as exc:
        raise DirectLandingError(
            "direct-landing-memory-git-unreadable",
            "direct landing cannot capture the accepted memory Git state",
            observed=public_failure_evidence(
                stage="direct-memory-admission",
                side="memory",
                name="git-state",
                error_type=type(exc).__name__,
                observed={"state": "unreadable"},
            ),
        ) from exc


def _start_or_observe_direct_landing(
    config: McpRuntimeConfig,
    contract: WorktreeContract,
    request: DirectLandingRequest,
    effective_input: EffectiveCloseoutInput,
    code_commit: str,
) -> dict[str, object]:
    if contract.memory_mode != "external":
        raise DirectLandingError(
            "direct-landing-memory-required",
            "direct landing currently requires external memory so the ledger row "
            "has a real mapping to commit; internal/disabled memory has no ledger",
        )
    if contract.memory_repo_path is None or contract.ledger_path is None:
        raise DirectLandingError(
            "direct-landing-memory-authority-missing",
            "external-memory direct landing requires the configured memory "
            "repository and ledger path",
        )
    candidate_tree = (request.candidate_tree or "").strip()
    if not candidate_tree:
        raise DirectLandingError(
            "direct-landing-candidate-tree-required",
            "direct landing apply requires the exact pre-commit gated candidate tree",
        )
    store = direct_landing_store(contract)
    identity = _DirectRequestIdentity(
        config,
        contract,
        request,
        effective_input,
        code_commit,
        candidate_tree,
    )
    prepared: tuple[DirectLandingOperationInput, LifecycleOperationCandidate] | None = None
    door = contract.closeout_door
    if door is None:
        raise DirectLandingError(
            "direct-landing-door-missing",
            "direct landing requires one current series door generation",
            expected={"disposition": "waiting-or-claimed"},
            observed={"disposition": "absent"},
        )
    if door.disposition == "waiting":
        with integration_authority_lock(config.coordination_root, contract.repo_name):
            current_contract, _location = reread_configured_contract(
                contract,
                config.config_path.as_posix(),
            )
            if current_contract != contract:
                raise DirectLandingError(
                    "direct-landing-contract-changed",
                    "series contract changed before direct landing admission",
                )
            prepared = _prepare_direct_landing_candidate(identity, door.generationId)
    record, claimed_contract, created, sprint_ref = _claim_direct_landing(
        config,
        contract,
        store,
        identity,
        prepared,
    )
    try:
        projection_effect = refresh_closeout_projection(
            claimed_contract.coordination_root,
            sprint_ref,
        )
    except Exception as exc:
        projection_effect = projection_refresh_failure_effect(
            claimed_contract.coordination_root,
            sprint_ref,
            exc,
        )
    if record.status == "completed" and record.result is not None:
        return _with_projection_effect(
            _with_lifecycle_operation(dict(record.result), claimed_contract, record),
            projection_effect,
        )
    if not created and record.status != "running":
        return _with_projection_effect(
            _direct_landing_observation(claimed_contract, record),
            projection_effect,
        )
    with integration_authority_lock(config.coordination_root, claimed_contract.repo_name):
        live_contract, _location = reread_configured_contract(
            claimed_contract,
            config.config_path.as_posix(),
        )
        _require_direct_claim_owner(live_contract, record)
        runtime = DirectLandingRuntime(live_contract, record)
        result = execute_or_require_direct_landing_recovery(live_contract, runtime)
        completed = runtime.store.read() or runtime.record
        result = _with_lifecycle_operation(result, live_contract, completed)
    return _with_projection_effect(result, projection_effect)


def _with_lifecycle_operation(
    result: dict[str, object],
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> dict[str, object]:
    """Expose the same durable generation on first completion and exact retry."""

    return {
        **result,
        "lifecycleOperation": operation_projection(
            record,
            contract=load_contract(contract.contract_path),
        ).model_dump(mode="json", exclude_none=True),
    }


def _prepare_direct_landing_candidate(
    identity: _DirectRequestIdentity,
    door_generation_id: str,
) -> tuple[DirectLandingOperationInput, LifecycleOperationCandidate]:
    config = identity.config
    contract = identity.contract
    request = identity.request
    memory_repo = contract.memory_repo_path
    assert memory_repo is not None and contract.ledger_path is not None
    candidate_tree = identity.candidate_tree
    code_tree = _verify_code_commit(contract, identity.code_commit, candidate_tree)
    door = contract.closeout_door
    if (
        door is None
        or door.disposition != "waiting"
        or door.generationId != door_generation_id
        or door.candidateTree != code_tree
    ):
        raise DirectLandingError(
            "direct-landing-door-candidate-moved",
            "the waiting series door no longer binds the gated direct candidate",
            expected={
                "generationId": door_generation_id,
                "candidateTree": code_tree,
                "disposition": "waiting",
            },
            observed={
                "generationId": door.generationId if door is not None else "",
                "candidateTree": door.candidateTree if door is not None else "",
                "disposition": door.disposition if door is not None else "absent",
            },
        )
    memory_before = _direct_memory_admission_snapshot(contract)
    _load_direct_ledger(contract.ledger_path)
    ledger_text = _read_direct_ledger_text(contract.ledger_path)
    operation_input = DirectLandingOperationInput(
        configPath=config.config_path.as_posix(),
        contractPath=contract.contract_path.as_posix(),
        effectiveInput=identity.effective_input,
        approvalNote=request.intent_note.strip(),
        gatePolicy=_gate_policy_snapshot(config),
        codeCommit=identity.code_commit,
        codeTree=code_tree,
        candidateTree=candidate_tree,
        memoryRepository=memory_repo.resolve().as_posix(),
        memoryBranch=contract.memory_work_branch,
        memoryRef=memory_before.headRef,
        memoryBefore=memory_before,
        ledgerPath=contract.ledger_path.resolve().as_posix(),
        ledgerBeforeText=ledger_text,
        ledgerBeforeSha256=_text_sha256(ledger_text),
    )
    try:
        intent = current_door_task_intent(contract)
    except TaskIntentError as exc:
        raise DirectLandingError(
            exc.status,
            exc.detail,
            next_action=exc.next_action,
        ) from exc
    candidate = lifecycle_operation_candidate(
        LifecycleOperationCandidateBinding(
            operation_input=operation_input,
            candidate_state=operation_state_fingerprint(contract),
            candidate_tree=candidate_tree,
            closeout_door_generation_id=door_generation_id,
            task_intent=intent,
        )
    )
    return operation_input, candidate


def _claim_direct_landing(
    config: McpRuntimeConfig,
    admitted_contract: WorktreeContract,
    store: LifecycleOperationStore,
    identity: _DirectRequestIdentity,
    prepared: tuple[DirectLandingOperationInput, LifecycleOperationCandidate] | None,
) -> tuple[LifecycleOperationRecord, WorktreeContract, bool, TaskDocumentRef]:
    """Create/replay the journal intent and claim one exact waiting series door."""

    with task_publication_lock(admitted_contract.coordination_root, admitted_contract.repo_name):
        contract, _location = reread_configured_contract(
            admitted_contract,
            config.config_path.as_posix(),
        )
        door = _require_direct_door(contract)
        if door.disposition == "claimed":
            return _resume_direct_claim(contract, store, identity, door)
        return _claim_waiting_direct_landing(contract, store, prepared, door)


def _require_direct_door(contract: WorktreeContract) -> CloseoutDoorGeneration:
    door = contract.closeout_door
    if door is None:
        raise DirectLandingError(
            "direct-landing-door-missing",
            "direct landing claim has no current series door generation",
        )
    return door


def _resume_direct_claim(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    identity: _DirectRequestIdentity,
    door: CloseoutDoorGeneration,
) -> tuple[LifecycleOperationRecord, WorktreeContract, bool, TaskDocumentRef]:
    current = store.read()
    try:
        current_intent = current_door_task_intent(contract)
    except TaskIntentError as exc:
        raise DirectLandingError(
            exc.status,
            exc.detail,
            next_action=exc.next_action,
        ) from exc
    if current is not None and not isinstance(current.taskIntent, TaskIntentIdentity):
        raise DirectLandingError(
            "lifecycle-operation-task-intent-unavailable",
            "the claimed legacy direct-landing journal cannot be reused",
            next_action="retire-and-republish",
        )
    if current is not None and current.taskIntent != current_intent:
        raise DirectLandingError(
            "lifecycle-operation-task-intent-stale",
            "the claimed direct-landing journal binds a different canonical task intent",
            next_action="retire-and-republish",
        )
    if current is None or not _same_direct_request(current, identity):
        raise DirectLandingError(
            "direct-landing-claim-owner-conflict",
            "the claimed series door belongs to another exact journal intent",
            expected={
                "operationKind": door.operationKind or "",
                "operationFingerprint": door.operationFingerprint,
                "operationKey": door.claimedOperationKey,
            },
            observed={
                "operationKind": current.operationKind if current is not None else "",
                "operationFingerprint": current.fingerprint if current is not None else "",
                "operationKey": current.operationKey if current is not None else "",
            },
        )
    _require_direct_claim_owner(contract, current)
    current = _publish_direct_door_proof(contract, store, current)
    return current, load_contract(contract.contract_path), False, door.sprintTaskDocumentRef


def _claim_waiting_direct_landing(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    prepared: tuple[DirectLandingOperationInput, LifecycleOperationCandidate] | None,
    door: CloseoutDoorGeneration,
) -> tuple[LifecycleOperationRecord, WorktreeContract, bool, TaskDocumentRef]:
    if door.disposition != "waiting" or prepared is None:
        raise DirectLandingError(
            "direct-landing-door-not-waiting",
            "direct landing claim requires the exact waiting source generation",
            expected={"disposition": "waiting"},
            observed={"disposition": door.disposition},
        )
    operation_input, candidate = prepared
    _require_waiting_direct_candidate(contract, door, candidate)
    try:
        require_first_ready_generation(
            contract.coordination_root,
            sprint_ref=door.sprintTaskDocumentRef,
            generation_id=door.generationId,
        )
    except CloseoutQueueError as exc:
        raise DirectLandingError(exc.status, str(exc)) from exc
    provisional = direct_landing_record(contract, operation_input, candidate, None)
    claimed = door_generation_for_operation(contract, provisional, "claimed")
    queued = provisional.model_copy(
        update={"doorPublication": prepare_door_publication(contract, claimed)}
    )
    queued = queued.model_copy(update={"dependencies": lifecycle_operation_dependencies(queued)})
    current, created = _create_direct_claim(store, queued, door)
    current = _publish_direct_door_proof(contract, store, current)
    return current, load_contract(contract.contract_path), created, door.sprintTaskDocumentRef


def _require_waiting_direct_candidate(
    contract: WorktreeContract,
    door: CloseoutDoorGeneration,
    candidate: LifecycleOperationCandidate,
) -> None:
    if candidate.state != operation_state_fingerprint(contract):
        raise DirectLandingError(
            "direct-landing-contract-changed",
            "series contract changed after direct landing candidate admission",
        )
    if candidate.tree != door.candidateTree:
        raise DirectLandingError(
            "direct-landing-door-candidate-moved",
            "the admitted direct candidate no longer equals the waiting door candidate",
            expected={"candidateTree": door.candidateTree},
            observed={"candidateTree": candidate.tree or ""},
        )
    if not isinstance(door.taskIntent, TaskIntentIdentity):
        raise DirectLandingError(
            "closeout-door-task-intent-unavailable",
            "the waiting direct-landing door predates canonical task intent",
            next_action="closeout_door.update-provenance",
        )
    if candidate.task_intent != door.taskIntent:
        raise DirectLandingError(
            "closeout-door-task-intent-stale",
            "the admitted direct-landing task intent no longer equals the waiting door",
            expected={"taskIntent": door.taskIntent.model_dump(mode="json", by_alias=True)},
            observed={
                "taskIntent": (
                    candidate.task_intent.model_dump(mode="json", by_alias=True)
                    if candidate.task_intent is not None
                    else None
                )
            },
            next_action="closeout_door.update-provenance",
        )


def _create_direct_claim(
    store: LifecycleOperationStore,
    queued: LifecycleOperationRecord,
    door: CloseoutDoorGeneration,
) -> tuple[LifecycleOperationRecord, bool]:
    current = store.read()
    if current is None:
        return store.create(queued)
    if current.fingerprint == queued.fingerprint:
        if current.doorPublication != queued.doorPublication:
            raise DirectLandingError(
                "direct-landing-journal-intent-conflict",
                "the retained direct journal does not match the exact claim intent",
            )
        return current, False
    if _direct_successor_is_authorized(current, door):
        return store.replace_terminal(queued), True
    raise DirectLandingError(
        "direct-landing-input-conflict",
        "a fresh direct landing requires an exact cancelled or superseded door successor",
        expected={
            "acceptedFingerprint": current.fingerprint,
            "acceptedDisposition": current.generationDisposition,
        },
        observed={
            "candidateFingerprint": queued.fingerprint,
            "doorPredecessor": door.predecessorGenerationId,
        },
    )


def _direct_successor_is_authorized(
    record: LifecycleOperationRecord,
    waiting_door,
) -> bool:
    publication = record.doorPublication
    predecessor = record.doorPublicationHistory[-1] if record.doorPublicationHistory else None
    return bool(
        record.status in {"completed", "cancelled"}
        and record.generationDisposition in {"cancelled", "superseded"}
        and publication is not None
        and publication.state == "proven"
        and publication.generation == waiting_door
        and predecessor is not None
        and predecessor.state == "proven"
        and predecessor.generation.disposition == "claimed"
        and predecessor.generation.operationKind == "direct-landing"
        and predecessor.generation.operationFingerprint == record.fingerprint
        and predecessor.generation.claimedOperationKey == record.operationKey
        and waiting_door.disposition == "waiting"
        and waiting_door.predecessorGenerationId == predecessor.generation.generationId
    )


def _publish_direct_door_proof(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
) -> LifecycleOperationRecord:
    publication = record.doorPublication
    if publication is None:
        raise DirectLandingError(
            "direct-landing-door-intent-missing",
            "canonical direct landing journal is missing its create-time claimed-door intent",
        )
    if publication.state == "proven":
        return record
    try:
        proof = publish_door_intent(contract.contract_path, publication)
    except DoorPublicationError as exc:
        raise DirectLandingError(
            exc.status,
            exc.detail,
            expected=exc.classification.expected,
            observed=exc.classification.observed,
        ) from exc
    return store.update(lambda current: current.model_copy(update={"doorPublication": proof}))


def _require_direct_claim_owner(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> None:
    door = contract.closeout_door
    publication = record.doorPublication
    if (
        door is None
        or door.disposition != "claimed"
        or door.operationKind != "direct-landing"
        or door.operationFingerprint != record.fingerprint
        or door.claimedOperationKey != record.operationKey
        or publication is None
        or publication.generation != door
    ):
        raise DirectLandingError(
            "direct-landing-claim-owner-conflict",
            "direct execution requires its exact immutable journal and claimed-door owner",
        )


def _with_projection_effect(payload: dict[str, object], effect) -> dict[str, object]:
    return {
        **payload,
        "projectionEffects": [effect.model_dump(mode="json")],
    }


def _same_direct_request(
    record: LifecycleOperationRecord,
    identity: _DirectRequestIdentity,
) -> bool:
    if record.operationKind != "direct-landing":
        return False
    accepted = direct_landing_input(record)
    expected = {
        "configPath": identity.config.config_path.as_posix(),
        "contractPath": identity.contract.contract_path.as_posix(),
        "effectiveInput": identity.effective_input,
        "approvalNote": identity.request.intent_note.strip(),
        "gatePolicy": _gate_policy_snapshot(identity.config),
        "codeCommit": identity.code_commit,
        "candidateTree": identity.candidate_tree,
    }
    return not any(getattr(accepted, field) != value for field, value in expected.items())


def _direct_landing_observation(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
) -> dict[str, object]:
    projection = operation_projection(record, contract=contract).model_dump(
        mode="json", exclude_none=True
    )
    return {
        "ok": False,
        "operation": "direct_landing",
        "state": "refused",
        "status": "direct-landing-operation-action-required",
        "summary": "The accepted direct-landing generation already exists; use its "
        "advertised task-addressed action.",
        "contractPath": record.contractPath,
        "dryRun": False,
        "lifecycleOperation": projection,
    }


def _load_direct_ledger(path: Path):
    try:
        return load_ledger(path)
    except (LedgerError, OSError) as exc:
        raise DirectLandingError(
            "direct-landing-ledger-invalid",
            "direct landing cannot parse the accepted ledger",
            observed=public_failure_evidence(
                stage="direct-ledger-read",
                side="ledger",
                name=path.name,
                error_type=type(exc).__name__,
                observed={"state": "unreadable"},
            ),
        ) from exc


def _read_direct_ledger_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DirectLandingError(
            "direct-landing-ledger-unreadable",
            "direct landing cannot read the accepted ledger bytes",
            observed=public_failure_evidence(
                stage="direct-ledger-read",
                side="ledger",
                name=path.name,
                error_type=type(exc).__name__,
                observed={"state": "unreadable"},
            ),
        ) from exc


def _gate_policy_snapshot(config: McpRuntimeConfig) -> list[GatePolicyRuleSnapshot]:
    return [
        GatePolicyRuleSnapshot(
            kind=rule.kind,
            delegatedRole=rule.delegated_role,
            requireReviewerVerdict=rule.require_reviewer_verdict,
        )
        for rule in config.orchestration.gate_policy.rules
    ]


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
