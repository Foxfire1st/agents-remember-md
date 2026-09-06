"""Exact JSON/Markdown CAS forcing for special structural task-doc writers."""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest import mock

import agents_remember.application.task_docs.task_doc_publication as task_publication
import agents_remember.application.task_docs.task_doc_tools as task_doc_tools_module
import agents_remember.application.task_docs.task_sprint_linkage as sprint_linkage
import test_task_sprint_linkage as fixture_mod
from agents_remember.application.task_docs.task_doc_publication import (
    TaskDocPublication,
    TaskDocPublicationConflict,
    TaskDocPublicationTransaction,
)
from agents_remember.application.task_docs.task_doc_tools import (
    TaskDocEdit,
    TaskDocTarget,
    task_doc_tool,
)
from agents_remember.tasks import SubTaskRef
from test_task_execution_topology import MASTER_A, MASTER_C


class TaskDocStructuralPublicationL2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = fixture_mod.SprintLinkageTests()
        self.owner.setUp()

    def tearDown(self) -> None:
        try:
            self.owner.doCleanups()
        finally:
            self.owner.tearDown()

    def _assert_attach_source_side_drift(self, side: str) -> None:
        self.owner._graph_ful_sprint()
        self.owner._write_master(MASTER_C)
        sprint_json = self.owner.tasks / "sprint" / "task.json"
        sprint_markdown = sprint_json.with_suffix(".md")
        master_json = self.owner.tasks / "master-c" / "task.json"
        master_markdown = master_json.with_suffix(".md")
        protected = {
            sprint_json: sprint_json.read_bytes(),
            sprint_markdown: sprint_markdown.read_bytes(),
            master_json if side == "markdown" else master_markdown: (
                master_json if side == "markdown" else master_markdown
            ).read_bytes(),
        }
        real_publish = task_publication.publish_task_doc_transaction_and_refresh

        def drift_before_publication(transaction: TaskDocPublicationTransaction):
            source = next(
                row for row in transaction.source_snapshots if row.json_path == master_json
            )
            target = source.json_path if side == "json" else source.markdown_path
            target.write_bytes(target.read_bytes() + b"\nexternal-task-authoring-drift\n")
            return real_publish(transaction)

        with (
            mock.patch.object(
                sprint_linkage,
                "publish_task_doc_transaction_and_refresh",
                side_effect=drift_before_publication,
            ),
            self.assertRaises(TaskDocPublicationConflict),
        ):
            self.owner._attach(
                {
                    "masterRef": MASTER_C.model_dump(),
                    "number": "M3",
                    "executionNature": "organizational",
                    "judgmentId": "J-1",
                }
            )
        self.assertEqual(
            {path: path.read_bytes() for path in protected},
            protected,
            "CAS refusal must not publish either selected/affected document",
        )

    def test_attach_refuses_json_side_drift_across_selected_master_batch(self) -> None:
        self._assert_attach_source_side_drift("json")

    def test_attach_refuses_markdown_side_drift_across_selected_master_batch(self) -> None:
        self._assert_attach_source_side_drift("markdown")


class TaskDocDetachAbsencePublicationL2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.owner = fixture_mod.SprintLinkageTests()
        self.owner.setUp()
        self.owner._write_master(MASTER_A, nature="organizational")
        self.owner._write_sprint(
            graph=None,
            orchestrates=["master-a", "master-c"],
            rows=[
                SubTaskRef(number="M1", name="a", masterRef=MASTER_A),
                SubTaskRef(number="M3", name="c", masterRef=MASTER_C),
            ],
        )

    def tearDown(self) -> None:
        try:
            self.owner.doCleanups()
        finally:
            self.owner.tearDown()

    def _detach(self) -> dict[str, object]:
        return self.owner._detach({"masterRef": MASTER_C.model_dump()})

    def _create_selected_master(self) -> dict[str, object]:
        document = fixture_mod._master(identity="master-c")
        return task_doc_tool(
            self.owner.cfg,
            TaskDocTarget(repo_id=fixture_mod.REPOSITORY, task_name="master-c"),
            operation="create",
            edit=TaskDocEdit(fields=document.model_dump(mode="json", by_alias=True)),
        )

    def test_detach_first_allows_later_exact_absence_bound_create(self) -> None:
        reached_publication = Barrier(2)
        release = Event()
        real_publish = task_doc_tools_module.publish_task_doc_set
        selected_json = self.owner.tasks / "master-c" / "task.json"
        sprint_json = self.owner.tasks / "sprint" / "task.json"

        def delayed_create(context: TaskDocPublication):
            accepted_paths = {source.json_path for source in context.source_snapshots}
            self.assertIn(selected_json, accepted_paths)
            self.assertNotIn(sprint_json, accepted_paths)
            reached_publication.wait(timeout=10)
            if not release.wait(timeout=10):
                raise AssertionError("create publication release was not signalled")
            return real_publish(context)

        with (
            mock.patch.object(
                task_doc_tools_module,
                "publish_task_doc_set",
                side_effect=delayed_create,
            ),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            pending_create = pool.submit(self._create_selected_master)
            reached_publication.wait(timeout=10)
            detached = self._detach()
            self.assertEqual(detached["state"], "detached")
            self.assertEqual(self.owner._sprint().orchestrates, ["master-a"])
            release.set()
            created = pending_create.result(timeout=10)
        self.assertTrue(created["ok"])
        self.assertEqual(created["operation"], "task_doc.create")
        self.assertEqual(self.owner._sprint().orchestrates, ["master-a"])

    def _assert_missing_master_side_appearance_refuses(self, side: str) -> None:
        real_publish = task_publication.publish_task_doc_transaction_and_refresh
        selected_json = self.owner.tasks / "master-c" / "task.json"
        selected_markdown = selected_json.with_suffix(".md")
        before = self.owner._snapshot()

        def create_one_side(transaction: TaskDocPublicationTransaction):
            selected_json.parent.mkdir(parents=True, exist_ok=True)
            target = selected_json if side == "json" else selected_markdown
            target.write_text(f"external-{side}-appearance\n", encoding="utf-8")
            return real_publish(transaction)

        with (
            mock.patch.object(
                sprint_linkage,
                "publish_task_doc_transaction_and_refresh",
                side_effect=create_one_side,
            ),
            self.assertRaises(TaskDocPublicationConflict),
        ):
            self._detach()
        self.assertEqual(
            {path: path.read_bytes() for path in before},
            before,
        )
        self.assertEqual(
            (selected_json if side == "json" else selected_markdown).read_text(encoding="utf-8"),
            f"external-{side}-appearance\n",
        )


if __name__ == "__main__":
    unittest.main()
