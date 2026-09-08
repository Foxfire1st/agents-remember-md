"""Produce and select real Gate-5 evidence for a proved private code output."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from agents_remember.certification.certificate_authority import compile_gate_certificate
from agents_remember.certification.certificate_models import (
    GateCertificateIssuanceContext,
    GateFiveSemanticInputs,
)
from agents_remember.certification.models import (
    GateResultAdmission,
    GateResultManifest,
    RailEvidenceReference,
    RailTerminalObservation,
)
from agents_remember.certification.repository_profiles.authority import (
    MAX_PROFILE_BYTES,
    AdmittedRepositoryProfile,
    resolve_repository_profile_path,
)
from agents_remember.certification.repository_profiles.execution import (
    AdmittedRepositoryProfileExecution,
    admit_repository_profile_execution,
)
from agents_remember.certification.results import build_rail_result, compile_gate_result_manifest
from agents_remember.errors import FinalCertificationError
from agents_remember.kernel.authority import require_repo
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.kernel.route_index import build_route_indexes
from agents_remember.kernel.route_index_census import route_index_source_snapshot
from agents_remember.memory_quality.check import (
    AVAILABLE_CHECKS,
    DriftCheckContext,
    run_memory_quality_check,
)
from agents_remember.memory_quality.final_certification.certify import (
    FinalCertificationEvidence,
    certify_final_full_memory_coherence,
)
from agents_remember.memory_quality.final_certification.models import FinalCertificationResult
from agents_remember.memory_quality.incremental_scope.affected_execution import (
    AffectedClosureExecution,
    RangeResolutionAffectedExecutor,
    RangeResolutionExecutionContext,
    execute_affected_closure,
)
from agents_remember.memory_quality.incremental_scope.affected_planning import (
    AffectedClosureAdmission,
    compile_affected_closure_plan,
)
from agents_remember.memory_quality.incremental_scope.candidate import (
    observe_contract_task,
    observe_git_tree_delta,
)
from agents_remember.memory_quality.incremental_scope.compiler import compile_scope_manifest
from agents_remember.memory_quality.incremental_scope.models import (
    ScopeCandidateIdentity,
    TaskObservationPair,
)
from agents_remember.memory_quality.incremental_scope.owners import (
    ContractDependencyAuthority,
    DependencyOwnerContext,
)
from agents_remember.memory_quality.integrity.check_missing_onboarding import (
    missing_onboarding_for_source,
)
from agents_remember.memory_quality.style.citations.resolution import Trees
from agents_remember.memory_quality.style.citations.source_index import open_repository_index
from agents_remember.models.lifecycles.operation import CloseoutOperationInput
from agents_remember.models.task_document import CanonicalTaskObservation
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
    current_certification_handoff,
)
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.integration.closeout.certification.selection import (
    select_recorded_terminals,
)
from agents_remember.worktrees.integration.closeout.curator_coherence import (
    require_current_curator_coherence,
)
from agents_remember.worktrees.integration.closeout.preparation.memory_execution import (
    observe_prepared_memory_candidate,
)
from agents_remember.worktrees.integration.closeout.preparation.memory_port import (
    PreparedMemoryCertificationRequest,
    PreparedMemoryCertificationResult,
)
from agents_remember.worktrees.modules.context import contract_context
from agents_remember.worktrees.modules.quality.certification_evidence import verify_result_evidence
from agents_remember.worktrees.modules.quality.certification_records import certificate_store
from agents_remember.worktrees.modules.quality.certification_terminal import (
    RecordedCertificationGeneration,
    RecordedGateTerminal,
)
from agents_remember.worktrees.modules.quality.clean_executor import (
    ReportBindings,
    _publish_reports,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifest,
    parse_published_quality_manifest,
)
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)


def _current(request: PreparedMemoryCertificationRequest) -> CloseoutCertificationHandoff:
    handoff = request.handoff
    current = current_certification_handoff(handoff.contract, handoff.record, handoff.store)
    actual = observe_prepared_memory_candidate(current, request.candidate.codeView)
    if actual != request.candidate:
        refuse("prepared-memory-candidate-moved", request.candidate, actual)
    if current.selected.run.repositoryPlan.candidateIdentity.value != actual.codeView.codeTree:
        refuse("prepared-memory-prefix-mismatch", actual.codeView.codeTree, current.selected.run)
    return current


@dataclass(frozen=True)
class _PreparedScopeAuthority:
    request: PreparedMemoryCertificationRequest

    def observe(self) -> ScopeCandidateIdentity:
        current = _current(self.request)
        pair = self.request.candidate.codeView.logicalPair
        # Use the original selected admission's actual task-source bytes. The door's
        # pre-curation memory tree is not the realized memory candidate after curation.
        snapshots = tuple(
            item
            for item in current.selected.authorities.inputSnapshots
            if item.owner == "task-document-source"
        )
        if len(snapshots) != 1:
            refuse("prepared-memory-task-baseline-missing", "one selected task source", snapshots)
        baseline = CanonicalTaskObservation.model_validate_json(snapshots[0].canonicalBytes)
        task = observe_contract_task(current.contract)
        if (
            baseline.taskDocumentRef != task.taskDocumentRef
            or baseline.semanticTopologyDigest != task.semanticTopologyDigest
            or baseline.taskIntent != task.taskIntent
        ):
            refuse("prepared-memory-task-moved", baseline, task)
        return ScopeCandidateIdentity(
            pairIdentity=pair,
            code=observe_git_tree_delta(
                Path(pair.codeRoot),
                namespace="code",
                root=pair.codeRoot,
                base_ref=current.contract.code_base_commit,
                candidate_tree=self.request.candidate.codeView.codeTree,
            ),
            memory=observe_git_tree_delta(
                Path(pair.memoryRoot),
                namespace="memory",
                root=pair.memoryRoot,
                base_ref=current.contract.memory_base_commit,
                candidate_tree=self.request.candidate.memoryTree,
            ),
            task=TaskObservationPair(base=baseline, candidate=task),
        )


def _run(request: PreparedMemoryCertificationRequest) -> FinalCertificationResult:
    current = _current(request)
    coherence = require_current_curator_coherence(current.contract)
    pair = request.candidate.codeView.logicalPair
    physical_code = Path(request.candidate.codeView.physicalCodeRoot)
    memory, onboarding = Path(pair.memoryRoot), Path(pair.onboardingRoot)
    context = replace(
        contract_context(current.contract),
        code_repository_root=physical_code,
        memory_root=memory,
        onboarding_root=onboarding,
    )
    prefix = tuple(
        item.certificate for item in current.selected.terminals[:4] if item.certificate is not None
    )
    if len(prefix) != 4:
        refuse("prepared-memory-prefix-incomplete", "four selected original certificates", prefix)
    authority = _PreparedScopeAuthority(request)
    candidate = authority.observe()
    # R06/R07 retain canonical logical roots and require every indexed code byte to
    # match the exact Git tree. HEAD-based final checks use only the proved view.
    logical_code = Path(pair.codeRoot)
    with open_repository_index(
        Trees(logical_code, memory, candidate_tree=candidate.code.candidateTree),
        verify_integrity=True,
    ) as index:
        scope = compile_scope_manifest(
            authority,
            ContractDependencyAuthority(
                DependencyOwnerContext(
                    logical_code,
                    memory,
                    onboarding,
                    current.contract.repo_name,
                    context.storage,
                    index,
                )
            ),
            checkers=AVAILABLE_CHECKS,
        )
        plan = compile_affected_closure_plan(
            AffectedClosureAdmission(authority, current.selected.run.admission, prefix),
            scope,
        )
        affected = execute_affected_closure(
            AffectedClosureExecution(
                authority,
                RangeResolutionAffectedExecutor(
                    RangeResolutionExecutionContext(logical_code, memory, onboarding, index),
                ),
            ),
            plan,
            prior_results=(),
        )
    _current(request)
    quality = run_memory_quality_check(
        onboarding,
        checks=AVAILABLE_CHECKS,
        drift_context=DriftCheckContext(
            physical_code,
            context,
            detail_limit=1000,
            include_rows=True,
            write_report=False,
        ),
        include_report_only_findings=True,
    )
    census = route_index_source_snapshot(
        code_root=physical_code,
        storage=context.storage,
        scoped_repo_path=current.contract.repo_name,
    )
    missing = tuple(
        row.to_dict()
        for source in census.eligible_paths
        if (
            row := missing_onboarding_for_source(
                physical_code,
                onboarding,
                context.storage,
                current.contract.repo_name,
                source,
            )
        )
        is not None
    )
    indexes = build_route_indexes(
        code_root=physical_code,
        onboarding_root=onboarding,
        repository=current.contract.repo_name,
        storage=context.storage,
        dry_run=True,
    )
    actual_indexes = {
        path.relative_to(onboarding).as_posix() for path in onboarding.rglob("overview.index.json")
    }
    stale = set(indexes.stale_indexes) | (actual_indexes - set(indexes.indexes))
    after = _current(request)
    if require_current_curator_coherence(after.contract) != coherence:
        refuse("prepared-memory-coherence-moved", coherence.record_digest, "changed")
    return certify_final_full_memory_coherence(
        FinalCertificationEvidence(
            current.selected.run.admission,
            prefix,
            (),
            candidate.code.candidateTree,
            candidate.memory.candidateTree,
            candidate.pairIdentity,
            plan,
            coherence,
            quality["checks"],
            len(missing),
            len(stale),
            affected.terminalStatus == "pass" and affected.incrementalMemoryReady,
        )
    )


def _profile(request: PreparedMemoryCertificationRequest) -> AdmittedRepositoryProfileExecution:
    current = _current(request)
    operation_input = current.record.input
    if not isinstance(operation_input, CloseoutOperationInput):
        refuse("prepared-memory-operation-kind", "closeout", current.record.operationKind)
    reference = require_repo(
        load_config(operation_input.configPath),
        current.contract.repo_name,
    ).certification_profile
    root = Path(request.candidate.codeView.physicalCodeRoot)
    path = resolve_repository_profile_path(root, reference)
    with path.open("rb") as stream:
        raw = stream.read(MAX_PROFILE_BYTES + 1)
    if len(raw) > MAX_PROFILE_BYTES:
        refuse("prepared-memory-profile-capacity", MAX_PROFILE_BYTES, len(raw))
    frozen = current.selected.run
    admitted = AdmittedRepositoryProfile(
        current.contract.repo_name,
        root,
        path,
        hashlib.sha256(raw).hexdigest(),
        frozen.repositoryProfile,
    )
    execution = admit_repository_profile_execution(
        admitted,
        purpose=frozen.repositoryPlan.purpose,
        mode=frozen.repositoryPlan.mode,
        candidate_identity=frozen.repositoryPlan.candidateIdentity,
        source_selection=frozen.repositoryPlan.sourceSelection,
    )
    if execution.plan != frozen.repositoryPlan:
        refuse("prepared-memory-profile-plan-moved", frozen.repositoryPlan, execution.plan)
    return execution


def _export(
    request: PreparedMemoryCertificationRequest,
    result: FinalCertificationResult,
    execution: AdmittedRepositoryProfileExecution,
    export: Path,
) -> bytes:
    current = _current(request)
    reports = current.contract.worktree_group / "reports"
    for definition in execution.published_artifacts:
        if definition.path == execution.decoder.artifactPath:
            continue
        publications = tuple(
            item.publication
            for item in current.selected.terminals[:4]
            if definition.path in item.publication.files
        )
        if not publications:
            if definition.required:
                refuse("prepared-memory-original-artifact-missing", definition.path, None)
            continue
        # Earlier gate outputs may have distinct generations. Select their original
        # supplying terminal, never the mutable newest-report pointer.
        publication = publications[-1]
        source = published_report_path_from_manifest(reports, publication, definition.path)
        with source.open("rb") as stream:
            raw = stream.read(definition.maxBytes + 1)
        record = publication.require_file(definition.path)
        if (
            len(raw) > definition.maxBytes
            or len(raw) != record.size
            or hashlib.sha256(raw).hexdigest() != record.sha256
        ):
            refuse("prepared-memory-original-artifact-moved", record, definition.path)
        destination = export / definition.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    payload = {
        execution.decoder.statusField: execution.decoder.passedValue
        if result.state == "green"
        else execution.decoder.failedValue,
        execution.decoder.exitCodeField: 0 if result.state == "green" else 1,
        "producer": "prepared-memory-certification",
        "preparedCandidate": request.candidate.model_dump(mode="json"),
        "certification": result.model_dump(mode="json"),
    }
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    destination = export / execution.decoder.artifactPath
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    return raw


def _manifest(
    request: PreparedMemoryCertificationRequest,
    result: FinalCertificationResult,
    artifact: str,
    raw: bytes,
) -> GateResultManifest:
    frozen = request.handoff.selected.run
    gate = frozen.certificationPlan.gates[4]
    items = {item.item.key: item for item in result.attestation.catalog}
    if set(items) != {(rail.identity.railId, rail.identity.version) for rail in gate.rails}:
        refuse("prepared-memory-catalog-mismatch", gate, result.attestation.catalog)
    rows = []
    for rail in gate.rails:
        item = items[(rail.identity.railId, rail.identity.version)]
        if (
            len(rail.evidenceContract) != 1
            or rail.evidenceContract[0].evidenceId != item.item.itemId
            or rail.requiredArtifacts
            or rail.outputArtifacts
        ):
            refuse("prepared-memory-rail-contract-mismatch", item.item, rail)
        rows.append(
            build_rail_result(
                gate,
                RailTerminalObservation(
                    rail=rail.identity,
                    status=item.status,
                    code="actual-r08-" + item.status,
                    evidence=(
                        RailEvidenceReference(
                            evidenceId=item.item.itemId,
                            reference=artifact,
                            sha256=hashlib.sha256(raw).hexdigest(),
                            size=len(raw),
                        ),
                    ),
                ),
            )
        )
    return compile_gate_result_manifest(
        frozen.registry,
        frozen.certificationPlan,
        gate,
        rows,
        GateResultAdmission(
            candidateIdentity=frozen.repositoryPlan.candidateIdentity,
            profileId=frozen.repositoryPlan.selectionId,
            altitude=frozen.certificationPlan.profileKind,
        ),
    )


def _select(
    request: PreparedMemoryCertificationRequest,
    result: FinalCertificationResult,
    manifest: GateResultManifest,
    publication: PublishedQualityManifest,
) -> RecordedGateTerminal:
    current = _current(request)
    frozen = current.selected.run
    prefix = tuple(
        item.certificate for item in current.selected.terminals[:4] if item.certificate is not None
    )
    certificate = None
    if result.state == "green":
        certificate = compile_gate_certificate(
            frozen.admission,
            frozen.certificationPlan.gates[4],
            manifest,
            prefix,
            GateCertificateIssuanceContext(
                provenance=frozen.provenance,
                gateFiveInputs=result.gateFiveInputs,
            ),
        )
    objects = certificate_store(current.contract.worktree_group)
    objects.publish(manifest)
    if certificate is not None:
        objects.publish(certificate)
    terminal = RecordedGateTerminal(
        manifest,
        objects.reference("result-manifest", manifest.manifestDigest),
        publication,
        certificate,
        None
        if certificate is None
        else objects.reference(
            "certificate",
            certificate.certificateDigest,
        ),
    )
    verify_result_evidence(
        current.contract.worktree_group / "reports", publication, manifest.railResults
    )
    current = _current(request)
    select_recorded_terminals(
        current.contract,
        current.store,
        current.record,
        RecordedCertificationGeneration((terminal,), ()),
    )
    return terminal


class PreparedMemoryCertificationAdapter:
    """Existing real checks/coherence producers; no automatic curator judgments or repairs."""

    def observe(self, request: PreparedMemoryCertificationRequest) -> GateFiveSemanticInputs | None:
        """Reobserve current inputs without publishing or selecting a replacement terminal."""
        return _run(request).gateFiveInputs

    def certify(
        self,
        request: PreparedMemoryCertificationRequest,
    ) -> PreparedMemoryCertificationResult:
        current = _current(request)
        if len(current.selected.terminals) != 4:
            refuse(
                "prepared-memory-terminal-already-selected",
                "exact code prefix only",
                current.selected.state,
            )
        result = _run(request)
        execution = _profile(request)
        with TemporaryDirectory(
            prefix="prepared-memory-export-", dir=current.contract.worktree_group
        ) as directory:
            export = Path(directory)
            raw = _export(request, result, execution, export)
            manifest = _manifest(request, result, execution.decoder.artifactPath, raw)
            publication = parse_published_quality_manifest(
                _publish_reports(
                    export,
                    current.contract.worktree_group / "reports",
                    candidate_tree=request.candidate.codeView.codeTree,
                    profile_execution=execution,
                    bindings=ReportBindings(
                        attestation=None,
                        runtime_authority_digest=None,
                        protected_generations=lambda: (
                            _current(request).selected.protected_generations
                        ),
                    ),
                )
            )
            published = published_report_path_from_manifest(
                current.contract.worktree_group / "reports",
                publication,
                execution.decoder.artifactPath,
            ).read_bytes()
            if published != raw:
                refuse(
                    "prepared-memory-publication-moved", hashlib.sha256(raw).hexdigest(), "changed"
                )
        terminal = _select(request, result, manifest, publication)
        if result.gateFiveInputs is None or terminal.certificateReference is None:
            raise FinalCertificationError(
                "prepared-memory-certification-red",
                result.reason or result.state,
                next_action="memory_quality_check",
            )
        return PreparedMemoryCertificationResult(
            _current(request),
            request.candidate,
            result.gateFiveInputs,
            terminal.resultReference,
            terminal.certificateReference,
        )
