"""Task-bound independent route-review evidence for code closeout admission."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agents_remember.errors import TaskIntentError
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks import RouteReviewRecord
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.leaf_doc import resolve_terminal_leaf_doc
from agents_remember.tasks.task_intent import (
    require_current_task_intent,
    task_intent_identity,
)
from agents_remember.worktrees.modules.git import require_git, worktree_candidate_tree
from agents_remember.worktrees.worktree_contract import WorktreeContract


class RouteReviewError(ValueError):
    """The leaf lacks a passing review for its exact current candidate tree."""

    def __init__(self, status: str, detail: str) -> None:
        self.status = status
        super().__init__(detail)


def code_candidate_tree(contract: WorktreeContract) -> str:
    if contract.kind == "series":
        return require_git(
            contract.code_repo_path,
            ["rev-parse", f"refs/heads/{contract.code_work_branch}^{{tree}}"],
        )
    return worktree_candidate_tree(
        contract.code_worktree,
        contract.worktree_group / "reports" / ".route-review-candidate.index",
    )


def code_change_present(contract: WorktreeContract) -> bool:
    """Whether the full current candidate differs from the leaf's accepted base tree."""
    candidate = code_candidate_tree(contract)
    repository = contract.code_repo_path if contract.kind == "series" else contract.code_worktree
    base_tree = require_git(
        repository,
        ["rev-parse", f"{contract.code_base_commit}^{{tree}}"],
    )
    return candidate != base_tree


def build_route_review(
    contract: WorktreeContract,
    candidate: ResolvedTaskDocument,
    payload: dict[str, Any],
    *,
    branch_addressed: bool = False,
    now: datetime | None = None,
) -> RouteReviewRecord:
    """Validate reviewer-authored evidence and stamp the current tree/time in the plane."""
    expected = {"verdict", "verdictRef", "routes"}
    unknown = set(payload) - expected
    if unknown:
        raise RouteReviewError(
            "route-review-invalid",
            "record_route_review accepts only verdict, verdictRef, and routes; "
            f"the plane owns candidateTree and reviewedAt (unknown: {sorted(unknown)})",
        )
    if contract.kind != "leaf" and not (branch_addressed and contract.kind == "series"):
        raise RouteReviewError("route-review-invalid-altitude", "route review belongs to a leaf")
    try:
        intent = task_intent_identity(contract.task_root, candidate)
        record = RouteReviewRecord.model_validate(
            {
                **payload,
                "candidateTree": code_candidate_tree(contract),
                "reviewedAt": (now or datetime.now(UTC)).replace(microsecond=0).isoformat(),
                "taskIntent": intent.model_dump(mode="json", by_alias=True),
            }
        )
    except (TaskIntentError, ValidationError) as exc:
        if isinstance(exc, TaskIntentError):
            raise RouteReviewError(exc.status, exc.detail) from exc
        raise RouteReviewError("route-review-invalid", str(exc)) from exc
    _require_evidence_files(contract.task_root, record)
    return record


def require_current_route_review(contract: WorktreeContract) -> dict[str, object]:
    """Return current passing evidence or refuse before curator/closeout work begins."""
    if contract.kind != "leaf":
        return {"required": False, "status": "not-required-master-altitude"}
    if not code_change_present(contract):
        return {"required": False, "status": "not-required-no-code-change"}
    found = resolve_terminal_leaf_doc(contract.task_root, contract.leaf_id)
    if found is None:
        raise RouteReviewError(
            "route-review-task-document-missing",
            f"leaf {contract.leaf_id!r} has no task document for route-review evidence",
        )
    _path, document = found
    candidate = ResolvedTaskDocument(
        ref=document_ref(contract, _path),
        path=_path,
        document=document,
    )
    review = document.routeReview
    if review is None:
        raise RouteReviewError(
            "route-review-required",
            "the current code change has no independent route-review record",
        )
    if review.verdict == "block":
        raise RouteReviewError(
            "route-review-blocked",
            f"independent route review blocks this candidate; see {review.verdictRef}",
        )
    current = code_candidate_tree(contract)
    if review.candidateTree != current:
        raise RouteReviewError(
            "route-review-stale",
            "the code candidate changed after independent route review; rerun route review "
            f"(reviewed {review.candidateTree}, current {current})",
        )
    current_intent = require_current_route_review_task_intent(contract, candidate)
    _require_evidence_files(contract.task_root, review)
    return {
        "required": True,
        "status": "current",
        "candidateTree": current,
        "taskIntent": current_intent.model_dump(mode="json", by_alias=True),
        "verdict": review.verdict,
        "verdictRef": review.verdictRef,
        "routeCount": len(review.routes),
    }


def require_current_route_review_task_intent(
    contract: WorktreeContract,
    candidate: ResolvedTaskDocument,
) -> TaskIntentIdentity:
    """Require the candidate's review to bind its current canonical task intent."""

    review = candidate.document.routeReview
    if review is None:
        raise RouteReviewError(
            "route-review-required",
            "the current code change has no independent route-review record",
        )
    try:
        current_intent = task_intent_identity(contract.task_root, candidate)
        return require_current_task_intent(
            review.taskIntent,
            current_intent,
            owner="route-review",
            next_action="record_route_review",
        )
    except TaskIntentError as exc:
        raise RouteReviewError(exc.status, exc.detail) from exc


def document_ref(contract: WorktreeContract, path: Path) -> TaskDocumentRef:
    """Return one confined task reference without selecting identity from prose."""

    root = contract.coordination_root / "tasks" / contract.repo_name
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve()):
        raise RouteReviewError(
            "route-review-task-document-outside-root",
            "route-review task document is outside configured repository tasks",
        )
    return TaskDocumentRef(
        repository=contract.repo_name,
        path=resolved.relative_to(root.resolve()).as_posix(),
    )


def _require_evidence_files(task_root: Path, review: RouteReviewRecord) -> None:
    refs = {review.verdictRef, *(route.evidenceRef for route in review.routes)}
    root = task_root.resolve()
    for ref in sorted(refs):
        supplied = Path(ref)
        if supplied.is_absolute():
            raise RouteReviewError(
                "route-review-evidence-outside-task",
                f"route-review evidence must use a task-relative path: {ref}",
            )
        resolved = (root / supplied).resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise RouteReviewError(
                "route-review-evidence-outside-task",
                f"route-review evidence escapes the task root: {ref}",
            )
        if not resolved.is_file():
            raise RouteReviewError(
                "route-review-evidence-missing",
                f"route-review evidence does not exist: {ref}",
            )
