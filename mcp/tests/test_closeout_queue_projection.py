"""L3 source-census purity, drift fencing, and terminal-empty forcing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agents_remember.controlplane.closeout_queue_store import CloseoutQueueStore
from agents_remember.models.closeout.projection import CloseoutQueueState
from agents_remember.models.queue.closeout_queue import CloseoutQueueRequest
from agents_remember.tasks import SprintExecutionEdge, read_task_doc, write_task_doc
from agents_remember.worktrees.activation.atomic_series_activation import (
    activation_path,
    atomic_series_source_pair,
    publish_atomic_series_selection,
)
from agents_remember.worktrees.integration.closeout.curator_coherence import (
    require_current_curator_coherence,
)
from agents_remember.worktrees.queue.closeout_queue import QueueActor, closeout_queue_tool
from agents_remember.worktrees.worktree_contract import load_contract
from test_closeout_queue import LEAF_A, MASTER_A, MASTER_B, NOW, SPRINT, QueueFixture


class CloseoutProjectionCensusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = QueueFixture(Path(self.temporary.name), memory_mode="internal")
        self.actor = QueueActor(role="orchestrator", task_document_ref=SPRINT)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _rebuild(self) -> dict[str, Any]:
        return closeout_queue_tool(
            self.fixture.cfg,
            CloseoutQueueRequest(action="rebuild", sprint_task_document_ref=SPRINT),
            actor=self.actor,
            now=NOW,
        )

    def _assert_shared_topology_identity(
        self,
        fixture: QueueFixture,
        master: Any,
        projected: dict[str, Any],
    ) -> None:
        contract = load_contract(fixture.contracts[master].contract_path)
        door = contract.closeout_door
        assert door is not None
        coherence = require_current_curator_coherence(contract)
        self.assertEqual(
            coherence.record.taskTopologyFingerprint,
            door.taskTopologyFingerprint,
        )
        member = next(
            item for item in projected["members"] if item["owningMaster"] == master.model_dump()
        )
        self.assertNotIn("door-task-topology-stale", member["reasons"])

    def test_rebuild_uses_only_current_waiting_doors_not_old_rows(self) -> None:
        self.fixture.declare(MASTER_A)
        store = CloseoutQueueStore(self.fixture.coord, SPRINT)
        current = store.read_raw(timestamp=NOW)
        malicious = current.members[0].model_copy(
            update={
                "generationId": "9" * 64,
                "taskDocumentRef": self.fixture.leaf_refs[MASTER_B],
                "owningMaster": MASTER_B,
            }
        )
        store.state_path.write_text(
            current.model_copy(update={"members": [malicious]}).model_dump_json(indent=2),
            encoding="utf-8",
        )
        self.assertEqual(self.fixture.status()["state"], "invalid-empty")
        self.assertEqual(self.fixture.status()["members"], [])
        rebuilt = self._rebuild()
        members = rebuilt["members"]
        assert isinstance(members, list)
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["generationId"], current.members[0].generationId)
        self.assertEqual(members[0]["taskDocumentRef"], LEAF_A.model_dump())

    def test_readiness_change_invalidates_the_current_projection(self) -> None:
        self.fixture.declare(MASTER_A)
        before = self.fixture.status()
        master_path = self.fixture.tasks / "master-a" / "task.json"
        master = read_task_doc(master_path)
        row = master.subTasks[0].model_copy(update={"status": "Completed"})
        write_task_doc(master_path.parent, master.model_copy(update={"subTasks": [row]}))
        after = self.fixture.status()
        self.assertEqual(before["state"], "valid-built")
        self.assertEqual(after["state"], "invalid-empty")
        self.assertEqual(after["members"], [])
        self.assertIsNotNone(after["nextAction"])

    def test_display_and_audit_changes_do_not_invalidate_structural_currentness(self) -> None:
        self.fixture.declare(MASTER_A)
        before = self.fixture.status()
        sibling_path = self.fixture.tasks / "master-b" / "task.json"
        sibling = read_task_doc(sibling_path)
        write_task_doc(sibling_path.parent, sibling.model_copy(update={"title": "new sibling"}))
        leaf_path = self.fixture.tasks / "master-a" / "leaf-a.json"
        leaf = read_task_doc(leaf_path)
        write_task_doc(
            leaf_path.parent,
            leaf.model_copy(update={"createdAt": "2026-09-01T00:00:00+00:00"}),
        )
        after = self.fixture.status()
        self.assertEqual(after["state"], "valid-built")
        self.assertEqual(after["sourceFingerprint"], before["sourceFingerprint"])
        self.assertEqual(after["members"], before["members"])

    def test_member_topology_refusal_is_an_exact_unreadable_status_and_rebuild(self) -> None:
        self.fixture.declare(MASTER_A)
        leaf_path = self.fixture.tasks / "master-a" / "leaf-a.json"
        leaf = read_task_doc(leaf_path)
        write_task_doc(leaf_path.parent, leaf.model_copy(update={"id": "OTHER"}))

        status = self.fixture.status()
        rebuilt = self._rebuild()

        for result in (status, rebuilt):
            self.assertEqual(result["state"], "invalid-empty")
            self.assertIsNone(result["effectiveSourceFingerprint"])
            self.assertEqual(result["members"], [])
            self.assertEqual(len(result["sourceProblems"]), 1)
            self.assertEqual(
                result["sourceProblems"][0],
                {
                    "kind": "task",
                    "address": LEAF_A.key,
                    "state": "invalid",
                    "errorType": "semantic-topology-parent-binding-stem-only",
                    "repairAction": (
                        "candidate source or file stem matches without the candidate document ID"
                    ),
                },
            )

    def test_relevant_dependency_edge_invalidates_v2_identity(self) -> None:
        self.fixture.declare(MASTER_A)
        sprint_path = self.fixture.tasks / "sprint" / "task.json"
        sprint = read_task_doc(sprint_path)
        graph = sprint.executionGraph
        assert graph is not None
        changed_graph = graph.model_copy(
            update={
                "edges": [
                    SprintExecutionEdge(
                        predecessor=MASTER_B,
                        successor=MASTER_A,
                        reason="B now gates A.",
                    )
                ]
            }
        )
        write_task_doc(
            sprint_path.parent,
            sprint.model_copy(update={"executionGraph": changed_graph}),
        )

        self.assertEqual(self.fixture.status()["state"], "invalid-empty")
        rebuilt = self._rebuild()
        self.assertIn("door-task-topology-stale", rebuilt["members"][0]["reasons"])

    def test_graphless_atomic_route_builds_current_waiting_member_without_selection(
        self,
    ) -> None:
        fixture = QueueFixture(
            Path(self.temporary.name) / "graphless",
            atomic_a=True,
            memory_mode="external",
        )
        sprint_path = fixture.tasks / "sprint" / "task.json"
        sprint = read_task_doc(sprint_path)
        write_task_doc(
            sprint_path.parent,
            sprint.model_copy(update={"executionGraph": None}),
        )

        projected = fixture.declare(MASTER_A)

        self.assertEqual(projected["state"], "valid-built")
        self.assertEqual(projected["members"][0]["classification"], "waiting")
        self.assertEqual(projected["members"][0]["reasons"], ["atomic-series-not-selected"])
        self._assert_shared_topology_identity(fixture, MASTER_A, projected)

    def test_dag_route_shares_one_identity_across_coherence_door_and_queue(self) -> None:
        fixture = QueueFixture(
            Path(self.temporary.name) / "dag-shared-identity",
            memory_mode="external",
        )

        projected = fixture.declare(MASTER_A)

        self.assertEqual(projected["state"], "valid-built")
        self._assert_shared_topology_identity(fixture, MASTER_A, projected)

    def test_review_evidence_drift_changes_source_identity_and_blocks_member(self) -> None:
        self.fixture.declare(MASTER_A)
        contract = self.fixture.contracts[MASTER_A]
        report = contract.task_root / "notes" / "reports" / "leaf-a-review.md"
        report.write_text("# Review\n\nChanged after declaration.\n", encoding="utf-8")
        self.assertEqual(self.fixture.status()["state"], "invalid-empty")
        rebuilt = self._rebuild()
        member = rebuilt["members"][0]
        self.assertEqual(member["classification"], "blocked")
        self.assertIn("route-review-evidence-stale", member["reasons"])

    def test_grade_evidence_drift_is_part_of_the_source_fingerprint(self) -> None:
        self.fixture.declare(MASTER_A)
        grade = self.fixture.tasks / "sprint" / "grade.md"
        grade.write_text("# Replaced grade evidence\n", encoding="utf-8")
        self.assertEqual(self.fixture.status()["state"], "invalid-empty")
        rebuilt = self._rebuild()
        self.assertIn(
            "door-scheduling-provenance-stale",
            rebuilt["members"][0]["reasons"],
        )

    def test_present_nonregular_series_authority_is_not_projected_as_absent(self) -> None:
        self.fixture.declare(MASTER_A)
        path = self.fixture.tasks / "master-b" / "series-contract.md"
        path.unlink()
        path.symlink_to(self.fixture.tasks / "missing-series-contract.md")

        status = self.fixture.status()
        rebuilt = self._rebuild()

        self.assertEqual(status["state"], "invalid-empty")
        self.assertEqual(rebuilt["state"], "invalid-empty")
        self.assertTrue(any(problem["kind"] == "series" for problem in rebuilt["sourceProblems"]))

    def test_present_nonregular_leaf_door_is_not_projected_as_absent(self) -> None:
        self.fixture.declare(MASTER_A)
        path = self.fixture.contracts[MASTER_A].contract_path
        path.unlink()
        path.symlink_to(self.fixture.tasks / "missing-leaf-contract.md")

        status = self.fixture.status()
        rebuilt = self._rebuild()

        self.assertEqual(status["state"], "invalid-empty")
        self.assertEqual(rebuilt["state"], "invalid-empty")
        self.assertTrue(
            any(
                problem["kind"] == "door"
                and problem["errorType"] == "enclosure-contract-nonregular"
                for problem in rebuilt["sourceProblems"]
            )
        )

    def test_leaf_door_ancestor_symlink_cannot_redirect_projection_authority(self) -> None:
        self.fixture.declare(MASTER_A)
        contract_path = self.fixture.contracts[MASTER_A].contract_path
        outside = self.fixture.root / "outside-enclosure"
        outside.mkdir()
        redirected_contract = outside / "series-contract.md"
        redirected_contract.write_bytes(contract_path.read_bytes())
        redirect_parent = self.fixture.coord / "redirected-enclosure"
        redirect_parent.symlink_to(outside, target_is_directory=True)
        leaf_path = self.fixture.tasks / "master-a" / "leaf-a.json"
        leaf = read_task_doc(leaf_path)
        redirected = leaf.model_copy(
            update={
                "enclosures": [
                    enclosure.model_copy(
                        update={
                            "enclosurePath": (redirect_parent / "series-contract.md").as_posix()
                        }
                    )
                    for enclosure in leaf.enclosures
                ]
            }
        )
        write_task_doc(leaf_path.parent, redirected)

        status = self.fixture.status()
        rebuilt = self._rebuild()

        self.assertEqual(status["state"], "invalid-empty")
        self.assertEqual(rebuilt["state"], "invalid-empty")
        self.assertEqual(rebuilt["members"], [])
        self.assertTrue(
            any(
                problem["kind"] == "door"
                and problem["errorType"] == "enclosure-contract-ancestor-noncanonical"
                for problem in rebuilt["sourceProblems"]
            )
        )

    def test_completed_sprint_rebuilds_as_valid_terminal_empty(self) -> None:
        self.fixture.declare(MASTER_A)
        sprint_path = self.fixture.tasks / "sprint" / "task.json"
        sprint = read_task_doc(sprint_path)
        write_task_doc(sprint_path.parent, sprint.model_copy(update={"status": "Completed"}))
        rebuilt = self._rebuild()
        self.assertEqual(rebuilt["state"], "valid-built")
        self.assertEqual(rebuilt["sourceClassification"], "terminal")
        self.assertEqual(rebuilt["members"], [])
        stored = CloseoutQueueState.model_validate_json(
            CloseoutQueueStore(self.fixture.coord, SPRINT).state_path.read_text(encoding="utf-8")
        )
        self.assertEqual(stored.serviceCondition, "valid-built")
        self.assertEqual(stored.sourceClassification, "terminal")

    def test_multiple_live_atomic_series_are_valid_active_paused_waiting_candidates(self) -> None:
        fixture = QueueFixture(
            Path(self.temporary.name) / "two-atomic",
            atomic_a=True,
            atomic_b=True,
            memory_mode="internal",
        )
        actor = QueueActor(role="orchestrator", task_document_ref=SPRINT)
        fixture.declare(MASTER_A)
        vacant = fixture.declare(MASTER_B)
        self.assertEqual(vacant["state"], "valid-built")
        self.assertTrue(
            all(
                member["classification"] == "waiting"
                and "atomic-series-not-selected" in member["reasons"]
                for member in vacant["members"]
            )
        )

        series_a = fixture.tasks / "master-a" / "series-contract.md"
        selected_a = publish_atomic_series_selection(
            load_contract(series_a),
            "active",
            timestamp=NOW,
        )
        self.assertEqual(fixture.status()["state"], "invalid-empty")
        rebuilt_a = closeout_queue_tool(
            fixture.cfg,
            CloseoutQueueRequest(action="rebuild", sprint_task_document_ref=SPRINT),
            actor=actor,
            now=NOW,
        )
        by_master = {member["owningMaster"]["path"]: member for member in rebuilt_a["members"]}
        self.assertEqual(by_master[MASTER_A.path]["classification"], "ready")
        self.assertIn(
            f"atomic-series-paused-by: {MASTER_A.key}",
            by_master[MASTER_B.path]["reasons"],
        )

        publish_atomic_series_selection(
            load_contract(fixture.tasks / "master-b" / "series-contract.md"),
            "active",
            timestamp="2026-08-26T00:00:01+00:00",
        )
        self.assertEqual(fixture.status()["state"], "invalid-empty")
        rebuilt_b = closeout_queue_tool(
            fixture.cfg,
            CloseoutQueueRequest(action="rebuild", sprint_task_document_ref=SPRINT),
            actor=actor,
            now=NOW,
        )
        by_master = {member["owningMaster"]["path"]: member for member in rebuilt_b["members"]}
        self.assertEqual(by_master[MASTER_B.path]["classification"], "ready")
        self.assertIn(
            f"atomic-series-paused-by: {MASTER_B.key}",
            by_master[MASTER_A.path]["reasons"],
        )
        self.assertEqual(selected_a.selected_master, MASTER_A)

    def test_malformed_activation_invalidates_only_projection_and_names_selection_repair(
        self,
    ) -> None:
        fixture = QueueFixture(
            Path(self.temporary.name) / "malformed-activation",
            atomic_b=True,
            memory_mode="internal",
        )
        fixture.declare(MASTER_B)
        series = load_contract(fixture.tasks / "master-b" / "series-contract.md")
        path = activation_path(fixture.coord, atomic_series_source_pair(series))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{malformed", encoding="utf-8")

        result = fixture.status()

        self.assertEqual(result["state"], "invalid-empty")
        problem = next(
            item for item in result["sourceProblems"] if item["address"] == path.as_posix()
        )
        self.assertIn("dispatch_agent", problem["repairAction"])
