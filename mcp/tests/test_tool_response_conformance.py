"""Dev-time conformance tests for MCP tool response contracts.

Production code already validates every tool payload against its registered model
in ``agents_remember.mcp.tools._tool_payload`` (``model_validate(...).model_dump(
mode="json", exclude_none=True)``). Strict models use ``extra="forbid"`` so
application-layer drift fails loudly at runtime. These tests move that guarantee into
suite so drift is caught at dev time instead of in a live call.

For every response-modeled tool payload builder we obtain a *representative*
response payload by invoking the real ``*_payload`` builder against a temporary
fixture workspace, then assert:

* the payload validates against the registered model with no error, and
* round-tripping the payload through the model does not fabricate keys.

We also assert the strict/flexible split is exactly what the response-model
taxonomy intends: every model that is not built on ``FlexibleResponseModel`` keeps
``extra="forbid"``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(MCP_SRC))
sys.path.insert(0, str(MCP_TESTS))

from agents_remember.application.closeout_door import CloseoutDoorRequest
from agents_remember.application.closeout_queue import CloseoutQueueRequest
from agents_remember.application.gate_tools import GateRaise, GateWait
from agents_remember.application.lifecycle.direct_landing import DirectLandingRequest
from agents_remember.application.lifecycle.legacy_operation_tool import LegacyOperationRequest
from agents_remember.application.lifecycle.lifecycle_enclosure_tools import EnclosureAdoptionRequest
from agents_remember.application.lifecycle.lifecycle_status_wait import LifecycleStatusWaitRequest
from agents_remember.application.memory_tools import CarryoverSelection, CitationOperationScope
from agents_remember.application.orchestration_tools import NudgeSubject, NudgeTarget
from agents_remember.application.provider_tools import (
    GrepaiSearchQuery,
    GrepaiTraceQuery,
    ProviderQueryScope,
)
from agents_remember.application.task_docs.task_doc_tools import TaskDocEdit, TaskDocTarget
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.application.terminal_tools import RetiredSpawnInputs
from agents_remember.application.worktree_services import (
    bind_worktree_services,
    build_default_worktree_services,
)
from agents_remember.application.worktree_tools import (
    CloseoutApproval,
    CloseoutCommitMessages,
    OperationControlRequest,
    StartExecution,
    TaskBases,
    TaskIdentity,
)
from agents_remember.controlplane.operator_inbox_records import (
    InboxAddress,
    InboxMessage,
    InboxPoster,
)
from agents_remember.controlplane.records import GateAnchor, GateRequest, GateVerdict
from agents_remember.kernel.memory_ledger import create_initial_ledger, write_ledger
from agents_remember.kernel.primitives.runtime_config import (
    load_config,
)
from agents_remember.mcp import tools
from agents_remember.mcp.tools import memory as memory_payload_tools
from agents_remember.mcp.tools.base import _tool_payload
from agents_remember.models.base import FlexibleResponseModel
from agents_remember.models.lifecycles.curator_coherence import CuratorCoherenceRequest
from agents_remember.models.memory import MemoryQualitySyncRequest
from agents_remember.models.structural.agent import (
    DispatchAgentRequest,
    RenameChildRequest,
    RetireChildRequest,
    StructuralMessageRequest,
)
from agents_remember.models.structural.gates import (
    StructuralGateDecisionRequest,
    StructuralLifecycleGateRequest,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.tools.tool_registry import TOOL_RESPONSE_MODELS
from agents_remember.observer import (
    AmbientLifecycle,
    EventStore,
    install_ambient,
    reset_ambient,
)
from agents_remember.observer.ambient import AmbientTiming
from agents_remember.serving.agent_notifier_heartbeat import AgentNotifierHeartbeatStore
from agents_remember.serving.build_info import ServingBuild
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.worktrees.queue.closeout_projection_publication import (
    refresh_closeout_projection,
)
from agents_remember.worktrees.services import reset_worktree_services
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    default_contract,
    load_contract,
    write_contract,
)
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    install_fixture_profile,
)
from selected_lifecycle_test_support import declare_selected_candidate
from task_reopen_test_support import (
    _completed_leaf_contract,
    _leaf_doc,
    _master_doc,
    _runtime_config,
)
from test_config import settings_payload
from test_worktree_support import (
    commit_file,
    git,
    init_repo,
    initialized_memory_repo,
    integrated_external_contract_fixture,
    write_file_onboarding,
)

REPO = "agents-remember"
DRY_RUN_SCOPE = ProviderQueryScope(dry_run=True)
EXTERNAL_MEMORY_BASES = TaskBases(memory_mode="external")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _run_git(repo: Path, args: list[str]) -> None:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)


def _write_leaf_task(
    coordination_root: Path,
    *,
    master: str = "master",
    doc_id: str = "leaf-1",
    slug: str | None = None,
    execution_graph: bool = True,
) -> None:
    repo = REPO
    slug = slug or doc_id
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
                "executionNature": "organizational" if execution_graph else "atomic",
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
    if not execution_graph:
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
        return

    sprint_root = coordination_root / "tasks" / repo / "sprint"
    orchestrates = sorted(
        path.name
        for path in (coordination_root / "tasks" / repo).iterdir()
        if path.is_dir() and path.name != sprint_root.name
    )
    sprint_fields: dict[str, object] = {
        "id": "SPRINT",
        "slug": "task",
        "title": "Sprint",
        "kind": "master",
        "repo": repo,
        "createdAt": "2026-07-07T09:00",
        "orchestrates": orchestrates,
        "integrationBranch": "super",
    }
    sprint_fields["executionGraph"] = {
        "nodes": [{"repository": repo, "path": f"{item}/task.json"} for item in orchestrates],
        "edges": [],
    }
    write_task_doc(
        sprint_root,
        TaskDocument.model_validate(sprint_fields),
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


def _base_fixture(root: Path, *, execution_graph: bool = True):
    """Code repo + memory layer + ``.codex/mcp`` settings for the simple tools."""
    repo = root / "workspace" / REPO
    memory = root / "ar-coordination" / "memory-repos" / f"ar-{REPO}"
    (memory / "system").mkdir(parents=True, exist_ok=True)
    (memory / "onboarding").mkdir(parents=True, exist_ok=True)
    (memory / "system" / "settings.md").write_text("# Settings\n", encoding="utf-8")
    _write_leaf_task(root / "ar-coordination", execution_graph=execution_graph)
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, ["init", "-b", "main"])
    _run_git(repo, ["config", "user.email", "agents-remember@example.invalid"])
    _run_git(repo, ["config", "user.name", "Agents Remember"])
    (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
    _run_git(repo, ["add", "README.md"])
    _run_git(repo, ["commit", "-m", "init"])
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _run_git(repo, ["update-ref", "refs/remotes/origin/main", head])
    _run_git(repo, ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"])
    if execution_graph:
        _run_git(repo, ["branch", "super", head])
    path = root / ".codex" / "mcp" / "settings.json"
    _write_json(path, settings_payload(root))
    return load_config(path)


def _simple_payloads(config) -> dict[str, dict]:
    """Tools whose real ``*_payload`` builder runs against the base fixture."""
    leaf_ref = TaskDocumentRef(repository=REPO, path="master/leaf-1.json")
    with mock.patch.object(
        memory_payload_tools,
        "citation_fix_tool",
        return_value={"ok": True, "repoId": REPO, "dryRun": True},
    ):
        citation_fix = tools.citation_fix_payload(
            config,
            REPO,
            contract_path="/fixture/leaf-contract.md",
            operation_scope=CitationOperationScope(),
            dry_run=True,
        )
    with mock.patch(
        "agents_remember.application.closeout_queue.resolve_ambient_seat",
        return_value=SimpleNamespace(
            binding_role="orchestrator",
            binding_task_document_ref=TaskDocumentRef(
                repository=REPO,
                path="sprint/task.json",
            ),
        ),
    ):
        closeout_queue = tools.closeout_queue_payload(
            config,
            CloseoutQueueRequest(
                action="status",
                sprint_task_document_ref=TaskDocumentRef(
                    repository=REPO,
                    path="sprint/task.json",
                ),
            ),
        )
    return {
        "ping": tools.ping_payload(),
        "server_info": tools.server_info_payload(
            config,
            ServingBuild(
                version="test",
                commit="abc1234",
                booted_at="2026-08-25T00:00:00Z",
                source_digest="sha256:" + "a" * 64,
                python_executable="/runtime/bin/python",
                package_root="/runtime/agents_remember",
            ).payload(),
        ),
        "closeout_door": tools.closeout_door_payload(
            config,
            CloseoutDoorRequest(
                action="status",
                contract_path="/missing/series-contract.md",
            ),
        ),
        "curator_coherence": tools.curator_coherence_payload(
            config,
            CuratorCoherenceRequest(
                action="status",
                contract_path="/missing/series-contract.md",
            ),
        ),
        "context_packet": tools.context_packet_payload(config, REPO),
        "read_ar_files": tools.read_ar_files_payload(
            config, REPO, [{"path": "README.md", "source": "full"}]
        ),
        "closeout_queue": closeout_queue,
        "attach_terminal_session_to_task": tools.attach_terminal_session_to_task_payload(
            config,
            session_id="missing-session",
            task_document_ref=TaskDocumentRef(
                repository=REPO,
                path="master/leaf-1.json",
            ),
        ),
        # Representative refusal payload: a legacy caller-supplied harness short-circuits before any
        # tmux spawn, so the conformance fixture never touches a real terminal host.
        "spawn_agent_session": tools.spawn_agent_session_payload(
            config,
            retired=RetiredSpawnInputs(harness="definitely-not-a-real-harness"),
        ),
        "hosted_session_readiness": tools.hosted_session_readiness_payload(
            config,
            session_id="missing-session",
        ),
        # Representative refusal payloads: neither session id has a catalog row, so both
        # short-circuit before touching a real tmux host.
        "session_retire": tools.session_retire_payload(
            config,
            actor_session_id="missing-actor",
            session_id="missing-session",
        ),
        "session_rename": tools.session_rename_payload(
            config,
            session_id="missing-session",
            label="New Label",
        ),
        "dispatch_agent": tools.dispatch_agent_payload(
            config,
            DispatchAgentRequest(leaf_ref, "worker", "Implement the leaf."),
            environ={},
        ),
        "retire_child": tools.retire_child_payload(
            config,
            RetireChildRequest(leaf_ref, "worker", "done"),
            environ={},
        ),
        "rename_child": tools.rename_child_payload(
            config,
            RenameChildRequest(leaf_ref, "worker", "Worker"),
            environ={},
        ),
        "rename_self": tools.rename_self_payload(config, label="Seat", environ={}),
        "message_parent": tools.message_parent_payload(
            config,
            StructuralMessageRequest("Review the report.", "The report is durable."),
            environ={},
        ),
        "message_child": tools.message_child_payload(
            config,
            StructuralMessageRequest(
                "Continue.",
                "Address the review finding.",
                task_document_ref=leaf_ref,
                role="worker",
            ),
            environ={},
        ),
        "runtime_install": tools.runtime_install_payload(config, install_provider_deps=False),
        "resolve_context": tools.resolve_context_payload(config, TaskRef(repo_id=REPO)),
        "drift_check": tools.drift_check_payload(config, REPO),
        "memory_quality_check": tools.memory_quality_check_payload(
            config,
            MemoryQualitySyncRequest(mode="sync", repo_id=REPO),
        ),
        "citation_fix": citation_fix,
        "route_index_refresh": tools.route_index_refresh_payload(config, REPO, dry_run=True),
        "memory_init": tools.memory_init_payload(config, REPO),
        "skills_install": tools.skills_install_payload(config),
        "provider_status": tools.provider_status_payload(config),
        "provider_diagnostics": tools.provider_diagnostics_payload(config),
        "provider_watchers": tools.provider_watchers_payload(config, action="status"),
        "grepai_search": tools.grepai_search_payload(
            config, GrepaiSearchQuery(query="query"), scope=DRY_RUN_SCOPE
        ),
        "grepai_trace": tools.grepai_trace_payload(
            config, GrepaiTraceQuery(trace_action="graph", symbol="sym"), scope=DRY_RUN_SCOPE
        ),
        "cgc_symbol_search": tools.cgc_symbol_search_payload(
            config, REPO, "sym", scope=DRY_RUN_SCOPE
        ),
        "cgc_callers": tools.cgc_callers_payload(config, REPO, "fn", scope=DRY_RUN_SCOPE),
        "cgc_callees": tools.cgc_callees_payload(config, REPO, "fn", scope=DRY_RUN_SCOPE),
        "cgc_dependencies": tools.cgc_dependencies_payload(
            config, REPO, "mod", scope=DRY_RUN_SCOPE
        ),
        "cgc_complexity": tools.cgc_complexity_payload(config, REPO, scope=DRY_RUN_SCOPE),
        "cgc_visualize": tools.cgc_visualize_payload(config, REPO, scope=DRY_RUN_SCOPE),
        "memory_baseline_status": tools.memory_baseline_status_payload(config, REPO),
        "memory_baseline_adopt": tools.memory_baseline_adopt_payload(config, REPO),
        "codex_benchmark_prepare": tools.codex_benchmark_prepare_payload(config),
        "codex_benchmark_run": tools.codex_benchmark_run_payload(config),
    }


def _worktree_lifecycle_fixture(root: Path, *, task_name: str, worktree_name: str):
    """Create one isolated, addressable worktree lifecycle fixture."""

    config = _base_fixture(root, execution_graph=False)
    settings = json.loads(config.config_path.read_text())
    settings["repositories"][REPO]["certificationProfile"] = (
        AGENTS_REMEMBER_PROFILE_REFERENCE.as_posix()
    )
    _write_json(config.config_path, settings)
    config = load_config(config.config_path)
    code_root = config.workspace_root / REPO
    install_fixture_profile(code_root, REPO, NODE_FIXTURE)
    _run_git(code_root, ["add", "-A"])
    _run_git(code_root, ["commit", "-m", "declare certification profile before branch cuts"])
    # worktree_start needs a memory git repo to exist even when memory is disabled.
    tools.memory_init_payload(config, REPO, dry_run=False, initialize_git=True)
    memory_root = root / "ar-coordination" / "memory-repos" / f"ar-{REPO}"
    _run_git(memory_root, ["config", "user.email", "agents-remember@example.invalid"])
    _run_git(memory_root, ["config", "user.name", "Agents Remember"])
    _run_git(memory_root, ["add", "-A"])
    _run_git(memory_root, ["commit", "-m", "seed memory content"])
    memory_content = git(memory_root, "rev-parse", "HEAD")
    code_head = git(config.workspace_root / REPO, "rev-parse", "main")
    _run_git(config.workspace_root / REPO, ["branch", "super", code_head])
    write_ledger(
        memory_root / "memory.md",
        create_initial_ledger(REPO, code_head, memory_content),
    )
    _run_git(memory_root, ["add", "memory.md"])
    _run_git(memory_root, ["commit", "-m", "seed memory ledger"])
    _run_git(memory_root, ["branch", "super", "HEAD"])
    _write_leaf_task(
        config.coordination_root,
        master=task_name,
        doc_id=worktree_name,
        execution_graph=False,
    )
    return config, code_head


def _worktree_payloads(root: Path) -> dict[str, dict]:
    """Drive isolated real worktree lifecycles and capture every step."""

    config, code_head = _worktree_lifecycle_fixture(
        root / "main",
        task_name="demo-task",
        worktree_name="demo-wt",
    )
    abandon_config, _ = _worktree_lifecycle_fixture(
        root / "abandon",
        task_name="abandon-task",
        worktree_name="abandon-wt",
    )
    _write_leaf_task(
        config.coordination_root,
        master="adoption-task",
        doc_id="adoption-wt",
        execution_graph=False,
    )
    sprint_root = config.coordination_root / "tasks" / REPO / "lifecycle-fixture-sprint"
    write_task_doc(
        sprint_root,
        TaskDocument.model_validate(
            {
                "id": "LIFECYCLE-FIXTURE-SPRINT",
                "slug": "lifecycle-fixture-sprint",
                "title": "Lifecycle fixture sprint",
                "kind": "master",
                "status": "inProgress",
                "repo": REPO,
                "createdAt": "2026-08-22T00:00:00+00:00",
                "orchestrates": ["demo-task"],
                "integrationBranch": "super",
                "executionGraph": {
                    "nodes": [{"repository": REPO, "path": "demo-task/task.json"}],
                    "edges": [],
                },
            }
        ),
    )

    payloads: dict[str, dict] = {}
    adoption_contract = default_contract(
        ContractTask(
            name="adoption-task",
            repo_name=REPO,
            coordination_root=config.coordination_root,
            workflow_kind="light-task",
            memory_mode="disabled",
        ),
        leaf=LeafIdentity(worktree_name="adoption-wt", leaf_id="adoption-wt"),
        code=RepoBranchPlan(
            repo_path=config.workspace_root / REPO,
            source_branch="main",
            work_branch="ar/adoption-wt",
            base_commit=code_head,
        ),
    )
    write_contract(adoption_contract.contract_path, adoption_contract)
    payloads["worktree_enclosure_adopt"] = tools.worktree_enclosure_adopt_payload(
        config,
        EnclosureAdoptionRequest(
            contract_path=adoption_contract.contract_path.as_posix(),
            expected_worktree_group=adoption_contract.worktree_group.as_posix(),
            rationale="representative explicit enclosure adoption preview",
        ),
    )
    payloads["worktree_legacy_operation"] = tools.worktree_legacy_operation_payload(
        config,
        adoption_contract.contract_path.as_posix(),
        LegacyOperationRequest(operation_kind="closeout", action="inspect"),
    )
    abandon_start = tools.worktree_start_payload(
        abandon_config,
        TaskIdentity(repo_id=REPO, task_name="abandon-task", worktree_name="abandon-wt"),
        bases=EXTERNAL_MEMORY_BASES,
        execution=StartExecution(skip_provider_setup=True),
    )
    assert abandon_start.get("contract_path"), abandon_start
    payloads["worktree_abandon"] = tools.worktree_abandon_payload(
        abandon_config, abandon_start["contract_path"], dry_run=False, force=True
    )
    payloads["worktree_start"] = tools.worktree_start_payload(
        config,
        TaskIdentity(repo_id=REPO, task_name="demo-task", worktree_name="demo-wt"),
        bases=EXTERNAL_MEMORY_BASES,
        execution=StartExecution(skip_provider_setup=True),
    )
    assert payloads["worktree_start"].get("contract_path"), payloads["worktree_start"]
    contract_path = payloads["worktree_start"]["contract_path"]
    contract = load_contract(Path(contract_path))
    contract = declare_selected_candidate(contract, config_path=config.config_path)
    payloads["worktree_status"] = tools.worktree_status_payload(
        config, TaskRef(repo_id=REPO, contract_path=contract_path)
    )
    payloads["worktree_attach"] = tools.worktree_attach_payload(
        config, TaskRef(repo_id=REPO, contract_path=contract_path)
    )
    payloads["worktree_sync"] = tools.worktree_sync_payload(config, contract_path, dry_run=True)
    payloads["worktree_closeout_preview"] = tools.worktree_closeout_preview_payload(
        config,
        contract_path,
        CloseoutCommitMessages(
            code="code commit message",
            memory="memory commit message",
            ledger="ledger commit message",
        ),
    )
    # Rebuild from the current sources before claiming the prepared fixture door.
    assert contract.closeout_door is not None
    refresh_closeout_projection(
        config.coordination_root, contract.closeout_door.sprintTaskDocumentRef
    )
    with (
        mock.patch(
            "agents_remember.worktrees.integration.lifecycle.lifecycle_operations."
            "launch_detached_worker"
        ),
    ):
        payloads["worktree_closeout_apply"] = tools.worktree_closeout_apply_payload(
            config,
            contract_path,
            CloseoutCommitMessages(
                code="code commit message",
                memory="memory commit message",
                ledger="ledger commit message",
            ),
            CloseoutApproval(intent_note="intent note"),
        )
    assert payloads["worktree_closeout_apply"]["ok"] is True, json.dumps(
        payloads["worktree_closeout_apply"], sort_keys=True
    )
    # Capture the real queued generation; this schema suite does not execute gates.
    payloads["worktree_status_wait"] = tools.worktree_status_wait_payload(
        config,
        LifecycleStatusWaitRequest(
            contract_path=contract_path,
            operation_kind="closeout",
            expected_generation=1,
            after_revision=1,
            timeout_seconds=0.0,
        ),
    )
    assert payloads["worktree_status_wait"]["ok"] is True, json.dumps(
        payloads["worktree_status_wait"], sort_keys=True
    )
    payloads["worktree_operation_control"] = tools.worktree_operation_control_payload(
        config,
        OperationControlRequest(
            contract_path=contract_path,
            operation_kind="closeout",
            action="cancel",
            expected_generation=1,
            intent_note="preview cancellation of queued operation",
            dry_run=True,
        ),
    )
    payloads.update(_landed_worktree_payloads(root / "landed"))
    # Representative direct-landing payload: the real builder refuses cleanly on the
    # fail-closed policy gate (directExecutionEnabled defaults off), which still exercises
    # the full payload → application path and validates the response model.
    payloads["direct_landing"] = tools.direct_landing_payload(
        config,
        DirectLandingRequest(
            contract_path="/fixture/series-contract.md",
            code_commit="a" * 40,
            memory_commit_message="direct memory content",
            ledger_commit_message="direct ledger mapping",
            intent_note="representative landing preview",
            dry_run=True,
        ),
    )
    return payloads


def _landed_worktree_payloads(root: Path) -> dict[str, dict]:
    """Exercise landed payloads from real Git outputs, without claiming gate acceptance."""
    integrated_root = root / "integrated"
    contract = integrated_external_contract_fixture(
        integrated_root, lifecycle_id="LC-CONFORMANCE-LANDED"
    )
    settings = settings_payload(integrated_root)
    settings["workspaceRoot"] = str(integrated_root)
    settings["repositories"] = {contract.repo_name: {}}
    config_path = integrated_root / "settings.json"
    _write_json(config_path, settings)
    config = load_config(config_path)
    contract_path = str(contract.contract_path)
    payloads = {
        "worktree_integrate": tools.worktree_integrate_payload(config, contract_path, dry_run=True),
        "worktree_cleanup": tools.worktree_cleanup_payload(config, contract_path, dry_run=False),
    }
    assert payloads["worktree_integrate"]["ok"] is True, json.dumps(
        payloads["worktree_integrate"], sort_keys=True
    )
    assert payloads["worktree_cleanup"]["ok"] is True, json.dumps(
        payloads["worktree_cleanup"], sort_keys=True
    )
    assert not contract.code_worktree.exists()
    assert contract.memory_worktree is not None and not contract.memory_worktree.exists()
    completed_root = root / "completed"
    completed = _completed_leaf_contract(completed_root)
    _leaf_doc(completed.task_root)
    _master_doc(completed.task_root)
    completed_config = _runtime_config(completed_root, completed)
    payloads["lifecycle_finalize_task"] = tools.lifecycle_finalize_task_payload(
        completed_config, str(completed.contract_path), dry_run=True
    )
    payloads["task_reopen"] = tools.task_reopen_payload(
        completed_config, str(completed.contract_path), dry_run=False
    )
    assert payloads["task_reopen"]["ok"] is True, json.dumps(
        payloads["task_reopen"], sort_keys=True
    )
    return payloads


def _carryover_payloads(root: Path) -> dict[str, dict]:
    """Landed-branch fixture for the c-11-memory-carryover-from-branch skill carryover tools."""
    code_repo = root / "repo-a"
    old_base = init_repo(code_repo, "main")
    git(code_repo, "checkout", "-b", "workbench/reado/v1.2")
    source_head = commit_file(
        code_repo, "feature.py", "def feature():\n    return 'landed'\n", "Add feature"
    )
    git(code_repo, "checkout", "main")
    git(code_repo, "merge", "--ff-only", "workbench/reado/v1.2")

    official_memory = root / "ar-coordination" / "memory-repos" / "ar-repo-a"
    initialized_memory_repo(official_memory, "repo-a", "main", "main", old_base)
    source_memory = root / "ar-coordination" / "memory-source-branch" / "ar-repo-a"
    write_file_onboarding(source_memory / "onboarding", "repo-a", "feature.py", source_head)
    onboarding_file = source_memory / "onboarding" / "feature.py.md"
    onboarding_file.write_text(
        onboarding_file.read_text(encoding="utf-8") + "Branch-learned behavior.\n",
        encoding="utf-8",
    )

    settings = settings_payload(root)
    settings["workspaceRoot"] = str(root)
    settings["repositories"] = {"repo-a": {}}
    path = root / ".codex" / "mcp" / "settings.json"
    _write_json(path, settings)
    config = load_config(path)
    contract = default_contract(
        ContractTask(
            name="carryover-recovery",
            repo_name="repo-a",
            coordination_root=root / "ar-coordination",
            workflow_kind="light-task",
            memory_mode="external",
        ),
        leaf=LeafIdentity(worktree_name="carryover-recovery", leaf_id="CARRYOVER-RECOVERY"),
        code=RepoBranchPlan(
            repo_path=code_repo,
            source_branch="main",
            work_branch="carryover-recovery",
            base_commit=source_head,
        ),
        memory=RepoBranchPlan(
            repo_path=official_memory,
            source_branch="main",
            work_branch="carryover-recovery",
            base_commit=git(official_memory, "rev-parse", "main"),
        ),
    )
    git(
        code_repo,
        "worktree",
        "add",
        "-b",
        contract.code_work_branch,
        str(contract.code_worktree),
        "main",
    )
    assert contract.memory_worktree is not None
    git(
        official_memory,
        "worktree",
        "add",
        "-b",
        contract.memory_work_branch,
        str(contract.memory_worktree),
        "main",
    )
    write_contract(contract.contract_path, contract)
    source = source_memory.as_posix()
    return {
        "memory_carryover_plan": tools.memory_carryover_plan_payload(
            config, _carryover_selection(source, old_base, contract.contract_path)
        ),
        "memory_carryover_apply": tools.memory_carryover_apply_payload(
            config,
            _carryover_selection(source, old_base, contract.contract_path),
            intent_note="intent note",
        ),
    }


def _carryover_selection(source: str, old_base: str, contract_path: Path) -> CarryoverSelection:
    return CarryoverSelection(
        repo_id="repo-a",
        contract_path=contract_path.as_posix(),
        source_memory=source,
        official_code_ref="main",
        source_code_ref="workbench/reado/v1.2",
        old_base=old_base,
    )


def _stale_agent_notifier(observer_root: Path) -> None:
    """Tick the agent-notifier heartbeat into the past so every capture carries the banner.

    Without this, these fixtures were a workspace whose agent-notifier had NEVER ticked --
    deliberately silent (see ``agent_notifier_staleness_banner``) -- so ``agentNotifierBanner``
    never fired and this suite validated the one shape the choke point cannot break.
    A ticked-then-quiet row is the mutation point: it is the state in which the choke
    point adds a key, so it is the state the contract has to be checked in.
    """
    AgentNotifierHeartbeatStore(observer_root).tick(now=datetime.now(UTC) - timedelta(hours=6))


def _lifecycle_payloads(root: Path) -> dict[str, dict]:
    """Drive the ambient lifecycle through each signal, capturing every payload.

    The signals require an installed ambient; a long heartbeat keeps the capture
    deterministic, and the ambient is reset afterward so other suites see none.
    ``lifecycle_block`` stays here as lower-level compatibility coverage; it is
    not an advertised public MCP tool.
    """
    observer_root = root / "logs" / "observer"
    _stale_agent_notifier(observer_root)
    install_ambient(
        AmbientLifecycle(EventStore(observer_root), timing=AmbientTiming(heartbeat_seconds=3600))
    )
    try:
        return {
            "lifecycle_start": tools.lifecycle_start_payload(),
            "lifecycle_phase": tools.lifecycle_phase_payload("build"),
            "lifecycle_block": tools.lifecycle_block_payload(
                kind="decision", prompt="ok?", options=["a", "b"]
            ),
            "lifecycle_resume": tools.lifecycle_resume_payload(),
            "lifecycle_end": tools.lifecycle_end_payload("completed"),
            "switch_lifecycle": tools.switch_lifecycle_payload(),
            # Captured last: switch_lifecycle leaves a fresh running lifecycle, the
            # only state await_developer accepts. The notification does not self-dismiss
            # (the choke point guards on its name), so its own payload reports
            # awaiting-developer.
            "lifecycle_turn_end_notification": tools.lifecycle_turn_end_notification_payload(
                "Turn complete; your move."
            ),
        }
    finally:
        reset_ambient()


def _task_doc_payloads(root: Path) -> dict[str, dict]:
    """Author a representative task document (JSON-primary; markdown rendered)."""
    config = _base_fixture(root)
    return {
        "task_doc": tools.task_doc_payload(
            config,
            TaskDocTarget(repo_id=REPO, task_name="demo-task"),
            operation="create",
            edit=TaskDocEdit(
                fields={
                    "id": "DEMO",
                    "slug": "task",
                    "title": "Demo",
                    "kind": "master",
                    "repo": REPO,
                    "type": "Code",
                    "createdAt": "2026-01-01T00:00",
                    "objective": "demo",
                }
            ),
        )
    }


def _gate_payloads(config) -> dict[str, dict]:
    """Control-plane gate substrate, including lower-level compatibility builders."""
    observer_root = config.coordination_root / "logs" / "observer"
    _stale_agent_notifier(observer_root)
    install_ambient(
        AmbientLifecycle(
            EventStore(observer_root),
            timing=AmbientTiming(heartbeat_seconds=3600),
        )
    )
    try:
        started = tools.lifecycle_start_payload()

        def approve_lifecycle_gate(_seconds: float) -> None:
            open_gates = [
                gate
                for gate in tools.gate_list_payload(config, lifecycle_id=started["lifecycleId"])[
                    "gates"
                ]
                if gate["state"] == "open"
            ]
            tools.gate_decide_payload(
                config,
                gate_id=open_gates[0]["id"],
                lifecycle_id=started["lifecycleId"],
                verdict=GateVerdict(decision="approve", by="developer", via="dashboard"),
            )

        lifecycle_gate = tools.lifecycle_gate_payload(
            config,
            GateRaise(
                kind="agent-question",
                request=GateRequest(packet={"summary": "demo gate"}),
                ask={"kind": "question", "prompt": "Continue?", "options": ["yes", "no"]},
            ),
            wait=GateWait(timeout_seconds=None, sleep=approve_lifecycle_gate),
        )
    finally:
        reset_ambient()

    created = tools.gate_create_payload(
        config,
        kind="closeout-approval",
        anchor=GateAnchor(lifecycle_id="gate-demo"),
    )
    gate_id = created["gateId"]
    internal_gate_decide = tools.gate_decide_payload(
        config,
        gate_id=gate_id,
        lifecycle_id="gate-demo",
        verdict=GateVerdict(decision="approve", by="developer", via="dashboard"),
    )
    internal_gate_list = tools.gate_list_payload(config, lifecycle_id="gate-demo")
    task_ref = TaskDocumentRef(repository=REPO, path="master/leaf-1.json")
    return {
        # Public structural gate fixtures deliberately exercise the fail-closed ambient-seat
        # boundary. Internal exact-id builders retain their own representative records below.
        "lifecycle_gate": tools.structural_lifecycle_gate_payload(
            config,
            StructuralLifecycleGateRequest(kind="agent-question"),
            environ={},
        ),
        "lifecycle_gate_internal": lifecycle_gate,
        "gate_create": created,
        "gate_decide": tools.structural_gate_decide_payload(
            config,
            StructuralGateDecisionRequest(
                task_document_ref=task_ref,
                kind="closeout-approval",
                decision="approve",
            ),
            environ={},
        ),
        "gate_decide_internal": internal_gate_decide,
        "gate_wait": tools.gate_wait_payload(
            config,
            gate_id=gate_id,
            lifecycle_id="gate-demo",
            wait=GateWait(timeout_seconds=30.0, poll_seconds=1.0, sleep=lambda _s: None),
        ),
        "gate_response_wait": tools.gate_response_wait_payload(
            config,
            gate_id=gate_id,
            lifecycle_id="gate-demo",
            wait=GateWait(sleep=lambda _s: None),
        ),
        "gate_list": tools.structural_gate_list_payload(config, environ={}),
        "gate_list_internal": internal_gate_list,
    }


def _operator_inbox_payloads(config) -> dict[str, dict]:
    """External-chat inbox substrate: post, poll, then consume one entry."""
    posted = tools.operator_inbox_post_payload(
        config,
        address=InboxAddress(lifecycle_id="inbox-demo", agent_id="agent-a"),
        message=InboxMessage(ask="Continue?", response="Yes, proceed.", gate_id="gate-demo"),
        poster=InboxPoster(created_by="developer", created_via="dashboard"),
    )
    return {
        "operator_inbox_post": posted,
        "operator_inbox_poll": tools.operator_inbox_poll_payload(
            config,
            lifecycle_id=None,
            agent_id="agent-a",
        ),
        "operator_inbox_consume": tools.operator_inbox_consume_payload(
            config,
            entry_id=posted["entryId"],
            consumed_by="model",
            consumed_via="cli",
        ),
        "operator_inbox_supersede": tools.operator_inbox_supersede_payload(
            config,
            entry_id=posted["entryId"],
            reason="overtaken",
            superseded_by="developer",
        ),
    }


def _orchestration_payloads(config) -> dict[str, dict]:
    return {
        "orchestration_nudge_manager": tools.orchestration_nudge_manager_payload(
            config,
            reason="missing-turn-report",
            target=NudgeTarget(agent_id="manager-a"),
            subject=NudgeSubject(
                subject="worker 260703-L3",
                agent_id="worker-a",
                artifact_path="notes/reports/260703-L3-worker-report.md",
            ),
        )
    }


def _allowed_keys(model) -> set[str]:
    """Serialized keys the model is allowed to emit (field names plus aliases)."""
    allowed: set[str] = set()
    for name, info in model.model_fields.items():
        allowed.add(name)
        if info.alias:
            allowed.add(info.alias)
        serialization_alias = getattr(info, "serialization_alias", None)
        if serialization_alias:
            allowed.add(serialization_alias)
    return allowed


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested for item in value.values() for nested in _recursive_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _recursive_keys(item)}
    return set()


@pytest.mark.integration
class ToolPayloadIntegrationTests(unittest.TestCase):
    payloads: dict[str, dict]
    _temp_dirs: list[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls._temp_dirs = [tempfile.mkdtemp() for _ in range(8)]
        for directory in cls._temp_dirs:
            cls.addClassCleanup(shutil.rmtree, directory, ignore_errors=True)
        bind_worktree_services(build_default_worktree_services())
        try:
            (
                base,
                worktree,
                carryover,
                lifecycle,
                task_doc_root,
                gate_root,
                inbox_root,
                orch_root,
            ) = (Path(d) for d in cls._temp_dirs)
            cls.payloads = {}
            cls.payloads.update(_simple_payloads(_base_fixture(base)))
            cls.payloads.update(_worktree_payloads(worktree))
            cls.payloads.update(_carryover_payloads(carryover))
            cls.payloads.update(_lifecycle_payloads(lifecycle))
            cls.payloads.update(_task_doc_payloads(task_doc_root))
            cls.payloads.update(_gate_payloads(_base_fixture(gate_root)))
            cls.payloads.update(_operator_inbox_payloads(_base_fixture(inbox_root)))
            cls.payloads.update(_orchestration_payloads(_base_fixture(orch_root)))
        finally:
            reset_worktree_services()

    def test_every_modeled_tool_has_a_representative_payload(self) -> None:
        self.assertEqual(set(self.payloads), set(TOOL_RESPONSE_MODELS))

    def test_the_choke_point_injections_are_actually_exercised(self) -> None:
        # This suite sits exactly at the mutation point -- ``_tool_payload`` is where the
        # two envelope-wide keys get set -- but it can only catch drift in them if the
        # captures were taken in a state where they FIRE. They are captured in an active
        # lifecycle (``nextStep``) whose agent-notifier ticked and then went quiet
        # (``agentNotifierBanner``, plus its legacy ``supervisorBanner`` alias); assert both,
        # so a fixture that quietly stops producing
        # them is a failure here rather than a silent hole in every assertion below.
        with_next_step = {name for name, body in self.payloads.items() if "nextStep" in body}
        with_banner = {
            name
            for name, body in self.payloads.items()
            if "agentNotifierBanner" in body
            and body["supervisorBanner"] == body["agentNotifierBanner"]
        }
        self.assertIn("lifecycle_start", with_next_step)
        self.assertIn("lifecycle_start", with_banner)
        self.assertIn("lifecycle_gate_internal", with_banner)

    def test_representative_payloads_conform_to_registered_models(self) -> None:
        for tool_name, model in TOOL_RESPONSE_MODELS.items():
            with self.subTest(tool=tool_name):
                payload = self.payloads[tool_name]
                # (a) The representative payload validates against the model.
                model.model_validate(payload)
                # (b) Round-tripping does not fabricate keys. Strict models may
                # only emit declared fields; intentionally flexible models may
                # also pass through keys that were present on the input payload,
                # so the round trip must not invent keys that are neither
                # declared nor part of the input.
                round_trip = model.model_validate(payload).model_dump(
                    mode="json", exclude_none=True
                )
                allowed = _allowed_keys(model)
                if issubclass(model, FlexibleResponseModel):
                    allowed |= set(payload)
                self.assertLessEqual(
                    set(round_trip),
                    allowed,
                    f"{tool_name} round trip produced undeclared keys: "
                    f"{sorted(set(round_trip) - allowed)}",
                )

    def test_no_public_response_serializes_private_operation_identity_keys(self) -> None:
        prohibited = {"operationKey", "claimedOperationKey", "legacyOperationKey"}
        for tool_name, payload in self.payloads.items():
            with self.subTest(tool=tool_name):
                self.assertFalse(prohibited & _recursive_keys(payload))


class ToolResponseConformanceTests(unittest.TestCase):
    """Direct model checks need no application workspace or service composition."""

    def test_generic_flexible_payload_preserves_lifecycle_named_provider_fields(self) -> None:
        provider_payload = {
            "ok": True,
            "operation": "grepai_search",
            "provider": "grepai",
            "operationKey": "provider-owned-operation-key",
            "claimedOperationKey": {"nested": "provider-owned-claim"},
            "legacyOperationKey": "provider-owned-legacy-key",
        }
        result = _tool_payload("grepai_search", provider_payload)
        for key in ("operationKey", "claimedOperationKey", "legacyOperationKey"):
            self.assertEqual(result[key], provider_payload[key])

    def test_strict_response_models_forbid_extra_fields(self) -> None:
        for tool_name, model in TOOL_RESPONSE_MODELS.items():
            with self.subTest(tool=tool_name):
                # The response-model taxonomy decides strictness: anything not
                # built on FlexibleResponseModel is a strict contract and must
                # keep extra="forbid"; the flexible base must keep extra="allow".
                expected = "allow" if issubclass(model, FlexibleResponseModel) else "forbid"
                self.assertEqual(
                    model.model_config.get("extra"),
                    expected,
                    f"{tool_name} ({model.__name__}) must use extra={expected!r}",
                )

    def test_flexible_response_models_reject_reserved_snake_decision_keys_only(self) -> None:
        accepted = FlexibleResponseModel.model_validate(
            {"providerNative": {"unrelated_extra": True}}
        )
        self.assertEqual(
            accepted.model_dump(mode="json"),
            {"providerNative": {"unrelated_extra": True}},
        )
        for reserved in (
            "developer_decision_required",
            "decision_surface",
        ):
            with (
                self.subTest(key=reserved),
                self.assertRaisesRegex(
                    ValueError,
                    "reserved lifecycle decision keys must use camelCase",
                ),
            ):
                FlexibleResponseModel.model_validate({"providerNative": {reserved: True}})

    def test_completion_cleanup_fields_are_declared_on_both_edge_models(self) -> None:
        expected = {
            "autoClosedSeats",
            "autoCloseDeferredSeats",
            "autoCloseFailedSeats",
            "autoLandedSeats",
        }
        for tool_name in ("worktree_integrate", "lifecycle_finalize_task"):
            with self.subTest(tool=tool_name):
                self.assertLessEqual(expected, set(TOOL_RESPONSE_MODELS[tool_name].model_fields))


if __name__ == "__main__":
    unittest.main()
