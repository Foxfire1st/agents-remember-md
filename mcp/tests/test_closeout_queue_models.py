"""Strict L3 door and projection wire-model invariants."""

from __future__ import annotations

import unittest

from agents_remember.models.closeout.projection import (
    MAX_CLOSEOUT_CANDIDATES,
    MAX_CLOSEOUT_REASONS,
    MAX_CLOSEOUT_SOURCE_PROBLEMS,
    CloseoutProjectionMember,
    CloseoutQueueState,
    ProjectionSourceProblem,
)
from agents_remember.models.closeout.source import CandidateAdmissionFacts, SchedulingGradeInput
from agents_remember.models.lifecycles.door import (
    CloseoutDoorGeneration,
    CloseoutDoorRequest,
    DoorAdmissionProvenance,
    DoorProvenance,
    DoorSchedulingProvenance,
)
from agents_remember.models.queue.closeout_queue import CloseoutQueueResponse
from agents_remember.models.task_document_ref import TaskDocumentRef
from pydantic import ValidationError

NOW = "2026-08-24T00:00:00+00:00"
HEX40 = "a" * 40
HEX64 = "b" * 64
SPRINT = TaskDocumentRef(repository="repo-a", path="sprint/task.json")
MASTER = TaskDocumentRef(repository="repo-a", path="master/task.json")
LEAF = TaskDocumentRef(repository="repo-a", path="master/leaf.json")


def door_generation(**updates: object) -> CloseoutDoorGeneration:
    not_applicable = DoorProvenance(
        state="not-applicable",
        fingerprint="c" * 64,
    )
    payload: dict[str, object] = {
        "generationId": HEX64,
        "disposition": "waiting",
        "taskId": "L3",
        "taskName": "master",
        "taskDocumentRef": LEAF,
        "owningMasterTaskDocumentRef": MASTER,
        "sprintTaskDocumentRef": SPRINT,
        "contractPath": "/coord/tasks/repo-a/master/enclosures/leaf/contract.md",
        "candidateTree": HEX40,
        "codeBaseCommit": "d" * 40,
        "taskTopologyFingerprint": "e" * 64,
        "taskIntent": {"schema": "task-intent/v1", "digest": "9" * 64},
        "reviewProvenance": not_applicable,
        "memoryProvenance": not_applicable,
        "ledgerProvenance": not_applicable,
        "admissionProvenance": DoorAdmissionProvenance(fingerprint="f" * 64),
        "schedulingProvenance": DoorSchedulingProvenance(
            priority="normal",
            judgmentId="J-1",
            fingerprint="1" * 64,
        ),
        "declaredBy": f"manager@{MASTER.key}",
        "declaredAt": NOW,
    }
    payload.update(updates)
    return CloseoutDoorGeneration.model_validate(payload)


def projection_member(**updates: object) -> CloseoutProjectionMember:
    payload: dict[str, object] = {
        "generationId": HEX64,
        "taskDocumentRef": LEAF,
        "owningMaster": MASTER,
        "contractPath": "/contract.md",
        "candidateTree": HEX40,
        "sourceDoorFingerprint": "2" * 64,
        "classification": "ready",
        "reasons": [],
        "priority": "normal",
        "order": 0,
    }
    payload.update(updates)
    return CloseoutProjectionMember.model_validate(payload)


class CloseoutDoorModelTests(unittest.TestCase):
    def test_dispositions_are_only_waiting_deferred_withdrawn_claimed(self) -> None:
        for disposition in ("waiting", "deferred", "withdrawn"):
            self.assertEqual(door_generation(disposition=disposition).disposition, disposition)
        with self.assertRaises(ValidationError):
            door_generation(disposition="cancelled")
        claimed = door_generation(
            disposition="claimed",
            operationKind="closeout",
            operationFingerprint="3" * 64,
            claimedOperationKey="4" * 64,
        )
        self.assertEqual(claimed.disposition, "claimed")

    def test_operation_identity_exists_only_on_claimed_generation(self) -> None:
        with self.assertRaisesRegex(ValidationError, "claimed door requires"):
            door_generation(operationKind="closeout")
        with self.assertRaisesRegex(ValidationError, "claimed door requires"):
            door_generation(disposition="claimed")

    def test_request_surface_separates_source_publication_from_projection(self) -> None:
        declare = CloseoutDoorRequest(
            action="declare",
            contract_path="/contract.md",
            grade=SchedulingGradeInput(priority="normal", judgmentId="J-1"),
            admission=CandidateAdmissionFacts(),
        )
        self.assertEqual(declare.action, "declare")
        with self.assertRaises(ValidationError):
            CloseoutDoorRequest.model_validate(
                {"action": "select", "contract_path": "/contract.md"}
            )
        with self.assertRaises(ValidationError):
            CloseoutDoorRequest(action="defer", contract_path="/contract.md")
        with self.assertRaises(ValidationError):
            CloseoutDoorRequest(
                action="status",
                contract_path="/contract.md",
                candidate_task_document_ref=LEAF,
            )


class CloseoutProjectionModelTests(unittest.TestCase):
    def test_invalid_empty_carries_no_membership_or_source_identity(self) -> None:
        with self.assertRaises(ValidationError):
            CloseoutQueueState(
                sprintTaskDocumentRef=SPRINT,
                revision=1,
                serviceCondition="invalid-empty",
                sourceFingerprint=HEX64,
                updatedAt=NOW,
            )
        with self.assertRaises(ValidationError):
            CloseoutQueueState(
                sprintTaskDocumentRef=SPRINT,
                revision=1,
                serviceCondition="valid-built",
                updatedAt=NOW,
            )

    def test_terminal_valid_build_is_empty(self) -> None:
        terminal = CloseoutQueueState(
            sprintTaskDocumentRef=SPRINT,
            revision=1,
            serviceCondition="valid-built",
            sourceClassification="terminal",
            sourceFingerprint=HEX64,
            updatedAt=NOW,
        )
        self.assertEqual(terminal.members, [])
        with self.assertRaises(ValidationError):
            CloseoutQueueState.model_validate(
                {
                    **terminal.model_dump(mode="json"),
                    "members": [projection_member().model_dump(mode="json")],
                }
            )

    def test_member_reasons_and_all_wire_lists_are_bounded(self) -> None:
        with self.assertRaises(ValidationError):
            projection_member(
                reasons=[f"reason-{index}" for index in range(MAX_CLOSEOUT_REASONS + 1)]
            )
        response = {
            "ok": True,
            "action": "status",
            "state": "valid-built",
            "summary": "current",
            "sprintTaskDocumentRef": SPRINT.model_dump(),
            "revision": 1,
            "sourceClassification": "active",
            "sourceFingerprint": HEX64,
            "effectiveSourceFingerprint": HEX64,
            "members": [projection_member().model_dump()] * (MAX_CLOSEOUT_CANDIDATES + 1),
            "updatedAt": NOW,
        }
        with self.assertRaises(ValidationError):
            CloseoutQueueResponse.model_validate(response)
        response["members"] = []
        response["sourceProblems"] = [
            ProjectionSourceProblem(
                kind="task",
                address=f"task-{index}",
                state="missing",
                errorType="missing",
                repairAction="repair",
            ).model_dump()
            for index in range(MAX_CLOSEOUT_SOURCE_PROBLEMS + 1)
        ]
        with self.assertRaises(ValidationError):
            CloseoutQueueResponse.model_validate(response)
