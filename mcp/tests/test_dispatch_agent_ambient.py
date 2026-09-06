"""dispatch_agent ambient caller mode: spawning without plane-injected hosted identity."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.application.structural.agent_tools import (
    StructuralAgentRuntime,
    dispatch_agent_tool,
)
from agents_remember.application.terminal_tools import SpawnOverrides
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.kernel.agentic_settings import agentic_settings_path
from agents_remember.kernel.primitives.observer_paths import observer_root
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, RepositoryScope
from agents_remember.models.structural.agent import DispatchAgentRequest
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.terminal_catalog import TerminalCatalogEntry
from agents_remember.serving.ambient_seat import (
    AmbientSeatError,
    resolve_ambient_caller,
)
from agents_remember.serving.terminal import TerminalSessionBinding, TerminalSessionSpec
from agents_remember.serving.terminal_catalog import TerminalCatalog, terminal_catalog_path
from agents_remember.tasks import TaskDocument, write_task_doc


def _config(root: Path) -> McpRuntimeConfig:
    return McpRuntimeConfig(
        config_path=root / "settings.json",
        coordination_root=root,
        workspace_root=root,
        transcript_root=root / "logs" / "mcp",
        repositories={"repo": RepositoryScope("repo", root / "workspace" / "repo")},
    )


def _task_doc(**values: object) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": values.pop("id"),
            "slug": values.pop("slug"),
            "title": values.pop("title"),
            "kind": values.pop("kind"),
            "repo": "repo",
            "createdAt": "2026-08-11T00:00",
            **values,
        }
    )


def _write_topology(root: Path) -> tuple[TaskDocumentRef, TaskDocumentRef, TaskDocumentRef]:
    task_root = root / "tasks" / "repo"
    write_task_doc(
        task_root / "sprint",
        _task_doc(
            id="SPRINT",
            slug="sprint",
            title="Sprint",
            kind="master",
            orchestrates=["master"],
            integrationBranch="ar/super",
            executionGraph={
                "nodes": [
                    {"repository": "repo", "path": "master/task.json"},
                ],
                "edges": [],
            },
        ),
    )
    write_task_doc(
        task_root / "master",
        _task_doc(
            id="MASTER",
            slug="master",
            title="Master",
            kind="master",
            executionNature="atomic",
            subTasks=[
                {
                    "number": "leaf-1",
                    "name": "Leaf 1",
                    "file": "leaf-1.md",
                    "status": "inProgress",
                }
            ],
        ),
    )
    write_task_doc(
        task_root / "master",
        _task_doc(
            id="leaf-1",
            slug="leaf-1",
            title="Leaf 1",
            kind="subTask",
            master="task.md",
        ),
    )
    return (
        TaskDocumentRef(repository="repo", path="sprint/task.json"),
        TaskDocumentRef(repository="repo", path="master/task.json"),
        TaskDocumentRef(repository="repo", path="master/leaf-1.json"),
    )


def _seat(
    session_id: str,
    document: TaskDocumentRef,
    role: str,
    *,
    status: str = "running",
    spawned_by_kind: str | None = "ambient",
) -> TerminalCatalogEntry:
    return TerminalCatalogEntry(
        id=session_id,
        label=session_id,
        kind="harness",
        harness="codex",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("codex",),
        created_at="2026-08-11T00:00:00+00:00",
        last_attached_at="2026-08-11T00:00:00+00:00",
        status=status,  # type: ignore[arg-type]
        task_document_ref=document,
        seat_role=role,
        spawned_by_kind=spawned_by_kind,
    )


def _detected(_command: str) -> str | None:
    return "/usr/bin/harness"


def _write_architect_settings(root: Path) -> None:
    """A settings-owned architect launch selection (mirrors the spawn wire tests)."""
    path = agentic_settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "orchestration": {
                    "roles": {
                        "architect": {
                            "harness": "claude",
                            "model": "claude-fable-5",
                            "effort": "max",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class _FakeHost:
    """A terminal host that records spawns and never owns a real tmux session."""

    def __init__(self) -> None:
        self.ensured: list[dict[str, object]] = []
        self.known: set[str] = set()
        self.terminated: list[tuple[str, str | None]] = []

    def has_session(self, tmux_name: str) -> bool:
        return tmux_name in self.known

    def shutdown(self) -> None:
        return None

    def ensure(self, sid: str, spec: TerminalSessionSpec) -> TerminalSessionBinding:
        tmux_name = spec.tmux_name_for(sid)
        self.ensured.append({"sid": sid, "env": dict(spec.env or {}), "command": spec.command})
        self.known.add(tmux_name)
        return TerminalSessionBinding(
            sid=sid,
            tmux_name=tmux_name,
            cwd=spec.cwd,
            command=spec.command,
            lifecycle_id=spec.lifecycle_id,
            suspend_unsafe=spec.suspend_unsafe,
        )

    def terminate(self, sid: str, *, tmux_name: str | None = None) -> None:
        self.terminated.append((sid, tmux_name))


class DispatchAgentAmbientTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sprint, self.master, self.leaf = _write_topology(self.root)
        self.config = _config(self.root)
        self.catalog = TerminalCatalog(terminal_catalog_path(self.root))

    def test_ambient_dispatch_refuses_unknown_task_reference_before_spawn(self) -> None:
        with mock.patch(
            "agents_remember.application.structural.agent_tools.spawn_agent_session_tool"
        ) as spawn:
            result = dispatch_agent_tool(
                self.config,
                DispatchAgentRequest(
                    task_document_ref=TaskDocumentRef(
                        repository="repo", path="sprint/missing.json"
                    ),
                    role="architect",
                    brief="Design the sprint.",
                ),
                StructuralAgentRuntime(environ={}),
            )

        self.assertEqual(result["status"], "task-document-not-found")
        spawn.assert_not_called()

    def test_ambient_dispatch_refuses_role_altitude_mismatch_before_spawn(self) -> None:
        with mock.patch(
            "agents_remember.application.structural.agent_tools.spawn_agent_session_tool"
        ) as spawn:
            result = dispatch_agent_tool(
                self.config,
                DispatchAgentRequest(
                    task_document_ref=self.sprint,
                    role="worker",
                    brief="Work the sprint.",
                ),
                StructuralAgentRuntime(environ={}),
            )

        self.assertEqual(result["status"], "seat-role-altitude-mismatch")
        spawn.assert_not_called()

    def test_role_without_hosted_identity_never_falls_back_to_ambient_dispatch(self) -> None:
        with (
            self.assertRaisesRegex(AmbientSeatError, "without the plane-injected"),
            mock.patch(
                "agents_remember.application.structural.agent_tools.spawn_agent_session_tool"
            ) as spawn,
        ):
            resolve_ambient_caller(environ={"AR_SPAWN_ROLE": "architect"})
        spawn.assert_not_called()

        with mock.patch(
            "agents_remember.application.structural.agent_tools.spawn_agent_session_tool"
        ) as spawn:
            result = dispatch_agent_tool(
                self.config,
                DispatchAgentRequest(
                    task_document_ref=self.sprint,
                    role="orchestrator",
                    brief="Orchestrate the sprint.",
                ),
                StructuralAgentRuntime(environ={"AR_SPAWN_ROLE": "architect"}),
            )

        self.assertEqual(result["status"], "ambient-seat-incomplete")
        spawn.assert_not_called()

    def test_ambient_dispatch_runs_the_real_spawn_and_persists_the_brief(self) -> None:
        host = _FakeHost()
        _write_architect_settings(self.root)
        result = dispatch_agent_tool(
            self.config,
            DispatchAgentRequest(
                task_document_ref=self.sprint,
                role="architect",
                brief="Design the sprint.",
            ),
            StructuralAgentRuntime(
                host=host,  # type: ignore[arg-type]
                spawn_overrides=SpawnOverrides(host=host, which=_detected),  # type: ignore[arg-type]
                environ={},
            ),
        )

        self.assertEqual(result["status"], "dispatch-queued")
        self.assertEqual(result["taskDocumentRef"], self.sprint.model_dump())
        rows = self.catalog.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].spawned_by_kind, "ambient")
        self.assertEqual(rows[0].binding_role, "architect")
        inbox = next(iter(OperatorInboxStore(observer_root(self.config)).current().values()))
        self.assertEqual(inbox.messageKind, "dispatch-brief")
        self.assertIsNone(inbox.senderAgentId)

    def test_ambient_dispatch_rolls_back_via_system_closure_when_brief_persistence_fails(
        self,
    ) -> None:
        host = _FakeHost()
        _write_architect_settings(self.root)
        with mock.patch.object(
            OperatorInboxStore,
            "append",
            side_effect=OSError("append refused"),
        ):
            result = dispatch_agent_tool(
                self.config,
                DispatchAgentRequest(
                    task_document_ref=self.sprint,
                    role="architect",
                    brief="Design the sprint.",
                ),
                StructuralAgentRuntime(
                    host=host,  # type: ignore[arg-type]
                    spawn_overrides=SpawnOverrides(host=host, which=_detected),  # type: ignore[arg-type]
                    environ={},
                ),
            )

        self.assertEqual(result["status"], "dispatch-persistence-refused")
        self.assertIn("child retired", result["detail"])
        rows = self.catalog.list(include_terminated=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "terminated")
        self.assertEqual([entry for entry, _ in host.terminated], [rows[0].id])

    def test_plane_dispatch_refuses_broken_plane_identity_without_downgrading(self) -> None:
        with mock.patch(
            "agents_remember.application.structural.agent_tools.spawn_agent_session_tool"
        ) as spawn:
            result = dispatch_agent_tool(
                self.config,
                DispatchAgentRequest(
                    task_document_ref=self.sprint,
                    role="architect",
                    brief="Design the sprint.",
                ),
                StructuralAgentRuntime(
                    environ={"AR_HOSTED_SESSION_ID": "ghost", "AR_SPAWN_ROLE": "architect"}
                ),
            )

        self.assertEqual(result["status"], "ambient-seat-stale")
        spawn.assert_not_called()

    def test_plane_dispatch_refuses_an_unauthorized_child_role(self) -> None:
        self.catalog.upsert(_seat("architect", self.sprint, "architect", spawned_by_kind="plane"))
        with mock.patch(
            "agents_remember.application.structural.agent_tools.spawn_agent_session_tool"
        ) as spawn:
            result = dispatch_agent_tool(
                self.config,
                DispatchAgentRequest(
                    task_document_ref=self.sprint,
                    role="system-specialist",
                    brief="Investigate the sprint.",
                ),
                StructuralAgentRuntime(
                    environ={
                        "AR_HOSTED_SESSION_ID": "architect",
                        "AR_SPAWN_ROLE": "architect",
                    }
                ),
            )

        self.assertEqual(result["status"], "structural-child-refused")
        spawn.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
