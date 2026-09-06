"""Tests for the internal ``spawn_agent_session`` primitive + serving paste endpoint.

Public callers use ``dispatch_agent``; this primitive composes the existing opener and seat claim.
These tests inject a fake host + fake paster + a fake ``which`` so the composition is exercised without a
real tmux server, and drive the ``POST /api/terminal/{session}/paste`` endpoint through ``TestClient``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.application.terminal_tools import (
    RetiredSpawnInputs,
    SpawnedBy,
    SpawnOverrides,
    SpawnSeat,
)
from agents_remember.kernel.agentic_settings import agentic_settings_path
from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
)
from agents_remember.mcp.tools.terminal import spawn_agent_session_payload
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
)
from agents_remember.observer import (
    reset_ambient,
)
from agents_remember.serving.harness_logs import CommandEvidence
from agents_remember.serving.terminal import (
    TerminalSessionBinding,
    TerminalSessionSpec,
)
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
)
from agents_remember.serving.terminal_paste import PasteResult
from agents_remember.tasks import (
    TaskDocument,
    write_task_doc,
)
from test_worktree_support import write_current_task_lineage

# The source root of the agents_remember package this test process imported -- what the opener
# seeds onto every harness-runner spawn's PYTHONPATH.
LEAF_REF = TaskDocumentRef(repository="repo", path="master/leaf-1.json")


_SEAT_FIELDS = (
    "kind",
    "task_document_ref",
    "replacement_for_task_document_ref",
    "level",
    "label",
    "env",
)
_RETIRED_FIELDS = (
    "context",
    "submit",
    "harness",
    "model",
    "effort",
    "launch_args",
    "prompt_keywords",
    "session_commands",
)
_OVERRIDE_FIELDS = ("session_id", "host", "paster", "session_log", "which")
_SPAWNED_BY_FIELDS = {
    "spawned_by_session": "session_id",
    "spawned_by_lifecycle": "lifecycle_id",
    "spawned_by_kind": "caller_kind",
    "structural_parent_task_document_ref": "structural_parent_task_document_ref",
    "structural_parent_role": "structural_parent_role",
}


def call_spawn(config: Any, **flat: Any) -> dict:
    """Call the payload builder from the flat kwargs these tests are written in.

    The builder takes four parameter objects (seat, retired inputs, spawner provenance,
    substituted collaborators); this keeps the test call sites reading as one spawn request.
    """
    unknown = (
        set(flat)
        - set(_SEAT_FIELDS)
        - set(_RETIRED_FIELDS)
        - set(_OVERRIDE_FIELDS)
        - set(_SPAWNED_BY_FIELDS)
    )
    assert not unknown, f"unknown spawn kwargs: {sorted(unknown)}"
    return spawn_agent_session_payload(
        config,
        seat=SpawnSeat(**{k: flat[k] for k in _SEAT_FIELDS if k in flat}),
        retired=RetiredSpawnInputs(**{k: flat[k] for k in _RETIRED_FIELDS if k in flat}),
        spawned_by=SpawnedBy(**{v: flat[k] for k, v in _SPAWNED_BY_FIELDS.items() if k in flat}),
        overrides=SpawnOverrides(**{k: flat[k] for k in _OVERRIDE_FIELDS if k in flat}),
    )


def _config(root: Path) -> McpRuntimeConfig:
    return McpRuntimeConfig(
        config_path=root / "settings.json",
        coordination_root=root,
        workspace_root=root,
        transcript_root=root / "logs" / "mcp",
    )


def _detected(_command: str) -> str | None:
    return "/usr/bin/harness"


def _write_leaf_task(
    coordination_root: Path,
    *,
    repo: str = "repo",
    master: str = "master",
    doc_id: str = "leaf-1",
    slug: str = "leaf-1",
) -> None:
    write_task_doc(
        coordination_root / "tasks" / repo / "sprint",
        TaskDocument.model_validate(
            {
                "id": "SPRINT",
                "slug": "task",
                "title": "Sprint",
                "kind": "master",
                "repo": repo,
                "createdAt": "2026-07-07T09:59",
                "orchestrates": [master],
            }
        ),
    )
    task_root = coordination_root / "tasks" / repo / master
    write_task_doc(
        task_root,
        TaskDocument.model_validate(
            {
                "id": master.upper(),
                "slug": "task",
                "title": "Master",
                "kind": "master",
                "repo": repo,
                "createdAt": "2026-07-07T10:00",
                "subTasks": [
                    {
                        "number": doc_id,
                        "name": "Leaf",
                        "file": f"{slug}.md",
                        "status": "inProgress",
                    }
                ],
            }
        ),
    )
    write_task_doc(
        task_root,
        TaskDocument.model_validate(
            {
                "id": doc_id,
                "slug": slug,
                "title": "Leaf",
                "kind": "subTask",
                "repo": repo,
                "createdAt": "2026-07-07T10:01",
                "master": "task.md",
            }
        ),
    )


class _FakeHost:
    def __init__(self) -> None:
        self.ensured: list[dict[str, object]] = []
        self.known: set[str] = set()

    def has_session(self, tmux_name: str) -> bool:
        return tmux_name in self.known

    def shutdown(self) -> None:
        # The create_app lifespan calls this on teardown; the fake has nothing to reap.
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


class _FakeLog:
    def __init__(self) -> None:
        self.bound_path: Path | None = None
        self.messages: set[str] = set()
        self.commands: dict[str, CommandEvidence] = {}

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_spawn_agent_session.py:195).
    def message_present(self, entry_id: str) -> bool:  # pragma: no cover
        return entry_id in self.messages

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_spawn_agent_session.py:198).
    def command_evidence(self, command: str) -> CommandEvidence:  # pragma: no cover
        return self.commands.get(command, CommandEvidence())


class _FakePaster:
    def __init__(
        self, *, delivered: bool = True, submitted: bool = True, capture: str = ""
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._delivered = delivered
        self._submitted = submitted
        self._capture = capture
        self.log = _FakeLog()

    # 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_spawn_agent_session.py:212).
    def paste(  # pragma: no cover
        self,
        tmux_name: str,
        text: str,
        *,
        submit: bool = False,
        accepted=None,
        **_kwargs: object,
    ) -> PasteResult:
        self.calls.append({"tmux": tmux_name, "text": text, "submit": submit})
        if self._delivered and submit and self._submitted:
            if text.startswith("/"):
                self.log.commands[text] = CommandEvidence(
                    recorded=True,
                    succeeded=True,
                    output="command ok",
                )
            if "id=" in text:
                entry_id = text.split("id=", 1)[1].split("]", 1)[0]
                self.log.messages.add(entry_id)
                self.log.bound_path = Path("/tmp/fake-session.jsonl")
        verified = bool(accepted()) if accepted is not None else False
        return PasteResult(
            delivered=self._delivered,
            submitted=self._submitted and verified if submit else False,
            capture=self._capture,
        )


def _running_chat(session_id: str, *, task_document_ref: TaskDocumentRef) -> TerminalCatalogEntry:
    return TerminalCatalogEntry(
        id=session_id,
        label="Claude Code",
        kind="harness",
        harness="claude",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("claude",),
        created_at="2026-07-04T00:00:00Z",
        last_attached_at="2026-07-04T00:00:00Z",
        status="running",
        task_document_ref=task_document_ref,
        spawn_role="worker",
    )


class SpawnAgentSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.config = _config(self.tmp)
        _write_leaf_task(self.tmp)
        self.repo = write_current_task_lineage(
            self.tmp,
            repo_name="repo",
            master_name="master",
            leaf_id="leaf-1",
        )
        self.catalog = TerminalCatalog(self.tmp / "logs" / "dashboard" / "terminal-sessions.json")
        self.host = _FakeHost()
        reset_ambient()

    def tearDown(self) -> None:
        reset_ambient()

    def _spawn(self, **kwargs: object) -> dict:
        base: dict[str, object] = {
            "session_id": "worker-1",
            "host": self.host,
            "which": _detected,
        }
        base.update(kwargs)
        paster = base.get("paster")
        if "session_log" not in base and isinstance(paster, _FakePaster):
            base["session_log"] = paster.log
        return call_spawn(self.config, **base)

    def test_spawns_bound_seat_without_brief_or_readiness_claim(self) -> None:
        paster = _FakePaster(delivered=True, submitted=True)
        payload = self._spawn(
            task_document_ref=LEAF_REF,
            spawned_by_session="manager-9",
            spawned_by_lifecycle="LC-manager",
            paster=paster,
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "spawned-unbriefed")
        self.assertEqual(payload["session"], "worker-1")
        self.assertEqual(payload["taskDocumentRef"], LEAF_REF.model_dump())
        self.assertEqual(payload["spawnedBySession"], "manager-9")
        self.assertEqual(payload["spawnedByLifecycle"], "LC-manager")
        self.assertNotIn("contextDelivered", payload)
        self.assertNotIn("submitted", payload)
        # Provenance persisted on the catalog row.
        row = self.catalog.get("worker-1")
        assert row is not None
        self.assertEqual(row.spawned_by_session, "manager-9")
        self.assertEqual(row.spawned_by_lifecycle, "LC-manager")
        self.assertEqual(paster.calls, [])

    def test_seat_taken_is_surfaced_never_overridden(self) -> None:
        path = agentic_settings_path(self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "orchestration": {
                        "roles": {
                            "worker": {
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
        self.catalog.upsert(_running_chat("owner-1", task_document_ref=LEAF_REF))
        self.host.known.add("ar-owner-1")
        paster = _FakePaster()
        payload = self._spawn(
            session_id="intruder",
            task_document_ref=LEAF_REF,
            env={"AR_SPAWN_ROLE": "worker"},
            paster=paster,
        )
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "seat-taken")
        self.assertEqual(payload["ownerSession"], "owner-1")
        # Never spawned or pasted.
        self.assertEqual(self.host.ensured, [])
        self.assertEqual(paster.calls, [])
        self.assertIsNone(self.catalog.get("intruder"))

    def test_spawn_refuses_forged_structural_parent_before_host_creation(self) -> None:
        parent = TaskDocumentRef(repository="repo", path="master/task.json")
        payload = self._spawn(
            task_document_ref=LEAF_REF,
            env={"AR_SPAWN_ROLE": "worker"},
            spawned_by_kind="plane",
            structural_parent_task_document_ref=parent,
            structural_parent_role="manager",
        )

        self.assertEqual(payload["status"], "task-binding-invalid")
        self.assertEqual(self.host.ensured, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
