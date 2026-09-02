"""One structured curator-coherence authority shared by memory and closeout."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from agents_remember.errors import (
    CuratorCoherenceError,
    CuratorCoherencePairError,
    FutureCodeCandidateError,
    MemoryCandidatePairError,
    TaskIntentError,
)
from agents_remember.models.closeout.source import EvidenceFact
from agents_remember.models.lifecycles.curator_coherence import (
    CuratorCoherenceAuthority,
    CuratorCoherenceRecord,
    CuratorQualityAttestation,
    CuratorSourceCandidate,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks.document_refs import (
    ResolvedTaskDocument,
    TaskDocumentRefError,
    TaskDocumentTopology,
)
from agents_remember.tasks.leaf_doc import resolve_terminal_leaf_doc
from agents_remember.tasks.task_intent import (
    require_current_task_intent,
    task_intent_identity,
)
from agents_remember.worktrees.integration.closeout.future_code_candidate import (
    capture_future_code_candidate,
)
from agents_remember.worktrees.integration.closeout.memory_candidate_pair import (
    resolve_memory_candidate_pair,
)
from agents_remember.worktrees.modules.git import worktree_candidate_tree
from agents_remember.worktrees.queue.closeout_projection_members import (
    candidate_task_topology_fingerprint,
)
from agents_remember.worktrees.queue.closeout_queue_graph import graph_context
from agents_remember.worktrees.worktree_contract import WorktreeContract

from .curator_coherence_render import render_curator_coherence

QUALITY_REPORT_NAME = "curator-memory-quality.md"
QUALITY_ATTESTATION_NAME = "curator-memory-quality.json"


@dataclass(frozen=True)
class CuratorCoherencePaths:
    canonical: Path
    generations: Path
    snapshots: Path

    def generation_record(self, digest: str) -> Path:
        return self.generations / digest / "record.json"

    def generation_report(self, digest: str) -> Path:
        return self.generations / digest / "report.md"


@dataclass(frozen=True)
class CuratorCoherenceObservation:
    candidate: ResolvedTaskDocument
    pair_identity: MemoryCandidatePairIdentity
    code_candidate_tree: str
    memory_candidate_tree: str
    task_topology_fingerprint: str
    task_intent: TaskIntentIdentity
    attestation_path: Path
    attestation_sha256: str
    quality_report_path: Path
    attestation: CuratorQualityAttestation

    @property
    def source_candidates(self) -> list[CuratorSourceCandidate]:
        return self.attestation.sourceChangeCandidates


@dataclass(frozen=True)
class ValidatedCuratorCoherence:
    authority: CuratorCoherenceAuthority
    record: CuratorCoherenceRecord
    record_path: Path
    report_path: Path
    record_digest: str
    evidence: list[EvidenceFact]


@dataclass(frozen=True)
class CuratorCoherenceNoImpact:
    """Candidate-bound no-impact judgments accepted by onboarding body gates."""

    content_sources: frozenset[str] = frozenset()
    source_routes: frozenset[str] = frozenset()


def curator_coherence_no_impact(
    validated: ValidatedCuratorCoherence,
) -> CuratorCoherenceNoImpact:
    """Project exact current coherence judgments into the two no-impact lanes."""

    judgments = validated.record.judgments
    return CuratorCoherenceNoImpact(
        content_sources=frozenset(
            judgment.sourceFile
            for judgment in judgments
            if judgment.disposition == "no-content-impact"
        ),
        source_routes=frozenset(
            judgment.sourceFile
            for judgment in judgments
            if judgment.disposition == "no-route-impact"
        ),
    )


def curator_coherence_paths(contract: WorktreeContract) -> CuratorCoherencePaths:
    _require_leaf_external_memory(contract)
    reports = contract.task_root / "notes" / "reports"
    history = reports / "curator-coherence" / contract.leaf_id
    return CuratorCoherencePaths(
        canonical=reports / f"{contract.leaf_id}-curator-coherence.json",
        generations=history / "generations",
        snapshots=history / "attempts",
    )


def observe_curator_coherence_source(
    contract: WorktreeContract,
) -> CuratorCoherenceObservation:
    """Capture every identity a publication must freeze and later re-prove."""

    _require_leaf_external_memory(contract)
    try:
        pair_identity = resolve_memory_candidate_pair(
            contract,
            requested_contract_path=contract.contract_path,
            requested_repo_id=contract.repo_name,
        )
    except MemoryCandidatePairError as exc:
        raise CuratorCoherencePairError(exc) from exc
    candidate, master, sprint, graph = _task_context(contract)
    try:
        code_tree = capture_future_code_candidate(contract).codeCandidateTree
        memory_tree = _memory_candidate_tree(contract)
    except FutureCodeCandidateError as exc:
        raise CuratorCoherenceError(exc.status, str(exc), next_action="prepare") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise CuratorCoherenceError(
            "curator-coherence-candidate-unreadable",
            "could not capture the exact code and memory candidates",
            next_action="prepare",
        ) from exc
    attestation_path = contract.worktree_group / "reports" / QUALITY_ATTESTATION_NAME
    report_path = contract.worktree_group / "reports" / QUALITY_REPORT_NAME
    attestation, attestation_digest = _quality_attestation(
        contract,
        attestation_path,
        report_path,
        pair_identity,
    )
    topology_fingerprint = candidate_task_topology_fingerprint(
        sprint,
        master,
        candidate,
        graph=graph,
    )
    try:
        intent = task_intent_identity(contract.task_root, candidate)
    except TaskIntentError as exc:
        raise CuratorCoherenceError(
            exc.status,
            exc.detail,
            next_action=exc.next_action or "task_doc",
        ) from exc
    return CuratorCoherenceObservation(
        candidate=candidate,
        pair_identity=pair_identity,
        code_candidate_tree=code_tree,
        memory_candidate_tree=memory_tree,
        task_topology_fingerprint=topology_fingerprint,
        task_intent=intent,
        attestation_path=attestation_path,
        attestation_sha256=attestation_digest,
        quality_report_path=report_path,
        attestation=attestation,
    )


def current_curator_coherence_predecessor(contract: WorktreeContract) -> str:
    """Digest the stable authority bytes, including malformed bytes, for exact replacement CAS."""

    path = curator_coherence_paths(contract).canonical
    if not path.exists():
        return ""
    try:
        return _digest(path.read_bytes())
    except OSError as exc:
        raise CuratorCoherenceError(
            "curator-coherence-authority-unreadable",
            "the stable authority exists but its bytes cannot be read for replacement",
            next_action="developer-decision",
        ) from exc


def load_curator_coherence_authority(
    contract: WorktreeContract,
) -> ValidatedCuratorCoherence:
    """Validate authority, immutable generation, and generated projection bytes."""

    paths = curator_coherence_paths(contract)
    try:
        authority_bytes = paths.canonical.read_bytes()
        authority = CuratorCoherenceAuthority.model_validate_json(authority_bytes)
    except (OSError, ValidationError) as exc:
        raise CuratorCoherenceError(
            "curator-coherence-authority-missing",
            f"the stable structured authority is absent or unreadable: {paths.canonical}",
            next_action="publish",
        ) from exc
    _require_authority_identity(contract, authority)
    record_path = paths.generation_record(authority.currentRecordDigest)
    report_path = paths.generation_report(authority.currentRecordDigest)
    expected_record_ref = _task_relative(contract, record_path)
    expected_report_ref = _task_relative(contract, report_path)
    if (authority.recordPath, authority.reportPath) != (
        expected_record_ref,
        expected_report_ref,
    ):
        raise CuratorCoherenceError(
            "curator-coherence-authority-path-invalid",
            "the stable authority does not identify its exact content-addressed generation",
            expected={"recordPath": expected_record_ref, "reportPath": expected_report_ref},
            observed={"recordPath": authority.recordPath, "reportPath": authority.reportPath},
            next_action="publish",
        )
    try:
        record_bytes = record_path.read_bytes()
        report_bytes = report_path.read_bytes()
        record = CuratorCoherenceRecord.model_validate_json(record_bytes)
    except (OSError, ValidationError) as exc:
        raise CuratorCoherenceError(
            "curator-coherence-generation-unreadable",
            "the authority's content-addressed generation is absent or invalid",
            next_action="publish",
        ) from exc
    record_digest = _digest(record_bytes)
    report_digest = _digest(report_bytes)
    if record_digest != authority.currentRecordDigest:
        raise CuratorCoherenceError(
            "curator-coherence-record-digest-mismatch",
            "canonical record bytes do not match the authority digest",
            expected={"recordDigest": authority.currentRecordDigest},
            observed={"recordDigest": record_digest},
            next_action="publish",
        )
    expected_projection = render_curator_coherence(record).encode("utf-8")
    if (
        report_digest,
        record.reportSha256,
        authority.reportSha256,
        report_bytes,
    ) != (
        authority.reportSha256,
        authority.reportSha256,
        authority.reportSha256,
        expected_projection,
    ):
        raise CuratorCoherenceError(
            "curator-coherence-projection-digest-mismatch",
            "generated Markdown does not match the structured record and authority digest",
            next_action="publish",
        )
    _require_record_identity(contract, record)
    _require_evidence_refs(contract, record)
    return ValidatedCuratorCoherence(
        authority=authority,
        record=record,
        record_path=record_path,
        report_path=report_path,
        record_digest=record_digest,
        evidence=[
            _fact(contract.worktree_group / "reports" / QUALITY_REPORT_NAME),
            _fact(contract.worktree_group / "reports" / QUALITY_ATTESTATION_NAME),
            _fact(paths.canonical),
            _fact(record_path),
            _fact(report_path),
        ],
    )


def require_current_curator_coherence(
    contract: WorktreeContract,
) -> ValidatedCuratorCoherence:
    """The single validator used by public validation, preflight, and admission."""

    observation = observe_curator_coherence_source(contract)
    validated = load_curator_coherence_authority(contract)
    record = validated.record
    try:
        require_current_task_intent(
            record.taskIntent,
            observation.task_intent,
            owner="curator-coherence",
            next_action="publish",
        )
    except TaskIntentError as exc:
        raise CuratorCoherenceError(
            exc.status,
            exc.detail,
            next_action=exc.next_action or "publish",
        ) from exc
    observed = {
        "pairIdentity": observation.pair_identity.model_dump(mode="json"),
        "codeCandidateTree": observation.code_candidate_tree,
        "memoryCandidateTree": observation.memory_candidate_tree,
        "taskTopologyFingerprint": observation.task_topology_fingerprint,
        "taskIntent": observation.task_intent.model_dump(mode="json", by_alias=True),
        "attestationSha256": observation.attestation_sha256,
        "sourceCandidates": [
            candidate.model_dump(mode="json") for candidate in observation.source_candidates
        ],
    }
    expected = {
        "pairIdentity": record.pairIdentity.model_dump(mode="json"),
        "codeCandidateTree": record.codeCandidateTree,
        "memoryCandidateTree": record.memoryCandidateTree,
        "taskTopologyFingerprint": record.taskTopologyFingerprint,
        "taskIntent": record.taskIntent.model_dump(mode="json", by_alias=True),
        "attestationSha256": record.attestationSha256,
        "sourceCandidates": [
            candidate.model_dump(mode="json") for candidate in record.sourceCandidates
        ],
    }
    if observed != expected:
        raise CuratorCoherenceError(
            "curator-coherence-stale",
            "the live coherence authority does not bind the exact current candidate sources",
            expected=expected,
            observed=observed,
            next_action="publish",
        )
    if record.taskDocumentRef != observation.candidate.ref:
        raise CuratorCoherenceError(
            "curator-coherence-task-mismatch",
            "the live coherence record names a different leaf task document",
            next_action="publish",
        )
    return validated


def curator_coherence_evidence(contract: WorktreeContract) -> list[EvidenceFact]:
    return require_current_curator_coherence(contract).evidence


def _quality_attestation(
    contract: WorktreeContract,
    attestation_path: Path,
    report_path: Path,
    pair_identity: MemoryCandidatePairIdentity,
) -> tuple[CuratorQualityAttestation, str]:
    assert contract.memory_worktree is not None
    try:
        attestation_bytes = attestation_path.read_bytes()
        report_bytes = report_path.read_bytes()
        attestation = CuratorQualityAttestation.model_validate_json(attestation_bytes)
    except (OSError, ValidationError) as exc:
        raise CuratorCoherenceError(
            "curator-coherence-attestation-unreadable",
            "the exact ar-curator-memory-quality/v1 attestation is absent or invalid",
            next_action="memory_quality_check",
        ) from exc
    expected_onboarding = (contract.memory_worktree / "onboarding").resolve().as_posix()
    expected_report = report_path.resolve().as_posix()
    expected_ready = (
        "ready-for-closeout",
        0,
        0,
        0,
        0,
        expected_onboarding,
        expected_report,
        _digest(report_bytes),
        pair_identity,
    )
    observed_ready = (
        attestation.checklistStatus,
        attestation.curatorActionableCount,
        attestation.memoryRepairCount,
        attestation.missingOnboardingCount,
        attestation.staleRouteIndexCount,
        attestation.onboardingRoot,
        attestation.reportPath,
        attestation.reportSha256,
        attestation.pairIdentity,
    )
    if observed_ready != expected_ready:
        raise CuratorCoherenceError(
            "curator-coherence-memory-not-ready",
            "memory quality and its rendered checklist do not prove exact closeout readiness",
            next_action="memory_quality_check",
        )
    return attestation, _digest(attestation_bytes)


def _task_context(contract: WorktreeContract):
    topology = TaskDocumentTopology(contract.coordination_root)
    found = resolve_terminal_leaf_doc(contract.task_root, contract.leaf_id)
    if found is None:
        raise CuratorCoherenceError(
            "curator-coherence-task-missing",
            "the leaf enclosure has no exact canonical task document",
            next_action="task_doc",
        )
    try:
        candidate = topology.resolve(topology.canonical_ref(contract.repo_name, found[0]))
        master_ref = topology.parent(candidate.ref)
        if master_ref is None:
            raise TaskDocumentRefError("task-document-parent-missing", candidate.ref.key)
        master = topology.resolve(master_ref)
        sprint_ref = topology.parent(master.ref)
        if sprint_ref is None:
            raise TaskDocumentRefError("task-document-parent-missing", master.ref.key)
        sprint = topology.resolve(sprint_ref)
        authored_graph = sprint.document.executionGraph
        graph = (
            graph_context(topology, sprint.ref, authored_graph=authored_graph)
            if authored_graph is not None
            else None
        )
    except TaskDocumentRefError as exc:
        raise CuratorCoherenceError(exc.status, str(exc), next_action="task_doc") from exc
    return candidate, master, graph.sprint if graph is not None else sprint, graph


def _memory_candidate_tree(contract: WorktreeContract) -> str:
    assert contract.memory_worktree is not None
    reports = contract.worktree_group / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".curator-coherence-memory-", dir=reports) as temporary:
        return worktree_candidate_tree(contract.memory_worktree, Path(temporary) / "index")


def _require_leaf_external_memory(contract: WorktreeContract) -> None:
    if contract.kind != "leaf" or contract.memory_mode != "external":
        raise CuratorCoherenceError(
            "curator-coherence-not-applicable",
            "curator coherence requires one external-memory leaf enclosure",
            next_action="status",
        )
    if contract.memory_worktree is None:
        raise CuratorCoherenceError(
            "curator-coherence-memory-worktree-missing",
            "the external-memory leaf has no memory worktree",
            next_action="worktree_status",
        )


def _require_authority_identity(
    contract: WorktreeContract, authority: CuratorCoherenceAuthority
) -> None:
    if (authority.leafId, authority.contractPath) != (
        contract.leaf_id,
        contract.contract_path.as_posix(),
    ):
        raise CuratorCoherenceError(
            "curator-coherence-authority-identity-mismatch",
            "the stable authority belongs to a different leaf or contract",
            next_action="developer-decision",
        )


def _require_record_identity(contract: WorktreeContract, record: CuratorCoherenceRecord) -> None:
    if (record.leafId, record.contractPath) != (
        contract.leaf_id,
        contract.contract_path.as_posix(),
    ):
        raise CuratorCoherenceError(
            "curator-coherence-record-identity-mismatch",
            "the selected record belongs to a different leaf or contract",
            next_action="developer-decision",
        )


def _require_evidence_refs(contract: WorktreeContract, record: CuratorCoherenceRecord) -> None:
    for judgment in record.judgments:
        evidence = resolve_curator_evidence_ref(contract, judgment.evidenceRef)
        observed = _digest(evidence.read_bytes())
        if observed != judgment.evidenceSha256:
            raise CuratorCoherenceError(
                "curator-coherence-evidence-stale",
                f"judgment evidence bytes changed: {judgment.evidenceRef}",
                expected={"evidenceSha256": judgment.evidenceSha256},
                observed={"evidenceSha256": observed},
                next_action="publish",
            )


def resolve_curator_evidence_ref(contract: WorktreeContract, reference: str) -> Path:
    """Resolve one explicit evidence namespace without implicit path fallback."""

    namespace, separator, relative_text = reference.partition(":")
    roots = {
        "code": contract.code_worktree,
        "memory": contract.memory_worktree,
        "task": contract.task_root,
    }
    root = roots.get(namespace)
    relative = Path(relative_text)
    if (
        not separator
        or root is None
        or not relative_text
        or relative.is_absolute()
        or relative == Path(".")
    ):
        raise CuratorCoherenceError(
            "curator-coherence-evidence-invalid",
            "judgment evidence must use one explicit code:, memory:, or task: file reference",
            next_action="publish",
        )
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve(strict=False)
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise CuratorCoherenceError(
            "curator-coherence-evidence-invalid",
            f"judgment evidence must identify one existing scoped file: {reference}",
            next_action="publish",
        )
    return resolved


def _task_relative(contract: WorktreeContract, path: Path) -> str:
    resolved = path.resolve(strict=False)
    root = contract.task_root.resolve()
    if not resolved.is_relative_to(root):
        raise CuratorCoherenceError(
            "curator-coherence-path-outside-task",
            "coherence authority paths must remain inside the task root",
            next_action="developer-decision",
        )
    return resolved.relative_to(root).as_posix()


def _fact(path: Path) -> EvidenceFact:
    return EvidenceFact(path=path.resolve().as_posix(), sha256=_digest(path.read_bytes()))


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "CuratorCoherenceNoImpact",
    "CuratorCoherenceObservation",
    "CuratorCoherencePaths",
    "ValidatedCuratorCoherence",
    "curator_coherence_evidence",
    "curator_coherence_no_impact",
    "curator_coherence_paths",
    "current_curator_coherence_predecessor",
    "load_curator_coherence_authority",
    "observe_curator_coherence_source",
    "require_current_curator_coherence",
    "resolve_curator_evidence_ref",
]
