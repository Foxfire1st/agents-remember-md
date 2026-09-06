"""Tests for the JSON-primary task-document layer (slice 3c, commit 1).

Covers the ``ar-task-document/v1`` schema (round-trip, alias, strictness, progress
helpers), the deterministic markdown renderer (the ``w-02-light-task-workflow``
template shape, checkbox mapping, escaping, empty sections), the JSON+markdown
store, the ``task_doc`` application operations and error paths (including contract
lifecycle-key pickup), and the MCP tool registration.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

import agents_remember.tasks.store as task_store
from agents_remember.application.task_docs.task_doc_tools import (
    TaskDocCall,
    TaskDocEdit,
    TaskDocTarget,
    task_doc_tool,
)
from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
    RepositoryScope,
)
from agents_remember.tasks import (
    TaskDocument,
    completion_blockers,
    current_step,
    json_path_for,
    markdown_path_for,
    read_task_doc,
    render_markdown,
    step_done,
    step_total,
    write_task_doc,
    write_task_docs,
)


def _doc(**over: Any) -> TaskDocument:
    base: dict[str, Any] = {
        "id": "T1",
        "slug": "task",
        "title": "Hello",
        "kind": "light",
        "repo": "r",
        "type": "Docs",
        "createdAt": "2026-01-01T00:00",
    }
    base.update(over)
    return TaskDocument.model_validate(base)


def _master(**over: Any) -> TaskDocument:
    base: dict[str, Any] = {
        "id": "series",
        "slug": "series",
        "title": "Series",
        "kind": "master",
        "repo": "agents-remember",
        "type": "Master (Code)",
        "createdAt": "2026-01-01T00:00",
    }
    base.update(over)
    return TaskDocument.model_validate(base)


def _config(coord: Path) -> McpRuntimeConfig:
    """Build the configured repository authority used by task-doc publication."""
    repo = coord / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
        ],
        cwd=repo,
        check=True,
    )
    return McpRuntimeConfig(
        config_path=coord / "settings.json",
        coordination_root=coord,
        workspace_root=coord,
        transcript_root=coord / "logs" / "mcp",
        repositories={"agents-remember": RepositoryScope(repo_id="agents-remember", path=repo)},
    )


class SchemaTests(unittest.TestCase):
    def test_progress_counts_every_declared_parent_and_child(self) -> None:
        doc = _doc(
            steps=[
                {
                    "id": "S1",
                    "title": "One",
                    "status": "inProgress",
                    "substeps": [
                        {"id": "S1.a", "title": "a", "status": "done"},
                        {"id": "S1.b", "title": "b", "status": "pending"},
                    ],
                },
                {"id": "S2", "title": "Two", "status": "done"},
            ]
        )
        # Parent S1 remains visible beside its two children, plus S2: 4 units, 2 done.
        self.assertEqual((step_done(doc), step_total(doc)), (2, 4))
        self.assertEqual(
            [(item.id, item.parentId, item.status) for item in completion_blockers(doc)],
            [("S1", None, "inProgress"), ("S1.b", "S1", "pending")],
        )

    def test_current_step_prefers_active_then_first_unfinished_then_none(self) -> None:
        active = _doc(steps=[{"id": "S1", "title": "One", "status": "blocked"}])
        self.assertEqual(current_step(active), "S1 — One")
        pending = _doc(
            steps=[
                {"id": "S1", "title": "One", "status": "done"},
                {"id": "S2", "title": "Two", "status": "pending"},
            ]
        )
        self.assertEqual(current_step(pending), "S2 — Two")
        finished = _doc(steps=[{"id": "S1", "title": "One", "status": "done"}])
        self.assertIsNone(current_step(finished))


class RenderTests(unittest.TestCase):
    def test_golden_small_light_doc(self) -> None:
        doc = _doc(
            status="planning",
            objective="Obj.",
            requirements=["one"],
            steps=[{"id": "S1", "title": "Do", "status": "done"}],
            references=["ref"],
        )
        expected = (
            "\n".join(
                [
                    "# Task: Hello",
                    "",
                    "**Status:** planning",
                    "**Repo:** r",
                    "**Type:** Docs",
                    "**Created:** 2026-01-01T00:00",
                    "",
                    "---",
                    "",
                    "## Objective",
                    "",
                    "Obj.",
                    "",
                    "---",
                    "",
                    "## Requirements",
                    "",
                    "- one",
                    "",
                    "---",
                    "",
                    "## Design",
                    "",
                    "No design reasoning needed.",
                    "",
                    "---",
                    "",
                    "## Implementation Steps",
                    "",
                    "### S1 — Do",
                    "",
                    "---",
                    "",
                    "## Route Review",
                    "",
                    "_No candidate-bound route review recorded._",
                    "",
                    "---",
                    "",
                    "## Proposed Code Examples",
                    "",
                    "No code examples are needed for this task.",
                    "",
                    "---",
                    "",
                    "## Decision Log",
                    "",
                    "_None recorded._",
                    "",
                    "---",
                    "",
                    "## Open Questions",
                    "",
                    "- None.",
                    "",
                    "---",
                    "",
                    "## References",
                    "",
                    "- ref",
                ]
            )
            + "\n"
        )
        self.assertEqual(render_markdown(doc), expected)

    def test_decision_cell_escapes_pipe_and_newline(self) -> None:
        md = render_markdown(
            _doc(decisions=[{"at": "t", "decision": "a | b\nc", "rationale": "r"}])
        )
        self.assertIn(r"| t | a \| b c | r |", md)

    def test_code_example_fence_preserves_blank_lines(self) -> None:
        md = render_markdown(
            _doc(
                codeExamples=[
                    {
                        "id": "E1",
                        "title": "Ex",
                        "distinctChange": "c",
                        "why": "w",
                        "language": "python",
                        "snippet": "a = 1\n\nb = 2",
                    }
                ]
            )
        )
        self.assertIn("```python\na = 1\n\nb = 2\n```", md)

    def test_real_subtask_extensions_round_trip_content_complete(self) -> None:
        # Models this 03c sub-task's extensions (R4 acceptance): a descriptive status, extra
        # header lines, and bespoke freeform sections beyond the bare template.
        doc = _doc(
            kind="subTask",
            id="3C",
            slug="03c_x",
            master="task.md",
            status="inProgress",
            statusNote="core JSON format landed",
            headerNotes=[
                {"label": "Verified", "value": "2026-06-18 — 3 commits landed"},
                {"label": "Reopened", "value": "2026-06-19 — pilot surfaced gaps"},
            ],
            objective="Make the task document JSON-primary.",
            sections=[
                {"heading": "Reopened", "body": "gaps the pilot surfaced"},
                {"heading": "Status history", "body": "verbatim, pre-normalization"},
            ],
        )
        md = render_markdown(doc)
        self.assertIn("**Status:** inProgress — core JSON format landed", md)
        self.assertIn("**Verified:** 2026-06-18 — 3 commits landed", md)
        self.assertIn("**Reopened:** 2026-06-19 — pilot surfaced gaps", md)
        self.assertIn("## Reopened", md)
        self.assertIn("## Status history", md)
        # the JSON round-trips losslessly
        self.assertEqual(TaskDocument.model_validate(doc.model_dump(by_alias=True)), doc)


class MasterRenderTests(unittest.TestCase):
    def test_golden_master(self) -> None:
        doc = _master(
            title="Series X",
            type="Master (Code / Docs)",
            status="inProgress",
            createdAt="2026-06-12T15:58",
            subTasks=[
                {
                    "number": "1",
                    "name": "Design",
                    "file": "01_d.md",
                    "status": "Completed",
                    "scope": "keystone",
                },
                {"number": "3c", "name": "Persist", "file": "03c_p.md", "status": "inProgress"},
                {"number": "4", "name": "Serve", "status": "planning"},
            ],
            decisions=[{"at": "2026-06-12T15:58", "decision": "8 slices", "rationale": "fits"}],
            sections=[
                {"kind": "freeform", "heading": "Objective", "body": "Ship 3.0.0."},
                {"kind": "subTasks", "heading": "Sub-tasks (execution order)", "body": "> note"},
                {"kind": "sharedDecisions", "heading": "Shared Decisions"},
                {"kind": "freeform", "heading": "Invariants", "body": "- never weaker"},
            ],
        )
        expected = (
            "\n".join(
                [
                    "# Task: Series X",
                    "",
                    "**Status:** inProgress",
                    "**Repo:** agents-remember",
                    "**Type:** Master (Code / Docs)",
                    "**Created:** 2026-06-12T15:58",
                    "",
                    "---",
                    "",
                    "## Objective",
                    "",
                    "Ship 3.0.0.",
                    "",
                    "---",
                    "",
                    "## Sub-tasks (execution order)",
                    "",
                    "> note",
                    "",
                    "1. ✅ **Design** · `01_d.md` — keystone",
                    "3c. 🔨 **Persist** · `03c_p.md`",
                    "4. ⬜ **Serve**",
                    "",
                    "---",
                    "",
                    "## Shared Decisions",
                    "",
                    "| Date-Time | Decision | Rationale |",
                    "| --- | --- | --- |",
                    "| 2026-06-12T15:58 | 8 slices | fits |",
                    "",
                    "---",
                    "",
                    "## Invariants",
                    "",
                    "- never weaker",
                ]
            )
            + "\n"
        )
        self.assertEqual(render_markdown(doc), expected)


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def test_write_then_read_roundtrips_and_leaves_no_tmp(self) -> None:
        doc = _doc(objective="o", steps=[{"id": "S1", "title": "a", "status": "done"}])
        json_path, md_path = write_task_doc(self.root, doc)
        self.assertEqual(json_path, json_path_for(self.root, doc))
        self.assertEqual(md_path, markdown_path_for(self.root, doc))
        self.assertTrue(json_path.exists() and md_path.exists())
        self.assertEqual(read_task_doc(json_path), doc)
        self.assertEqual(md_path.read_text(encoding="utf-8"), render_markdown(doc))
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_batch_failure_removes_new_files_published_before_later_document(self) -> None:
        docs = [
            _doc(id="L1", slug="01_first", kind="subTask"),
            _doc(id="L2", slug="02_second", kind="subTask"),
        ]
        real_atomic_write = task_store.atomic_write_text
        call_count = 0

        def fail_on_second_document(path: Path, text: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise OSError("injected second-document failure")
            real_atomic_write(path, text)

        with (
            patch.object(task_store, "atomic_write_text", side_effect=fail_on_second_document),
            self.assertRaisesRegex(OSError, "injected second-document failure"),
        ):
            write_task_docs(self.root, docs)

        self.assertEqual(list(self.root.iterdir()), [])


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coord = Path(tempfile.mkdtemp())
        self.cfg = _config(self.coord)

    def _create(self, **fields: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": "3C",
            "slug": "03c_x",
            "title": "Smoke",
            "kind": "subTask",
            "repo": "agents-remember",
            "type": "Code",
            "createdAt": "2026-01-01T00:00",
        }
        payload.update(fields)
        return task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="3c-x"),
            operation="create",
            edit=TaskDocEdit(fields=payload),
        )

    def _create_parent_master(self, **fields: Any) -> dict[str, Any]:
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
            TaskDocTarget(repo_id="agents-remember", task_name="3c-x"),
            operation="create",
            edit=TaskDocEdit(fields=payload),
        )

    def _call(
        self,
        operation: str,
        *,
        fields: dict[str, Any] | None = None,
        step: dict[str, Any] | None = None,
        decision: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="3c-x", slug="03c_x"),
            operation=operation,
            edit=TaskDocEdit(fields=fields, step=step, decision=decision),
            call=TaskDocCall(dry_run=dry_run),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
