"""Public forcing for the isolated schema-1 lifecycle bridge."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.application.lifecycle.legacy_operation_tool import (
    LegacyOperationAction,
    LegacyOperationRequest,
    worktree_legacy_operation_tool,
)
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.worktree_tools import (
    OperationControlRequest,
    worktree_operation_control_tool,
    worktree_status_tool,
)
from agents_remember.models.lifecycles.operation import (
    IntegrateOperationInput,
    IntegrationOperationAuthority,
    LifecycleOperationRecord,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.worktrees.integration.legacy.legacy_operation_bridge import (
    legacy_bridge_removal_guard,
)
from agents_remember.worktrees.integration.legacy.legacy_operation_public import (
    LEGACY_BRIDGE_REMOVAL_CONDITION,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_generation_resume import (
    requeued_same_generation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    operation_state_fingerprint,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationSchemaError,
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    operation_key,
    start_or_observe_operation,
)
from agents_remember.worktrees.modules.git import head_commit, require_git
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import write_contract
from closeout_fixture_test_support import selected_fixture
from test_closeout_queue import MASTER_A
from test_worktree_support import git


def _byte_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested for item in value.values() for nested in _recursive_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _recursive_keys(item)}
    return set()


class LegacyOperationBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _legacy_fixture(self) -> tuple[Any, Any, Path, str]:
        fixture = selected_fixture(Path(self.temp.name) / "fixture", memory_mode="external")
        contract = fixture.contracts[MASTER_A]
        git(contract.code_worktree, "add", "-A")
        git(contract.code_worktree, "commit", "-m", "legacy exact code output")
        code_commit = head_commit(contract.code_worktree)
        candidate_tree = require_git(
            contract.code_worktree, ["rev-parse", f"{code_commit}^{{tree}}"]
        )
        fingerprint = hashlib.sha256(b"legacy exact closeout input").hexdigest()
        payload = {
            "schemaVersion": "1.0",
            "taskId": contract.task_id,
            "taskName": contract.task_name,
            "contractPath": contract.contract_path.as_posix(),
            "operationKind": "closeout",
            "candidateState": operation_state_fingerprint(contract),
            "candidateTree": candidate_tree,
            "fingerprint": fingerprint,
            "operationKey": operation_key(contract.contract_path, "closeout", fingerprint),
            "input": {
                "kind": "closeout",
                "configPath": fixture.config_path.as_posix(),
                "contractPath": contract.contract_path.as_posix(),
                "codeCommitMessage": "legacy exact code output",
                "memoryCommitMessage": "",
                "ledgerCommitMessage": "",
                "approvalNote": "legacy owner approval",
                "gatePolicy": [],
            },
            "status": "input-required",
            "phase": "contract-finalization",
            "queuedAt": "2026-08-01T00:00:00+00:00",
            "startedAt": "2026-08-01T00:00:01+00:00",
            "heartbeatAt": "2026-08-01T00:00:02+00:00",
            "currentCommand": "schema-1 closeout stopped after code output",
            "reportPath": (contract.worktree_group / "reports" / "legacy.json").as_posix(),
            "result": {"detail": "PRIVATE-LEGACY-RESULT-/internal/result"},
            "failure": "PRIVATE-LEGACY-FAILURE-/internal/failure",
            "irreversibleBoundaryEntered": True,
            "approvalClaimed": True,
            "recoveryCommits": {"codeCommit": code_commit},
            "attempt": 1,
        }
        path = operation_record_path(contract.worktree_group, "closeout")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps(payload, sort_keys=True).encode()
        path.write_bytes(original)
        return fixture, contract, path, hashlib.sha256(original).hexdigest()

    def _terminal_fixture(self) -> tuple[Any, Any, Path, str]:
        fixture = selected_fixture(Path(self.temp.name) / "terminal", memory_mode="external")
        contract = fixture.close_contract(MASTER_A)
        fingerprint = hashlib.sha256(b"terminal legacy closeout").hexdigest()
        payload = {
            "schemaVersion": "1.0",
            "taskId": contract.task_id,
            "taskName": contract.task_name,
            "contractPath": contract.contract_path.as_posix(),
            "operationKind": "closeout",
            "candidateState": operation_state_fingerprint(contract),
            "candidateTree": require_git(contract.code_worktree, ["rev-parse", "HEAD^{tree}"]),
            "fingerprint": fingerprint,
            "operationKey": operation_key(contract.contract_path, "closeout", fingerprint),
            "input": {},
            "status": "completed",
            "phase": "completed",
            "queuedAt": "2026-08-01T00:00:00+00:00",
            "finishedAt": "2026-08-01T00:00:03+00:00",
            "currentCommand": "legacy closeout completed",
            "reportPath": (contract.worktree_group / "reports" / "legacy.json").as_posix(),
            "irreversibleBoundaryEntered": True,
            "approvalClaimed": True,
            "recoveryCommits": {
                "codeCommit": contract.code_commit,
                "memoryContentCommit": contract.memory_content_commit,
                "ledgerCommit": contract.ledger_commit,
            },
        }
        path = operation_record_path(contract.worktree_group, "closeout")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps(payload, sort_keys=True).encode()
        path.write_bytes(original)
        return fixture, contract, path, hashlib.sha256(original).hexdigest()

    def _terminal_integrate_fixture(self) -> tuple[Any, Any, Path, str]:
        fixture = selected_fixture(Path(self.temp.name) / "integrate", memory_mode="external")
        closed = fixture.close_contract(MASTER_A)
        git(
            closed.code_repo_path,
            "update-ref",
            f"refs/heads/{closed.code_source_branch}",
            closed.code_commit,
        )
        assert closed.memory_repo_path is not None
        git(
            closed.memory_repo_path,
            "update-ref",
            f"refs/heads/{closed.memory_source_branch}",
            closed.ledger_commit,
        )
        contract = replace(
            closed,
            integration_status="completed",
            integrated_code_commit=closed.code_commit,
            integrated_memory_content_commit=closed.memory_content_commit,
            integrated_ledger_commit=closed.ledger_commit,
        )
        write_contract(contract.contract_path, contract)
        fingerprint = hashlib.sha256(b"terminal legacy integrate").hexdigest()
        payload = {
            "schemaVersion": "1.0",
            "taskId": contract.task_id,
            "taskName": contract.task_name,
            "contractPath": contract.contract_path.as_posix(),
            "operationKind": "integrate",
            "candidateState": operation_state_fingerprint(contract),
            "candidateTree": None,
            "fingerprint": fingerprint,
            "operationKey": operation_key(contract.contract_path, "integrate", fingerprint),
            "input": {},
            "status": "completed",
            "phase": "completed",
            "queuedAt": "2026-08-01T00:00:00+00:00",
            "finishedAt": "2026-08-01T00:00:03+00:00",
            "currentCommand": "legacy integrate completed",
            "reportPath": (contract.worktree_group / "reports" / "legacy.json").as_posix(),
            "irreversibleBoundaryEntered": True,
            "approvalClaimed": True,
            "recoveryCommits": {
                "codeCommit": contract.integrated_code_commit,
                "memoryContentCommit": contract.integrated_memory_content_commit,
                "ledgerCommit": contract.integrated_ledger_commit,
            },
        }
        path = operation_record_path(contract.worktree_group, "integrate")
        path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps(payload, sort_keys=True).encode()
        path.write_bytes(original)
        return fixture, contract, path, hashlib.sha256(original).hexdigest()

    @staticmethod
    def _start_unrelated_integration_below_source_admission(fixture, contract) -> None:
        """Seed the other-kind journal for tests scoped strictly to legacy arbitration."""

        with mock.patch(
            "agents_remember.worktrees.integration.lifecycle.lifecycle_operations."
            "classify_integration_door_authority",
            return_value=SimpleNamespace(valid=True),
        ):
            start_or_observe_operation(
                IntegrateOperationInput(
                    configPath=fixture.config_path.as_posix(),
                    contractPath=contract.contract_path.as_posix(),
                ),
                contract,
                launcher=lambda _contract, _record: None,
            )

    @staticmethod
    def _request(
        digest: str,
        *,
        action: LegacyOperationAction = "migrate",
    ) -> LegacyOperationRequest:
        return LegacyOperationRequest(
            operation_kind="closeout",
            action=action,
            expected_digest=digest,
            memory_commit_message="finish legacy memory",
            ledger_commit_message="finish legacy ledger",
            audit_reason="bounded schema-1 recovery",
        )

    def test_normal_reader_refuses_legacy_and_inspect_preserves_exact_bytes(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        with self.assertRaises(LifecycleOperationSchemaError) as caught:
            LifecycleOperationStore(path).read()
        self.assertEqual(caught.exception.expected_schema, "3.0")
        self.assertEqual(caught.exception.observed_schema, "1.0")
        self.assertIn("expected schemaVersion 3.0", str(caught.exception))
        self.assertIn("observed 1.0", str(caught.exception))
        before = _byte_tree(Path(self.temp.name))
        result = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            self._request(digest, action="inspect"),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["migratable"])
        self.assertEqual(result["legacyDigest"], digest)
        self.assertEqual(_byte_tree(Path(self.temp.name)), before)

    def test_operation_key_mismatch_is_publicly_digest_bounded(self) -> None:
        fixture, contract, path, _digest = self._legacy_fixture()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["operationKey"] = "f" * 64
        changed = json.dumps(payload, sort_keys=True).encode()
        path.write_bytes(changed)
        digest = hashlib.sha256(changed).hexdigest()
        before = _byte_tree(Path(self.temp.name))

        result = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            self._request(digest, action="inspect"),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "legacy-operation-key-mismatch")
        self.assertEqual(result["expected"]["generation"], 1)
        self.assertEqual(result["observed"]["generation"], 1)
        self.assertTrue(result["observed"]["operationIdentityComparison"]["matches"] is False)
        self.assertNotIn("operationKey", _recursive_keys(result))
        self.assertNotIn("claimedOperationKey", _recursive_keys(result))
        self.assertEqual(_byte_tree(Path(self.temp.name)), before)

    def test_migration_dry_run_is_byte_exact_and_leaves_no_lock_or_receipt(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        original = path.read_bytes()
        path.unlink()
        integration_input = IntegrateOperationInput(
            configPath=fixture.config_path.as_posix(),
            contractPath=contract.contract_path.as_posix(),
        )
        sibling_store = LifecycleOperationStore(
            operation_record_path(contract.worktree_group, "integrate")
        )
        code_commit = head_commit(contract.code_worktree)
        sibling_fingerprint = hashlib.sha256(b"legacy dry-run sibling").hexdigest()
        sibling_store.create(
            LifecycleOperationRecord(
                taskId=contract.task_id,
                taskName=contract.task_name,
                contractPath=contract.contract_path.as_posix(),
                operationKind="integrate",
                candidateState=operation_state_fingerprint(contract),
                candidateTree=None,
                fingerprint=sibling_fingerprint,
                operationKey=operation_key(
                    contract.contract_path,
                    "integrate",
                    sibling_fingerprint,
                ),
                integrationAuthority=IntegrationOperationAuthority(
                    targetKind="sprint-super",
                    codeRepository=contract.code_repo_path.as_posix(),
                    codeSourceBranch=contract.code_source_branch,
                    codeSourceRef=f"refs/heads/{contract.code_source_branch}",
                    codeSourceCommit=code_commit,
                    codeCandidateCommit=code_commit,
                    memoryRepository=(
                        contract.memory_repo_path.as_posix()
                        if contract.memory_repo_path is not None
                        else ""
                    ),
                    memorySourceBranch=contract.memory_source_branch,
                    memorySourceRef=(
                        f"refs/heads/{contract.memory_source_branch}"
                        if contract.memory_source_branch
                        else ""
                    ),
                    memorySourceCommit=contract.memory_base_commit,
                    memoryContentCommit=contract.memory_content_commit,
                    ledgerCommit=contract.ledger_commit,
                ),
                input=integration_input,
                status="queued",
                phase="queued",
                queuedAt="2026-08-22T15:59:00+00:00",
                currentCommand="waiting to start integrate",
                reportPath=(contract.worktree_group / "reports" / "integrate.json").as_posix(),
            )
        )
        sibling_runtime = lifecycle_operation_worker.OperationRuntime(sibling_store)
        sibling_runtime.start()
        sibling_runtime.finish({"ok": True}, ok=True)
        sibling_store.update(
            lambda current: current.model_copy(
                update={
                    "workerPid": 4242,
                    "workerLease": "8" * 64,
                    "workerProcessFingerprint": "9" * 64,
                }
            )
        )
        path.write_bytes(original)
        before = _byte_tree(Path(self.temp.name))

        def exited(request):
            return request.model_copy(
                update={
                    "state": "exited",
                    "observedAt": "2026-08-22T16:00:00+00:00",
                    "detail": "dead sibling is projected without publication",
                }
            )

        with mock.patch(
            "agents_remember.worktrees.integration.lifecycle.worker.state."
            "observe_worker_termination",
            side_effect=exited,
        ):
            result = worktree_legacy_operation_tool(
                fixture.cfg,
                contract.contract_path.as_posix(),
                replace(self._request(digest), dry_run=True),
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "would-migrate")
        self.assertEqual(_byte_tree(Path(self.temp.name)), before)

    def test_migration_preserves_proof_and_only_advertises_recover(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        original = path.read_bytes()
        result = worktree_legacy_operation_tool(
            fixture.cfg, contract.contract_path.as_posix(), self._request(digest)
        )
        self.assertTrue(result["ok"])
        record = LifecycleOperationStore(path).read()
        assert record is not None and record.legacyMigration is not None
        assert record.recoveryCommits is not None
        proof = record.legacyMigration
        self.assertEqual(proof.originalSha256, digest)
        self.assertEqual(base64.b64decode(proof.originalBytesBase64), original)
        self.assertEqual(proof.codeCommit, record.recoveryCommits.codeCommit)
        self.assertEqual(proof.migrationVersion, "schema-1-closeout-v1")
        self.assertTrue(record.irreversibleBoundaryEntered)
        self.assertIsNone(record.result)
        self.assertIsNone(record.failure)
        with self.assertRaisesRegex(RuntimeError, "legacy migration proof is immutable"):
            amended_proof = proof.model_copy(
                update={"auditReason": "different bounded schema-1 recovery"}
            )
            LifecycleOperationStore(path).resume_generation(
                lambda current: requeued_same_generation(current).model_copy(
                    update={"legacyMigration": amended_proof}
                ),
                expected_generation=record.generation,
            )
        self.assertEqual(LifecycleOperationStore(path).read(), record)
        self.assertEqual(
            [item["action"] for item in result["lifecycleOperation"]["legalControls"]],
            ["recover"],
        )

    def test_migration_replay_is_exact_and_amendment_is_refused(self) -> None:
        fixture, contract, _path, digest = self._legacy_fixture()
        request = self._request(digest)
        first = worktree_legacy_operation_tool(
            fixture.cfg, contract.contract_path.as_posix(), request
        )
        second = worktree_legacy_operation_tool(
            fixture.cfg, contract.contract_path.as_posix(), request
        )
        changed = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            replace(request, memory_commit_message="amended memory"),
        )
        self.assertEqual(first["legacyDigest"], second["legacyDigest"])
        self.assertFalse(changed["ok"])
        self.assertEqual(changed["status"], "legacy-migration-amendment-refused")
        self.assertEqual(changed["nextTool"], "worktree_operation_control")

    def test_invalid_operation_key_is_never_blessed(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["operationKey"] = "f" * 64
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = worktree_legacy_operation_tool(
            fixture.cfg, contract.contract_path.as_posix(), self._request(digest)
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "legacy-operation-key-mismatch")

    def test_invalid_legacy_envelope_never_exposes_validation_input(self) -> None:
        fixture, contract, path, _digest = self._legacy_fixture()
        private_sentinel = "PRIVATE-LEGACY-VALIDATION-/secret/path"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["operationKind"] = private_sentinel
        changed = json.dumps(payload, sort_keys=True).encode()
        path.write_bytes(changed)
        digest = hashlib.sha256(changed).hexdigest()
        before = _byte_tree(Path(self.temp.name))

        result = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            self._request(digest, action="inspect"),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "legacy-envelope-unsupported")
        self.assertEqual(result["observed"]["failure"]["errorType"], "ValidationError")
        self.assertNotIn(private_sentinel, repr(result))
        self.assertEqual(_byte_tree(Path(self.temp.name)), before)

    def test_record_read_os_error_is_bounded_and_nonmutating(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        private = "PRIVATE_LEGACY_RECORD_PATH /tmp/legacy-record stderr"
        before = _byte_tree(Path(self.temp.name))
        real_read_bytes = Path.read_bytes

        def unreadable(current: Path) -> bytes:
            if current == path:
                raise PermissionError(private)
            return real_read_bytes(current)

        with mock.patch.object(Path, "read_bytes", new=unreadable):
            result = worktree_legacy_operation_tool(
                fixture.cfg,
                contract.contract_path.as_posix(),
                self._request(digest, action="inspect"),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "legacy-operation-unreadable")
        self.assertEqual(result["observed"]["failure"]["stage"], "legacy-record-read")
        self.assertEqual(result["observed"]["failure"]["errorType"], "PermissionError")
        self.assertNotIn(private, repr(result))
        self.assertEqual(_byte_tree(Path(self.temp.name)), before)

    def test_archive_receipt_read_os_error_is_bounded_and_nonmutating(self) -> None:
        fixture, contract, path, digest = self._terminal_fixture()
        request = replace(
            self._request(digest, action="archive"),
            memory_commit_message=None,
            ledger_commit_message=None,
        )
        archived = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            request,
        )
        self.assertTrue(archived["ok"])
        archive_path = path.with_name(f"{path.stem}.schema-1-archive.json")
        private = "PRIVATE_LEGACY_ARCHIVE_PATH /tmp/archive stderr"
        before = _byte_tree(Path(self.temp.name))
        real_read_text = Path.read_text

        def unreadable(
            current: Path,
            encoding: str | None = None,
            errors: str | None = None,
        ) -> str:
            if current == archive_path:
                raise PermissionError(private)
            return real_read_text(current, encoding=encoding, errors=errors)

        with mock.patch.object(Path, "read_text", new=unreadable):
            result = worktree_legacy_operation_tool(
                fixture.cfg,
                contract.contract_path.as_posix(),
                request,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "legacy-archive-invalid")
        self.assertEqual(result["observed"]["failure"]["stage"], "legacy-archive-read")
        self.assertNotIn(private, repr(result))
        self.assertEqual(_byte_tree(Path(self.temp.name)), before)

    def test_migration_publication_os_error_is_bounded_and_recoverable(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        private = "PRIVATE_LEGACY_PUBLICATION_PATH /tmp/publication stderr"
        original = path.read_bytes()
        before = _byte_tree(Path(self.temp.name))
        with mock.patch(
            "agents_remember.worktrees.integration.legacy.legacy_operation_bridge.atomic_write_text",
            side_effect=OSError(private),
        ):
            result = worktree_legacy_operation_tool(
                fixture.cfg,
                contract.contract_path.as_posix(),
                self._request(digest),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "legacy-migration-publication-interrupted")
        self.assertEqual(result["nextAction"], "migrate")
        self.assertNotIn(private, repr(result))
        after = _byte_tree(Path(self.temp.name))
        added = set(after) - set(before)
        added_paths = {Path(key) for key in added}
        lifecycle_root = path.parent.relative_to(Path(self.temp.name))
        locator_lock_root = Path("fixture/coordination/controlplane/lifecycle-enclosures/locks")
        self.assertEqual(
            {item.parent for item in added_paths},
            {lifecycle_root, locator_lock_root},
        )
        lifecycle_root_locks = {item.name for item in added_paths if item.parent == lifecycle_root}
        locator_locks = [item for item in added_paths if item.parent == locator_lock_root]
        self.assertEqual(len(locator_locks), 1)
        self.assertEqual(locator_locks[0].suffix, ".lock")
        fixed_locks = {
            "closeout-operation.json.lock",
            "direct-landing-operation.json.lock",
            "integrate-operation.json.lock",
        }
        self.assertTrue(fixed_locks <= lifecycle_root_locks)
        self.assertEqual(lifecycle_root_locks, fixed_locks)
        self.assertTrue(all(after[key] == b"" for key in added))
        self.assertEqual({key: value for key, value in after.items() if key not in added}, before)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(path.with_name(f"{path.stem}.schema-1-archive.json").exists())

    def test_concurrent_exact_migration_publishes_one_canonical_record(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()

        def migrate() -> dict:
            return worktree_legacy_operation_tool(
                fixture.cfg, contract.contract_path.as_posix(), self._request(digest)
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _index: migrate(), range(2)))
        self.assertTrue(all(result["ok"] for result in results))
        record = LifecycleOperationStore(path).read()
        assert record is not None and record.legacyMigration is not None
        self.assertEqual(record.generation, 1)
        self.assertEqual(record.legacyMigration.originalSha256, digest)

    def test_terminal_archive_preserves_bytes_and_receipt_replays_after_crash(self) -> None:
        fixture, contract, path, digest = self._terminal_fixture()
        request = replace(
            self._request(digest, action="archive"),
            memory_commit_message=None,
            ledger_commit_message=None,
        )
        original_unlink = Path.unlink

        def interrupted_unlink(candidate: Path, *args, **kwargs):
            if candidate == path:
                raise OSError("crash after archive receipt")
            return original_unlink(candidate, *args, **kwargs)

        with mock.patch.object(Path, "unlink", new=interrupted_unlink):
            interrupted = worktree_legacy_operation_tool(
                fixture.cfg,
                contract.contract_path.as_posix(),
                request,
            )
        self.assertFalse(interrupted["ok"])
        self.assertEqual(interrupted["status"], "legacy-archive-publication-pending")
        self.assertEqual(interrupted["nextTool"], "worktree_legacy_operation")
        archive_path = path.with_name(f"{path.stem}.schema-1-archive.json")
        self.assertTrue(path.exists())
        self.assertTrue(archive_path.exists())
        next_args = dict(interrupted["nextArgs"])
        next_contract = next_args.pop("contract_path")
        replay = worktree_legacy_operation_tool(
            fixture.cfg,
            next_contract,
            LegacyOperationRequest(**next_args),
        )
        self.assertTrue(replay["ok"])
        self.assertFalse(path.exists())
        self.assertTrue(archive_path.exists())
        self.assertEqual(replay["legacyDigest"], digest)

    def test_migrate_write_interruption_returns_executable_same_action(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        request = self._request(digest)
        original = path.read_bytes()
        with mock.patch(
            "agents_remember.worktrees.integration.legacy.legacy_operation_bridge.atomic_write_text",
            side_effect=OSError("migration replace interrupted"),
        ):
            interrupted = worktree_legacy_operation_tool(
                fixture.cfg,
                contract.contract_path.as_posix(),
                request,
            )
        self.assertFalse(interrupted["ok"])
        self.assertEqual(interrupted["status"], "legacy-migration-publication-interrupted")
        self.assertEqual(path.read_bytes(), original)
        next_args = dict(interrupted["nextArgs"])
        next_contract = next_args.pop("contract_path")
        migrated = worktree_legacy_operation_tool(
            fixture.cfg,
            next_contract,
            LegacyOperationRequest(**next_args),
        )
        self.assertTrue(migrated["ok"])
        self.assertEqual(migrated["state"], "migrated")

    def test_archive_refuses_nonterminal_or_incomplete_evidence(self) -> None:
        fixture, contract, path, digest = self._terminal_fixture()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "input-required"
        payload["phase"] = "contract-finalization"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            self._request(digest, action="archive"),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "legacy-digest-changed")
        exact_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            self._request(exact_digest, action="archive"),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "legacy-archive-terminal-proof-required")

    def test_terminal_integrate_archive_proves_protected_refs(self) -> None:
        fixture, contract, path, digest = self._terminal_integrate_fixture()
        request = LegacyOperationRequest(
            operation_kind="integrate",
            action="archive",
            expected_digest=digest,
            audit_reason="archive exact terminal integrate",
        )
        result = worktree_legacy_operation_tool(
            fixture.cfg, contract.contract_path.as_posix(), request
        )
        self.assertTrue(result["ok"])
        self.assertFalse(path.exists())

    def test_terminal_integrate_archive_refuses_moved_protected_ref(self) -> None:
        fixture, contract, path, digest = self._terminal_integrate_fixture()
        git(
            contract.code_repo_path,
            "update-ref",
            f"refs/heads/{contract.code_source_branch}",
            contract.code_base_commit,
        )
        result = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            LegacyOperationRequest(
                operation_kind="integrate",
                action="archive",
                expected_digest=digest,
                audit_reason="archive exact terminal integrate",
            ),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "legacy-integrate-archive-evidence-mismatch")
        self.assertTrue(path.exists())

    def test_migration_refuses_moved_live_code_identity(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        (contract.code_worktree / "after-incident.txt").write_text("later\n", encoding="utf-8")
        git(contract.code_worktree, "add", "after-incident.txt")
        git(contract.code_worktree, "commit", "-m", "move legacy code ref")
        result = worktree_legacy_operation_tool(
            fixture.cfg, contract.contract_path.as_posix(), self._request(digest)
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            result["status"],
            {"legacy-closeout-not-migratable", "legacy-code-output-mismatch"},
        )
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_inspect_remains_available_while_mutation_refuses_other_active_kind(self) -> None:
        fixture, contract, path, digest = self._terminal_fixture()
        original = path.read_bytes()
        path.unlink()
        self._start_unrelated_integration_below_source_admission(fixture, contract)
        path.write_bytes(original)
        inspected = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            self._request(digest, action="inspect"),
        )
        refused = worktree_legacy_operation_tool(
            fixture.cfg,
            contract.contract_path.as_posix(),
            self._request(digest, action="archive"),
        )
        self.assertTrue(inspected["ok"])
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["status"], "legacy-cross-kind-authority-active")

    def test_archive_reconciles_other_kind_natural_exit_without_reading_schema_target(
        self,
    ) -> None:
        fixture, contract, path, digest = self._terminal_fixture()
        original = path.read_bytes()
        path.unlink()
        self._start_unrelated_integration_below_source_admission(fixture, contract)
        integrate_path = operation_record_path(contract.worktree_group, "integrate")
        integrate_store = LifecycleOperationStore(integrate_path)
        runtime = lifecycle_operation_worker.OperationRuntime(integrate_store)
        runtime.start()
        runtime.finish({"ok": True}, ok=True)
        integrate_store.update(
            lambda current: current.model_copy(
                update={
                    "workerPid": 4242,
                    "workerLease": "6" * 64,
                    "workerProcessFingerprint": "7" * 64,
                }
            )
        )
        path.write_bytes(original)

        def exited(request):
            return request.model_copy(
                update={
                    "state": "exited",
                    "observedAt": "2026-08-22T15:00:00+00:00",
                    "detail": "other operation exited",
                }
            )

        with mock.patch(
            "agents_remember.worktrees.integration.lifecycle.worker.state."
            "observe_worker_termination",
            side_effect=exited,
        ):
            archived = worktree_legacy_operation_tool(
                fixture.cfg,
                contract.contract_path.as_posix(),
                replace(
                    self._request(digest, action="archive"),
                    memory_commit_message=None,
                    ledger_commit_message=None,
                ),
            )
        self.assertTrue(archived["ok"])
        self.assertFalse(path.exists())
        other = integrate_store.read()
        assert other is not None
        self.assertIsNone(other.workerPid)

    def test_migrated_recover_resumes_same_generation(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        migrated = worktree_legacy_operation_tool(
            fixture.cfg, contract.contract_path.as_posix(), self._request(digest)
        )
        with mock.patch(
            "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
            return_value=WorktreeCommandResult(
                0,
                {
                    "contract_path": contract.contract_path.as_posix(),
                    "task_name": contract.task_name,
                },
            ),
        ):
            status = worktree_status_tool(
                fixture.cfg,
                TaskRef(
                    repo_id=contract.repo_name,
                    contract_path=contract.contract_path.as_posix(),
                ),
            )
        closeout = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
        self.assertEqual([row["action"] for row in closeout["legalControls"]], ["recover"])
        stale_recover = closeout["legalControls"][0]
        arguments = dict(stale_recover["arguments"])
        control_contract = arguments.pop("contract_path")
        with mock.patch(
            "agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls."
            "launch_detached_worker"
        ) as launch:
            result = worktree_operation_control_tool(
                fixture.cfg,
                OperationControlRequest(contract_path=control_contract, **arguments),
            )
        record = LifecycleOperationStore(path).read()
        assert record is not None
        self.assertTrue(result["ok"])
        self.assertEqual((record.generation, record.attempt), (1, 2))
        launch.assert_called_once()
        runtime = lifecycle_operation_worker.OperationRuntime(LifecycleOperationStore(path))
        runtime.start()
        terminal_result: dict[str, object] = {
            "state": "completed-after-migration",
            "summary": "canonical generation completed",
        }
        runtime.finish(terminal_result, ok=True)
        before_stale = _byte_tree(Path(self.temp.name))
        with mock.patch(
            "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
            return_value=WorktreeCommandResult(
                0,
                {
                    "contract_path": contract.contract_path.as_posix(),
                    "task_name": contract.task_name,
                },
            ),
        ):
            terminal_status = worktree_status_tool(
                fixture.cfg,
                TaskRef(
                    repo_id=contract.repo_name,
                    contract_path=contract.contract_path.as_posix(),
                ),
            )
        terminal = next(
            row for row in terminal_status["lifecycleOperations"] if row["kind"] == "closeout"
        )
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["result"], terminal_result)
        self.assertNotIn("recover", [row["action"] for row in terminal["legalControls"]])
        stale_result = worktree_operation_control_tool(
            fixture.cfg,
            OperationControlRequest(contract_path=control_contract, **arguments),
        )
        self.assertFalse(stale_result["ok"])
        self.assertNotEqual(stale_result.get("nextAction"), "recover")
        self.assertEqual(_byte_tree(Path(self.temp.name)), before_stale)
        public = json.dumps(
            [migrated, status, result, terminal_status, stale_result], sort_keys=True
        )
        self.assertFalse(
            {
                "operationKey",
                "claimedOperationKey",
                "legacyOperationKey",
                "originalBytesBase64",
            }
            & _recursive_keys([migrated, status, result])
        )
        for private in (
            contract.code_repo_path.as_posix(),
            (contract.worktree_group / "reports" / "legacy.json").as_posix(),
            "legacy exact code output",
            "legacy owner approval",
            "schema-1 closeout stopped after code output",
            "PRIVATE-LEGACY-RESULT-/internal/result",
            "PRIVATE-LEGACY-FAILURE-/internal/failure",
        ):
            self.assertNotIn(private, public)

    def test_migrated_worker_termination_outranks_historical_recovery(self) -> None:
        fixture, contract, path, digest = self._legacy_fixture()
        worktree_legacy_operation_tool(
            fixture.cfg, contract.contract_path.as_posix(), self._request(digest)
        )
        store = LifecycleOperationStore(path)
        with mock.patch(
            "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
            return_value=WorktreeCommandResult(
                0,
                {
                    "contract_path": contract.contract_path.as_posix(),
                    "task_name": contract.task_name,
                },
            ),
        ):
            status = worktree_status_tool(
                fixture.cfg,
                TaskRef(
                    repo_id=contract.repo_name,
                    contract_path=contract.contract_path.as_posix(),
                ),
            )
        closeout = next(row for row in status["lifecycleOperations"] if row["kind"] == "closeout")
        stale_recover = dict(closeout["legalControls"][0]["arguments"])

        def launch_active(_contract, _record) -> None:
            store.update(
                lambda current: current.model_copy(
                    update={
                        "status": "running",
                        "phase": "recovering-after-claim",
                        "startedAt": current.startedAt or "2026-08-23T12:00:00+00:00",
                        "heartbeatAt": "2026-08-23T12:00:01+00:00",
                        "currentCommand": "recover task state",
                        "workerPid": 4242,
                        "workerLease": "a" * 64,
                        "workerProcessFingerprint": "b" * 64,
                    }
                )
            )

        with mock.patch(
            "agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls."
            "launch_detached_worker",
            side_effect=launch_active,
        ) as launch:
            recovered = worktree_operation_control_tool(
                fixture.cfg,
                OperationControlRequest(**stale_recover),
            )
        self.assertTrue(recovered["ok"])
        launch.assert_called_once()

        private = "PRIVATE_MIGRATED_SIGNAL /tmp/worker backend stderr"

        def still_live(request: WorkerTerminationEvidence) -> WorkerTerminationEvidence:
            return request.model_copy(
                update={
                    "state": "termination-required",
                    "detail": private,
                }
            )

        with (
            mock.patch(
                "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
                return_value=WorktreeCommandResult(
                    0,
                    {
                        "contract_path": contract.contract_path.as_posix(),
                        "task_name": contract.task_name,
                    },
                ),
            ),
            mock.patch(
                "agents_remember.worktrees.integration.lifecycle.worker.state."
                "observe_worker_termination",
                side_effect=still_live,
            ),
        ):
            active_status = worktree_status_tool(
                fixture.cfg,
                TaskRef(
                    repo_id=contract.repo_name,
                    contract_path=contract.contract_path.as_posix(),
                ),
            )
        active = next(
            row for row in active_status["lifecycleOperations"] if row["kind"] == "closeout"
        )
        # L18: a retained exact worker binding is ordinary live authority until a
        # real cancellation/termination transition records durable termination
        # evidence (worker_termination_required_result).  While the launched
        # recovery worker is live the projection stays on the running generation
        # and advertises only cancel, never a synthetic worker-termination result.
        self.assertEqual(active["result"]["state"], "legacy-closeout-recovery-required")
        self.assertEqual(active["result"]["nextAction"], "recover")
        self.assertEqual([row["action"] for row in active["legalControls"]], ["cancel"])
        cancel = active["legalControls"][0]

        def denied(request: WorkerTerminationEvidence) -> WorkerTerminationEvidence:
            return request.model_copy(update={"state": "termination-required", "detail": private})

        with (
            mock.patch(
                "agents_remember.worktrees.integration.lifecycle.worker.state."
                "observe_worker_termination",
                side_effect=still_live,
            ),
            mock.patch(
                "agents_remember.worktrees.integration.lifecycle.control.cancellation."
                "signal_worker_and_prove_exit",
                side_effect=denied,
            ),
        ):
            refused = worktree_operation_control_tool(
                fixture.cfg,
                OperationControlRequest(**cancel["arguments"]),
            )
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["status"], "worker-termination-required")
        self.assertEqual(refused["nextAction"], "cancel")
        retained = store.read()
        assert retained is not None and retained.workerTermination is not None
        self.assertEqual(retained.status, "termination-required")
        self.assertEqual(retained.workerTermination.state, "termination-required")
        with (
            mock.patch(
                "agents_remember.application.worktree_tools.git_worktree_manager.status_result",
                return_value=WorktreeCommandResult(
                    0,
                    {
                        "contract_path": contract.contract_path.as_posix(),
                        "task_name": contract.task_name,
                    },
                ),
            ),
            mock.patch(
                "agents_remember.worktrees.integration.lifecycle.worker.state."
                "observe_worker_termination",
                side_effect=still_live,
            ),
        ):
            retained_status = worktree_status_tool(
                fixture.cfg,
                TaskRef(
                    repo_id=contract.repo_name,
                    contract_path=contract.contract_path.as_posix(),
                ),
            )
        retained_projection = next(
            row for row in retained_status["lifecycleOperations"] if row["kind"] == "closeout"
        )
        self.assertEqual(retained_projection["result"]["state"], "worker-termination-required")
        self.assertEqual(
            [row["action"] for row in retained_projection["legalControls"]], ["cancel"]
        )
        self.assertEqual(retained_projection["failure"], retained_projection["result"]["summary"])
        self.assertEqual(retained_projection["guidance"], retained_projection["result"]["summary"])
        retained_bytes = path.read_bytes()
        retained_tree = _byte_tree(Path(self.temp.name))

        with mock.patch(
            "agents_remember.worktrees.integration.lifecycle.worker.state."
            "observe_worker_termination",
            side_effect=still_live,
        ):
            stale = worktree_operation_control_tool(
                fixture.cfg,
                OperationControlRequest(**stale_recover),
            )
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["status"], "lifecycle-control-not-legal")
        self.assertEqual(stale["nextAction"], "cancel")
        self.assertEqual(stale["nextArgs"]["action"], "cancel")
        self.assertEqual(path.read_bytes(), retained_bytes)
        self.assertEqual(_byte_tree(Path(self.temp.name)), retained_tree)
        self.assertNotIn(
            private,
            json.dumps([active_status, refused, retained_status, stale], sort_keys=True),
        )

    def test_schema_one_parser_is_isolated_and_removal_guard_is_measurable(self) -> None:
        fixture, contract, _path, digest = self._legacy_fixture()
        before = legacy_bridge_removal_guard(contract.coordination_root)
        self.assertEqual(before["recognizedSchemaOne"], 1)
        self.assertFalse(before["eligibleAfterReleaseWindow"])
        self.assertIn("one release window", LEGACY_BRIDGE_REMOVAL_CONDITION)
        normal_sources = (
            "lifecycle_operation_store.py",
            "lifecycle_operations.py",
            "operation.py",
        )
        roots = Path(__file__).parents[1] / "src" / "agents_remember"
        for name in normal_sources:
            matches = list(roots.rglob(name))
            self.assertEqual(len(matches), 1)
            self.assertNotIn("legacy_operation_schema", matches[0].read_text(encoding="utf-8"))
        worktree_legacy_operation_tool(
            fixture.cfg, contract.contract_path.as_posix(), self._request(digest)
        )
        after = legacy_bridge_removal_guard(contract.coordination_root)
        self.assertEqual(after["recognizedSchemaOne"], 0)
        self.assertEqual(after["nonterminalMigrated"], 1)
        self.assertFalse(after["eligibleAfterReleaseWindow"])


if __name__ == "__main__":
    unittest.main()
