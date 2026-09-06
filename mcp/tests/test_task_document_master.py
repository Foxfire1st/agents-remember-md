from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agents_remember.application.task_docs.task_doc_tools import (
    TaskDocCall,
    TaskDocEdit,
    TaskDocError,
    TaskDocTarget,
    task_doc_tool,
)
from agents_remember.tasks import TaskDocument, read_task_doc, write_task_doc
from test_task_document import _config, _doc, _master


class MasterApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coord = Path(tempfile.mkdtemp())
        self.cfg = _config(self.coord)

    def _create(self, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": "series",
            "slug": "series",
            "title": "Series",
            "kind": "master",
            "repo": "agents-remember",
            "type": "Master (Code)",
            "createdAt": "2026-01-01T00:00",
            "sections": [{"kind": "subTasks", "heading": "Sub-tasks"}],
        }
        payload.update(fields)
        return task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="series"),
            operation="create",
            edit=TaskDocEdit(fields=payload),
        )

    def _op(self, operation: str, dry_run: bool = False, **kw: Any) -> dict[str, Any]:
        return task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="series"),
            operation=operation,
            edit=TaskDocEdit(**kw),
            call=TaskDocCall(dry_run=dry_run),
        )

    def _complete_row(self, number: str) -> None:
        self._op("set_subtask", subtask={"number": number, "status": "Completed"})

    def test_create_master_writes_task_json_without_lifecycle(self) -> None:
        result = self._create()
        self.assertEqual(result["kind"], "master")
        self.assertTrue(str(result["docPath"]).endswith("task.json"))
        self.assertIsNone(result["lifecycleId"])

    def test_set_subtask_inserts_then_updates_by_number(self) -> None:
        self._create(subTasks=[{"number": "1", "name": "A", "status": "planning"}])
        self._author_leaf(number="1", slug="01_a")
        self._op("set_subtask", subtask={"number": "3c", "name": "B", "status": "inProgress"})
        result = self._op(
            "set_subtask", subtask={"number": "1", "status": "Completed", "scope": "done"}
        )
        doc = read_task_doc(Path(str(result["docPath"])))
        self.assertEqual(
            [(s.number, s.status) for s in doc.subTasks],
            [("1", "Completed"), ("3c", "inProgress")],
        )
        self.assertEqual(doc.subTasks[0].scope, "done")

    def test_set_subtask_completed_refuses_unready_or_missing_exact_leaf(self) -> None:
        self._create(subTasks=[{"number": "1", "name": "A", "status": "planning"}])
        with self.assertRaises(TaskDocError) as missing:
            self._op("set_subtask", subtask={"number": "1", "status": "Completed"})
        self.assertIn("no leaf task document exists", str(missing.exception))

        leaf_json, _leaf_md = self._author_leaf(number="1", slug="01_a")
        leaf = read_task_doc(leaf_json)
        data = leaf.model_dump(by_alias=True)
        data["steps"] = [{"id": "S1", "title": "Open", "status": "pending"}]
        write_task_doc(leaf_json.parent, TaskDocument.model_validate(data))
        with self.assertRaises(TaskDocError) as unresolved:
            self._op("set_subtask", subtask={"number": "1", "status": "Completed"})
        self.assertIn("'id': 'S1'", str(unresolved.exception))

    def test_replace_cannot_erase_or_change_unresolved_row_identity_or_multiplicity(self) -> None:
        task_root = self.coord / "tasks" / "agents-remember" / "series"
        original = _master(
            subTasks=[
                {
                    "number": "1",
                    "name": "First",
                    "file": "01_a.md",
                    "status": "planning",
                },
                {
                    "number": "1",
                    "name": "Duplicate",
                    "file": "01_a.md",
                    "status": "inProgress",
                },
            ]
        )
        master_json, _ = write_task_doc(task_root, original)
        base = read_task_doc(master_json).model_dump(by_alias=True)
        candidates: list[dict[str, Any]] = []

        dropped = TaskDocument.model_validate(base).model_dump(by_alias=True)
        dropped["subTasks"].pop()
        candidates.append(dropped)
        repointed = TaskDocument.model_validate(base).model_dump(by_alias=True)
        repointed["subTasks"][0]["file"] = "01_other.md"
        candidates.append(repointed)
        renamed = TaskDocument.model_validate(base).model_dump(by_alias=True)
        renamed["subTasks"][0]["number"] = "2"
        candidates.append(renamed)
        changed_kind = TaskDocument.model_validate(base).model_dump(by_alias=True)
        changed_kind["kind"] = "subTask"
        changed_kind["slug"] = "task"
        changed_kind["subTasks"] = []
        changed_kind["sections"] = []
        candidates.append(changed_kind)

        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(TaskDocError) as raised:
                self._op("replace", fields=candidate)
            self.assertIn(
                "cannot remove, rename, or repoint unresolved master rows", str(raised.exception)
            )
            self.assertEqual(read_task_doc(master_json), original)

        metadata = TaskDocument.model_validate(base).model_dump(by_alias=True)
        metadata["subTasks"][0]["name"] = "Renamed metadata"
        metadata["subTasks"][0]["scope"] = "Clarified scope"
        changed = self._op("replace", fields=metadata)
        row = read_task_doc(Path(str(changed["docPath"]))).subTasks[0]
        self.assertEqual((row.name, row.scope), ("Renamed metadata", "Clarified scope"))

    def test_master_completion_revalidates_pending_leaf_behind_completed_row(self) -> None:
        task_root = self.coord / "tasks" / "agents-remember" / "series"
        leaf = _doc(
            id="1",
            slug="01_a",
            kind="subTask",
            repo="agents-remember",
            master="task.md",
            steps=[{"id": "S1", "title": "Open", "status": "pending"}],
        )
        write_task_doc(task_root, leaf)
        legacy = _master(
            status="inProgress",
            subTasks=[
                {
                    "number": "1",
                    "name": "False completion",
                    "file": "01_a.md",
                    "status": "Completed",
                }
            ],
        )
        write_task_doc(task_root, legacy)
        with self.assertRaises(TaskDocError) as pending:
            self._op("set_status", fields={"status": "Completed"})
        self.assertIn("'id': 'S1'", str(pending.exception))

    def _author_leaf(self, *, number: str = "1", slug: str = "01_a") -> tuple[Path, Path]:
        leaf = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="series"),
            operation="create",
            edit=TaskDocEdit(
                fields={
                    "id": number,
                    "slug": slug,
                    "title": f"Leaf {number}",
                    "kind": "subTask",
                    "master": "task.md",
                    "repo": "agents-remember",
                    "createdAt": "2026-01-01T00:00",
                }
            ),
        )
        return Path(str(leaf["docPath"])), Path(str(leaf["renderedPath"]))

    def test_remove_subtask_deletes_leaf_doc_and_row(self) -> None:
        # remove means remove: the master row AND the leaf doc (json + md) are gone.
        self._create()
        leaf_json, leaf_md = self._author_leaf()
        self._complete_row("1")
        self.assertTrue(leaf_json.exists() and leaf_md.exists())
        result = self._op("remove_subtask", subtask={"number": "1"})
        self.assertEqual(result["removedSubtask"], "1")
        master = read_task_doc(Path(str(result["docPath"])))
        self.assertEqual([s.number for s in master.subTasks], [])
        self.assertFalse(leaf_json.exists())
        self.assertFalse(leaf_md.exists())
        self.assertIn(leaf_json.as_posix(), result["deletedFiles"])

    def test_remove_subtask_refuses_unresolved_row_without_touching_any_file(self) -> None:
        created = self._create()
        leaf_json, leaf_markdown = self._author_leaf()
        master_json = Path(str(created["docPath"]))
        master_markdown = Path(str(created["renderedPath"]))
        before = {
            path: path.read_bytes()
            for path in (master_json, master_markdown, leaf_json, leaf_markdown)
        }

        with self.assertRaises(TaskDocError) as raised:
            self._op("remove_subtask", subtask={"number": "1"})

        self.assertIn(
            "cannot remove, rename, or repoint unresolved master rows", str(raised.exception)
        )
        self.assertEqual({path: path.read_bytes() for path in before}, before)
