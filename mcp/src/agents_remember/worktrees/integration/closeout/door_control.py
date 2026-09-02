"""Contract publication boundary for closeout-door source commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents_remember.controlplane.task_publication_lock import task_publication_lock
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.door import CloseoutDoorGeneration, CloseoutDoorRequest
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.integration.closeout.door import (
    DoorPublicationError,
    prepare_door_publication,
    publish_door_intent,
)
from agents_remember.worktrees.integration.closeout.door_source import (
    authorize_door_actor,
    door_task_context,
    updated_door_generation,
)
from agents_remember.worktrees.integration.configured_contract_authority import (
    reread_configured_contract,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_lifecycle_evidence_pair,
)
from agents_remember.worktrees.queue.closeout_projection_publication import (
    projection_refresh_failure_effect,
    refresh_closeout_projection,
)
from agents_remember.worktrees.queue.closeout_queue_errors import CloseoutQueueError
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract

DoorActor = DeclaredCaller


def closeout_door_tool(
    config: McpRuntimeConfig,
    request: CloseoutDoorRequest,
    *,
    actor: DeclaredCaller,
    admitted_contract: WorktreeContract,
) -> dict[str, Any]:
    """Read or publish one authoritative door source, then refresh its projection."""

    if request.action == "status":
        context = door_task_context(config, admitted_contract, request)
        authorize_door_actor(actor, context, request.action)
        return _response(request, admitted_contract, effects=[])

    with task_publication_lock(config.coordination_root, admitted_contract.repo_name):
        contract, _location = reread_configured_contract(
            admitted_contract,
            config.config_path.as_posix(),
        )
        context = door_task_context(config, contract, request)
        authorize_door_actor(actor, context, request.action)
        generation = updated_door_generation(context, request, actor)
        try:
            proof = publish_door_intent(
                contract.contract_path,
                prepare_door_publication(contract, generation),
            )
        except DoorPublicationError as exc:
            return _publication_failure(request, contract.contract_path, generation, exc)
        if proof.state != "proven":
            raise CloseoutQueueError(
                "closeout-door-publication-interrupted",
                "door source publication did not produce exact canonical contract bytes",
            )
        published = load_contract(contract.contract_path)
    try:
        effect = refresh_closeout_projection(
            config.coordination_root,
            generation.sprintTaskDocumentRef,
        )
    except Exception as exc:
        effect = projection_refresh_failure_effect(
            config.coordination_root,
            generation.sprintTaskDocumentRef,
            exc,
        )
    return _response(request, published, effects=[effect.model_dump(mode="json")])


def _response(
    request: CloseoutDoorRequest,
    contract: WorktreeContract,
    *,
    effects: list[dict[str, Any]],
) -> dict[str, Any]:
    generation = contract.closeout_door
    unavailable = generation is not None and not isinstance(
        generation.taskIntent, TaskIntentIdentity
    )
    return {
        "ok": True,
        "operation": "closeout_door",
        "action": request.action,
        "state": (
            "closeout-door-task-intent-unavailable"
            if unavailable
            else generation.disposition
            if generation is not None
            else "absent"
        ),
        "summary": (
            "The legacy closeout door predates canonical task intent."
            if unavailable
            else f"Closeout door {generation.generationId[:12]} is {generation.disposition}."
            if generation is not None
            else "No closeout door generation is published."
        ),
        "contractPath": contract.contract_path.as_posix(),
        "generation": generation.model_dump(mode="json") if generation is not None else None,
        "projectionEffects": effects,
        **({"nextAction": "closeout_door.update-provenance"} if unavailable else {}),
    }


def _publication_failure(
    request: CloseoutDoorRequest,
    path: Path,
    generation: CloseoutDoorGeneration,
    error: DoorPublicationError,
) -> dict[str, Any]:
    classification = error.classification
    public = public_lifecycle_evidence_pair(
        classification.expected,
        classification.observed,
    )
    recoverable = classification.state == "accepted-before"
    return {
        "ok": False,
        "operation": "closeout_door",
        "action": request.action,
        "state": "refused",
        "summary": error.detail,
        "status": error.status,
        "detail": error.detail,
        "contractPath": path.as_posix(),
        "generation": generation.model_dump(mode="json"),
        "projectionEffects": [],
        "expected": public.expected,
        "observed": public.observed,
        "developerDecisionRequired": not recoverable,
        "decisionSurface": (
            None if recoverable else "the contract is outside the journaled door publication"
        ),
        "nextAction": (
            "retry the same closeout_door request" if recoverable else "developer-decision"
        ),
    }
