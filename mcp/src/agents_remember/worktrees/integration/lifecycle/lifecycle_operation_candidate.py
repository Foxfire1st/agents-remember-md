"""Immutable candidate identity for task-bound lifecycle operations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from agents_remember.models.lifecycles.operation import (
    IntegrationOperationAuthority,
    LifecycleOperationInput,
)
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.closeout_input import CloseoutCandidateSnapshot


@dataclass(frozen=True)
class LifecycleOperationCandidate:
    state: str
    tree: str | None
    fingerprint: str
    task_intent: TaskIntentIdentity | None = None


@dataclass(frozen=True)
class LifecycleOperationCandidateBinding:
    """All evidence whose exact combination identifies one operation generation."""

    operation_input: LifecycleOperationInput
    candidate_state: str
    candidate_tree: str | None = None
    closeout_candidate: CloseoutCandidateSnapshot | None = None
    closeout_door_generation_id: str | None = None
    integration_authority: IntegrationOperationAuthority | None = None
    task_intent: TaskIntentIdentity | None = None


def fingerprint_payload(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def lifecycle_operation_candidate(
    binding: LifecycleOperationCandidateBinding,
) -> LifecycleOperationCandidate:
    """Bind durable input and every route-specific candidate fact to one fingerprint."""
    operation_input = binding.operation_input
    closeout_candidate = binding.closeout_candidate
    resolved_tree = (
        closeout_candidate.candidate_tree
        if closeout_candidate is not None
        else binding.candidate_tree
    )
    payload = {
        "input": operation_input.model_dump(mode="json"),
        "candidateState": binding.candidate_state,
        "candidateTree": resolved_tree,
        "integrationAuthority": (
            binding.integration_authority.model_dump(mode="json")
            if binding.integration_authority is not None
            else None
        ),
    }
    if binding.task_intent is not None:
        payload["taskIntent"] = binding.task_intent.model_dump(mode="json", by_alias=True)
    if closeout_candidate is not None:
        payload.update(
            {
                "candidateHead": closeout_candidate.head_commit,
                "candidateHeadTree": closeout_candidate.head_tree,
            }
        )
    if binding.closeout_door_generation_id is not None:
        payload["closeoutDoorGenerationId"] = binding.closeout_door_generation_id
    return LifecycleOperationCandidate(
        state=binding.candidate_state,
        tree=resolved_tree,
        fingerprint=fingerprint_payload(payload),
        task_intent=binding.task_intent,
    )
