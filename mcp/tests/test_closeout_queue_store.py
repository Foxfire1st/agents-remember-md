"""L3 forcing for the two-state disposable projection store."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents_remember.controlplane.closeout_queue_records import CloseoutProjectionBuild
from agents_remember.controlplane.closeout_queue_store import (
    CloseoutQueueStore,
    ProjectionSourceIdentity,
)
from agents_remember.models.closeout.projection import (
    CloseoutProjectionMember,
    ProjectionSourceProblem,
)
from agents_remember.models.task_document_ref import TaskDocumentRef

NOW = "2026-08-24T00:00:00+00:00"
SPRINT = TaskDocumentRef(repository="repo-a", path="sprint/task.json")
LEAF = TaskDocumentRef(repository="repo-a", path="master/leaf.json")
MASTER = TaskDocumentRef(repository="repo-a", path="master/task.json")
HEX40 = "a" * 40
HEX64 = "b" * 64


def _member(*, generation: str = HEX64) -> CloseoutProjectionMember:
    return CloseoutProjectionMember(
        generationId=generation,
        taskDocumentRef=LEAF,
        owningMaster=MASTER,
        contractPath="/coord/tasks/repo-a/master/enclosures/leaf/contract.md",
        candidateTree=HEX40,
        sourceDoorFingerprint="c" * 64,
        classification="ready",
        priority="normal",
        order=0,
    )


def _build(*, fingerprint: str = HEX64) -> CloseoutProjectionBuild:
    return CloseoutProjectionBuild(
        sprintTaskDocumentRef=SPRINT,
        sourceFingerprint=fingerprint,
        sourceClassification="active",
        members=[_member()],
        builtAt=NOW,
    )


def _active_source(*, fingerprint: str = HEX64) -> ProjectionSourceIdentity:
    return ProjectionSourceIdentity(
        fingerprint,
        classification="active",
        members=(_member(),),
    )


class CloseoutProjectionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CloseoutQueueStore(self.root, SPRINT)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, text: str) -> None:
        self.store.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.state_path.write_text(text, encoding="utf-8")

    def test_effective_read_preserves_artifact_diagnostic_and_refuses_source_mismatch(self) -> None:
        self._write("not-json")
        effective = self.store.read_effective(
            timestamp=NOW,
            source=_active_source(),
        )
        self.assertEqual(effective.serviceCondition, "invalid-empty")
        self.assertEqual(effective.sourceProblems[0].kind, "projection")

        self.store.invalidate(timestamp=NOW)
        published, outcome = self.store.publish_build(
            _build(),
            current_source=_active_source,
        )
        self.assertEqual(outcome.outcome, "published")
        mismatched = self.store.read_effective(
            timestamp=NOW,
            source=ProjectionSourceIdentity("d" * 64),
        )
        self.assertEqual(mismatched.serviceCondition, "invalid-empty")
        self.assertEqual(mismatched.members, [])
        self.assertEqual(mismatched.revision, published.revision)
        self.assertEqual(mismatched.sourceProblems[0].errorType, "source-fingerprint-mismatch")

    def test_stale_off_side_builder_never_publishes(self) -> None:
        self.store.invalidate(timestamp=NOW)
        state, outcome = self.store.publish_build(
            _build(),
            current_source=lambda: _active_source(fingerprint="d" * 64),
        )
        self.assertEqual(outcome.outcome, "source-changed")
        self.assertEqual(state.serviceCondition, "invalid-empty")
        self.assertFalse(self.store.build_path.exists())

    def test_source_unreadability_keeps_canonical_state_non_admitting(self) -> None:
        self.store.invalidate(timestamp=NOW)
        problem = ProjectionSourceProblem(
            kind="door",
            address="/door",
            state="unreadable",
            errorType="contract-unreadable",
            repairAction="repair the exact door",
        )
        state, outcome = self.store.publish_build(
            _build(),
            current_source=lambda: ProjectionSourceIdentity(None, (problem,)),
        )
        self.assertEqual(outcome.outcome, "source-unreadable")
        self.assertEqual(outcome.sourceProblems, [problem])
        self.assertEqual(state.serviceCondition, "invalid-empty")
