"""CAS publication and action dispatch for structured curator coherence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from agents_remember.controlplane.task_publication_lock import task_publication_lock
from agents_remember.errors import CuratorCoherenceError
from agents_remember.kernel.atomic_write import atomic_replace, atomic_write_text
from agents_remember.models.lifecycles.curator_coherence import (
    CuratorCoherenceAuthority,
    CuratorCoherenceRecord,
    CuratorCoherenceRecordedJudgment,
    CuratorCoherenceRequest,
    CuratorCoherenceSnapshot,
)
from agents_remember.tasks.document_refs import TaskDocumentRefError, TaskDocumentTopology
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract

from .curator_coherence import (
    CuratorCoherenceObservation,
    curator_coherence_paths,
    current_curator_coherence_predecessor,
    load_curator_coherence_authority,
    observe_curator_coherence_source,
    require_current_curator_coherence,
)
from .curator_coherence_judgments import (
    exact_curator_judgments,
    require_recorded_judgments_current,
)
from .curator_coherence_render import render_curator_coherence


@dataclass(frozen=True)
class _RecordPublication:
    judgments: list[CuratorCoherenceRecordedJudgment]
    fingerprint: str
    predecessor: str


@dataclass(frozen=True)
class _ObservationView:
    state: str
    summary: str
    predecessor: str
    currentness_status: str | None = None
    next_action: str | None = None


def curator_coherence_action(
    contract: WorktreeContract,
    request: CuratorCoherenceRequest,
) -> dict[str, object]:
    if request.action == "status":
        return _status(contract, request)
    if request.action == "prepare":
        return _prepare(contract, request)
    if request.action == "validate":
        return _validate(contract, request)
    return _publish(contract, request)


def _status(contract: WorktreeContract, request: CuratorCoherenceRequest) -> dict[str, object]:
    paths = curator_coherence_paths(contract)
    try:
        observation = observe_curator_coherence_source(contract)
    except CuratorCoherenceError as exc:
        return _success(
            request,
            contract,
            state="source-not-ready",
            summary=str(exc),
            canonicalPath=paths.canonical.as_posix(),
            currentnessStatus=exc.status,
            nextAction=exc.next_action,
        )
    if not paths.canonical.exists():
        return _observation_payload(
            request,
            contract,
            observation,
            _ObservationView(
                state="absent",
                summary="No live curator-coherence authority has been published for this leaf.",
                predecessor="",
            ),
        )
    try:
        validated = require_current_curator_coherence(contract)
    except CuratorCoherenceError as exc:
        predecessor = current_curator_coherence_predecessor(contract)
        return _observation_payload(
            request,
            contract,
            observation,
            _ObservationView(
                state="stale",
                summary=str(exc),
                predecessor=predecessor,
                currentness_status=exc.status,
                next_action=exc.next_action,
            ),
        )
    return _validated_payload(request, contract, validated, state="current")


def _prepare(contract: WorktreeContract, request: CuratorCoherenceRequest) -> dict[str, object]:
    observation = observe_curator_coherence_source(contract)
    predecessor = current_curator_coherence_predecessor(contract)
    return _observation_payload(
        request,
        contract,
        observation,
        _ObservationView(
            state="prepared",
            summary=(
                "Exact source candidates and optimistic-concurrency identities are prepared; "
                "supply one agent-owned judgment per candidate to publish."
            ),
            predecessor=predecessor,
        ),
    )


def _validate(contract: WorktreeContract, request: CuratorCoherenceRequest) -> dict[str, object]:
    validated = require_current_curator_coherence(contract)
    return _validated_payload(request, contract, validated, state="valid")


def _publish(contract: WorktreeContract, request: CuratorCoherenceRequest) -> dict[str, object]:
    observation = observe_curator_coherence_source(contract)
    _require_expected_observation(request, observation)
    judgments = exact_curator_judgments(contract, observation.source_candidates, request.judgments)
    _authorize_publisher(contract, observation, request)
    publication_fingerprint = _publication_fingerprint(request, observation, judgments)
    predecessor = current_curator_coherence_predecessor(contract)
    replay = _idempotent_replay(contract, publication_fingerprint)
    if replay is not None:
        return _validated_payload(request, contract, replay, state="already-current")
    if predecessor != request.expected_predecessor_digest:
        raise CuratorCoherenceError(
            "curator-coherence-predecessor-stale",
            "the live canonical predecessor changed after prepare",
            expected={"predecessorAuthorityDigest": request.expected_predecessor_digest or ""},
            observed={"predecessorAuthorityDigest": predecessor},
            next_action="prepare",
        )
    record, report = _record(
        contract,
        request,
        observation,
        _RecordPublication(
            judgments=judgments,
            fingerprint=publication_fingerprint,
            predecessor=predecessor,
        ),
    )
    record_bytes = _json_bytes(record.model_dump(mode="json", by_alias=True))
    report_bytes = report.encode("utf-8")
    record_digest = _digest(record_bytes)
    paths = curator_coherence_paths(contract)
    record_path = paths.generation_record(record_digest)
    report_path = paths.generation_report(record_digest)
    snapshot_path: Path | None = None
    with task_publication_lock(contract.coordination_root, contract.repo_name):
        current_contract = load_contract(contract.contract_path)
        if current_contract != contract:
            raise CuratorCoherenceError(
                "curator-coherence-contract-stale",
                "the leaf contract changed during coherence publication",
                next_action="prepare",
            )
        replay = _idempotent_replay(current_contract, publication_fingerprint)
        if replay is not None:
            return _validated_payload(request, current_contract, replay, state="already-current")
        _require_predecessor(current_contract, request)
        _require_observation_unchanged(
            observation, observe_curator_coherence_source(current_contract)
        )
        require_recorded_judgments_current(current_contract, judgments)
        _publish_generation(record_path, report_path, record_bytes, report_bytes)
        if request.freeze_snapshot:
            snapshot_path = _publish_snapshot(
                current_contract,
                record,
                record_digest,
                record_path,
                report_path,
            )
        _require_observation_unchanged(
            observation, observe_curator_coherence_source(current_contract)
        )
        require_recorded_judgments_current(current_contract, judgments)
        authority = CuratorCoherenceAuthority(
            leafId=current_contract.leaf_id,
            contractPath=current_contract.contract_path.as_posix(),
            currentRecordDigest=record_digest,
            recordPath=_task_relative(current_contract, record_path),
            reportPath=_task_relative(current_contract, report_path),
            reportSha256=record.reportSha256,
        )
        atomic_write_text(
            paths.canonical,
            _json_bytes(authority.model_dump(mode="json")).decode("utf-8"),
        )
    validated = require_current_curator_coherence(contract)
    payload = _validated_payload(request, contract, validated, state="published")
    if snapshot_path is not None:
        payload["snapshotPath"] = snapshot_path.as_posix()
    return payload


def _record(
    contract: WorktreeContract,
    request: CuratorCoherenceRequest,
    observation: CuratorCoherenceObservation,
    publication: _RecordPublication,
) -> tuple[CuratorCoherenceRecord, str]:
    assert request.semantic_requirement_revision is not None
    assert request.delivery_attempt is not None
    assert request.caller is not None
    provisional = CuratorCoherenceRecord(
        leafId=contract.leaf_id,
        contractPath=contract.contract_path.as_posix(),
        taskDocumentRef=observation.candidate.ref,
        semanticRequirementRevision=request.semantic_requirement_revision.strip(),
        deliveryAttempt=request.delivery_attempt.strip(),
        pairIdentity=observation.pair_identity,
        codeCandidateTree=observation.code_candidate_tree,
        memoryCandidateTree=observation.memory_candidate_tree,
        taskTopologyFingerprint=observation.task_topology_fingerprint,
        taskIntent=observation.task_intent,
        attestationPath=observation.attestation_path.resolve().as_posix(),
        attestationSha256=observation.attestation_sha256,
        attestationReportSha256=observation.attestation.reportSha256,
        sourceCandidates=observation.source_candidates,
        judgments=publication.judgments,
        predecessorAuthorityDigest=publication.predecessor,
        publicationFingerprint=publication.fingerprint,
        publishedBy=f"{request.caller.role}@{request.caller.task_document_ref.key}",
        reportSha256="0" * 64,
    )
    report = render_curator_coherence(provisional)
    record = provisional.model_copy(update={"reportSha256": _digest(report.encode("utf-8"))})
    rendered = render_curator_coherence(record)
    if rendered != report:
        raise RuntimeError("curator-coherence projection unexpectedly depends on its own digest")
    return record, rendered


def _authorize_publisher(
    contract: WorktreeContract,
    observation: CuratorCoherenceObservation,
    request: CuratorCoherenceRequest,
) -> None:
    caller = request.caller
    assert caller is not None
    topology = TaskDocumentTopology(contract.coordination_root)
    try:
        master_ref = topology.parent(observation.candidate.ref)
        sprint_ref = topology.parent(master_ref) if master_ref is not None else None
    except TaskDocumentRefError as exc:
        raise CuratorCoherenceError(exc.status, str(exc), next_action="task_doc") from exc
    allowed = (
        caller.role == "curator" and caller.task_document_ref == observation.candidate.ref
    ) or (
        caller.role == "architect"
        and sprint_ref is not None
        and caller.task_document_ref == sprint_ref
    )
    if not allowed:
        raise CuratorCoherenceError(
            "curator-coherence-caller-refused",
            "publish requires the exact leaf curator or owning sprint architect",
            next_action="developer-decision",
        )


def _require_expected_observation(
    request: CuratorCoherenceRequest, observation: CuratorCoherenceObservation
) -> None:
    expected = {
        "codeCandidateTree": request.expected_code_candidate_tree,
        "memoryCandidateTree": request.expected_memory_candidate_tree,
        "taskTopologyFingerprint": request.expected_task_topology_fingerprint,
        "taskIntent": (
            request.expected_task_intent.model_dump(mode="json", by_alias=True)
            if request.expected_task_intent is not None
            else None
        ),
        "attestationSha256": request.expected_attestation_sha256,
    }
    observed = {
        "codeCandidateTree": observation.code_candidate_tree,
        "memoryCandidateTree": observation.memory_candidate_tree,
        "taskTopologyFingerprint": observation.task_topology_fingerprint,
        "taskIntent": observation.task_intent.model_dump(mode="json", by_alias=True),
        "attestationSha256": observation.attestation_sha256,
    }
    if expected != observed:
        raise CuratorCoherenceError(
            "curator-coherence-prepared-source-stale",
            "a prepared code, memory, task, or attestation identity changed before publish",
            expected=expected,
            observed=observed,
            next_action="prepare",
        )


def _require_observation_unchanged(
    expected: CuratorCoherenceObservation,
    observed: CuratorCoherenceObservation,
) -> None:
    expected_identity = _observation_identity(expected)
    observed_identity = _observation_identity(observed)
    if expected_identity != observed_identity:
        raise CuratorCoherenceError(
            "curator-coherence-source-raced",
            "code, memory, task topology, or attestation changed during publication",
            expected=expected_identity,
            observed=observed_identity,
            next_action="prepare",
        )


def _observation_identity(observation: CuratorCoherenceObservation) -> dict[str, object]:
    return {
        "pairIdentity": observation.pair_identity.model_dump(mode="json"),
        "codeCandidateTree": observation.code_candidate_tree,
        "memoryCandidateTree": observation.memory_candidate_tree,
        "taskTopologyFingerprint": observation.task_topology_fingerprint,
        "taskIntent": observation.task_intent.model_dump(mode="json", by_alias=True),
        "attestationSha256": observation.attestation_sha256,
        "sourceCandidates": [candidate.identity for candidate in observation.source_candidates],
    }


def _publication_fingerprint(
    request: CuratorCoherenceRequest,
    observation: CuratorCoherenceObservation,
    judgments: list[CuratorCoherenceRecordedJudgment],
) -> str:
    assert request.caller is not None
    return _digest(
        _json_bytes(
            {
                "schema": "ar-curator-coherence-publication/v1",
                "contractPath": request.contract_path,
                "semanticRequirementRevision": request.semantic_requirement_revision,
                "deliveryAttempt": request.delivery_attempt,
                "predecessorAuthorityDigest": request.expected_predecessor_digest,
                "source": _observation_identity(observation),
                "judgments": [judgment.model_dump(mode="json") for judgment in judgments],
                "caller": request.caller.model_dump(mode="json"),
                "freezeSnapshot": request.freeze_snapshot,
            }
        )
    )


def _idempotent_replay(contract: WorktreeContract, fingerprint: str):
    paths = curator_coherence_paths(contract)
    if not paths.canonical.exists():
        return None
    try:
        current = load_curator_coherence_authority(contract)
    except CuratorCoherenceError:
        return None
    if current.record.publicationFingerprint != fingerprint:
        return None
    return require_current_curator_coherence(contract)


def _require_predecessor(contract: WorktreeContract, request: CuratorCoherenceRequest) -> None:
    observed = current_curator_coherence_predecessor(contract)
    if observed != request.expected_predecessor_digest:
        raise CuratorCoherenceError(
            "curator-coherence-predecessor-stale",
            "the live canonical predecessor changed during publication",
            expected={"predecessorAuthorityDigest": request.expected_predecessor_digest or ""},
            observed={"predecessorAuthorityDigest": observed},
            next_action="prepare",
        )


def _publish_generation(
    record_path: Path,
    report_path: Path,
    record_bytes: bytes,
    report_bytes: bytes,
) -> None:
    destination = record_path.parent
    if destination.exists():
        try:
            matches = (
                record_path.read_bytes() == record_bytes
                and report_path.read_bytes() == report_bytes
            )
        except OSError as exc:
            raise CuratorCoherenceError(
                "curator-coherence-generation-incomplete",
                "an immutable coherence generation directory is incomplete or unreadable",
                next_action="developer-decision",
            ) from exc
        if not matches:
            raise CuratorCoherenceError(
                "curator-coherence-content-address-collision",
                "an immutable coherence generation path contains different bytes",
                next_action="developer-decision",
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        temporary.mkdir()
        _write_fsynced(temporary / "record.json", record_bytes)
        _write_fsynced(temporary / "report.md", report_bytes)
        atomic_replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _publish_snapshot(
    contract: WorktreeContract,
    record: CuratorCoherenceRecord,
    record_digest: str,
    record_path: Path,
    report_path: Path,
) -> Path:
    paths = curator_coherence_paths(contract)
    attempt = re.sub(r"[^A-Za-z0-9._-]+", "-", record.deliveryAttempt).strip("-")
    if not attempt:
        raise CuratorCoherenceError(
            "curator-coherence-attempt-invalid",
            "delivery attempt cannot form an immutable snapshot identity",
            next_action="publish",
        )
    path = paths.snapshots / f"{attempt}-{record_digest}.json"
    snapshot = CuratorCoherenceSnapshot(
        semanticRequirementRevision=record.semanticRequirementRevision,
        deliveryAttempt=record.deliveryAttempt,
        recordDigest=record_digest,
        recordPath=_task_relative(contract, record_path),
        reportPath=_task_relative(contract, report_path),
    )
    content = _json_bytes(snapshot.model_dump(mode="json")).decode("utf-8")
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise CuratorCoherenceError(
                "curator-coherence-snapshot-conflict",
                "an immutable attempt snapshot path contains different bytes",
                next_action="developer-decision",
            )
        return path
    atomic_write_text(path, content)
    return path


def _observation_payload(
    request: CuratorCoherenceRequest,
    contract: WorktreeContract,
    observation: CuratorCoherenceObservation,
    view: _ObservationView,
) -> dict[str, object]:
    paths = curator_coherence_paths(contract)
    return _success(
        request,
        contract,
        state=view.state,
        summary=view.summary,
        canonicalPath=paths.canonical.as_posix(),
        pairIdentity=observation.pair_identity.model_dump(mode="json"),
        codeCandidateTree=observation.code_candidate_tree,
        memoryCandidateTree=observation.memory_candidate_tree,
        taskTopologyFingerprint=observation.task_topology_fingerprint,
        taskIntent=observation.task_intent.model_dump(mode="json", by_alias=True),
        **(
            {"currentnessStatus": view.currentness_status}
            if view.currentness_status is not None
            else {}
        ),
        **({"nextAction": view.next_action} if view.next_action is not None else {}),
        attestationPath=observation.attestation_path.resolve().as_posix(),
        attestationSha256=observation.attestation_sha256,
        attestationReportSha256=observation.attestation.reportSha256,
        predecessorAuthorityDigest=view.predecessor,
        candidateCount=len(observation.source_candidates),
        candidates=[
            candidate.model_dump(mode="json") for candidate in observation.source_candidates
        ],
    )


def _validated_payload(request, contract, validated, *, state: str) -> dict[str, object]:
    record = validated.record
    return _success(
        request,
        contract,
        state=state,
        summary="The sole live structured curator-coherence authority is exact and current.",
        canonicalPath=curator_coherence_paths(contract).canonical.as_posix(),
        recordPath=validated.record_path.as_posix(),
        reportPath=validated.report_path.as_posix(),
        semanticRequirementRevision=record.semanticRequirementRevision,
        deliveryAttempt=record.deliveryAttempt,
        pairIdentity=record.pairIdentity.model_dump(mode="json"),
        codeCandidateTree=record.codeCandidateTree,
        memoryCandidateTree=record.memoryCandidateTree,
        taskTopologyFingerprint=record.taskTopologyFingerprint,
        taskIntent=record.taskIntent.model_dump(mode="json", by_alias=True),
        attestationPath=record.attestationPath,
        attestationSha256=record.attestationSha256,
        attestationReportSha256=record.attestationReportSha256,
        predecessorAuthorityDigest=record.predecessorAuthorityDigest,
        recordDigest=validated.record_digest,
        reportDigest=record.reportSha256,
        candidateCount=len(record.sourceCandidates),
        candidates=[candidate.model_dump(mode="json") for candidate in record.sourceCandidates],
        validationResult={
            "state": "valid",
            "candidateCount": len(record.sourceCandidates),
            "checked": [
                "code-candidate",
                "memory-candidate",
                "code-memory-pair",
                "task-topology",
                "task-intent",
                "attestation",
                "candidate-judgments",
                "authority-record",
                "generated-projection",
            ],
        },
    )


def _success(
    request: CuratorCoherenceRequest,
    contract: WorktreeContract,
    *,
    state: str,
    summary: str,
    **extra: object,
) -> dict[str, object]:
    return {
        "ok": True,
        "operation": "curator_coherence",
        "action": request.action,
        "state": state,
        "summary": summary,
        "contractPath": contract.contract_path.as_posix(),
        **extra,
    }


def _task_relative(contract: WorktreeContract, path: Path) -> str:
    return path.resolve(strict=False).relative_to(contract.task_root.resolve()).as_posix()


def _write_fsynced(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = ["curator_coherence_action"]
