"""Tests for seat lifecycle: landing, retirement, live identity/rename, and turn-state.

Covers the leaf doc's failing-first list: the retire policy matrix, integrate/finalize auto-land
hooks, rename projection + provenance, turn-state classification from scripted pane fixtures, and
the terminal-mark vs liveness interplay.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest import mock

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.application import completion_cleanup, worktree_tools
from agents_remember.application.lifecycle.configured_contract_admission import (
    ConfiguredContractAccepted,
)
from agents_remember.application.lifecycle.lifecycle_operation_worker import (
    integration_completion_payload,
)
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
    InboxRouting,
    InboxSubject,
    create_operator_inbox_entry,
)
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
    RepositoryScope,
    RetirementSettings,
)
from agents_remember.mcp.tools.terminal import session_rename_payload, session_retire_payload
from agents_remember.models.lifecycles.operation import IntegrateOperationInput
from agents_remember.models.operator_inbox import AgentRole
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.terminal_catalog import (
    TerminalCatalogEntry,
)
from agents_remember.serving.landing import land_seats_for_task
from agents_remember.serving.retire import SeatClosure
from agents_remember.serving.retire_policy import (
    RetirePolicyError,
    SeatRef,
    check_retire_authority,
)
from agents_remember.serving.terminal_catalog import (
    TerminalCatalog,
)
from agents_remember.serving.terminal_liveness import (
    LivenessProbe,
    TerminalCatalogLivenessConfig,
    observe_terminal_liveness,
)
from agents_remember.serving.terminal_paste import TmuxPaneCapturer
from agents_remember.serving.turn_state import classify_turn_state
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.tasks.document_refs import TaskDocumentTopology
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import WorktreeContract

SPRINT = TaskDocumentRef(repository="repo", path="sprint/task.json")
MASTER_A = TaskDocumentRef(repository="repo", path="master-a/task.json")
LEAF_A1 = TaskDocumentRef(repository="repo", path="master-a/leaf-1.json")
LEAF_A2 = TaskDocumentRef(repository="repo", path="master-a/leaf-2.json")
OTHER_LEAF_A = TaskDocumentRef(repository="repo", path="master-a/other-leaf.json")
MASTER_B = TaskDocumentRef(repository="repo", path="master-b/task.json")
LEAF_B2 = TaskDocumentRef(repository="repo", path="master-b/leaf-2.json")


def _task_doc(**values: object) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": values.pop("id"),
            "slug": values.pop("slug"),
            "title": values.pop("title"),
            "kind": values.pop("kind"),
            "repo": "repo",
            "createdAt": "2026-07-07T00:00",
            **values,
        }
    )


def _write_task_topology(root: Path) -> None:
    task_root = root / "tasks" / "repo"
    write_task_doc(
        task_root / "sprint",
        _task_doc(
            id="SPRINT",
            slug="sprint",
            title="Sprint",
            kind="master",
            orchestrates=["master-a", "master-b"],
        ),
    )
    for master, leaves in (
        ("master-a", ("leaf-1", "leaf-2", "other-leaf")),
        ("master-b", ("leaf-2",)),
    ):
        write_task_doc(
            task_root / master,
            _task_doc(
                id=master,
                slug=master,
                title=master,
                kind="master",
                subTasks=[
                    {
                        "number": leaf,
                        "name": leaf,
                        "file": f"{leaf}.md",
                        "status": "inProgress",
                    }
                    for leaf in leaves
                ],
            ),
        )
        for leaf in leaves:
            write_task_doc(
                task_root / master,
                _task_doc(
                    id=leaf,
                    slug=leaf,
                    title=leaf,
                    kind="subTask",
                    master="task.md",
                ),
            )


def _config(root: Path, **retirement_overrides: bool) -> McpRuntimeConfig:
    _write_task_topology(root)
    return McpRuntimeConfig(
        config_path=root / "settings.json",
        coordination_root=root,
        workspace_root=root,
        transcript_root=root / "logs" / "mcp",
        repositories={
            "repo": RepositoryScope(
                repo_id="repo",
                path=root / "workspace" / "repo",
            )
        },
        retirement=RetirementSettings(**retirement_overrides)
        if retirement_overrides
        else RetirementSettings(),
    )


def _get(catalog: TerminalCatalog, session_id: str) -> TerminalCatalogEntry:
    entry = catalog.get(session_id)
    assert entry is not None
    return entry


def _entry(
    session_id: str,
    *,
    task_document_ref: TaskDocumentRef | None = None,
    spawn_role: str | None = None,
) -> TerminalCatalogEntry:
    """A running harness seat holding one task document as ``spawn_role``.

    Those two are what every case here varies -- the leaf a seat occupies and the role that
    occupies it. Rarer shapes (a terminated row, a plain terminal row, an unbound replacement)
    are ``replace(...)`` on the frozen ``TerminalCatalogEntry``, which already owns the fields.
    """
    return TerminalCatalogEntry(
        id=session_id,
        label=f"Seat {session_id}",
        kind="harness",
        harness="claude",
        lifecycle_id=None,
        cwd=Path("/workspace"),
        tmux_name=f"ar-{session_id}",
        command=("claude",),
        created_at="2026-07-07T00:00:00+00:00",
        last_attached_at="2026-07-07T00:00:00+00:00",
        status="running",
        task_document_ref=task_document_ref,
        spawn_role=spawn_role,
    )


class _FakeHost:
    """A minimal ``terminate``-capable host: retirement never needs a real tmux server."""

    def __init__(self) -> None:
        self.terminated: list[str] = []

    def terminate(self, sid: str, *, tmux_name: str | None = None) -> None:  # noqa: ARG002
        self.terminated.append(sid)


# --- Retire policy matrix (E1) --------------------------------------------------------------


class RetirePolicyMatrixTests(unittest.TestCase):
    """The exact authority matrix from the leaf doc's failing-first list."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        _write_task_topology(root)
        self.topology = TaskDocumentTopology(root)

    def test_manager_retires_own_worker(self) -> None:
        actor = SeatRef("mgr", MASTER_A, "manager")
        target = SeatRef("worker-1", LEAF_A1, "worker")
        check_retire_authority(actor, target, self.topology)  # must not raise

    def test_manager_retires_own_reviewer(self) -> None:
        actor = SeatRef("mgr", MASTER_A, "manager")
        target = SeatRef("rev-1", LEAF_A1, "reviewer")
        check_retire_authority(actor, target, self.topology)  # must not raise

    def test_manager_retires_its_same_master_exit_reviewer(self) -> None:
        actor = SeatRef("mgr", MASTER_A, "manager")
        target = SeatRef("master-reviewer", MASTER_A, "reviewer")
        check_retire_authority(actor, target, self.topology)  # must not raise

    def test_architect_retires_only_its_same_sprint_plan_reviewer(self) -> None:
        actor = SeatRef("architect", SPRINT, "architect")
        check_retire_authority(
            actor,
            SeatRef(
                "plan-reviewer",
                SPRINT,
                "reviewer",
                structural_parent_task_document_ref=SPRINT,
                structural_parent_role="architect",
            ),
            self.topology,
        )
        with self.assertRaisesRegex(RetirePolicyError, "same-sprint plan-review"):
            check_retire_authority(
                actor,
                SeatRef(
                    "super-reviewer",
                    SPRINT,
                    "reviewer",
                    structural_parent_task_document_ref=SPRINT,
                    structural_parent_role="orchestrator",
                ),
                self.topology,
            )
        with self.assertRaisesRegex(RetirePolicyError, "same-sprint plan-review"):
            check_retire_authority(
                actor,
                SeatRef("leaf-reviewer", LEAF_A1, "reviewer"),
                self.topology,
            )

    def test_manager_refused_against_other_masters_worker(self) -> None:
        actor = SeatRef("mgr", MASTER_A, "manager")
        target = SeatRef("worker-1", LEAF_B2, "worker")
        with self.assertRaisesRegex(RetirePolicyError, "own master"):
            check_retire_authority(actor, target, self.topology)

    def test_manager_refused_against_a_manager_seat(self) -> None:
        actor = SeatRef("mgr-a", MASTER_A, "manager")
        target = SeatRef("mgr-b", MASTER_A, "manager")
        with self.assertRaisesRegex(RetirePolicyError, "own master"):
            check_retire_authority(actor, target, self.topology)

    def test_no_seat_retires_itself(self) -> None:
        actor = SeatRef("same", SPRINT, "orchestrator")
        target = SeatRef("same", SPRINT, "orchestrator")
        with self.assertRaisesRegex(RetirePolicyError, "never retires itself"):
            check_retire_authority(actor, target, self.topology)

    def test_manager_cannot_self_retire_even_against_worker_role_confusion(self) -> None:
        # Owner-never-self-retires is checked BEFORE the role-scoped rule -- no role authority
        # ever overrides it, even a manager whose own row were somehow role-tagged "worker".
        actor = SeatRef("same", LEAF_A1, "worker")
        with self.assertRaisesRegex(RetirePolicyError, "never retires itself"):
            check_retire_authority(actor, actor, self.topology)

    def test_orchestrator_retires_a_completed_manager(self) -> None:
        actor = SeatRef("orch", SPRINT, "orchestrator")
        target = SeatRef("mgr", MASTER_A, "manager")
        check_retire_authority(actor, target, self.topology)  # must not raise

    def test_orchestrator_retires_any_role(self) -> None:
        actor = SeatRef("orch", SPRINT, "orchestrator")
        for role in ("worker", "reviewer", "manager", "designer", "strategist"):
            with self.subTest(role=role):
                check_retire_authority(
                    actor,
                    SeatRef(f"x-{role}", LEAF_A1, role),
                    self.topology,
                )

    def test_unprivileged_role_has_no_retire_authority(self) -> None:
        actor = SeatRef("w", LEAF_A1, "worker")
        target = SeatRef("other", LEAF_A1, "worker")
        with self.assertRaisesRegex(RetirePolicyError, "no retire authority"):
            check_retire_authority(actor, target, self.topology)


# --- session_retire MCP tool ------------------------------------------------------------------


class SessionRetireToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.config = _config(self.root)
        self.catalog = TerminalCatalog(self.root / "logs" / "dashboard" / "terminal-sessions.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _with_catalog_patched(self, fn):
        with (
            mock.patch(
                "agents_remember.application.terminal_tools.TerminalCatalog",
                return_value=self.catalog,
            ),
            mock.patch(
                "agents_remember.application.terminal_tools.TerminalHost", return_value=_FakeHost()
            ),
        ):
            return fn()

    def test_unknown_target_session_is_refused(self) -> None:
        result = self._with_catalog_patched(
            lambda: session_retire_payload(
                self.config, actor_session_id="mgr", session_id="missing"
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown-session")

    def test_unknown_actor_session_is_refused(self) -> None:
        self.catalog.upsert(_entry("worker-1", task_document_ref=LEAF_A1, spawn_role="worker"))
        result = self._with_catalog_patched(
            lambda: session_retire_payload(
                self.config, actor_session_id="missing-actor", session_id="worker-1"
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown-actor")

    def test_manager_retires_own_worker_end_to_end(self) -> None:
        self.catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))
        self.catalog.upsert(_entry("worker-1", task_document_ref=LEAF_A1, spawn_role="worker"))
        result = self._with_catalog_patched(
            lambda: session_retire_payload(
                self.config, actor_session_id="mgr", session_id="worker-1", reason="leaf done"
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "retired")
        self.assertEqual(result["retiredBySession"], "mgr")
        self.assertEqual(result["retiredReason"], "leaf done")
        self.assertEqual(result["retiredEdge"], "manual")
        retired = _get(self.catalog, "worker-1")
        self.assertEqual(retired.status, "terminated")
        self.assertIsNotNone(retired.retired_at)

    def test_manager_retires_unbound_failed_dispatch_from_replacement_task(self) -> None:
        self.catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))
        self.catalog.upsert(
            replace(
                _entry("worker-failed", spawn_role="worker"),
                replacement_for_task_document_ref=LEAF_A1,
            )
        )

        result = self._with_catalog_patched(
            lambda: session_retire_payload(
                self.config,
                actor_session_id="mgr",
                session_id="worker-failed",
                reason="failed dispatch cleanup",
            )
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "retired")

    def test_manager_refused_against_other_masters_worker(self) -> None:
        self.catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))
        self.catalog.upsert(_entry("worker-2", task_document_ref=LEAF_B2, spawn_role="worker"))
        result = self._with_catalog_patched(
            lambda: session_retire_payload(
                self.config, actor_session_id="mgr", session_id="worker-2"
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "retire-refused")
        self.assertIn("own master", result["detail"])
        # Never terminated -- a refused retire touches nothing.
        self.assertEqual(_get(self.catalog, "worker-2").status, "running")

    def test_no_seat_retires_itself(self) -> None:
        self.catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))
        result = self._with_catalog_patched(
            lambda: session_retire_payload(self.config, actor_session_id="mgr", session_id="mgr")
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "retire-refused")
        self.assertIn("never retires itself", result["detail"])

    def test_retiring_an_already_retired_seat_is_idempotent(self) -> None:
        self.catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))
        self.catalog.upsert(_entry("worker-1", task_document_ref=LEAF_A1, spawn_role="worker"))
        first = self._with_catalog_patched(
            lambda: session_retire_payload(
                self.config, actor_session_id="mgr", session_id="worker-1"
            )
        )
        self.assertEqual(first["status"], "retired")
        second = self._with_catalog_patched(
            lambda: session_retire_payload(
                self.config,
                actor_session_id="mgr",
                session_id="worker-1",
                reason="different reason",
            )
        )
        self.assertTrue(second["ok"])
        self.assertEqual(second["status"], "already-retired")
        # Provenance from the FIRST retire is preserved, never re-stamped by the second call.
        self.assertEqual(second["retiredReason"], first["retiredReason"])

    def test_orchestrator_retires_a_manager(self) -> None:
        self.catalog.upsert(_entry("orch", task_document_ref=SPRINT, spawn_role="orchestrator"))
        self.catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))
        result = self._with_catalog_patched(
            lambda: session_retire_payload(
                self.config, actor_session_id="orch", session_id="mgr", reason="master finalized"
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "retired")


# --- session_rename MCP tool: projection + provenance -----------------------------------------


class SessionRenameToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.config = _config(self.root)
        self.catalog = TerminalCatalog(self.root / "logs" / "dashboard" / "terminal-sessions.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _rename(self, session_id: str, label: str) -> dict:
        with mock.patch(
            "agents_remember.application.terminal_tools.TerminalCatalog", return_value=self.catalog
        ):
            return session_rename_payload(self.config, session_id=session_id, label=label)

    def test_unknown_session_is_refused(self) -> None:
        result = self._rename("missing", "New Name")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unknown-session")

    def test_first_rename_freezes_the_spawn_time_label_for_audit(self) -> None:
        self.catalog.upsert(_entry("worker-1"))  # label defaults to "Seat worker-1"
        result = self._rename("worker-1", "Alice's Worker")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "renamed")
        self.assertEqual(result["label"], "Alice's Worker")
        self.assertEqual(result["spawnedLabel"], "Seat worker-1")
        # Surfaced through the catalog projection (``to_json``) immediately.
        projected = _get(self.catalog, "worker-1").to_json()
        self.assertEqual(projected["label"], "Alice's Worker")
        self.assertEqual(projected["spawnedLabel"], "Seat worker-1")

    def test_second_rename_never_overwrites_the_original_spawn_label(self) -> None:
        self.catalog.upsert(_entry("worker-1"))
        self._rename("worker-1", "First Rename")
        result = self._rename("worker-1", "Second Rename")
        self.assertEqual(result["label"], "Second Rename")
        self.assertEqual(result["spawnedLabel"], "Seat worker-1")

    def test_rename_never_touches_the_spawned_role(self) -> None:
        # L6 role-seat immutability: identity text only.
        self.catalog.upsert(_entry("worker-1", spawn_role="worker"))
        self._rename("worker-1", "Renamed")
        self.assertEqual(_get(self.catalog, "worker-1").spawn_role, "worker")

    def test_rename_of_a_retired_session_is_refused(self) -> None:
        self.catalog.upsert(replace(_entry("worker-1"), status="terminated"))
        result = self._rename("worker-1", "Too Late")
        self.assertEqual(result["status"], "unknown-session")


# --- Turn-state classification from scripted pane fixtures -------------------------------------


class TurnStateClassificationTests(unittest.TestCase):
    def test_busy_marker_classifies_working(self) -> None:
        result = classify_turn_state("some output\n(esc to interrupt)\n", harness="claude")
        self.assertEqual(result.state, "working")

    def test_spinner_glyph_classifies_working(self) -> None:
        result = classify_turn_state("⠋ Thinking about the request", harness="claude")
        self.assertEqual(result.state, "working")

    def test_confirmation_prompt_classifies_awaiting_input(self) -> None:
        result = classify_turn_state(
            "Do you want to proceed with this edit? (y/n)", harness="claude"
        )
        self.assertEqual(result.state, "awaiting-input")

    def test_idle_prompt_classifies_turn_ended(self) -> None:
        result = classify_turn_state("assistant: done.\n>\n", harness="claude")
        self.assertEqual(result.state, "turn-ended")

    def test_modern_codex_composer_classifies_turn_ended(self) -> None:
        result = classify_turn_state(
            "header\n\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} Explain this codebase",
            harness="codex",
        )
        self.assertEqual(result.state, "turn-ended")

    def test_midline_codex_glyph_in_transcript_does_not_mark_ready(self) -> None:
        result = classify_turn_state(
            "assistant described the glyph "
            "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} inside prose",
            harness="codex",
        )
        self.assertEqual(result.state, "stale")

    def test_historical_codex_prompt_line_does_not_mark_ready(self) -> None:
        result = classify_turn_state(
            "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} previous submitted user prompt\n"
            "■ You've hit your usage limit.",
            harness="codex",
        )
        self.assertEqual(result.state, "stale")

    def test_nonempty_codex_composer_draft_does_not_mark_ready(self) -> None:
        result = classify_turn_state(
            "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} unrelated existing draft",
            harness="codex",
        )
        self.assertEqual(result.state, "stale")

    def test_codex_empty_composer_with_footer_marks_ready(self) -> None:
        result = classify_turn_state(
            "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} Explain this codebase\n"
            "\n  gpt-5.6-sol xhigh fast · ~/Projects",
            harness="codex",
        )
        self.assertEqual(result.state, "turn-ended")

    def test_historical_thinking_prose_does_not_override_live_codex_composer(self) -> None:
        result = classify_turn_state(
            "I was thinking about this earlier.\n"
            "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} Explain this codebase\n\n"
            "  gpt-5.6-sol xhigh fast · ~/Projects",
            harness="codex",
        )
        self.assertEqual(result.state, "turn-ended")

    def test_empty_capture_classifies_stale(self) -> None:
        result = classify_turn_state("", harness="claude")
        self.assertEqual(result.state, "stale")

    def test_none_capture_classifies_stale(self) -> None:
        result = classify_turn_state(None, harness="claude")
        self.assertEqual(result.state, "stale")

    def test_unrecognized_pane_shape_classifies_stale(self) -> None:
        result = classify_turn_state("some garbled bytes with no known marker", harness=None)
        self.assertEqual(result.state, "stale")

    def test_busy_marker_takes_precedence_over_idle_shaped_text(self) -> None:
        # A busy marker anywhere in the capture wins even if the pane ALSO contains
        # something that looks like an idle prompt further up the scrollback.
        result = classify_turn_state(">\nesc to interrupt", harness="claude")
        self.assertEqual(result.state, "working")

    def test_busy_marker_takes_precedence_over_codex_composer(self) -> None:
        result = classify_turn_state(
            "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK} "
            "Explain this codebase\nesc to interrupt",
            harness="codex",
        )
        self.assertEqual(result.state, "working")


class TurnStateSweepWiringTests(unittest.TestCase):
    """Turn-state classification rides the L5 liveness sweep call -- no second probe."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.catalog = TerminalCatalog(self.root / "terminal-sessions.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_alive_legacy_harness_pane_is_diagnostic_only(self) -> None:
        entry = _entry("chat-1")
        self.catalog.upsert(entry)

        class _AliveHost:
            def get(self, _sid: str) -> None:
                return None

            def has_session(self, tmux_name: str) -> bool:  # noqa: ARG002
                return True

        def capture(_tmux_name: str) -> str:
            return "(esc to interrupt)"

        capturer: TmuxPaneCapturer = capture
        observation = observe_terminal_liveness(
            self.catalog,
            _AliveHost(),
            entry,
            checked_at=datetime(2026, 7, 8, tzinfo=UTC),
            probe=LivenessProbe(hysteresis=TerminalCatalogLivenessConfig(), pane_capturer=capturer),
        )
        self.assertTrue(observation.alive)
        self.assertTrue(observation.turn_state_changed)
        self.assertEqual(observation.entry.turn_state, "stale")
        self.assertEqual(observation.entry.control_state, "unsupported")
        assert observation.entry.control_raw is not None
        self.assertEqual(observation.entry.control_raw["paneDiagnostic"], "working")

    def test_plain_terminal_rows_are_never_classified(self) -> None:
        entry = replace(_entry("term-1"), kind="terminal", harness=None)
        self.catalog.upsert(entry)

        class _AliveHost:
            def get(self, _sid: str) -> None:
                return None

            def has_session(self, tmux_name: str) -> bool:  # noqa: ARG002
                return True

        calls: list[str] = []

        def _capture(tmux_name: str) -> str:
            calls.append(tmux_name)
            return "(esc to interrupt)"

        observation = observe_terminal_liveness(
            self.catalog,
            _AliveHost(),
            entry,
            checked_at=datetime(2026, 7, 8, tzinfo=UTC),
            probe=LivenessProbe(hysteresis=TerminalCatalogLivenessConfig(), pane_capturer=_capture),
        )
        self.assertIsNone(observation.entry.turn_state)
        self.assertEqual(calls, [])  # never captured for a plain terminal row


# --- Terminal-mark vs liveness interplay --------------------------------------------------------


class TerminalMarkVsLivenessInterplayTests(unittest.TestCase):
    """A retire is an explicit terminal mark: liveness hysteresis never resurrects it."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.catalog = TerminalCatalog(self.root / "terminal-sessions.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_retired_row_stays_terminated_after_an_alive_liveness_probe(self) -> None:
        self.catalog.upsert(_entry("worker-1"))
        self.catalog.mark_retired(
            "worker-1",
            at="2026-07-08T00:00:00+00:00",
            by_session="mgr",
            reason="done",
            edge="manual",
        )
        updated = self.catalog.record_liveness_probe(
            "worker-1", alive=True, checked_at=datetime(2026, 7, 8, 0, 1, tzinfo=UTC)
        )
        assert updated is not None
        self.assertEqual(updated.status, "terminated")
        self.assertEqual(updated.retired_reason, "done")

    def test_retiring_an_already_terminated_row_does_not_restamp_provenance(self) -> None:
        self.catalog.upsert(_entry("worker-1"))
        self.catalog.mark_terminated("worker-1", "2026-07-08T00:00:00+00:00")
        result = self.catalog.mark_retired(
            "worker-1",
            at="2026-07-08T01:00:00+00:00",
            by_session="mgr",
            reason="late retire",
            edge="manual",
        )
        assert result is not None
        # Already terminated (via plain /terminate, not retire) -- with_retirement is a no-op,
        # so it never gets retroactive retirement provenance from a later retire call.
        self.assertIsNone(result.retired_at)


# --- Automation hooks: integrate / finalize auto-land --------------------------------------------


class LandSeatsForTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.catalog = TerminalCatalog(self.root / "terminal-sessions.json")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_lands_only_matching_role_and_leaf_without_terminating(self) -> None:
        self.catalog.upsert(_entry("worker-1", task_document_ref=LEAF_A1, spawn_role="worker"))
        self.catalog.upsert(_entry("reviewer-1", task_document_ref=LEAF_A1, spawn_role="reviewer"))
        self.catalog.upsert(
            _entry("worker-other-leaf", task_document_ref=LEAF_A2, spawn_role="worker")
        )
        self.catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))

        landed = land_seats_for_task(
            self.catalog,
            SeatClosure(
                reason="leaf integrated", edge="leaf-integration", at="2026-07-08T00:00:00+00:00"
            ),
            task_document_ref=LEAF_A1,
            roles=frozenset({"worker", "reviewer"}),
        )

        self.assertEqual({entry.id for entry in landed}, {"worker-1", "reviewer-1"})
        self.assertEqual(_get(self.catalog, "worker-1").status, "landed")
        self.assertEqual(_get(self.catalog, "reviewer-1").landed_edge, "leaf-integration")
        self.assertEqual(_get(self.catalog, "mgr").status, "running")
        self.assertEqual(_get(self.catalog, "worker-other-leaf").status, "running")

    def test_terminated_seats_are_skipped_by_landing(self) -> None:
        self.catalog.upsert(
            replace(
                _entry("worker-1", task_document_ref=LEAF_A1, spawn_role="worker"),
                status="terminated",
            )
        )
        landed = land_seats_for_task(
            self.catalog,
            SeatClosure(reason="x", edge="leaf-integration", at="2026-07-08T00:00:00+00:00"),
            task_document_ref=LEAF_A1,
            roles=frozenset({"worker"}),
        )
        self.assertEqual(landed, [])


class AutoLandHookIntegrationTests(unittest.TestCase):
    """The completion-edge cleanup wiring in the detached integration owner."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self.contract_path = self.root / "enclosures" / "contract.md"
        self.contract_path.parent.mkdir(parents=True, exist_ok=True)
        self.contract_path.write_text("placeholder", encoding="utf-8")
        self.fake_contract = WorktreeContract(
            task_id="leaf-1",
            task_name="Leaf",
            repo_name="repo",
            workflow_kind="light-task",
            memory_mode="internal",
            coordination_root=self.root,
            task_root=self.root / "tasks" / "repo" / "master-a",
            contract_path=self.contract_path,
            task_artifact=self.contract_path,
            worktree_group=self.root / "worktrees",
            code_repo_path=self.root / "workspace" / "repo",
            code_source_branch="main",
            code_work_branch="leaf-1",
            code_base_commit="abc123",
            code_worktree=self.root / "worktrees" / "leaf-1",
        )

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _config(self, **retirement_overrides: bool) -> McpRuntimeConfig:
        return _config(self.root, **retirement_overrides)

    def _catalog(self) -> TerminalCatalog:
        return TerminalCatalog(self.root / "logs" / "dashboard" / "terminal-sessions.json")

    def _complete_integration(self, config: McpRuntimeConfig) -> dict[str, object]:
        return integration_completion_payload(
            config,
            IntegrateOperationInput(
                configPath=config.config_path.as_posix(),
                contractPath=self.contract_path.as_posix(),
                autoCompleteSeats=config.retirement.auto_land_on_integration,
            ),
            WorktreeCommandResult(0, {"state": "integrated"}),
        )

    def _post_report(
        self,
        session_id: str,
        *,
        task_document_ref: TaskDocumentRef = LEAF_A1,
        role: AgentRole = "worker",
    ) -> None:
        OperatorInboxStore(self.root / "logs" / "observer").append(
            create_operator_inbox_entry(
                InboxMessage(
                    ask="Turn report",
                    response=f"Completed by {session_id}",
                    message_kind="turn-report",
                    artifact_path=f"notes/reports/{session_id}.md",
                    subject=InboxSubject(
                        task_document_ref=task_document_ref,
                        seat_role=role,
                        agent_id=session_id,
                    ),
                ),
                entry_id=f"report-{session_id}-{task_document_ref.key}",
                now="2026-08-05T10:00:00+00:00",
                routing=InboxRouting(address=InboxAddress(recipient_role="manager")),
                poster=InboxPoster(
                    created_by="model",
                    created_via="cli",
                    sender_agent_id=session_id,
                    sender_role=role,
                ),
            )
        )

    def test_integrate_closes_reported_leaf_roles_and_retains_transcript(self) -> None:
        catalog = self._catalog()
        seat_roles: tuple[tuple[str, AgentRole], ...] = (
            ("worker-1", "worker"),
            ("reviewer-1", "reviewer"),
            ("curator-1", "curator"),
        )
        for session_id, role in seat_roles:
            catalog.upsert(_entry(session_id, task_document_ref=LEAF_A1, spawn_role=role))
            self._post_report(session_id, role=role)
        catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))
        catalog.upsert(_entry("orch", task_document_ref=SPRINT, spawn_role="orchestrator"))
        transcript = self.root / "logs" / "mcp" / "worker-1.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text('{"report":"retained"}\n', encoding="utf-8")
        host = _FakeHost()

        with (
            mock.patch.object(completion_cleanup, "load_contract", return_value=self.fake_contract),
            mock.patch.object(completion_cleanup, "TerminalHost", return_value=host),
        ):
            result = self._complete_integration(self._config())

        closed = {"worker-1", "reviewer-1", "curator-1"}
        self.assertTrue(result["ok"])
        self.assertEqual(set(cast(list[str], result["autoClosedSeats"])), closed)
        self.assertEqual(result["autoCloseDeferredSeats"], [])
        self.assertEqual(result["autoCloseFailedSeats"], [])
        self.assertEqual(set(host.terminated), closed)
        for session_id in closed:
            entry = _get(catalog, session_id)
            self.assertEqual(entry.status, "terminated")
            self.assertEqual(entry.retired_reason, "auto-close: leaf integrated into master")
            self.assertEqual(entry.retired_edge, "leaf-integration")
        self.assertEqual(_get(catalog, "mgr").status, "running")
        self.assertEqual(_get(catalog, "orch").status, "running")
        self.assertEqual(transcript.read_text(encoding="utf-8"), '{"report":"retained"}\n')

    def test_integrate_defers_missing_or_wrong_leaf_reports(self) -> None:
        catalog = self._catalog()
        catalog.upsert(_entry("worker-1", task_document_ref=LEAF_A1, spawn_role="worker"))
        catalog.upsert(_entry("reviewer-1", task_document_ref=LEAF_A1, spawn_role="reviewer"))
        self._post_report(
            "reviewer-1",
            task_document_ref=OTHER_LEAF_A,
            role="reviewer",
        )
        host = _FakeHost()

        with (
            mock.patch.object(completion_cleanup, "load_contract", return_value=self.fake_contract),
            mock.patch.object(completion_cleanup, "TerminalHost", return_value=host),
        ):
            result = self._complete_integration(self._config())

        self.assertEqual(result["autoClosedSeats"], [])
        self.assertEqual(
            set(cast(list[str], result["autoCloseDeferredSeats"])),
            {"worker-1", "reviewer-1"},
        )
        self.assertEqual(result["autoCloseFailedSeats"], [])
        self.assertEqual(host.terminated, [])
        self.assertEqual(_get(catalog, "worker-1").status, "running")
        self.assertEqual(_get(catalog, "reviewer-1").status, "running")

    def test_auto_close_opt_out_restores_landing_for_all_leaf_roles(self) -> None:
        catalog = self._catalog()
        for session_id, role in (
            ("worker-1", "worker"),
            ("reviewer-1", "reviewer"),
            ("curator-1", "curator"),
        ):
            catalog.upsert(_entry(session_id, task_document_ref=LEAF_A1, spawn_role=role))
        catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))

        with (
            mock.patch.object(completion_cleanup, "load_contract", return_value=self.fake_contract),
        ):
            result = self._complete_integration(self._config(auto_close_completed_seats=False))

        self.assertEqual(
            set(cast(list[str], result["autoLandedSeats"])),
            {"worker-1", "reviewer-1", "curator-1"},
        )
        self.assertNotIn("autoClosedSeats", result)
        for session_id in ("worker-1", "reviewer-1", "curator-1"):
            self.assertEqual(_get(catalog, session_id).status, "landed")
        self.assertEqual(_get(catalog, "mgr").status, "running")

    def test_integrate_skips_cleanup_when_edge_gate_is_off(self) -> None:
        catalog = self._catalog()
        catalog.upsert(_entry("worker-1", task_document_ref=LEAF_A1, spawn_role="worker"))
        with (
            mock.patch.object(completion_cleanup, "load_contract", return_value=self.fake_contract),
        ):
            result = self._complete_integration(self._config(auto_land_on_integration=False))
        self.assertNotIn("autoClosedSeats", result)
        self.assertNotIn("autoLandedSeats", result)
        self.assertEqual(_get(catalog, "worker-1").status, "running")

    def test_integrate_skips_cleanup_on_dry_run(self) -> None:
        catalog = self._catalog()
        catalog.upsert(_entry("worker-1", task_document_ref=LEAF_A1, spawn_role="worker"))
        with (
            mock.patch.object(
                worktree_tools.git_worktree_manager,
                "integrate_result",
                return_value=mock.Mock(payload={"state": "integrated"}, returncode=0),
            ),
            mock.patch.object(
                worktree_tools,
                "admit_configured_contract",
                return_value=ConfiguredContractAccepted(
                    contract_path=self.contract_path,
                    contract=self.fake_contract,
                    location=mock.Mock(),
                ),
            ),
            mock.patch.object(completion_cleanup, "load_contract", return_value=self.fake_contract),
        ):
            result = worktree_tools.worktree_integrate_tool(
                self._config(), contract_path=str(self.contract_path), dry_run=True
            )
        self.assertNotIn("autoClosedSeats", result)
        self.assertNotIn("autoLandedSeats", result)
        self.assertEqual(_get(catalog, "worker-1").status, "running")

    def test_finalize_retries_leaf_roles_but_never_closes_manager(self) -> None:
        catalog = self._catalog()
        catalog.upsert(_entry("mgr", task_document_ref=MASTER_A, spawn_role="manager"))
        catalog.upsert(_entry("reviewer-1", task_document_ref=LEAF_A1, spawn_role="reviewer"))
        catalog.upsert(_entry("curator-1", task_document_ref=LEAF_A1, spawn_role="curator"))
        self._post_report("reviewer-1", role="reviewer")
        self._post_report("curator-1", role="curator")
        host = _FakeHost()
        with (
            mock.patch.object(
                worktree_tools.git_worktree_manager,
                "finalize_result",
                return_value=mock.Mock(payload={"state": "finalized"}, returncode=0),
            ),
            mock.patch.object(completion_cleanup, "load_contract", return_value=self.fake_contract),
            mock.patch.object(completion_cleanup, "TerminalHost", return_value=host),
        ):
            result = worktree_tools.lifecycle_finalize_task_tool(
                self._config(), str(self.contract_path)
            )
        self.assertEqual(
            set(cast(list[str], result["autoClosedSeats"])),
            {"reviewer-1", "curator-1"},
        )
        self.assertEqual(set(host.terminated), {"reviewer-1", "curator-1"})
        self.assertEqual(_get(catalog, "mgr").status, "running")
        self.assertEqual(_get(catalog, "reviewer-1").retired_edge, "master-finalization")


class RetirementSettingsConfigTests(unittest.TestCase):
    def test_defaults_are_both_on(self) -> None:
        settings = RetirementSettings()
        self.assertTrue(settings.auto_land_on_integration)
        self.assertTrue(settings.auto_land_on_finalize)
        self.assertTrue(settings.auto_close_completed_seats)


if __name__ == "__main__":
    unittest.main()
