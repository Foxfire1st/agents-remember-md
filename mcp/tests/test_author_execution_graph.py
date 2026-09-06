"""L11-R5/R6/R8: the incremental ``author_execution_graph`` operation.

Split from ``test_task_execution_topology.py`` (file-size limit); fixtures and shared
helpers are imported from it.
"""

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
from agents_remember.tasks import Section, SubTaskRef, read_task_doc, write_task_doc
from agents_remember.tasks.document_refs import TaskDocumentTopology
from test_task_execution_topology import (
    MASTER_A,
    MASTER_B,
    REPOSITORY,
    SPRINT,
    _config,
    _master,
)
from test_worktree_support import git, init_repo

JUDGMENT_HEADING = "Judgment Register (canonical judgment authority)"
JUDGMENT_HEADER = (
    "| Judgment id | Kind (dependency meaning, execution nature, blast radius, priority, "
    "blocker placement, reprioritization, or leaf move) | Subject | Decision | Rationale | "
    "Evidence/fact refs | Author | Confidence | Supersedes |"
)
JUDGMENT_SEPARATOR = "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"


def _judgment_register(rows: list[str]) -> str:
    return "\n".join([JUDGMENT_HEADER, JUDGMENT_SEPARATOR, *rows])


def _judgment_row(judgment_id: str, author: str = "strategist") -> str:
    return (
        f"| {judgment_id} | leaf move | graph | segmentation=a | Explicit graph ruling. | "
        f"notes.md | {author} | high | |"
    )


class ExecutionGraphAuthoringTests(unittest.TestCase):
    """L11-R5/R6/R8: the incremental authoring operation."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.coord = Path(self.temp.name)
        self.tasks = self.coord / "tasks" / REPOSITORY
        self.tasks.mkdir(parents=True)
        self.code = self.coord / "code"
        init_repo(self.code)
        git(self.code, "branch", "super", "main")
        self.cfg = _config(self.coord, self.code)
        self.topology = TaskDocumentTopology(self.coord)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_fixture(
        self,
        *,
        register: bool = True,
        graph: dict[str, Any] | None = None,
        judgment_author: str = "strategist",
        leafs_a: list[str] | None = None,
    ) -> None:
        leafs = leafs_a or ["L1", "L2", "L3"]
        write_task_doc(
            self.tasks / "master-a",
            _master(identity="MASTER-A", execution_nature="organizational").model_copy(
                update={
                    "subTasks": [
                        SubTaskRef(number=leaf, name=leaf, file=f"{leaf.lower()}.md")
                        for leaf in leafs
                    ]
                }
            ),
        )
        write_task_doc(
            self.tasks / "master-b",
            _master(identity="MASTER-B", execution_nature="atomic").model_copy(
                update={"subTasks": [SubTaskRef(number="L1", name="L1", file="l1.md")]}
            ),
        )
        sections = []
        if register:
            sections.append(
                Section(
                    kind="freeform",
                    heading=JUDGMENT_HEADING,
                    body=_judgment_register([_judgment_row("J-1", judgment_author)]),
                )
            )
        write_task_doc(
            self.tasks / "sprint",
            _master(
                identity="SPRINT",
                orchestrates=["master-a", "master-b"],
                execution_graph=graph
                or {
                    "nodes": [MASTER_A.model_dump(), MASTER_B.model_dump()],
                    "edges": [],
                },
            ).model_copy(update={"integrationBranch": "super", "sections": sections}),
        )

    def _author(self, mutations: list[dict[str, Any]], *, dry_run: bool = False) -> dict[str, Any]:
        return task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id=REPOSITORY, task_name="sprint"),
            operation="author_execution_graph",
            edit=TaskDocEdit(fields={"mutations": mutations}),
            call=TaskDocCall(dry_run=dry_run),
        )

    def _snapshot(self) -> dict[Path, bytes]:
        return {path: path.read_bytes() for path in self.tasks.rglob("*") if path.is_file()}

    def test_dry_run_previews_and_writes_nothing_then_apply_publishes(self) -> None:
        self._write_fixture()
        before = self._snapshot()
        mutations = [
            {"op": "remove_node", "ref": MASTER_A.model_dump()},
            {
                "op": "add_node",
                "ref": MASTER_A.model_dump(),
                "kind": "segment",
                "leafIds": ["L1"],
                "judgmentId": "J-1",
            },
            {
                "op": "add_node",
                "ref": MASTER_A.model_dump(),
                "kind": "segment",
                "leafIds": ["L2", "L3"],
                "judgmentId": "J-1",
            },
            {
                "op": "add_edge",
                "predecessor": MASTER_B.model_dump(),
                "successor": {"ref": MASTER_A.model_dump(), "leafId": "L2"},
                "reason": "framework first",
                "judgmentId": "J-1",
            },
        ]
        preview = self._author(mutations, dry_run=True)
        self.assertEqual(preview["state"], "would-author")
        self.assertTrue(preview["dryRun"])
        self.assertEqual(len(preview["documents"]), 1)
        self.assertIn("(leafs: `L2`, `L3`)", preview["documents"][0]["rendered"])
        self.assertEqual(preview["leafPlacementFacts"], [])
        self.assertEqual(before, self._snapshot())

        applied = self._author(mutations)
        self.assertEqual(applied["state"], "authored")
        sprint = read_task_doc(self.tasks / "sprint" / "task.json")
        assert sprint.executionGraph is not None
        self.assertEqual(
            [(node.kind, node.ref, node.leafIds) for node in sprint.executionGraph.nodes],
            [
                ("master", MASTER_B, []),
                ("segment", MASTER_A, ["L1"]),
                ("segment", MASTER_A, ["L2", "L3"]),
            ],
        )
        waves = sprint.executionGraph.derived_waves()
        self.assertEqual(
            [[node.leafIds or [node.ref.key] for node in wave] for wave in waves],
            [[[MASTER_B.key], ["L1"]], [["L2", "L3"]]],
        )
        self.assertEqual(sprint.executionGraph.edges[0].judgmentId, "J-1")
        rendered = (self.tasks / "sprint" / "task.md").read_text(encoding="utf-8")
        self.assertIn(f"- `{MASTER_A.key}` (leafs: `L1`)", rendered)
        self.assertIn(
            f"`{MASTER_B.key}` → `{MASTER_A.key}` (leafs: `L2`, `L3`) — framework first",
            rendered,
        )
        self.assertIn(f"- Wave 2: `{MASTER_A.key}` (leafs: `L2`, `L3`)", rendered)
        masters = self.topology.validate_execution_topology(SPRINT)
        self.assertEqual([master.ref for master in masters], [MASTER_A, MASTER_B])

    def test_batch_atomicity_leaves_everything_untouched_on_failure(self) -> None:
        self._write_fixture()
        before = self._snapshot()
        with self.assertRaisesRegex(TaskDocError, "node-duplicate"):
            self._author(
                [
                    {
                        "op": "add_node",
                        "ref": MASTER_A.model_dump(),
                        "kind": "segment",
                        "leafIds": ["L1"],
                        "judgmentId": "J-1",
                    },
                    {
                        "op": "add_node",
                        "ref": MASTER_A.model_dump(),
                        "kind": "segment",
                        "leafIds": ["L1"],
                        "judgmentId": "J-1",
                    },
                ]
            )
        self.assertEqual(before, self._snapshot())
