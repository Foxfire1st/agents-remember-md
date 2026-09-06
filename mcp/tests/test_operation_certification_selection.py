"""Real owner observation, immutable evidence and lifecycle CAS composition.

Report payloads are explicit fixture observations, never acceptance evidence.
No gate executor or worker process is launched by this suite.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from agents_remember.certification.certificate_models import GateCertificate
from agents_remember.certification.digests import content_digest
from agents_remember.certification.frozen_run.authorities import CandidateAuthorityRecords
from agents_remember.certification.lifecycle_recovery import compile_certification_recovery_record
from agents_remember.certification.repository_profiles.authority import load_repository_profile
from agents_remember.certification.repository_profiles.execution import (
    admit_repository_profile_execution,
)
from agents_remember.errors import CertificationContractError
from agents_remember.models.lifecycles.certification import (
    OperationCertificationState,
    SelectedRecoveryDecision,
)
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    LifecycleOperationRecord,
)
from agents_remember.models.task_intent import MissingTaskIntent, TaskIntentIdentity
from agents_remember.tasks import TaskDocument, write_task_doc
from agents_remember.tasks.leaf_doc import resolve_terminal_leaf_doc
from agents_remember.worktrees.integration.closeout.certification import (
    admission as admission_module,
)
from agents_remember.worktrees.integration.closeout.certification import (
    observation as observation_module,
)
from agents_remember.worktrees.integration.closeout.certification import (
    selection as selection_module,
)
from agents_remember.worktrees.integration.closeout.certification.admission import (
    FrozenCloseoutAdmission,
    prepare_closeout_certification,
    select_initial_certification,
    validate_selected_currentness,
)
from agents_remember.worktrees.integration.closeout.certification.observation import (
    CandidateObservationRequest,
    observe_certification_candidate,
)
from agents_remember.worktrees.integration.closeout.certification.selection import (
    require_selected_certification,
    select_certification_state,
    select_recorded_terminals,
    terminal_selection,
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
from agents_remember.worktrees.modules.git import require_git, worktree_candidate_tree
from agents_remember.worktrees.modules.quality import clean_executor
from agents_remember.worktrees.modules.quality.certification_records import (
    record_published_generation,
)
from agents_remember.worktrees.modules.quality.certification_terminal import (
    RecordedCertificationGeneration,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    REPORT_SET_MANIFEST,
    load_published_quality_manifest,
)
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)
from agents_remember.worktrees.queue.closeout_staged_quality import PreparedStagedCode
from agents_remember.worktrees.worktree_contract import WorktreeContract
from closeout_fixture_test_support import selected_fixture
from closeout_input_test_support import closeout_operation_input
from gate_certification_test_support import _gate_catalog
from pydantic import ValidationError
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


def _state(fixture: _Fixture) -> OperationCertificationState:
    assert fixture.record.certification is not None
    return fixture.record.certification


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


def _select_reports(fixture: _Fixture, export: Path) -> _Fixture:
    recorded = _publish(fixture, export)
    record = select_recorded_terminals(fixture.contract, fixture.store, fixture.record, recorded)
    return replace(fixture, record=record)


def _successor(fixture: _Fixture) -> _Fixture:
    previous = fixture.store.update(
        lambda record: record.model_copy(
            update={
                "status": "failed",
                "phase": "failed",
                "finishedAt": datetime.now(UTC).isoformat(),
            }
        )
    )
    operation_input = fixture.operation_input.model_copy(
        update={"approvalNote": "successor fixture owner"}
    )
    frozen = prepare_closeout_certification(
        fixture.contract,
        operation_input,
        previous,
        candidate_tree=fixture.frozen.prepared.candidateTree,
    )
    assert frozen is not None
    record = fixture.store.replace_terminal(
        _queued(fixture.contract, operation_input, frozen.prepared.candidateTree)
    )
    record = select_initial_certification(fixture.contract, fixture.store, record, frozen)
    return replace(fixture, operation_input=operation_input, frozen=frozen, record=record)


def test_initial_selection_reads_real_owner_inputs_and_original_objects(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    loaded = require_selected_certification(fixture.contract, fixture.record)
    assert loaded.run == fixture.frozen.prepared.frozen_run
    assert loaded.run.provenance == fixture.frozen.prepared.provenance
    assert loaded.authorities == fixture.frozen.observed.authorities
    assert loaded.admission == fixture.frozen.admission
    assert loaded.recovery.semanticEnvelope.reusePlan.firstGateToRun == 1
    assert loaded.terminals == ()
    assert fixture.record.recordRevision == 2
    assert fixture.record.meaningfulRevision == 2
    assert (
        validate_selected_currentness(fixture.contract, fixture.operation_input, fixture.record)
        == loaded
    )


def _corrupt_terminal_selection(payload: dict[str, Any], damage: str) -> None:
    terminal = payload["terminals"][0]
    predecessor = {
        key: payload[key]
        for key in (
            "operationKey",
            "generation",
            "frozenRun",
            "candidateAuthorities",
            "lifecycleAdmission",
        )
    }
    if damage == "publication-hash":
        terminal["publication"]["canonicalBytes"] += " "
    elif damage == "result-kind":
        terminal["result"] = payload["frozenRun"]
    elif damage == "certificate-kind":
        terminal["certificate"] = terminal["result"]
    elif damage == "uncertified-reuse":
        terminal.update(certificate=None, reusedFrom=predecessor)
    elif damage == "predecessor-kind":
        predecessor["frozenRun"] = payload["candidateAuthorities"]
        payload["predecessor"] = predecessor


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("publication-hash", "retained certification bytes"),
        ("result-kind", "exact result manifest"),
        ("certificate-kind", "exact certificate reference"),
        ("uncertified-reuse", "only certified predecessor"),
        ("predecessor-kind", "predecessor requires an exact"),
        ("authority-kind", "selection requires an exact"),
        ("recovery-kind", "another object kind"),
        ("duplicate-recovery", "cannot be selected twice"),
        ("duplicate-terminal", "unique and ordered"),
        ("reordered-terminals", "unique and ordered"),
        ("unbound-inputs", "explicit predecessor"),
        ("certified-history", "only original uncertified"),
        ("duplicate-history", "each original attempt once"),
        ("reordered-history", "ordered"),
    ],
)
def test_journal_rejects_malformed_selected_evidence_without_writing(
    tmp_path: Path, damage: str, message: str
) -> None:
    fixture = _select_reports(_fixture(tmp_path), tmp_path / "export")
    payload = _state(fixture).model_dump(mode="json")
    terminal = payload["terminals"][0]
    if damage in {
        "publication-hash",
        "result-kind",
        "certificate-kind",
        "uncertified-reuse",
        "predecessor-kind",
    }:
        _corrupt_terminal_selection(payload, damage)
    elif damage == "authority-kind":
        payload["frozenRun"] = payload["candidateAuthorities"]
    elif damage == "recovery-kind":
        payload["recoveryDecisions"][0]["reference"] = payload["frozenRun"]
    elif damage == "duplicate-recovery":
        payload["recoveryDecisions"] *= 2
    elif damage == "duplicate-terminal":
        payload["terminals"] = [terminal, terminal]
    elif damage == "reordered-terminals":
        payload["terminals"].reverse()
    elif damage == "unbound-inputs":
        payload["inputTerminals"] = [terminal]
    elif damage == "certified-history":
        payload["terminalHistory"] = [terminal]
    else:
        history = [dict(item, certificate=None) for item in payload["terminals"][:2]]
        payload["terminalHistory"] = (
            [history[0], history[0]] if damage == "duplicate-history" else history[::-1]
        )
    before = fixture.store.path.read_bytes()

    def corrupt(record: LifecycleOperationRecord) -> LifecycleOperationRecord:
        selected = OperationCertificationState.model_validate(payload)
        return record.model_copy(update={"certification": selected})

    with pytest.raises(ValidationError, match=message):
        fixture.store.update(corrupt)
    assert fixture.store.path.read_bytes() == before
    assert require_selected_certification(fixture.contract, fixture.record).state == _state(fixture)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("certification", None, "certification-selection-missing"),
        ("taskId", "another-task", "certification-selection-owner-mismatch"),
        ("contractPath", "/another/contract.md", "certification-selection-owner-mismatch"),
        ("candidateTree", "0" * 40, "certification-selection-binding-mismatch"),
        ("predecessorFingerprint", "0" * 64, "selected-predecessor-missing"),
    ],
)
def test_selected_readback_refuses_wrong_owner_or_missing_predecessor(
    tmp_path: Path, field: str, value: object, code: str
) -> None:
    fixture = _fixture(tmp_path)
    before = fixture.store.path.read_bytes()
    proposed = fixture.record.model_copy(update={field: value})
    with pytest.raises(CertificationContractError) as caught:
        require_selected_certification(fixture.contract, proposed)
    assert [finding["code"] for finding in caught.value.findings] == [code]
    assert fixture.store.path.read_bytes() == before


@pytest.mark.parametrize(
    "damage", ["missing-prefix", "uncertified-prefix", "unbound-reuse", "unselected-certificate"]
)
def test_selected_readback_requires_the_original_terminal_dependencies(
    tmp_path: Path, damage: str
) -> None:
    fixture = _select_reports(_fixture(tmp_path), tmp_path / "export")
    state = _state(fixture)
    if damage == "missing-prefix":
        state = state.model_copy(update={"terminals": state.terminals[1:]})
        code = "selected-terminal-prefix-invalid"
    elif damage == "uncertified-prefix":
        state = state.model_copy(
            update={
                "terminals": (
                    state.terminals[0].model_copy(update={"certificate": None}),
                    *state.terminals[1:],
                )
            }
        )
        code = "selected-terminal-after-red"
    elif damage == "unbound-reuse":
        predecessor = selection_module.CertificationPredecessor(
            operationKey=state.operationKey,
            generation=state.generation,
            frozenRun=state.frozenRun,
            candidateAuthorities=state.candidateAuthorities,
            lifecycleAdmission=state.lifecycleAdmission,
        )
        state = state.model_copy(
            update={
                "terminals": (state.terminals[0].model_copy(update={"reusedFrom": predecessor}),)
            }
        )
        code = "selected-reused-terminal-unbound"
    else:
        loaded = require_selected_certification(fixture.contract, fixture.record)
        certificate = loaded.terminals[0].certificate
        assert certificate is not None
        recovery = compile_certification_recovery_record(
            fixture.frozen.admission,
            (certificate,),
            (),
            provenance=fixture.frozen.prepared.provenance,
        )
        objects = fixture.frozen.prepared.certificate_store()
        objects.publish(recovery)
        state = state.model_copy(
            update={
                "terminals": (),
                "recoveryDecisions": (
                    SelectedRecoveryDecision(
                        reference=objects.reference("recovery", recovery.recoveryDigest)
                    ),
                ),
            }
        )
        code = "selected-recovery-certificates-missing"
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        require_selected_certification(
            fixture.contract, fixture.record.model_copy(update={"certification": state})
        )
    assert [finding["code"] for finding in caught.value.findings] == [code]
    assert fixture.store.path.read_bytes() == before


def test_store_cannot_erase_selected_certification_after_cancellation(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    cancelled = fixture.store.update(
        lambda record: record.model_copy(update={"cancelRequested": True})
    )
    assert cancelled.certification == _state(fixture)
    before = fixture.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="live noncancelled operation"):
        fixture.store.update(lambda record: record.model_copy(update={"certification": None}))
    assert fixture.store.path.read_bytes() == before


def test_selected_readback_refuses_a_redigested_authority_projection(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    original = fixture.frozen.observed.authorities
    payload = original.model_dump(mode="json")
    rules = payload["semanticEnvelope"]["worktree"]
    rules["preCommitHookRan"] = not rules["preCommitHookRan"]
    payload["authorityDigest"] = content_digest(payload["semanticEnvelope"])
    changed = CandidateAuthorityRecords.model_validate(payload)
    objects = fixture.frozen.prepared.certificate_store()
    objects.publish(changed)
    reference = objects.reference("candidate-authorities", changed.authorityDigest)
    assert objects.load_reference(reference) == changed
    selected = _state(fixture).model_copy(update={"candidateAuthorities": reference})
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        require_selected_certification(
            fixture.contract, fixture.record.model_copy(update={"certification": selected})
        )
    assert caught.value.findings[0]["code"] == "certification-owner-projections-mismatch"
    assert fixture.store.path.read_bytes() == before
    assert require_selected_certification(fixture.contract, fixture.record).authorities == original


def test_history_reader_enforces_capacity_and_reuses_only_its_loaded_cache(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    cache: dict[int, selection_module.LoadedCertificationSelection] = {}
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        selection_module._load_selection(fixture.contract, fixture.record, cache, remaining=0)
    assert caught.value.findings[0]["code"] == "selected-history-capacity"
    assert cache == {}
    loaded = selection_module._load_selection(fixture.contract, fixture.record, cache, remaining=1)
    assert cache == {fixture.record.generation: loaded}
    assert (
        selection_module._load_selection(fixture.contract, fixture.record, cache, remaining=1)
        is loaded
    )
    assert fixture.store.path.read_bytes() == before


def test_existing_selected_generation_does_not_prepare_or_select_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    preparation_calls = []
    actual_prepare = admission_module.prepare_staged_code

    def prepare(*args, **kwargs):
        preparation_calls.append((args, kwargs))
        return actual_prepare(*args, **kwargs)

    monkeypatch.setattr(admission_module, "prepare_staged_code", prepare)
    before = fixture.store.path.read_bytes()
    frozen = prepare_closeout_certification(
        fixture.contract,
        fixture.operation_input,
        fixture.record,
        candidate_tree=fixture.frozen.prepared.candidateTree,
    )
    assert frozen is None
    assert (
        select_initial_certification(fixture.contract, fixture.store, fixture.record, frozen)
        is fixture.record
    )
    assert preparation_calls == []
    assert fixture.store.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["missing-admission", "already-selected"])
def test_initial_selection_owner_refuses_missing_or_repeated_admission(
    tmp_path: Path, fault: str
) -> None:
    fixture = _fixture(tmp_path, select=fault == "already-selected")
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        admission_module.initial_certification_state(
            fixture.contract,
            fixture.record,
            None if fault == "missing-admission" else fixture.frozen,
        )
    expected = (
        "certification-admission-missing"
        if fault == "missing-admission"
        else "certification-selection-already-exists"
    )
    assert caught.value.findings[0]["code"] == expected
    assert fixture.store.path.read_bytes() == before


@pytest.mark.parametrize("field", ["frozenRun", "candidateAuthorities", "lifecycleAdmission"])
def test_store_refuses_selected_authority_replacement_without_writing(
    tmp_path: Path, field: str
) -> None:
    fixture = _fixture(tmp_path)
    selected = _state(fixture)
    reference = getattr(selected, field)
    replacement = selected.model_copy(
        update={field: reference.model_copy(update={"contentSha256": "0" * 64})}
    )
    before = fixture.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="selected certification authority cannot change"):
        fixture.store.update(
            lambda record: record.model_copy(update={"certification": replacement})
        )
    assert fixture.store.path.read_bytes() == before


@pytest.mark.parametrize("field", ["generation", "operationKey"])
def test_store_refuses_another_selected_owner(tmp_path: Path, field: str) -> None:
    fixture = _fixture(tmp_path)
    selected = _state(fixture).model_copy(update={field: 2 if field == "generation" else "0" * 64})
    before = fixture.store.path.read_bytes()
    with pytest.raises(ValidationError, match="exact closeout operation generation"):
        fixture.store.update(lambda record: record.model_copy(update={"certification": selected}))
    assert fixture.store.path.read_bytes() == before


@pytest.mark.parametrize("winner", ["cancel-request", "terminal", "cancelled"])
def test_nonlive_owner_never_selects_orphaned_objects(tmp_path: Path, winner: str) -> None:
    fixture = _fixture(tmp_path, select=False)
    change: dict[str, object] = {"cancelRequested": True}
    if winner == "terminal":
        change = {"status": "failed", "phase": "failed", "finishedAt": "2026-09-06T12:00:00+00:00"}
    elif winner == "cancelled":
        change = {
            "status": "cancelled",
            "phase": "cancelled",
            "finishedAt": "2026-09-06T12:00:00+00:00",
        }
    current = fixture.store.update(lambda record: record.model_copy(update=change))
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        select_initial_certification(fixture.contract, fixture.store, current, fixture.frozen)
    assert caught.value.findings[0]["code"] == "certification-selection-not-current"
    assert fixture.store.read() == current and current.certification is None
    assert fixture.store.path.read_bytes() == before
    assert fixture.frozen.prepared.certificate_store().load_reference(
        fixture.frozen.prepared.frozen_reference
    )


def test_readback_allows_heartbeat_but_preserves_its_progress_in_the_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path, select=False)
    original = selection_module.require_selected_certification
    observations: list[LifecycleOperationRecord] = []

    def readback(contract, record):
        result = original(contract, record)
        observations.append(
            fixture.store.update(
                lambda current: current.model_copy(
                    update={
                        "heartbeatAt": "2026-09-06T12:00:00+00:00",
                        "currentCommand": "verify original report",
                    }
                )
            )
        )
        return result

    monkeypatch.setattr(selection_module, "require_selected_certification", readback)
    selected = select_initial_certification(
        fixture.contract, fixture.store, fixture.record, fixture.frozen
    )
    assert len(observations) == 1
    heartbeat = observations[0]
    assert selected.heartbeatAt == heartbeat.heartbeatAt
    assert selected.currentCommand == heartbeat.currentCommand
    assert selected.recordRevision == heartbeat.recordRevision + 1
    assert selected.meaningfulRevision == heartbeat.meaningfulRevision + 1
    assert selected.certification is not None
    assert fixture.store.read() == selected
    assert original(fixture.contract, selected).run == fixture.frozen.prepared.frozen_run


@pytest.mark.parametrize("moment", ["readback", "cas"])
def test_cancellation_during_readback_or_cas_refuses_selection_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    fixture = _fixture(tmp_path, select=False)
    calls: list[str] = []
    original = selection_module.require_selected_certification
    original_cas = fixture.store.update_if_current

    def cancel():
        calls.append("cancel")
        fixture.store.update(lambda current: current.model_copy(update={"cancelRequested": True}))

    def readback(contract, record):
        result = original(contract, record)
        if moment == "readback":
            cancel()
        return result

    def raced_cas(observed, transform):
        calls.append("cas")
        if moment == "cas":
            cancel()
        return original_cas(observed, transform)

    monkeypatch.setattr(selection_module, "require_selected_certification", readback)
    monkeypatch.setattr(fixture.store, "update_if_current", raced_cas)
    with pytest.raises(CertificationContractError) as caught:
        select_initial_certification(
            fixture.contract, fixture.store, fixture.record, fixture.frozen
        )
    assert caught.value.findings[0]["code"] == (
        "certification-selection-observation-moved"
        if moment == "readback"
        else "certification-selection-cas-lost"
    )
    assert calls == (["cancel"] if moment == "readback" else ["cas", "cancel"])
    current = fixture.store.read()
    assert current is not None and current.cancelRequested and current.certification is None


def test_missing_owner_after_readback_never_selects_certification(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, select=False)
    fixture.store.path.unlink()
    with pytest.raises(CertificationContractError) as caught:
        select_initial_certification(
            fixture.contract, fixture.store, fixture.record, fixture.frozen
        )
    assert caught.value.findings[0]["code"] == "certification-selection-observation-moved"
    assert not fixture.store.path.exists()


def test_terminal_and_recovery_history_append_once_and_preserve_exact_bytes(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    recorded = _publish(fixture, tmp_path / "export")
    first = RecordedCertificationGeneration(recorded.terminals[:1], recorded.records[:1])
    selected = select_recorded_terminals(fixture.contract, fixture.store, fixture.record, first)
    assert selected.certification is not None
    certificate = recorded.terminals[0].certificate
    assert certificate is not None
    recovery = compile_certification_recovery_record(
        fixture.frozen.admission,
        (certificate,),
        (),
        provenance=fixture.frozen.prepared.provenance,
    )
    objects = fixture.frozen.prepared.certificate_store()
    objects.publish(recovery)
    appended = selected.certification.model_copy(
        update={
            "recoveryDecisions": (
                *selected.certification.recoveryDecisions,
                SelectedRecoveryDecision(
                    reference=objects.reference("recovery", recovery.recoveryDigest)
                ),
            )
        }
    )
    selected = select_certification_state(fixture.contract, fixture.store, selected, appended)
    selected = select_recorded_terminals(fixture.contract, fixture.store, selected, recorded)
    stable = fixture.store.path.read_bytes()
    assert (
        select_recorded_terminals(fixture.contract, fixture.store, selected, recorded) == selected
    )
    assert fixture.store.path.read_bytes() == stable
    assert selected.certification is not None
    assert selected.certification.terminals[0] == terminal_selection(recorded.terminals[0])
    for change in (
        {"recoveryDecisions": appended.recoveryDecisions[1:]},
        {"terminals": selected.certification.terminals[1:]},
    ):
        replacement = selected.certification.model_copy(update=change)
        with pytest.raises(RuntimeError, match="cannot be replaced"):
            fixture.store.update(
                lambda record, replacement=replacement: record.model_copy(
                    update={"certification": replacement}
                )
            )
        assert fixture.store.path.read_bytes() == stable
    first_recovery = appended.recoveryDecisions[0].reference
    objects.exact_path(first_recovery.kind, first_recovery.semanticDigest).unlink()
    with pytest.raises(CertificationContractError):
        require_selected_certification(fixture.contract, selected)
    assert fixture.store.path.read_bytes() == stable


@pytest.mark.parametrize(
    "kind", ["frozenRun", "candidateAuthorities", "lifecycleAdmission", "recovery"]
)
def test_selected_reference_requires_original_provenance_bytes(tmp_path: Path, kind: str) -> None:
    fixture = _fixture(tmp_path)
    selected = _state(fixture)
    reference = (
        selected.recoveryDecisions[0].reference if kind == "recovery" else getattr(selected, kind)
    )
    objects = fixture.frozen.prepared.certificate_store()
    path = objects.exact_path(reference.kind, reference.semanticDigest)
    payload = json.loads(path.read_bytes())
    payload["provenance"]["producer"] = "different-observed-producer"
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError):
        require_selected_certification(fixture.contract, fixture.record)
    assert fixture.store.path.read_bytes() == before


def test_terminal_selection_rejects_a_stored_certificate_for_another_candidate(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    recorded = _publish(fixture, tmp_path / "export")
    terminal = recorded.terminals[0]
    assert terminal.certificate is not None
    envelope = terminal.certificate.semanticEnvelope.model_copy(
        update={
            "candidateCodeTree": terminal.certificate.semanticEnvelope.candidateCodeTree.model_copy(
                update={"value": "0" * 40}
            ),
        }
    )
    wrong = GateCertificate(
        semanticEnvelope=envelope,
        certificateDigest=content_digest(envelope),
        provenance=terminal.certificate.provenance,
    )
    objects = fixture.frozen.prepared.certificate_store()
    objects.publish(wrong)
    forged = replace(
        terminal,
        certificate=wrong,
        certificateReference=objects.reference("certificate", wrong.certificateDigest),
    )
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError):
        select_recorded_terminals(
            fixture.contract,
            fixture.store,
            fixture.record,
            RecordedCertificationGeneration((forged,), ()),
        )
    assert fixture.store.path.read_bytes() == before


def test_original_publication_survives_pointer_loss_but_corrupt_bytes_refuse(
    tmp_path: Path,
) -> None:
    fixture = _select_reports(_fixture(tmp_path), tmp_path / "export")
    before = fixture.store.path.read_bytes()
    reports = fixture.contract.worktree_group / "reports"
    (reports / REPORT_SET_MANIFEST).unlink()
    loaded = require_selected_certification(fixture.contract, fixture.record)
    assert len(loaded.terminals) == 4
    terminal = loaded.terminals[0]
    evidence = terminal.result.railResults[0].evidence[0]
    path = published_report_path_from_manifest(reports, terminal.publication, evidence.reference)
    path.write_bytes(b"corrupt original evidence\n")
    with pytest.raises(CertificationContractError):
        require_selected_certification(fixture.contract, fixture.record)
    assert fixture.store.path.read_bytes() == before


@pytest.mark.parametrize(
    "damage",
    [
        "predecessor-ref",
        "input-publication",
        "earlier-recovery",
        "archive-owner",
        "archive-missing",
        "predecessor-generation",
        "reused-owner",
    ],
)
def test_successor_readback_checks_original_selected_history(tmp_path: Path, damage: str) -> None:
    prior = _select_reports(_fixture(tmp_path), tmp_path / "export")
    fixture = _successor(prior)
    selected = _state(fixture)
    assert selected.predecessor is not None
    assert selected.inputTerminals == _state(prior).terminals
    loaded = require_selected_certification(fixture.contract, fixture.record)
    assert tuple(item.certificate for item in loaded.terminals) == tuple(
        item.certificate
        for item in require_selected_certification(prior.contract, prior.record).terminals
    )
    if damage == "predecessor-ref":
        reference = selected.predecessor.candidateAuthorities.model_copy(
            update={"contentSha256": "0" * 64}
        )
        selected = selected.model_copy(
            update={
                "predecessor": selected.predecessor.model_copy(
                    update={"candidateAuthorities": reference}
                )
            }
        )
    elif damage == "input-publication":
        original = selected.inputTerminals[0]
        encoded = original.publication.canonicalBytes + " "
        publication = original.publication.model_copy(
            update={
                "canonicalBytes": encoded,
                "contentSha256": hashlib.sha256(encoded.encode()).hexdigest(),
            }
        )
        selected = selected.model_copy(
            update={
                "inputTerminals": (
                    original.model_copy(update={"publication": publication}),
                    *selected.inputTerminals[1:],
                )
            }
        )
    elif damage == "predecessor-generation":
        selected = selected.model_copy(
            update={
                "predecessor": selected.predecessor.model_copy(
                    update={"generation": selected.generation}
                )
            }
        )
    elif damage == "reused-owner":
        terminal = selected.terminals[0]
        assert terminal.reusedFrom is not None
        replacement = terminal.model_copy(
            update={"reusedFrom": terminal.reusedFrom.model_copy(update={"operationKey": "0" * 64})}
        )
        selected = selected.model_copy(update={"terminals": (replacement, *selected.terminals[1:])})
    else:
        predecessor = _state(prior)
        path = fixture.store.path.with_name(f"{fixture.store.path.stem}.generation-1.json")
        if damage == "archive-missing":
            path.unlink()
        elif damage == "archive-owner":
            payload = json.loads(path.read_bytes())
            payload["operationKey"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
        else:
            reference = predecessor.recoveryDecisions[0].reference
            fixture.frozen.prepared.certificate_store().exact_path(
                reference.kind, reference.semanticDigest
            ).unlink()
    before = fixture.store.path.read_bytes()
    with pytest.raises((CertificationContractError, RuntimeError)) as refused:
        require_selected_certification(
            fixture.contract, fixture.record.model_copy(update={"certification": selected})
        )
    if damage == "input-publication":
        assert isinstance(refused.value, CertificationContractError)
        assert len(refused.value.findings) == 1
        assert dict(refused.value.findings[0]) == {
            "code": "selected-input-terminals-mismatch",
            "path": "candidate",
            "expected": tuple(item.model_dump(mode="json") for item in _state(prior).terminals),
            "observed": tuple(item.model_dump(mode="json") for item in selected.inputTerminals),
        }
    assert fixture.store.path.read_bytes() == before
    if damage in {"earlier-recovery", "archive-owner"}:
        pointer = fixture.contract.worktree_group / "reports" / REPORT_SET_MANIFEST
        accepted = pointer.read_bytes()
        with pytest.raises((CertificationContractError, RuntimeError)):
            _publish(fixture, tmp_path / "refused-publication")
        assert pointer.read_bytes() == accepted


@pytest.mark.parametrize("movement", ["unstaged", "untracked", "wrong-branch", "source-tip"])
def test_authority_observation_detects_actual_checkout_movement(
    tmp_path: Path, movement: str
) -> None:
    fixture = _fixture(tmp_path)
    root = fixture.contract.code_worktree
    if movement == "unstaged":
        path = next(path for path in root.iterdir() if path.is_file() and path.name != ".git")
        path.write_bytes(path.read_bytes() + b"changed\n")
    elif movement == "untracked":
        (root / "new-candidate.txt").write_text("new\n", encoding="utf-8")
    elif movement == "wrong-branch":
        require_git(root, ["checkout", "-b", "unexpected-branch"])
    else:
        source = fixture.contract.code_repo_path
        head = require_git(source, ["rev-parse", fixture.contract.code_source_branch])
        tree = require_git(source, ["rev-parse", f"{head}^{{tree}}"])
        successor = require_git(source, ["commit-tree", tree, "-p", head, "-m", "source movement"])
        require_git(
            source,
            ["update-ref", f"refs/heads/{fixture.contract.code_source_branch}", successor, head],
        )
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError):
        validate_selected_currentness(fixture.contract, fixture.operation_input, fixture.record)
    assert fixture.store.path.read_bytes() == before


def test_observation_refuses_a_canonical_task_changed_after_door_selection(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    found = resolve_terminal_leaf_doc(fixture.contract.task_root, fixture.contract.leaf_id)
    assert found is not None
    path, document = found
    payload = document.model_dump(mode="json")
    payload["objective"] = "A different canonical task intent after the selected door."
    write_task_doc(path.parent, TaskDocument.model_validate(payload))
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        validate_selected_currentness(fixture.contract, fixture.operation_input, fixture.record)
    assert caught.value.findings[0]["code"] == "candidate-task-authority-mismatch"
    assert fixture.store.path.read_bytes() == before


def test_observation_refuses_a_different_external_memory_checkout_branch(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, memory_mode="external")
    memory = fixture.contract.memory_worktree
    assert memory is not None
    code = require_git(fixture.contract.code_worktree, ["rev-parse", "HEAD"])
    require_git(memory, ["checkout", "-b", "unexpected-memory-branch"])
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        validate_selected_currentness(fixture.contract, fixture.operation_input, fixture.record)
    assert caught.value.findings[0]["code"] == "candidate-memory-branch-mismatch"
    assert fixture.store.path.read_bytes() == before
    assert require_git(fixture.contract.code_worktree, ["rev-parse", "HEAD"]) == code


@pytest.mark.parametrize("boundary", ["lineage", "ancestry"])
def test_observation_refuses_actual_source_ref_movement_between_owner_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    fixture = _fixture(tmp_path)
    source = fixture.contract.code_repo_path
    branch = fixture.contract.code_source_branch
    original = require_git(source, ["rev-parse", branch])
    tree = require_git(source, ["rev-parse", f"{original}^{{tree}}"])
    successor = require_git(source, ["commit-tree", tree, "-p", original, "-m", "racing source"])
    actual_lineage = observation_module.source_lineage_for_contract
    actual_ancestor = observation_module.is_ancestor
    moved = False

    def advance_once() -> None:
        nonlocal moved
        if not moved:
            require_git(source, ["update-ref", f"refs/heads/{branch}", successor, original])
            moved = True

    def read_lineage(contract):
        value = actual_lineage(contract)
        if boundary == "lineage":
            advance_once()
        return value

    def read_ancestor(root, ancestor, descendant):
        value = actual_ancestor(root, ancestor, descendant)
        if boundary == "ancestry":
            advance_once()
        return value

    monkeypatch.setattr(observation_module, "source_lineage_for_contract", read_lineage)
    monkeypatch.setattr(observation_module, "is_ancestor", read_ancestor)
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        validate_selected_currentness(fixture.contract, fixture.operation_input, fixture.record)
    expected = (
        "candidate-source-lineage-moved" if boundary == "lineage" else "candidate-source-ref-moved"
    )
    assert caught.value.findings[0]["code"] == expected
    assert moved and require_git(source, ["rev-parse", branch]) == successor
    assert fixture.store.path.read_bytes() == before


@pytest.mark.parametrize("missing", ["topology", "intent", "legacy-intent"])
def test_observation_refuses_incomplete_task_authority_without_journal_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    fixture = _fixture(tmp_path)
    observer = observation_module.worktree_services().memory_quality
    actual = observer.observe_contract_task(fixture.contract)
    updates = (
        {"semanticTopologyDigest": None}
        if missing == "topology"
        else {"taskIntent": MissingTaskIntent() if missing == "legacy-intent" else None}
    )
    monkeypatch.setattr(
        observer, "observe_contract_task", lambda _: actual.model_copy(update=updates)
    )
    before = fixture.store.path.read_bytes()
    with pytest.raises(CertificationContractError) as refused:
        validate_selected_currentness(fixture.contract, fixture.operation_input, fixture.record)
    assert refused.value.findings[0]["code"] == "candidate-task-authority-incomplete"
    assert fixture.store.path.read_bytes() == before


def test_observation_refuses_primary_checkout_without_staging_it(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    contract = replace(fixture.contract, code_worktree=fixture.contract.code_repo_path)
    before = require_git(contract.code_repo_path, ["write-tree"])
    with pytest.raises(CertificationContractError):
        observe_certification_candidate(
            CandidateObservationRequest(
                contract,
                fixture.operation_input.effectiveInput,
                fixture.frozen.prepared.frozen_run.repositoryProfile,
                PreparedStagedCode(before, False),
                fixture.frozen.prepared.provenance,
            )
        )
    assert require_git(contract.code_repo_path, ["write-tree"]) == before


@pytest.mark.parametrize("outcome", ["interrupted", "red"])
def test_only_an_original_interruption_can_be_replaced_in_the_same_generation(
    tmp_path: Path, outcome: Literal["interrupted", "red"]
) -> None:
    fixture = _fixture(tmp_path)
    original = _publish(fixture, tmp_path / "interrupted-export", outcome=outcome)
    selected = select_recorded_terminals(fixture.contract, fixture.store, fixture.record, original)
    loaded = require_selected_certification(fixture.contract, selected)
    assert loaded.terminals[0].certificate is None
    replacement = _publish(fixture, tmp_path / "replacement-export")
    before = fixture.store.path.read_bytes()
    if outcome == "red":
        with pytest.raises(CertificationContractError, match="admission refused"):
            select_recorded_terminals(fixture.contract, fixture.store, selected, replacement)
        assert fixture.store.path.read_bytes() == before
        return
    updated = select_recorded_terminals(fixture.contract, fixture.store, selected, replacement)
    assert updated.certification is not None
    assert updated.certification.terminalHistory == (terminal_selection(original.terminals[0]),)
    assert updated.generation == selected.generation
    loaded = require_selected_certification(fixture.contract, updated)
    assert len(loaded.terminals) == 4
    assert original.terminals[0].publication.generation in loaded.protected_generations
    for index in range(3):
        _publish(fixture, tmp_path / f"later-publication-{index}")
    assert require_selected_certification(fixture.contract, updated) == loaded
    retained_state = updated.certification
    stable = fixture.store.path.read_bytes()
    with pytest.raises(RuntimeError, match="retain the exact original history"):
        fixture.store.update(
            lambda record: record.model_copy(
                update={
                    "certification": retained_state.model_copy(update={"terminalHistory": ()}),
                }
            )
        )
    assert fixture.store.path.read_bytes() == stable
    report = original.terminals[0].publication
    decoder = published_report_path_from_manifest(
        fixture.contract.worktree_group / "reports",
        report,
        report.result_decoder.artifactPath,
    )
    decoder.write_bytes(b"destroyed original interrupted outcome")
    with pytest.raises(RuntimeError):
        require_selected_certification(fixture.contract, updated)
