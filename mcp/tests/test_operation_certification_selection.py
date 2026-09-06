"""Real owner observation, immutable evidence and lifecycle CAS composition.

Report payloads are explicit fixture observations, never acceptance evidence.
No gate executor or worker process is launched by this suite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from agents_remember.certification.repository_profiles.authority import load_repository_profile
from agents_remember.certification.repository_profiles.execution import (
    admit_repository_profile_execution,
)
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    LifecycleOperationRecord,
)
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.integration.closeout.certification.admission import (
    FrozenCloseoutAdmission,
    prepare_closeout_certification,
    select_initial_certification,
)
from agents_remember.worktrees.integration.closeout.certification.selection import (
    require_selected_certification,
)
from agents_remember.worktrees.integration.lifecycle.generation.creation import (
    queued_operation_record,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    LifecycleOperationCandidateBinding,
    lifecycle_operation_candidate,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    operation_state_fingerprint,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules.git import worktree_candidate_tree
from agents_remember.worktrees.modules.quality import clean_executor
from agents_remember.worktrees.modules.quality.certification_records import (
    record_published_generation,
)
from agents_remember.worktrees.modules.quality.certification_terminal import (
    RecordedCertificationGeneration,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract
from closeout_fixture_test_support import selected_fixture
from closeout_input_test_support import closeout_operation_input
from gate_certification_test_support import _gate_catalog
from test_closeout_queue import MASTER_A
from test_worktree_support import TEST_CERTIFICATION_PROFILE_REFERENCE


@dataclass(frozen=True)
class _Fixture:
    contract: WorktreeContract
    operation_input: CloseoutOperationInput
    store: LifecycleOperationStore
    record: LifecycleOperationRecord
    frozen: FrozenCloseoutAdmission


def _queued(
    contract: WorktreeContract, operation_input: CloseoutOperationInput, tree: str
) -> LifecycleOperationRecord:
    door = contract.closeout_door
    assert door is not None
    assert isinstance(door.taskIntent, TaskIntentIdentity)
    candidate = lifecycle_operation_candidate(
        LifecycleOperationCandidateBinding(
            operation_input=operation_input,
            candidate_state=operation_state_fingerprint(contract),
            candidate_tree=tree,
            closeout_door_generation_id=door.generationId,
            task_intent=door.taskIntent,
        )
    )
    return queued_operation_record(contract, operation_input, candidate, None, datetime.now(UTC))


def _fixture(root: Path, *, select: bool = True, memory_mode: str = "internal") -> _Fixture:
    fixture = selected_fixture(root, memory_mode=memory_mode)
    contract = fixture.contracts[MASTER_A]
    operation_input = closeout_operation_input(contract, config_path=fixture.config_path)
    frozen = prepare_closeout_certification(
        contract,
        operation_input,
        None,
        candidate_tree=worktree_candidate_tree(contract.code_worktree, root / "candidate.index"),
    )
    assert frozen is not None
    store = LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))
    record, created = store.create(
        _queued(contract, operation_input, frozen.prepared.candidateTree)
    )
    assert created
    if select:
        record = select_initial_certification(contract, store, record, frozen)
    return _Fixture(contract, operation_input, store, record, frozen)


def _publish(
    fixture: _Fixture, export: Path, *, outcome: Literal["green", "interrupted", "red"] = "green"
) -> RecordedCertificationGeneration:
    """Publish declared fixture bytes through the real immutable report and certificate owners."""
    export.mkdir()
    prepared = fixture.frozen.prepared
    gates = _gate_catalog(prepared.lane, export)
    if outcome != "green":
        gates = [{**gates[0], "disposition": outcome}]
    if outcome == "red":
        rails = gates[0]["rails"]
        assert isinstance(rails, list)
        planned = prepared.lane.certificationPlan.gates[0].rails
        prerequisites = {item.key for rail in planned for item in rail.prerequisites}
        failed = next(
            rail.identity.key
            for rail in planned
            if rail.applicability.status == "applicable" and rail.identity.key not in prerequisites
        )
        for rail in rails:
            assert isinstance(rail, dict)
            if rail["key"] == failed:
                rail.update(status="fail", exitCode=1)
    payload = {
        "status": "passed" if outcome == "green" else "failed",
        "exitCode": int(outcome != "green"),
        "gates": gates,
        "fixtureInvocation": export.name,
    }
    (export / "clean-quality-results.json").write_text(json.dumps(payload), encoding="utf-8")
    admitted = load_repository_profile(
        fixture.contract.repo_name,
        fixture.contract.code_worktree,
        TEST_CERTIFICATION_PROFILE_REFERENCE,
    )
    execution = admit_repository_profile_execution(
        admitted,
        purpose="closeout",
        mode="targeted",
        candidate_identity=prepared.frozen_run.repositoryPlan.candidateIdentity,
        source_selection=prepared.frozen_run.repositoryPlan.sourceSelection,
    )

    def protected() -> frozenset[str]:
        current = fixture.store.read()
        assert current is not None
        return require_selected_certification(fixture.contract, current).protected_generations

    clean_executor._publish_reports(
        export,
        fixture.contract.worktree_group / "reports",
        candidate_tree=prepared.candidateTree,
        profile_execution=execution,
        bindings=clean_executor.ReportBindings(
            attestation=None,
            runtime_authority_digest=None,
            protected_generations=protected,
        ),
    )
    publication = load_published_quality_manifest(fixture.contract.worktree_group / "reports")
    recorded = record_published_generation(prepared, publication, payload)
    assert len(recorded.terminals) == (4 if outcome == "green" else 1)
    assert all(
        (item.certificate is not None) == (outcome == "green") for item in recorded.terminals
    )
    return recorded
