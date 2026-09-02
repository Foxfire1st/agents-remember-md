"""L3 task-first multi-scope publication and actionable-effect forcing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast
from unittest import mock

from agents_remember.application.task_docs.task_doc_queue_scope import (
    TaskDocScopeChange,
    resolve_projection_scope_union,
)
from agents_remember.models.closeout.projection import (
    CloseoutQueueState,
    ProjectionInvalidationResult,
    ProjectionRebuildResult,
    TaskDocProjectionEffect,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks.document import TaskDocument
from agents_remember.worktrees import task_fact_publication as publication
from agents_remember.worktrees.queue.closeout_projection_publication import (
    ProjectionInvalidationReceipt,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract

NOW = "2026-08-24T00:00:00+00:00"
SPRINT_A = TaskDocumentRef(repository="repo-a", path="a/task.json")
SPRINT_B = TaskDocumentRef(repository="repo-a", path="b/task.json")


def _sprint_document(**updates: object) -> TaskDocument:
    document = TaskDocument.model_validate(
        {
            "id": "SPRINT-A",
            "slug": "sprint-a",
            "title": "Sprint A",
            "kind": "master",
            "status": "inProgress",
            "repo": "repo-a",
            "type": "Sprint",
            "createdAt": NOW,
            "orchestrates": ["master-a"],
        }
    )
    return document.model_copy(update=updates)


def _leaf_document(**updates: object) -> TaskDocument:
    document = TaskDocument.model_validate(
        {
            "id": "L1",
            "slug": "01_leaf",
            "title": "Leaf",
            "kind": "subTask",
            "status": "inProgress",
            "repo": "repo-a",
            "type": "Code",
            "createdAt": NOW,
            "lifecycleId": "old-lifecycle",
        }
    )
    return document.model_copy(update=updates)


def _complete_effect(ref: TaskDocumentRef) -> TaskDocProjectionEffect:
    return TaskDocProjectionEffect(
        sprintTaskDocumentRef=ref,
        queueExisted=True,
        priorRevision=2,
        priorSourceFingerprint="a" * 64,
        invalidation=ProjectionInvalidationResult(outcome="persisted-empty", revision=3),
        rebuild=ProjectionRebuildResult(
            outcome="published",
            revision=4,
            sourceFingerprint="b" * 64,
            sourceClassification="active",
        ),
        rebuiltRevision=4,
    )


class _FakeStore:
    events: ClassVar[list[str]] = []

    def __init__(self, _root: Path, sprint_ref: TaskDocumentRef) -> None:
        self.sprint_ref = sprint_ref
        self.state_path = Path("/projection") / sprint_ref.path

    def exists(self) -> bool:
        return True

    def read_raw(self, *, timestamp: str) -> CloseoutQueueState:
        del timestamp
        return CloseoutQueueState(
            sprintTaskDocumentRef=self.sprint_ref,
            revision=2,
            serviceCondition="valid-built",
            sourceClassification="active",
            sourceFingerprint="a" * 64,
            updatedAt=NOW,
        )

    def invalidate(
        self,
        *,
        timestamp: str,
    ) -> tuple[CloseoutQueueState, ProjectionInvalidationResult]:
        del timestamp
        self.events.append(f"invalidate:{self.sprint_ref.key}")
        if self.sprint_ref == SPRINT_A:
            raise OSError("forced scope A failure")
        state = CloseoutQueueState(
            sprintTaskDocumentRef=self.sprint_ref,
            revision=3,
            serviceCondition="invalid-empty",
            updatedAt=NOW,
        )
        return state, ProjectionInvalidationResult(outcome="persisted-empty", revision=3)


class _PresenceFailureStore(_FakeStore):
    def exists(self) -> bool:
        if self.sprint_ref == SPRINT_A:
            raise OSError("forced presence failure")
        return super().exists()


class TaskFactPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        _FakeStore.events = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scope_preflight_precedes_task_first_fixed_order_invalidation(self) -> None:
        events: list[str] = []

        def publish() -> str:
            events.append("task-published")
            return "durable-task-result"

        def rebuild(
            _root: Path,
            receipt: ProjectionInvalidationReceipt,
            **_kwargs: object,
        ) -> TaskDocProjectionEffect:
            ref = receipt.sprint_ref
            events.append(f"rebuild:{ref.key}")
            return _complete_effect(ref)

        def scopes() -> tuple[TaskDocumentRef, ...]:
            events.append("scopes-resolved")
            return (SPRINT_B, SPRINT_A, SPRINT_B)

        with (
            mock.patch.object(publication, "CloseoutQueueStore", _FakeStore),
            mock.patch.object(
                publication,
                "rebuild_invalidated_closeout_projection",
                side_effect=rebuild,
            ),
        ):
            result = publication.publish_task_fact_mutation(
                self.root,
                "repo-a",
                validate=lambda: events.append("validated"),
                projection_scopes=scopes,
                publication=publish,
            )

        self.assertEqual(result.result, "durable-task-result")
        self.assertEqual(events[:3], ["validated", "scopes-resolved", "task-published"])
        self.assertEqual(
            _FakeStore.events,
            [f"invalidate:{SPRINT_A.key}", f"invalidate:{SPRINT_B.key}"],
        )
        self.assertEqual(events[3:], [f"rebuild:{SPRINT_B.key}"])
        self.assertEqual(len(result.projection_effects), 2)
        failed, completed = result.projection_effects
        self.assertEqual(failed.sprintTaskDocumentRef, SPRINT_A)
        self.assertEqual(failed.invalidation.outcome, "failed")
        self.assertEqual(failed.rebuild.outcome, "not-attempted")
        self.assertIn("closeout_queue(action='rebuild'", failed.nextAction or "")
        self.assertEqual(completed.sprintTaskDocumentRef, SPRINT_B)
        self.assertIsNone(completed.nextAction)

    def test_scope_refusal_happens_before_task_publication(self) -> None:
        writes = 0

        def publish() -> str:
            nonlocal writes
            writes += 1
            return "must-not-publish"

        with self.assertRaisesRegex(ValueError, "unclassified exact delta"):
            publication.publish_task_fact_mutation(
                self.root,
                "repo-a",
                validate=lambda: None,
                projection_scopes=lambda: (_ for _ in ()).throw(
                    ValueError("unclassified exact delta")
                ),
                publication=publish,
            )

        self.assertEqual(writes, 0)

    def test_empty_projection_scope_publishes_without_queue_churn(self) -> None:
        store = mock.Mock(side_effect=AssertionError("queue must not be opened"))
        with mock.patch.object(publication, "CloseoutQueueStore", store):
            result = publication.publish_task_fact_mutation(
                self.root,
                "repo-a",
                validate=lambda: None,
                projection_scopes=lambda: (),
                publication=lambda: "audit-published",
            )

        self.assertEqual(result.result, "audit-published")
        self.assertEqual(result.projection_effects, ())
        store.assert_not_called()

    def test_evidence_and_audit_only_delta_selects_no_task_driven_scope(self) -> None:
        original = _sprint_document()
        candidate = original.model_copy(
            update={
                "references": ["requirements/CCR-R04.md"],
                "statusNote": "Review evidence recorded.",
            }
        )

        scopes = resolve_projection_scope_union(
            self.root,
            "repo-a",
            (TaskDocScopeChange(SPRINT_A, original, candidate),),
        )

        self.assertEqual(scopes, ())

    def test_completion_delta_selects_the_affected_sprint(self) -> None:
        original = _sprint_document()
        candidate = original.model_copy(update={"status": "Completed"})

        scopes = resolve_projection_scope_union(
            self.root,
            "repo-a",
            (TaskDocScopeChange(SPRINT_A, original, candidate),),
        )

        self.assertEqual(scopes, (SPRINT_A,))

    def test_contract_lifecycle_restamp_selects_no_task_driven_scope(self) -> None:
        task_root = self.root / "tasks" / "repo-a" / "master"
        contract = cast(
            WorktreeContract,
            SimpleNamespace(
                coordination_root=self.root,
                repo_name="repo-a",
                task_root=task_root,
            ),
        )
        original = _leaf_document()
        candidate = original.model_copy(update={"lifecycleId": "new-lifecycle"})
        leaf_ref = TaskDocumentRef(repository="repo-a", path="master/01_leaf.json")
        topology = mock.Mock()
        topology.canonical_ref.return_value = leaf_ref
        topology.resolve.return_value = SimpleNamespace(document=original)

        with mock.patch.object(publication, "TaskDocumentTopology", return_value=topology):
            scopes = publication.contract_projection_scopes(contract, (candidate,))

        self.assertEqual(scopes, ())
        topology.resolve.assert_called_once_with(leaf_ref)

    def test_projection_failure_never_rolls_back_or_reinvokes_task_publication(self) -> None:
        writes = 0

        def publish() -> str:
            nonlocal writes
            writes += 1
            return "committed"

        with (
            mock.patch.object(publication, "CloseoutQueueStore", _FakeStore),
            mock.patch.object(
                publication,
                "rebuild_invalidated_closeout_projection",
                side_effect=RuntimeError("forced rebuild cut"),
            ),
        ):
            result = publication.publish_task_fact_mutation(
                self.root,
                "repo-a",
                validate=lambda: None,
                projection_scopes=lambda: (SPRINT_B,),
                publication=publish,
            )
        self.assertEqual((writes, result.result), (1, "committed"))
        effect = result.projection_effects[0]
        self.assertEqual(effect.invalidation.outcome, "persisted-empty")
        self.assertEqual(effect.rebuild.outcome, "source-unreadable")
        self.assertIsNotNone(effect.nextAction)

    def test_presence_failure_is_a_present_typed_effect_and_later_scopes_continue(self) -> None:
        with (
            mock.patch.object(publication, "CloseoutQueueStore", _PresenceFailureStore),
            mock.patch.object(
                publication,
                "rebuild_invalidated_closeout_projection",
                side_effect=lambda _root, receipt, **_kwargs: _complete_effect(receipt.sprint_ref),
            ),
        ):
            result = publication.publish_task_fact_mutation(
                self.root,
                "repo-a",
                validate=lambda: None,
                projection_scopes=lambda: (SPRINT_A, SPRINT_B),
                publication=lambda: "committed",
            )

        failed, completed = result.projection_effects
        self.assertEqual(result.result, "committed")
        self.assertTrue(failed.queueExisted)
        self.assertEqual(failed.invalidation.outcome, "failed")
        self.assertEqual(failed.rebuild.outcome, "not-attempted")
        self.assertEqual(completed.sprintTaskDocumentRef, SPRINT_B)

    def test_store_construction_failure_is_bounded_and_later_scopes_continue(self) -> None:
        def store_factory(root: Path, sprint_ref: TaskDocumentRef) -> _FakeStore:
            if sprint_ref == SPRINT_A:
                raise OSError("forced store construction failure")
            return _FakeStore(root, sprint_ref)

        with (
            mock.patch.object(publication, "CloseoutQueueStore", side_effect=store_factory),
            mock.patch.object(
                publication,
                "rebuild_invalidated_closeout_projection",
                side_effect=lambda _root, receipt, **_kwargs: _complete_effect(receipt.sprint_ref),
            ),
        ):
            result = publication.publish_task_fact_mutation(
                self.root,
                "repo-a",
                validate=lambda: None,
                projection_scopes=lambda: (SPRINT_A, SPRINT_B),
                publication=lambda: "committed",
            )

        self.assertEqual(result.result, "committed")
        self.assertEqual(result.projection_effects[0].invalidation.outcome, "failed")
        self.assertEqual(result.projection_effects[1].sprintTaskDocumentRef, SPRINT_B)

    def test_effect_model_requires_recovery_only_when_incomplete(self) -> None:
        complete = _complete_effect(SPRINT_A)
        self.assertIsNone(complete.nextAction)
        with self.assertRaises(ValueError):
            TaskDocProjectionEffect.model_validate(
                {**complete.model_dump(), "nextAction": "rebuild"}
            )
        incomplete = complete.model_copy(
            update={
                "rebuild": ProjectionRebuildResult(outcome="source-changed"),
                "rebuiltRevision": None,
                "nextAction": "closeout_queue(action='rebuild')",
            }
        )
        self.assertEqual(incomplete.rebuild.outcome, "source-changed")
