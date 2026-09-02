"""Structured curator-coherence evidence for lifecycle fixtures.

The helper deliberately drives the same prepare/publish API as production. It
authors only the upstream memory-quality attestation and agent judgment inputs;
the canonical authority and Markdown projection remain lifecycle-owned output.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.curator_coherence import (
    CuratorCoherenceJudgment,
    CuratorCoherenceRequest,
    CuratorSourceCandidate,
    memory_quality_attestation_dependencies,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.worktrees.integration.closeout.curator_coherence_publication import (
    curator_coherence_action,
)
from agents_remember.worktrees.integration.closeout.future_code_candidate import (
    capture_future_code_candidate,
)
from agents_remember.worktrees.integration.closeout.memory_candidate_pair import (
    resolve_memory_candidate_pair,
)
from agents_remember.worktrees.modules.git import worktree_candidate_tree
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract


def write_curator_task_topology(contract: WorktreeContract) -> TaskDocumentRef:
    """Create the complete leaf/master/sprint lineage required by the validator."""

    master_slug = contract.task_root.name
    canonical_series = contract.task_root / "series-contract.md"
    atomic_series = (
        load_contract(canonical_series)
        if contract.parent_contract_path == canonical_series and canonical_series.is_file()
        else None
    )
    execution_nature = "atomic" if atomic_series is not None else "organizational"
    sprint_branch = (
        atomic_series.code_source_branch
        if atomic_series is not None
        else contract.code_source_branch
    )
    master_ref = TaskDocumentRef(
        repository=contract.repo_name,
        path=f"{master_slug}/task.json",
    )
    write_task_doc(
        contract.task_root,
        TaskDocument.model_validate(
            {
                "id": f"{contract.leaf_id}-MASTER",
                "slug": "task",
                "title": f"{contract.task_name} master",
                "kind": "master",
                "status": "inProgress",
                "repo": contract.repo_name,
                "createdAt": "2026-08-13T00:00:00+00:00",
                "executionNature": execution_nature,
                "subTasks": [
                    {
                        "number": contract.leaf_id,
                        "name": contract.task_name,
                        "file": f"{contract.leaf_id}.md",
                        "status": "inProgress",
                    }
                ],
            }
        ),
    )
    sprint_ref = TaskDocumentRef(repository=contract.repo_name, path="sprint/task.json")
    write_task_doc(
        contract.coordination_root / "tasks" / contract.repo_name / "sprint",
        TaskDocument.model_validate(
            {
                "id": "FIXTURE-SPRINT",
                "slug": "task",
                "title": "Fixture sprint",
                "kind": "master",
                "status": "inProgress",
                "repo": contract.repo_name,
                "createdAt": "2026-08-13T00:00:00+00:00",
                "orchestrates": [master_slug],
                "integrationBranch": sprint_branch,
                "executionGraph": {
                    "nodes": [master_ref.model_dump(mode="json")],
                    "edges": [],
                },
            }
        ),
    )
    return sprint_ref


def write_curator_evidence(
    contract: WorktreeContract,
    *,
    caller_ref: TaskDocumentRef,
    report_text: str | None = None,
    source_candidates: list[dict[str, str]] | None = None,
) -> None:
    """Publish exact structured fixture evidence for the current candidate.

    When a task-only mutation invalidates an existing fixture authority, omitted
    report and candidate inputs are retained from the current attestation. That
    makes the refresh explicit without fabricating a different semantic review.
    """

    report = contract.worktree_group / "reports" / "curator-memory-quality.md"
    attestation_path = report.with_suffix(".json")
    previous = _previous_attestation(attestation_path)
    text = report_text or _previous_report(report) or _curator_report()
    candidates = (
        source_candidates if source_candidates is not None else _previous_candidates(previous)
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(text, encoding="utf-8")
    assert contract.memory_worktree is not None
    (contract.memory_worktree / "onboarding").mkdir(parents=True, exist_ok=True)
    pair = resolve_memory_candidate_pair(
        contract,
        requested_contract_path=contract.contract_path,
        requested_repo_id=contract.repo_name,
    )
    assert contract.memory_worktree is not None
    code_tree = capture_future_code_candidate(contract).codeCandidateTree
    with TemporaryDirectory(prefix=".fixture-curator-memory-") as temporary:
        memory_tree = worktree_candidate_tree(
            contract.memory_worktree,
            Path(temporary) / "index",
        )
    report_sha256 = hashlib.sha256(text.encode()).hexdigest()
    attestation = {
        "schema": "ar-curator-memory-quality/v1",
        "checklistStatus": "ready-for-closeout",
        "curatorActionableCount": 0,
        "memoryRepairCount": 0,
        "missingOnboardingCount": 0,
        "staleRouteIndexCount": 0,
        "sourceChangeCandidateCount": len(candidates),
        "sourceChangeCandidates": candidates,
        "pairIdentity": pair.model_dump(mode="json"),
        "onboardingRoot": (contract.memory_worktree / "onboarding").resolve().as_posix(),
        "reportPath": report.resolve().as_posix(),
        "reportSha256": report_sha256,
        "dependencies": memory_quality_attestation_dependencies(
            pair_identity=pair,
            code_candidate_tree=code_tree,
            memory_candidate_tree=memory_tree,
            report_sha256=report_sha256,
        ).model_dump(mode="json"),
    }
    attestation_path.write_text(
        json.dumps(attestation, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    evidence = (
        contract.task_root / "notes" / "reports" / f"{contract.leaf_id}-fixture-curator-evidence.md"
    )
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("# Fixture curator evidence\n", encoding="utf-8")
    prepared = curator_coherence_action(
        contract,
        CuratorCoherenceRequest(action="prepare", contract_path=contract.contract_path.as_posix()),
    )
    judgments = [
        CuratorCoherenceJudgment(
            **CuratorSourceCandidate.model_validate(candidate).model_dump(mode="json"),
            disposition="reconciled",
            rationale="The fixture candidate has one explicit reconciliation judgment.",
            evidenceRef=f"task:{evidence.relative_to(contract.task_root).as_posix()}",
        )
        for candidate in candidates
    ]
    published = curator_coherence_action(
        contract,
        CuratorCoherenceRequest(
            action="publish",
            contract_path=contract.contract_path.as_posix(),
            semantic_requirement_revision="FIXTURE-R01@v1",
            delivery_attempt="A001",
            judgments=judgments,
            expected_predecessor_digest=str(prepared["predecessorAuthorityDigest"]),
            expected_code_candidate_tree=str(prepared["codeCandidateTree"]),
            expected_memory_candidate_tree=str(prepared["memoryCandidateTree"]),
            expected_task_topology_fingerprint=str(prepared["taskTopologyFingerprint"]),
            expected_task_intent=TaskIntentIdentity.model_validate(prepared["taskIntent"]),
            expected_attestation_sha256=str(prepared["attestationSha256"]),
            caller=DeclaredCaller(role="architect", task_document_ref=caller_ref),
        ),
    )
    assert published["state"] in {"published", "already-current"}


def _previous_attestation(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _previous_candidates(attestation: dict[str, object]) -> list[dict[str, str]]:
    value = attestation.get("sourceChangeCandidates", [])
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _previous_report(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _curator_report() -> str:
    return """# Curator Memory Quality Checklist

- Status: **ready-for-closeout**

## Summary

| Class | Count | Gate meaning |
| --- | ---: | --- |
| Repairable memory findings | 0 | Must reach zero |
| Missing onboarding | 0 | Must reach zero |
| Stale route indexes | 0 | Must reach zero |
"""
