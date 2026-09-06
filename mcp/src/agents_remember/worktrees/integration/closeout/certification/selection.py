"""Publish/read back certification objects, then select them through the operation CAS."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.certification.certificate_models import GateCertificate, GateFiveSemanticInputs
from agents_remember.certification.certificate_store import ContentAddressedCertificateStore
from agents_remember.certification.digests import content_digest
from agents_remember.certification.frozen_run.authorities import CandidateAuthorityRecords
from agents_remember.certification.frozen_run.models import FrozenCertificationRun
from agents_remember.certification.lifecycle_admission import (
    CompiledLifecycleAdmission,
    LifecycleAdmissionAuthorities,
    compile_lifecycle_admission,
)
from agents_remember.certification.lifecycle_models import (
    CertificationRecoveryRecord,
    LifecycleAdmissionManifest,
    PriorRedDispositionManifest,
)
from agents_remember.certification.lifecycle_recovery import compile_certification_recovery_record
from agents_remember.certification.models import GateResultAdmission, GateResultManifest
from agents_remember.certification.results import compile_gate_result_manifest
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.certification import (
    CertificationPredecessor,
    OperationCertificationState,
    RetainedCertificationBytes,
    SelectedGateTerminal,
    SelectedRecoveryDecision,
)
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.worktrees.integration.lifecycle.certification_observation import (
    observe_certification_publication,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules.quality.certification_evidence import (
    verify_publication_authority,
    verify_result_evidence,
    verify_terminal_publication_authority,
)
from agents_remember.worktrees.modules.quality.certification_records import certificate_store
from agents_remember.worktrees.modules.quality.certification_terminal import (
    RecordedCertificationGeneration,
    RecordedGateTerminal,
    catalog_gates,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    parse_published_quality_manifest,
    published_manifest_payload,
    require_real_file_or_missing,
)
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract

from .observation import refuse
from .recovery import RecoveryInputSnapshot, build_prior_red_context

MAX_SELECTED_GENERATIONS = 256


def load_typed[Object](
    store: ContentAddressedCertificateStore,
    reference: CertificateObjectReference,
    model: type[Object],
) -> Object:
    value = store.load_reference(reference)
    if not isinstance(value, model):
        refuse("selected-certification-object-kind", model.__name__, reference.kind)
    return value


@dataclass(frozen=True)
class LoadedCertificationSelection:
    state: OperationCertificationState
    run: FrozenCertificationRun
    authorities: CandidateAuthorityRecords
    admission: CompiledLifecycleAdmission
    recovery: CertificationRecoveryRecord
    terminals: tuple[RecordedGateTerminal, ...]
    protected_generations: frozenset[str]


def require_unchanged_retry_admissible(selected: LoadedCertificationSelection) -> None:
    """A selected red catalog requires an explicit corrective successor, never a retry."""
    red = tuple(item.result for item in selected.terminals if item.result.disposition == "red")
    if red:
        refuse(
            "unchanged-red-recovery",
            "explicit corrective successor",
            {"selectedRedCatalogs": [item.manifestDigest for item in red]},
        )


def require_selected_certification(
    contract: WorktreeContract, record: LifecycleOperationRecord
) -> LoadedCertificationSelection:
    """Read only explicit generation edges; never search for a reusable predecessor."""
    return _load_selection(contract, record, {}, MAX_SELECTED_GENERATIONS)


def _load_selection(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    cache: dict[int, LoadedCertificationSelection],
    remaining: int,
) -> LoadedCertificationSelection:
    if remaining == 0:
        refuse("selected-history-capacity", MAX_SELECTED_GENERATIONS, record.generation)
    if record.generation in cache:
        return cache[record.generation]
    selected = record.certification
    if selected is None:
        refuse("certification-selection-missing", "journal-selected immutable authority", None)
    if (
        selected.operationKey != record.operationKey
        or selected.generation != record.generation
        or record.contractPath != contract.contract_path.as_posix()
        or record.operationKind != "closeout"
        or record.taskId != contract.task_id
    ):
        refuse("certification-selection-owner-mismatch", record.operationKey, selected.operationKey)
    store = certificate_store(contract.worktree_group)
    run = load_typed(store, selected.frozenRun, FrozenCertificationRun)
    authorities = load_typed(store, selected.candidateAuthorities, CandidateAuthorityRecords)
    lifecycle = load_typed(store, selected.lifecycleAdmission, LifecycleAdmissionManifest)
    prior_red = (
        load_typed(store, selected.priorRedDisposition, PriorRedDispositionManifest)
        if selected.priorRedDisposition is not None
        else None
    )
    prior = _load_predecessor(contract, record, selected, cache, remaining)
    recoveries = tuple(
        load_typed(store, decision.reference, CertificationRecoveryRecord)
        for decision in selected.recoveryDecisions
    )
    envelope = lifecycle.semanticEnvelope
    if (
        run.repositoryPlan.candidateIdentity.value != record.candidateTree
        or envelope.candidate.contractPath != record.contractPath
        or envelope.candidate.candidateCodeTree != run.repositoryPlan.candidateIdentity
        or envelope.certificationAdmissionDigest != run.admission.admissionDigest
        or envelope.registryDigest != run.registry.registryDigest
        or envelope.certificationPlanDigest != run.certificationPlan.planDigest
        or envelope.repositoryProfileDigest != run.repositoryProfile.profileDigest
        or envelope.repositoryPlanDigest != run.repositoryPlan.planDigest
        or envelope.priorRedDispositionDigest
        != (prior_red.dispositionDigest if prior_red is not None else None)
    ):
        refuse("certification-selection-binding-mismatch", "one exact admitted candidate", selected)
    candidate = envelope.candidate
    authority = authorities.semanticEnvelope
    if (
        candidate.mutationAuthorityDigest != content_digest(authority.mutation)
        or candidate.sourceAuthorityDigest != content_digest(authority.source)
        or candidate.worktreeRuleDigest != content_digest(authority.worktree)
        or candidate.generatedInputsDigest != content_digest(authority.generated)
    ):
        refuse("certification-owner-projections-mismatch", authorities.authorityDigest, candidate)
    admission = _recompile_admission(run, lifecycle, prior_red, prior)
    _require_inherited_terminals(selected, prior)
    terminals = tuple(
        load_selected_terminal(contract.worktree_group / "reports", store, run, terminal)
        for terminal in selected.terminals
    )
    protected = {terminal.publication.generation for terminal in terminals}
    if prior is not None:
        protected.update(prior.protected_generations)
    for terminal in selected.terminalHistory:
        retained = load_selected_terminal(contract.worktree_group / "reports", store, run, terminal)
        _require_interrupted_terminal(contract.worktree_group / "reports", retained)
        protected.add(retained.publication.generation)
    _require_terminal_chain(run, terminals)
    _require_recovery_history(admission, recoveries, selected.recoveryDecisions, prior, terminals)
    loaded = LoadedCertificationSelection(
        selected,
        run,
        authorities,
        admission,
        recoveries[-1],
        terminals,
        frozenset(protected),
    )
    cache[record.generation] = loaded
    return loaded


def _load_predecessor(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    selected: OperationCertificationState,
    cache: dict[int, LoadedCertificationSelection],
    remaining: int,
) -> LoadedCertificationSelection | None:
    predecessor = selected.predecessor
    if predecessor is None:
        if record.predecessorFingerprint or selected.inputTerminals:
            refuse("selected-predecessor-missing", "explicit original generation", None)
        return None
    if predecessor.generation != record.generation - 1:
        refuse("selected-predecessor-generation", record.generation - 1, predecessor.generation)
    path = operation_record_path(contract.worktree_group, "closeout")
    archive = path.with_name(f"{path.stem}.generation-{predecessor.generation}.json")
    require_real_file_or_missing(archive, purpose="selected certification predecessor")
    original = LifecycleOperationStore(archive).read()
    if original is None or (
        original.operationKey != predecessor.operationKey
        or original.generation != predecessor.generation
        or original.fingerprint != record.predecessorFingerprint
        or original.successorFingerprint != record.fingerprint
    ):
        refuse("selected-predecessor-journal-mismatch", predecessor, original)
    loaded = _load_selection(contract, original, cache, remaining - 1)
    if _predecessor_identity(loaded.state) != predecessor:
        refuse("selected-predecessor-authority-mismatch", predecessor, loaded.state)
    if selected.inputTerminals != loaded.state.terminals:
        refuse("selected-input-terminals-mismatch", loaded.state.terminals, selected.inputTerminals)
    return loaded


def _predecessor_identity(selected: OperationCertificationState) -> CertificationPredecessor:
    return CertificationPredecessor(
        operationKey=selected.operationKey,
        generation=selected.generation,
        frozenRun=selected.frozenRun,
        candidateAuthorities=selected.candidateAuthorities,
        lifecycleAdmission=selected.lifecycleAdmission,
    )


def _require_inherited_terminals(
    selected: OperationCertificationState, prior: LoadedCertificationSelection | None
) -> None:
    for terminal in selected.terminals:
        if terminal.reusedFrom is None:
            continue
        original = next(
            (item for item in selected.inputTerminals if item.gate == terminal.gate), None
        )
        if original is None or prior is None:
            refuse("selected-reused-terminal-unbound", "selected original input", terminal)
        expected = original.model_copy(
            update={"reusedFrom": original.reusedFrom or _predecessor_identity(prior.state)}
        )
        if terminal != expected:
            refuse("selected-reused-terminal-mismatch", expected, terminal)


def _recompile_admission(
    run: FrozenCertificationRun,
    lifecycle: LifecycleAdmissionManifest,
    prior_red: PriorRedDispositionManifest | None,
    prior: LoadedCertificationSelection | None,
) -> CompiledLifecycleAdmission:
    context = None
    red = (
        tuple(item.result for item in prior.terminals if item.result.disposition == "red")
        if prior
        else ()
    )
    if bool(red) != (prior_red is not None):
        refuse("selected-prior-red-mismatch", "exact selected red disposition", prior_red)
    if prior_red is not None:
        # A loaded predecessor has an exact prefix with at most one final red terminal.
        assert prior is not None and red
        context = build_prior_red_context(
            prior.run,
            RecoveryInputSnapshot(run, lifecycle.semanticEnvelope.candidate),
            red[0],
            prior_red.semanticEnvelope.dispositions,
            tuple(item.certificate for item in prior.terminals if item.certificate is not None),
        )
    compiled = compile_lifecycle_admission(
        LifecycleAdmissionAuthorities(
            run.registry,
            run.certificationPlan,
            run.repositoryProfile,
            run.repositoryPlan,
            lifecycle.semanticEnvelope.candidate,
        ),
        provenance=lifecycle.provenance,
        prior_red=context,
    )
    if compiled.lifecycle != lifecycle or compiled.priorRedDisposition != prior_red:
        refuse("selected-admission-recompile-mismatch", compiled.lifecycle, lifecycle)
    return compiled


def _require_terminal_chain(
    run: FrozenCertificationRun, terminals: tuple[RecordedGateTerminal, ...]
) -> None:
    gates = tuple(item.result.gate for item in terminals)
    if gates != tuple(range(1, len(terminals) + 1)):
        refuse("selected-terminal-prefix-invalid", "exact ordered terminal prefix", gates)
    certificates = tuple(item.certificate for item in terminals if item.certificate is not None)
    if any(item.certificate is None for item in terminals[:-1]):
        refuse("selected-terminal-after-red", "zero later gate terminals", gates)
    memory = certificates[-1].semanticEnvelope.gateFiveInputs if certificates else None
    validate_certificate_chain(run.admission, certificates, gate_five_inputs=memory)


def _require_recovery_history(
    admission: CompiledLifecycleAdmission,
    recoveries: tuple[CertificationRecoveryRecord, ...],
    decisions: tuple[SelectedRecoveryDecision, ...],
    prior: LoadedCertificationSelection | None,
    terminals: tuple[RecordedGateTerminal, ...],
) -> None:
    available = (*(() if prior is None else prior.terminals), *terminals)
    certificates = {
        item.certificate.identity: item.certificate
        for item in available
        if item.certificate is not None
    }
    for recovery, decision in zip(recoveries, decisions, strict=True):
        identities = recovery.semanticEnvelope.admittedCertificates
        if any(identity not in certificates for identity in identities):
            refuse("selected-recovery-certificates-missing", identities, tuple(certificates))
        rebuilt = compile_certification_recovery_record(
            admission,
            tuple(certificates[identity] for identity in identities),
            recovery.semanticEnvelope.inputChanges,
            provenance=recovery.provenance,
            gate_five_inputs=recovery_memory_inputs(decision),
        )
        if rebuilt != recovery:
            refuse("selected-recovery-mismatch", rebuilt, recovery)


def load_selected_terminal(
    reports: Path,
    store: ContentAddressedCertificateStore,
    current_run: FrozenCertificationRun,
    selected: SelectedGateTerminal,
) -> RecordedGateTerminal:
    result = load_typed(store, selected.result, GateResultManifest)
    certificate = (
        load_typed(store, selected.certificate, GateCertificate)
        if selected.certificate is not None
        else None
    )
    publication = parse_published_quality_manifest(json.loads(selected.publication.canonicalBytes))
    run = (
        load_typed(store, selected.reusedFrom.frozenRun, FrozenCertificationRun)
        if selected.reusedFrom is not None
        else current_run
    )
    verify_terminal_publication_authority(run, result, publication)
    verify_result_evidence(reports, publication, result.railResults)
    gate_plan = run.certificationPlan.gates[selected.gate - 1]
    rebuilt = compile_gate_result_manifest(
        run.registry,
        run.certificationPlan,
        gate_plan,
        result.railResults,
        GateResultAdmission(
            candidateIdentity=run.repositoryPlan.candidateIdentity,
            profileId=run.repositoryPlan.selectionId,
            altitude=result.altitude,
        ),
    )
    if rebuilt != result:
        refuse("selected-gate-result-mismatch", rebuilt, result)
    if certificate is not None:
        verify_publication_authority(certificate, result, publication)
    # Recompilation binds the selected gate; publication validation binds the certificate
    # gate and result digest. A selected certificate must still name a green result.
    if certificate is not None and result.disposition != "green":
        refuse("selected-gate-terminal-mismatch", selected.gate, result.gate)
    return RecordedGateTerminal(
        result, selected.result, publication, certificate, selected.certificate
    )


def terminal_selection(terminal: RecordedGateTerminal) -> SelectedGateTerminal:
    encoded = json.dumps(
        published_manifest_payload(terminal.publication),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return SelectedGateTerminal(
        gate=terminal.result.gate,
        result=terminal.resultReference,
        certificate=terminal.certificateReference,
        publication=RetainedCertificationBytes(
            canonicalBytes=encoded,
            contentSha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        ),
    )


def recovery_memory_inputs(decision: SelectedRecoveryDecision) -> GateFiveSemanticInputs | None:
    retained = decision.memoryInputs
    if retained is None:
        return None
    try:
        inputs = GateFiveSemanticInputs.model_validate_json(retained.canonicalBytes)
    except ValueError as error:
        refuse("selected-memory-inputs-invalid", "canonical Gate-5 inputs", str(error))
    if retain_memory_inputs(inputs) != retained:
        refuse("selected-memory-inputs-noncanonical", "exact canonical bytes", retained)
    return inputs


def retain_memory_inputs(inputs: GateFiveSemanticInputs) -> RetainedCertificationBytes:
    encoded = json.dumps(
        inputs.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return RetainedCertificationBytes(
        canonicalBytes=encoded,
        contentSha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def _require_interrupted_terminal(reports: Path, terminal: RecordedGateTerminal) -> None:
    publication = terminal.publication
    path = published_report_path_from_manifest(
        reports, publication, publication.result_decoder.artifactPath
    )
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        refuse("selected-interruption-catalog-invalid", "original decoder object", payload)
    gate = next(
        (item for item in catalog_gates(payload) if item["gate"] == terminal.result.gate), None
    )
    if (
        terminal.result.disposition != "green"
        or gate is None
        or gate.get("disposition") != "interrupted"
    ):
        refuse("selected-terminal-not-interrupted", "explicit interrupted original gate", gate)


def select_certification_state(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    observed: LifecycleOperationRecord,
    selected: OperationCertificationState,
) -> LifecycleOperationRecord:
    """Read back every immutable object before the sole owner CAS selects it."""
    if observed.cancelRequested or observed.status not in {"queued", "running"}:
        refuse("certification-selection-not-current", "live noncancelled owner", observed.status)
    proposed = observed.model_copy(update={"certification": selected})
    require_selected_certification(contract, proposed)
    observed = observe_certification_publication(store, observed)
    proposed = observed.model_copy(update={"certification": selected})
    current, won = store.update_if_current(observed, lambda _record: proposed)
    if not won:
        refuse("certification-selection-cas-lost", observed.recordRevision, current.recordRevision)
    if current.certification != selected:
        refuse("certification-selection-readback-mismatch", selected, current.certification)
    return current


def select_recorded_terminals(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    observed: LifecycleOperationRecord,
    recorded: RecordedCertificationGeneration,
) -> LifecycleOperationRecord:
    selected = observed.certification
    if selected is None:
        refuse("certification-selection-missing", "selected frozen run before execution", None)
    existing = {item.gate: item for item in selected.terminals}
    combined = list(selected.terminals)
    history = list(selected.terminalHistory)
    for terminal in recorded.terminals:
        proposed = terminal_selection(terminal)
        previous = existing.get(proposed.gate)
        if previous is not None:
            if previous.model_copy(update={"reusedFrom": None}) != proposed:
                if previous.certificate is not None or previous != selected.terminals[-1]:
                    refuse("selected-terminal-replacement", previous, proposed)
                retained = load_selected_terminal(
                    contract.worktree_group / "reports",
                    certificate_store(contract.worktree_group),
                    require_selected_certification(contract, observed).run,
                    previous,
                )
                _require_interrupted_terminal(contract.worktree_group / "reports", retained)
                history.append(previous)
                combined[combined.index(previous)] = proposed
            continue
        combined.append(proposed)
    replacement = selected.model_copy(
        update={"terminals": tuple(combined), "terminalHistory": tuple(history)}
    )
    return select_certification_state(contract, store, observed, replacement)
