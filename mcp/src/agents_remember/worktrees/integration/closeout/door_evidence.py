"""Exact repository and ledger evidence owned by a closeout-door generation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agents_remember.kernel.memory_ledger import find_mapping, load_ledger
from agents_remember.models.closeout.source import EvidenceFact
from agents_remember.models.lifecycles.door import (
    CloseoutDoorGeneration,
    DoorEvidenceFact,
    DoorProvenance,
)
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.worktrees.modules.git import branch_commit, worktree_candidate_tree
from agents_remember.worktrees.queue.closeout_queue_errors import (
    CloseoutQueueError,
    bounded_queue_failure_detail,
)
from agents_remember.worktrees.queue.closeout_queue_evidence import curator_evidence
from agents_remember.worktrees.route_review import (
    RouteReviewError,
    code_candidate_tree,
    code_change_present,
    require_current_route_review_task_intent,
)
from agents_remember.worktrees.source_lineage import require_current_source_lineage
from agents_remember.worktrees.worktree_contract import WorktreeContract


@dataclass(frozen=True)
class DoorCandidateEvidence:
    """Current candidate facts compared with one immutable door generation."""

    candidate_tree: str
    memory_candidate_tree: str
    ledger_memory_commit: str
    review: DoorProvenance
    memory: DoorProvenance
    ledger: DoorProvenance

    def fingerprint_fact(self, contract: WorktreeContract) -> dict[str, object]:
        return {
            "candidateTree": self.candidate_tree,
            "memoryCandidateTree": self.memory_candidate_tree,
            "codeBaseCommit": contract.code_base_commit,
            "memoryBaseCommit": contract.memory_base_commit,
            "ledgerMemoryCommit": self.ledger_memory_commit,
            "review": self.review.model_dump(mode="json"),
            "memory": self.memory.model_dump(mode="json"),
            "ledger": self.ledger.model_dump(mode="json"),
        }


def require_source_bases_current(contract: WorktreeContract) -> None:
    """Require transitive ancestry and the exact immediate source heads."""

    try:
        require_current_source_lineage(contract, operation="closeout door publication")
    except RuntimeError as exc:
        raise CloseoutQueueError(
            "closeout-door-source-lineage-stale",
            bounded_queue_failure_detail(
                exc,
                stage="door-source-lineage",
                side="contract",
                name="source-lineage",
            ),
        ) from exc
    if (
        branch_commit(contract.code_repo_path, contract.code_source_branch)
        != contract.code_base_commit
    ):
        raise CloseoutQueueError(
            "closeout-door-code-source-moved",
            "code source moved after leaf start; run worktree_sync, then retry",
        )
    if contract.memory_mode != "external":
        return
    if contract.memory_repo_path is None or not contract.memory_base_commit:
        raise CloseoutQueueError(
            "closeout-door-memory-source-missing",
            "external memory base is incomplete",
        )
    if (
        branch_commit(contract.memory_repo_path, contract.memory_source_branch)
        != contract.memory_base_commit
    ):
        raise CloseoutQueueError(
            "closeout-door-memory-source-moved",
            "memory source moved after leaf start; run worktree_sync, then retry",
        )


def ledger_mapping(contract: WorktreeContract) -> str | None:
    """Return the exact source-code to source-memory ledger edge when applicable."""

    if contract.memory_mode != "external":
        return None
    if contract.ledger_path is None:
        raise CloseoutQueueError(
            "closeout-door-ledger-missing",
            "external-memory contract has no ledger path",
        )
    row = find_mapping(load_ledger(contract.ledger_path), contract.code_base_commit)
    if row is None:
        raise CloseoutQueueError(
            "closeout-door-ledger-incompatible",
            f"ledger does not map code base {contract.code_base_commit}",
        )
    return row.memory_commit


def memory_candidate_tree(contract: WorktreeContract) -> str | None:
    """Hash the exact external-memory worktree candidate, or mark it inapplicable."""

    if contract.memory_worktree is None:
        return None
    return worktree_candidate_tree(
        contract.memory_worktree,
        contract.worktree_group / "reports" / ".closeout-door-memory.index",
    )


def capture_door_candidate_evidence(
    contract: WorktreeContract,
    candidate: ResolvedTaskDocument,
) -> DoorCandidateEvidence:
    """Read the single canonical evidence snapshot used by declaration and projection."""

    require_source_bases_current(contract)
    try:
        candidate_tree = code_candidate_tree(contract)
        memory_tree = memory_candidate_tree(contract) or ""
        mapping = ledger_mapping(contract) or ""
        review = _review_provenance(contract, candidate, candidate_tree)
        memory = _provenance(
            curator_evidence(contract) if contract.memory_mode == "external" else [],
            applicable=contract.memory_mode == "external",
        )
        ledger = _ledger_provenance(contract, mapping)
    except CloseoutQueueError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise CloseoutQueueError(
            "closeout-door-candidate-evidence-unreadable",
            bounded_queue_failure_detail(
                exc,
                stage="door-candidate-evidence",
                side="canonical-source",
                name="candidate-evidence",
            ),
        ) from exc
    return DoorCandidateEvidence(
        candidate_tree=candidate_tree,
        memory_candidate_tree=memory_tree,
        ledger_memory_commit=mapping,
        review=review,
        memory=memory,
        ledger=ledger,
    )


def door_candidate_evidence_blockers(
    contract: WorktreeContract,
    door: CloseoutDoorGeneration,
    current: DoorCandidateEvidence,
) -> list[str]:
    """Name each current candidate fact that no longer matches the declared source."""

    blockers: list[str] = []
    if door.candidateTree != current.candidate_tree:
        blockers.append("door-code-candidate-stale")
    if door.codeBaseCommit != contract.code_base_commit:
        blockers.append("door-code-base-stale")
    if door.memoryCandidateTree != current.memory_candidate_tree:
        blockers.append("door-memory-candidate-stale")
    if door.memoryBaseCommit != contract.memory_base_commit:
        blockers.append("door-memory-base-stale")
    if door.ledgerMemoryCommit != current.ledger_memory_commit:
        blockers.append("door-ledger-mapping-stale")
    if door.reviewProvenance != current.review:
        blockers.append("door-review-provenance-stale")
    if door.memoryProvenance != current.memory:
        blockers.append("door-memory-provenance-stale")
    if door.ledgerProvenance != current.ledger:
        blockers.append("door-ledger-provenance-stale")
    return blockers


def _review_provenance(
    contract: WorktreeContract,
    candidate: ResolvedTaskDocument,
    candidate_tree: str,
) -> DoorProvenance:
    try:
        changed = code_change_present(contract)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CloseoutQueueError(
            "closeout-door-code-candidate-unreadable",
            "cannot compare the exact code candidate with its recorded base",
        ) from exc
    if not changed:
        return DoorProvenance(
            state="not-applicable",
            fingerprint=_fingerprint(
                {
                    "state": "not-applicable",
                    "reason": "no-code-change",
                    "candidateTree": candidate_tree,
                    "codeBaseCommit": contract.code_base_commit,
                }
            ),
        )
    if candidate.document.routeReview is None:
        raise CloseoutQueueError(
            "closeout-door-route-review-required",
            "the changed code candidate has no canonical route-review record",
        )
    review = candidate.document.routeReview
    if review.verdict == "block":
        raise CloseoutQueueError(
            "closeout-door-route-review-blocked",
            f"independent route review blocks this candidate; see {review.verdictRef}",
        )
    if review.candidateTree != candidate_tree:
        raise CloseoutQueueError(
            "closeout-door-route-review-stale",
            "the canonical route-review record does not match the current candidate tree",
        )
    try:
        require_current_route_review_task_intent(contract, candidate)
    except RouteReviewError as exc:
        raise CloseoutQueueError(exc.status, str(exc)) from exc
    refs = {review.verdictRef, *(route.evidenceRef for route in review.routes)}
    if len(refs) > 256:
        raise CloseoutQueueError(
            "closeout-door-route-review-too-large",
            "route-review provenance exceeds the bounded door evidence collection",
        )
    evidence = [_door_task_evidence(contract.task_root, ref) for ref in sorted(refs)]
    return DoorProvenance(
        state="proven",
        fingerprint=_fingerprint(
            {
                "record": review.model_dump(mode="json"),
                "evidence": [fact.model_dump(mode="json") for fact in evidence],
            }
        ),
        evidence=evidence,
    )


def _door_task_evidence(task_root: Path, ref: str) -> DoorEvidenceFact:
    supplied = Path(ref)
    root = task_root.resolve()
    resolved = (root / supplied).resolve(strict=False)
    if supplied.is_absolute() or not resolved.is_relative_to(root) or not resolved.is_file():
        raise CloseoutQueueError(
            "closeout-door-route-evidence-invalid",
            f"route-review evidence is not an exact task-relative file: {ref}",
        )
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise CloseoutQueueError(
            "closeout-door-route-evidence-unreadable",
            f"route-review evidence cannot be read: {ref}",
        ) from exc
    return DoorEvidenceFact(path=supplied.as_posix(), sha256=hashlib.sha256(payload).hexdigest())


def _provenance(facts: list[EvidenceFact], *, applicable: bool) -> DoorProvenance:
    if not applicable:
        return DoorProvenance(
            state="not-applicable",
            fingerprint=_fingerprint({"state": "not-applicable"}),
        )
    evidence = [DoorEvidenceFact(path=fact.path, sha256=fact.sha256) for fact in facts]
    return DoorProvenance(
        state="proven",
        fingerprint=_fingerprint([fact.model_dump(mode="json") for fact in evidence]),
        evidence=evidence,
    )


def _ledger_provenance(contract: WorktreeContract, mapping: str) -> DoorProvenance:
    if contract.memory_mode != "external":
        return _provenance([], applicable=False)
    if contract.ledger_path is None:
        raise CloseoutQueueError(
            "closeout-door-ledger-missing",
            "external-memory door requires the exact ledger",
        )
    payload = contract.ledger_path.read_bytes()
    fact = DoorEvidenceFact(
        path=contract.ledger_path.resolve().as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    return DoorProvenance(
        state="proven",
        fingerprint=_fingerprint({"mapping": mapping, "evidence": fact.model_dump(mode="json")}),
        evidence=[fact],
    )


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "DoorCandidateEvidence",
    "capture_door_candidate_evidence",
    "door_candidate_evidence_blockers",
    "ledger_mapping",
    "memory_candidate_tree",
    "require_source_bases_current",
]
