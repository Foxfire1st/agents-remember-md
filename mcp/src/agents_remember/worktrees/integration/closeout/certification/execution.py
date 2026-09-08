"""Execute only the gate suffix selected by the current closeout journal owner."""

from __future__ import annotations

from dataclasses import dataclass, replace

from agents_remember.certification.certificate_invalidation import CertificateInputChange
from agents_remember.certification.certificate_models import GateFiveSemanticInputs
from agents_remember.certification.frozen_run.models import FrozenCertificationRun
from agents_remember.certification.lifecycle_recovery import compile_certification_recovery_record
from agents_remember.kernel.authority import require_repo
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.certification import SelectedRecoveryDecision
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    LifecycleOperationRecord,
)
from agents_remember.worktrees.integration.lifecycle.certification_observation import (
    observe_certification_publication,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.modules.quality.certification_records import certificate_store
from agents_remember.worktrees.modules.quality.certification_run import SelectedCodeCertification
from agents_remember.worktrees.modules.quality.certification_terminal import (
    RecordedCertificationGeneration,
    RecordedGateTerminal,
)
from agents_remember.worktrees.modules.quality.execution.models import (
    CodeCertificationExecution,
    RetainedGateExecution,
)
from agents_remember.worktrees.modules.quality.gate import (
    QualityGatePlan,
    QualityGateTarget,
    closeout_profile_purpose,
    run_strict_code_quality_gate,
)
from agents_remember.worktrees.services import CertificationContinuationPort, worktree_services
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract

from .admission import validate_selected_currentness
from .observation import refuse
from .recovery import RecoveryInputSnapshot, derive_certificate_input_changes
from .selection import (
    LoadedCertificationSelection,
    load_selected_terminal,
    recovery_memory_inputs,
    require_unchanged_retry_admissible,
    retain_memory_inputs,
    select_certification_state,
    select_recorded_terminals,
)


@dataclass(frozen=True)
class CloseoutCertificationHandoff:
    """Exact owner and selected objects passed to memory or finalization composition."""

    contract: WorktreeContract
    record: LifecycleOperationRecord
    store: LifecycleOperationStore
    selected: LoadedCertificationSelection


def current_certification_handoff(
    contract: WorktreeContract,
    owner: LifecycleOperationRecord,
    store: LifecycleOperationStore,
) -> CloseoutCertificationHandoff:
    """Reprove the live worker and selected authorities for a continuation action."""
    current = store.read()
    if current is None or (
        current.operationKey != owner.operationKey
        or current.generation != owner.generation
        or current.workerLease != owner.workerLease
        or current.workerPid != owner.workerPid
        or current.status != "running"
        or current.cancelRequested
    ):
        refuse(
            "certification-worker-no-longer-current",
            owner.operationKey,
            None if current is None else current.operationKey,
        )
    if not isinstance(current.input, CloseoutOperationInput):
        refuse("certification-worker-kind", "closeout", current.operationKind)
    actual = load_contract(contract.contract_path)
    selected = validate_selected_currentness(actual, current.input, current)
    return CloseoutCertificationHandoff(actual, current, store, selected)


def _execution_inputs(handoff: CloseoutCertificationHandoff) -> CodeCertificationExecution:
    selected = handoff.selected
    pool = {
        item.certificate.certificateDigest: item.certificate
        for item in selected.terminals
        if item.certificate is not None
    }
    for terminal in _original_input_terminals(handoff):
        if terminal.certificate is not None:
            pool[terminal.certificate.certificateDigest] = terminal.certificate
    identities = selected.recovery.semanticEnvelope.admittedCertificates
    # The selected loader has recompiled recovery against this exact original pool.
    certificates = tuple(pool[identity.certificateDigest] for identity in identities)
    reused = selected.recovery.semanticEnvelope.reusePlan.reusedCertificates
    retained = tuple(
        RetainedGateExecution(item.certificate, item.result, item.publication)
        for item in selected.terminals
        if item.certificate is not None and item.certificate.identity in reused
    )
    execution = CodeCertificationExecution(
        selected.run,
        selected.recovery.semanticEnvelope.reusePlan,
        selected.recovery.semanticEnvelope.inputChanges,
        certificates,
        retained,
    )
    execution.validate()
    return execution


def _original_input_terminals(
    handoff: CloseoutCertificationHandoff,
) -> tuple[RecordedGateTerminal, ...]:
    predecessor = handoff.selected.state.predecessor
    if predecessor is None:
        return ()
    objects = certificate_store(handoff.contract.worktree_group)
    original_run = objects.load_reference(predecessor.frozenRun)
    assert isinstance(original_run, FrozenCertificationRun)
    return tuple(
        load_selected_terminal(
            handoff.contract.worktree_group / "reports", objects, original_run, item
        )
        for item in handoff.selected.state.inputTerminals
    )


def _inherited_memory_terminal(
    handoff: CloseoutCertificationHandoff,
) -> RecordedGateTerminal | None:
    selected = handoff.selected
    if len(selected.terminals) != 4 or len(selected.state.inputTerminals) != 5:
        return None
    original = _original_input_terminals(handoff)[-1]
    return original if original.certificate is not None else None


def _observed_recovery_changes(
    selected: LoadedCertificationSelection,
    original_memory: GateFiveSemanticInputs | None,
    memory_inputs: GateFiveSemanticInputs | None,
) -> tuple[CertificateInputChange, ...]:
    if original_memory is not None and memory_inputs is None:
        return (
            CertificateInputChange(
                changeClass="memory-onboarding",
                reason="Current memory authority is unavailable; the original Gate 5 cannot be reused.",
            ),
        )
    candidate = selected.admission.lifecycle.semanticEnvelope.candidate
    return derive_certificate_input_changes(
        RecoveryInputSnapshot(selected.run, candidate, original_memory),
        RecoveryInputSnapshot(selected.run, candidate, memory_inputs),
    )


def _advance_recovery(
    handoff: CloseoutCertificationHandoff,
    *,
    memory_inputs: GateFiveSemanticInputs | None,
    inherited_memory: RecordedGateTerminal | None = None,
) -> CloseoutCertificationHandoff:
    """Append a decision from actual newly selected green terminals after execution."""
    terminals = handoff.selected.terminals
    require_unchanged_retry_admissible(handoff.selected)
    chain = tuple(item.certificate for item in terminals if item.certificate is not None)
    if inherited_memory is not None and inherited_memory.certificate is not None:
        chain = (*chain, inherited_memory.certificate)
    original_memory = chain[-1].semanticEnvelope.gateFiveInputs if chain else None
    changes = _observed_recovery_changes(handoff.selected, original_memory, memory_inputs)
    recovery = compile_certification_recovery_record(
        handoff.selected.admission,
        chain,
        changes,
        provenance=handoff.selected.run.provenance,
        gate_five_inputs=memory_inputs,
    )
    if len(terminals) == 5 and recovery.semanticEnvelope.reusePlan.firstGateToRun is not None:
        refuse(
            "certification-memory-successor-required",
            "explicit successor for changed selected Gate-5 inputs",
            recovery.semanticEnvelope.reusePlan,
        )
    objects = certificate_store(handoff.contract.worktree_group)
    retained = retain_memory_inputs(memory_inputs) if memory_inputs is not None else None
    if (
        recovery == handoff.selected.recovery
        and retained == handoff.selected.state.recoveryDecisions[-1].memoryInputs
    ):
        return handoff
    objects.publish(recovery)
    decision = SelectedRecoveryDecision(
        reference=objects.reference("recovery", recovery.recoveryDigest), memoryInputs=retained
    )
    state = handoff.selected.state
    selected_terminals = state.terminals
    if inherited_memory is not None and recovery.semanticEnvelope.reusePlan.firstGateToRun is None:
        original = state.inputTerminals[-1]
        inherited = original.model_copy(
            update={"reusedFrom": original.reusedFrom or state.predecessor}
        )
        selected_terminals = (*selected_terminals, inherited)
    updated = state.model_copy(
        update={
            "recoveryDecisions": (*state.recoveryDecisions, decision),
            "terminals": selected_terminals,
        }
    )
    record = select_certification_state(handoff.contract, handoff.store, handoff.record, updated)
    return current_certification_handoff(handoff.contract, record, handoff.store)


def _run_code(handoff: CloseoutCertificationHandoff) -> CloseoutCertificationHandoff:
    execution = _execution_inputs(handoff)
    operation_input = handoff.record.input
    assert isinstance(operation_input, CloseoutOperationInput)
    profile_reference = require_repo(
        load_config(operation_input.configPath), handoff.contract.repo_name
    ).certification_profile

    def select_terminals(recorded: RecordedCertificationGeneration) -> None:
        current = current_certification_handoff(handoff.contract, handoff.record, handoff.store)
        select_recorded_terminals(current.contract, current.store, current.record, recorded)

    def protected_generations() -> frozenset[str]:
        current = current_certification_handoff(handoff.contract, handoff.record, handoff.store)
        return current.selected.protected_generations

    def authorize_start() -> None:
        # The full selected graph was already verified. Immediately before launch,
        # require its exact live owner; only concurrent heartbeat progress may differ.
        observe_certification_publication(handoff.store, handoff.record)

    purpose = closeout_profile_purpose(handoff.contract)
    if execution.run.repositoryPlan.purpose != purpose:
        raise ValueError("frozen closeout purpose differs from current atomic owner")
    run_strict_code_quality_gate(
        QualityGateTarget(
            handoff.contract.code_worktree,
            handoff.contract.worktree_group,
            handoff.contract.repo_name,
            profile_reference,
            purpose,
        ),
        diff_base=handoff.contract.code_base_commit,
        plan=QualityGatePlan(
            mode=execution.run.repositoryPlan.mode,
            selected=SelectedCodeCertification(
                execution, select_terminals, protected_generations, authorize_start
            ),
        ),
    )
    return _advance_recovery(
        current_certification_handoff(handoff.contract, handoff.record, handoff.store),
        memory_inputs=None,
    )


def _observe_current_memory(
    handoff: CloseoutCertificationHandoff, continuation: CertificationContinuationPort
) -> GateFiveSemanticInputs | None:
    inputs = continuation.observe_memory(handoff)
    observe_certification_publication(handoff.store, handoff.record)
    if inputs is None:
        return None
    try:
        return GateFiveSemanticInputs.model_validate(inputs.model_dump(mode="json"))
    except ValueError as error:
        refuse("certification-memory-inputs-invalid", "current canonical Gate-5 inputs", str(error))


def _refresh_selected_recovery(
    handoff: CloseoutCertificationHandoff,
) -> CloseoutCertificationHandoff:
    """Reobserve memory inputs before choosing which selected certificates can be reused."""
    # Preparation imports this module's handoff owners; defer until they are defined.
    from agents_remember.worktrees.integration.closeout.preparation.code_view import (  # noqa: PLC0415
        prepare_code_view,
    )

    selected_certificates = tuple(
        item.certificate.identity
        for item in handoff.selected.terminals
        if item.certificate is not None
    )
    continuation = worktree_services().certification_continuation
    has_memory_certificate = bool(selected_certificates and selected_certificates[-1].gate == 5)
    inherited_memory = _inherited_memory_terminal(handoff)
    memory_inputs = None
    if has_memory_certificate or inherited_memory is not None:
        if continuation is None:
            refuse("certification-continuation-unbound", "registered memory owner", None)
        handoff, _ = prepare_code_view(handoff)
        memory_inputs = _observe_current_memory(handoff, continuation)
    if (
        selected_certificates
        != handoff.selected.recovery.semanticEnvelope.reusePlan.reusedCertificates
        or has_memory_certificate
        or inherited_memory is not None
    ):
        handoff = _advance_recovery(
            handoff, memory_inputs=memory_inputs, inherited_memory=inherited_memory
        )
    return handoff


def execute_selected_closeout(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    store: LifecycleOperationStore,
) -> WorktreeCommandResult:
    """R21 alone controls actual starts; absent downstream composition cannot complete."""
    # Finalization imports preparation, which imports this module's handoff owners.
    from agents_remember.worktrees.integration.closeout.preparation.finalization import (  # noqa: PLC0415
        resume_prepared_closeout,
    )

    recovered = resume_prepared_closeout(contract, record, store)
    if recovered is not None:
        return recovered
    handoff = current_certification_handoff(contract, record, store)
    require_unchanged_retry_admissible(handoff.selected)
    handoff = _refresh_selected_recovery(handoff)
    first = handoff.selected.recovery.semanticEnvelope.reusePlan.firstGateToRun
    if first in (1, 2, 3, 4):
        handoff = _run_code(handoff)
        first = handoff.selected.recovery.semanticEnvelope.reusePlan.firstGateToRun
    continuation = worktree_services().certification_continuation
    if continuation is None:
        refuse(
            "certification-continuation-unbound",
            "registered memory/finalization owner",
            {"firstGateToRun": first},
        )
    if first in (5, None):
        handoff = replace(
            handoff, record=observe_certification_publication(handoff.store, handoff.record)
        )
    if first == 5:
        return continuation.run_memory(handoff)
    if first is None:
        current_memory = _observe_current_memory(handoff, continuation)
        expected_memory = recovery_memory_inputs(handoff.selected.state.recoveryDecisions[-1])
        if current_memory != expected_memory:
            refuse("certification-memory-inputs-moved", expected_memory, current_memory)
        return continuation.finalize(handoff)
    refuse("certification-code-prefix-incomplete", "Gate 5 or finalization", first)
