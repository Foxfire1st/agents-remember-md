"""Prepare real source, memory coherence, and a live code-gate handoff for publication recovery.

The sole consumer runs the production memory-certification and publication adapters.
This fixture prepares their isolated repository inputs and four code certificates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest import mock
from uuid import uuid4

import pytest
from agents_remember.application import worktree_tools
from agents_remember.application.lifecycle.lifecycle_operation_worker import OperationRuntime
from agents_remember.certification.repository_profiles.canonical import repository_profile_digest
from agents_remember.certification.repository_profiles.models import RepositoryCertificationProfile
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.coordination_context.models import CoordinationContext
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.kernel.route_index import build_route_indexes
from agents_remember.kernel.route_index_census import route_index_source_snapshot
from agents_remember.memory_quality.check import (
    AVAILABLE_CHECKS,
    DRIFT_CHECK_NAME,
    DriftCheckContext,
    run_memory_quality_check,
)
from agents_remember.memory_quality.curator_checklist import (
    CuratorChecklist,
    report_path_for,
    write_curator_checklist,
)
from agents_remember.memory_quality.integrity.check_missing_onboarding import (
    missing_onboarding_for_source,
)
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.curator_coherence import CuratorCoherenceRequest
from agents_remember.models.lifecycles.door import CloseoutDoorRequest
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks import write_task_doc
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
    current_certification_handoff,
    execute_selected_closeout,
)
from agents_remember.worktrees.integration.closeout.curator_coherence import (
    require_current_curator_coherence,
)
from agents_remember.worktrees.integration.closeout.curator_coherence_publication import (
    curator_coherence_action,
)
from agents_remember.worktrees.integration.closeout.door_control import (
    DoorActor,
    closeout_door_tool,
)
from agents_remember.worktrees.integration.closeout.future_code_candidate import (
    capture_future_code_candidate,
)
from agents_remember.worktrees.integration.closeout.memory_candidate_pair import (
    resolve_memory_candidate_pair,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.modules.context import contract_context
from agents_remember.worktrees.modules.git import worktree_candidate_tree
from agents_remember.worktrees.modules.quality import clean_executor, gate
from agents_remember.worktrees.services import bind_worktree_services, worktree_services
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract
from repository_profile_test_support import (
    NODE_FIXTURE,
    install_fixture_profile,
)
from test_closeout_certification_entrypoint import _executor, _store
from test_closeout_queue import MASTER_A, SPRINT, QueueFixture, _grade, _leaf
from test_worktree_support import git


@dataclass(frozen=True)
class _FullMemoryScan:
    context: CoordinationContext
    quality: dict[str, Any]
    missing: tuple[dict[str, str], ...]
    stale_indexes: tuple[str, ...]
    eligible: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.quality["ok"] and not self.missing and not self.stale_indexes


def _context(contract: WorktreeContract) -> CoordinationContext:
    assert contract.memory_worktree is not None
    return replace(
        contract_context(contract),
        code_repository_root=contract.code_worktree,
        memory_root=contract.memory_worktree,
        onboarding_root=contract.memory_worktree / "onboarding",
    )


def _scan(contract: WorktreeContract) -> _FullMemoryScan:
    context = _context(contract)
    quality = run_memory_quality_check(
        context.onboarding_root,
        checks=AVAILABLE_CHECKS,
        drift_context=DriftCheckContext(
            contract.code_worktree,
            context,
            detail_limit=1000,
            include_rows=True,
            write_report=False,
        ),
        include_report_only_findings=True,
    )
    census = route_index_source_snapshot(
        code_root=contract.code_worktree,
        storage=context.storage,
        scoped_repo_path=contract.repo_name,
    )
    missing = tuple(
        row.to_dict()
        for source in census.eligible_paths
        if (
            row := missing_onboarding_for_source(
                contract.code_worktree,
                context.onboarding_root,
                context.storage,
                contract.repo_name,
                source,
            )
        )
        is not None
    )
    indexes = build_route_indexes(
        code_root=contract.code_worktree,
        onboarding_root=context.onboarding_root,
        repository=contract.repo_name,
        storage=context.storage,
        dry_run=True,
    )
    actual = {
        path.relative_to(context.onboarding_root).as_posix()
        for path in context.onboarding_root.rglob("overview.index.json")
    }
    assert set(quality["checks"]) == set(AVAILABLE_CHECKS)
    assert len(quality["checks"][DRIFT_CHECK_NAME]["rows"]) > 0
    return _FullMemoryScan(
        context,
        quality,
        missing,
        tuple(sorted(set(indexes.stale_indexes) | (actual - set(indexes.indexes)))),
        census.eligible_paths,
    )


def _write_source_cards(contract: WorktreeContract) -> None:
    context = _context(contract)
    commit = git(contract.code_worktree, "rev-parse", "HEAD")
    date = git(contract.code_worktree, "show", "-s", "--format=%cI", "HEAD")
    sources = route_index_source_snapshot(
        code_root=contract.code_worktree,
        storage=context.storage,
        scoped_repo_path=contract.repo_name,
    ).eligible_paths
    assert {"README.md", "feature.txt"}.issubset(sources)
    for source in (*sources, None):
        path = context.onboarding_root / (f"{source}.md" if source else "overview.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        identity = f"| path | `{source}` |" if source else "| sourceRoute | `.` |"
        doc_type = "file-level-onboarding" if source else "repo-overview"
        path.write_text(
            f"# {source or 'Fixture source overview'}\n\n"
            "| Field | Value |\n| --- | --- |\n"
            f"| repository | {contract.repo_name} |\n{identity}\n"
            f"| doc_type | {doc_type} |\n"
            f"| lastVerifiedCommitHash | `{commit}` |\n"
            f"| lastVerifiedCommitDate | {date} |\n\n"
            "## Purpose\n\n"
            "This temporary source belongs to the isolated memory certification fixture.\n\n"
            "## Update History\n\n"
            f"- {date}: Describe the actual committed fixture source.\n",
            encoding="utf-8",
        )
    build_route_indexes(
        code_root=contract.code_worktree,
        onboarding_root=context.onboarding_root,
        repository=contract.repo_name,
        storage=context.storage,
    )
    git(context.memory_root, "add", "-A")


def _publish_actual_coherence(contract: WorktreeContract, scan: _FullMemoryScan) -> None:
    assert scan.ok, (scan.quality, scan.missing, scan.stale_indexes)
    pair = resolve_memory_candidate_pair(
        contract,
        requested_contract_path=contract.contract_path,
        requested_repo_id=contract.repo_name,
    )
    memory_tree = worktree_candidate_tree(
        scan.context.memory_root, contract.worktree_group / "memory.index"
    )
    result = write_curator_checklist(
        CuratorChecklist(
            report_path_for(contract.worktree_group),
            contract.repo_name,
            contract.code_worktree,
            scan.context.onboarding_root,
            pair,
            capture_future_code_candidate(contract).codeCandidateTree,
            memory_tree,
            scan.quality,
            list(scan.quality["findings"]),
            [],
            {"eligible": len(scan.eligible), "missing": list(scan.missing)},
            list(scan.stale_indexes),
            (),
            scan.quality["checks"][DRIFT_CHECK_NAME]["rows"],
            scan.quality["reportOnlyFindings"],
        )
    )
    assert result["checklistStatus"] == "ready-for-closeout"
    assert result["sourceChangeCandidateCount"] == 0
    prepared = curator_coherence_action(
        contract,
        CuratorCoherenceRequest(action="prepare", contract_path=contract.contract_path.as_posix()),
    )
    published = curator_coherence_action(
        contract,
        CuratorCoherenceRequest(
            action="publish",
            contract_path=contract.contract_path.as_posix(),
            semantic_requirement_revision="FIXTURE-R01@v1",
            delivery_attempt="A001",
            judgments=[],
            expected_predecessor_digest=str(prepared["predecessorAuthorityDigest"]),
            expected_code_candidate_tree=str(prepared["codeCandidateTree"]),
            expected_memory_candidate_tree=str(prepared["memoryCandidateTree"]),
            expected_task_topology_fingerprint=str(prepared["taskTopologyFingerprint"]),
            expected_task_intent=TaskIntentIdentity.model_validate(prepared["taskIntent"]),
            expected_attestation_sha256=str(prepared["attestationSha256"]),
            caller=DeclaredCaller(role="architect", task_document_ref=SPRINT),
        ),
    )
    assert published["state"] in {"published", "already-current"}
    assert require_current_curator_coherence(contract).record.sourceCandidates == []


@dataclass
class _Fixture:
    handoff: CloseoutCertificationHandoff


def _fixture(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Fixture:
    queue = QueueFixture(root, memory_mode="external")
    # Stop at the scheduler continuation boundary; the consumer runs real Gate 5.
    bind_worktree_services(replace(worktree_services(), certification_continuation=None))
    # Leave code-view preparation to the consumer after this temporary patch is restored.
    monkeypatch.setattr(
        "agents_remember.worktrees.integration.closeout.preparation.code_view.prepare_code_view",
        lambda handoff: (handoff, None),
    )
    contract = queue.contracts[MASTER_A]
    path = install_fixture_profile(contract.code_worktree, contract.repo_name, NODE_FIXTURE)
    profile = RepositoryCertificationProfile.model_validate_json(path.read_bytes())
    # A bounded actual R08 catalog, its semantic inputs, and complete item population
    # share the declared result document. This fixture bound is frozen before admission.
    profile = profile.model_copy(
        update={
            "publishedArtifacts": tuple(
                item.model_copy(update={"maxBytes": 256 * 1024})
                if item.path == "result.json"
                else item
                for item in profile.publishedArtifacts
            )
        }
    )
    profile = profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})
    path.write_text(profile.model_dump_json(), encoding="utf-8")
    git(contract.code_worktree, "add", "-A")
    git(
        contract.code_worktree,
        "commit",
        "-m",
        "Prepare real source for isolated memory certification",
    )
    _write_source_cards(contract)
    slug = Path(queue.leaf_refs[MASTER_A].path).stem
    write_task_doc(contract.task_root, _leaf(contract, slug))
    queue.set_priority(queue.leaf_refs[MASTER_A], "normal")
    _publish_actual_coherence(contract, _scan(contract))
    # Use the actual door API directly: QueueFixture.declare intentionally writes
    # synthetic upstream curator evidence, which this producer fixture must not use.
    closeout_door_tool(
        load_config(queue.config_path),
        CloseoutDoorRequest.model_validate(
            {
                "action": "declare",
                "contract_path": contract.contract_path.as_posix(),
                "grade": _grade("normal", queue.leaf_refs[MASTER_A]),
                "admission": {},
            }
        ),
        actor=DoorActor(role="manager", task_document_ref=MASTER_A),
        admitted_contract=contract,
    )
    contract = load_contract(contract.contract_path)
    queue.contracts[MASTER_A] = contract
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        admitted = worktree_tools.worktree_closeout_apply_tool(
            load_config(queue.config_path),
            contract.contract_path.as_posix(),
            worktree_tools.CloseoutCommitMessages(
                code="Fixture code", memory="Fixture memory", ledger="Fixture ledger"
            ),
            worktree_tools.CloseoutApproval(intent_note="Exercise isolated memory certification"),
        )
    assert admitted["ok"] is True and admitted["state"] == "queued", admitted
    launch.assert_called_once()
    store = _store(contract)
    from agents_remember.worktrees.integration.lifecycle.worker.termination import (  # noqa: PLC0415
        worker_process_fingerprint,
    )

    fingerprint = worker_process_fingerprint(os.getpid())
    assert fingerprint is not None
    lease = uuid4().hex * 2
    store.update(
        lambda record: record.model_copy(
            update={
                "workerPid": os.getpid(),
                "workerLease": lease,
                "workerProcessFingerprint": fingerprint,
            }
        )
    )
    owner = OperationRuntime(store, worker_lease=lease).start()
    calls: list[clean_executor.CleanQualityRequest] = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    with pytest.raises(CertificationContractError) as unbound:
        execute_selected_closeout(contract, owner, store)
    assert unbound.value.findings[0]["code"] == "certification-continuation-unbound"
    handoff = current_certification_handoff(contract, owner, store)
    assert tuple(item.result.gate for item in handoff.selected.terminals) == (1, 2, 3, 4)
    return _Fixture(handoff)
