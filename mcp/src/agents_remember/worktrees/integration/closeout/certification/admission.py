"""Freeze real closeout authorities before a journal owner can launch certification."""

from __future__ import annotations

from dataclasses import dataclass

from agents_remember.certification.certificate_models import GateCertificate
from agents_remember.certification.certificate_store import ContentAddressedCertificateStore
from agents_remember.certification.frozen_run.authorities import CandidateAuthorityRecords
from agents_remember.certification.lifecycle_admission import (
    CompiledLifecycleAdmission,
    LifecycleAdmissionAuthorities,
    PriorRedAdmissionContext,
    compile_lifecycle_admission,
)
from agents_remember.certification.lifecycle_models import CertificationRecoveryRecord
from agents_remember.certification.lifecycle_recovery import compile_certification_recovery_record
from agents_remember.certification.repository_profiles.authority import load_repository_profile
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.authority import require_repo
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.certification import (
    CertificationPredecessor,
    OperationCertificationState,
    SelectedRecoveryDecision,
)
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    LifecycleOperationRecord,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.modules.quality.certification_records import (
    CertificationRunTarget,
    PreparedCertificationRun,
    prepare_certification_records,
)
from agents_remember.worktrees.modules.quality.gate import QualityGateTarget
from agents_remember.worktrees.queue.closeout_staged_quality import (
    PreparedStagedCode,
    prepare_staged_code,
)
from agents_remember.worktrees.route_review import RouteReviewError, require_current_route_review
from agents_remember.worktrees.worktree_contract import WorktreeContract

from .observation import (
    CandidateObservationRequest,
    ObservedCertificationCandidate,
    observe_certification_candidate,
    refuse,
)
from .recovery import (
    RecoveryInputSnapshot,
    build_prior_red_context,
    derive_certificate_input_changes,
)
from .retained_output import require_retained_output_currentness
from .selection import (
    LoadedCertificationSelection,
    require_selected_certification,
    select_certification_state,
)


@dataclass(frozen=True)
class FrozenCloseoutAdmission:
    prepared: PreparedCertificationRun
    observed: ObservedCertificationCandidate
    admission: CompiledLifecycleAdmission
    recovery: CertificationRecoveryRecord
    predecessor: LoadedCertificationSelection | None


def prepare_closeout_certification(
    contract: WorktreeContract,
    operation_input: CloseoutOperationInput,
    current: LifecycleOperationRecord | None,
    *,
    candidate_tree: str,
) -> FrozenCloseoutAdmission | None:
    """An existing generation is observed; only a new admission runs strict preparation."""
    if current is not None and current.status in {"queued", "running"}:
        validate_selected_currentness(contract, operation_input, current)
        return None
    predecessor = require_selected_certification(contract, current) if current is not None else None
    config = load_config(operation_input.configPath)
    profile_reference = require_repo(config, contract.repo_name).certification_profile
    # A malformed or missing profile refuses before staging or invoking a hook.
    load_repository_profile(contract.repo_name, contract.code_worktree, profile_reference)
    target = QualityGateTarget(
        contract.code_worktree, contract.worktree_group, contract.repo_name, profile_reference
    )
    try:
        preparation = prepare_staged_code(target, candidate_tree=candidate_tree)
    except RuntimeError as error:
        raise CertificationContractError(
            "strict preparation refused before certification admission",
            (
                {
                    "code": "candidate-preparation-refused",
                    "path": str(contract.code_worktree),
                    "expected": candidate_tree,
                    "observed": str(error),
                },
            ),
        ) from error
    prepared = prepare_certification_records(
        CertificationRunTarget(
            contract.repo_name, contract.code_worktree, profile_reference, contract.worktree_group
        ),
        mode="targeted",
        candidate_tree=preparation.candidate_tree,
        diff_base=contract.code_base_commit,
    )
    observed = observe_certification_candidate(
        CandidateObservationRequest(
            contract,
            operation_input.effectiveInput,
            prepared.frozen_run.repositoryProfile,
            preparation,
            prepared.provenance,
        )
    )
    prior_red = _prior_red_context(predecessor, prepared, observed, operation_input)
    admission = compile_lifecycle_admission(
        _authorities(prepared, observed), provenance=prepared.provenance, prior_red=prior_red
    )
    changes = (
        derive_certificate_input_changes(
            RecoveryInputSnapshot(
                predecessor.run, predecessor.admission.lifecycle.semanticEnvelope.candidate
            ),
            RecoveryInputSnapshot(prepared.frozen_run, observed.candidate),
        )
        if predecessor is not None
        else ()
    )
    recovery = compile_certification_recovery_record(
        admission, _certificates(predecessor), changes, provenance=prepared.provenance
    )
    return FrozenCloseoutAdmission(prepared, observed, admission, recovery, predecessor)


def _authorities(
    prepared: PreparedCertificationRun, observed: ObservedCertificationCandidate
) -> LifecycleAdmissionAuthorities:
    run = prepared.frozen_run
    return LifecycleAdmissionAuthorities(
        run.registry,
        run.certificationPlan,
        run.repositoryProfile,
        run.repositoryPlan,
        observed.candidate,
    )


def _certificates(prior: LoadedCertificationSelection | None) -> tuple[GateCertificate, ...]:
    if prior is None:
        return ()
    return tuple(item.certificate for item in prior.terminals if item.certificate is not None)


def _prior_red_context(
    prior: LoadedCertificationSelection | None,
    prepared: PreparedCertificationRun,
    observed: ObservedCertificationCandidate,
    operation_input: CloseoutOperationInput,
) -> PriorRedAdmissionContext | None:
    red = (
        tuple(item.result for item in prior.terminals if item.result.disposition == "red")
        if prior
        else ()
    )
    dispositions = operation_input.correctiveDispositions
    if not red:
        if dispositions:
            refuse("prior-red-catalog-missing", "selected prior red catalog", None)
        return None
    # The loaded predecessor's exact prefix permits only its last terminal to be red.
    assert prior is not None
    return build_prior_red_context(
        prior.run,
        RecoveryInputSnapshot(prepared.frozen_run, observed.candidate),
        red[0],
        dispositions,
        _certificates(prior),
    )


def _retain_original_authorities(
    store: ContentAddressedCertificateStore, observed: CandidateAuthorityRecords
) -> CandidateAuthorityRecords:
    path = store.exact_path("candidate-authorities", observed.authorityDigest)
    if path.exists() or path.is_symlink():
        original = store.load(CandidateAuthorityRecords, observed.authorityDigest)
        if original.semanticEnvelope != observed.semanticEnvelope:
            refuse("candidate-authorities-collision", observed, original)
        return original
    store.publish(observed)
    return store.load(CandidateAuthorityRecords, observed.authorityDigest)


def select_initial_certification(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    record: LifecycleOperationRecord,
    frozen: FrozenCloseoutAdmission | None,
) -> LifecycleOperationRecord:
    if frozen is None:
        require_selected_certification(contract, record)
        return record
    selected = initial_certification_state(contract, record, frozen)
    return select_certification_state(contract, store, record, selected)


def initial_certification_state(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    frozen: FrozenCloseoutAdmission | None,
) -> OperationCertificationState:
    """Publish/read back exact originals for the store's atomic initial selection."""
    if frozen is None:
        refuse("certification-admission-missing", "explicit new frozen admission", None)
    assert frozen is not None
    if record.certification is not None:
        refuse(
            "certification-selection-already-exists",
            "new generation without selection",
            record.generation,
        )
    objects = frozen.prepared.certificate_store()
    retained_authorities = _retain_original_authorities(objects, frozen.observed.authorities)
    objects.publish(frozen.admission.lifecycle)
    prior_red = frozen.admission.priorRedDisposition
    if prior_red is not None:
        objects.publish(prior_red)
    objects.publish(frozen.recovery)
    predecessor = None
    inputs = ()
    if frozen.predecessor is not None:
        prior = frozen.predecessor.state
        predecessor = CertificationPredecessor(
            operationKey=prior.operationKey,
            generation=prior.generation,
            frozenRun=prior.frozenRun,
            candidateAuthorities=prior.candidateAuthorities,
            lifecycleAdmission=prior.lifecycleAdmission,
        )
        inputs = prior.terminals
    reused = {
        item.certificateDigest
        for item in frozen.recovery.semanticEnvelope.reusePlan.reusedCertificates
    }
    terminals = tuple(
        item.model_copy(update={"reusedFrom": item.reusedFrom or predecessor})
        for item in inputs
        if item.certificate is not None and item.certificate.semanticDigest in reused
    )
    selected = OperationCertificationState(
        operationKey=record.operationKey,
        generation=record.generation,
        frozenRun=frozen.prepared.frozen_reference,
        candidateAuthorities=objects.reference(
            "candidate-authorities", retained_authorities.authorityDigest
        ),
        lifecycleAdmission=objects.reference(
            "lifecycle-admission", frozen.admission.lifecycle.admissionDigest
        ),
        priorRedDisposition=objects.reference("prior-red-disposition", prior_red.dispositionDigest)
        if prior_red
        else None,
        predecessor=predecessor,
        inputTerminals=inputs,
        recoveryDecisions=(
            SelectedRecoveryDecision(
                reference=objects.reference("recovery", frozen.recovery.recoveryDigest)
            ),
        ),
        terminals=terminals,
    )
    require_selected_certification(contract, record.model_copy(update={"certification": selected}))
    return selected


def validate_selected_currentness(
    contract: WorktreeContract,
    operation_input: CloseoutOperationInput,
    record: LifecycleOperationRecord,
) -> LoadedCertificationSelection:
    selected = require_selected_certification(contract, record)
    profile = require_repo(
        load_config(operation_input.configPath), contract.repo_name
    ).certification_profile
    current_profile = load_repository_profile(contract.repo_name, contract.code_worktree, profile)
    if current_profile.canonical != selected.run.repositoryProfile:
        refuse(
            "selected-profile-moved",
            selected.run.repositoryProfile.profileDigest,
            current_profile.canonical.profileDigest,
        )
    rules = selected.authorities.semanticEnvelope.worktree
    current = observe_certification_candidate(
        CandidateObservationRequest(
            contract,
            operation_input.effectiveInput,
            selected.run.repositoryProfile,
            PreparedStagedCode(rules.stagedTree, rules.preCommitHookRan),
            selected.run.provenance,
        )
    )
    expected = selected.admission.lifecycle.semanticEnvelope.candidate
    if (
        current.candidate != expected
        or current.authorities.semanticEnvelope != selected.authorities.semanticEnvelope
    ):
        require_retained_output_currentness(contract, record, selected, current)
    try:
        require_current_route_review(contract)
    except RouteReviewError as error:
        raise CertificationContractError(
            "selected certification route-review authority refused",
            ({"code": error.status, "path": "routeReview", "detail": str(error)},),
        ) from error
    return selected
