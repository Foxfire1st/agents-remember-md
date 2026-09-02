"""Route-review binding machinery for ``task_doc`` (facade-extracted).

The call-level knobs (``TaskDocCall``), the policy gate for branch-addressed
direct execution (``_enforce_branch_addressed_policy``), the leaf/series
contract binding behind ``record_route_review`` (``_RouteReviewBinding``,
``_record_route_review_bound``, ``_require_route_review_binding``), and the
route-review authority rule (``_enforce_route_review_authority``). Extracted
from ``task_doc_tools.py`` so the facade stays under the file-size cap; the
facade re-exports the names its callers import (same pattern as
``task_reopen.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agents_remember.errors import AgentsRememberError
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.tasks import TaskDocument
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.tasks.leaf_doc import (
    TerminalLeafResolutionError,
    resolve_terminal_leaf_doc,
)
from agents_remember.worktrees.route_review import (
    RouteReviewError,
    build_route_review,
    document_ref,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract


class TaskDocError(AgentsRememberError):
    """Raised when a task-document operation cannot be completed."""


@dataclass(frozen=True)
class TaskDocCall:
    """Call-level knobs that are not part of the edit.

    ``branch_addressed`` opts into the policy-gated series-contract binding for
    ``record_route_review`` under sanctioned direct execution.
    """

    dry_run: bool = False
    branch_addressed: bool = False


DEFAULT_TASK_DOC_CALL = TaskDocCall()
"""The ordinary call: a real mutation, worktree-contract binding."""


def _enforce_branch_addressed_policy(
    config: McpRuntimeConfig, operation: str, branch_addressed: bool
) -> None:
    """Refuse a branch-addressed call the policy does not sanction."""
    if not branch_addressed:
        return
    if operation != "record_route_review":
        raise TaskDocError(
            "branch_addressed mode is only defined for record_route_review; "
            f"operation {operation!r} resolves its own contract binding"
        )
    if not config.direct_execution_enabled:
        raise TaskDocError(
            "branch_addressed mode is disabled by policy; enable directExecutionEnabled "
            "in the MCP authority settings for sanctioned direct execution"
        )


@dataclass(frozen=True)
class _RouteReviewBinding:
    """How a route-review call binds its leaf: worktree contract or series branch.

    ``branch_addressed`` opts into the policy-gated series-contract binding used
    by sanctioned direct execution (no leaf worktree); the selected document is
    then the leaf identity itself.
    """

    contract: WorktreeContract | None
    task_root: Path
    selected_path: Path
    branch_addressed: bool = False


def _record_route_review(
    doc: TaskDocument,
    payload: dict[str, Any] | None,
    contract: WorktreeContract | None,
    task_root: Path,
    selected_path: Path,
) -> TaskDocument:
    """Legacy positional contract (kept): bind a leaf worktree contract review.

    ``task_doc_tool`` records through the binding form
    (``_record_route_review_bound``) so branch_addressed direct execution stays
    available; this positional form preserves the pre-wave-2 call shape and its
    error dialect.
    """
    if doc.kind == "master":
        raise TaskDocError("record_route_review is valid only for a leaf task document")
    if contract is None:
        raise TaskDocError("record_route_review requires the leaf worktree contract")
    if payload is None:
        raise TaskDocError("record_route_review requires a review object")
    try:
        resolved = resolve_terminal_leaf_doc(
            task_root,
            contract.leaf_id,
            asserted_path=selected_path,
        )
    except TerminalLeafResolutionError as exc:
        raise TaskDocError(str(exc)) from exc
    if resolved is None or resolved[0].resolve() != selected_path.resolve():
        raise TaskDocError(
            "record_route_review target is not the exact task document bound to the leaf contract"
        )
    try:
        review = build_route_review(
            contract,
            ResolvedTaskDocument(
                ref=document_ref(contract, selected_path),
                path=selected_path,
                document=doc,
            ),
            payload,
        )
    except (RouteReviewError, ValidationError) as exc:
        raise TaskDocError(str(exc)) from exc
    data = doc.model_dump(by_alias=True)
    data["routeReview"] = review.model_dump(mode="json")
    return _validate(data)


def _record_route_review_bound(
    doc: TaskDocument,
    payload: dict[str, Any] | None,
    binding: _RouteReviewBinding,
) -> TaskDocument:
    if doc.kind == "master":
        raise TaskDocError(
            "record_route_review is valid only for a leaf task document; "
            f"{binding.selected_path} is a master document -- bind the review to a "
            "leaf (its worktree contract) or re-stamp the series contract and retry "
            "with branch_addressed=true for direct execution"
        )
    if payload is None:
        raise TaskDocError("record_route_review requires a review object")
    _require_route_review_binding(binding)
    contract = binding.contract
    assert contract is not None  # _require_route_review_binding proves the binding
    try:
        review = build_route_review(
            contract,
            ResolvedTaskDocument(
                ref=document_ref(contract, binding.selected_path),
                path=binding.selected_path,
                document=doc,
            ),
            payload,
            branch_addressed=binding.branch_addressed,
        )
    except (RouteReviewError, ValidationError) as exc:
        raise TaskDocError(str(exc)) from exc
    data = doc.model_dump(by_alias=True)
    data["routeReview"] = review.model_dump(mode="json")
    return _validate(data)


def _require_route_review_binding(binding: _RouteReviewBinding) -> None:
    """Refuse a route-review binding that is not exact for its mode."""
    contract = binding.contract
    if contract is None:
        raise TaskDocError(
            "record_route_review requires a contract binding; no leaf worktree "
            f"contract exists for {binding.task_root} -- re-stamp the series "
            "contract (series-contract.md) or use branch_addressed=true for direct "
            "execution"
        )
    if binding.branch_addressed:
        if contract.kind != "series":
            raise TaskDocError(
                "record_route_review branch_addressed mode requires the task-root "
                f"series contract; {contract.contract_path} is a {contract.kind} contract"
            )
        if not binding.selected_path.resolve().is_relative_to(binding.task_root.resolve()):
            raise TaskDocError(
                "record_route_review branch_addressed target is outside the task root"
            )
        return
    if contract.kind != "leaf":
        raise TaskDocError(
            "record_route_review requires the leaf worktree contract; "
            f"{contract.contract_path} is a {contract.kind} contract -- pass the "
            "leaf enclosure contract, or use branch_addressed=true to bind the "
            "task-root series contract for direct execution"
        )
    try:
        resolved = resolve_terminal_leaf_doc(
            binding.task_root,
            contract.leaf_id,
            asserted_path=binding.selected_path,
        )
    except TerminalLeafResolutionError as exc:
        raise TaskDocError(str(exc)) from exc
    if resolved is None or resolved[0].resolve() != binding.selected_path.resolve():
        raise TaskDocError(
            "record_route_review target is not the exact task document bound to the leaf contract"
        )


def _enforce_route_review_authority(
    operation: str,
    original: TaskDocument | None,
    candidate: TaskDocument,
) -> None:
    candidate_review = candidate.routeReview.model_dump_json() if candidate.routeReview else None
    original_review = (
        original.routeReview.model_dump_json()
        if original is not None and original.routeReview is not None
        else None
    )
    if operation == "create" and candidate_review is not None:
        raise TaskDocError(
            "create cannot author route-review evidence; use task_doc.record_route_review"
        )
    if operation == "replace" and candidate_review != original_review:
        raise TaskDocError(
            "replace cannot add, remove, or change route-review evidence; "
            "use task_doc.record_route_review"
        )


def _validate(data: dict[str, Any]) -> TaskDocument:
    try:
        return TaskDocument.model_validate(data)
    except ValidationError as exc:
        raise TaskDocError(f"invalid task document: {exc}") from exc
