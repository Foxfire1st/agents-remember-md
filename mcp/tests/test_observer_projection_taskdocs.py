from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import (
    AcceptanceObligationQuestion,
    ApprovedRequirementPacketRef,
)
from agents_remember.observer.projection import SeriesNode, TaskDocNode
from agents_remember.observer.reducer import AnalyticalInputs, build_analytics
from agents_remember.serving.projections.snapshots import (
    read_enclosures,
    read_series_documents,
    read_task_documents,
)
from agents_remember.serving.projections.snapshots_impl._common import (
    TASK_DOCUMENT_SUMMARY_LIMIT,
)
from agents_remember.serving.projections.snapshots_impl._task_documents import (
    _task_doc_body_revision,
)
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    WorktreeContract,
    default_contract,
    write_contract,
)
from test_observer_projection import FRESH


class TaskDocumentsReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.coord = Path(self._dir.name)

    def _doc(self, **over: object) -> TaskDocument:
        base: dict[str, object] = {
            "id": "D",
            "slug": "task",
            "title": "Demo",
            "kind": "light",
            "repo": "repo-a",
            "createdAt": "2026-01-01T00:00",
        }
        base.update(over)
        return TaskDocument.model_validate(base)

    @staticmethod
    def _write_addressable_contract(contract: WorktreeContract) -> None:
        write_contract(contract.contract_path, contract)
        publish_new_lifecycle_operation_location(
            contract,
            contract_text=contract.contract_path.read_text(encoding="utf-8"),
        )

    def test_reads_lifecycle_keyed_progress(self) -> None:
        root = self.coord / "tasks" / "repo-a" / "demo"
        write_task_doc(
            root,
            self._doc(
                lifecycleId="LC1",
                steps=[
                    {"id": "S1", "title": "a", "status": "done"},
                    {"id": "S2", "title": "b", "status": "inProgress"},
                ],
            ),
        )
        nodes = read_task_documents(self.coord, enclosures=[], now=FRESH)
        self.assertEqual(len(nodes), 1)
        self.assertEqual(
            (nodes[0].lifecycleId, nodes[0].stepsDone, nodes[0].stepsTotal, nodes[0].currentStep),
            ("LC1", 1, 2, "S2 — b"),
        )
        self.assertEqual(nodes[0].createdAt, "2026-01-01T00:00")

    def test_projects_intentional_skip_for_parent_and_nested_step(self) -> None:
        root = self.coord / "tasks" / "repo-a" / "demo"
        disposition = {
            "kind": "intentionalSkip",
            "reason": "Superseded.",
            "recordedAt": "2026-08-03T12:00:00+00:00",
            "recordedVia": "task_doc.skip_step",
            "lifecycleId": "LC1",
        }
        write_task_doc(
            root,
            self._doc(
                lifecycleId="LC1",
                steps=[
                    {
                        "id": "S1",
                        "title": "Parent",
                        "status": "done",
                        "disposition": disposition,
                        "substeps": [
                            {
                                "id": "C1",
                                "title": "Child",
                                "status": "done",
                                "disposition": disposition,
                            }
                        ],
                    }
                ],
            ),
        )
        [node] = read_task_documents(self.coord, enclosures=[], now=FRESH)
        self.assertEqual(node.stepsDone, 2)
        self.assertEqual(node.stepsTotal, 2)
        parent_disposition = node.steps[0].disposition
        child_disposition = node.steps[0].substeps[0].disposition
        assert parent_disposition is not None
        assert child_disposition is not None
        self.assertEqual(parent_disposition.reason, "Superseded.")
        self.assertEqual(child_disposition.recordedVia, "task_doc.skip_step")

    def test_projects_docs_without_lifecycle_and_skips_non_task_json(self) -> None:
        root = self.coord / "tasks" / "repo-a" / "demo"
        write_task_doc(root, self._doc(slug="03c_x", kind="subTask"))  # no lifecycleId
        (root / "other.json").write_text('{"schema": "other/v1"}', encoding="utf-8")
        notes = root / "notes"
        write_task_doc(
            notes,
            self._doc(id="SUPERSEDED", slug="superseded-master", kind="master"),
        )
        nodes = read_task_documents(self.coord, enclosures=[], now=FRESH)
        self.assertEqual(len(nodes), 1)
        self.assertIsNone(nodes[0].lifecycleId)
        self.assertEqual(nodes[0].docPath, (root / "03c_x.json").as_posix())

    def test_exposes_orchestrates_on_the_task_doc_node(self) -> None:
        # The orchestration-command relation rides the projection so the dashboard can derive
        # the orchestration > master > leaf hierarchy; docs without the field project [].
        sprint_root = self.coord / "tasks" / "repo-a" / "sprint-02"
        write_task_doc(
            sprint_root,
            self._doc(
                id="SPRINT-02",
                slug="task",
                kind="master",
                title="Sprint 02",
                orchestrates=["260706_management-repo"],
            ),
        )
        plain_root = self.coord / "tasks" / "repo-a" / "demo"
        write_task_doc(plain_root, self._doc())
        nodes = read_task_documents(self.coord, enclosures=[], now=FRESH)
        by_id = {node.id: node for node in nodes}
        self.assertEqual(by_id["SPRINT-02"].orchestrates, ["260706_management-repo"])
        self.assertEqual(by_id["D"].orchestrates, [])

    def test_projects_sprint_master_ref_rows_and_seats(self) -> None:
        # L14-R2/R3: a sprint's typed masterRef rows and first-class seats ride the projection
        # (the dashboard's sprint -> master drill-down and seat structure); ordinary rows and
        # docs carry neither.
        write_task_doc(
            self.coord / "tasks" / "repo-a" / "sprint",
            self._doc(
                id="SPRINT",
                slug="task",
                kind="master",
                title="Sprint",
                orchestrates=["master"],
                subTasks=[
                    {
                        "number": "1",
                        "name": "Commanded master",
                        "status": "inProgress",
                        "masterRef": {"repository": "repo-a", "path": "master/task.json"},
                    }
                ],
                seats=[
                    {
                        "role": "orchestrator",
                        "label": "Orch",
                        "identity": "agent-1",
                        "state": "active",
                    },
                    {"role": "strategist"},
                ],
            ),
        )
        write_task_doc(self.coord / "tasks" / "repo-a" / "series", self._master())
        write_task_doc(self.coord / "tasks" / "repo-a" / "demo", self._doc())

        nodes = read_task_documents(self.coord, enclosures=[], now=FRESH)

        by_id = {node.id: node for node in nodes}
        sprint = by_id["SPRINT"]
        self.assertEqual(
            sprint.subTasks[0].masterRef,
            TaskDocumentRef(repository="repo-a", path="master/task.json"),
        )
        self.assertEqual(
            [(seat.role, seat.label, seat.identity, seat.state) for seat in sprint.seats],
            [("orchestrator", "Orch", "agent-1", "active"), ("strategist", "", None, "planned")],
        )
        self.assertIsNone(by_id["series"].subTasks[0].masterRef)
        self.assertEqual(by_id["series"].seats, [])
        self.assertEqual(by_id["D"].seats, [])

    def test_body_revision_covers_sprint_structure(self) -> None:
        # An open reader renders the fetched body and refetches only when bodyRevision moves, so
        # the revision must cover the sprint's seats and typed masterRef rows (L14-R2) — an
        # already-open sprint doc would otherwise never pick up a linkage or seat edit.
        root = self.coord / "tasks" / "repo-a" / "sprint"
        base: dict[str, object] = {
            "id": "SPRINT",
            "slug": "task",
            "kind": "master",
            "title": "Sprint",
            "orchestrates": ["master"],
        }
        write_task_doc(root, self._doc(**base))
        first = read_task_documents(self.coord, enclosures=[], now=FRESH)[0].bodyRevision
        write_task_doc(root, self._doc(**base, seats=[{"role": "orchestrator"}]))
        second = read_task_documents(self.coord, enclosures=[], now=FRESH)[0].bodyRevision
        write_task_doc(
            root,
            self._doc(
                **base,
                subTasks=[
                    {
                        "number": "1",
                        "name": "Commanded master",
                        "status": "planning",
                        "masterRef": {"repository": "repo-a", "path": "master/task.json"},
                    }
                ],
            ),
        )
        third = read_task_documents(self.coord, enclosures=[], now=FRESH)[0].bodyRevision
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)

    def test_body_revision_canonicalizes_typed_intent_slots(self) -> None:
        requirement = ApprovedRequirementPacketRef(
            path="requirements/R1-v1.md",
            stableId="R1",
            version="v1",
        )
        question = AcceptanceObligationQuestion(id="Q1", question="Is the proof exact?")
        document = self._doc(
            requirements=["plain requirement", requirement],
            openQuestions=["plain question", question],
        )

        revision = _task_doc_body_revision(document)
        self.assertEqual(revision, _task_doc_body_revision(document))
        self.assertNotEqual(
            revision,
            _task_doc_body_revision(
                document.model_copy(
                    update={
                        "requirements": [
                            "plain requirement",
                            requirement.model_copy(update={"version": "v2"}),
                        ]
                    }
                )
            ),
        )
        self.assertNotEqual(
            revision,
            _task_doc_body_revision(
                document.model_copy(
                    update={
                        "openQuestions": [
                            "plain question",
                            question.model_copy(update={"question": "Is the proof current?"}),
                        ]
                    }
                )
            ),
        )

    def test_summary_limit_never_evicts_task_root_grouping_authorities(self) -> None:
        sprint_root = self.coord / "tasks" / "repo-a" / "sprint"
        master_root = self.coord / "tasks" / "repo-a" / "master"
        write_task_doc(
            sprint_root,
            self._doc(
                id="SPRINT",
                slug="task",
                kind="master",
                title="Sprint",
                orchestrates=["master"],
            ),
        )
        write_task_doc(
            master_root,
            self._doc(id="MASTER", slug="task", kind="master", title="Master"),
        )
        leaves_root = self.coord / "tasks" / "repo-a" / "leaves"
        for index in range(TASK_DOCUMENT_SUMMARY_LIMIT + 1):
            write_task_doc(
                leaves_root,
                self._doc(
                    id=f"LEAF-{index:03d}",
                    slug=f"leaf-{index:03d}",
                    kind="subTask",
                    title=f"Leaf {index:03d}",
                ),
            )

        nodes = read_task_documents(self.coord, enclosures=[], now=FRESH)

        by_id = {node.id: node for node in nodes}
        self.assertEqual(len(nodes), TASK_DOCUMENT_SUMMARY_LIMIT)
        self.assertIn("SPRINT", by_id)
        self.assertIn("MASTER", by_id)
        self.assertEqual(by_id["SPRINT"].orchestrates, ["master"])
        self.assertEqual(
            sum(node.id.startswith("LEAF-") for node in nodes),
            TASK_DOCUMENT_SUMMARY_LIMIT - 2,
        )

    def test_leaf_contract_alone_is_not_a_task_document(self) -> None:
        contract = default_contract(
            ContractTask(
                name="demo",
                repo_name="repo-a",
                coordination_root=self.coord,
                workflow_kind="light-task",
                memory_mode="disabled",
            ),
            leaf=LeafIdentity(worktree_name="01_leaf-work", lifecycle_id="LC-LEAF"),
            code=RepoBranchPlan(
                repo_path=self.coord / "repos" / "repo-a",
                source_branch="ar/demo",
                work_branch="ar/demo-leaf",
                base_commit="abc123",
            ),
        )
        self._write_addressable_contract(contract)

        self.assertEqual(
            read_task_documents(self.coord, enclosures=read_enclosures(self.coord), now=FRESH),
            [],
        )

    def test_resolves_leaf_doc_lifecycle_from_matching_enclosure_leaf_id(self) -> None:
        root = self.coord / "tasks" / "repo-a" / "demo"
        leaf_id = "17_task-reader-top-progress-and-master-content"
        contract = default_contract(
            ContractTask(
                name="demo",
                repo_name="repo-a",
                coordination_root=self.coord,
                workflow_kind="light-task",
                memory_mode="disabled",
            ),
            leaf=LeafIdentity(worktree_name=leaf_id, leaf_id=leaf_id, lifecycle_id="LC-LEAF"),
            code=RepoBranchPlan(
                repo_path=self.coord / "repos" / "repo-a",
                source_branch="ar/demo",
                work_branch="ar/demo-leaf",
                base_commit="abc123",
            ),
        )
        self._write_addressable_contract(contract)
        write_task_doc(
            root,
            self._doc(
                slug=leaf_id,
                kind="subTask",
                steps=[{"id": "S1", "title": "a", "status": "inProgress"}],
            ),
        )

        [node] = read_task_documents(self.coord, enclosures=read_enclosures(self.coord), now=FRESH)

        self.assertEqual(node.lifecycleId, "LC-LEAF")
        self.assertEqual(node.docPath, (root / f"{leaf_id}.json").as_posix())

    def test_resolves_leaf_doc_lifecycle_from_doc_id_case_insensitively(self) -> None:
        # The real-world series shape: the enclosure leaf id is the lowercase
        # enclosures/ directory name ("260628-l7"), the doc slug is a numbered filename that never
        # matches it, the doc id is uppercase ("260628-L7"), and the doc carries no lifecycleId and
        # no enclosures[] refs. The doc must still bind to the enclosure's lifecycle.
        root = self.coord / "tasks" / "repo-a" / "demo"
        contract = default_contract(
            ContractTask(
                name="demo",
                repo_name="repo-a",
                coordination_root=self.coord,
                workflow_kind="light-task",
                memory_mode="disabled",
            ),
            leaf=LeafIdentity(
                worktree_name="cgc-dependency-command-repair",
                leaf_id="260628-l7",
                lifecycle_id="LC-LEAF-CASE",
            ),
            code=RepoBranchPlan(
                repo_path=self.coord / "repos" / "repo-a",
                source_branch="ar/demo",
                work_branch="ar/demo-leaf",
                base_commit="abc123",
            ),
        )
        self._write_addressable_contract(contract)
        write_task_doc(
            root,
            self._doc(
                id="260628-L7",
                slug="07_cgc-dependency-command-repair",
                kind="subTask",
                steps=[{"id": "S1", "title": "a", "status": "inProgress"}],
            ),
        )

        [node] = read_task_documents(self.coord, enclosures=read_enclosures(self.coord), now=FRESH)

        self.assertEqual(node.lifecycleId, "LC-LEAF-CASE")
        self.assertEqual(node.id, "260628-L7")

    def _master(self) -> TaskDocument:
        return TaskDocument.model_validate(
            {
                "id": "series",
                "slug": "series",
                "title": "Series",
                "kind": "master",
                "repo": "repo-a",
                "createdAt": "2026-01-01T00:00",
                "subTasks": [{"number": "1", "name": "A", "status": "inProgress"}],
                "sections": [{"kind": "subTasks", "heading": "Sub-tasks"}],
            }
        )

    def test_master_without_a_lifecycle_projects_as_task_document(self) -> None:
        root = self.coord / "tasks" / "repo-a" / "series"
        write_task_doc(root, self._master())
        [node] = read_task_documents(self.coord, enclosures=[], now=FRESH)
        self.assertIsNone(node.lifecycleId)
        self.assertEqual(node.kind, "master")
        self.assertEqual(node.title, "Series")

    def test_master_stays_on_series_surface(self) -> None:
        root = self.coord / "tasks" / "repo-a" / "series"
        write_task_doc(root, self._master())
        [task_node] = read_task_documents(self.coord, enclosures=[], now=FRESH)
        self.assertEqual(task_node.kind, "master")
        [series] = read_series_documents(self.coord, now=FRESH)
        self.assertEqual(series.seriesId, "series")
        self.assertEqual([ref.number for ref in series.subTasks], ["1"])
        # The always-on series surface is a bounded summary; authored sections are fetched
        # through the task-document body endpoint.
        self.assertEqual(series.sections, [])

    def test_discarded_planning_history_is_distinct_in_task_and_series_metrics(self) -> None:
        root = self.coord / "tasks" / "repo-a" / "series"
        data = self._master().model_dump(by_alias=True)
        data["discardedSubTasks"] = [
            {
                "number": "2",
                "name": "Never started",
                "file": "02_never_started.md",
                "scope": "retired planning work",
                "disposition": "discard-unstarted",
                "reason": "No implementation was needed",
                "discardedAt": "2026-08-24T12:00:00+00:00",
                "proof": {
                    "version": "task-unstarted-evidence/v1",
                    "taskDocumentRef": {
                        "repository": "repo-a",
                        "path": "series/02_never_started.json",
                    },
                    "taskState": "planning-unstarted",
                    "enclosureState": "absent",
                    "locatorState": "absent",
                    "doorState": "absent",
                    "operationState": "absent",
                    "seatState": "absent",
                    "reviewState": "absent",
                    "commitState": "absent",
                    "childJson": {"state": "missing"},
                    "childMarkdown": {"state": "missing"},
                    "fingerprint": "a" * 64,
                },
            }
        ]
        write_task_doc(root, TaskDocument.model_validate(data))

        [task_node] = read_task_documents(self.coord, enclosures=[], now=FRESH)
        [series] = read_series_documents(self.coord, now=FRESH)

        self.assertEqual(task_node.discardedCount, 1)
        self.assertIsNotNone(task_node.discardedSubTasks)
        assert task_node.discardedSubTasks is not None
        self.assertEqual(task_node.discardedSubTasks[0].number, "2")
        self.assertEqual(series.discardedCount, 1)
        self.assertEqual(series.discardedSubTasks[0].reason, "No implementation was needed")
        self.assertEqual((series.doneCount, series.totalCount), (0, 1))
        self.assertEqual([row.number for row in series.subTasks], ["1"])

    def test_nested_masters_stay_on_series_surface(self) -> None:
        parent_dir = self.coord / "tasks" / "repo-a" / "parent"
        child_dir = self.coord / "tasks" / "repo-a" / "child"
        write_task_doc(
            parent_dir,
            TaskDocument.model_validate(
                {
                    "id": "p",
                    "slug": "parent",
                    "title": "Parent",
                    "kind": "master",
                    "repo": "repo-a",
                    "createdAt": "2026-01-01T00:00",
                    "subTasks": [
                        {
                            "number": "06",
                            "name": "Child series",
                            "file": "../child/task.md",
                            "status": "inProgress",
                        }
                    ],
                }
            ),
        )
        write_task_doc(
            child_dir,
            TaskDocument.model_validate(
                {
                    "id": "c",
                    "slug": "child",
                    "title": "Child",
                    "kind": "master",
                    "repo": "repo-a",
                    "createdAt": "2026-01-01T00:00",
                    "master": "../parent/task.md",
                    "subTasks": [{"number": "1", "name": "A", "status": "inProgress"}],
                }
            ),
        )
        task_nodes = sorted(
            read_task_documents(self.coord, enclosures=[], now=FRESH),
            key=lambda node: node.title,
        )
        self.assertEqual([node.title for node in task_nodes], ["Child", "Parent"])
        nodes = sorted(read_series_documents(self.coord, now=FRESH), key=lambda node: node.seriesId)
        self.assertEqual([node.seriesId for node in nodes], ["child", "parent"])

    def test_missing_tasks_dir_is_empty(self) -> None:
        self.assertEqual(read_task_documents(self.coord / "nope", enclosures=[], now=FRESH), [])

    def test_archived_task_documents_are_not_projected(self) -> None:
        active = self.coord / "tasks" / "repo-a" / "active"
        archived = self.coord / "tasks" / "repo-a" / "0_archive" / "archived"
        write_task_doc(active, self._doc(slug="active", status="Completed"))
        write_task_doc(archived, self._doc(slug="archived", status="Completed"))

        nodes = read_task_documents(self.coord, enclosures=[], now=FRESH)

        self.assertEqual([node.title for node in nodes], ["Demo"])
        self.assertEqual(nodes[0].status, "Completed")

    def test_build_analytics_includes_task_documents(self) -> None:
        node = TaskDocNode(
            id="1",
            lifecycleId="LC1",
            repository="repo-a",
            title="t",
            status="planning",
            kind="light",
            docPath="p",
        )
        analytics = build_analytics(
            AnalyticalInputs(
                drift_snapshots=[],
                sidecar_staleness=[],
                setup_summaries=[],
                setup_progress=[],
                route_coverage=[],
                tool_reports=[],
                ledgers=[],
                task_documents=[node],
            ),
        )
        self.assertEqual(len(analytics.taskDocuments), 1)
        self.assertEqual(analytics.taskDocuments[0].lifecycleId, "LC1")

    def test_read_series_documents_projects_master(self) -> None:
        # The master is a checklist: each subtask is one checkbox; doneCount = declared
        # Completed subtasks, totalCount = number of subtasks. Full prose/decisions are omitted
        # from the always-on broadcast and fetched through the task-document body endpoint.
        root = self.coord / "tasks" / "repo-a" / "series-x"
        master = TaskDocument.model_validate(
            {
                "id": "series-x",
                "slug": "series-x",
                "title": "Series X",
                "kind": "master",
                "status": "inProgress",
                "repo": "repo-a",
                "createdAt": "2026-01-01T00:00",
                "objective": "Series X objective",
                "subTasks": [
                    {"number": "01", "name": "alpha", "status": "Completed"},
                    {"number": "02", "name": "beta", "status": "Completed"},
                    {"number": "03", "name": "gamma", "status": "inProgress"},
                ],
                "sections": [{"kind": "freeform", "heading": "Objective", "body": "the series"}],
                "decisions": [{"at": "2026-01-01T00:00", "decision": "d", "rationale": "r"}],
            }
        )
        write_task_doc(root, master)
        nodes = read_series_documents(self.coord, now=FRESH)
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node.seriesId, "series-x")
        self.assertEqual(node.objective, "")
        self.assertEqual((node.doneCount, node.totalCount), (2, 3))
        self.assertEqual(
            [(s.number, s.status) for s in node.subTasks],
            [("01", "Completed"), ("02", "Completed"), ("03", "inProgress")],
        )
        self.assertEqual(node.sections, [])
        self.assertEqual(node.decisions, [])

    def test_read_series_documents_orders_subtasks_by_leaf_creation(self) -> None:
        root = self.coord / "tasks" / "repo-a" / "series-z"
        write_task_doc(
            root,
            TaskDocument.model_validate(
                {
                    "id": "series-z",
                    "slug": "series-z",
                    "title": "Series Z",
                    "kind": "master",
                    "repo": "repo-a",
                    "createdAt": "2026-01-01T00:00",
                    "subTasks": [
                        {
                            "number": "99",
                            "name": "Alpha later",
                            "file": "alpha_later.md",
                            "status": "inProgress",
                        },
                        {
                            "number": "01",
                            "name": "Zulu earlier",
                            "file": "zulu_earlier.md",
                            "status": "planning",
                        },
                    ],
                }
            ),
        )
        write_task_doc(
            root,
            self._doc(
                id="alpha",
                slug="alpha_later",
                kind="subTask",
                title="Alpha later",
                createdAt="2026-01-03T00:00",
            ),
        )
        write_task_doc(
            root,
            self._doc(
                id="zulu",
                slug="zulu_earlier",
                kind="subTask",
                title="Zulu earlier",
                createdAt="2026-01-01T00:00",
            ),
        )

        [node] = read_series_documents(self.coord, now=FRESH)

        self.assertEqual(
            [(sub.name, sub.createdAt) for sub in node.subTasks],
            [
                ("Zulu earlier", "2026-01-01T00:00"),
                ("Alpha later", "2026-01-03T00:00"),
            ],
        )

    def test_read_series_documents_skips_leaf_docs(self) -> None:
        root = self.coord / "tasks" / "repo-a" / "demo"
        write_task_doc(root, self._doc(slug="03c_x", kind="subTask"))  # a leaf, not a master
        self.assertEqual(read_series_documents(self.coord, now=FRESH), [])

    def test_declared_subtask_status_is_authoritative_over_leaf_steps(self) -> None:
        # A subtask marked Completed in the master counts as done even if its own leaf doc
        # still has open steps -- series_done reads the declared status, never leaf steps.
        write_task_doc(
            self.coord / "tasks" / "repo-a" / "series-y",
            TaskDocument.model_validate(
                {
                    "id": "series-y",
                    "slug": "series-y",
                    "title": "Series Y",
                    "kind": "master",
                    "repo": "repo-a",
                    "createdAt": "2026-01-01T00:00",
                    "subTasks": [{"number": "01", "name": "alpha", "status": "Completed"}],
                }
            ),
        )
        # the slice's own leaf doc still has an open step
        write_task_doc(
            self.coord / "tasks" / "repo-a" / "slice-01",
            self._doc(
                slug="01_alpha",
                kind="subTask",
                lifecycleId="LC9",
                steps=[{"id": "S1", "title": "x", "status": "inProgress"}],
            ),
        )
        [node] = read_series_documents(self.coord, now=FRESH)
        self.assertEqual((node.doneCount, node.totalCount), (1, 1))  # declared Completed wins

    def test_read_series_documents_missing_tasks_dir_is_empty(self) -> None:
        self.assertEqual(read_series_documents(self.coord / "nope", now=FRESH), [])

    def test_build_analytics_includes_series(self) -> None:
        node = SeriesNode(
            seriesId="s",
            repository="repo-a",
            title="t",
            status="planning",
            docPath="p",
            doneCount=1,
            totalCount=2,
        )
        analytics = build_analytics(
            AnalyticalInputs(
                drift_snapshots=[],
                sidecar_staleness=[],
                setup_summaries=[],
                setup_progress=[],
                route_coverage=[],
                tool_reports=[],
                ledgers=[],
            ),
            series=[node],
        )
        self.assertEqual(len(analytics.series), 1)
        self.assertEqual(analytics.series[0].seriesId, "s")
