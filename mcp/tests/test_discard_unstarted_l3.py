from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import agents_remember.tasks.store as task_store
from agents_remember.application.task_docs.task_doc_discard import (
    discard_unstarted_subtask,
)
from agents_remember.application.task_docs.task_doc_response import task_doc_result
from agents_remember.application.task_docs.task_doc_tools import (
    TaskDocCall,
    TaskDocEdit,
    TaskDocError,
    TaskDocTarget,
    task_doc_tool,
)
from agents_remember.application.task_docs.task_execution_registration import (
    register_operator_inbox_execution_evidence,
    register_terminal_catalog_execution_evidence,
)
from agents_remember.application.task_docs.task_unstarted_evidence import (
    prove_task_unstarted,
)
from agents_remember.controlplane.agent_notifier_signals import (
    AgentNotifierSignalCooldownStore,
)
from agents_remember.controlplane.expectation_rows import ExpectationRowStore
from agents_remember.controlplane.operator_inbox_records import OperatorInboxEntry
from agents_remember.controlplane.operator_inbox_store import OperatorInboxStore
from agents_remember.controlplane.task_publication_lock import task_publication_lock
from agents_remember.kernel.primitives.observer_paths import observer_root
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    IntegrationOperationAuthority,
    LifecycleOperationRecord,
)
from agents_remember.models.task_doc import TaskDocResponse
from agents_remember.models.terminal_catalog import TerminalCatalogEntry
from agents_remember.observer.store import EventStore
from agents_remember.serving.agent_notifier import run_agent_notifier_sweep
from agents_remember.serving.agent_notifier_heartbeat import AgentNotifierHeartbeatStore
from agents_remember.serving.agent_notifier_models import AgentNotifierContext
from agents_remember.serving.inbox_reclamation import TmuxSessionNameSnapshot
from agents_remember.serving.terminal import TerminalHost
from agents_remember.serving.terminal_catalog import TerminalCatalog, terminal_catalog_path
from agents_remember.serving.terminal_liveness import (
    TerminalCatalogLivenessSweeper,
    TerminalLivenessActions,
)
from agents_remember.serving.terminal_paste import TerminalPaster
from agents_remember.tasks import read_task_doc
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    operation_key,
    operation_state_fingerprint,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    inspect_lifecycle_operation_locator,
    lifecycle_operation_locator_path,
    publish_new_lifecycle_operation_location,
    reserve_new_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.task_leaf_binding import (
    LeafTaskBinding,
    TaskLeafBindingError,
    require_current_start_task_binding,
    resolve_leaf_task_binding,
)
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    WorktreeContract,
    contract_publication_text,
    default_contract,
)
from lifecycle_enclosure_test_support import terminalize_test_enclosure
from pydantic import ValidationError
from test_task_document import _config


class DiscardUnstartedL3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.coord = Path(tempfile.mkdtemp())
        self.cfg = _config(self.coord)
        self.task_root = self.coord / "tasks" / "agents-remember" / "series"

    def _create_planning_leaf(self) -> tuple[Path, Path, Path]:
        master = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="series"),
            operation="create",
            edit=TaskDocEdit(
                fields={
                    "id": "series",
                    "slug": "series",
                    "title": "Series",
                    "kind": "master",
                    "repo": "agents-remember",
                    "type": "Master (Code)",
                    "createdAt": "2026-01-01T00:00",
                    "subTasks": [
                        {
                            "number": "1",
                            "name": "Planning leaf",
                            "file": "01_leaf.md",
                            "status": "planning",
                            "scope": "bounded test",
                        }
                    ],
                    "sections": [{"kind": "subTasks", "heading": "Sub-tasks"}],
                }
            ),
        )
        leaf = task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="series"),
            operation="create",
            edit=TaskDocEdit(
                fields={
                    "id": "1",
                    "slug": "01_leaf",
                    "title": "Planning leaf",
                    "kind": "subTask",
                    "master": "task.md",
                    "repo": "agents-remember",
                    "type": "Code",
                    "createdAt": "2026-01-01T00:00",
                }
            ),
        )
        return (
            Path(str(master["docPath"])),
            Path(str(leaf["docPath"])),
            Path(str(leaf["renderedPath"])),
        )

    def _discard(self, *, dry_run: bool = False, reason: str = "No work began") -> dict[str, Any]:
        return task_doc_tool(
            self.cfg,
            TaskDocTarget(repo_id="agents-remember", task_name="series"),
            operation="remove_subtask",
            edit=TaskDocEdit(
                subtask={
                    "number": "1",
                    "disposition": "discard-unstarted",
                    "reason": reason,
                }
            ),
            call=TaskDocCall(dry_run=dry_run),
        )

    def _binding(self) -> LeafTaskBinding:
        return resolve_leaf_task_binding(
            self.coord,
            "agents-remember",
            self.task_root,
            "1",
            task_name="series",
        )

    def _start_contract(self) -> WorktreeContract:
        return default_contract(
            ContractTask(
                name="series",
                repo_name="agents-remember",
                coordination_root=self.coord,
                workflow_kind="light-task",
                memory_mode="disabled",
            ),
            leaf=LeafIdentity(
                worktree_name="series-1",
                leaf_id="1",
                lifecycle_id="LC-START-WINNER",
            ),
            code=RepoBranchPlan(
                repo_path=self.cfg.repositories["agents-remember"].path,
                source_branch="main",
                work_branch="ar/series-1",
                base_commit="a" * 40,
            ),
        )

    def _run_operator_inbox_sweep(
        self,
        store: OperatorInboxStore,
        *,
        now: datetime,
    ) -> None:
        class _UnusedHost:
            def has_session(self, tmux_name: str) -> bool:
                del tmux_name
                return True

        class _UnusedPaster:
            def paste(self, *_args: object, **_kwargs: object) -> None:
                raise AssertionError("a terminal turn report must not be redelivered")

        root = observer_root(self.cfg)
        run_agent_notifier_sweep(
            AgentNotifierContext(
                catalog=TerminalCatalog(self.coord / "controlplane" / "empty-catalog.json"),
                host=cast(TerminalHost, _UnusedHost()),
                paster=cast(TerminalPaster, _UnusedPaster()),
                inbox_store=store,
                expectation_store=ExpectationRowStore(root),
                signal_cooldown_store=AgentNotifierSignalCooldownStore(root),
                event_store=EventStore(root),
                heartbeat_store=AgentNotifierHeartbeatStore(root),
                coordination_root=self.coord,
                register_execution_evidence=lambda entries: (
                    register_operator_inbox_execution_evidence(self.coord, entries)
                ),
                tmux_name_snapshotter=lambda: TmuxSessionNameSnapshot(
                    frozenset(), "tmux-no-server"
                ),
            ),
            now=now,
        )

    def _run_terminal_catalog_sweep(
        self,
        catalog: TerminalCatalog,
        *,
        now: datetime,
    ) -> None:
        class _UnusedHost:
            def has_session(self, tmux_name: str) -> bool:
                del tmux_name
                return True

        TerminalCatalogLivenessSweeper(
            catalog,
            _UnusedHost(),
            now=lambda: now,
            actions=TerminalLivenessActions(
                register_execution_evidence=lambda entries: (
                    register_terminal_catalog_execution_evidence(self.coord, entries)
                )
            ),
        ).refresh()

    def test_discard_modules_have_direct_leaf_test_ownership(self) -> None:
        # The behavioral cases below enter through the public task_doc facade. These direct
        # imports bind both extracted production modules to this targeted leaf suite as well.
        self.assertTrue(callable(discard_unstarted_subtask))
        self.assertTrue(callable(task_doc_result))

    def test_preview_apply_and_lost_response_retry_converge_on_parent_audit(self) -> None:
        master_path, leaf_json, leaf_markdown = self._create_planning_leaf()

        preview = self._discard(dry_run=True)
        self.assertEqual(preview["discardState"], "would-discard")
        self.assertEqual(preview["discardAudit"]["disposition"], "discard-unstarted")
        self.assertEqual(preview["discardEvidence"]["state"], "unstarted")
        self.assertEqual(
            set(preview["wouldDeleteFiles"]),
            {leaf_json.as_posix(), leaf_markdown.as_posix()},
        )
        self.assertTrue(leaf_json.exists() and leaf_markdown.exists())

        applied = self._discard()
        self.assertEqual(applied["discardState"], "discarded")
        self.assertEqual(
            applied["discardEvidence"]["fingerprint"],
            preview["discardEvidence"]["fingerprint"],
        )
        self.assertEqual(set(applied["deletedFiles"]), set(preview["wouldDeleteFiles"]))
        self.assertFalse(leaf_json.exists() or leaf_markdown.exists())
        parent = read_task_doc(master_path)
        self.assertEqual(parent.subTasks, [])
        self.assertEqual(len(parent.discardedSubTasks), 1)
        self.assertEqual(parent.discardedSubTasks[0].number, "1")
        rendered = master_path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("Discarded Sub-Tasks", rendered)
        self.assertIn("No work began", rendered)

        retried = self._discard()
        self.assertEqual(retried["discardState"], "already-discarded")
        self.assertTrue(retried["alreadyDiscarded"])
        self.assertEqual(retried["deletedFiles"], [])
        self.assertEqual(read_task_doc(master_path).discardedSubTasks, parent.discardedSubTasks)
        TaskDocResponse.model_validate(preview)
        TaskDocResponse.model_validate(applied)
        TaskDocResponse.model_validate(retried)

    def test_task_progress_refuses_without_mutating_parent_or_child(self) -> None:
        master_path, leaf_json, leaf_markdown = self._create_planning_leaf()
        leaf = read_task_doc(leaf_json)
        data = leaf.model_dump(by_alias=True)
        data["status"] = "inProgress"
        task_store.write_task_doc(self.task_root, leaf.__class__.model_validate(data))
        before = {path: path.read_bytes() for path in (master_path, leaf_json, leaf_markdown)}

        refused = self._discard()

        self.assertFalse(refused["ok"])
        self.assertEqual(refused["discardState"], "refused-started")
        self.assertEqual(refused["discardEvidence"]["state"], "started")
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        TaskDocResponse.model_validate(refused)

    def test_free_form_files_and_queue_rows_cannot_decide_the_census(self) -> None:
        self._create_planning_leaf()
        arbitrary = self.task_root / "notes" / "curator-report.md"
        arbitrary.parent.mkdir(parents=True)
        arbitrary.write_text("free-form report", encoding="utf-8")
        disposable_queue = self.coord / "controlplane" / "closeout-queue" / "old.json"
        disposable_queue.parent.mkdir(parents=True)
        disposable_queue.write_text("{}", encoding="utf-8")

        evidence = prove_task_unstarted(self.cfg, self._binding())

        self.assertEqual(evidence.state, "unstarted")
        addresses = {str(fact.get("address")) for fact in evidence.facts}
        self.assertNotIn(arbitrary.as_posix(), addresses)
        self.assertNotIn(disposable_queue.as_posix(), addresses)

    def test_present_nonregular_locator_seat_and_report_sources_fail_closed(self) -> None:
        self._create_planning_leaf()
        binding = self._binding()
        locator = binding.contract_path

        exact_sources = (
            lifecycle_operation_locator_path(self.coord, locator),
            terminal_catalog_path(self.coord),
            OperatorInboxStore(observer_root(self.cfg)).log_path(),
        )
        for source in exact_sources:
            source.mkdir(parents=True)

        evidence = prove_task_unstarted(self.cfg, binding)

        self.assertEqual(evidence.state, "ambiguous")
        unreadable = {
            str(fact["kind"]) for fact in evidence.facts if fact.get("state") == "unreadable"
        }
        self.assertTrue({"locator", "seat", "review-report"}.issubset(unreadable))

    def test_symlinked_child_sources_fail_closed_before_the_census(self) -> None:
        _master_path, leaf_json, leaf_markdown = self._create_planning_leaf()
        for target in (leaf_json, leaf_markdown):
            with self.subTest(target=target.name):
                payload = target.read_bytes()
                target.unlink()
                target.symlink_to(self.task_root / "task.json")
                with self.assertRaisesRegex(TaskDocError, "not a regular file"):
                    self._discard()
                target.unlink()
                target.write_bytes(payload)

    def test_start_locator_winner_before_task_cas_publication_refuses_discard(self) -> None:
        master_path, leaf_json, leaf_markdown = self._create_planning_leaf()
        contract = self._start_contract()
        self.assertEqual(contract.contract_path, self._binding().contract_path)
        contract_text = contract_publication_text(contract.contract_path, contract)
        before = {path: path.read_bytes() for path in (master_path, leaf_json, leaf_markdown)}

        with task_publication_lock(self.coord, "agents-remember"):
            require_current_start_task_binding(
                self.coord,
                "agents-remember",
                self.task_root,
                "1",
                task_name="series",
            )
            reserve_new_lifecycle_operation_location(contract, contract_text=contract_text)

        observation = inspect_lifecycle_operation_locator(self.coord, contract.contract_path)
        self.assertEqual(observation.state, "reserved")
        refused = self._discard()

        self.assertEqual(refused["discardState"], "refused-started")
        self.assertEqual(refused["discardEvidence"]["state"], "started")
        self.assertEqual(refused["nextTool"], "worktree_start")
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_discard_winner_makes_start_binding_refuse_before_locator_reservation(self) -> None:
        self._create_planning_leaf()
        contract = self._start_contract()

        discarded = self._discard()

        self.assertEqual(discarded["discardState"], "discarded")
        with (
            task_publication_lock(self.coord, "agents-remember"),
            self.assertRaisesRegex(TaskLeafBindingError, "exactly one live row"),
        ):
            require_current_start_task_binding(
                self.coord,
                "agents-remember",
                self.task_root,
                "1",
                task_name="series",
            )
        observation = inspect_lifecycle_operation_locator(self.coord, contract.contract_path)
        self.assertEqual(observation.state, "missing")

    def test_unreadable_contract_still_censuses_exact_locator_journal_route(self) -> None:
        self._create_planning_leaf()
        contract = self._start_contract()
        text = contract_publication_text(contract.contract_path, contract)
        location = publish_new_lifecycle_operation_location(contract, contract_text=text)
        fingerprint = "b" * 64
        record = LifecycleOperationRecord(
            taskId=contract.task_id,
            taskName=contract.task_name,
            contractPath=contract.contract_path.as_posix(),
            operationKind="integrate",
            candidateState=operation_state_fingerprint(contract),
            candidateTree=None,
            fingerprint=fingerprint,
            operationKey=operation_key(contract.contract_path, "integrate", fingerprint),
            input=IntegrateOperationInput(
                configPath=(self.coord / "config.json").as_posix(),
                contractPath=contract.contract_path.as_posix(),
            ),
            integrationAuthority=IntegrationOperationAuthority(
                targetKind="atomic-integration",
                codeRepository=contract.code_repo_path.as_posix(),
                codeSourceBranch=contract.code_source_branch,
                codeSourceRef=f"refs/heads/{contract.code_source_branch}",
                codeSourceCommit=contract.code_base_commit,
                codeCandidateCommit=contract.code_base_commit,
            ),
            status="queued",
            phase="queued",
            queuedAt="2026-01-01T00:00:00+00:00",
            currentCommand="waiting to integrate",
            reportPath=location.report_path("integrate").as_posix(),
        )
        LifecycleOperationStore(location.journal_path("integrate")).create(record)
        contract.contract_path.write_text("not a contract\n", encoding="utf-8")

        evidence = prove_task_unstarted(self.cfg, self._binding())

        self.assertEqual(evidence.state, "ambiguous")
        self.assertEqual(evidence.next_action, "recover-integrate-authority")
        self.assertEqual(evidence.next_tool, "worktree_status")
        self.assertTrue(
            any(
                fact.get("kind") == "manifest" and fact.get("state") == "present"
                for fact in evidence.facts
            )
        )
        self.assertTrue(
            any(
                fact.get("address") == location.journal_path("integrate").as_posix()
                for fact in evidence.facts
            )
        )

    def test_terminal_locator_routes_to_same_address_successor_start(self) -> None:
        self._create_planning_leaf()
        contract = replace(self._start_contract(), cleanup="abandoned")
        text = contract_publication_text(contract.contract_path, contract)
        location = publish_new_lifecycle_operation_location(contract, contract_text=text)
        terminal = terminalize_test_enclosure(location)

        evidence = prove_task_unstarted(self.cfg, self._binding())

        self.assertEqual(evidence.state, "started")
        self.assertEqual(evidence.next_action, "restart-terminal-enclosure")
        self.assertEqual(evidence.next_tool, "worktree_start")
        assert evidence.next_args is not None
        self.assertEqual(evidence.next_args["worktree_name"], contract.worktree_group.name)
        self.assertTrue(
            any(fact.get("address") == terminal.terminalReceiptPath for fact in evidence.facts)
        )

    def test_parent_and_child_publication_rolls_back_exact_bytes(self) -> None:
        master_path, leaf_json, leaf_markdown = self._create_planning_leaf()
        before = {path: path.read_bytes() for path in (master_path, leaf_json, leaf_markdown)}
        real_write = task_store.write_task_docs

        def write_then_fail(*args: Any, **kwargs: Any) -> None:
            real_write(*args, **kwargs)
            raise OSError("injected discard publication failure")

        with (
            patch.object(task_store, "write_task_docs", side_effect=write_then_fail),
            self.assertRaisesRegex(OSError, "injected discard publication failure"),
        ):
            self._discard()

        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_parent_audit_hard_cut_replays_only_the_accepted_child_bytes(self) -> None:
        master_path, leaf_json, leaf_markdown = self._create_planning_leaf()
        preview = self._discard(dry_run=True)
        parent = read_task_doc(master_path)
        data = parent.model_dump(by_alias=True)
        data["subTasks"] = []
        data["discardedSubTasks"] = [preview["discardAudit"]]
        task_store.write_task_doc(
            self.task_root,
            parent.__class__.model_validate(data),
        )

        resumed = self._discard()

        self.assertEqual(resumed["discardState"], "already-discarded")
        self.assertEqual(
            set(resumed["deletedFiles"]),
            {leaf_json.as_posix(), leaf_markdown.as_posix()},
        )
        self.assertFalse(leaf_json.exists() or leaf_markdown.exists())

    def test_parent_audit_hard_cut_preserves_later_contradictory_child_bytes(self) -> None:
        master_path, leaf_json, leaf_markdown = self._create_planning_leaf()
        preview = self._discard(dry_run=True)
        parent = read_task_doc(master_path)
        data = parent.model_dump(by_alias=True)
        data["subTasks"] = []
        data["discardedSubTasks"] = [preview["discardAudit"]]
        task_store.write_task_doc(
            self.task_root,
            parent.__class__.model_validate(data),
        )
        leaf_json.write_text('{"later":"contradictory"}\n', encoding="utf-8")
        before = {
            leaf_json: leaf_json.read_bytes(),
            leaf_markdown: leaf_markdown.read_bytes(),
        }

        with self.assertRaisesRegex(TaskDocError, "differ from the exact accepted"):
            self._discard()

        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(len(read_task_doc(master_path).discardedSubTasks), 1)

    def test_discard_response_collections_are_bounded_at_the_model_boundary(self) -> None:
        self._create_planning_leaf()
        result = self._discard(dry_run=True)
        valid = TaskDocResponse.model_validate(result)

        with self.assertRaises(ValidationError):
            TaskDocResponse.model_validate(
                {**valid.model_dump(mode="json"), "deletedFiles": ["x"] * 17}
            )
        evidence = valid.discardEvidence
        self.assertIsNotNone(evidence)
        assert evidence is not None
        with self.assertRaises(ValidationError):
            TaskDocResponse.model_validate(
                {
                    **valid.model_dump(mode="json"),
                    "discardEvidence": {
                        **evidence.model_dump(mode="json"),
                        "facts": [evidence.facts[0].model_dump(mode="json")] * 33,
                    },
                }
            )
        with self.assertRaises(ValidationError):
            TaskDocResponse.model_validate(
                {
                    **valid.model_dump(mode="json"),
                    "nextArgs": {str(index): index for index in range(33)},
                }
            )

    def test_reclaimed_terminal_seat_registers_monotonic_task_execution_history(self) -> None:
        self._create_planning_leaf()
        binding = self._binding()
        catalog = TerminalCatalog(terminal_catalog_path(self.coord))
        old = datetime(2026, 1, 1, tzinfo=UTC)
        entry = TerminalCatalogEntry(
            id="worker-seat",
            label="worker",
            kind="harness",
            harness="codex",
            lifecycle_id=None,
            cwd=self.task_root,
            tmux_name="worker-seat",
            command=("codex",),
            created_at=old.isoformat(),
            last_attached_at=old.isoformat(),
            status="terminated",
            terminated_at=old.isoformat(),
            task_document_ref=binding.task_ref,
            seat_role="worker",
        )
        catalog.upsert(entry)

        self._run_terminal_catalog_sweep(catalog, now=old + timedelta(days=2))

        self.assertIsNone(catalog.get(entry.id))
        leaf = read_task_doc(binding.leaf_json_path)
        self.assertEqual(len(leaf.executionRegistrations), 1)
        self.assertEqual(leaf.executionRegistrations[0].sourceId, entry.id)
        self.assertEqual(prove_task_unstarted(self.cfg, self._binding()).state, "started")

    def test_terminal_sweep_retains_seat_when_child_is_missing_but_parent_row_is_live(
        self,
    ) -> None:
        self._create_planning_leaf()
        binding = self._binding()
        binding.leaf_json_path.unlink()
        binding.leaf_markdown_path.unlink()
        catalog = TerminalCatalog(terminal_catalog_path(self.coord))
        old = datetime(2026, 1, 1, tzinfo=UTC)
        entry = TerminalCatalogEntry(
            id="worker-seat-missing-child",
            label="worker",
            kind="harness",
            harness="codex",
            lifecycle_id=None,
            cwd=self.task_root,
            tmux_name="worker-seat-missing-child",
            command=("codex",),
            created_at=old.isoformat(),
            last_attached_at=old.isoformat(),
            status="terminated",
            terminated_at=old.isoformat(),
            task_document_ref=binding.task_ref,
            seat_role="worker",
        )
        catalog.upsert(entry)

        self._run_terminal_catalog_sweep(catalog, now=old + timedelta(days=2))

        self.assertIsNotNone(catalog.get(entry.id))
        parent = read_task_doc(binding.parent_path)
        self.assertEqual(parent.executionRegistrations, [])

    def test_reclaimed_turn_report_registers_monotonic_task_execution_history(self) -> None:
        self._create_planning_leaf()
        binding = self._binding()
        store = OperatorInboxStore(observer_root(self.cfg))
        old = datetime(2026, 1, 1, tzinfo=UTC)
        entry = OperatorInboxEntry(
            id="review-turn-report",
            ts=old.isoformat(),
            state="landed",
            taskDocumentRef=binding.task_ref,
            senderRole="reviewer",
            messageKind="turn-report",
            subjectTaskDocumentRef=binding.task_ref,
            seatRole="reviewer",
            ask="review complete",
            response="review complete",
            createdAt=old.isoformat(),
            createdBy="reviewer",
            createdVia="cli",
            terminalAt=old.isoformat(),
            terminalReason="landed",
        )
        store.append(entry)

        self._run_operator_inbox_sweep(store, now=old + timedelta(days=3))

        self.assertNotIn(entry.id, store.current())
        leaf = read_task_doc(binding.leaf_json_path)
        self.assertEqual(len(leaf.executionRegistrations), 1)
        self.assertEqual(leaf.executionRegistrations[0].sourceId, entry.id)
        evidence = prove_task_unstarted(self.cfg, self._binding())
        self.assertEqual(evidence.state, "started")
        self.assertEqual(evidence.next_action, "complete-started-task")

    def test_operator_inbox_sweep_retains_report_when_registration_publication_fails(
        self,
    ) -> None:
        self._create_planning_leaf()
        binding = self._binding()
        store = OperatorInboxStore(observer_root(self.cfg))
        old = datetime(2026, 1, 1, tzinfo=UTC)
        entry = OperatorInboxEntry(
            id="worker-turn-report-publication-refused",
            ts=old.isoformat(),
            state="landed",
            taskDocumentRef=binding.task_ref,
            senderRole="worker",
            messageKind="turn-report",
            subjectTaskDocumentRef=binding.task_ref,
            seatRole="worker",
            ask="work complete",
            response="work complete",
            createdAt=old.isoformat(),
            createdBy="worker",
            createdVia="cli",
            terminalAt=old.isoformat(),
            terminalReason="landed",
        )
        store.append(entry)

        with patch(
            "agents_remember.application.task_docs.task_execution_registration."
            "publish_prepared_task_documents",
            side_effect=OSError("forced task publication refusal"),
        ):
            self._run_operator_inbox_sweep(store, now=old + timedelta(days=3))

        self.assertIn(entry.id, store.current())
        leaf = read_task_doc(binding.leaf_json_path)
        self.assertEqual(leaf.executionRegistrations, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
