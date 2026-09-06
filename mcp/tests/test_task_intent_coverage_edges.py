"""Focused branch coverage for canonical task-intent consumers."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from agents_remember.errors import TaskIntentError
from agents_remember.models.lifecycles import operation as operation_model
from agents_remember.models.lifecycles.door import CloseoutDoorGeneration
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    LifecycleOperationRecord,
)
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import MissingTaskIntent, TaskIntentIdentity
from agents_remember.tasks import RouteReviewRecord
from agents_remember.tasks.document_refs import ResolvedTaskDocument, TaskDocumentRefError
from agents_remember.worktrees import direct_landing as direct_landing_module
from agents_remember.worktrees import route_review as route_review_module
from agents_remember.worktrees.integration.closeout import (
    curator_coherence as curator_coherence_module,
)
from agents_remember.worktrees.integration.closeout import door as door_module
from agents_remember.worktrees.integration.closeout import door_source as door_source_module
from agents_remember.worktrees.integration.closeout import (
    operation_admission as operation_admission_module,
)
from agents_remember.worktrees.integration.closeout import (
    task_intent_identity as task_intent_identity_module,
)
from agents_remember.worktrees.integration.closeout import (
    task_intent_legacy_census as legacy_census_module,
)
from agents_remember.worktrees.integration.direct_landing.direct_landing_errors import (
    DirectLandingError,
)
from agents_remember.worktrees.integration.legacy import (
    legacy_operation_bridge as legacy_bridge_module,
)
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operation_projection as operation_projection_module,
)
from agents_remember.worktrees.integration.lifecycle import (
    lifecycle_operations as lifecycle_operations_module,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_candidate import (
    LifecycleOperationCandidate,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.queue import closeout_projection as closeout_projection_module
from test_task_intent_consumers_and_legacy import (
    INTENT,
    LEAF,
    MASTER,
    OTHER_INTENT,
    SPRINT,
    _closeout_record_payload,
    _door_payload,
    _leaf_document,
    _master_document,
    _pair,
    _route_review_payload,
    _write_json,
)


def _value(**fields: object) -> Any:
    return cast(Any, SimpleNamespace(**fields))


def test_operation_model_refuses_every_incoherent_task_intent_state() -> None:
    base = {
        "doorPublication": None,
        "doorPublicationHistory": [],
    }
    with pytest.raises(ValueError, match="integrate operations do not carry"):
        operation_model._require_task_intent_state(
            _value(**base, operationKind="integrate", taskIntent=INTENT)
        )
    with pytest.raises(ValueError, match="commit operations require"):
        operation_model._require_task_intent_state(
            _value(**base, operationKind="closeout", taskIntent=None)
        )
    mismatched = _value(generation=_value(taskIntent=OTHER_INTENT))
    with pytest.raises(ValueError, match="operation and door publication"):
        operation_model._require_task_intent_state(
            _value(
                operationKind="closeout",
                taskIntent=INTENT,
                doorPublication=mismatched,
                doorPublicationHistory=[],
            )
        )


@pytest.mark.parametrize("publication_field", ["doorPublication", "doorPublicationHistory"])
def test_operation_model_compares_legacy_intent_to_every_door_publication(
    publication_field: str,
) -> None:
    matching = _value(generation=_value(taskIntent=MissingTaskIntent()))
    values: dict[str, object] = {
        "operationKind": "direct-landing",
        "taskIntent": MissingTaskIntent(),
        "doorPublication": None,
        "doorPublicationHistory": [],
    }
    values[publication_field] = matching if publication_field == "doorPublication" else [matching]
    operation_model._require_task_intent_state(_value(**values))

    mismatched = _value(generation=_value(taskIntent=INTENT))
    values[publication_field] = (
        mismatched if publication_field == "doorPublication" else [mismatched]
    )
    with pytest.raises(ValueError, match="operation and door publication"):
        operation_model._require_task_intent_state(_value(**values))


def test_operation_projection_split_preserves_each_existing_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied_door = _value(state="developer-decision")
    context = operation_projection_module.OperationProjectionContext(door=supplied_door)
    assert (
        operation_projection_module._door_observation(_value(doorPublication=None), None, context)
        is supplied_door
    )

    classified = _value(state="current")
    publication = _value(state="intent")
    monkeypatch.setattr(
        operation_projection_module,
        "classify_door_publication",
        lambda _publication, _contract: classified,
    )
    assert (
        operation_projection_module._door_observation(
            _value(doorPublication=publication),
            cast(Any, object()),
            operation_projection_module.OperationProjectionContext(),
        )
        is classified
    )

    original = ({"state": "original"}, "failure", "guidance")
    assert operation_projection_module._legacy_intent_override(False, *original) == original
    overridden = operation_projection_module._legacy_intent_override(True, *original)
    assert overridden[0] == {
        "state": "lifecycle-operation-task-intent-unavailable",
        "summary": "The legacy operation predates canonical task intent.",
        "nextAction": "retire-and-republish",
    }
    assert overridden[1] == "lifecycle-operation-task-intent-unavailable"

    assert not operation_projection_module._operation_cancellable(
        contract=None,
        intent_unavailable=True,
        legal_controls=[],
    )
    assert operation_projection_module._operation_cancellable(
        contract=cast(Any, object()),
        intent_unavailable=False,
        legal_controls=[{"action": "cancel"}],
    )
    assert not operation_projection_module._operation_cancellable(
        contract=cast(Any, object()),
        intent_unavailable=False,
        legal_controls=[{"action": "recover"}],
    )
    assert not operation_projection_module._operation_cancellable(
        contract=None,
        intent_unavailable=False,
        legal_controls=[],
    )
    assert not operation_projection_module._operation_cancellable(
        contract=None,
        intent_unavailable=False,
        legal_controls=[],
    )
    assert not operation_projection_module._operation_cancellable(
        contract=None,
        intent_unavailable=False,
        legal_controls=[],
    )
    assert not operation_projection_module._operation_cancellable(
        contract=None,
        intent_unavailable=False,
        legal_controls=[],
    )
    assert not operation_projection_module._operation_cancellable(
        contract=None,
        intent_unavailable=False,
        legal_controls=[],
    )


def test_operation_projection_rejects_non_mapping_public_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = LifecycleOperationRecord.model_validate(_closeout_record_payload())
    monkeypatch.setattr(
        operation_projection_module,
        "public_lifecycle_evidence",
        lambda _result: "private result",
    )
    projected = operation_projection_module.operation_projection(record)

    assert projected.status == "incoherent"
    assert projected.result is not None
    assert projected.result["state"] == "lifecycle-projection-incoherent"
    assert projected.result["expected"] == {"result": "public mapping"}
    assert projected.result["observed"] == {"resultType": "str"}
    assert projected.legalControls == []
    assert projected.recommendedAction is None
    assert projected.cancellable is False


@pytest.mark.parametrize(
    "door_intent,journal_intent,expected",
    [
        (
            OTHER_INTENT,
            OTHER_INTENT,
            ("closeout-door-task-intent-stale", "closeout_door.update-provenance"),
        ),
        (
            INTENT,
            OTHER_INTENT,
            ("lifecycle-operation-task-intent-stale", "retire-and-republish"),
        ),
        (INTENT, INTENT, None),
        (
            MissingTaskIntent(),
            INTENT,
            ("closeout-door-task-intent-unavailable", "closeout_door.update-provenance"),
        ),
        (
            INTENT,
            MissingTaskIntent(),
            ("lifecycle-operation-task-intent-unavailable", "retire-and-republish"),
        ),
    ],
)
def test_claimed_direct_landing_replay_requires_current_canonical_intent(
    monkeypatch: pytest.MonkeyPatch,
    door_intent: TaskIntentIdentity | MissingTaskIntent,
    journal_intent: TaskIntentIdentity | MissingTaskIntent,
    expected: tuple[str, str] | None,
) -> None:
    door = _value(taskIntent=door_intent, sprintTaskDocumentRef=SPRINT)
    contract = _value(closeout_door=door)
    current = _value(taskIntent=journal_intent)
    store = _value(read=lambda: current)

    def canonical_intent(_contract: object) -> TaskIntentIdentity:
        if not isinstance(door.taskIntent, TaskIntentIdentity):
            raise TaskIntentError(
                "closeout-door-task-intent-unavailable",
                "the claimed door predates canonical task intent",
                next_action="closeout_door.update-provenance",
            )
        if door.taskIntent != INTENT:
            raise TaskIntentError(
                "closeout-door-task-intent-stale",
                "the claimed door binds a stale canonical task intent",
                next_action="closeout_door.update-provenance",
            )
        return INTENT

    publish = mock.Mock(return_value=current)
    reloaded = cast(Any, object())
    monkeypatch.setattr(direct_landing_module, "current_door_task_intent", canonical_intent)
    monkeypatch.setattr(direct_landing_module, "_same_direct_request", lambda *_args: True)
    monkeypatch.setattr(direct_landing_module, "_require_direct_claim_owner", lambda *_args: None)
    monkeypatch.setattr(direct_landing_module, "_publish_direct_door_proof", publish)
    monkeypatch.setattr(direct_landing_module, "load_contract", lambda _path: reloaded)
    contract.contract_path = Path("/contract.md")

    if expected is not None:
        with pytest.raises(DirectLandingError) as caught:
            direct_landing_module._resume_direct_claim(
                contract,
                store,
                cast(Any, object()),
                door,
            )
        assert (caught.value.status, caught.value.next_action) == expected
        publish.assert_not_called()
        return

    replayed = direct_landing_module._resume_direct_claim(
        contract,
        store,
        cast(Any, object()),
        door,
    )
    assert replayed == (current, reloaded, False, SPRINT)
    publish.assert_called_once_with(contract, store, current)


def test_waiting_direct_landing_task_intent_refusal_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(direct_landing_module, "operation_state_fingerprint", lambda _c: "state")
    candidate = _value(state="state", tree="tree", task_intent=INTENT)
    legacy_door = _value(candidateTree="tree", taskIntent=MissingTaskIntent())
    with pytest.raises(DirectLandingError) as door_missing:
        direct_landing_module._require_waiting_direct_candidate(
            cast(Any, object()), legacy_door, candidate
        )
    assert door_missing.value.status == "closeout-door-task-intent-unavailable"

    with pytest.raises(DirectLandingError) as candidate_stale:
        direct_landing_module._require_waiting_direct_candidate(
            cast(Any, object()),
            _value(candidateTree="tree", taskIntent=OTHER_INTENT),
            candidate,
        )
    assert candidate_stale.value.status == "closeout-door-task-intent-stale"


def test_direct_landing_candidate_translates_current_door_intent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    door = _value(
        disposition="waiting",
        generationId="g1",
        candidateTree="code-tree",
    )
    contract = _value(
        contract_path=Path("/contract.md"),
        memory_repo_path=Path("/memory"),
        ledger_path=Path("/memory/memory.md"),
        closeout_door=door,
        memory_work_branch="memory-branch",
    )
    identity = _value(
        config=_value(config_path=Path("/config.json")),
        contract=contract,
        request=_value(intent_note=" approved "),
        candidate_tree="candidate",
        code_commit="commit",
        effective_input=cast(Any, object()),
    )
    monkeypatch.setattr(direct_landing_module, "_verify_code_commit", lambda *_: "code-tree")
    monkeypatch.setattr(
        direct_landing_module,
        "_direct_memory_admission_snapshot",
        lambda _contract: _value(headRef="memory-head"),
    )
    monkeypatch.setattr(direct_landing_module, "_load_direct_ledger", lambda _path: None)
    monkeypatch.setattr(direct_landing_module, "_read_direct_ledger_text", lambda _path: "ledger")
    monkeypatch.setattr(direct_landing_module, "_gate_policy_snapshot", lambda _config: [])
    monkeypatch.setattr(
        direct_landing_module,
        "DirectLandingOperationInput",
        lambda **_kwargs: cast(Any, object()),
    )
    monkeypatch.setattr(
        direct_landing_module,
        "current_door_task_intent",
        mock.Mock(
            side_effect=TaskIntentError(
                "closeout-door-task-intent-unavailable",
                "legacy door",
                next_action="closeout_door.update-provenance",
            )
        ),
    )
    with pytest.raises(DirectLandingError) as caught:
        direct_landing_module._prepare_direct_landing_candidate(identity, "g1")
    assert caught.value.status == "closeout-door-task-intent-unavailable"
    assert caught.value.next_action == "closeout_door.update-provenance"


def test_curator_coherence_translates_projection_and_currentness_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate_document_at(tmp_path / "tasks" / "repo" / "master" / "leaf.json")
    contract = _value(
        contract_path=tmp_path / "group" / "series-contract.md",
        repo_name="repo",
        task_root=tmp_path / "tasks" / "repo" / "master",
        worktree_group=tmp_path / "group",
    )
    monkeypatch.setattr(curator_coherence_module, "_require_leaf_external_memory", lambda _: None)
    monkeypatch.setattr(
        curator_coherence_module, "resolve_memory_candidate_pair", lambda *_a, **_k: _pair()
    )
    monkeypatch.setattr(
        curator_coherence_module,
        "_task_context",
        lambda _contract: (candidate, candidate, candidate, None),
    )
    monkeypatch.setattr(
        curator_coherence_module,
        "capture_future_code_candidate",
        lambda _contract: _value(codeCandidateTree="a" * 40),
    )
    monkeypatch.setattr(curator_coherence_module, "_memory_candidate_tree", lambda _: "b" * 40)
    monkeypatch.setattr(
        curator_coherence_module,
        "_quality_attestation",
        lambda *_args: (cast(Any, object()), "c" * 64),
    )
    monkeypatch.setattr(
        curator_coherence_module,
        "candidate_task_topology_fingerprint",
        lambda *_args, **_kwargs: "d" * 64,
    )
    monkeypatch.setattr(
        curator_coherence_module,
        "task_intent_identity",
        mock.Mock(side_effect=TaskIntentError("task-intent-schema-unclassified", "bad slot")),
    )
    with pytest.raises(curator_coherence_module.CuratorCoherenceError) as projection:
        curator_coherence_module.observe_curator_coherence_source(contract)
    assert projection.value.status == "task-intent-schema-unclassified"
    assert projection.value.next_action == "task_doc"

    observation = _value(task_intent=INTENT)
    validated = _value(record=_value(taskIntent=OTHER_INTENT))
    monkeypatch.setattr(
        curator_coherence_module, "observe_curator_coherence_source", lambda _: observation
    )
    monkeypatch.setattr(
        curator_coherence_module, "load_curator_coherence_authority", lambda _: validated
    )
    with pytest.raises(curator_coherence_module.CuratorCoherenceError) as current:
        curator_coherence_module.require_current_curator_coherence(contract)
    assert current.value.status == "curator-coherence-task-intent-stale"
    assert current.value.next_action == "publish"


def _candidate_document_at(path: Path, *, kind: str = "subTask") -> ResolvedTaskDocument:
    return ResolvedTaskDocument(
        ref=LEAF,
        path=path,
        document=_leaf_document().model_copy(update={"kind": kind}),
    )


def test_contract_task_intent_candidate_refuses_each_addressing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_root = tmp_path / "tasks" / "repo" / "master"
    task_root.mkdir(parents=True)
    contract = _value(
        coordination_root=tmp_path,
        task_root=task_root,
        kind="leaf",
        leaf_id="L1",
        repo_name="repo",
        closeout_door=None,
    )

    class Topology:
        candidate: ResolvedTaskDocument | None = None
        error: Exception | None = None

        def resolve(self, _ref: object) -> ResolvedTaskDocument:
            if self.error is not None:
                raise self.error
            assert self.candidate is not None
            return self.candidate

        def canonical_ref(self, _repo: str, _path: Path) -> TaskDocumentRef:
            return LEAF

    topology = Topology()
    monkeypatch.setattr(task_intent_identity_module, "TaskDocumentTopology", lambda _: topology)
    monkeypatch.setattr(
        task_intent_identity_module, "resolve_terminal_leaf_doc", lambda *_args: None
    )
    with pytest.raises(TaskIntentError) as missing:
        task_intent_identity_module.contract_task_intent_candidate(contract)
    assert missing.value.status == "task-intent-task-document-missing"

    contract.kind = "series"
    with pytest.raises(TaskIntentError) as series:
        task_intent_identity_module.contract_task_intent_candidate(contract)
    assert series.value.status == "task-intent-candidate-required"

    topology.error = TaskDocumentRefError("task-document-missing", "missing exact ref")
    with pytest.raises(TaskIntentError) as ref_error:
        task_intent_identity_module.contract_task_intent_candidate(contract, candidate_ref=LEAF)
    assert ref_error.value.status == "task-document-missing"

    topology.error = None
    topology.candidate = _candidate_document_at(tmp_path / "outside" / "leaf.json")
    with pytest.raises(TaskIntentError) as outside:
        task_intent_identity_module.contract_task_intent_candidate(contract, candidate_ref=LEAF)
    assert outside.value.status == "task-intent-task-document-outside-root"

    topology.candidate = _candidate_document_at(task_root / "task.json", kind="master")
    with pytest.raises(TaskIntentError) as master:
        task_intent_identity_module.contract_task_intent_candidate(contract, candidate_ref=LEAF)
    assert master.value.status == "task-intent-leaf-required"

    contract.closeout_door = None
    with pytest.raises(TaskIntentError) as door:
        task_intent_identity_module.current_door_task_intent(contract)
    assert door.value.status == "closeout-door-missing"


def test_closeout_door_and_admission_refuse_missing_or_mismatched_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting = CloseoutDoorGeneration.model_validate(_door_payload())
    record = LifecycleOperationRecord.model_validate(
        {**_closeout_record_payload(), "taskIntent": INTENT.model_dump(mode="json", by_alias=True)}
    )
    contract = _value(closeout_door=waiting)
    with pytest.raises(RuntimeError, match="same canonical task intent"):
        door_module.door_generation_for_operation(contract, record, "claimed")

    with pytest.raises(RuntimeError, match="missing canonical task intent"):
        operation_admission_module._current_operation_task_intent(record, None)

    request = door_source_module.CloseoutDoorRequest(
        action="defer",
        contract_path="/contract.md",
        expected_generation_id=waiting.generationId,
    )
    with pytest.raises(door_source_module.CloseoutQueueError) as transition:
        door_source_module._transitioned_generation(waiting, request)
    assert transition.value.status == "closeout-door-task-intent-unavailable"

    monkeypatch.setattr(
        door_source_module,
        "task_intent_identity",
        mock.Mock(side_effect=TaskIntentError("task-intent-schema-unclassified", "bad slot")),
    )
    with pytest.raises(door_source_module.CloseoutQueueError) as projected:
        door_source_module._door_task_intent(_value(task_root=Path("/tasks")), cast(Any, object()))
    assert projected.value.status == "task-intent-schema-unclassified"


def test_lifecycle_store_legacy_retirement_failure_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = LifecycleOperationRecord.model_validate(_closeout_record_payload())
    successor = LifecycleOperationRecord.model_validate(
        {
            **_closeout_record_payload(status="queued"),
            "taskIntent": INTENT.model_dump(mode="json", by_alias=True),
            "fingerprint": "4" * 64,
            "operationKey": "5" * 64,
        }
    )
    absent = LifecycleOperationStore(tmp_path / "absent.json")
    with pytest.raises(RuntimeError, match="bytes are unreadable"):
        absent._retire_missing_intent_generation(missing, successor)

    path = tmp_path / "closeout-operation.json"
    path.write_text("legacy bytes\n", encoding="utf-8")
    store = LifecycleOperationStore(path)
    archive = tmp_path / "closeout-operation.legacy-missing-intent-generation-1.json"
    archive.write_text("contradictory bytes\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="archive contradicts"):
        store._retire_missing_intent_generation(missing, successor)

    archive.unlink()
    monkeypatch.setattr(store, "_write", mock.Mock(side_effect=RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        store._retire_missing_intent_generation(missing, successor)
    assert not archive.exists()

    archive.write_text("legacy bytes\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="write failed"):
        store._retire_missing_intent_generation(missing, successor)
    assert archive.read_text(encoding="utf-8") == "legacy bytes\n"


def test_lifecycle_closeout_candidate_and_legacy_replacement_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_input = cast(CloseoutOperationInput, object())
    monkeypatch.setattr(
        lifecycle_operations_module, "closeout_contract_sha256", lambda _contract: "state"
    )
    candidate = LifecycleOperationCandidate(
        state="state", tree="tree", fingerprint="f" * 64, task_intent=INTENT
    )
    missing_door = _value(candidateTree="tree", taskIntent=MissingTaskIntent())
    with pytest.raises(lifecycle_operations_module.LifecycleControlError) as missing:
        lifecycle_operations_module._require_waiting_closeout_candidate(
            cast(Any, object()), operation_input, candidate, missing_door
        )
    assert missing.value.status == "closeout-door-task-intent-unavailable"

    stale_door = _value(candidateTree="tree", taskIntent=OTHER_INTENT)
    with pytest.raises(lifecycle_operations_module.LifecycleControlError) as stale:
        lifecycle_operations_module._require_waiting_closeout_candidate(
            cast(Any, object()), operation_input, candidate, stale_door
        )
    assert stale.value.status == "closeout-door-task-intent-stale"

    active = LifecycleOperationRecord.model_validate(_closeout_record_payload(status="running"))
    active_store = _value(create=lambda _queued, *, initial_certification=None: (active, False))
    with pytest.raises(lifecycle_operations_module.LifecycleControlError) as active_refusal:
        lifecycle_operations_module._create_or_replace_generation(
            active_store,
            cast(LifecycleOperationRecord, object()),
            creation=lifecycle_operations_module._GenerationCreation(
                contract=cast(Any, object()),
                operation_input=cast(Any, object()),
                candidate=candidate,
            ),
        )
    assert active_refusal.value.status == ("lifecycle-operation-task-intent-unavailable")

    terminal = LifecycleOperationRecord.model_validate(_closeout_record_payload(status="failed"))
    replacement = cast(LifecycleOperationRecord, object())
    terminal_store = _value(
        create=lambda _queued, *, initial_certification=None: (terminal, False),
        replace_terminal=mock.Mock(return_value=replacement),
    )
    assert lifecycle_operations_module._create_or_replace_generation(
        terminal_store,
        replacement,
        creation=lifecycle_operations_module._GenerationCreation(
            contract=cast(Any, object()),
            operation_input=cast(Any, object()),
            candidate=candidate,
        ),
    ) == (replacement, True)
    terminal_store.replace_terminal.assert_called_once_with(replacement, initial_certification=None)


def test_legacy_bridge_preserves_task_intent_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_input = _value(
        configPath="/config.json",
        contractPath="/contract.md",
        approvalNote="approved",
        codeCommitMessage="code",
    )
    commits = _value(codeCommit="a" * 40)
    monkeypatch.setattr(
        legacy_bridge_module,
        "_require_migratable_closeout",
        lambda *_args: (legacy_input, commits, Path("/repo"), "refs/heads/main", "b" * 40),
    )
    monkeypatch.setattr(legacy_bridge_module, "_legacy_gate_policy", lambda _input: [])
    monkeypatch.setattr(legacy_bridge_module, "_stamp", lambda: "2026-09-01T00:00:00+00:00")
    monkeypatch.setattr(
        legacy_bridge_module,
        "contract_task_intent",
        mock.Mock(
            side_effect=TaskIntentError(
                "task-intent-task-document-missing",
                "missing task",
                next_action="task_doc",
            )
        ),
    )
    legacy = _value(
        operationKey="1" * 64,
        fingerprint="2" * 64,
        candidateState="3" * 64,
        candidateTree="b" * 40,
        taskId="L1",
        taskName="master",
        contractPath="/contract.md",
        queuedAt="2026-09-01T00:00:00+00:00",
        startedAt=None,
        heartbeatAt=None,
        reportPath="/report.md",
    )
    values = legacy_bridge_module._MigrationValues(
        original=b"legacy",
        digest=sha256(b"legacy").hexdigest(),
        memory_commit_message="memory",
        ledger_commit_message="ledger",
        audit_reason="approved migration",
    )
    with pytest.raises(legacy_bridge_module.LegacyBridgeError) as caught:
        legacy_bridge_module._migrated_closeout_record(
            _value(memory_mode="external"), legacy, values
        )
    assert caught.value.status == "task-intent-task-document-missing"
    assert caught.value.next_action == "task_doc"


def test_projection_member_translates_task_intent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate_document_at(tmp_path / "tasks" / "repo" / "master" / "leaf.json")
    master = replace(candidate, ref=MASTER)
    sprint = replace(candidate, ref=SPRINT)
    door = _value()
    contract = _value(task_root=tmp_path)
    source = closeout_projection_module._DoorSource(
        address=tmp_path / "series-contract.md", contract=contract, door=door
    )
    tasks = _value(sprint=sprint)
    doors = _value(
        waiting=((source, candidate, master, 1),),
        activation_waiting={},
    )
    evidence = _value(fingerprint_fact=lambda _contract: {"fingerprint": "f" * 64})
    monkeypatch.setattr(
        closeout_projection_module, "capture_door_candidate_evidence", lambda *_: evidence
    )
    monkeypatch.setattr(
        closeout_projection_module, "door_candidate_evidence_blockers", lambda *_: []
    )
    monkeypatch.setattr(
        closeout_projection_module,
        "scheduling_source_fact",
        lambda *_: ([], {"state": "ready"}),
    )
    monkeypatch.setattr(
        closeout_projection_module,
        "semantic_topology_source_fact",
        lambda *_args, **_kwargs: ("a" * 64, {"state": "current"}),
    )
    monkeypatch.setattr(
        closeout_projection_module,
        "task_intent_identity",
        mock.Mock(side_effect=TaskIntentError("task-intent-schema-unclassified", "bad slot")),
    )
    with pytest.raises(closeout_projection_module._ProjectionSourceRefusal) as caught:
        closeout_projection_module._projection_members(
            tasks, doors, graph=None, grade_authority=cast(Any, object())
        )
    assert caught.value.problem.errorType == "task-intent-schema-unclassified"
    assert caught.value.problem.repairAction == "bad slot"


def test_route_review_translates_task_intent_and_confinement_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_root = tmp_path / "tasks" / "repo" / "master"
    task_root.mkdir(parents=True)
    candidate = _candidate_document_at(task_root / "leaf.json")
    contract = _value(
        kind="leaf",
        task_root=task_root,
        coordination_root=tmp_path,
        repo_name="repo",
        leaf_id="L1",
    )
    monkeypatch.setattr(
        route_review_module,
        "task_intent_identity",
        mock.Mock(side_effect=TaskIntentError("task-intent-schema-unclassified", "bad slot")),
    )
    review_payload = {
        key: value
        for key, value in _route_review_payload().items()
        if key in {"verdict", "verdictRef", "routes"}
    }
    with pytest.raises(route_review_module.RouteReviewError) as build:
        route_review_module.build_route_review(contract, candidate, review_payload)
    assert build.value.status == "task-intent-schema-unclassified"

    review = RouteReviewRecord.model_validate(
        {**_route_review_payload(), "taskIntent": INTENT.model_dump(mode="json", by_alias=True)}
    )
    document = _leaf_document().model_copy(update={"routeReview": review})
    monkeypatch.setattr(route_review_module, "code_change_present", lambda _contract: True)
    monkeypatch.setattr(
        route_review_module,
        "resolve_terminal_leaf_doc",
        lambda *_args: (task_root / "leaf.json", document),
    )
    monkeypatch.setattr(route_review_module, "code_candidate_tree", lambda _contract: "a" * 40)
    with pytest.raises(route_review_module.RouteReviewError) as current:
        route_review_module.require_current_route_review(contract)
    assert current.value.status == "task-intent-schema-unclassified"

    with pytest.raises(route_review_module.RouteReviewError) as outside:
        route_review_module.document_ref(contract, tmp_path / "outside" / "leaf.json")
    assert outside.value.status == "route-review-task-document-outside-root"


def test_route_review_task_intent_helper_refuses_a_missing_review(tmp_path: Path) -> None:
    candidate = _candidate_document_at(tmp_path / "tasks" / "repo" / "master" / "leaf.json")

    with pytest.raises(route_review_module.RouteReviewError) as caught:
        route_review_module.require_current_route_review_task_intent(
            _value(task_root=tmp_path), candidate
        )

    assert caught.value.status == "route-review-required"


def _legacy_counts() -> Any:
    return {
        record_class: [0, 0] for record_class in legacy_census_module.LEGACY_INTENT_RECORD_CLASSES
    }


def test_legacy_census_internal_failure_and_bounded_input_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValueError, match="enumerate every record class"):
        legacy_census_module.TaskIntentLegacyCensus(classes=())

    unreadable: list[str] = []
    missing_root = tmp_path / "missing"
    assert (
        legacy_census_module._bounded_files(
            missing_root, "*.json", unreadable, owner="curator-coherence"
        )
        == ()
    )
    assert legacy_census_module._task_document_files(missing_root, unreadable) == ()

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    assert (
        legacy_census_module._json_object(invalid_json, unreadable, owner="lifecycle-operation")
        is None
    )
    not_object = tmp_path / "list.json"
    not_object.write_text("[]\n", encoding="utf-8")
    assert (
        legacy_census_module._json_object(not_object, unreadable, owner="lifecycle-operation")
        is None
    )

    bounded = tmp_path / "bounded.json"
    bounded.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(legacy_census_module, "_MAX_CENSUS_FILE_BYTES", 1)
    with pytest.raises(ValueError, match="bounded size"):
        legacy_census_module._bounded_text(bounded)

    problem_limit = legacy_census_module._MAX_CENSUS_PROBLEMS
    monkeypatch.setattr(legacy_census_module, "_MAX_CENSUS_PROBLEMS", 1)
    capped = ["existing"]
    legacy_census_module._problem(capped, "owner", bounded, "ignored")
    assert capped == ["existing"]
    monkeypatch.setattr(legacy_census_module, "_MAX_CENSUS_PROBLEMS", problem_limit)

    counts = _legacy_counts()
    intent_problems: list[str] = []
    legacy_census_module._count_intent(
        "not-an-object", bounded, "route-review", counts, intent_problems
    )
    legacy_census_module._count_intent(
        {"taskIntent": {"schema": "task-intent/v1", "digest": "invalid"}},
        bounded,
        "route-review",
        counts,
        intent_problems,
    )
    assert any("container-not-object" in item for item in intent_problems)
    assert any("task-intent-invalid" in item for item in intent_problems)


def test_legacy_census_canonical_task_owner_edge_matrix(tmp_path: Path) -> None:
    unreadable: list[str] = []

    malformed = tmp_path / "malformed" / "task.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("{", encoding="utf-8")
    assert legacy_census_module._declared_task_document_files(malformed, unreadable) == ()

    unsupported = tmp_path / "unsupported" / "task.json"
    _write_json(unsupported, [])
    assert legacy_census_module._declared_task_document_files(unsupported, unreadable) == ()

    invalid = tmp_path / "invalid" / "task.json"
    invalid_payload = _master_document("leaf.md").model_dump(mode="json", by_alias=True)
    invalid_payload["kind"] = "future"
    _write_json(invalid, invalid_payload)
    assert legacy_census_module._declared_task_document_files(invalid, unreadable) == ()
    assert any("task-document-invalid" in item for item in unreadable)

    nonmaster = tmp_path / "nonmaster" / "task.json"
    _write_json(nonmaster, _leaf_document().model_dump(mode="json", by_alias=True))
    assert legacy_census_module._declared_task_document_files(nonmaster, unreadable) == ()

    master = tmp_path / "declared" / "task.json"
    master.parent.mkdir(parents=True)
    assert legacy_census_module._declared_task_document_path(master, "", unreadable) is None
    assert (
        legacy_census_module._declared_task_document_path(master, "nested/leaf.md", unreadable)
        is None
    )
    assert any("subtask-ref-invalid" in item for item in unreadable)

    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (master.parent / "leaf.json").symlink_to(outside)
    assert legacy_census_module._declared_task_document_path(master, "leaf.md", unreadable) is None
    assert any("subtask-ref-unconfined" in item for item in unreadable)


def test_legacy_census_scanners_classify_every_malformed_current_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = tmp_path / "tasks"
    counts = _legacy_counts()
    unreadable: list[str] = []

    route_root = tasks / "repo" / "master"
    _write_json(
        route_root / "task.json",
        _master_document(
            "no-review.md",
            "broken.md",
            "unsupported.md",
        ).model_dump(mode="json", by_alias=True),
    )
    _write_json(route_root / "no-review.json", {"schema": "ar-task-document/v1"})
    (route_root / "broken.json").write_text("{", encoding="utf-8")
    _write_json(route_root / "unsupported.json", {"schema": "ar-task-document/future"})
    legacy_census_module._scan_route_reviews(tasks, counts, unreadable)

    reports = route_root / "notes" / "reports"
    reports.mkdir(parents=True)
    (reports / "broken-curator-coherence.json").write_text("{", encoding="utf-8")
    _write_json(
        reports / "unsupported-curator-coherence.json",
        {"schemaVersion": "future", "recordPath": "record.json"},
    )
    _write_json(
        reports / "missing-ref-curator-coherence.json",
        {"schemaVersion": "ar-curator-coherence-authority/v1"},
    )
    _write_json(
        reports / "unconfined-curator-coherence.json",
        {
            "schemaVersion": "ar-curator-coherence-authority/v1",
            "recordPath": "/outside.json",
        },
    )
    _write_json(
        reports / "missing-record-curator-coherence.json",
        {
            "schemaVersion": "ar-curator-coherence-authority/v1",
            "recordPath": "notes/reports/missing.json",
        },
    )
    legacy_census_module._scan_curator_coherence(tasks, counts, unreadable)

    doors = route_root / "doors"
    no_cell = doors / "no-cell" / "series-contract.md"
    no_cell.parent.mkdir(parents=True)
    no_cell.write_text("---\n---\n", encoding="utf-8")
    ambiguous = doors / "ambiguous" / "series-contract.md"
    ambiguous.parent.mkdir(parents=True)
    ambiguous.write_text("  closeout_door: {}\n  closeout_door: {}\n", encoding="utf-8")
    invalid = doors / "invalid" / "series-contract.md"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("  closeout_door: {\n", encoding="utf-8")
    unreadable_door = doors / "unreadable" / "series-contract.md"
    unreadable_door.parent.mkdir(parents=True)
    unreadable_door.write_text("bytes", encoding="utf-8")
    real_bounded_text = legacy_census_module._bounded_text

    def bounded_text(path: Path) -> str:
        if path == unreadable_door:
            raise OSError("unreadable")
        return real_bounded_text(path)

    monkeypatch.setattr(legacy_census_module, "_bounded_text", bounded_text)
    legacy_census_module._scan_closeout_doors(tasks, counts, unreadable)

    worktrees = tmp_path / "worktrees"
    broken_operation = worktrees / "broken" / ".lifecycle" / "closeout-operation.json"
    broken_operation.parent.mkdir(parents=True)
    broken_operation.write_text("{", encoding="utf-8")
    _write_json(
        worktrees / "future" / ".lifecycle" / "direct-landing-operation.json",
        {"schemaVersion": "future", "operationKind": "direct-landing"},
    )
    legacy_census_module._scan_lifecycle_operations(worktrees, counts, unreadable)
    legacy_census_module._scan_lifecycle_operations(tmp_path / "no-worktrees", counts, unreadable)

    expected_markers = {
        "route-review": ["JSONDecodeError", "unsupported-task-document"],
        "curator-coherence": [
            "unsupported-authority",
            "record-path-missing",
            "record-path-unconfined",
        ],
        "closeout-door": ["OSError", "door-cell-ambiguous", "JSONDecodeError"],
        "lifecycle-operation": ["JSONDecodeError", "unsupported-current-record"],
    }
    for owner, markers in expected_markers.items():
        assert all(
            any(item.startswith(f"{owner}:") and marker in item for item in unreadable)
            for marker in markers
        )
