from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import agents_remember.tasks.document_refs as task_document_refs
import agents_remember.tasks.store as task_store
from agents_remember.application.task_docs import task_doc_queue_scope
from agents_remember.application.task_docs.task_doc_queue_scope import (
    TaskDocScopeChange,
    resolve_projection_scope_union,
)
from agents_remember.application.task_docs.task_doc_tools import (
    TaskDocCall,
    TaskDocEdit,
    TaskDocError,
    TaskDocTarget,
    task_doc_tool,
)
from agents_remember.application.task_docs.task_execution_topology import (
    ExecutionTopologyError,
    ExecutionTopologyInventoryRequest,
    inventory_execution_topology,
    require_commanded_masters_completed,
)
from agents_remember.controlplane.closeout_queue_store import queue_store_paths
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, RepositoryScope
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.serving.projections.snapshots_impl._task_documents import read_task_documents
from agents_remember.tasks import (
    Section,
    SprintExecutionGraph,
    TaskDocument,
    read_task_doc,
    write_task_doc,
)
from agents_remember.tasks.document_refs import TaskDocumentRefError, TaskDocumentTopology
from pydantic import ValidationError
from test_worktree_support import git, init_repo

REPOSITORY = "agents-remember"
SPRINT = TaskDocumentRef(repository=REPOSITORY, path="sprint/task.json")
MASTER_A = TaskDocumentRef(repository=REPOSITORY, path="master-a/task.json")
MASTER_B = TaskDocumentRef(repository=REPOSITORY, path="master-b/task.json")
MASTER_C = TaskDocumentRef(repository=REPOSITORY, path="master-c/task.json")


def _config(coordination_root: Path, code_repository: Path) -> McpRuntimeConfig:
    scope = RepositoryScope(REPOSITORY, code_repository)
    return cast(
        McpRuntimeConfig,
        SimpleNamespace(
            coordination_root=coordination_root,
            repositories={REPOSITORY: scope},
        ),
    )


def _master(
    *,
    identity: str,
    orchestrates: list[str] | None = None,
    execution_nature: str | None = None,
    execution_graph: dict[str, Any] | None = None,
    title: str | None = None,
) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": identity,
            "slug": identity,
            "title": title or identity,
            "kind": "master",
            "repo": REPOSITORY,
            "type": "Master",
            "createdAt": "2026-08-15T00:00:00+00:00",
            "orchestrates": orchestrates or [],
            "executionNature": execution_nature,
            "executionGraph": execution_graph,
        }
    )


def _graph(*, reverse: bool = False) -> dict[str, Any]:
    predecessor, successor = (MASTER_B, MASTER_A) if reverse else (MASTER_A, MASTER_B)
    return {
        "nodes": [MASTER_A.model_dump(), MASTER_B.model_dump()],
        "edges": [
            {
                "predecessor": predecessor.model_dump(),
                "successor": successor.model_dump(),
                "reason": "Shared contract must land first.",
            }
        ],
    }


_JUDGMENT_HEADER = (
    "| Judgment id | Kind (dependency meaning, execution nature, blast radius, priority, "
    "blocker placement, reprioritization, or leaf move) | Subject | Decision | Rationale | "
    "Evidence/fact refs | Author | Confidence | Supersedes |"
)


def _judgment_row(judgment_id: str, author: str = "strategist") -> str:
    return (
        f"| {judgment_id} | execution nature | graph | nature=ruled | Explicit graph ruling. | "
        f"notes.md | {author} | high | |"
    )


class ExecutionGraphSchemaTests(unittest.TestCase):
    def test_graph_derives_stable_waves_without_persisting_positions(self) -> None:
        graph = SprintExecutionGraph.model_validate(_graph())
        self.assertEqual(
            [[node.ref for node in wave] for wave in graph.derived_waves()],
            [[MASTER_A], [MASTER_B]],
        )
        self.assertNotIn("wave", graph.model_dump(mode="json"))
        self.assertNotIn("position", graph.model_dump(mode="json"))

    def test_graph_releases_a_multi_parent_successor_only_after_every_predecessor(self) -> None:
        graph = SprintExecutionGraph.model_validate(
            {
                "nodes": [
                    MASTER_A.model_dump(),
                    MASTER_B.model_dump(),
                    MASTER_C.model_dump(),
                ],
                "edges": [
                    {
                        "predecessor": MASTER_A.model_dump(),
                        "successor": MASTER_C.model_dump(),
                        "reason": "First dependency.",
                    },
                    {
                        "predecessor": MASTER_B.model_dump(),
                        "successor": MASTER_C.model_dump(),
                        "reason": "Second dependency.",
                    },
                ],
            }
        )
        self.assertEqual(
            [[node.ref for node in wave] for wave in graph.derived_waves()],
            [[MASTER_A, MASTER_B], [MASTER_C]],
        )

    def test_graph_refuses_duplicates_unknown_endpoints_self_edges_blank_reasons_and_cycles(
        self,
    ) -> None:
        mutations = (
            {"nodes": [MASTER_A.model_dump(), MASTER_A.model_dump()], "edges": []},
            {
                "nodes": [MASTER_A.model_dump()],
                "edges": [
                    {
                        "predecessor": MASTER_A.model_dump(),
                        "successor": MASTER_B.model_dump(),
                        "reason": "x",
                    }
                ],
            },
            {
                "nodes": [MASTER_A.model_dump()],
                "edges": [
                    {
                        "predecessor": MASTER_A.model_dump(),
                        "successor": MASTER_A.model_dump(),
                        "reason": "x",
                    }
                ],
            },
            {
                "nodes": [MASTER_A.model_dump(), MASTER_B.model_dump()],
                "edges": [
                    {
                        "predecessor": MASTER_A.model_dump(),
                        "successor": MASTER_B.model_dump(),
                        "reason": " ",
                    }
                ],
            },
            {
                "nodes": [MASTER_A.model_dump(), MASTER_B.model_dump()],
                "edges": [*_graph()["edges"], *_graph()["edges"]],
            },
            {
                "nodes": [MASTER_A.model_dump(), MASTER_B.model_dump()],
                "edges": [*_graph()["edges"], *_graph(reverse=True)["edges"]],
            },
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                SprintExecutionGraph.model_validate(mutation)

    def test_execution_fields_are_master_only_and_split_sprint_from_commanded_master(self) -> None:
        with self.assertRaisesRegex(ValidationError, "master-only"):
            TaskDocument.model_validate(
                {
                    **_master(identity="plain").model_dump(by_alias=True),
                    "kind": "subTask",
                    "executionNature": "atomic",
                }
            )
        with self.assertRaisesRegex(ValidationError, "has no executionNature"):
            _master(
                identity="sprint",
                orchestrates=["master-a"],
                execution_nature="atomic",
            )
        with self.assertRaisesRegex(ValidationError, "orchestration sprint"):
            _master(identity="master-a", execution_graph={"nodes": [MASTER_A.model_dump()]})


class ExecutionTopologyTests(unittest.TestCase):
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

    def test_inventory_enumerates_sprints_and_proposes_branch_backed_nature(self) -> None:
        write_task_doc(
            self.tasks / "sprint",
            _master(identity="SPRINT", orchestrates=["master-a", "master-b"]),
        )
        write_task_doc(self.tasks / "master-a", _master(identity="MASTER-A"))
        write_task_doc(self.tasks / "master-b", _master(identity="MASTER-B"))
        git(self.code, "branch", "ar/master-a", "super")

        inv = inventory_execution_topology(
            ExecutionTopologyInventoryRequest(
                coordination_root=self.coord,
                repo_id=REPOSITORY,
                code_repository=self.code,
            )
        )
        self.assertEqual(inv["sprintCount"], 1)
        self.assertEqual(inv["commandedMasterCount"], 2)
        self.assertEqual(inv["sprints"][0]["executionGraph"], "missing")
        self.assertEqual(inv["sprints"][0]["edgesRequireRuling"], True)
        by_path = {m["taskDocumentRef"]["path"]: m for m in inv["commandedMasters"]}
        self.assertEqual(by_path["master-a/task.json"]["proposedNature"], "atomic")
        self.assertEqual(by_path["master-a/task.json"]["branch"], "ar/master-a")
        self.assertEqual(by_path["master-b/task.json"]["proposedNature"], "organizational")
        self.assertEqual(by_path["master-b/task.json"]["branch"], None)

    def test_inventory_reports_zero_counts_on_an_empty_task_tree(self) -> None:
        inv = inventory_execution_topology(
            ExecutionTopologyInventoryRequest(
                coordination_root=self.coord,
                repo_id=REPOSITORY,
                code_repository=self.code,
            )
        )
        self.assertEqual(inv["sprintCount"], 0)
        self.assertEqual(inv["commandedMasterCount"], 0)

    def test_inventory_refuses_when_branch_enumeration_fails(self) -> None:
        with (
            mock.patch(
                "agents_remember.application.task_docs.task_execution_topology.run_git",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr="boom"),
            ),
            self.assertRaisesRegex(ExecutionTopologyError, "cannot enumerate branches"),
        ):
            inventory_execution_topology(
                ExecutionTopologyInventoryRequest(
                    coordination_root=self.coord,
                    repo_id=REPOSITORY,
                    code_repository=self.code,
                )
            )

    def test_queue_scope_split_has_direct_topology_test_ownership(self) -> None:
        self.assertTrue(callable(task_doc_queue_scope.resolve_projection_scope_union))

    def test_new_sprint_scope_union_includes_its_own_projection(self) -> None:
        new_sprint = TaskDocumentRef(repository=REPOSITORY, path="new-sprint/task.json")
        candidate = _master(identity="NEW-SPRINT", orchestrates=["master-a"])

        scopes = resolve_projection_scope_union(
            self.coord,
            REPOSITORY,
            (TaskDocScopeChange(new_sprint, None, candidate),),
        )

        self.assertEqual(scopes, (new_sprint,))

    def test_new_graphless_sprint_publication_isolated_from_unrelated_malformed_task(
        self,
    ) -> None:
        write_task_doc(
            self.tasks / "master-a",
            _master(identity="MASTER-A", execution_nature="atomic"),
        )
        malformed = self.tasks / "unrelated" / "task.json"
        malformed.parent.mkdir()
        malformed.write_text("{not-json", encoding="utf-8")
        new_sprint = TaskDocumentRef(repository=REPOSITORY, path="new-sprint/task.json")

        fields = (
            _master(identity="NEW-SPRINT", orchestrates=["master-a"])
            .model_copy(update={"integrationBranch": "super"})
            .model_dump(by_alias=True)
        )
        preview = self._task_doc(
            "new-sprint",
            "create",
            fields=fields,
            dry_run=True,
        )
        self.assertFalse((self.tasks / "new-sprint" / "task.json").exists())
        self.assertEqual(
            {
                TaskDocumentRef.model_validate(effect["sprintTaskDocumentRef"])
                for effect in preview["projectionEffects"]
            },
            {new_sprint},
        )

        created = self._task_doc("new-sprint", "create", fields=fields)

        self.assertTrue((self.tasks / "new-sprint" / "task.json").is_file())
        self.assertEqual(
            {
                TaskDocumentRef.model_validate(effect["sprintTaskDocumentRef"])
                for effect in created["projectionEffects"]
            },
            {new_sprint},
        )

    def test_master_alias_change_includes_old_and_new_sprint_consumers(self) -> None:
        old_sprint = _master(identity="SPRINT", orchestrates=["Old master title"])
        new_sprint_ref = TaskDocumentRef(repository=REPOSITORY, path="new-sprint/task.json")
        new_sprint = _master(identity="NEW-SPRINT", orchestrates=["New master title"])
        original = _master(identity="MASTER-A", title="Old master title")
        candidate = original.model_copy(update={"title": "New master title"})
        write_task_doc(self.tasks / "sprint", old_sprint)
        write_task_doc(self.tasks / "new-sprint", new_sprint)
        write_task_doc(self.tasks / "master-a", original)

        scopes = resolve_projection_scope_union(
            self.coord,
            REPOSITORY,
            (TaskDocScopeChange(MASTER_A, original, candidate),),
        )

        self.assertEqual(scopes, tuple(sorted((SPRINT, new_sprint_ref), key=lambda ref: ref.key)))

    def test_unchanged_documents_have_no_scope_despite_unrelated_malformed_task(self) -> None:
        sprint = _master(identity="SPRINT", orchestrates=["master-a"])
        master = _master(identity="MASTER-A")
        write_task_doc(self.tasks / "sprint", sprint)
        write_task_doc(self.tasks / "master-a", master)
        broken = self.tasks / "broken" / "task.json"
        broken.parent.mkdir()
        broken.write_text("{malformed", encoding="utf-8")

        scopes = resolve_projection_scope_union(
            self.coord,
            REPOSITORY,
            (
                TaskDocScopeChange(MASTER_A, master, master),
                TaskDocScopeChange(SPRINT, sprint, sprint),
            ),
        )

        self.assertEqual(scopes, ())

    def test_master_publication_refreshes_related_sprint_despite_unrelated_malformed_task(
        self,
    ) -> None:
        sprint = _master(identity="SPRINT", orchestrates=["master-a"])
        master = _master(identity="MASTER-A", execution_nature="atomic")
        write_task_doc(self.tasks / "sprint", sprint)
        write_task_doc(self.tasks / "master-a", master)
        broken = self.tasks / "broken" / "task.json"
        broken.parent.mkdir()
        broken.write_text("{malformed", encoding="utf-8")

        updated = self._task_doc(
            "master-a",
            "set_field",
            fields={"status": "inProgress"},
        )

        self.assertEqual(
            read_task_doc(self.tasks / "master-a" / "task.json").status,
            "inProgress",
        )
        self.assertEqual(
            {
                TaskDocumentRef.model_validate(effect["sprintTaskDocumentRef"])
                for effect in updated["projectionEffects"]
            },
            {SPRINT},
        )
        effect = updated["projectionEffects"][0]
        self.assertEqual(effect["rebuild"]["outcome"], "published")
        self.assertIsNotNone(effect["rebuild"]["sourceFingerprint"])
        self.assertIsNone(effect["nextAction"])

    def test_unparented_leaf_has_no_projection_scope(self) -> None:
        leaf = TaskDocument.model_validate(
            {
                "id": "L1",
                "slug": "leaf",
                "title": "Leaf",
                "kind": "subTask",
                "type": "Code",
                "repo": REPOSITORY,
                "createdAt": "2026-08-24T00:00:00+00:00",
            }
        )
        ref = TaskDocumentRef(repository=REPOSITORY, path="master/leaf.json")
        self.assertEqual(
            resolve_projection_scope_union(
                self.coord,
                REPOSITORY,
                (TaskDocScopeChange(ref, None, leaf),),
            ),
            (),
        )

    def test_completion_topology_errors_are_normalized_at_the_queue_boundary(self) -> None:
        topology = mock.Mock()
        topology.validate_execution_topology.side_effect = TaskDocumentRefError(
            "task-execution-topology-migration-required",
            "executionGraph is missing",
        )
        with self.assertRaisesRegex(
            ExecutionTopologyError,
            "task-execution-topology-migration-required",
        ):
            require_commanded_masters_completed(topology, SPRINT, {})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_legacy(self, *, register: bool = True) -> None:
        sections = (
            [
                Section(
                    kind="freeform",
                    heading="Judgment Register (canonical judgment authority)",
                    body="\n".join(
                        [
                            _JUDGMENT_HEADER,
                            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
                            _judgment_row("J-nature-a"),
                            _judgment_row("J-nature-b"),
                            _judgment_row("J-edge"),
                        ]
                    ),
                )
            ]
            if register
            else []
        )
        write_task_doc(
            self.tasks / "sprint",
            _master(identity="SPRINT", orchestrates=["master-a", "master-b"]).model_copy(
                update={"integrationBranch": "super", "sections": sections}
            ),
        )
        write_task_doc(self.tasks / "master-a", _master(identity="MASTER-A"))
        write_task_doc(self.tasks / "master-b", _master(identity="MASTER-B"))

    def _bootstrap_fields(self) -> dict[str, Any]:
        """The first-batch mutations that author a graph onto a graph-less sprint (L13)."""

        return {
            "mutations": [
                {"op": "add_node", "ref": MASTER_A.model_dump()},
                {"op": "add_node", "ref": MASTER_B.model_dump()},
                {
                    "op": "set_nature",
                    "ref": MASTER_A.model_dump(),
                    "executionNature": "organizational",
                    "judgmentId": "J-nature-a",
                },
                {
                    "op": "set_nature",
                    "ref": MASTER_B.model_dump(),
                    "executionNature": "atomic",
                    "judgmentId": "J-nature-b",
                },
                {
                    "op": "add_edge",
                    "predecessor": MASTER_A.model_dump(),
                    "successor": MASTER_B.model_dump(),
                    "reason": "Shared contract must land first.",
                    "judgmentId": "J-edge",
                },
            ]
        }

    def _task_doc(
        self,
        task_name: str,
        operation: str,
        *,
        fields: dict[str, Any],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id=REPOSITORY, task_name=task_name),
            operation=operation,
            edit=TaskDocEdit(fields=fields),
            call=TaskDocCall(dry_run=dry_run),
        )

    def _bootstrap(self, *, dry_run: bool = False) -> dict[str, Any]:
        return task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id=REPOSITORY, task_name="sprint"),
            operation="author_execution_graph",
            edit=TaskDocEdit(fields=self._bootstrap_fields()),
            call=TaskDocCall(dry_run=dry_run),
        )

    def test_legacy_documents_are_readable_but_topology_use_requires_migration(self) -> None:
        self._write_legacy()
        with self.assertRaises(TaskDocumentRefError) as raised:
            self.topology.validate_execution_topology(SPRINT)
        self.assertEqual(raised.exception.status, "task-execution-topology-migration-required")

        # L13-R7: a nature-less legacy master under the atomic-sequential default
        # takes an explicit nature write without any migration dead-end.
        task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id=REPOSITORY, task_name="master-a"),
            operation="set_field",
            edit=TaskDocEdit(fields={"executionNature": "organizational"}),
        )
        self.assertEqual(
            read_task_doc(self.tasks / "master-a" / "task.json").executionNature,
            "organizational",
        )

    def test_topology_refuses_a_non_sprint_and_confines_override_identity(self) -> None:
        write_task_doc(
            self.tasks / "master-a",
            _master(identity="MASTER-A", execution_nature="organizational"),
        )
        with self.assertRaises(TaskDocumentRefError) as raised:
            self.topology.validate_execution_topology(MASTER_A)
        self.assertEqual(raised.exception.status, "task-execution-graph-sprint-required")

        foreign_sprint = _master(
            identity="SPRINT",
            orchestrates=["master-a", "master-b"],
            execution_graph=_graph(),
        ).model_copy(update={"repo": "foreign"})
        with self.assertRaises(TaskDocumentRefError) as raised:
            self.topology.validate_execution_topology(
                SPRINT,
                overrides={SPRINT: foreign_sprint},
            )
        self.assertEqual(raised.exception.status, "task-document-repo-mismatch")

        outside = self.coord / "outside"
        outside.mkdir()
        escape = self.tasks / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        escaped_ref = TaskDocumentRef(repository=REPOSITORY, path="escape/task.json")
        with self.assertRaises(TaskDocumentRefError) as raised:
            self.topology.validate_execution_topology(
                escaped_ref,
                overrides={escaped_ref: foreign_sprint.model_copy(update={"repo": REPOSITORY})},
            )
        self.assertEqual(raised.exception.status, "task-document-outside-root")

    def test_topology_refuses_unknown_duplicate_and_drifted_command_membership(self) -> None:
        write_task_doc(
            self.tasks / "master-a",
            _master(identity="MASTER-A", execution_nature="organizational"),
        )
        write_task_doc(
            self.tasks / "master-b",
            _master(identity="MASTER-B", execution_nature="atomic"),
        )
        cases = (
            (["master-a", "missing-master"], _graph()),
            (["master-a", "MASTER-A"], _graph()),
            (
                ["master-a", "master-b"],
                {"nodes": [MASTER_A.model_dump()], "edges": []},
            ),
        )
        for orchestrates, graph in cases:
            with self.subTest(orchestrates=orchestrates, graph=graph):
                write_task_doc(
                    self.tasks / "sprint",
                    _master(
                        identity="SPRINT",
                        orchestrates=orchestrates,
                        execution_graph=graph,
                    ),
                )
                with self.assertRaises(TaskDocumentRefError) as raised:
                    self.topology.validate_execution_topology(SPRINT)
                self.assertEqual(
                    raised.exception.status,
                    "task-execution-graph-membership-invalid",
                )

    def test_task_doc_create_completes_a_graph_and_refuses_an_alias_collision(self) -> None:
        write_task_doc(
            self.tasks / "master-b",
            _master(identity="MASTER-B", execution_nature="atomic"),
        )
        write_task_doc(
            self.tasks / "sprint",
            _master(
                identity="SPRINT",
                orchestrates=["Alias A", "master-b"],
                execution_graph=_graph(),
            ).model_copy(update={"integrationBranch": "super"}),
        )
        created = self._task_doc(
            "master-a",
            "create",
            fields=_master(
                identity="MASTER-A",
                title="Alias A",
                execution_nature="organizational",
            ).model_dump(by_alias=True),
        )
        self.assertEqual(created["status"], "planning")
        with self.assertRaisesRegex(TaskDocError, "membership-invalid"):
            self._task_doc(
                "master-c",
                "create",
                fields=_master(
                    identity="MASTER-C",
                    title="Alias A",
                    execution_nature="atomic",
                ).model_dump(by_alias=True),
            )

    def test_task_doc_set_field_and_replace_refuse_alias_drift_or_collision(self) -> None:
        write_task_doc(
            self.tasks / "master-a",
            _master(
                identity="MASTER-A",
                title="Alias A",
                execution_nature="organizational",
            ),
        )
        write_task_doc(
            self.tasks / "master-b",
            _master(identity="MASTER-B", execution_nature="atomic"),
        )
        write_task_doc(
            self.tasks / "sprint",
            _master(
                identity="SPRINT",
                orchestrates=["Alias A", "master-b"],
                execution_graph=_graph(),
            ).model_copy(update={"integrationBranch": "super"}),
        )
        with self.assertRaisesRegex(TaskDocError, "membership-invalid"):
            self._task_doc(
                "master-a",
                "set_field",
                fields={"title": "Renamed away"},
            )
        write_task_doc(self.tasks / "master-c", _master(identity="MASTER-C"))
        replacement = read_task_doc(self.tasks / "master-c" / "task.json").model_dump(by_alias=True)
        replacement["title"] = "Alias A"
        with self.assertRaisesRegex(TaskDocError, "membership-invalid"):
            self._task_doc("master-c", "replace", fields=replacement)
        changed = self._task_doc(
            "master-a",
            "set_field",
            fields={"executionNature": "atomic"},
        )
        self.assertEqual(changed["status"], "planning")

        downgraded_master = read_task_doc(self.tasks / "master-a" / "task.json").model_dump(
            by_alias=True
        )
        downgraded_master.update(
            {
                "kind": "subTask",
                "slug": "task",
                "executionNature": None,
            }
        )
        with self.assertRaisesRegex(TaskDocError, "membership-invalid"):
            self._task_doc("master-a", "replace", fields=downgraded_master)

        downgraded_sprint = read_task_doc(self.tasks / "sprint" / "task.json").model_dump(
            by_alias=True
        )
        downgraded_sprint.update(
            {
                "kind": "subTask",
                "slug": "task",
                "orchestrates": [],
                "integrationBranch": None,
                "executionGraph": None,
            }
        )
        with self.assertRaisesRegex(TaskDocError, "cannot remove its execution topology"):
            self._task_doc("sprint", "replace", fields=downgraded_sprint)

    def test_bootstrap_previews_then_atomically_publishes_graph_natures_render_and_projection(
        self,
    ) -> None:
        self._write_legacy()
        before = {path: path.read_bytes() for path in self.tasks.rglob("*") if path.is_file()}
        preview = self._bootstrap(dry_run=True)
        self.assertEqual(preview["state"], "would-author")
        self.assertEqual(preview["bootstrapped"], True)
        self.assertEqual(len(preview["documents"]), 3)
        self.assertEqual(
            preview["executionWaves"], [[MASTER_A.model_dump()], [MASTER_B.model_dump()]]
        )
        self.assertEqual(
            before,
            {path: path.read_bytes() for path in self.tasks.rglob("*") if path.is_file()},
        )

        applied = self._bootstrap()
        self.assertEqual(applied["state"], "authored")
        self.assertEqual(applied["bootstrapped"], True)
        sprint = read_task_doc(self.tasks / "sprint" / "task.json")
        master_a = read_task_doc(self.tasks / "master-a" / "task.json")
        master_b = read_task_doc(self.tasks / "master-b" / "task.json")
        self.assertEqual(master_a.executionNature, "organizational")
        self.assertEqual(master_b.executionNature, "atomic")
        self.assertIsNotNone(sprint.executionGraph)
        assert sprint.executionGraph is not None
        self.assertEqual(
            [[node.ref for node in wave] for wave in sprint.executionGraph.derived_waves()],
            [[MASTER_A], [MASTER_B]],
        )
        self.assertEqual(
            [item.ref for item in self.topology.validate_execution_topology(SPRINT)],
            [MASTER_A, MASTER_B],
        )
        self.assertEqual(
            [[node.ref for node in wave] for wave in self.topology.execution_waves(SPRINT)],
            [[MASTER_A], [MASTER_B]],
        )
        rendered = (self.tasks / "sprint" / "task.md").read_text(encoding="utf-8")
        self.assertIn("## Execution Graph", rendered)
        self.assertIn(f"- `{MASTER_A.key}`", rendered)
        self.assertIn(
            f"- `{MASTER_A.key}` → `{MASTER_B.key}` — Shared contract must land first.",
            rendered,
        )
        self.assertIn(f"- Wave 1: `{MASTER_A.key}`", rendered)
        self.assertIn(
            "**Execution nature:** `atomic`",
            (self.tasks / "master-b" / "task.md").read_text(encoding="utf-8"),
        )
        projected = {
            node.id: node
            for node in read_task_documents(
                self.coord,
                enclosures=[],
                now=datetime.now(UTC),
            )
        }
        self.assertEqual(projected["MASTER-A"].executionNature, "organizational")
        self.assertEqual(
            [[node.ref for node in wave] for wave in projected["SPRINT"].executionWaves],
            [[MASTER_A], [MASTER_B]],
        )
        assert projected["SPRINT"].executionGraph is not None
        self.assertEqual(
            projected["SPRINT"].executionGraph.model_dump(mode="json"),
            {
                "nodes": [
                    {"kind": "master", "ref": MASTER_A.model_dump(), "leafIds": []},
                    {"kind": "master", "ref": MASTER_B.model_dump(), "leafIds": []},
                ],
                "edges": [
                    {
                        "predecessor": {"ref": MASTER_A.model_dump(), "leafId": None},
                        "successor": {"ref": MASTER_B.model_dump(), "leafId": None},
                        "reason": "Shared contract must land first.",
                        "judgmentId": "J-edge",
                    }
                ],
            },
        )

    def test_graph_authoring_refreshes_every_sprint_consuming_changed_master_nature(
        self,
    ) -> None:
        self._write_legacy()
        other_sprint = TaskDocumentRef(
            repository=REPOSITORY,
            path="other-sprint/task.json",
        )
        write_task_doc(
            self.tasks / "other-sprint",
            _master(identity="OTHER-SPRINT", orchestrates=["master-a"]).model_copy(
                update={"integrationBranch": "other-super"}
            ),
        )
        expected = {SPRINT, other_sprint}

        preview = self._bootstrap(dry_run=True)
        self.assertEqual(
            {
                TaskDocumentRef.model_validate(effect["sprintTaskDocumentRef"])
                for effect in preview["projectionEffects"]
            },
            expected,
        )

        applied = self._bootstrap()
        self.assertEqual(
            {
                TaskDocumentRef.model_validate(effect["sprintTaskDocumentRef"])
                for effect in applied["projectionEffects"]
            },
            expected,
        )

    def test_execution_waves_validates_and_returns_one_pinned_sprint_snapshot(self) -> None:
        self._write_legacy()
        self._bootstrap()
        sprint_path = self.tasks / "sprint" / "task.json"
        real_read = task_document_refs.read_task_doc_with_source
        sprint_reads = 0

        def counted_source_read(
            path: Path,
        ) -> tuple[TaskDocument, task_store.TaskDocSourceSnapshot]:
            nonlocal sprint_reads
            if path == sprint_path:
                sprint_reads += 1
            return real_read(path)

        with mock.patch.object(
            task_document_refs,
            "read_task_doc_with_source",
            side_effect=counted_source_read,
        ):
            self.assertEqual(
                [[node.ref for node in wave] for wave in self.topology.execution_waves(SPRINT)],
                [[MASTER_A], [MASTER_B]],
            )
        self.assertEqual(sprint_reads, 1)

    def test_bootstrap_refuses_a_segment_node_on_an_atomic_master(self) -> None:
        self._write_legacy()
        fields = self._bootstrap_fields()
        fields["mutations"][1] = {
            "op": "add_node",
            "ref": MASTER_B.model_dump(),
            "kind": "segment",
            "leafIds": ["L1"],
            "judgmentId": "J-edge",
        }
        with self.assertRaisesRegex(TaskDocError, "lump nodes only"):
            task_doc_tool(
                self.cfg,
                TaskDocTarget(repo_id=REPOSITORY, task_name="sprint"),
                operation="author_execution_graph",
                edit=TaskDocEdit(fields=fields),
            )

    def test_bootstrap_refuses_non_exact_membership_and_rolls_back_cross_root_failure(
        self,
    ) -> None:
        self._write_legacy()
        incomplete = self._bootstrap_fields()
        incomplete["mutations"] = [
            mutation
            for mutation in incomplete["mutations"]
            if mutation.get("ref") != MASTER_B.model_dump()
            and mutation.get("successor") != MASTER_B.model_dump()
        ]
        with self.assertRaisesRegex(TaskDocError, "membership must exactly match"):
            task_doc_tool(
                self.cfg,
                TaskDocTarget(repo_id=REPOSITORY, task_name="sprint"),
                operation="author_execution_graph",
                edit=TaskDocEdit(fields=incomplete),
            )

        before = {
            path: path.read_bytes()
            for path in self.tasks.rglob("*")
            if path.is_file() and path.suffix in {".json", ".md"}
        }
        real_write = task_store.atomic_write_text
        calls = 0

        def fail_third_write(path: Path, text: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("forced cross-root publication failure")
            real_write(path, text)

        with (
            mock.patch.object(task_store, "atomic_write_text", side_effect=fail_third_write),
            self.assertRaisesRegex(OSError, "forced cross-root"),
        ):
            self._bootstrap()
        self.assertEqual(
            before,
            {
                path: path.read_bytes()
                for path in self.tasks.rglob("*")
                if path.is_file() and path.suffix in {".json", ".md"}
            },
        )
        state_path, pending_path = queue_store_paths(self.coord, SPRINT)
        self.assertFalse(state_path.exists())
        self.assertFalse(pending_path.exists())

    def test_bootstrap_refuses_invalid_request_shapes_before_reading_or_writing(self) -> None:
        self._write_legacy()
        invalid_requests = (
            ("mutations", {}),
            ("at least 1 item", {"mutations": []}),
            ("mutations", {"mutations": [{"op": "explode"}]}),
        )
        before = {path: path.read_bytes() for path in self.tasks.rglob("*") if path.is_file()}
        for expected, fields in invalid_requests:
            with self.subTest(expected=expected), self.assertRaisesRegex(TaskDocError, expected):
                task_doc_tool(
                    self.cfg,
                    TaskDocTarget(repo_id=REPOSITORY, task_name="sprint"),
                    operation="author_execution_graph",
                    edit=TaskDocEdit(fields=fields),
                )
        self.assertEqual(
            before,
            {path: path.read_bytes() for path in self.tasks.rglob("*") if path.is_file()},
        )

    def test_bootstrap_refuses_missing_or_non_sprint_target(self) -> None:
        with self.assertRaisesRegex(TaskDocError, "task document not found"):
            self._bootstrap()

        write_task_doc(self.tasks / "sprint", _master(identity="SPRINT"))
        with self.assertRaisesRegex(TaskDocError, "requires an orchestration sprint"):
            self._bootstrap()

    def test_bootstrap_refuses_unresolved_or_non_master_entries(self) -> None:
        self._write_legacy()
        fields = self._bootstrap_fields()
        fields["mutations"].append(
            {
                "op": "set_nature",
                "ref": MASTER_C.model_dump(),
                "executionNature": "atomic",
                "judgmentId": "J-edge",
            }
        )
        with self.assertRaisesRegex(TaskDocError, "is not commanded by the sprint"):
            task_doc_tool(
                self.cfg,
                TaskDocTarget(repo_id=REPOSITORY, task_name="sprint"),
                operation="author_execution_graph",
                edit=TaskDocEdit(fields=fields),
            )

        write_task_doc(
            self.tasks / "master-b",
            TaskDocument.model_validate(
                {
                    "id": "MASTER-B",
                    "slug": "task",
                    "title": "MASTER-B",
                    "kind": "subTask",
                    "repo": REPOSITORY,
                    "type": "Code",
                    "createdAt": "2026-08-15T00:00:00+00:00",
                }
            ),
        )
        with self.assertRaisesRegex(TaskDocError, "is not commanded by the sprint"):
            self._bootstrap()

    def test_regular_master_edit_refuses_a_task_root_outside_the_repository(self) -> None:
        outside_contract = self.coord / "outside" / "series-contract.md"
        with self.assertRaisesRegex(TaskDocError, "outside tasks/agents-remember"):
            task_doc_tool(
                self.cfg,
                TaskDocTarget(
                    repo_id=REPOSITORY,
                    contract_path=outside_contract.as_posix(),
                ),
                operation="create",
                edit=TaskDocEdit(
                    fields=_master(
                        identity="OUTSIDE",
                        execution_nature="organizational",
                    ).model_dump(by_alias=True)
                ),
            )
        self.assertFalse((outside_contract.parent / "task.json").exists())
        self.assertFalse((outside_contract.parent / "task.md").exists())

    def test_bootstrap_normalizes_an_out_of_root_sprint_to_task_doc_error(self) -> None:
        outside = self.coord / "outside"
        write_task_doc(
            outside,
            _master(identity="SPRINT", orchestrates=["master-a", "master-b"]),
        )
        with self.assertRaisesRegex(TaskDocError, "outside tasks/agents-remember"):
            task_doc_tool(
                self.cfg,
                TaskDocTarget(
                    repo_id=REPOSITORY,
                    contract_path=(outside / "series-contract.md").as_posix(),
                ),
                operation="author_execution_graph",
                edit=TaskDocEdit(fields=self._bootstrap_fields()),
            )
