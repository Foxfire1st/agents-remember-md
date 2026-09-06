"""Select original full-gate authority through the live integration operation CAS."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Never

from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    CertificateReusePlan,
    plan_certificate_reuse,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.frozen_run.models import FrozenCertificationRun
from agents_remember.errors import CertificationContractError
from agents_remember.models.lifecycles.integration_certification import (
    IntegrationCertificationSelection,
    validate_integration_completion_identity,
)
from agents_remember.models.lifecycles.operation import (
    IntegrationQualityCertification,
    LifecycleOperationRecord,
)
from agents_remember.worktrees.integration.closeout.certification.selection import (
    load_selected_terminal,
    terminal_selection,
)
from agents_remember.worktrees.integration.lifecycle.certification_observation import (
    observe_certification_publication,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.modules.git import repository_identity, require_git
from agents_remember.worktrees.modules.quality.certification_records import (
    CertificationRunTarget,
    PreparedCertificationRun,
    certificate_store,
    prepare_certification_records,
    prepared_from_frozen_run,
)
from agents_remember.worktrees.modules.quality.certification_terminal import (
    RecordedCertificationGeneration,
    RecordedGateTerminal,
    catalog_gates,
)
from agents_remember.worktrees.modules.quality.execution.models import (
    CodeCertificationExecution,
    RetainedGateExecution,
)
from agents_remember.worktrees.modules.quality.gate import QualityGatePlan, QualityGateTarget
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract


@dataclass(frozen=True)
class IntegrationCertificationOwner:
    record: LifecycleOperationRecord
    store: LifecycleOperationStore


@dataclass(frozen=True)
class IntegrationCertificationRequest:
    contract: WorktreeContract
    target: QualityGateTarget
    plan: QualityGatePlan
    owner: IntegrationCertificationOwner | None
    attestation: dict[str, str] | None


@dataclass(frozen=True)
class LoadedIntegrationCertification:
    record: LifecycleOperationRecord
    state: IntegrationCertificationSelection
    prepared: PreparedCertificationRun
    terminals: tuple[RecordedGateTerminal, ...]
    input_changes: tuple[CertificateInputChange, ...]
    reuse_plan: CertificateReusePlan

    def execution(self) -> CodeCertificationExecution:
        certificates = tuple(
            item.certificate for item in self.terminals if item.certificate is not None
        )
        retained = tuple(
            RetainedGateExecution(item.certificate, item.result, item.publication)
            for item in self.terminals
            if item.certificate is not None
        )
        return CodeCertificationExecution(
            self.prepared.frozen_run, self.reuse_plan, self.input_changes, certificates, retained
        )


def _refuse(code: str, expected: object, observed: object) -> Never:
    raise CertificationContractError(
        "integration certification authority refused",
        (
            {
                "code": code,
                "path": "integrationCertification",
                "expected": expected,
                "observed": observed,
            },
        ),
    )


def _current(request: IntegrationCertificationRequest) -> LifecycleOperationRecord:
    owner = request.owner
    if owner is None:
        _refuse("integration-certification-owner-missing", "actual running operation owner", None)
    assert owner is not None
    current = owner.store.read()
    prior = owner.record
    if current is None or (
        current.operationKind != "integrate"
        or current.contractPath != request.contract.contract_path.as_posix()
        or (current.operationKey, current.generation, current.workerLease, current.workerPid)
        != (prior.operationKey, prior.generation, prior.workerLease, prior.workerPid)
        or current.status != "running"
        or current.cancelRequested
        or current.integrationAuthority != prior.integrationAuthority
    ):
        _refuse(
            "integration-certification-owner-not-current",
            prior.operationKey,
            None if current is None else current.operationKey,
        )
    assert current is not None
    authority = current.integrationAuthority
    contract = request.contract
    repository = repository_identity(contract.code_repo_path)
    checkout_repository = repository_identity(request.target.code_worktree)
    expected = {
        "repository": repository.as_posix() if repository is not None else None,
        "commit": contract.code_commit,
        "tree": require_git(request.target.code_worktree, ["rev-parse", "HEAD^{tree}"]),
    }
    observed = {
        "repository": authority.codeRepository if authority is not None else None,
        "commit": authority.codeCandidateCommit if authority is not None else None,
        "tree": require_git(request.target.code_worktree, ["write-tree"]),
    }
    if (
        repository is None
        or checkout_repository != repository
        or expected != observed
        or require_git(request.target.code_worktree, ["rev-parse", "HEAD"]) != contract.code_commit
    ):
        _refuse("integration-certification-candidate-mismatch", expected, observed)

    return current


def authorize_integration_start(request: IntegrationCertificationRequest) -> None:
    """Recheck the live journal owner after sandbox and retained-evidence preparation."""
    verified = _current(request)
    assert request.owner is not None
    observe_certification_publication(request.owner.store, verified)


def _identity(
    request: IntegrationCertificationRequest, record: LifecycleOperationRecord
) -> dict[str, object]:
    authority = record.integrationAuthority
    if authority is None:
        _refuse("integration-authority-missing", "exact accepted integration authority", None)
    return {
        "operationKey": record.operationKey,
        "generation": record.generation,
        "integrationAuthorityDigest": content_digest(authority),
        "profileReference": (
            request.target.profile_reference.as_posix()
            if request.target.profile_reference is not None
            else ""
        ),
        "diffBase": request.contract.code_base_commit,
        "mode": request.plan.mode,
        "memoryCapBytes": request.plan.memory_cap_bytes,
        "completionFingerprint": (
            request.attestation["completionFingerprint"]
            if request.attestation is not None
            else None
        ),
    }


def prepare_integration_certification(
    request: IntegrationCertificationRequest,
) -> LoadedIntegrationCertification:
    """Publish/read back and select once, before the first executor invocation."""
    record = _current(request)
    if record.integrationCertification is None:
        prepared = prepare_certification_records(
            CertificationRunTarget(
                request.target.repository_id,
                request.target.code_worktree,
                request.target.profile_reference,
                request.target.worktree_group,
            ),
            mode=request.plan.mode,
            diff_base=request.contract.code_base_commit,
            candidate_tree=require_git(request.target.code_worktree, ["rev-parse", "HEAD^{tree}"]),
        )
        state = IntegrationCertificationSelection.model_validate(
            {
                **_identity(request, record),
                "frozenRun": prepared.frozen_reference,
            }
        )
        record = _select(request, record, state)
    return _load(request, record)


def load_integration_certification(
    request: IntegrationCertificationRequest,
) -> LoadedIntegrationCertification:
    return _load(request, _current(request))


def protected_integration_generations(request: IntegrationCertificationRequest) -> frozenset[str]:
    loaded = load_integration_certification(request)
    # _load verifies the original historical references and their complete publication bytes.
    history = (
        json.loads(item.publication.canonicalBytes)["generation"]
        for item in loaded.state.terminalHistory
    )
    return frozenset((*history, *(item.publication.generation for item in loaded.terminals)))


def _load(
    request: IntegrationCertificationRequest,
    record: LifecycleOperationRecord,
) -> LoadedIntegrationCertification:
    state = record.integrationCertification
    if state is None:
        _refuse("integration-certification-selection-missing", "explicit selected run", None)
    assert state is not None
    identity = _identity(request, record)
    if any(getattr(state, key) != value for key, value in identity.items()):
        _refuse(
            "integration-certification-selection-mismatch", identity, state.model_dump(mode="json")
        )
    store = certificate_store(request.contract.worktree_group)
    run = store.load_reference(state.frozenRun)
    if not isinstance(run, FrozenCertificationRun) or (
        run.repositoryPlan.candidateIdentity.value
        != require_git(request.target.code_worktree, ["rev-parse", "HEAD^{tree}"])
        or run.repositoryPlan.mode != "full"
        or run.repositoryProfile.profile.repositoryId != request.contract.repo_name
    ):
        _refuse(
            "integration-certification-run-mismatch",
            record.candidateTree,
            state.frozenRun.semanticDigest,
        )
    assert isinstance(run, FrozenCertificationRun)
    prepared = prepared_from_frozen_run(request.contract.worktree_group, run)
    reports = request.contract.worktree_group / "reports"
    terminals = tuple(load_selected_terminal(reports, store, run, item) for item in state.terminals)
    for terminal in terminals:
        attestation = (
            None
            if terminal.publication.attestation is None
            else dict(terminal.publication.attestation)
        )
        if attestation != request.attestation:
            _refuse(
                "integration-certification-publication-mismatch", request.attestation, attestation
            )
    for original in state.terminalHistory:
        _require_interrupted(request, load_selected_terminal(reports, store, run, original))
    certificates = tuple(item.certificate for item in terminals if item.certificate is not None)
    validate_certificate_chain(run.admission, certificates)
    changes = (
        (
            CertificateInputChange(
                changeClass="unchanged-interruption",
                reason="Exact integration owner, frozen admission and original terminal prefix reread.",
            ),
        )
        if terminals
        else ()
    )
    return LoadedIntegrationCertification(
        record,
        state,
        prepared,
        terminals,
        changes,
        plan_certificate_reuse(run.admission, certificates, changes),
    )


def _select(
    request: IntegrationCertificationRequest,
    observed: LifecycleOperationRecord,
    selected: IntegrationCertificationSelection,
) -> LifecycleOperationRecord:
    _current(request)
    proposed = observed.model_copy(update={"integrationCertification": selected})
    _load(request, proposed)
    assert request.owner is not None
    fresh = observe_certification_publication(request.owner.store, observed)
    proposed = fresh.model_copy(update={"integrationCertification": selected})
    current, won = request.owner.store.update_if_current(fresh, lambda _: proposed)
    if not won or current.integrationCertification != selected:
        _refuse(
            "integration-certification-cas-lost", observed.recordRevision, current.recordRevision
        )
    return current


def _require_interrupted(
    request: IntegrationCertificationRequest,
    terminal: RecordedGateTerminal,
) -> None:
    publication = terminal.publication
    path = published_report_path_from_manifest(
        request.contract.worktree_group / "reports",
        publication,
        publication.result_decoder.artifactPath,
    )
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        _refuse("integration-interruption-catalog-invalid", "original decoder object", None)
    assert isinstance(payload, dict)
    gate = next(
        (item for item in catalog_gates(payload) if item["gate"] == terminal.result.gate), None
    )
    if (
        terminal.result.disposition != "green"
        or gate is None
        or gate.get("disposition") != "interrupted"
    ):
        _refuse("integration-terminal-not-interrupted", "explicit original interrupted gate", gate)


def require_resumable_integration(selected: LoadedIntegrationCertification) -> None:
    if any(item.result.disposition == "red" for item in selected.terminals):
        _refuse(
            "integration-unchanged-red-refused",
            "corrected candidate and successor operation",
            "original red catalog",
        )


def select_integration_terminals(
    request: IntegrationCertificationRequest,
    recorded: RecordedCertificationGeneration,
) -> None:
    loaded = load_integration_certification(request)
    terminals = tuple(terminal_selection(item) for item in recorded.terminals)
    history = loaded.state.terminalHistory
    before = loaded.state.terminals
    if before and terminals[: len(before)] != before and before[-1].certificate is None:
        _require_interrupted(request, loaded.terminals[-1])
        history = (*history, before[-1])
    state = loaded.state.model_copy(update={"terminals": terminals, "terminalHistory": history})
    _select(request, loaded.record, state)


def select_completed_integration(
    request: IntegrationCertificationRequest,
    certificate: IntegrationQualityCertification,
) -> None:
    loaded = load_integration_certification(request)
    try:
        certificate = IntegrationQualityCertification.model_validate(
            certificate.model_dump(mode="json")
        )
    except ValueError as error:
        _refuse(
            "integration-completion-candidate-mismatch",
            "canonical complete quality certification",
            str(error),
        )
    if (
        certificate.frozenRun != loaded.state.frozenRun
        or certificate.terminals != loaded.state.terminals
    ):
        _refuse(
            "integration-completion-reference-mismatch",
            loaded.state.frozenRun.semanticDigest,
            certificate.frozenRun.semanticDigest,
        )
    try:
        validate_integration_completion_identity(
            loaded.state, certificate.completionFingerprint, certificate.attestation
        )
    except ValueError:
        _refuse(
            "integration-completion-candidate-mismatch",
            {
                "completionFingerprint": loaded.state.completionFingerprint,
                "diffBase": loaded.state.diffBase,
                "memoryCapBytes": loaded.state.memoryCapBytes,
            },
            {
                "completionFingerprint": certificate.completionFingerprint,
                "diffBase": certificate.attestation["diffBase"],
                "memoryCapBytes": certificate.attestation["memoryCapBytes"],
            },
        )
    if (
        certificate.codeCommit != request.contract.code_commit
        or certificate.candidateTree != loaded.prepared.candidateTree
        or certificate.attestation != request.attestation
    ):
        _refuse(
            "integration-completion-candidate-mismatch",
            {
                "codeCommit": request.contract.code_commit,
                "candidateTree": loaded.prepared.candidateTree,
                "attestation": request.attestation,
            },
            {
                "codeCommit": certificate.codeCommit,
                "candidateTree": certificate.candidateTree,
                "attestation": certificate.attestation,
            },
        )
    original = loaded.record.qualityCertification
    if original is not None and original != certificate:
        _refuse(
            "integration-completion-already-selected",
            original.resultSha256,
            certificate.resultSha256,
        )
    assert request.owner is not None
    fresh = observe_certification_publication(request.owner.store, loaded.record)
    proposed = fresh.model_copy(update={"qualityCertification": certificate})
    current, won = request.owner.store.update_if_current(fresh, lambda _: proposed)
    if not won or current.qualityCertification != certificate:
        _refuse(
            "integration-completion-cas-lost", loaded.record.recordRevision, current.recordRevision
        )
