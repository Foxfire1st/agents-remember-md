from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from agents_remember.application import worktree_status as status_application
from agents_remember.application import worktree_tools
from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.controlplane.records import (
    GateVerdict,
    create_gate,
    decide_gate,
)
from agents_remember.controlplane.store import GateStore
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, load_config
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    IntegrateOperationInput,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.integration.lifecycle.generation.creation import (
    snapshot_integration_authority,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_control_errors import (
    LifecycleControlError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    operation_key,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_lease import (
    contract_lifecycle_lease,
    require_lifecycle_operation_compatible,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location_errors import (
    LifecycleOperationLocationError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    operation_fingerprint,
    start_or_observe_operation,
)
from agents_remember.worktrees.integration.lifecycle.observation.projection import (
    _operation_location_decision,
    latest_operation_projection,
    observe_operation,
)
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    default_contract,
    load_contract,
    write_contract,
)
from closeout_input_test_support import (
    _fixture_leaf_document,
    closeout_operation_input,
    start_closeout_operation,
    with_commit_proven,
    with_mutation_intent,
)
from integration_branch_authority_test_support import (
    _authority_fixture,
    _closed_external_leaf_worktrees,
)
from lifecycle_control_test_support import (
    cancel_current_generation,
    control_current_generation,
)
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    install_fixture_profile,
)
from selected_lifecycle_test_support import (
    completed_selected_closeout_for_integration,
    finish_closeout_for_integration,
    ready_selected_integration,
    selected_contract,
)

TEST_WORKER_LEASE = "a" * 64
TEST_WORKER_FINGERPRINT = "b" * 64


def _reserve_test_worker(store: LifecycleOperationStore) -> str:
    store.update(
        lambda current: current.model_copy(
            update={
                "workerPid": os.getpid(),
                "workerLease": TEST_WORKER_LEASE,
                "workerProcessFingerprint": TEST_WORKER_FINGERPRINT,
            }
        )
    )
    return TEST_WORKER_LEASE


def _contract(tmp_path: Path, *, selected_profile: bool = False):
    coordination = tmp_path / "ar-coordination"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "lifecycle-tests@agents-remember.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Lifecycle Tests"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    if selected_profile:
        install_fixture_profile(repo, "repo")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", base_commit],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        cwd=repo,
        check=True,
    )
    (repo / "ar-memory").mkdir()
    contract = default_contract(
        ContractTask(
            name="durable-lifecycle",
            repo_name="repo",
            coordination_root=coordination,
            workflow_kind="light-task",
            memory_mode="internal",
        ),
        leaf=LeafIdentity(worktree_name="durable-lifecycle", leaf_id="L23"),
        code=RepoBranchPlan(
            repo_path=repo,
            source_branch="main",
            work_branch="feature/l23",
            base_commit=base_commit,
        ),
    )
    contract.code_worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            contract.code_work_branch,
            contract.code_worktree,
            contract.code_source_branch,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    write_contract(contract.contract_path, contract)
    publish_new_lifecycle_operation_location(
        contract,
        contract_text=contract.contract_path.read_text(encoding="utf-8"),
    )
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "version": 1,
                "coordinationRoot": coordination.as_posix(),
                "workspaceRoot": tmp_path.as_posix(),
                "repositories": {
                    "repo": (
                        {"certificationProfile": AGENTS_REMEMBER_PROFILE_REFERENCE.as_posix()}
                        if selected_profile
                        else {}
                    )
                },
            }
        ),
        encoding="utf-8",
    )
    if selected_profile:
        write_task_doc(
            contract.task_root,
            _fixture_leaf_document(
                contract, leaf_id=contract.leaf_id, leaf_slug=contract.leaf_id.lower()
            ),
        )
        _write_lifecycle_task_context(contract, integration_branch="main")
    return contract


def _input(contract, *, message: str = "close L23") -> CloseoutOperationInput:
    return closeout_operation_input(contract, code=message)


def _publish_integration_branch_authority(contract) -> str:
    repo = contract.code_repo_path
    commit = subprocess.run(
        ["git", "rev-parse", "refs/heads/main"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "branch", "super", commit], cwd=repo, check=True)
    _write_lifecycle_task_context(contract, integration_branch="super")
    return commit


def _write_lifecycle_task_context(contract, *, integration_branch: str) -> None:
    master_path = contract.task_root / "task.json"
    previous_master = json.loads(master_path.read_text()) if master_path.is_file() else {}
    sprint_path = contract.task_root.parent / "lifecycle-fixture-sprint" / "task.json"
    previous_sprint = json.loads(sprint_path.read_text()) if sprint_path.is_file() else {}
    write_task_doc(
        contract.task_root,
        TaskDocument.model_validate(
            {
                **previous_master,
                "id": "DURABLE-LIFECYCLE",
                "slug": "durable-lifecycle",
                "title": "Durable lifecycle",
                "kind": "master",
                "status": "inProgress",
                "repo": "repo",
                "createdAt": "2026-08-15T00:00:00+00:00",
                "executionNature": "organizational",
                "subTasks": previous_master.get(
                    "subTasks",
                    [
                        {
                            "number": contract.leaf_id,
                            "name": contract.leaf_id,
                            "file": f"{contract.leaf_id.lower()}.md",
                            "status": "inProgress",
                        }
                    ],
                ),
            }
        ),
    )
    write_task_doc(
        contract.task_root.parent / "lifecycle-fixture-sprint",
        TaskDocument.model_validate(
            {
                **previous_sprint,
                "id": "LIFECYCLE-FIXTURE-SPRINT",
                "slug": "lifecycle-fixture-sprint",
                "title": "Lifecycle fixture sprint",
                "kind": "master",
                "status": "inProgress",
                "repo": "repo",
                "createdAt": "2026-08-15T00:00:00+00:00",
                "orchestrates": ["durable-lifecycle"],
                "integrationBranch": integration_branch,
                "executionGraph": {
                    "nodes": [{"repository": "repo", "path": "durable-lifecycle/task.json"}],
                    "edges": [],
                },
            }
        ),
    )


def _integration_ready(contract):
    commit = _publish_integration_branch_authority(contract)
    closed = replace(
        load_contract(contract.contract_path),
        code_source_branch="super",
        closeout_status="completed",
        code_commit=commit,
    )
    write_contract(closed.contract_path, closed)
    return closed


def _completed_closeout_for_integration(contract):
    commit = _publish_integration_branch_authority(contract)
    contract = replace(load_contract(contract.contract_path), code_source_branch="super")
    write_contract(contract.contract_path, contract)
    return finish_closeout_for_integration(contract, commit, _input(contract))


def test_integration_authority_refuses_incomplete_closeout_edges(tmp_path: Path) -> None:
    closed = ready_selected_integration(selected_contract(tmp_path / "internal"))
    operation_input = IntegrateOperationInput(
        configPath=(closed.code_repo_path.parent / "settings.json").as_posix(),
        contractPath=closed.contract_path.as_posix(),
    )
    with pytest.raises(RuntimeError, match="completed closeout code commit"):
        snapshot_integration_authority(
            replace(closed, code_commit=""),
            operation_input,
        )

    fixture = _authority_fixture(tmp_path / "external", external_memory=True)
    external = _closed_external_leaf_worktrees(
        fixture, tmp_path / "external", publish_closeout_evidence=False
    )
    with pytest.raises(RuntimeError, match="external-memory integration authority"):
        snapshot_integration_authority(
            replace(external, memory_content_commit=""),
            IntegrateOperationInput(
                configPath=fixture.config_path.as_posix(),
                contractPath=external.contract_path.as_posix(),
            ),
        )


def test_start_returns_immediately_and_duplicate_observes_one_launch(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path, candidate_file=("candidate.py", "VALUE = 1\n"))
    launches = []

    def launcher(loaded, record) -> None:
        launches.append((loaded.task_name, record.fingerprint))

    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)

    first = start_closeout_operation(
        _input(contract, message="  close L23  "), launcher=launcher, now=now
    )
    second = start_closeout_operation(_input(contract), launcher=launcher, now=now)

    assert first.status == second.status == "queued"
    assert len(launches) == 1
    assert "job" not in first.model_dump_json().lower()
    assert "pid" not in first.model_dump_json().lower()


def test_conflicting_commit_message_refuses_while_task_operation_exists(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path, candidate_file=("candidate.py", "VALUE = 1\n"))
    start_closeout_operation(_input(contract), launcher=lambda *_: None)

    with pytest.raises(RuntimeError, match="conflicting closeout intent"):
        start_closeout_operation(
            _input(contract, message="different mutation"), launcher=lambda *_: None
        )


def test_contract_lifecycle_lease_excludes_cross_kind_and_terminal_mutation(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)

    with contract_lifecycle_lease(contract):
        with pytest.raises(RuntimeError, match=r"integrate cannot proceed.*closeout"):
            require_lifecycle_operation_compatible(contract, operation_kind="integrate")
        with pytest.raises(RuntimeError, match=r"terminal mutation cannot proceed.*closeout"):
            require_lifecycle_operation_compatible(contract, operation_kind=None)
        require_lifecycle_operation_compatible(contract, operation_kind="closeout")


def test_changed_worktree_is_a_different_closeout_candidate(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    first = store.read()
    assert first is not None and first.candidateTree is not None
    (contract.code_worktree / "later.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match="closeout candidate changed outside the accepted generation's proven output",
    ):
        start_closeout_operation(_input(contract), launcher=lambda *_: None)

    current = store.read()
    assert current is not None
    assert current.candidateTree == first.candidateTree


def test_status_projects_every_current_task_operation(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    config = load_config(tmp_path / "settings.json")
    closeout = Mock()
    closeout.model_dump.return_value = {"kind": "closeout", "status": "completed"}
    integrate = Mock()
    integrate.model_dump.return_value = {"kind": "integrate", "status": "cancelled"}
    result = WorktreeCommandResult(
        0,
        {
            "contract_path": contract.contract_path.as_posix(),
            "task_name": contract.task_name,
        },
    )
    with (
        patch.object(worktree_tools.git_worktree_manager, "status_result", return_value=result),
        patch.object(status_application, "resolve_lifecycle_caller", return_value=None),
        patch.object(
            status_application,
            "current_operation_projections",
            return_value=[closeout, integrate],
        ) as current,
    ):
        payload = worktree_tools.worktree_status_tool(
            config,
            TaskRef(
                repo_id=contract.repo_name,
                contract_path=contract.contract_path.as_posix(),
            ),
        )

    assert "lifecycleOperation" not in payload
    assert payload["lifecycleOperations"] == [
        {"kind": "closeout", "status": "completed"},
        {"kind": "integrate", "status": "cancelled"},
    ]
    closeout.model_dump.assert_called_once_with(mode="json", exclude_none=True)
    integrate.model_dump.assert_called_once_with(mode="json", exclude_none=True)
    current.assert_called_once()
    assert current.call_args.kwargs["contract"] == contract
    assert current.call_args.kwargs["location"].worktree_group == contract.worktree_group

    with (
        patch.object(
            worktree_tools.git_worktree_manager,
            "status_result",
            return_value=WorktreeCommandResult(0, {"task_name": "unattached"}),
        ),
        patch.object(status_application, "current_operation_projections") as current,
    ):
        unattached = worktree_tools.worktree_status_tool(
            config,
            TaskRef(repo_id=contract.repo_name),
        )
    assert "lifecycleOperation" not in unattached
    current.assert_not_called()


def test_closeout_preview_path_is_task_addressed() -> None:
    config = cast(McpRuntimeConfig, Mock())
    messages = worktree_tools.CloseoutCommitMessages(code="close L23")
    approval = worktree_tools.CloseoutApproval(dry_run=True)
    with patch.object(worktree_tools, "_worktree_closeout", return_value={"ok": True}) as close:
        assert worktree_tools.worktree_closeout_apply_tool(
            config, "/tmp/contract.yaml", messages, approval
        ) == {"ok": True}
    close.assert_called_once()


def test_stale_queued_generation_requires_explicit_recover(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    old = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    start_closeout_operation(_input(contract), launcher=lambda *_: None, now=old)
    launches = []

    observed = start_closeout_operation(
        _input(contract),
        launcher=lambda _, record: launches.append(record.attempt),
        now=old + timedelta(seconds=31),
    )
    assert observed.status == "queued"
    assert launches == []
    with patch(
        "agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls.launch_detached_worker",
        side_effect=lambda _, record: launches.append(record.attempt),
    ):
        projection = control_current_generation(contract.contract_path, "closeout", "recover")
    assert launches == [2]
    assert projection.status == "queued"


def test_repeated_apply_resumes_exact_preboundary_failure_same_generation(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    store.update(
        lambda record: record.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": "2026-08-12T12:01:00+00:00",
            }
        )
    )
    failed = store.read()
    assert failed is not None
    attempts: list[int] = []

    observed = start_closeout_operation(
        _input(contract), launcher=lambda _, record: attempts.append(record.attempt)
    )
    current = store.read()
    assert observed.status == "queued"
    assert current is not None
    assert current.operationKey == failed.operationKey
    assert current.generation == failed.generation
    assert current.input == failed.input
    assert current.candidateState == failed.candidateState
    assert current.attempt == failed.attempt + 1
    assert attempts == [2]


def test_cancel_before_boundary_proves_exit_before_releasing_worker_authority(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    worker_lease = "c" * 64
    worker_pid = 4242
    process_fingerprint = "d" * 64
    store.update(
        lambda current: current.model_copy(
            update={
                "status": "running",
                "phase": "preflight",
                "startedAt": "2026-08-22T09:59:00+00:00",
                "heartbeatAt": "2026-08-22T10:00:00+00:00",
                "currentCommand": "validate lifecycle operation",
                "workerPid": worker_pid,
                "workerLease": worker_lease,
                "workerProcessFingerprint": process_fingerprint,
            }
        )
    )
    running = store.read()
    assert running is not None
    assert running.workerPid == worker_pid
    assert running.workerLease == worker_lease
    assert running.workerProcessFingerprint == process_fingerprint

    def prove_exit(request):
        persisted = store.read()
        assert persisted is not None
        assert persisted.workerPid == request.pid
        assert persisted.workerTermination is not None
        assert persisted.workerTermination.state == "requested"
        return request.model_copy(
            update={
                "state": "exited",
                "observedAt": "2026-08-22T10:00:00+00:00",
                "detail": "exact process exited",
            }
        )

    with (
        patch(
            "agents_remember.worktrees.integration.lifecycle.worker.state."
            "observe_worker_termination",
            return_value=None,
        ),
        patch(
            "agents_remember.worktrees.integration.lifecycle.control.cancellation."
            "signal_worker_and_prove_exit",
            side_effect=prove_exit,
        ),
    ):
        projection = cancel_current_generation(contract.contract_path, "closeout")

    current = store.read()
    assert projection.status == "cancelled"
    assert current is not None and current.workerPid is None
    assert current.workerTermination is not None
    assert current.workerTermination.state == "exited"


def test_cancel_after_boundary_refuses_without_making_approval_reusable(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path, candidate_file=("boundary.txt", "boundary\n"))
    operation_input = _input(contract)
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    store.update(with_mutation_intent)
    store.update(
        lambda record: with_commit_proven(record).model_copy(
            update={"status": "running", "phase": "code-commit", "approvalClaimed": True}
        )
    )

    with pytest.raises(LifecycleControlError) as raised:
        cancel_current_generation(contract.contract_path, "closeout")
    assert raised.value.status == "lifecycle-immutable-output-recovery-required"
    assert raised.value.next_action == "recover"
    assert raised.value.observed["irreversibleBoundaryEntered"] is True
    mutation_evidence = raised.value.observed["mutationEvidence"]
    assert isinstance(mutation_evidence, dict)
    code_evidence = mutation_evidence["code"]
    assert isinstance(code_evidence, dict)
    assert code_evidence["state"] == "commit-proven"
    assert store.read().approvalClaimed is True  # type: ignore[union-attr]


def test_internal_operation_key_is_stable_but_not_part_of_projection(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    operation_input = _input(contract)
    fingerprint = operation_fingerprint(operation_input)
    key = operation_key(contract.contract_path, "closeout", fingerprint)

    projection = start_closeout_operation(operation_input, launcher=lambda *_: None)

    assert len(key) == 64
    assert key not in projection.model_dump_json()


def test_consumed_gate_recovers_only_the_same_internal_operation(tmp_path: Path) -> None:
    store = GateStore(tmp_path / "observer")
    opened = create_gate(
        "closeout-approval", gate_id="01KZTTESTGATE0000000000000", now="2026-08-12T10:00:00+00:00"
    )
    approved = decide_gate(
        opened,
        GateVerdict(decision="approve", via="chat", by="developer"),
        now="2026-08-12T10:01:00+00:00",
    )
    store.append(opened)
    store.append(approved)

    claimed = store.claim_approval(
        None,
        kind="closeout-approval",
        now="2026-08-12T10:02:00+00:00",
        operation_key="a" * 64,
    )
    recovered = store.claim_approval(
        None,
        kind="closeout-approval",
        now="2026-08-12T10:03:00+00:00",
        operation_key="a" * 64,
    )
    conflicting = store.claim_approval(
        None,
        kind="closeout-approval",
        now="2026-08-12T10:04:00+00:00",
        operation_key="b" * 64,
    )

    assert claimed.permitted and recovered.permitted
    assert not conflicting.permitted
    assert len(store.read(None)) == 3


def test_observe_latest_terminal_cancel_and_launch_failure_are_task_addressed(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path)
    assert observe_operation(contract.contract_path, "closeout") is None
    assert latest_operation_projection(contract.contract_path) is None

    private_launch = "PRIVATE_LAUNCH_STDERR_SENTINEL /tmp/native-runner"
    with pytest.raises(RuntimeError, match="lifecycle-worker-launch-failed") as raised:
        start_closeout_operation(
            _input(contract),
            launcher=lambda *_: (_ for _ in ()).throw(RuntimeError(private_launch)),
        )
    assert private_launch not in str(raised.value)
    failed = observe_operation(contract.contract_path, "closeout")
    assert failed is not None and failed.status == "failed"
    assert private_launch not in str(failed.model_dump(mode="json"))
    assert failed.result["failureEvidence"]["errorType"] == "RuntimeError"  # type: ignore[index]
    assert cancel_current_generation(contract.contract_path, "closeout").status == "cancelled"

    contract = completed_selected_closeout_for_integration(
        selected_contract(tmp_path / "integration")
    )
    integration = IntegrateOperationInput(
        configPath=(contract.code_repo_path.parent / "settings.json").as_posix(),
        contractPath=contract.contract_path.as_posix(),
    )
    start_or_observe_operation(integration, contract, launcher=lambda *_: None)
    latest = latest_operation_projection(contract.contract_path)
    assert latest is not None and latest.kind == "integrate" and latest.status == "queued"


def test_detached_launcher_uses_native_environment_and_private_process_group(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record = store.read()
    assert record is not None
    process = SimpleNamespace(pid=9876)
    process_fingerprint = "c" * 64
    environment = {"PATH": "/usr/bin", "PYTHONPATH": "/existing"}

    with (
        patch.object(lifecycle_operations, "git_environment", return_value=environment),
        patch.object(
            lifecycle_operations,
            "native_subprocess_environment",
            return_value=dict(environment),
        ) as native_env,
        patch.object(
            lifecycle_operations,
            "native_command",
            side_effect=lambda command, _env: command,
        ),
        patch.object(
            lifecycle_operations,
            "worker_process_fingerprint",
            return_value=process_fingerprint,
        ) as fingerprint,
        patch.object(
            lifecycle_operations,
            "retain_detached_worker_child",
        ) as retain_child,
        patch.object(lifecycle_operations.subprocess, "Popen", return_value=process) as popen,
    ):
        lifecycle_operations.launch_detached_worker(contract, record)

    native_env.assert_called_once()
    command = popen.call_args.args[0]
    assert "agents_remember.application.lifecycle.lifecycle_operation_worker" in command
    assert popen.call_args.kwargs["env"]["PYTHONPATH"] == "/existing"
    assert (contract.code_worktree / "mcp" / "src").as_posix() not in str(
        popen.call_args.kwargs["env"]
    )
    assert popen.call_args.kwargs["start_new_session"] is True
    bound = store.read()
    assert bound is not None
    assert bound.workerPid == 9876
    assert bound.workerProcessFingerprint == process_fingerprint
    fingerprint.assert_called_once_with(9876)
    retain_child.assert_called_once_with(process)


def test_input_identity_and_closeout_approval_are_validated_before_launch(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    with pytest.raises(RuntimeError, match="does not resolve"):
        lifecycle_operations._validate_input_identity(
            contract,
            _input(contract).model_copy(update={"contractPath": (tmp_path / "other").as_posix()}),
        )
    with pytest.raises(RuntimeError, match="non-empty approval intent"):
        lifecycle_operations._validate_input_identity(
            contract,
            _input(contract).model_copy(update={"approvalNote": "  "}),
        )


def test_operation_runtime_tracks_progress_reports_and_terminal_outcomes(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path, candidate_file=("runtime.txt", "runtime\n"))
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    running = runtime.start()
    assert running.status == "running"
    assert runtime.start().status == "running"

    runtime.progress(
        "approval-claim",
        {
            "current_command": "claim exact approval",
            "approval_claimed": True,
        },
    )
    assert store.read().approvalClaimed is True  # type: ignore[union-attr]
    current = store.read()
    assert current is not None
    intent = with_mutation_intent(current).mutationEvidence["code"]
    runtime.progress(
        "code-commit",
        {"mutation_evidence": intent.model_dump(mode="json")},
    )
    current = store.read()
    assert current is not None
    proven = with_commit_proven(current).mutationEvidence["code"]
    runtime.progress(
        "code-commit",
        {"mutation_evidence": proven.model_dump(mode="json")},
    )
    after_proof = store.read()
    assert after_proof is not None and after_proof.recoveryCommits is not None
    assert after_proof.recoveryCommits.codeCommit == "e" * 40
    runtime.progress(
        "ledger-commit",
        {
            "recovery_commits": {
                "codeCommit": "e" * 40,
                "memoryContentCommit": "b" * 40,
                "ledgerCommit": "c" * 40,
            }
        },
    )
    recorded = store.read()
    assert recorded is not None and recorded.recoveryCommits is not None
    assert recorded.recoveryCommits.codeCommit == "e" * 40
    assert recorded.recoveryCommits.ledgerCommit == "c" * 40
    with pytest.raises(RuntimeError, match="cannot be cleared"):
        store.update(lambda record: record.model_copy(update={"recoveryCommits": None}))
    with pytest.raises(RuntimeError, match="can only fill empty cells"):
        store.update(
            lambda record: record.model_copy(
                update={
                    "recoveryCommits": LifecycleOperationRecoveryCommits(
                        codeCommit="e" * 40,
                        memoryContentCommit="d" * 40,
                        ledgerCommit="c" * 40,
                    )
                }
            )
        )

    quality = store.path.parent / lifecycle_operation_worker.QUALITY_PROGRESS_REPORT
    quality.write_text(
        json.dumps(
            {
                "status": "running",
                "step": "dagger",
                "detail": "PRIVATE_BACKEND_STDERR_SENTINEL /tmp/private",
            }
        ),
        encoding="utf-8",
    )
    assert runtime._quality_command() == "quality stage: dagger"
    quality.write_text("not-json", encoding="utf-8")
    dagger = store.path.parent / "dagger-progress.log"
    dagger.write_text("first\nPRIVATE_DAGGER_LOG_SENTINEL /tmp/private\n", encoding="utf-8")
    assert runtime._quality_command() is None
    dagger.unlink()
    assert runtime._quality_command() is None

    quality.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    assert runtime._quality_command() is None
    quality.write_text(
        json.dumps({"status": "running", "step": 3, "detail": "bad"}), encoding="utf-8"
    )
    assert runtime._quality_command() is None

    runtime.finish({"reason": "merge needs reconciliation"}, ok=False)
    recovery = store.read()
    assert recovery is not None and recovery.status == "input-required"
    assert recovery.phase == "contract-finalization"
    assert recovery.workerPid is None


def test_operation_runtime_heartbeat_updates_running_and_ignores_terminal_record(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    runtime.stop = Mock()
    runtime.stop.wait.side_effect = [False, True]
    with patch.object(runtime, "_quality_command", return_value="quality stage: dagger"):
        runtime.heartbeat()
    assert store.read().currentCommand == "quality stage: dagger"  # type: ignore[union-attr]

    store.update(
        lambda record: record.model_copy(update={"status": "completed", "phase": "completed"})
    )
    runtime.stop.wait.side_effect = [False, True]
    runtime.heartbeat()
    assert store.read().status == "completed"  # type: ignore[union-attr]


def test_operation_runtime_failure_modes_and_cancelled_progress(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    runtime.finish({"developerDecisionRequired": True, "summary": "choose"}, ok=False)
    assert store.read().status == "input-required"  # type: ignore[union-attr]
    assert store.read().workerPid is None  # type: ignore[union-attr]
    cancelled = cancel_current_generation(contract.contract_path, "closeout")
    assert cancelled.status == "cancelled"
    with pytest.raises(lifecycle_operation_worker.OperationCancelled):
        runtime.progress("quality", {})
    runtime.finish({"ok": True}, ok=True)
    assert store.read().status == "cancelled"  # type: ignore[union-attr]

    second_contract = selected_contract(tmp_path / "second")
    start_closeout_operation(_input(second_contract), launcher=lambda *_: None)
    second_store = LifecycleOperationStore(
        operation_record_path(second_contract.worktree_group, "closeout")
    )
    second_runtime = lifecycle_operation_worker.OperationRuntime(second_store)
    second_runtime.start()
    second_runtime.finish({"commit": "a" * 40}, ok=True)
    assert second_store.read().status == "completed"  # type: ignore[union-attr]


def test_execute_operation_dispatches_closeout_and_integration_payloads(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    runtime = Mock()
    config = load_config(Path(_input(contract).configPath))
    with patch.object(lifecycle_operation_worker, "load_config", return_value=config):
        start_closeout_operation(_input(contract), launcher=lambda *_: None)
        closeout_store = LifecycleOperationStore(
            operation_record_path(contract.worktree_group, "closeout")
        )
        closeout = closeout_store.read()
        assert closeout is not None
        with patch.object(
            lifecycle_operation_worker,
            "execute_selected_closeout",
            return_value=WorktreeCommandResult(0, {"state": "closed"}),
        ) as execute_selected:
            lifecycle_operation_worker.execute_operation(closeout, runtime)
        execute_selected.assert_called_once_with(
            load_contract(contract.contract_path), closeout, runtime.store
        )
        runtime.finish.assert_called_with(
            {"state": "closed", "ok": True, "operation": "worktree_closeout_apply"}, ok=True
        )
        closeout_runtime = lifecycle_operation_worker.OperationRuntime(closeout_store)
        closeout_runtime.start()
        closeout_runtime.finish(
            {"state": "closed", "ok": True, "operation": "worktree_closeout_apply"},
            ok=True,
        )

        contract = completed_selected_closeout_for_integration(
            selected_contract(tmp_path / "integration")
        )
        integration_input = IntegrateOperationInput(
            configPath=(contract.code_repo_path.parent / "settings.json").as_posix(),
            contractPath=contract.contract_path.as_posix(),
            autoCompleteSeats=False,
        )
        start_or_observe_operation(integration_input, contract, launcher=lambda *_: None)
        integration = LifecycleOperationStore(
            operation_record_path(contract.worktree_group, "integrate")
        ).read()
        assert integration is not None
        runtime.store.read.return_value = integration
        with patch.object(
            lifecycle_operation_worker,
            "integrate_result",
            return_value=WorktreeCommandResult(2, {"reason": "blocked"}),
        ):
            lifecycle_operation_worker.execute_operation(integration, runtime)
        runtime.finish.assert_called_with(
            {
                "reason": "blocked",
                "ok": False,
                "operation": "worktree_integrate",
            },
            ok=False,
        )


def test_integration_completion_auto_closes_seats_only_for_a_green_edge(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    operation_input = IntegrateOperationInput(
        configPath="settings.json",
        contractPath=contract.contract_path.as_posix(),
        autoCompleteSeats=True,
    )
    with patch.object(
        lifecycle_operation_worker,
        "auto_complete_seats",
        return_value={"completedSeatCount": 3},
    ) as complete:
        payload = lifecycle_operation_worker.integration_completion_payload(
            cast(McpRuntimeConfig, SimpleNamespace()),
            operation_input,
            WorktreeCommandResult(0, {"state": "integrated"}),
        )
    assert payload["completedSeatCount"] == 3
    complete.assert_called_once()

    with patch.object(lifecycle_operation_worker, "auto_complete_seats") as complete:
        lifecycle_operation_worker.integration_completion_payload(
            cast(McpRuntimeConfig, SimpleNamespace()),
            operation_input,
            WorktreeCommandResult(2, {"state": "blocked"}),
        )
    complete.assert_not_called()


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (lifecycle_operation_worker.OperationCancelled(), 0),
        (RuntimeError("worker failed"), 1),
        (None, 0),
    ],
)
def test_run_worker_records_execution_outcome(
    tmp_path: Path, side_effect: Exception | None, expected: int
) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    worker_lease = _reserve_test_worker(store)
    with patch.object(lifecycle_operation_worker, "execute_operation", side_effect=side_effect):
        assert (
            lifecycle_operation_worker.run_worker(
                contract.contract_path,
                "closeout",
                worker_lease,
            )
            == expected
        )
    current = store.read()
    if expected == 1:
        assert current is not None and current.status == "failed"


def test_run_worker_refuses_missing_or_non_startable_durable_state(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    with pytest.raises(RuntimeError, match="no closeout operation is queued"):
        lifecycle_operation_worker.run_worker(
            contract.contract_path,
            "closeout",
            TEST_WORKER_LEASE,
        )

    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    worker_lease = _reserve_test_worker(store)
    store.update(
        lambda record: record.model_copy(update={"status": "cancelled", "phase": "cancelled"})
    )
    assert (
        lifecycle_operation_worker.run_worker(contract.contract_path, "closeout", worker_lease) == 0
    )

    second = selected_contract(tmp_path / "non-startable")
    start_closeout_operation(_input(second), launcher=lambda *_: None)
    second_store = LifecycleOperationStore(operation_record_path(second.worktree_group, "closeout"))
    second_lease = _reserve_test_worker(second_store)
    second_store.update(
        lambda record: record.model_copy(update={"status": "input-required", "phase": "failed"})
    )
    with pytest.raises(RuntimeError, match="cannot start from durable state"):
        lifecycle_operation_worker.run_worker(second.contract_path, "closeout", second_lease)


def test_run_worker_observes_terminal_generation_while_waiting_for_lease(
    tmp_path: Path,
) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    queued = store.read()
    assert queued is not None
    waiting = queued.model_copy(update={"workerLease": "a" * 64})
    terminal = waiting.model_copy(update={"status": "completed", "phase": "completed"})
    observed = SimpleNamespace(read=Mock(side_effect=[waiting, terminal]))

    with patch.object(
        lifecycle_operation_worker,
        "located_lifecycle_operation_store",
        return_value=observed,
    ):
        assert (
            lifecycle_operation_worker.run_worker(
                contract.contract_path,
                "closeout",
                "b" * 64,
            )
            == 0
        )


def test_run_worker_observes_matching_lease_after_waiting(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    queued = store.read()
    assert queued is not None
    waiting = queued.model_copy(update={"workerLease": "a" * 64})
    matching = queued.model_copy(update={"workerLease": "b" * 64})
    terminal = matching.model_copy(update={"status": "completed", "phase": "completed"})
    observed = SimpleNamespace(read=Mock(side_effect=[waiting, matching]))
    runtime = Mock()
    runtime.start.return_value = terminal

    with (
        patch.object(
            lifecycle_operation_worker,
            "located_lifecycle_operation_store",
            return_value=observed,
        ),
        patch.object(
            lifecycle_operation_worker,
            "OperationRuntime",
            return_value=runtime,
        ),
    ):
        assert (
            lifecycle_operation_worker.run_worker(
                contract.contract_path,
                "closeout",
                "b" * 64,
            )
            == 0
        )
    runtime.start.assert_called_once_with()


def test_operation_location_decision_binds_developer_decision(tmp_path: Path) -> None:
    contract = selected_contract(tmp_path)
    start_closeout_operation(_input(contract), launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record = store.read()
    assert record is not None

    error = LifecycleOperationLocationError(
        "operation-location-mismatch",
        "the readable contract contradicts its immutable enclosure manifest",
        expected={
            "contractPath": contract.contract_path.as_posix(),
            "route": "locator -> root manifest -> root journal",
        },
        observed={"state": "manifest-identity-mismatch"},
    )
    decision = _operation_location_decision(record, error)

    assert decision.result is not None
    assert decision.result["state"] == "operation-location-mismatch"
    assert decision.result["developerDecisionRequired"] is True
    assert decision.result["nextAction"] == "developer-decision"
    assert decision.recommendedAction is not None
    assert decision.recommendedAction.action == "developer-decision"
    assert decision.legalControls == []
    assert decision.cancellable is False
