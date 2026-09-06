"""Shared exact-authority fixtures for the split L4 forcing suites."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from agents_remember.application.lifecycle import lifecycle_operation_worker
from agents_remember.kernel.memory_ledger import (
    create_initial_ledger,
    load_ledger,
    prepend_mapping,
    write_ledger,
)
from agents_remember.models.lifecycles.door import (
    CloseoutDoorGeneration,
    DoorAdmissionProvenance,
    DoorProvenance,
    DoorSchedulingProvenance,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import (
    SprintExecutionGraph,
    SprintExecutionNode,
    TaskDocument,
    read_task_doc,
    write_task_doc,
)
from agents_remember.worktrees.integration.closeout.task_intent_identity import (
    contract_task_intent,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    publish_new_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.modules.startup import start_contract
from agents_remember.worktrees.worktree_contract import (
    ContractTask,
    LeafIdentity,
    RepoBranchPlan,
    WorktreeContract,
    contract_publication_text,
    default_contract,
    default_series_contract,
    load_contract,
    write_contract,
)
from closeout_input_test_support import (
    closeout_operation_input,
    publish_closeout_finalization,
    start_closeout_operation,
)
from repository_profile_test_support import AGENTS_REMEMBER_PROFILE_REFERENCE
from selected_lifecycle_test_support import selected_closeout_operation_input
from test_source_lineage import _commit_on, _fixture, _git


def _closed_leaf_worktree(
    fixture,
    _root: Path,
    *,
    candidate_commit: bool,
    publish_closeout_evidence: bool = True,
):
    worktree = fixture.leaf_contract.code_worktree
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(fixture.code_repo, "worktree", "add", worktree.as_posix(), "leaf")
    if candidate_commit:
        (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        _git(worktree, "add", "candidate.txt")
        _git(worktree, "commit", "-m", "closed leaf candidate")
    _git(fixture.code_repo, "switch", "ar/master")
    closed = replace(
        fixture.leaf_contract,
        code_worktree=worktree,
        code_source_branch="ar/master",
        code_work_branch="leaf",
        closeout_status="completed",
        approved_for_commit=True,
        human_review_status="approved",
        code_commit=_git(worktree, "rev-parse", "HEAD"),
    )
    write_contract(closed.contract_path, closed)
    if publish_closeout_evidence:
        return _publish_completed_closeout_fixture(fixture, closed)
    fixture.leaf_contract = closed
    return closed


def _closed_external_leaf_worktrees(
    fixture,
    _root: Path,
    *,
    publish_closeout_evidence: bool = True,
):
    memory_repo = fixture.leaf_contract.memory_repo_path
    assert memory_repo is not None
    code_worktree = fixture.leaf_contract.code_worktree
    memory_worktree = fixture.leaf_contract.memory_worktree
    assert memory_worktree is not None
    code_worktree.parent.mkdir(parents=True, exist_ok=True)
    memory_worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(fixture.code_repo, "worktree", "add", code_worktree.as_posix(), "leaf")
    _git(memory_repo, "worktree", "add", memory_worktree.as_posix(), "leaf")
    (memory_worktree / "onboarding").mkdir()
    (code_worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _git(code_worktree, "add", "candidate.txt")
    _git(code_worktree, "commit", "-m", "closed code")
    code_commit = _git(code_worktree, "rev-parse", "HEAD")
    (memory_worktree / "candidate.md").write_text("# Candidate\n", encoding="utf-8")
    _git(memory_worktree, "add", "candidate.md")
    _git(memory_worktree, "commit", "-m", "closed memory")
    memory_commit = _git(memory_worktree, "rev-parse", "HEAD")
    write_ledger(
        memory_worktree / "memory.md",
        prepend_mapping(
            load_ledger(memory_worktree / "memory.md"),
            code_commit,
            memory_commit,
        ),
    )
    _git(memory_worktree, "add", "memory.md")
    _git(memory_worktree, "commit", "-m", "closed ledger")
    ledger_commit = _git(memory_worktree, "rev-parse", "HEAD")
    closed = replace(
        fixture.leaf_contract,
        code_worktree=code_worktree,
        memory_worktree=memory_worktree,
        ledger_path=memory_worktree / "memory.md",
        closeout_status="completed",
        approved_for_commit=True,
        human_review_status="approved",
        code_commit=code_commit,
        memory_content_commit=memory_commit,
        ledger_commit=ledger_commit,
    )
    write_contract(closed.contract_path, closed)
    if publish_closeout_evidence:
        return _publish_completed_closeout_fixture(fixture, closed)
    fixture.leaf_contract = closed
    return closed


def _publish_completed_closeout_fixture(
    fixture, closed: WorktreeContract, *, final_source_branch: str | None = None
) -> WorktreeContract:
    """Attach integration evidence after valid admission, optionally varying its final target."""

    input_factory = (
        selected_closeout_operation_input if closed.kind == "leaf" else closeout_operation_input
    )
    operation_input = input_factory(
        closed,
        config_path=fixture.config_path,
        approval_note="approved closed integration fixture",
    )
    start_closeout_operation(operation_input, launcher=lambda *_: None)
    store = LifecycleOperationStore(operation_record_path(closed.worktree_group, "closeout"))
    runtime = lifecycle_operation_worker.OperationRuntime(store)
    runtime.start()
    finalized = replace(
        load_contract(closed.contract_path),
        code_source_branch=final_source_branch or closed.code_source_branch,
        closeout_status="completed",
        approved_for_commit=True,
        human_review_status="approved",
        code_commit=closed.code_commit,
        memory_content_commit=closed.memory_content_commit,
        ledger_commit=closed.ledger_commit,
    )
    write_contract(finalized.contract_path, finalized)
    publish_closeout_finalization(runtime, finalized)
    runtime.finish({"state": "closed"}, ok=True)
    if finalized.kind == "series":
        fixture.master_contract = finalized
    else:
        fixture.leaf_contract = finalized
    return load_contract(finalized.contract_path)


def _authority_fixture(root: Path, *, external_memory: bool = False) -> Any:
    fixture: Any = _fixture(
        root,
        external_memory=external_memory,
        publish_locations=False,
        selected_profile=True,
    )
    configured_code = root / "repo"
    configured_code.symlink_to(fixture.code_repo, target_is_directory=True)
    memory_mode = "external" if external_memory else "internal"
    if not external_memory:
        (configured_code / "ar-memory").mkdir()
    if fixture.leaf_contract.memory_repo_path is not None:
        configured_memory = fixture.coordination / "memory-repos" / "ar-repo"
        configured_memory.parent.mkdir(parents=True, exist_ok=True)
        configured_memory.symlink_to(
            fixture.leaf_contract.memory_repo_path,
            target_is_directory=True,
        )
    config_path = root / "settings.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "coordinationRoot": fixture.coordination.as_posix(),
                "workspaceRoot": root.as_posix(),
                "repositories": {
                    "repo": {"certificationProfile": AGENTS_REMEMBER_PROFILE_REFERENCE.as_posix()}
                },
            }
        ),
        encoding="utf-8",
    )
    fixture.config_path = config_path
    task_root = fixture.coordination / "tasks" / "repo"
    for repo in filter(None, (fixture.code_repo, fixture.leaf_contract.memory_repo_path)):
        _git(repo, "branch", "ar/atomic-two", "super")
    if external_memory:
        memory_repo = fixture.leaf_contract.memory_repo_path
        assert memory_repo is not None
        code_head = _git(fixture.code_repo, "rev-parse", "ar/master")
        _git(memory_repo, "switch", "ar/master")
        (memory_repo / "base.md").write_text("# Base memory\n", encoding="utf-8")
        _git(memory_repo, "add", "base.md")
        _git(memory_repo, "commit", "-m", "base memory content")
        base_memory = _git(memory_repo, "rev-parse", "ar/master")
        write_ledger(
            memory_repo / "memory.md",
            create_initial_ledger("repo", code_head, base_memory),
        )
        _git(memory_repo, "add", "memory.md")
        _git(memory_repo, "commit", "-m", "base memory ledger")
        _git(memory_repo, "branch", "-f", "leaf", "ar/master")
        _git(memory_repo, "branch", "-f", "super", "ar/master")
        _git(memory_repo, "switch", "super")
    master_contract = replace(
        fixture.master_contract,
        memory_mode=memory_mode,
        code_work_branch="ar/master",
        memory_work_branch=("ar/master" if external_memory else ""),
        memory_base_commit=(
            _git(cast(Path, fixture.leaf_contract.memory_repo_path), "rev-parse", "ar/master")
            if external_memory
            else ""
        ),
    )
    write_contract(master_contract.contract_path, master_contract)
    publish_new_lifecycle_operation_location(
        master_contract,
        contract_text=contract_publication_text(
            master_contract.contract_path,
            master_contract,
        ),
    )
    fixture.master_contract = master_contract
    fixture.leaf_contract = replace(
        fixture.leaf_contract,
        memory_mode=memory_mode,
        code_source_branch="ar/master",
        memory_source_branch=("ar/master" if external_memory else ""),
        memory_base_commit=(
            _git(cast(Path, fixture.leaf_contract.memory_repo_path), "rev-parse", "ar/master")
            if external_memory
            else ""
        ),
    )
    write_contract(fixture.leaf_contract.contract_path, fixture.leaf_contract)
    publish_new_lifecycle_operation_location(
        fixture.leaf_contract,
        contract_text=contract_publication_text(
            fixture.leaf_contract.contract_path,
            fixture.leaf_contract,
        ),
    )
    master_doc = read_task_doc(task_root / "master" / "task.json")
    write_task_doc(
        task_root / "master",
        master_doc.model_copy(update={"executionNature": "atomic"}),
    )
    sprint = read_task_doc(task_root / "sprint" / "task.json")
    master_ref = TaskDocumentRef(repository="repo", path="master/task.json")
    sibling_ref = TaskDocumentRef(repository="repo", path="atomic-two/task.json")
    write_task_doc(
        task_root / "sprint",
        sprint.model_copy(
            update={
                "integrationBranch": "super",
                "orchestrates": ["master", "atomic-two"],
                "executionGraph": SprintExecutionGraph(
                    nodes=[
                        SprintExecutionNode(ref=master_ref),
                        SprintExecutionNode(ref=sibling_ref),
                    ],
                    edges=[],
                ),
            }
        ),
    )
    write_task_doc(
        task_root / "atomic-two",
        _doc(
            id="ATOMIC-TWO",
            slug="atomic-two",
            title="Atomic Two",
            kind="master",
            executionNature="atomic",
        ),
    )
    for repo in filter(None, (fixture.code_repo, fixture.leaf_contract.memory_repo_path)):
        _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "main"))
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    memory_repo = fixture.leaf_contract.memory_repo_path
    sibling = default_series_contract(
        ContractTask(
            "atomic-two",
            "repo",
            fixture.coordination,
            "light-task",
            memory_mode,
        ),
        code=RepoBranchPlan(
            fixture.code_repo,
            "super",
            "ar/atomic-two",
            _git(fixture.code_repo, "rev-parse", "super"),
        ),
        memory=(
            RepoBranchPlan(
                memory_repo,
                "super",
                "ar/atomic-two",
                _git(memory_repo, "rev-parse", "super"),
            )
            if memory_repo is not None
            else None
        ),
        task_root=task_root / "atomic-two",
    )
    write_contract(sibling.contract_path, sibling)
    return fixture


def _doc(**values: object) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "repo": "repo",
            "createdAt": "2026-08-15T00:00:00+00:00",
            **values,
        }
    )


def _add_atomic_master_to_sprint(fixture, task_root: Path) -> None:
    write_task_doc(
        task_root,
        _doc(
            id="ATOMIC-THREE",
            slug="atomic-three",
            title="Atomic Three",
            kind="master",
            executionNature="atomic",
        ),
    )
    sprint_path = fixture.coordination / "tasks" / "repo" / "sprint" / "task.json"
    sprint = read_task_doc(sprint_path)
    atomic_ref = TaskDocumentRef(repository="repo", path="atomic-three/task.json")
    assert sprint.executionGraph is not None
    write_task_doc(
        sprint_path.parent,
        sprint.model_copy(
            update={
                "orchestrates": [*sprint.orchestrates, "atomic-three"],
                "executionGraph": sprint.executionGraph.model_copy(
                    update={
                        "nodes": [*sprint.executionGraph.nodes, SprintExecutionNode(ref=atomic_ref)]
                    }
                ),
            }
        ),
    )


def _complete_atomic_master(fixture) -> None:
    master_path = fixture.coordination / "tasks" / "repo" / "master" / "task.json"
    master = read_task_doc(master_path)
    write_task_doc(
        master_path.parent,
        master.model_copy(
            update={
                "status": "Completed",
                "subTasks": [
                    row.model_copy(update={"status": "Completed"}) for row in master.subTasks
                ],
            }
        ),
    )


def _record_atomic_leaf_landing(
    fixture,
    code_commit: str,
    *,
    memory_content_commit: str = "",
    ledger_commit: str = "",
):
    """Persist the exact child landing facts consumed by the atomic-series seal."""

    landed = replace(
        fixture.leaf_contract,
        closeout_status="completed",
        approved_for_commit=True,
        human_review_status="approved",
        code_commit=code_commit,
        memory_content_commit=memory_content_commit,
        ledger_commit=ledger_commit,
        integration_status="completed",
        integrated_code_commit=code_commit,
        integrated_memory_content_commit=memory_content_commit,
        integrated_ledger_commit=ledger_commit,
    )
    landed = replace(
        landed,
        closeout_door=_claimed_atomic_leaf_door(fixture, landed),
    )
    write_contract(landed.contract_path, landed)
    fixture.leaf_contract = landed
    return landed


def _record_additional_atomic_leaf_landing(
    fixture,
    first: WorktreeContract,
    code_commit: str,
    memory_content_commit: str,
    ledger_commit: str,
):
    """Add one canonical child row and persist its exact sequential landing facts."""

    leaf_id = "LEAF-C"
    series = fixture.master_contract
    memory_repo = series.memory_repo_path
    assert memory_repo is not None
    task = ContractTask(
        series.task_name,
        series.repo_name,
        series.coordination_root,
        series.workflow_kind,
        series.memory_mode,
        parent_task_name=series.parent_task_name,
    )
    leaf = default_contract(
        task,
        leaf=LeafIdentity(worktree_name=leaf_id.lower(), leaf_id=leaf_id),
        code=RepoBranchPlan(
            series.code_repo_path,
            series.code_work_branch,
            f"ar/{leaf_id.lower()}",
            first.integrated_code_commit,
        ),
        memory=RepoBranchPlan(
            memory_repo,
            series.memory_work_branch,
            f"ar/{leaf_id.lower()}",
            first.integrated_ledger_commit,
        ),
    )
    landed = replace(
        leaf,
        parent_task_name=series.task_name,
        parent_contract_path=series.contract_path,
        closeout_status="completed",
        approved_for_commit=True,
        human_review_status="approved",
        code_commit=code_commit,
        memory_content_commit=memory_content_commit,
        ledger_commit=ledger_commit,
        integration_status="completed",
        integrated_code_commit=code_commit,
        integrated_memory_content_commit=memory_content_commit,
        integrated_ledger_commit=ledger_commit,
    )
    first_doc_path = fixture.coordination / "tasks" / "repo" / fixture.leaf_ref.path
    first_doc = read_task_doc(first_doc_path)
    write_task_doc(
        landed.task_root,
        first_doc.model_copy(
            update={
                "id": leaf_id,
                "slug": leaf_id.lower(),
                "title": leaf_id,
            }
        ),
    )
    master = read_task_doc(landed.task_root / "task.json")
    row = master.subTasks[0].model_copy(
        update={
            "number": leaf_id,
            "name": leaf_id,
            "file": f"{leaf_id.lower()}.md",
        }
    )
    write_task_doc(
        landed.task_root,
        master.model_copy(update={"subTasks": [*master.subTasks, row]}),
    )
    landed = replace(
        landed,
        closeout_door=_claimed_atomic_leaf_door(fixture, landed),
    )
    write_contract(landed.contract_path, landed)
    return landed


def _claimed_atomic_leaf_door(fixture, leaf: WorktreeContract) -> CloseoutDoorGeneration:
    """Attach exact task ownership to synthetic already-landed atomic leaf facts."""

    leaf_ref = TaskDocumentRef(
        repository=leaf.repo_name,
        path=f"{leaf.task_name}/{leaf.leaf_id.lower()}.json",
    )
    master_ref = TaskDocumentRef(
        repository=leaf.repo_name,
        path=f"{leaf.task_name}/task.json",
    )
    sprint_ref = TaskDocumentRef(repository=leaf.repo_name, path="sprint/task.json")
    identity = json.dumps(
        {
            "contractPath": leaf.contract_path.as_posix(),
            "candidateTree": _git(
                fixture.code_repo,
                "rev-parse",
                f"{leaf.code_commit}^{{tree}}",
            ),
            "taskDocumentRef": leaf_ref.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    generation = hashlib.sha256(identity).hexdigest()
    not_applicable = DoorProvenance(
        state="not-applicable",
        fingerprint=hashlib.sha256(b"atomic-landed-not-applicable").hexdigest(),
    )
    return CloseoutDoorGeneration(
        generationId=generation,
        disposition="claimed",
        taskId=leaf.task_id,
        taskName=leaf.task_name,
        taskDocumentRef=leaf_ref,
        owningMasterTaskDocumentRef=master_ref,
        sprintTaskDocumentRef=sprint_ref,
        contractPath=leaf.contract_path.as_posix(),
        candidateTree=_git(fixture.code_repo, "rev-parse", f"{leaf.code_commit}^{{tree}}"),
        memoryCandidateTree=(
            _git(leaf.memory_repo_path, "rev-parse", f"{leaf.ledger_commit}^{{tree}}")
            if leaf.memory_repo_path is not None and leaf.ledger_commit
            else ""
        ),
        codeBaseCommit=leaf.code_base_commit,
        memoryBaseCommit=leaf.memory_base_commit,
        ledgerMemoryCommit=leaf.memory_base_commit,
        taskTopologyFingerprint=hashlib.sha256(b"atomic-landed-topology").hexdigest(),
        taskIntent=contract_task_intent(leaf, candidate_ref=leaf_ref),
        reviewProvenance=not_applicable,
        memoryProvenance=not_applicable,
        ledgerProvenance=not_applicable,
        admissionProvenance=DoorAdmissionProvenance(
            fingerprint=hashlib.sha256(b"atomic-landed-admission").hexdigest(),
        ),
        schedulingProvenance=DoorSchedulingProvenance(
            priority="normal",
            judgmentId="ATOMIC-LANDING-FIXTURE",
            fingerprint=hashlib.sha256(b"atomic-landed-scheduling").hexdigest(),
        ),
        declaredBy="test-fixture:atomic-landing",
        declaredAt="2026-08-22T00:00:00+00:00",
        operationKind="direct-landing",
        operationFingerprint=hashlib.sha256(identity + b"fingerprint").hexdigest(),
        claimedOperationKey=hashlib.sha256(identity + b"operation").hexdigest(),
    )


def _land_two_external_atomic_leaves(
    fixture,
) -> tuple[WorktreeContract, WorktreeContract]:
    """Build the exact two-leaf code+memory chain consumed by series integration."""

    memory_repo = fixture.master_contract.memory_repo_path
    assert memory_repo is not None
    _commit_on(fixture.code_repo, "ar/master", "atomic-code-one.txt")
    first_code = _git(fixture.code_repo, "rev-parse", "ar/master")
    _commit_on(memory_repo, "ar/master", "atomic-memory-one.md")
    first_memory = _git(memory_repo, "rev-parse", "ar/master")
    write_ledger(
        memory_repo / "memory.md",
        prepend_mapping(
            load_ledger(memory_repo / "memory.md"),
            first_code,
            first_memory,
        ),
    )
    _git(memory_repo, "add", "memory.md")
    _git(memory_repo, "commit", "-m", "Record first atomic ledger")
    first_ledger = _git(memory_repo, "rev-parse", "ar/master")
    first = _record_atomic_leaf_landing(
        fixture,
        first_code,
        memory_content_commit=first_memory,
        ledger_commit=first_ledger,
    )

    _commit_on(fixture.code_repo, "ar/master", "atomic-code-two.txt")
    second_code = _git(fixture.code_repo, "rev-parse", "ar/master")
    _commit_on(memory_repo, "ar/master", "atomic-memory-two.md")
    second_memory = _git(memory_repo, "rev-parse", "ar/master")
    write_ledger(
        memory_repo / "memory.md",
        prepend_mapping(
            load_ledger(memory_repo / "memory.md"),
            second_code,
            second_memory,
        ),
    )
    _git(memory_repo, "add", "memory.md")
    _git(memory_repo, "commit", "-m", "Record second atomic ledger")
    second_ledger = _git(memory_repo, "rev-parse", "ar/master")
    second = _record_additional_atomic_leaf_landing(
        fixture,
        first,
        code_commit=second_code,
        memory_content_commit=second_memory,
        ledger_commit=second_ledger,
    )
    _complete_atomic_master(fixture)
    return first, second


def _atomic_three_spec(fixture, task_root: Path) -> start_contract.MasterSeriesContractSpec:
    return start_contract.MasterSeriesContractSpec(
        coordination_root=fixture.coordination,
        repo_name="repo",
        code_repo=fixture.code_repo,
        memory_root=None,
        task_root=task_root,
        task_name="atomic-three",
        parent_task_name="sprint",
        protected_branch="super",
    )


def _assert_exact_series_preview(
    test: unittest.TestCase,
    preview: WorktreeCommandResult,
) -> None:
    changed_code_paths = preview.payload["changed_code_paths"]
    proposed = preview.payload["proposed_commits"]
    assert isinstance(changed_code_paths, dict)
    assert isinstance(proposed, dict)
    code_proposed = proposed["code"]
    memory_proposed = proposed["memory"]
    ledger_proposed = proposed["ledger"]
    assert isinstance(code_proposed, dict)
    assert isinstance(memory_proposed, dict)
    assert isinstance(ledger_proposed, dict)
    test.assertEqual(changed_code_paths["count"], 0)
    test.assertEqual(
        (code_proposed["would_commit"], code_proposed["ref"], "worktree" in code_proposed),
        (False, "refs/heads/ar/master", False),
    )
    test.assertEqual(
        (
            memory_proposed["would_commit"],
            memory_proposed["ref"],
            "worktree" in memory_proposed,
        ),
        (False, "refs/heads/ar/master", False),
    )
    test.assertFalse(memory_proposed["metadata_refresh_after_code_commit"])
    test.assertFalse(memory_proposed["entity_fingerprint_refresh_after_code_commit"])
    test.assertFalse(memory_proposed["route_refresh_after_code_commit"])
    test.assertFalse(memory_proposed["memory_quality_check_before_commit"])
    test.assertFalse(ledger_proposed["would_update"])
    test.assertEqual(
        preview.payload["closeout_order"],
        [
            "read-exact-series-code-ref",
            "read-exact-series-memory-ref",
            "verify-existing-ledger-maps-exact-series-commits",
            "record-existing-series-commits-in-contract",
        ],
    )
    for key, subkey, expected in (
        ("onboarding_metadata_refresh", "required", {"count": 0}),
        ("entity_fingerprint_refresh", "required", []),
        ("route_overview_metadata_refresh", "required", []),
        ("route_index_refresh", "written", 0),
    ):
        section = preview.payload[key]
        assert isinstance(section, dict)
        found = section[subkey]
        if isinstance(expected, dict):
            assert isinstance(found, dict)
            test.assertEqual(found["count"], expected["count"])
        else:
            test.assertEqual(found, expected)
