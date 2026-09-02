"""Single contract publication owner for closeout-door generations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from agents_remember.models.lifecycles.door import (
    CloseoutDoorDisposition,
    CloseoutDoorGeneration,
    DoorDependencyInputs,
    DoorPublicationEvidence,
    closeout_door_dependencies,
    require_closeout_door_dependencies,
)
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    fingerprint_payload,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.worktree_contract import (
    ContractError,
    WorktreeContract,
    contract_publication_text,
    load_contract,
    write_contract,
)


@dataclass(frozen=True)
class DoorContractReadFailure:
    """Bounded canonical-reader failure used by read-only status projection."""

    errorType: str
    detail: str


@dataclass(frozen=True)
class DoorPublicationClassification:
    """One exact accepted/published/conflicting observation of a door intent."""

    state: Literal["accepted-before", "published", "developer-decision"]
    expected: dict[str, object]
    observed: dict[str, object]

    def decision_payload(self) -> dict[str, object]:
        detail = "the contract is unreadable or outside the journaled door publication"
        return {
            "state": "closeout-door-publication-conflict",
            "reason": detail,
            "summary": detail,
            "developerDecisionRequired": True,
            "decisionSurface": detail,
            "nextAction": "developer-decision",
            "expected": self.expected,
            "observed": self.observed,
        }


class DoorPublicationError(RuntimeError):
    """Typed interruption/conflict from the canonical door publisher."""

    def __init__(
        self,
        status: Literal[
            "closeout-door-publication-interrupted",
            "closeout-door-publication-conflict",
        ],
        detail: str,
        classification: DoorPublicationClassification,
    ) -> None:
        self.status = status
        self.detail = detail
        self.classification = classification
        super().__init__(detail)


def door_generation_for_operation(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    disposition: CloseoutDoorDisposition,
    *,
    predecessor_generation_id: str = "",
) -> CloseoutDoorGeneration:
    """Claim the exact already-published waiting generation for one journal intent."""

    if disposition != "claimed":
        raise RuntimeError(
            "cancel, retire, and supersede are journal outcomes, not door dispositions"
        )
    waiting = contract.closeout_door
    if waiting is None or waiting.disposition != "waiting":
        raise RuntimeError("closeout operation requires one exact waiting door generation")
    if predecessor_generation_id and waiting.predecessorGenerationId != predecessor_generation_id:
        raise RuntimeError(
            "claimed door predecessor assertion does not match its source generation"
        )
    if waiting.contractPath != record.contractPath or waiting.taskId != record.taskId:
        raise RuntimeError("waiting door does not identify the accepted closeout operation task")
    if (
        not isinstance(waiting.taskIntent, TaskIntentIdentity)
        or record.taskIntent != waiting.taskIntent
    ):
        raise RuntimeError("waiting door and operation must bind the same canonical task intent")
    require_closeout_door_dependencies(waiting)
    return waiting.model_copy(
        update={
            "disposition": "claimed",
            "operationKind": record.operationKind,
            "operationFingerprint": record.fingerprint,
            "claimedOperationKey": record.operationKey,
        }
    )


def successor_waiting_door(
    claimed: CloseoutDoorGeneration,
    *,
    declared_by: str,
    declared_at: str,
) -> CloseoutDoorGeneration:
    """Derive one deterministic schedulable source successor from a claimed generation."""

    if claimed.disposition != "claimed":
        raise RuntimeError("a waiting successor requires one exact claimed predecessor")
    dependencies = closeout_door_dependencies(
        DoorDependencyInputs(
            candidate_tree=claimed.candidateTree,
            memory_candidate_tree=claimed.memoryCandidateTree,
            task_topology_fingerprint=claimed.taskTopologyFingerprint,
            task_intent=claimed.taskIntent,
            review=claimed.reviewProvenance,
            memory=claimed.memoryProvenance,
            ledger=claimed.ledgerProvenance,
            admission=claimed.admissionProvenance,
            scheduling=claimed.schedulingProvenance,
            predecessor=claimed.generationId,
        )
    )
    identity = {
        "schema": "ar-closeout-door-successor/v1",
        "predecessorGenerationId": claimed.generationId,
        "taskId": claimed.taskId,
        "taskDocumentRef": claimed.taskDocumentRef.model_dump(mode="json"),
        "owningMasterTaskDocumentRef": claimed.owningMasterTaskDocumentRef.model_dump(mode="json"),
        "sprintTaskDocumentRef": claimed.sprintTaskDocumentRef.model_dump(mode="json"),
        "contractPath": claimed.contractPath,
        "candidateTree": claimed.candidateTree,
        "memoryCandidateTree": claimed.memoryCandidateTree,
        "codeBaseCommit": claimed.codeBaseCommit,
        "memoryBaseCommit": claimed.memoryBaseCommit,
        "ledgerMemoryCommit": claimed.ledgerMemoryCommit,
        "taskTopologyFingerprint": claimed.taskTopologyFingerprint,
        "taskIntent": claimed.taskIntent.model_dump(mode="json", by_alias=True),
        "reviewProvenance": claimed.reviewProvenance.model_dump(mode="json"),
        "memoryProvenance": claimed.memoryProvenance.model_dump(mode="json"),
        "ledgerProvenance": claimed.ledgerProvenance.model_dump(mode="json"),
        "admissionProvenance": claimed.admissionProvenance.model_dump(mode="json"),
        "schedulingProvenance": claimed.schedulingProvenance.model_dump(mode="json"),
        "dependencies": dependencies.model_dump(mode="json"),
    }
    return claimed.model_copy(
        update={
            "generationId": fingerprint_payload(identity),
            "predecessorGenerationId": claimed.generationId,
            "disposition": "waiting",
            "declaredBy": declared_by,
            "declaredAt": declared_at,
            "dependencies": dependencies,
            "operationKind": None,
            "operationFingerprint": "",
            "claimedOperationKey": "",
        }
    )


def prepare_door_publication(
    contract: WorktreeContract,
    generation: CloseoutDoorGeneration,
) -> DoorPublicationEvidence:
    """Return exact before/after contract-byte evidence before publication."""

    _require_door_transition(contract.closeout_door, generation)
    updated = replace(contract, closeout_door=generation)
    published_text = contract_publication_text(contract.contract_path, updated)
    return DoorPublicationEvidence(
        state="intent",
        generation=generation,
        expectedBeforeContractSha256=closeout_contract_sha256(contract),
        expectedPublishedContractSha256=hashlib.sha256(published_text.encode("utf-8")).hexdigest(),
    )


def classify_door_publication(
    intent: DoorPublicationEvidence,
    live: WorktreeContract | DoorContractReadFailure,
) -> DoorPublicationClassification:
    """Classify one canonical contract read without performing another read."""

    expected: dict[str, object] = {
        "beforeContractSha256": intent.expectedBeforeContractSha256,
        "publishedContractSha256": intent.expectedPublishedContractSha256,
        "generationId": intent.generation.generationId,
        "disposition": intent.generation.disposition,
    }
    if isinstance(live, DoorContractReadFailure):
        return DoorPublicationClassification(
            "developer-decision",
            expected,
            {
                "readStatus": "unreadable",
                "side": "contract",
                "name": Path(intent.generation.contractPath).name,
                "errorType": live.errorType,
            },
        )
    current_sha = closeout_contract_sha256(live)
    current_door = live.closeout_door
    observed: dict[str, object] = {
        "readStatus": "readable",
        "contractSha256": current_sha,
        "generationId": current_door.generationId if current_door else "",
        "disposition": current_door.disposition if current_door else "",
    }
    if current_sha == intent.expectedPublishedContractSha256:
        state: Literal["published", "developer-decision"] = (
            "published" if current_door == intent.generation else "developer-decision"
        )
        return DoorPublicationClassification(state, expected, observed)
    if current_sha == intent.expectedBeforeContractSha256:
        try:
            _require_door_transition(current_door, intent.generation)
        except RuntimeError as exc:
            return DoorPublicationClassification(
                "developer-decision",
                expected,
                {
                    **observed,
                    "transitionFailure": {
                        "stage": "door-transition-validation",
                        "side": "contract",
                        "name": Path(intent.generation.contractPath).name,
                        "errorType": type(exc).__name__,
                    },
                },
            )
        return DoorPublicationClassification("accepted-before", expected, observed)
    return DoorPublicationClassification("developer-decision", expected, observed)


def observe_door_publication(
    contract_path: Path,
    intent: DoorPublicationEvidence,
) -> DoorPublicationClassification:
    """Read through the canonical contract reader and classify its exact outcome."""

    try:
        live: WorktreeContract | DoorContractReadFailure = load_contract(contract_path)
    except (ContractError, OSError, UnicodeError, ValueError) as exc:
        live = DoorContractReadFailure(type(exc).__name__, "")
    return classify_door_publication(intent, live)


def publish_door_intent(
    contract_path: Path,
    intent: DoorPublicationEvidence,
) -> DoorPublicationEvidence:
    """Publish or prove exactly the intended contract generation idempotently."""

    classification = observe_door_publication(contract_path, intent)
    if classification.state == "published":
        return intent.model_copy(
            update={
                "state": "proven",
                "observedPublishedContractSha256": intent.expectedPublishedContractSha256,
            }
        )
    if classification.state == "developer-decision":
        raise DoorPublicationError(
            "closeout-door-publication-conflict",
            "the contract is unreadable or outside the journaled door publication",
            classification,
        )
    try:
        current = load_contract(contract_path)
        revalidated = classify_door_publication(intent, current)
        if revalidated.state != "accepted-before":
            raise DoorPublicationError(
                "closeout-door-publication-conflict",
                "the contract changed after door publication preflight",
                revalidated,
            )
        write_contract(contract_path, replace(current, closeout_door=intent.generation))
    except DoorPublicationError:
        raise
    except (ContractError, OSError, RuntimeError, UnicodeError, ValueError) as exc:
        after = observe_door_publication(contract_path, intent)
        if after.state == "published":
            return intent.model_copy(
                update={
                    "state": "proven",
                    "observedPublishedContractSha256": intent.expectedPublishedContractSha256,
                }
            )
        if after.state == "accepted-before":
            raise DoorPublicationError(
                "closeout-door-publication-interrupted",
                "the journaled closeout-door publication did not change contract bytes",
                after,
            ) from exc
        raise DoorPublicationError(
            "closeout-door-publication-conflict",
            "the contract changed or became unreadable during door publication",
            after,
        ) from exc
    after = observe_door_publication(contract_path, intent)
    if after.state == "accepted-before":
        raise DoorPublicationError(
            "closeout-door-publication-interrupted",
            "the journaled closeout-door publication did not change contract bytes",
            after,
        )
    if after.state == "developer-decision":
        raise DoorPublicationError(
            "closeout-door-publication-conflict",
            "the door publication did not produce its exact intended contract",
            after,
        )
    return intent.model_copy(
        update={
            "state": "proven",
            "observedPublishedContractSha256": intent.expectedPublishedContractSha256,
        }
    )


def _require_door_transition(
    current: CloseoutDoorGeneration | None,
    updated: CloseoutDoorGeneration,
) -> None:
    if current is None:
        return
    if current.generationId == updated.generationId:
        immutable = (
            "predecessorGenerationId",
            "taskId",
            "taskName",
            "contractPath",
            "codeBaseCommit",
            "memoryBaseCommit",
            "taskDocumentRef",
            "owningMasterTaskDocumentRef",
            "sprintTaskDocumentRef",
            "candidateTree",
            "memoryCandidateTree",
            "taskTopologyFingerprint",
            "taskIntent",
            "reviewProvenance",
            "memoryProvenance",
            "ledgerProvenance",
            "admissionProvenance",
            "schedulingProvenance",
            "declaredBy",
            "declaredAt",
        )
        if any(getattr(current, field) != getattr(updated, field) for field in immutable):
            raise RuntimeError("closeout-door generation identity is immutable")
        allowed = {
            "waiting": {"waiting", "deferred", "withdrawn", "claimed"},
            "deferred": {"waiting", "deferred", "withdrawn"},
            "withdrawn": {"withdrawn"},
            "claimed": {"claimed"},
        }
        if updated.disposition not in allowed[current.disposition]:
            raise RuntimeError("invalid closeout-door disposition transition")
        if current.disposition == "claimed" and (
            current.operationKind != updated.operationKind
            or current.operationFingerprint != updated.operationFingerprint
            or current.claimedOperationKey != updated.claimedOperationKey
        ):
            raise RuntimeError("claimed closeout-door operation identity is immutable")
        return
    successor_edge = (current.disposition, updated.disposition)
    if updated.predecessorGenerationId != current.generationId or successor_edge not in {
        ("claimed", "waiting"),
        ("withdrawn", "waiting"),
        ("waiting", "waiting"),
        ("deferred", "deferred"),
    }:
        raise RuntimeError("new closeout-door generation requires the exact predecessor link")
    if current.disposition == "claimed" and (
        updated.operationKind is not None
        or updated.operationFingerprint
        or updated.claimedOperationKey
    ):
        raise RuntimeError("a claimed cancellation successor must clear operation identity")
