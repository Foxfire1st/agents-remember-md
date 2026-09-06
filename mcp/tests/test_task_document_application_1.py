from __future__ import annotations

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
from test_task_document import ApplicationTests


class ApplicationTests1(ApplicationTests):
    def test_create_writes_both_files(self) -> None:
        result = self._create(steps=[{"id": "S1", "title": "One", "status": "inProgress"}])
        self.assertEqual(result["operation"], "task_doc.create")
        self.assertEqual((result["stepsDone"], result["stepsTotal"]), (0, 1))
        self.assertTrue(Path(str(result["docPath"])).exists())
        self.assertTrue(Path(str(result["renderedPath"])).exists())
        self.assertNotIn("masterSync", result)

    def test_leaf_create_syncs_parent_master_row(self) -> None:
        self._create_parent_master()
        result = self._create(master="task.md")

        sync = result["masterSync"]
        self.assertEqual(sync["status"], "created")
        master = read_task_doc(Path(str(sync["masterDocPath"])))
        self.assertEqual(len(master.subTasks), 1)
        [row] = master.subTasks
        self.assertEqual(row.number, "3C")
        self.assertEqual(row.name, "Smoke")
        self.assertEqual(row.file, "03c_x.md")
        self.assertEqual(row.status, "planning")
        self.assertEqual(row.scope, "")

    def test_leaf_updates_preserve_manual_master_scope(self) -> None:
        self._create_parent_master()
        self._create(master="task.md")
        task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="3c-x"),
            operation="set_subtask",
            edit=TaskDocEdit(subtask={"number": "3C", "scope": "keep this prose"}),
        )

        result = self._call("set_field", fields={"title": "Renamed", "status": "inProgress"})

        self.assertEqual(result["masterSync"]["status"], "updated")
        master = read_task_doc(Path(str(result["masterSync"]["masterDocPath"])))
        [row] = master.subTasks
        self.assertEqual(row.name, "Renamed")
        self.assertEqual(row.status, "inProgress")
        self.assertEqual(row.scope, "keep this prose")

    def test_done_child_cannot_hide_pending_parent_from_progress_or_master_sync(self) -> None:
        self._create_parent_master()
        created = self._create(
            master="task.md",
            steps=[
                {
                    "id": "S1",
                    "title": "Parent",
                    "status": "pending",
                    "substeps": [{"id": "C1", "title": "Child", "status": "done"}],
                }
            ],
        )
        self.assertEqual((created["stepsDone"], created["stepsTotal"]), (1, 2))
        master = read_task_doc(Path(str(created["masterSync"]["masterDocPath"])))
        self.assertEqual(master.subTasks[0].status, "inProgress")

    def test_an_unreadable_parent_master_refuses_the_leaf_edit_rather_than_dropping_the_row(
        self,
    ) -> None:
        """A leaf that names a master owes it a row on every edit.

        If the master cannot be read the row cannot be computed, and writing the leaf anyway
        would leave the series silently describing the previous title forever. So the whole
        edit is refused, naming the file to repair -- and the leaf on disk is exactly what it
        was before the call.
        """

        self._create_parent_master()
        self._create(master="task.md")
        task_root = self.coord / "tasks" / "agents-remember" / "3c-x"
        master_path = task_root / "task.json"
        leaf_path = task_root / "03c_x.json"
        leaf_before = leaf_path.read_text(encoding="utf-8")
        master_path.write_text('{"schema": "ar-task-document/v1",', encoding="utf-8")

        with self.assertRaises(TaskDocError) as raised:
            self._call("set_field", fields={"title": "Renamed"})

        self.assertIn("cannot read parent master task document", str(raised.exception))
        self.assertIn("task.json", str(raised.exception))
        self.assertEqual(leaf_path.read_text(encoding="utf-8"), leaf_before)
        self.assertEqual(read_task_doc(leaf_path).title, "Smoke")

    def test_explicit_cross_series_master_ref_never_falls_back_to_local_master(self) -> None:
        created_master = self._create_parent_master()
        master_path = Path(str(created_master["docPath"]))
        master_before = master_path.read_bytes()

        result = self._create(master="../other-series/task.md")

        self.assertNotIn("masterSync", result)
        self.assertEqual(master_path.read_bytes(), master_before)
        self.assertEqual(read_task_doc(master_path).subTasks, [])

    def test_leaf_sync_refuses_duplicate_or_mispointed_exact_parent_row_before_write(
        self,
    ) -> None:
        self._create_parent_master()
        created_leaf = self._create(master="task.md")
        leaf_path = Path(str(created_leaf["docPath"]))
        master_path = self.coord / "tasks" / "agents-remember" / "3c-x" / "task.json"
        base = read_task_doc(master_path).model_dump(by_alias=True)

        candidates: list[tuple[str, dict[str, Any]]] = []
        duplicate = TaskDocument.model_validate(base).model_dump(by_alias=True)
        duplicate["subTasks"].append(dict(duplicate["subTasks"][0]))
        candidates.append(("at most one row", duplicate))
        mispointed = TaskDocument.model_validate(base).model_dump(by_alias=True)
        mispointed["subTasks"][0]["file"] = "03c_other.md"
        candidates.append(("points at", mispointed))

        for expected, candidate in candidates:
            write_task_doc(master_path.parent, TaskDocument.model_validate(candidate))
            before = {
                path: path.read_bytes()
                for path in (
                    leaf_path,
                    leaf_path.with_suffix(".md"),
                    master_path,
                    master_path.with_suffix(".md"),
                )
            }

            with self.subTest(expected=expected), self.assertRaises(TaskDocError) as raised:
                self._call("set_field", fields={"title": "Refused rename"})

            self.assertIn(expected, str(raised.exception))
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_leaf_sync_demotes_completed_master_when_work_becomes_unresolved(self) -> None:
        self._create_parent_master(status="Completed")
        self._create(
            master="task.md",
            status="Completed",
            steps=[{"id": "S1", "title": "One", "status": "done"}],
        )
        self._call("set_field", fields={"status": "inProgress"})
        master_path = self.coord / "tasks" / "agents-remember" / "3c-x" / "task.json"
        self.assertEqual(read_task_doc(master_path).status, "Completed")

        preview = self._call(
            "set_step",
            step={"id": "S1", "status": "pending"},
            dry_run=True,
        )

        self.assertEqual(preview["masterSync"]["status"], "would-update")
        self.assertIn("**Status:** inProgress", preview["masterSync"]["rendered"])
        self.assertEqual(read_task_doc(master_path).status, "Completed")

        self._call("set_step", step={"id": "S1", "status": "pending"})
        master = read_task_doc(master_path)
        self.assertEqual(master.status, "inProgress")
        self.assertEqual(master.subTasks[0].status, "inProgress")

    def test_set_status_and_set_field(self) -> None:
        self._create()
        status_result = self._call("set_status", fields={"status": "inProgress"})
        self.assertEqual(status_result["status"], "inProgress")
        updated = self._call("set_field", fields={"objective": "new", "bogus": "x"})
        self.assertEqual(updated["operation"], "task_doc.set_field")
        self.assertEqual(read_task_doc(Path(str(updated["docPath"]))).objective, "new")

    def test_set_field_cannot_repoint_plane_owned_contract_identity(self) -> None:
        self._create()
        for fields in (
            {"seriesContractPath": "tasks/other/series-contract.md"},
            {
                "enclosures": [
                    {"leafId": "other", "enclosurePath": "tasks/other/series-contract.md"}
                ]
            },
        ):
            with self.subTest(fields=fields), self.assertRaises(TaskDocError):
                self._call("set_field", fields=fields)

    def test_dry_run_does_not_mutate_existing_files(self) -> None:
        created = self._create(objective="orig")
        json_path = Path(str(created["docPath"]))
        md_path = Path(str(created["renderedPath"]))
        before_json = json_path.read_text(encoding="utf-8")
        before_md = md_path.read_text(encoding="utf-8")
        result = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="3c-x", slug="03c_x"),
            operation="set_field",
            edit=TaskDocEdit(fields={"objective": "changed"}),
            call=TaskDocCall(dry_run=True),
        )
        self.assertIn("changed", str(result["rendered"]))  # the would-be render reflects the edit
        # …but disk is untouched
        self.assertEqual(json_path.read_text(encoding="utf-8"), before_json)
        self.assertEqual(md_path.read_text(encoding="utf-8"), before_md)

    def test_dry_run_would_lose_flags_unmodeled_md_content(self) -> None:
        created = self._create(objective="orig")
        md_path = Path(str(created["renderedPath"]))
        # a clean re-preview (no real change) matches disk exactly: no loss, empty diff
        clean = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="3c-x", slug="03c_x"),
            operation="set_field",
            edit=TaskDocEdit(fields={"objective": "orig"}),
            call=TaskDocCall(dry_run=True),
        )
        self.assertFalse(clean["wouldLose"])
        self.assertEqual(clean["diff"], "")
        # a hand-authored line the JSON does not model → wouldLose true + the diff shows it dropped
        md_path.write_text(
            md_path.read_text(encoding="utf-8") + "\n## Bespoke hand note\nkeep me\n",
            encoding="utf-8",
        )
        lossy = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="3c-x", slug="03c_x"),
            operation="set_field",
            edit=TaskDocEdit(fields={"objective": "orig"}),
            call=TaskDocCall(dry_run=True),
        )
        self.assertTrue(lossy["wouldLose"])
        self.assertIn("keep me", str(lossy["diff"]))

    def test_replace_rewrites_structural_fields_and_decisions(self) -> None:
        created = self._create(
            objective="old",
            steps=[{"id": "S1", "title": "Old step", "status": "done"}],
            codeExamples=[
                {
                    "id": "E1",
                    "title": "Old example",
                    "distinctChange": "old",
                    "why": "old",
                }
            ],
            decisions=[{"at": "t1", "decision": "old", "rationale": "old"}],
        )
        json_path = Path(str(created["docPath"]))
        result = self._call(
            "replace",
            fields={
                "id": "3C",
                "slug": "03c_x",
                "title": "Smoke reset",
                "kind": "subTask",
                "repo": "agents-remember",
                "type": "Code",
                "createdAt": "2026-01-01T00:00",
                "objective": "new",
                "steps": [{"id": "S2", "title": "New step", "status": "pending"}],
                "codeExamples": [
                    {
                        "id": "E2",
                        "title": "New example",
                        "distinctChange": "new",
                        "why": "new",
                    }
                ],
                "decisions": [],
            },
        )
        self.assertEqual(result["operation"], "task_doc.replace")
        doc = read_task_doc(json_path)
        self.assertEqual(doc.title, "Smoke reset")
        self.assertEqual([step.id for step in doc.steps], ["S2"])
        self.assertEqual([example.id for example in doc.codeExamples], ["E2"])
        self.assertEqual(doc.decisions, [])

    def test_replace_rejects_document_path_change(self) -> None:
        self._create()
        with self.assertRaises(TaskDocError):
            self._call(
                "replace",
                fields={
                    "id": "3C",
                    "slug": "different",
                    "title": "Moved",
                    "kind": "subTask",
                    "repo": "agents-remember",
                    "type": "Code",
                    "createdAt": "2026-01-01T00:00",
                },
            )

    def test_set_step_inserts_then_updates_without_duplicating(self) -> None:
        self._create(steps=[{"id": "S1", "title": "One", "status": "pending"}])
        self._call(
            "set_step",
            step={"id": "S1.a", "title": "sub", "status": "pending", "parent": "S1"},
        )
        result = self._call(
            "set_step",
            step={"id": "S1.a", "title": "sub", "status": "done", "parent": "S1"},
        )
        doc = read_task_doc(Path(str(result["docPath"])))
        self.assertEqual(len(doc.steps[0].substeps), 1)
        self.assertEqual(doc.steps[0].substeps[0].status, "done")

    def test_skip_step_is_exact_audited_and_does_not_cascade(self) -> None:
        self._create(
            lifecycleId="LC-DOC",
            steps=[
                {
                    "id": "S1",
                    "title": "Parent",
                    "status": "pending",
                    "substeps": [
                        {"id": "C1", "title": "Child one", "status": "pending"},
                        {"id": "C2", "title": "Child two", "status": "blocked"},
                    ],
                }
            ],
        )

        parent_result = self._call(
            "skip_step",
            step={"id": "S1", "reason": "  Superseded by the accepted design.  "},
        )
        parent_doc = read_task_doc(Path(str(parent_result["docPath"])))
        parent = parent_doc.steps[0]
        parent_disposition = parent.disposition
        assert parent_disposition is not None
        self.assertEqual(parent.status, "done")
        self.assertEqual([sub.status for sub in parent.substeps], ["pending", "blocked"])
        self.assertEqual(parent_disposition.reason, "Superseded by the accepted design.")
        self.assertEqual(parent_disposition.kind, "intentionalSkip")
        self.assertEqual(parent_disposition.recordedVia, "task_doc.skip_step")
        self.assertEqual(parent_disposition.lifecycleId, "LC-DOC")
        self.assertRegex(parent_disposition.recordedAt, r"\+00:00$")
        self.assertEqual(parent_doc.decisions[-1].decision, "Intentionally skip step S1.")

        child_result = self._call(
            "skip_step",
            step={"id": "C1", "parent": "S1", "reason": "No longer required."},
        )
        child_doc = read_task_doc(Path(str(child_result["docPath"])))
        self.assertEqual(child_doc.steps[0].substeps[0].status, "done")
        self.assertEqual(child_doc.steps[0].substeps[1].status, "blocked")
        self.assertEqual(
            child_doc.decisions[-1].decision,
            "Intentionally skip step S1/C1.",
        )
