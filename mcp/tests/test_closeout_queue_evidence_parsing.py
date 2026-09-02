"""Focused proof that closeout consumes structured coherence, never Markdown tables."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents_remember.errors import CuratorCoherenceError
from agents_remember.models.closeout.source import EvidenceFact
from agents_remember.models.lifecycles.curator_coherence import (
    CuratorCoherenceRecord,
    CuratorCoherenceRecordedJudgment,
    CuratorQualityAttestation,
    CuratorSourceCandidate,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.integration.closeout import curator_coherence
from agents_remember.worktrees.integration.closeout.curator_coherence_render import (
    render_curator_coherence,
)
from agents_remember.worktrees.queue import closeout_queue_evidence as evidence
from agents_remember.worktrees.queue.closeout_queue_errors import CloseoutQueueError
from pydantic import ValidationError


def _contract(**fields: object) -> Any:
    return cast(Any, SimpleNamespace(**fields))


def _candidate() -> CuratorSourceCandidate:
    return CuratorSourceCandidate(
        sourceFile="mcp/a.py",
        onboardingFile="mcp/a.py.md",
        classification="drifted",
    )


def _judgment() -> CuratorCoherenceRecordedJudgment:
    return CuratorCoherenceRecordedJudgment(
        **_candidate().model_dump(mode="json"),
        disposition="reconciled",
        rationale="The sidecar now explains the changed owner.",
        evidenceRef="memory:onboarding/mcp/a.py.md",
        evidenceSha256="9" * 64,
    )


def _pair() -> MemoryCandidatePairIdentity:
    return MemoryCandidatePairIdentity(
        repoId="repo",
        contractPath="/coordination/tasks/repo/task/enclosures/l1/series-contract.md",
        contractDigest="1" * 64,
        codeRoot="/group/code",
        memoryRoot="/group/memory",
        codeSourceBranch="master",
        codeWorkBranch="leaf",
        codeBaseCommit="2" * 40,
        memorySourceBranch="master",
        memoryWorkBranch="leaf",
        memoryBaseCommit="3" * 40,
        onboardingRoot="/group/memory/onboarding",
        ledgerPath="/group/memory/memory.md",
    )


def _record() -> CuratorCoherenceRecord:
    return CuratorCoherenceRecord(
        leafId="L1",
        contractPath="/coordination/tasks/repo/task/enclosures/l1/series-contract.md",
        taskDocumentRef=TaskDocumentRef(repository="repo", path="task/01_leaf.json"),
        semanticRequirementRevision="R1@v1",
        deliveryAttempt="A003",
        pairIdentity=_pair(),
        codeCandidateTree="a" * 40,
        memoryCandidateTree="b" * 40,
        taskTopologyFingerprint="c" * 64,
        taskIntent=TaskIntentIdentity(digest="9" * 64),
        attestationPath="/group/reports/curator-memory-quality.json",
        attestationSha256="d" * 64,
        attestationReportSha256="e" * 64,
        sourceCandidates=[_candidate()],
        judgments=[_judgment()],
        predecessorAuthorityDigest="",
        publicationFingerprint="f" * 64,
        publishedBy="architect@repo/sprint/task.json",
        reportSha256="0" * 64,
    )


def test_structured_attestation_and_record_require_exact_unique_identity_sets() -> None:
    attestation = {
        "schema": "ar-curator-memory-quality/v1",
        "checklistStatus": "ready-for-closeout",
        "curatorActionableCount": 0,
        "memoryRepairCount": 0,
        "missingOnboardingCount": 0,
        "staleRouteIndexCount": 0,
        "sourceChangeCandidateCount": 1,
        "sourceChangeCandidates": [_candidate().model_dump(mode="json")],
        "pairIdentity": _pair().model_dump(mode="json"),
        "onboardingRoot": "/memory/onboarding",
        "reportPath": "/group/reports/curator-memory-quality.md",
        "reportSha256": "1" * 64,
    }
    assert CuratorQualityAttestation.model_validate(attestation).sourceChangeCandidateCount == 1
    with pytest.raises(ValidationError, match="must be unique"):
        CuratorQualityAttestation.model_validate(
            {
                **attestation,
                "sourceChangeCandidateCount": 2,
                "sourceChangeCandidates": [
                    _candidate().model_dump(mode="json"),
                    _candidate().model_dump(mode="json"),
                ],
            }
        )

    with pytest.raises(ValidationError, match="exactly cover"):
        CuratorCoherenceRecord.model_validate(
            {**_record().model_dump(mode="json"), "judgments": []}
        )


def test_markdown_is_a_deterministic_projection_not_a_machine_input() -> None:
    record = _record()
    report = render_curator_coherence(record)
    assert "Generated projection" in report
    assert "A003" in report
    assert "The sidecar now explains" in report
    assert "| mcp/a.py | mcp/a.py.md | drifted | reconciled |" in report
    with pytest.raises(json.JSONDecodeError):
        json.loads(report)


def test_closeout_queue_delegates_to_the_shared_coherence_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _contract()
    facts = [EvidenceFact(path="authority.json", sha256="a" * 64)]
    called: list[object] = []

    def validate(observed: object) -> list[EvidenceFact]:
        called.append(observed)
        return facts

    monkeypatch.setattr(curator_coherence, "curator_coherence_evidence", validate)
    assert evidence.curator_evidence(contract) == facts
    assert called == [contract]
    assert evidence.curator_evidence_blockers(contract, facts, required=True) == []

    def stale(_observed: object) -> list[EvidenceFact]:
        raise CuratorCoherenceError("curator-coherence-stale", "stale")

    monkeypatch.setattr(curator_coherence, "curator_coherence_evidence", stale)
    with pytest.raises(CloseoutQueueError, match="stale"):
        evidence.curator_evidence(contract)
    assert evidence.curator_evidence_blockers(contract, facts, required=True) == [
        "memory-readiness-evidence-stale"
    ]


def test_register_parsers_remain_separate_from_curator_authority() -> None:
    header = "| " + " | ".join(evidence.PRIORITY_REGISTER_HEADER) + " |"
    separator = "| --- | --- | --- | --- |"
    evidence._require_register_header([header, separator], evidence.PRIORITY_REGISTER_HEADER)
    evidence._require_register_separator(separator, 4)
    source, cells = evidence._register_row("| leaf | high | two | J-1 |", 4)
    assert source.startswith("| leaf")
    assert cells == ["leaf", "high", "two", "J-1"]

    invalid_calls = (
        lambda: evidence._require_register_header([], evidence.PRIORITY_REGISTER_HEADER),
        lambda: evidence._require_register_separator("--- | ---", 2),
        lambda: evidence._require_register_separator("| -- | --- |", 2),
        lambda: evidence._register_row("leaf | high |", 2),
        lambda: evidence._register_row("| leaf | high |", 3),
    )
    for call in invalid_calls:
        with pytest.raises(CloseoutQueueError):
            call()
