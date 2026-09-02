"""Failure, identity, and publication edges for curator-coherence authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from agents_remember.application import curator_coherence as application
from agents_remember.application.lifecycle.configured_contract_admission import (
    ConfiguredContractRefused,
)
from agents_remember.application.memory_quality import controller
from agents_remember.errors import (
    CuratorCoherenceError,
    CuratorCoherencePairError,
    FutureCodeCandidateError,
    MemoryCandidatePairError,
    MemoryCandidatePairFailure,
)
from agents_remember.mcp.registration import tasks as task_registration
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.curator_coherence import (
    CuratorCoherenceJudgment,
    CuratorCoherenceRecord,
    CuratorCoherenceRecordedJudgment,
    CuratorCoherenceRequest,
    CuratorCoherenceResponse,
    CuratorQualityAttestation,
    CuratorSourceCandidate,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.tasks.document_refs import TaskDocumentRefError
from agents_remember.worktrees.integration.closeout import curator_coherence as coherence
from agents_remember.worktrees.integration.closeout import (
    curator_coherence_judgments as judgments,
)
from agents_remember.worktrees.integration.closeout import (
    curator_coherence_publication as publication,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract
from pydantic import ValidationError
from test_closeout_queue import MASTER_A, SPRINT, QueueFixture


def _value(**fields: object) -> Any:
    return cast(Any, SimpleNamespace(**fields))


def _source(name: str = "a.py") -> CuratorSourceCandidate:
    return CuratorSourceCandidate(
        sourceFile=f"mcp/{name}",
        onboardingFile=f"onboarding/{name}.md",
        classification="drifted",
    )


def _pair() -> MemoryCandidatePairIdentity:
    return MemoryCandidatePairIdentity(
        repoId="repo",
        contractPath="/coordination/tasks/repo/task/enclosures/l1/series-contract.md",
        contractDigest="9" * 64,
        codeRoot="/code/worktree",
        memoryRoot="/memory/worktree",
        codeSourceBranch="super",
        codeWorkBranch="leaf-code",
        codeBaseCommit="a" * 40,
        memorySourceBranch="memory-super",
        memoryWorkBranch="leaf-memory",
        memoryBaseCommit="b" * 40,
        onboardingRoot="/memory/worktree/onboarding",
        ledgerPath="/memory/worktree/ledger.jsonl",
    )


def _recorded(candidate: CuratorSourceCandidate) -> CuratorCoherenceRecordedJudgment:
    return CuratorCoherenceRecordedJudgment(
        **candidate.model_dump(mode="json"),
        disposition="reconciled",
        rationale="The exact candidate was reconciled.",
        evidenceRef="task:notes/evidence.md",
        evidenceSha256="e" * 64,
    )


def _record_fields() -> dict[str, object]:
    candidate = _source()
    return {
        "leafId": "L1",
        "contractPath": "/coordination/tasks/repo/task/enclosures/l1/series-contract.md",
        "taskDocumentRef": {"repository": "repo", "path": "master/leaf.json"},
        "semanticRequirementRevision": "R1@v1",
        "deliveryAttempt": "A001",
        "pairIdentity": _pair().model_dump(mode="json"),
        "codeCandidateTree": "a" * 40,
        "memoryCandidateTree": "b" * 40,
        "taskTopologyFingerprint": "c" * 64,
        "taskIntent": {"schema": "task-intent/v1", "digest": "9" * 64},
        "attestationPath": "/worktree/reports/curator-memory-quality.json",
        "attestationSha256": "d" * 64,
        "attestationReportSha256": "e" * 64,
        "sourceCandidates": [candidate.model_dump(mode="json")],
        "judgments": [_recorded(candidate).model_dump(mode="json")],
        "publicationFingerprint": "f" * 64,
        "publishedBy": "curator@repo/master/leaf.json",
        "reportSha256": "1" * 64,
    }


def _attestation(
    candidates: list[CuratorSourceCandidate] | None = None,
) -> CuratorQualityAttestation:
    selected = candidates or []
    return CuratorQualityAttestation.model_validate(
        {
            "schema": "ar-curator-memory-quality/v1",
            "checklistStatus": "ready-for-closeout",
            "curatorActionableCount": 0,
            "memoryRepairCount": 0,
            "missingOnboardingCount": 0,
            "staleRouteIndexCount": 0,
            "sourceChangeCandidateCount": len(selected),
            "sourceChangeCandidates": [item.model_dump(mode="json") for item in selected],
            "pairIdentity": _pair().model_dump(mode="json"),
            "onboardingRoot": "/memory/onboarding",
            "reportPath": "/reports/curator-memory-quality.md",
            "reportSha256": "e" * 64,
        }
    )


def _observation(tmp_path: Path, *, candidate_ref: TaskDocumentRef | None = None) -> Any:
    return coherence.CuratorCoherenceObservation(
        candidate=_value(
            ref=candidate_ref or TaskDocumentRef(repository="repo", path="master/leaf.json")
        ),
        pair_identity=_pair(),
        code_candidate_tree="a" * 40,
        memory_candidate_tree="b" * 40,
        task_topology_fingerprint="c" * 64,
        task_intent=TaskIntentIdentity(digest="9" * 64),
        attestation_path=tmp_path / "curator-memory-quality.json",
        attestation_sha256="d" * 64,
        quality_report_path=tmp_path / "curator-memory-quality.md",
        attestation=_attestation(),
    )


def _quality_source(
    contract: WorktreeContract,
    attestation: Path,
    report: Path,
    pair_identity: MemoryCandidatePairIdentity,
) -> coherence._QualityAttestationSource:
    return coherence._QualityAttestationSource(
        attestation_path=attestation,
        report_path=report,
        pair_identity=pair_identity,
        code_candidate_tree=coherence.capture_future_code_candidate(contract).codeCandidateTree,
        memory_candidate_tree=coherence._memory_candidate_tree(contract),
    )


def _request_for(
    contract: WorktreeContract,
    *,
    attempt: str,
    predecessor: str | None = None,
    caller: DeclaredCaller | None = None,
) -> CuratorCoherenceRequest:
    prepared = publication.curator_coherence_action(
        contract,
        CuratorCoherenceRequest(
            action="prepare",
            contract_path=contract.contract_path.as_posix(),
        ),
    )
    return CuratorCoherenceRequest(
        action="publish",
        contract_path=contract.contract_path.as_posix(),
        semantic_requirement_revision="MCAR-R02@v1",
        delivery_attempt=attempt,
        judgments=[],
        expected_predecessor_digest=(
            str(prepared["predecessorAuthorityDigest"]) if predecessor is None else predecessor
        ),
        expected_code_candidate_tree=str(prepared["codeCandidateTree"]),
        expected_memory_candidate_tree=str(prepared["memoryCandidateTree"]),
        expected_task_topology_fingerprint=str(prepared["taskTopologyFingerprint"]),
        expected_task_intent=TaskIntentIdentity.model_validate(prepared["taskIntent"]),
        expected_attestation_sha256=str(prepared["attestationSha256"]),
        caller=caller or DeclaredCaller(role="architect", task_document_ref=SPRINT),
    )


def test_models_refuse_blank_mismatched_and_duplicate_authority_inputs() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        CuratorSourceCandidate(
            sourceFile=" ", onboardingFile="onboarding/a.md", classification="drifted"
        )

    attestation = _attestation().model_dump(mode="json", by_alias=True)
    attestation["sourceChangeCandidateCount"] = 1
    with pytest.raises(ValidationError, match="does not match"):
        CuratorQualityAttestation.model_validate(attestation)

    with pytest.raises(ValidationError, match="must not be blank"):
        CuratorCoherenceJudgment(
            **_source().model_dump(mode="json"),
            disposition="reconciled",
            rationale=" ",
            evidenceRef="task:notes/evidence.md",
        )

    fields = _record_fields()
    fields["sourceCandidates"] = [
        _source().model_dump(mode="json"),
        _source().model_dump(mode="json"),
    ]
    with pytest.raises(ValidationError, match="source candidates must be unique"):
        CuratorCoherenceRecord.model_validate(fields)

    first, second = _source("a.py"), _source("b.py")
    fields = _record_fields()
    fields["sourceCandidates"] = [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ]
    fields["judgments"] = [
        _recorded(first).model_dump(mode="json"),
        _recorded(first).model_dump(mode="json"),
    ]
    with pytest.raises(ValidationError, match="judgments must be unique"):
        CuratorCoherenceRecord.model_validate(fields)

    fields["judgments"] = [_recorded(first).model_dump(mode="json")]
    with pytest.raises(ValidationError, match="exactly cover"):
        CuratorCoherenceRecord.model_validate(fields)


def test_request_and_response_shapes_are_total_and_action_specific() -> None:
    request = CuratorCoherenceRequest(
        action="status",
        contract_path=" /coordination/contract.md ",
        semantic_requirement_revision=None,
        delivery_attempt=None,
    )
    assert request.contract_path == "/coordination/contract.md"
    with pytest.raises(ValidationError, match="contract_path must not be blank"):
        CuratorCoherenceRequest(action="status", contract_path=" ")
    with pytest.raises(ValidationError, match="must not be blank"):
        CuratorCoherenceRequest(
            action="publish",
            contract_path="/coordination/contract.md",
            semantic_requirement_revision=" ",
        )
    with pytest.raises(ValidationError, match="publish requires every"):
        CuratorCoherenceRequest(action="publish", contract_path="/coordination/contract.md")
    with pytest.raises(ValidationError, match="only publish may freeze"):
        CuratorCoherenceRequest(
            action="validate", contract_path="/coordination/contract.md", freeze_snapshot=True
        )

    response = {
        "ok": True,
        "action": "status",
        "state": "current",
        "summary": "current",
        "contractPath": "/coordination/contract.md",
    }
    assert CuratorCoherenceResponse.model_validate(response).status is None
    with pytest.raises(ValidationError, match="cannot carry refusal"):
        CuratorCoherenceResponse.model_validate(
            {**response, "status": "unexpected", "detail": "unexpected"}
        )
    with pytest.raises(ValidationError, match="requires typed refusal"):
        CuratorCoherenceResponse.model_validate({**response, "ok": False})


def test_application_projects_domain_and_post_execution_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = CuratorCoherenceRequest(action="status", contract_path="/coordination/contract.md")
    accepted = _value(contract=object())
    monkeypatch.setattr(
        application,
        "admit_configured_contract",
        lambda *_args, **_kwargs: accepted,
    )
    monkeypatch.setattr(
        application,
        "execute_configured_contract_operation",
        lambda _accepted, operation: operation(),
    )
    monkeypatch.setattr(
        application,
        "curator_coherence_action",
        lambda *_args: (_ for _ in ()).throw(
            CuratorCoherencePairError(
                MemoryCandidatePairError(
                    "curator-coherence-stale",
                    "stale",
                    failure=MemoryCandidatePairFailure(
                        field="pairIdentity",
                        contract_path="/coordination/contract.md",
                        expected={"candidate": "old"},
                        observed={"candidate": "new"},
                        next_action="prepare",
                        next_args={"contract_path": "/coordination/contract.md"},
                    ),
                )
            )
        ),
    )
    result = application.curator_coherence_tool(cast(Any, object()), request)
    assert result["status"] == "curator-coherence-stale"
    assert result["pairField"] == "pairIdentity"
    assert result["expected"] == {"candidate": "old"}
    assert result["observed"] == {"candidate": "new"}
    assert result["nextArgs"] == {"contract_path": "/coordination/contract.md"}

    expected_only = application._domain_refusal(
        request,
        CuratorCoherenceError("expected-only", "expected", expected={"candidate": "old"}),
    )
    observed_only = application._domain_refusal(
        request,
        CuratorCoherenceError("observed-only", "observed", observed={"candidate": "new"}),
    )
    assert "observed" not in expected_only
    assert "expected" not in observed_only

    refusal = ConfiguredContractRefused(
        reason="authority-invalid",
        status="configured-contract-authority-invalid",
        detail="authority changed",
        expected={"repository": "repo"},
        observed={"repository": "other"},
    )
    monkeypatch.setattr(
        application,
        "execute_configured_contract_operation",
        lambda *_args: refusal,
    )
    projected = application.curator_coherence_tool(cast(Any, object()), request)
    assert projected["status"] == "configured-contract-authority-invalid"
    assert projected["expected"] == {"repository": "repo"}
    assert projected["observed"] == {"repository": "other"}

    with mock.patch.object(
        application,
        "project_configured_contract_refusal",
        return_value={
            "status": "configured-contract-authority-invalid",
            "detail": "authority changed",
            "observed": {"repository": "other"},
        },
    ):
        observed_projection = application._configured_refusal(request, refusal)
    assert "expected" not in observed_projection
    assert observed_projection["observed"] == {"repository": "other"}


def test_memory_quality_projects_missing_contract_and_current_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(RuntimeError, match="lost its leaf contract"):
        controller._attach_coherence_readiness(
            _value(contract=None), {"checklistStatus": "ready-for-closeout"}
        )

    contract = object()
    canonical = tmp_path / "coherence.json"
    monkeypatch.setattr(
        controller,
        "curator_coherence_paths",
        lambda _contract: _value(canonical=canonical),
    )
    monkeypatch.setattr(
        controller,
        "require_current_curator_coherence",
        lambda _contract: _value(record_digest="a" * 64),
    )
    response: dict[str, object] = {"ok": True, "checklistStatus": "ready-for-closeout"}
    controller._attach_coherence_readiness(_value(contract=contract), response)
    assert response["coherenceStatus"] == "current"
    assert response["coherenceRecordDigest"] == "a" * 64
    assert response["closeoutReady"] is True


def test_registered_curator_tool_delegates_to_the_public_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered: list[Any] = []

    class _Server:
        def tool(self):
            def decorator(function):
                registered.append(function)
                return function

            return decorator

    request = CuratorCoherenceRequest(action="status", contract_path="/coordination/contract.md")
    config = cast(Any, object())
    monkeypatch.setattr(
        task_registration,
        "curator_coherence_payload",
        lambda received_config, received_request: {
            "config": received_config,
            "request": received_request,
        },
    )
    task_registration._register_curator_coherence_tools(cast(Any, _Server()), config)
    assert registered[0](request) == {"config": config, "request": request}


def test_source_capture_translates_future_and_generic_candidate_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    contract = _value(
        kind="leaf",
        memory_mode="external",
        memory_worktree=tmp_path / "memory",
        contract_path=tmp_path / "contract.md",
        repo_name="repo",
    )
    monkeypatch.setattr(coherence, "_task_context", lambda _contract: (None, None, None, None))
    pair_error = MemoryCandidatePairError(
        "memory-candidate-pair-stale",
        "pair changed",
        failure=MemoryCandidatePairFailure(
            field="pairIdentity",
            contract_path="/contract",
        ),
    )
    monkeypatch.setattr(
        coherence,
        "resolve_memory_candidate_pair",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(pair_error),
    )
    with pytest.raises(CuratorCoherencePairError) as pair_refusal:
        coherence.observe_curator_coherence_source(contract)
    assert pair_refusal.value.status == "memory-candidate-pair-stale"

    monkeypatch.setattr(
        coherence, "resolve_memory_candidate_pair", lambda *_args, **_kwargs: _pair()
    )
    monkeypatch.setattr(
        coherence,
        "capture_future_code_candidate",
        lambda _contract: (_ for _ in ()).throw(
            FutureCodeCandidateError("future-code-candidate-stale", "stale code")
        ),
    )
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence.observe_curator_coherence_source(contract)
    assert raised.value.status == "future-code-candidate-stale"

    monkeypatch.setattr(
        coherence,
        "capture_future_code_candidate",
        lambda _contract: _value(codeCandidateTree="a" * 40),
    )
    monkeypatch.setattr(
        coherence,
        "_memory_candidate_tree",
        lambda _contract: (_ for _ in ()).throw(OSError("unreadable")),
    )
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence.observe_curator_coherence_source(contract)
    assert raised.value.status == "curator-coherence-candidate-unreadable"


def test_authority_loader_rejects_wrong_paths_bytes_digests_and_projection(tmp_path: Path) -> None:
    fixture = QueueFixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    paths = coherence.curator_coherence_paths(contract)
    canonical_bytes = paths.canonical.read_bytes()
    authority = json.loads(canonical_bytes)
    record_path = contract.task_root / authority["recordPath"]
    report_path = contract.task_root / authority["reportPath"]
    record_bytes = record_path.read_bytes()
    report_bytes = report_path.read_bytes()

    with (
        mock.patch.object(Path, "read_bytes", side_effect=OSError("unreadable")),
        pytest.raises(CuratorCoherenceError) as raised,
    ):
        coherence.current_curator_coherence_predecessor(contract)
    assert raised.value.status == "curator-coherence-authority-unreadable"

    wrong_path = {**authority, "recordPath": "notes/reports/wrong/record.json"}
    paths.canonical.write_text(json.dumps(wrong_path), encoding="utf-8")
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence.load_curator_coherence_authority(contract)
    assert raised.value.status == "curator-coherence-authority-path-invalid"

    paths.canonical.write_bytes(canonical_bytes)
    record_path.unlink()
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence.load_curator_coherence_authority(contract)
    assert raised.value.status == "curator-coherence-generation-unreadable"
    record_path.write_bytes(record_bytes)

    record = json.loads(record_bytes)
    record["publishedBy"] = "curator@repo/other.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence.load_curator_coherence_authority(contract)
    assert raised.value.status == "curator-coherence-record-digest-mismatch"
    record_path.write_bytes(record_bytes)

    report_path.write_text("# altered projection\n", encoding="utf-8")
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence.load_curator_coherence_authority(contract)
    assert raised.value.status == "curator-coherence-projection-digest-mismatch"
    report_path.write_bytes(report_bytes)

    wrong_identity = {**authority, "leafId": "OTHER"}
    paths.canonical.write_text(json.dumps(wrong_identity), encoding="utf-8")
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence.load_curator_coherence_authority(contract)
    assert raised.value.status == "curator-coherence-authority-identity-mismatch"
    paths.canonical.write_bytes(canonical_bytes)


def test_current_validator_rejects_source_and_task_identity_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = QueueFixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    observation = coherence.observe_curator_coherence_source(contract)
    validated = coherence.load_curator_coherence_authority(contract)
    monkeypatch.setattr(coherence, "load_curator_coherence_authority", lambda _contract: validated)
    monkeypatch.setattr(
        coherence,
        "observe_curator_coherence_source",
        lambda _contract: replace(observation, code_candidate_tree="f" * 40),
    )
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence.require_current_curator_coherence(contract)
    assert raised.value.status == "curator-coherence-stale"

    wrong_ref = TaskDocumentRef(repository=contract.repo_name, path="master-a/other.json")
    monkeypatch.setattr(
        coherence,
        "observe_curator_coherence_source",
        lambda _contract: replace(observation, candidate=_value(ref=wrong_ref)),
    )
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence.require_current_curator_coherence(contract)
    assert raised.value.status == "curator-coherence-task-mismatch"

    wrong_record = validated.record.model_copy(update={"leafId": "OTHER"})
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence._require_record_identity(contract, wrong_record)
    assert raised.value.status == "curator-coherence-record-identity-mismatch"


def test_attestation_topology_and_path_boundaries_fail_with_typed_refusals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = QueueFixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    report = contract.worktree_group / "reports" / coherence.QUALITY_REPORT_NAME
    attestation = contract.worktree_group / "reports" / coherence.QUALITY_ATTESTATION_NAME
    attestation_bytes = attestation.read_bytes()
    pair_identity = CuratorQualityAttestation.model_validate_json(attestation_bytes).pairIdentity
    attestation.unlink()
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence._quality_attestation(
            contract,
            _quality_source(contract, attestation, report, pair_identity),
        )
    assert raised.value.status == "curator-coherence-attestation-unreadable"
    attestation.write_bytes(attestation_bytes)

    not_ready = json.loads(attestation_bytes)
    not_ready["checklistStatus"] = "action-required"
    not_ready["curatorActionableCount"] = 1
    attestation.write_text(json.dumps(not_ready), encoding="utf-8")
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence._quality_attestation(
            contract,
            _quality_source(contract, attestation, report, pair_identity),
        )
    assert raised.value.status == "curator-coherence-memory-not-ready"
    attestation.write_bytes(attestation_bytes)

    empty_contract = _value(
        task_root=tmp_path / "empty",
        coordination_root=fixture.coord,
        leaf_id="MISSING",
        repo_name=contract.repo_name,
    )
    empty_contract.task_root.mkdir()
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence._task_context(empty_contract)
    assert raised.value.status == "curator-coherence-task-missing"

    candidate_ref = TaskDocumentRef(repository=contract.repo_name, path="master-a/leaf.json")
    candidate = _value(ref=candidate_ref)
    topology = mock.Mock()
    topology.canonical_ref.return_value = candidate_ref
    topology.resolve.return_value = candidate
    topology.parent.return_value = None
    monkeypatch.setattr(coherence, "resolve_terminal_leaf_doc", lambda *_args: (Path("leaf"), None))
    monkeypatch.setattr(coherence, "TaskDocumentTopology", lambda _root: topology)
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence._task_context(empty_contract)
    assert raised.value.status == "task-document-parent-missing"

    master_ref = TaskDocumentRef(repository=contract.repo_name, path="master-a/task.json")
    master = _value(ref=master_ref)
    topology.resolve.side_effect = [candidate, master]
    topology.parent.side_effect = [master_ref, None]
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence._task_context(empty_contract)
    assert raised.value.status == "task-document-parent-missing"

    topology.resolve.side_effect = TaskDocumentRefError("task-document-invalid", "invalid")
    with pytest.raises(CuratorCoherenceError) as raised:
        coherence._task_context(empty_contract)
    assert raised.value.status == "task-document-invalid"


def test_attestation_rejects_a_different_code_memory_pair(tmp_path: Path) -> None:
    fixture = QueueFixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    report = contract.worktree_group / "reports" / coherence.QUALITY_REPORT_NAME
    attestation = contract.worktree_group / "reports" / coherence.QUALITY_ATTESTATION_NAME
    attestation_bytes = attestation.read_bytes()
    pair_identity = CuratorQualityAttestation.model_validate_json(attestation_bytes).pairIdentity
    wrong_pair = json.loads(attestation_bytes)
    wrong_pair["pairIdentity"]["contractDigest"] = "0" * 64
    attestation.write_text(json.dumps(wrong_pair), encoding="utf-8")

    with pytest.raises(CuratorCoherenceError) as raised:
        coherence._quality_attestation(
            contract,
            _quality_source(contract, attestation, report, pair_identity),
        )

    assert raised.value.status == "curator-coherence-memory-not-ready"


def test_applicability_and_path_boundaries_fail_with_typed_refusals(tmp_path: Path) -> None:
    with pytest.raises(CuratorCoherenceError, match="external-memory leaf"):
        coherence._require_leaf_external_memory(_value(kind="series", memory_mode="external"))
    with pytest.raises(CuratorCoherenceError, match="no memory worktree"):
        coherence._require_leaf_external_memory(
            _value(kind="leaf", memory_mode="external", memory_worktree=None)
        )
    task_root = tmp_path / "task"
    task_root.mkdir()
    contract = cast(
        WorktreeContract,
        _value(
            code_worktree=tmp_path / "code",
            memory_worktree=tmp_path / "memory",
            task_root=task_root,
        ),
    )
    with pytest.raises(CuratorCoherenceError, match="explicit code"):
        coherence.resolve_curator_evidence_ref(contract, "unscoped-evidence.md")
    with pytest.raises(CuratorCoherenceError, match="inside the task root"):
        coherence._task_relative(contract, tmp_path / "outside.json")


def test_status_distinguishes_source_absence_staleness_and_current_authority(
    tmp_path: Path,
) -> None:
    fixture = QueueFixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    request = CuratorCoherenceRequest(
        action="status", contract_path=contract.contract_path.as_posix()
    )
    assert publication.curator_coherence_action(contract, request)["state"] == "current"

    paths = coherence.curator_coherence_paths(contract)
    authority = paths.canonical.read_bytes()
    paths.canonical.unlink()
    assert publication.curator_coherence_action(contract, request)["state"] == "absent"
    paths.canonical.write_bytes(authority)

    with mock.patch.object(
        publication,
        "observe_curator_coherence_source",
        side_effect=CuratorCoherenceError("source-unready", "source unready"),
    ):
        assert (
            publication.curator_coherence_action(contract, request)["state"] == "source-not-ready"
        )

    with (
        mock.patch.object(
            publication,
            "require_current_curator_coherence",
            side_effect=CuratorCoherenceError("source-stale", "source stale"),
        ),
        mock.patch.object(
            publication,
            "current_curator_coherence_predecessor",
            return_value="a" * 64,
        ),
    ):
        stale = publication.curator_coherence_action(contract, request)
    assert stale["state"] == "stale"
    assert stale["predecessorAuthorityDigest"] == "a" * 64


def test_publish_rechecks_predecessor_contract_replay_authority_and_source(
    tmp_path: Path,
) -> None:
    fixture = QueueFixture(tmp_path)
    contract = fixture.contracts[MASTER_A]

    stale_predecessor = _request_for(contract, attempt="EDGE-PREDECESSOR", predecessor="f" * 64)
    with pytest.raises(CuratorCoherenceError) as raised:
        publication.curator_coherence_action(contract, stale_predecessor)
    assert raised.value.status == "curator-coherence-predecessor-stale"

    stale_contract = _request_for(contract, attempt="EDGE-CONTRACT")
    with (
        mock.patch.object(
            publication,
            "load_contract",
            return_value=replace(contract, cleanup="completed"),
        ),
        pytest.raises(CuratorCoherenceError) as raised,
    ):
        publication.curator_coherence_action(contract, stale_contract)
    assert raised.value.status == "curator-coherence-contract-stale"

    replay_request = _request_for(contract, attempt="EDGE-REPLAY")
    validated = coherence.require_current_curator_coherence(contract)
    with mock.patch.object(
        publication,
        "_idempotent_replay",
        side_effect=[None, validated],
    ):
        replay = publication.curator_coherence_action(contract, replay_request)
    assert replay["state"] == "already-current"

    observation = coherence.observe_curator_coherence_source(contract)
    denied_request = _request_for(
        contract,
        attempt="EDGE-DENIED",
        caller=DeclaredCaller(role="worker", task_document_ref=SPRINT),
    )
    with pytest.raises(CuratorCoherenceError) as raised:
        publication._authorize_publisher(contract, observation, denied_request)
    assert raised.value.status == "curator-coherence-caller-refused"

    invalid_topology = mock.Mock()
    invalid_topology.parent.side_effect = TaskDocumentRefError(
        "task-document-parent-invalid", "invalid parent"
    )
    with (
        mock.patch.object(
            publication,
            "TaskDocumentTopology",
            return_value=invalid_topology,
        ),
        pytest.raises(CuratorCoherenceError) as raised,
    ):
        publication._authorize_publisher(contract, observation, denied_request)
    assert raised.value.status == "task-document-parent-invalid"

    raced = replace(observation, attestation_sha256="f" * 64)
    with pytest.raises(CuratorCoherenceError) as raised:
        publication._require_observation_unchanged(observation, raced)
    assert raised.value.status == "curator-coherence-source-raced"

    with (
        mock.patch.object(
            publication,
            "current_curator_coherence_predecessor",
            return_value="f" * 64,
        ),
        pytest.raises(CuratorCoherenceError) as raised,
    ):
        publication._require_predecessor(contract, replay_request)
    assert raised.value.status == "curator-coherence-predecessor-stale"


def test_projection_and_atomic_publication_edges_refuse_without_partial_output(
    tmp_path: Path,
) -> None:
    fixture = QueueFixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    observation = coherence.observe_curator_coherence_source(contract)
    request = _request_for(contract, attempt="EDGE-ATOMIC")
    current = coherence.require_current_curator_coherence(contract)
    publication_input = publication._RecordPublication(
        judgments=[],
        fingerprint="f" * 64,
        predecessor=current.record_digest,
    )
    with (
        mock.patch.object(
            publication,
            "render_curator_coherence",
            side_effect=["first projection\n", "second projection\n"],
        ),
        pytest.raises(RuntimeError, match="depends on its own digest"),
    ):
        publication._record(contract, request, observation, publication_input)

    incomplete_record = tmp_path / "incomplete" / "record.json"
    incomplete_report = incomplete_record.with_name("report.md")
    incomplete_record.parent.mkdir()
    incomplete_record.write_bytes(b"record\n")
    with pytest.raises(CuratorCoherenceError) as raised:
        publication._publish_generation(
            incomplete_record,
            incomplete_report,
            b"record\n",
            b"report\n",
        )
    assert raised.value.status == "curator-coherence-generation-incomplete"

    destination = tmp_path / "failed" / "record.json"
    with (
        mock.patch.object(publication, "_write_fsynced", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        publication._publish_generation(
            destination,
            destination.with_name("report.md"),
            b"record\n",
            b"report\n",
        )
    assert not destination.parent.exists()
    assert not list((tmp_path / "failed").parent.glob(".failed.*.tmp"))

    invalid_attempt = current.record.model_copy(update={"deliveryAttempt": "///"})
    with pytest.raises(CuratorCoherenceError) as raised:
        publication._publish_snapshot(
            contract,
            invalid_attempt,
            current.record_digest,
            current.record_path,
            current.report_path,
        )
    assert raised.value.status == "curator-coherence-attempt-invalid"

    conflict_record = current.record.model_copy(update={"deliveryAttempt": "EDGE-CONFLICT"})
    snapshot = (
        coherence.curator_coherence_paths(contract).snapshots
        / f"EDGE-CONFLICT-{current.record_digest}.json"
    )
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text("different\n", encoding="utf-8")
    with pytest.raises(CuratorCoherenceError) as raised:
        publication._publish_snapshot(
            contract,
            conflict_record,
            current.record_digest,
            current.record_path,
            current.report_path,
        )
    assert raised.value.status == "curator-coherence-snapshot-conflict"


def test_recorded_judgments_report_unreadable_evidence_and_recheck_every_item(
    tmp_path: Path,
) -> None:
    with pytest.raises(CuratorCoherenceError) as raised:
        judgments._evidence_digest(tmp_path / "missing.md", "task:missing.md")
    assert raised.value.status == "curator-coherence-evidence-unreadable"

    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    recorded = [
        _recorded(_source("a.py")).model_copy(
            update={
                "evidenceRef": "task:first.md",
                "evidenceSha256": hashlib.sha256(b"first\n").hexdigest(),
            }
        ),
        _recorded(_source("b.py")).model_copy(
            update={
                "evidenceRef": "task:second.md",
                "evidenceSha256": hashlib.sha256(b"second\n").hexdigest(),
            }
        ),
    ]
    contract = cast(
        WorktreeContract,
        _value(code_worktree=tmp_path, memory_worktree=tmp_path, task_root=tmp_path),
    )
    judgments.require_recorded_judgments_current(contract, recorded)
