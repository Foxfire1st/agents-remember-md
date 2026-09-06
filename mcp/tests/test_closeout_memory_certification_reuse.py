"""Gate-5 reuse through actual memory producers, immutable evidence, and journal CAS.

The code executor remains the existing explicit Node test adapter. Memory checks,
scope planning, coherence publication, R08/R21 compilers, and selected-object readers
are real. The small continuation below is test wiring, not the production L35/L36
adapter: its finalization boundary records a pending handoff and never commits refs.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest import mock
from uuid import uuid4

import pytest
from agents_remember.application import worktree_tools
from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.certification.certificate_authority import (
    compile_gate_certificate,
    validate_certificate_chain,
)
from agents_remember.certification.certificate_models import (
    GateCertificateIssuanceContext,
    GateFiveSemanticInputs,
)
from agents_remember.certification.models import (
    GateResultAdmission,
    RailEvidenceReference,
    RailTerminalObservation,
)
from agents_remember.certification.repository_profiles.authority import load_repository_profile
from agents_remember.certification.repository_profiles.canonical import repository_profile_digest
from agents_remember.certification.repository_profiles.execution import (
    admit_repository_profile_execution,
)
from agents_remember.certification.repository_profiles.models import RepositoryCertificationProfile
from agents_remember.certification.results import build_rail_result, compile_gate_result_manifest
from agents_remember.errors import CertificationContractError, CuratorCoherenceError
from agents_remember.kernel.coordination_context.models import CoordinationContext
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.kernel.route_index import build_route_indexes
from agents_remember.kernel.route_index_census import route_index_source_snapshot
from agents_remember.memory_quality.check import (
    AVAILABLE_CHECKS,
    DRIFT_CHECK_NAME,
    DriftCheckContext,
    run_memory_quality_check,
)
from agents_remember.memory_quality.curator_checklist import (
    CuratorChecklist,
    report_path_for,
    write_curator_checklist,
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
from agents_remember.memory_quality.incremental_scope.candidate import ContractScopeAuthority
from agents_remember.memory_quality.incremental_scope.compiler import compile_scope_manifest
from agents_remember.memory_quality.incremental_scope.errors import ScopeUnprovenError
from agents_remember.memory_quality.incremental_scope.owners import (
    ContractDependencyAuthority,
    DependencyOwnerContext,
)
from agents_remember.memory_quality.integrity.check_missing_onboarding import (
    missing_onboarding_for_source,
)
from agents_remember.memory_quality.style.citations.resolution import Trees
from agents_remember.memory_quality.style.citations.source_index import open_repository_index
from agents_remember.models.certification.references import (
    CertificateObjectKind,
    CertificateObjectReference,
)
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.curator_coherence import CuratorCoherenceRequest
from agents_remember.models.lifecycles.door import CloseoutDoorRequest
from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.operation import CloseoutOperationInput
from agents_remember.models.lifecycles.preparation import CloseoutPreparationIntent
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks import write_task_doc
from agents_remember.worktrees.integration.closeout.certification.admission import (
    initial_certification_state,
    prepare_closeout_certification,
)
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
    current_certification_handoff,
    execute_selected_closeout,
)
from agents_remember.worktrees.integration.closeout.certification.selection import (
    recovery_memory_inputs,
    select_recorded_terminals,
)
from agents_remember.worktrees.integration.closeout.curator_coherence import (
    require_current_curator_coherence,
)
from agents_remember.worktrees.integration.closeout.curator_coherence_publication import (
    curator_coherence_action,
)
from agents_remember.worktrees.integration.closeout.door_control import (
    DoorActor,
    closeout_door_tool,
)
from agents_remember.worktrees.integration.closeout.future_code_candidate import (
    capture_future_code_candidate,
)
from agents_remember.worktrees.integration.closeout.memory_candidate_pair import (
    resolve_memory_candidate_pair,
)
from agents_remember.worktrees.integration.closeout.preparation.policy import (
    PreparationPolicyError,
    observe_git_preparation_policy,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.modules.context import contract_context
from agents_remember.worktrees.modules.git import worktree_candidate_tree
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.modules.quality import clean_executor, gate
from agents_remember.worktrees.modules.quality.certification_evidence import verify_result_evidence
from agents_remember.worktrees.modules.quality.certification_records import certificate_store
from agents_remember.worktrees.modules.quality.certification_terminal import (
    RecordedCertificationGeneration,
    RecordedGateTerminal,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
)
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)
from agents_remember.worktrees.services import bind_worktree_services, worktree_services
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    install_fixture_profile,
)
from test_closeout_certification_entrypoint import _executor, _store
from test_closeout_queue import MASTER_A, SPRINT, QueueFixture, _grade, _leaf
from test_operation_certification_selection import _queued
from test_worktree_support import git


@dataclass(frozen=True)
class _FullMemoryScan:
    context: CoordinationContext
    quality: dict[str, Any]
    missing: tuple[dict[str, str], ...]
    stale_indexes: tuple[str, ...]
    eligible: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.quality["ok"] and not self.missing and not self.stale_indexes


def _context(contract: WorktreeContract) -> CoordinationContext:
    assert contract.memory_worktree is not None
    return replace(
        contract_context(contract),
        code_repository_root=contract.code_worktree,
        memory_root=contract.memory_worktree,
        onboarding_root=contract.memory_worktree / "onboarding",
    )


def _scan(contract: WorktreeContract) -> _FullMemoryScan:
    context = _context(contract)
    quality = run_memory_quality_check(
        context.onboarding_root,
        checks=AVAILABLE_CHECKS,
        drift_context=DriftCheckContext(
            contract.code_worktree,
            context,
            detail_limit=1000,
            include_rows=True,
            write_report=False,
        ),
        include_report_only_findings=True,
    )
    census = route_index_source_snapshot(
        code_root=contract.code_worktree,
        storage=context.storage,
        scoped_repo_path=contract.repo_name,
    )
    missing = tuple(
        row.to_dict()
        for source in census.eligible_paths
        if (
            row := missing_onboarding_for_source(
                contract.code_worktree,
                context.onboarding_root,
                context.storage,
                contract.repo_name,
                source,
            )
        )
        is not None
    )
    indexes = build_route_indexes(
        code_root=contract.code_worktree,
        onboarding_root=context.onboarding_root,
        repository=contract.repo_name,
        storage=context.storage,
        dry_run=True,
    )
    actual = {
        path.relative_to(context.onboarding_root).as_posix()
        for path in context.onboarding_root.rglob("overview.index.json")
    }
    assert set(quality["checks"]) == set(AVAILABLE_CHECKS)
    assert len(quality["checks"][DRIFT_CHECK_NAME]["rows"]) > 0
    return _FullMemoryScan(
        context,
        quality,
        missing,
        tuple(sorted(set(indexes.stale_indexes) | (actual - set(indexes.indexes)))),
        census.eligible_paths,
    )


def _write_source_cards(contract: WorktreeContract) -> None:
    context = _context(contract)
    commit = git(contract.code_worktree, "rev-parse", "HEAD")
    date = git(contract.code_worktree, "show", "-s", "--format=%cI", "HEAD")
    sources = route_index_source_snapshot(
        code_root=contract.code_worktree,
        storage=context.storage,
        scoped_repo_path=contract.repo_name,
    ).eligible_paths
    assert {"README.md", "feature.txt"}.issubset(sources)
    for source in (*sources, None):
        path = context.onboarding_root / (f"{source}.md" if source else "overview.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        identity = f"| path | `{source}` |" if source else "| sourceRoute | `.` |"
        doc_type = "file-level-onboarding" if source else "repo-overview"
        path.write_text(
            f"# {source or 'Fixture source overview'}\n\n"
            "| Field | Value |\n| --- | --- |\n"
            f"| repository | {contract.repo_name} |\n{identity}\n"
            f"| doc_type | {doc_type} |\n"
            f"| lastVerifiedCommitHash | `{commit}` |\n"
            f"| lastVerifiedCommitDate | {date} |\n\n"
            "## Purpose\n\n"
            "This temporary source belongs to the isolated memory certification fixture.\n\n"
            "## Update History\n\n"
            f"- {date}: Describe the actual committed fixture source.\n",
            encoding="utf-8",
        )
    build_route_indexes(
        code_root=contract.code_worktree,
        onboarding_root=context.onboarding_root,
        repository=contract.repo_name,
        storage=context.storage,
    )
    git(context.memory_root, "add", "-A")


def _publish_actual_coherence(contract: WorktreeContract, scan: _FullMemoryScan) -> None:
    assert scan.ok, (scan.quality, scan.missing, scan.stale_indexes)
    pair = resolve_memory_candidate_pair(
        contract,
        requested_contract_path=contract.contract_path,
        requested_repo_id=contract.repo_name,
    )
    memory_tree = worktree_candidate_tree(
        scan.context.memory_root, contract.worktree_group / "memory.index"
    )
    result = write_curator_checklist(
        CuratorChecklist(
            report_path_for(contract.worktree_group),
            contract.repo_name,
            contract.code_worktree,
            scan.context.onboarding_root,
            pair,
            capture_future_code_candidate(contract).codeCandidateTree,
            memory_tree,
            scan.quality,
            list(scan.quality["findings"]),
            [],
            {"eligible": len(scan.eligible), "missing": list(scan.missing)},
            list(scan.stale_indexes),
            scan.quality["checks"][DRIFT_CHECK_NAME]["rows"],
            scan.quality["reportOnlyFindings"],
        )
    )
    assert result["checklistStatus"] == "ready-for-closeout"
    assert result["sourceChangeCandidateCount"] == 0
    prepared = curator_coherence_action(
        contract,
        CuratorCoherenceRequest(action="prepare", contract_path=contract.contract_path.as_posix()),
    )
    published = curator_coherence_action(
        contract,
        CuratorCoherenceRequest(
            action="publish",
            contract_path=contract.contract_path.as_posix(),
            semantic_requirement_revision="FIXTURE-R01@v1",
            delivery_attempt="A001",
            judgments=[],
            expected_predecessor_digest=str(prepared["predecessorAuthorityDigest"]),
            expected_code_candidate_tree=str(prepared["codeCandidateTree"]),
            expected_memory_candidate_tree=str(prepared["memoryCandidateTree"]),
            expected_task_topology_fingerprint=str(prepared["taskTopologyFingerprint"]),
            expected_task_intent=TaskIntentIdentity.model_validate(prepared["taskIntent"]),
            expected_attestation_sha256=str(prepared["attestationSha256"]),
            caller=DeclaredCaller(role="architect", task_document_ref=SPRINT),
        ),
    )
    assert published["state"] in {"published", "already-current"}
    assert require_current_curator_coherence(contract).record.sourceCandidates == []


def _certify_memory(handoff: CloseoutCertificationHandoff) -> FinalCertificationResult:
    contract, selected = handoff.contract, handoff.selected
    scan = _scan(contract)
    assert scan.ok, (scan.quality, scan.missing, scan.stale_indexes)
    coherence = require_current_curator_coherence(contract)
    authority = ContractScopeAuthority(contract)
    candidate = authority.observe()
    code, memory = Path(candidate.pairIdentity.codeRoot), Path(candidate.pairIdentity.memoryRoot)
    prefix = tuple(
        item.certificate for item in selected.terminals[:4] if item.certificate is not None
    )
    assert len(prefix) == 4
    with open_repository_index(
        Trees(code, memory, candidate_tree=candidate.code.candidateTree)
    ) as index:
        scope = compile_scope_manifest(
            authority,
            ContractDependencyAuthority(
                DependencyOwnerContext(
                    code,
                    memory,
                    memory / "onboarding",
                    contract.repo_name,
                    scan.context.storage,
                    index,
                )
            ),
            checkers=AVAILABLE_CHECKS,
        )
        plan = compile_affected_closure_plan(
            AffectedClosureAdmission(authority, selected.run.admission, prefix), scope
        )
        affected = execute_affected_closure(
            AffectedClosureExecution(
                authority,
                RangeResolutionAffectedExecutor(
                    RangeResolutionExecutionContext(code, memory, memory / "onboarding", index)
                ),
            ),
            plan,
            prior_results=(),
        )
    assert affected.terminalStatus == "pass" and affected.incrementalMemoryReady, affected
    result = certify_final_full_memory_coherence(
        FinalCertificationEvidence(
            selected.run.admission,
            prefix,
            (),
            candidate.code.candidateTree,
            candidate.memory.candidateTree,
            candidate.pairIdentity,
            plan,
            coherence,
            scan.quality["checks"],
            len(scan.missing),
            len(scan.stale_indexes),
            True,
        )
    )
    assert result.state == "green" and result.gateFiveInputs is not None, result
    assert result.attestation.ok and all(
        item.status == "pass" for item in result.attestation.catalog
    )
    return result


def _publish_fifth(
    handoff: CloseoutCertificationHandoff,
) -> tuple[CloseoutCertificationHandoff, FinalCertificationResult]:
    contract, selected = handoff.contract, handoff.selected
    result = _certify_memory(handoff)
    assert result.gateFiveInputs is not None
    profile = load_repository_profile(
        contract.repo_name, contract.code_worktree, AGENTS_REMEMBER_PROFILE_REFERENCE
    )
    admitted = admit_repository_profile_execution(
        profile,
        purpose="closeout",
        mode=selected.run.repositoryPlan.mode,
        candidate_identity=selected.run.repositoryPlan.candidateIdentity,
        source_selection=selected.run.repositoryPlan.sourceSelection,
    )
    assert admitted.plan == selected.run.repositoryPlan
    export = contract.worktree_group / "actual-memory-export"
    export.mkdir()
    # The publisher retains the profile's required earlier artifacts byte-for-byte;
    # those files are not emitted as fresh code rail results.
    for definition in admitted.published_artifacts:
        if definition.path == admitted.decoder.artifactPath:
            continue
        original = next(
            terminal.publication
            for terminal in selected.terminals
            if definition.path in terminal.publication.files
        )
        source = published_report_path_from_manifest(
            contract.worktree_group / "reports", original, definition.path
        )
        destination = export / definition.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    payload = {
        admitted.decoder.statusField: admitted.decoder.passedValue
        if result.state == "green"
        else admitted.decoder.failedValue,
        admitted.decoder.exitCodeField: 0 if result.state == "green" else 1,
        "producer": "isolated-test-memory-continuation",
        "certification": result.model_dump(mode="json"),
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (export / admitted.decoder.artifactPath).write_bytes(encoded)
    clean_executor._publish_reports(
        export,
        contract.worktree_group / "reports",
        candidate_tree=selected.run.repositoryPlan.candidateIdentity.value,
        profile_execution=admitted,
        bindings=clean_executor.ReportBindings(
            attestation=None,
            runtime_authority_digest=None,
            protected_generations=lambda: selected.protected_generations,
        ),
    )
    publication = load_published_quality_manifest(contract.worktree_group / "reports")
    raw = published_report_path_from_manifest(
        contract.worktree_group / "reports", publication, admitted.decoder.artifactPath
    ).read_bytes()
    assert raw == encoded
    gate_plan = selected.run.certificationPlan.gates[4]
    items = {item.item.key: item for item in result.attestation.catalog}
    assert set(items) == {(rail.identity.railId, rail.identity.version) for rail in gate_plan.rails}
    results = []
    for rail in gate_plan.rails:
        item = items[(rail.identity.railId, rail.identity.version)]
        assert len(rail.evidenceContract) == 1
        assert rail.evidenceContract[0].evidenceId == item.item.itemId
        assert not rail.requiredArtifacts and not rail.outputArtifacts
        results.append(
            build_rail_result(
                gate_plan,
                RailTerminalObservation(
                    rail=rail.identity,
                    status=item.status,
                    code="actual-r08-" + item.status,
                    evidence=(
                        RailEvidenceReference(
                            evidenceId=item.item.itemId,
                            reference=admitted.decoder.artifactPath,
                            sha256=hashlib.sha256(raw).hexdigest(),
                            size=len(raw),
                        ),
                    ),
                ),
            )
        )
    manifest = compile_gate_result_manifest(
        selected.run.registry,
        selected.run.certificationPlan,
        gate_plan,
        results,
        GateResultAdmission(
            candidateIdentity=selected.run.repositoryPlan.candidateIdentity,
            profileId=selected.run.repositoryPlan.selectionId,
            altitude=selected.run.certificationPlan.profileKind,
        ),
    )
    prefix = tuple(item.certificate for item in selected.terminals if item.certificate is not None)
    certificate = compile_gate_certificate(
        selected.run.admission,
        gate_plan,
        manifest,
        prefix,
        GateCertificateIssuanceContext(
            provenance=selected.run.provenance, gateFiveInputs=result.gateFiveInputs
        ),
    )
    objects = certificate_store(contract.worktree_group)
    objects.publish(manifest)
    objects.publish(certificate)
    terminal = RecordedGateTerminal(
        manifest,
        objects.reference("result-manifest", manifest.manifestDigest),
        publication,
        certificate,
        objects.reference("certificate", certificate.certificateDigest),
    )
    assert objects.load_reference(terminal.resultReference) == manifest
    assert terminal.certificateReference is not None
    assert objects.load_reference(terminal.certificateReference) == certificate
    verify_result_evidence(contract.worktree_group / "reports", publication, manifest.railResults)
    record = select_recorded_terminals(
        contract, handoff.store, handoff.record, RecordedCertificationGeneration((terminal,), ())
    )
    return current_certification_handoff(contract, record, handoff.store), result


@dataclass
class _Fixture:
    queue: QueueFixture
    handoff: CloseoutCertificationHandoff
    r08: FinalCertificationResult | None
    code_calls: list[clean_executor.CleanQualityRequest]


def _fixture(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    live_worker: bool = False,
    publish_memory: bool = True,
) -> _Fixture:
    queue = QueueFixture(root, memory_mode="external")
    # This fixture stops at the scheduler continuation boundary before publishing Gate 5.
    bind_worktree_services(replace(worktree_services(), certification_continuation=None))
    # C preparation is a separate tested boundary. These scheduler scenarios supply
    # its unchanged handoff and exercise real memory evidence and reuse decisions.
    monkeypatch.setattr(
        "agents_remember.worktrees.integration.closeout.preparation.code_view.prepare_code_view",
        lambda handoff: (handoff, None),
    )
    contract = queue.contracts[MASTER_A]
    path = install_fixture_profile(contract.code_worktree, contract.repo_name, NODE_FIXTURE)
    profile = RepositoryCertificationProfile.model_validate_json(path.read_bytes())
    # A bounded actual R08 catalog, its semantic inputs, and complete item population
    # share the declared result document. This fixture bound is frozen before admission.
    profile = profile.model_copy(
        update={
            "publishedArtifacts": tuple(
                item.model_copy(update={"maxBytes": 256 * 1024})
                if item.path == "result.json"
                else item
                for item in profile.publishedArtifacts
            )
        }
    )
    profile = profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})
    path.write_text(profile.model_dump_json(), encoding="utf-8")
    git(contract.code_worktree, "add", "-A")
    git(
        contract.code_worktree,
        "commit",
        "-m",
        "Prepare real source for isolated memory certification",
    )
    _write_source_cards(contract)
    slug = Path(queue.leaf_refs[MASTER_A].path).stem
    write_task_doc(contract.task_root, _leaf(contract, slug))
    queue.set_priority(queue.leaf_refs[MASTER_A], "normal")
    _publish_actual_coherence(contract, _scan(contract))
    # Use the actual door API directly: QueueFixture.declare intentionally writes
    # synthetic upstream curator evidence, which this producer fixture must not use.
    closeout_door_tool(
        load_config(queue.config_path),
        CloseoutDoorRequest.model_validate(
            {
                "action": "declare",
                "contract_path": contract.contract_path.as_posix(),
                "grade": _grade("normal", queue.leaf_refs[MASTER_A]),
                "admission": {},
            }
        ),
        actor=DoorActor(role="manager", task_document_ref=MASTER_A),
        admitted_contract=contract,
    )
    contract = load_contract(contract.contract_path)
    queue.contracts[MASTER_A] = contract
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        admitted = worktree_tools.worktree_closeout_apply_tool(
            load_config(queue.config_path),
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(
                code="Fixture code", memory="Fixture memory", ledger="Fixture ledger"
            ),
            worktree_tools.CloseoutApproval(intent_note="Exercise isolated memory certification"),
        )
    assert admitted["ok"] is True and admitted["state"] == "queued", admitted
    launch.assert_called_once()
    store = _store(contract)
    lease = None
    if live_worker:
        from agents_remember.worktrees.integration.lifecycle.worker.termination import (  # noqa: PLC0415
            worker_process_fingerprint,
        )

        fingerprint = worker_process_fingerprint(os.getpid())
        assert fingerprint is not None
        lease = uuid4().hex * 2
        store.update(
            lambda record: record.model_copy(
                update={
                    "workerPid": os.getpid(),
                    "workerLease": lease,
                    "workerProcessFingerprint": fingerprint,
                }
            )
        )
    owner = OperationRuntime(store, worker_lease=lease).start()
    calls: list[clean_executor.CleanQualityRequest] = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    with pytest.raises(CertificationContractError) as unbound:
        execute_selected_closeout(contract, owner, store)
    assert unbound.value.findings[0]["code"] == "certification-continuation-unbound"
    handoff = current_certification_handoff(contract, owner, store)
    assert tuple(item.result.gate for item in handoff.selected.terminals) == (1, 2, 3, 4)
    if not publish_memory:
        return _Fixture(queue, handoff, None, calls)
    handoff, r08 = _publish_fifth(handoff)
    return _Fixture(queue, handoff, r08, calls)


class _Continuation:
    def __init__(self, *, cut: Literal["none", "memory", "cancel"] = "none") -> None:
        self.observations = 0
        self.finalizations: list[CloseoutCertificationHandoff] = []
        self.cut = cut
        self.unavailable: list[str] = []

    def observe_memory(
        self, handoff: CloseoutCertificationHandoff
    ) -> GateFiveSemanticInputs | None:
        self.observations += 1
        if self.observations == 2 and self.cut == "memory":
            assert handoff.contract.memory_worktree is not None
            path = handoff.contract.memory_worktree / "onboarding/README.md.md"
            path.write_text(path.read_text() + "\nThe actual memory candidate changed.\n")
        if self.observations == 2 and self.cut == "cancel":
            handoff.store.update(lambda record: record.model_copy(update={"cancelRequested": True}))
        try:
            return _certify_memory(handoff).gateFiveInputs
        except (CuratorCoherenceError, ScopeUnprovenError) as error:
            self.unavailable.append(type(error).__name__)
            return None

    def run_memory(self, _handoff: CloseoutCertificationHandoff) -> WorktreeCommandResult:
        raise AssertionError("The original current Gate 5 must not execute again")

    def finalize(self, handoff: CloseoutCertificationHandoff) -> WorktreeCommandResult:
        self.finalizations.append(handoff)
        return WorktreeCommandResult(1, {"state": "fixture-finalization-pending"})


def _originals(handoff: CloseoutCertificationHandoff) -> dict[Path, bytes]:
    objects = certificate_store(handoff.contract.worktree_group)
    refs = [handoff.selected.state.frozenRun]
    paths: set[Path] = set()
    for terminal in handoff.selected.terminals:
        refs.append(terminal.resultReference)
        if terminal.certificateReference is not None:
            refs.append(terminal.certificateReference)
        for name in terminal.publication.files:
            paths.add(
                published_report_path_from_manifest(
                    handoff.contract.worktree_group / "reports", terminal.publication, name
                )
            )
    paths.update(objects.exact_path(ref.kind, ref.semanticDigest) for ref in refs)
    return {path: path.read_bytes() for path in paths}


def _bind(continuation: _Continuation) -> None:
    bind_worktree_services(replace(worktree_services(), certification_continuation=continuation))


def test_current_producer_backed_gate_five_resumes_only_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    assert fixture.r08 is not None
    before = fixture.handoff.selected.state.terminals
    originals = _originals(fixture.handoff)
    continuation = _Continuation()
    _bind(continuation)
    result = execute_selected_closeout(
        fixture.handoff.contract, fixture.handoff.record, fixture.handoff.store
    )
    assert result.payload == {"state": "fixture-finalization-pending"}
    assert continuation.observations == 2 and len(continuation.finalizations) == 1
    current = continuation.finalizations[0]
    assert current.selected.recovery.semanticEnvelope.reusePlan.firstGateToRun is None
    assert current.selected.recovery.semanticEnvelope.reusePlan.zeroGateStarts
    assert current.selected.state.terminals == before
    assert (
        recovery_memory_inputs(current.selected.state.recoveryDecisions[-1])
        == fixture.r08.gateFiveInputs
    )
    assert len(fixture.code_calls) == 1 and current.record.status == "running"
    assert all(path.read_bytes() == data for path, data in originals.items())
    # Pending finalization can be observed again without appending another recovery
    # decision or emitting any previously certified gate as fresh work.
    journal = current.store.path.read_bytes()
    repeated = execute_selected_closeout(current.contract, current.record, current.store)
    assert repeated == result
    assert continuation.observations == 4 and len(continuation.finalizations) == 2
    assert continuation.finalizations[-1].selected == current.selected
    assert current.store.path.read_bytes() == journal
    assert len(fixture.code_calls) == 1
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_selected_fifth_terminal_requires_a_bound_current_memory_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    handoff = fixture.handoff
    assert worktree_services().certification_continuation is None
    journal = handoff.store.path.read_bytes()
    originals = _originals(handoff)
    with pytest.raises(CertificationContractError) as refused:
        execute_selected_closeout(handoff.contract, handoff.record, handoff.store)
    assert refused.value.findings[0]["code"] == "certification-continuation-unbound"
    assert handoff.store.path.read_bytes() == journal
    assert len(fixture.code_calls) == 1
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_metadata_successor_selects_the_exact_inherited_fifth_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    assert fixture.r08 is not None
    handoff = fixture.handoff
    originals = _originals(handoff)
    original_terminals = handoff.selected.state.terminals
    prior = handoff.store.update(
        lambda record: record.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": datetime.now(UTC).isoformat(),
            }
        )
    )
    assert isinstance(prior.input, CloseoutOperationInput)
    operation_input = prior.input.model_copy(update={"approvalNote": "Updated finalization note"})
    frozen = prepare_closeout_certification(
        handoff.contract,
        operation_input,
        prior,
        candidate_tree=handoff.selected.run.repositoryPlan.candidateIdentity.value,
    )
    assert frozen is not None
    queued = _queued(handoff.contract, operation_input, frozen.prepared.candidateTree)
    successor = handoff.store.replace_terminal(
        queued,
        initial_certification=lambda record: initial_certification_state(
            handoff.contract, record, frozen
        ),
    )
    assert successor.certification is not None
    assert successor.certification.inputTerminals == original_terminals
    assert len(successor.certification.terminals) == 4
    owner = OperationRuntime(handoff.store).start()
    continuation = _Continuation()
    _bind(continuation)
    execute_selected_closeout(handoff.contract, owner, handoff.store)
    current = continuation.finalizations[0]
    inherited = current.selected.state.terminals[-1]
    assert inherited == original_terminals[-1].model_copy(
        update={
            "reusedFrom": original_terminals[-1].reusedFrom or current.selected.state.predecessor,
        }
    )
    assert current.selected.recovery.semanticEnvelope.reusePlan.firstGateToRun is None
    assert len(fixture.code_calls) == 1 and continuation.observations == 2
    chain = tuple(
        item.certificate for item in current.selected.terminals if item.certificate is not None
    )
    validate_certificate_chain(
        current.selected.run.admission, chain, gate_five_inputs=fixture.r08.gateFiveInputs
    )
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_changed_actual_memory_requires_a_successor_before_selected_gate_five_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    handoff = fixture.handoff
    before = handoff.store.path.read_bytes()
    originals = _originals(handoff)
    assert handoff.contract.memory_worktree is not None
    path = handoff.contract.memory_worktree / "onboarding/README.md.md"
    path.write_text(path.read_text() + "\nChanged actual memory content.\n")
    continuation = _Continuation()
    _bind(continuation)
    with pytest.raises(CertificationContractError) as refused:
        execute_selected_closeout(handoff.contract, handoff.record, handoff.store)
    assert refused.value.findings[0]["code"] == "certification-memory-successor-required"
    assert continuation.unavailable and not continuation.finalizations
    assert handoff.store.path.read_bytes() == before
    assert len(fixture.code_calls) == 1
    assert all(path.read_bytes() == data for path, data in originals.items())


@pytest.mark.parametrize("cut", ["memory", "cancel"])
def test_second_actual_memory_observation_fences_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: Literal["memory", "cancel"]
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    handoff = fixture.handoff
    originals = _originals(handoff)
    continuation = _Continuation(cut=cut)
    _bind(continuation)
    with pytest.raises(CertificationContractError) as refused:
        execute_selected_closeout(handoff.contract, handoff.record, handoff.store)
    assert refused.value.findings[0]["code"] == (
        "certification-memory-inputs-moved"
        if cut == "memory"
        else "certification-selection-observation-moved"
    )
    assert continuation.observations == 2 and not continuation.finalizations
    assert len(fixture.code_calls) == 1
    record = handoff.store.read()
    assert record is not None and record.certification is not None
    assert record.certification.terminals == handoff.selected.state.terminals
    assert record.cancelRequested is (cut == "cancel")
    assert all(path.read_bytes() == data for path, data in originals.items())


def _policy_roots(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Real policy inputs isolated from host configuration and hook execution."""
    global_config = root / "global.gitconfig"
    global_config.write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    logical, private, hooks = root / "logical", root / "private", root / "hooks"
    logical.mkdir()
    hooks.mkdir()
    git(logical, "init", "-q", "-b", "policy-fixture")
    git(logical, "config", "user.name", "Policy Fixture")
    git(logical, "config", "user.email", "policy@example.invalid")
    git(logical, "config", "commit.gpgsign", "false")
    git(logical, "config", "core.hooksPath", str(hooks))
    (logical / "source.txt").write_text("policy fixture\n", encoding="utf-8")
    git(logical, "add", "source.txt")
    git(logical, "commit", "-qm", "Actual policy fixture")
    git(logical, "worktree", "add", "--detach", str(private), "HEAD")
    return logical, private, hooks


def _policy_intent(logical: Path, private: Path) -> CloseoutPreparationIntent:
    """Typed comparison fixture only: its references assert no certificate issuance."""

    def reference(kind: CertificateObjectKind, marker: str) -> dict[str, object]:
        return CertificateObjectReference(
            kind=kind, semanticDigest=marker * 64, contentSha256=marker * 64, sizeBytes=1
        ).model_dump(mode="json")

    policy = observe_git_preparation_policy(logical)
    parent = git(logical, "rev-parse", "HEAD")
    payload = {
        "schemaVersion": "closeout-preparation-intent/v1",
        "operationKey": "a" * 64,
        "generation": 1,
        "contractPath": str(logical.parent / "policy-contract.md"),
        "contractSha256": "b" * 64,
        "leg": "code",
        "writeEnabled": True,
        "repositoryIdentity": git(
            logical, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ),
        "logicalRoot": str(logical),
        "logicalRef": "refs/heads/policy-fixture",
        "expectedOldCommit": parent,
        "parentCommit": parent,
        "admittedTree": git(logical, "rev-parse", "HEAD^{tree}"),
        "privateRoot": str(private),
        "normalizedMessage": "Compare actual policy",
        "hookPolicy": "strict-code-no-verify",
        "gitConfigSha256": policy.git_config_sha256,
        "hooksSha256": policy.hooks_sha256,
        "frozenRun": reference("frozen-run", "c"),
        "candidateAuthorities": reference("candidate-authorities", "d"),
        "prefixCertificates": [reference("certificate", marker) for marker in "abcd"],
        "gateFiveCertificate": None,
        "existingMemoryProof": None,
    }
    return CloseoutPreparationIntent.model_validate(
        {**payload, "intentDigest": canonical_sha256(payload)}
    )


def test_preparation_policy_matches_actual_logical_and_linked_private_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logical, private, _hooks = _policy_roots(tmp_path, monkeypatch)
    intent = _policy_intent(logical, private)
    original = observe_git_preparation_policy(logical)
    observed = observe_git_preparation_policy(private)

    assert observed == original
    observed.require_intent(intent)
    assert git(logical, "rev-parse", "HEAD") == git(private, "rev-parse", "HEAD")
    assert git(logical, "status", "--porcelain") == git(private, "status", "--porcelain") == ""


@pytest.mark.parametrize("scope", ["worktree-local", "conditional"])
def test_preparation_policy_refuses_actual_private_commit_configuration_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    logical, private, _hooks = _policy_roots(tmp_path, monkeypatch)
    if scope == "worktree-local":
        git(logical, "config", "extensions.worktreeConfig", "true")
        git(private, "config", "--worktree", "commit.cleanup", "verbatim")
    else:
        included = tmp_path / "private.gitconfig"
        included.write_text("[commit]\n\tcleanup = verbatim\n", encoding="utf-8")
        git_dir = git(private, "rev-parse", "--absolute-git-dir")
        git(logical, "config", f"includeIf.gitdir:{git_dir}.path", str(included))
    intent = _policy_intent(logical, private)
    original = observe_git_preparation_policy(logical)
    observed = observe_git_preparation_policy(private)

    assert git(private, "config", "--get", "commit.cleanup") == "verbatim"
    assert observed.git_config_sha256 != original.git_config_sha256
    assert observed.hooks_sha256 == original.hooks_sha256
    with pytest.raises(PreparationPolicyError, match="differs from selected intent"):
        observed.require_intent(intent)
    assert git(logical, "rev-parse", "HEAD") == intent.expectedOldCommit
    assert git(private, "rev-parse", "HEAD") == intent.parentCommit


def test_preparation_policy_refuses_changed_actual_executable_hook_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logical, private, hooks = _policy_roots(tmp_path, monkeypatch)
    hook = hooks / "prepare-commit-msg"
    hook.write_bytes(b"#!/bin/sh\n# original bytes\nexit 0\n")
    hook.chmod(0o755)
    intent = _policy_intent(logical, private)
    observe_git_preparation_policy(private).require_intent(intent)
    hook.write_bytes(b"#!/bin/sh\n# changed bytes\nexit 0\n")
    observed = observe_git_preparation_policy(private)

    assert observed.git_config_sha256 == intent.gitConfigSha256
    assert observed.hooks_sha256 != intent.hooksSha256
    with pytest.raises(PreparationPolicyError, match="differs from selected intent"):
        observed.require_intent(intent)
    assert git(logical, "rev-parse", "HEAD") == intent.expectedOldCommit
    assert git(private, "status", "--porcelain") == ""


@pytest.mark.parametrize("link", ["hook", "ancestor"])
def test_preparation_policy_refuses_actual_linked_hook_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, link: str
) -> None:
    logical, private, hooks = _policy_roots(tmp_path, monkeypatch)
    hook = hooks / "prepare-commit-msg"
    hook.write_bytes(b"#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    observe_git_preparation_policy(private).require_intent(_policy_intent(logical, private))
    before = git(logical, "rev-parse", "HEAD")
    if link == "hook":
        target = tmp_path / "actual-hook"
        hook.rename(target)
        hook.symlink_to(target)
    else:
        target = tmp_path / "actual-hooks"
        hooks.rename(target)
        hooks.symlink_to(target, target_is_directory=True)
    with pytest.raises(PreparationPolicyError, match="hook path is not canonical"):
        observe_git_preparation_policy(private)
    assert git(logical, "rev-parse", "HEAD") == git(private, "rev-parse", "HEAD") == before
    assert git(private, "status", "--porcelain") == ""


@pytest.mark.parametrize("variable", ["GIT_AUTHOR_NAME", "GIT_COMMITTER_DATE"])
def test_preparation_policy_binds_identity_environment_without_disclosing_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
) -> None:
    logical, private, _hooks = _policy_roots(tmp_path, monkeypatch)
    monkeypatch.delenv(variable, raising=False)
    intent = _policy_intent(logical, private)
    secret = "private-identity-marker" if variable == "GIT_AUTHOR_NAME" else "1234567890 +0123"
    monkeypatch.setenv(variable, secret)
    observed = observe_git_preparation_policy(private)

    assert observed.git_config_sha256 != intent.gitConfigSha256
    assert observed.hooks_sha256 == intent.hooksSha256
    with pytest.raises(PreparationPolicyError, match="differs from selected intent") as refused:
        observed.require_intent(intent)
    captured = capsys.readouterr()
    assert secret not in repr(observed) + str(refused.value) + captured.out + captured.err
    assert git(logical, "rev-parse", "HEAD") == intent.expectedOldCommit


@pytest.mark.integration
def test_real_gate_five_prepares_then_publishes_memory_and_ledger(tmp_path: Path) -> None:
    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from agents_remember_test_support.testing.global_state import begin_pytest_process",
            "from agents_remember.application.worktree_services import build_default_worktree_services",
            "from agents_remember.worktrees.services import bind_worktree_services",
            "begin_pytest_process()",
            "bind_worktree_services(build_default_worktree_services())",
            "from test_closeout_memory_certification_reuse import _prepare_memory_output_scenario",
            "_prepare_memory_output_scenario(Path(sys.argv[1]))",
        )
    )
    with subprocess.Popen(
        [sys.executable, "-B", "-c", script, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail(f"memory preparation worker timed out\n{stdout}\n{stderr}")
        assert process.returncode == 0, stdout + stderr


def _prepare_memory_output_scenario(root: Path) -> None:
    from agents_remember.kernel.memory_ledger import (  # noqa: PLC0415
        find_mapping,
        parse_ledger_text,
    )
    from agents_remember.memory_quality.prepared_certification import (  # noqa: PLC0415
        PreparedMemoryCertificationAdapter,
    )
    from agents_remember.worktrees.integration.closeout.preparation.code_view import (  # noqa: PLC0415
        prepare_code_view,
    )
    from agents_remember.worktrees.integration.closeout.preparation.memory_execution import (  # noqa: PLC0415
        observe_prepared_memory_candidate,
    )
    from agents_remember.worktrees.integration.closeout.preparation.memory_output import (  # noqa: PLC0415
        prepare_memory_outputs,
    )
    from agents_remember.worktrees.integration.closeout.preparation.memory_port import (  # noqa: PLC0415
        PreparedMemoryCertificationRequest,
    )

    with pytest.MonkeyPatch.context() as patch:
        fixture = _fixture(root, patch, live_worker=True, publish_memory=False)
    # Restore the scheduler-only code-view stub before exercising the actual owner.
    handoff, view = prepare_code_view(fixture.handoff)
    assert view.disposition == "existing"
    candidate = observe_prepared_memory_candidate(handoff, view)
    assert len(handoff.selected.terminals) == 4
    request = PreparedMemoryCertificationRequest(handoff, candidate)
    adapter = PreparedMemoryCertificationAdapter()
    certified = adapter.certify(request)
    assert adapter.observe(replace(request, handoff=certified.handoff)) == certified.memoryInputs
    memory_root = handoff.contract.memory_worktree
    assert memory_root is not None
    roots = (handoff.contract.code_worktree, memory_root)
    before = tuple(git(path, "rev-parse", "HEAD") for path in roots)
    ledger_before = (memory_root / "memory.md").read_bytes()
    prepared = prepare_memory_outputs(certified)
    assert prepared.memory.intent.writeEnabled and prepared.ledger.intent.writeEnabled
    memory_private = Path(prepared.memory.intent.privateRoot or "")
    ledger_private = Path(prepared.ledger.intent.privateRoot or "")
    memory_commit = git(memory_private, "rev-parse", "HEAD")
    ledger_commit = git(ledger_private, "rev-parse", "HEAD")
    assert git(memory_private, "rev-parse", "HEAD^{tree}") == candidate.memoryTree
    assert git(memory_private, "rev-parse", "HEAD^") == before[1]
    assert git(ledger_private, "rev-parse", "HEAD^") == memory_commit
    assert git(memory_root, "diff", "--name-only", memory_commit, ledger_commit) == "memory.md"
    mapping = find_mapping(parse_ledger_text(prepared.ledgerBytes.decode()), view.codeCommit)
    assert mapping is not None and mapping.memory_commit == memory_commit
    assert (ledger_private / "memory.md").read_bytes() == prepared.ledgerBytes
    journal = handoff.store.path.read_bytes()
    repeated = prepare_memory_outputs(replace(certified, handoff=prepared.handoff))
    assert repeated == prepared
    assert handoff.store.path.read_bytes() == journal
    assert tuple(git(path, "rev-parse", "HEAD") for path in roots) == before
    assert (memory_root / "memory.md").read_bytes() == ledger_before

    _publish_and_recover_memory_outputs(
        prepared, view.codeCommit, memory_commit, ledger_commit, ledger_before
    )


def _publish_and_recover_memory_outputs(
    prepared, code_commit: str, memory_commit: str, ledger_commit: str, ledger_before: bytes
) -> None:
    from agents_remember.worktrees.integration.closeout.preparation import (  # noqa: PLC0415
        finalization,
    )
    from agents_remember.worktrees.integration.closeout.preparation.continuation import (  # noqa: PLC0415
        PreparedCloseoutContinuation,
    )

    handoff = prepared.handoff
    memory_root = handoff.contract.memory_worktree
    assert memory_root is not None
    publish = finalization.publish_git_closeout_ref

    def interrupt_after_memory_publication(capability):
        result = publish(capability)
        if capability.binding.prepared_commit == memory_commit:
            assert result.after.state == "new"
            raise RuntimeError("interrupt after actual memory ref publication")
        return result

    with (
        mock.patch.object(
            finalization, "publish_git_closeout_ref", interrupt_after_memory_publication
        ),
        pytest.raises(RuntimeError, match="interrupt after actual memory ref publication"),
    ):
        PreparedCloseoutContinuation().finalize(handoff)
    interrupted = handoff.store.read()
    assert interrupted is not None
    assert interrupted.mutationEvidence["memory"].state == "mutation-intent"
    assert git(memory_root, "rev-parse", "HEAD") == memory_commit
    assert (memory_root / "memory.md").read_bytes() == ledger_before
    closed = finalization.resume_prepared_closeout(handoff.contract, interrupted, handoff.store)
    assert closed is not None and closed.returncode == 0
    assert git(handoff.contract.code_worktree, "rev-parse", "HEAD") == code_commit
    assert git(memory_root, "rev-parse", "HEAD") == ledger_commit
    assert (memory_root / "memory.md").read_bytes() == prepared.ledgerBytes
    contract = load_contract(handoff.contract.contract_path)
    assert contract.closeout_status == "completed"
    assert (contract.code_commit, contract.memory_content_commit, contract.ledger_commit) == (
        code_commit,
        memory_commit,
        ledger_commit,
    )

    current = handoff.store.read()
    assert current is not None
    journal = handoff.store.path.read_bytes()
    reopened = finalization.resume_prepared_closeout(contract, current, handoff.store)
    assert reopened == closed
    assert handoff.store.path.read_bytes() == journal
    assert git(memory_root, "rev-parse", "HEAD") == ledger_commit
