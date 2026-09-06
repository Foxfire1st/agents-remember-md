"""Invoke the real memory producer against a proved in-flight code/memory pair."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.prepared_memory import (
    PreparedCodeExecutionView,
    PreparedMemoryCandidate,
)
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
    current_certification_handoff,
)
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.modules.git import worktree_candidate_tree
from agents_remember.worktrees.services import worktree_services

from .code_view import observe_prepared_code_view, prepare_code_view
from .memory_port import PreparedMemoryCertificationRequest, PreparedMemoryCertificationResult


def observe_prepared_memory_candidate(
    handoff: CloseoutCertificationHandoff, view: PreparedCodeExecutionView
) -> PreparedMemoryCandidate:
    """Bind actual current memory bytes to the selected physical code view."""
    current = current_certification_handoff(handoff.contract, handoff.record, handoff.store)
    if observe_prepared_code_view(current) != view:
        refuse("prepared-memory-code-view-moved", view.viewDigest, "changed")
    with TemporaryDirectory(
        prefix="prepared-memory-tree-", dir=current.contract.worktree_group
    ) as directory:
        memory_tree = worktree_candidate_tree(
            Path(view.logicalPair.memoryRoot), Path(directory) / "index"
        )
    payload = {
        "schemaVersion": "prepared-memory-candidate/v1",
        "codeView": view.model_dump(mode="json"),
        "memoryTree": memory_tree,
    }
    current = current_certification_handoff(current.contract, current.record, current.store)
    if observe_prepared_code_view(current) != view:
        refuse("prepared-memory-code-view-moved", view.viewDigest, "changed")
    return PreparedMemoryCandidate.model_validate(
        {**payload, "candidateDigest": canonical_sha256(payload)}
    )


def certify_prepared_memory(
    handoff: CloseoutCertificationHandoff,
) -> PreparedMemoryCertificationResult:
    """No installed producer means a typed refusal before any protected publication.

    The L35/L36 producer owns actual impact planning, stamping, catalogs and
    coherence. Its response must identify the originals it selected in this same
    journal; the caller never promotes a returned dictionary to a certificate.
    """
    producer = worktree_services().prepared_memory_certification
    if producer is None:
        refuse(
            "prepared-memory-certification-unavailable",
            "registered production memory producer",
            None,
        )
    handoff, view = prepare_code_view(handoff)
    candidate = observe_prepared_memory_candidate(handoff, view)
    result = producer.certify(PreparedMemoryCertificationRequest(handoff, candidate))
    return current_prepared_memory_result(result)


def current_prepared_memory_result(
    result: PreparedMemoryCertificationResult,
) -> PreparedMemoryCertificationResult:
    """Reopen the selected producer originals without running certification again."""
    handoff = result.handoff
    view = result.candidate.codeView
    current = current_certification_handoff(handoff.contract, handoff.record, handoff.store)
    actual = observe_prepared_memory_candidate(current, view)
    if result.candidate != actual or (
        result.handoff.record.operationKey,
        result.handoff.record.generation,
        result.handoff.record.workerLease,
        result.handoff.record.workerPid,
    ) != (
        current.record.operationKey,
        current.record.generation,
        current.record.workerLease,
        current.record.workerPid,
    ):
        refuse(
            "prepared-memory-producer-owner-mismatch",
            actual.candidateDigest,
            result.candidate.candidateDigest,
        )
    terminals = current.selected.terminals
    if len(terminals) != 5:
        refuse(
            "prepared-memory-certificate-unselected",
            "actual selected fifth terminal",
            len(terminals),
        )
    terminal = terminals[-1]
    certificate = terminal.certificate
    if certificate is None or (
        terminal.resultReference != result.resultManifest
        or terminal.certificateReference != result.certificate
        or certificate.semanticEnvelope.gateFiveInputs != result.memoryInputs
        or result.memoryInputs.memoryTree.value != actual.memoryTree
        or result.memoryInputs.candidatePairAuthorityDigest != view.logicalPair.contractDigest
    ):
        refuse(
            "prepared-memory-certificate-mismatch",
            "exact realized current pair and selected originals",
            result.certificate,
        )
    return replace(result, handoff=current, candidate=actual)
