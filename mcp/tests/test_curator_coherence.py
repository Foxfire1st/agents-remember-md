"""Lifecycle-owned curator-coherence publication and shared-consumer proofs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agents_remember.application.memory_quality import controller
from agents_remember.errors import CuratorCoherenceError
from agents_remember.mcp.tools.curator_coherence import curator_coherence_payload
from agents_remember.memory_quality import curator_checklist
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.curator_coherence import (
    CuratorCoherenceJudgment,
    CuratorCoherenceRequest,
    CuratorSourceCandidate,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.models.task_intent import TaskIntentIdentity
from agents_remember.worktrees.integration.closeout import (
    curator_coherence as coherence,
)
from agents_remember.worktrees.integration.closeout import (
    curator_coherence_publication as publish,
)
from agents_remember.worktrees.integration.closeout.curator_coherence import (
    curator_coherence_paths,
)
from agents_remember.worktrees.integration.closeout.curator_coherence_judgments import (
    exact_curator_judgments,
    require_recorded_judgments_current,
)
from agents_remember.worktrees.modules import closeout
from pydantic import ValidationError
from test_closeout_queue import MASTER_A, SPRINT, QueueFixture


def _value(**fields: object) -> Any:
    return cast(Any, SimpleNamespace(**fields))


def _candidate(name: str = "a.py") -> CuratorSourceCandidate:
    return CuratorSourceCandidate(
        sourceFile=f"mcp/{name}",
        onboardingFile=f"mcp/{name}.md",
        classification="drifted",
    )


def _judgment(candidate: CuratorSourceCandidate, evidence: str) -> CuratorCoherenceJudgment:
    return CuratorCoherenceJudgment(
        **candidate.model_dump(mode="json"),
        disposition="reconciled",
        rationale=f"Reconciled {candidate.sourceFile} without broadening its route.",
        evidenceRef=evidence,
    )


def _pair(tmp_path: Path | None = None) -> MemoryCandidatePairIdentity:
    root = tmp_path or Path("/candidate")
    return MemoryCandidatePairIdentity(
        repoId="repo",
        contractPath=(root / "contract.md").as_posix(),
        contractDigest="1" * 64,
        codeRoot=(root / "code").as_posix(),
        memoryRoot=(root / "memory").as_posix(),
        codeSourceBranch="master",
        codeWorkBranch="leaf",
        codeBaseCommit="2" * 40,
        memorySourceBranch="master",
        memoryWorkBranch="leaf",
        memoryBaseCommit="3" * 40,
        onboardingRoot=(root / "memory" / "onboarding").as_posix(),
        ledgerPath=(root / "memory" / "memory.md").as_posix(),
    )


def _request(**updates: object) -> CuratorCoherenceRequest:
    fields: dict[str, object] = {
        "action": "publish",
        "contract_path": "/coordination/tasks/repo/task/enclosures/l1/series-contract.md",
        "semantic_requirement_revision": "R1@v1",
        "delivery_attempt": "A003",
        "judgments": [],
        "expected_predecessor_digest": "",
        "expected_code_candidate_tree": "a" * 40,
        "expected_memory_candidate_tree": "b" * 40,
        "expected_task_topology_fingerprint": "c" * 64,
        "expected_task_intent": {"schema": "task-intent/v1", "digest": "9" * 64},
        "expected_attestation_sha256": "d" * 64,
        "freeze_snapshot": True,
        "caller": DeclaredCaller(
            role="architect",
            task_document_ref=TaskDocumentRef(repository="repo", path="sprint/task.json"),
        ),
    }
    fields.update(updates)
    return CuratorCoherenceRequest.model_validate(fields)


def _observation(candidates: list[CuratorSourceCandidate]) -> Any:
    return _value(
        pair_identity=_pair(),
        code_candidate_tree="a" * 40,
        memory_candidate_tree="b" * 40,
        task_topology_fingerprint="c" * 64,
        task_intent=TaskIntentIdentity(digest="9" * 64),
        attestation_sha256="d" * 64,
        source_candidates=candidates,
    )


def test_request_keeps_requirement_attempt_and_snapshot_identities_separate() -> None:
    request = _request()
    assert request.semantic_requirement_revision == "R1@v1"
    assert request.delivery_attempt == "A003"
    assert request.expected_attestation_sha256 == "d" * 64
    with pytest.raises(ValidationError, match="forbid publication-only fields"):
        CuratorCoherenceRequest(
            action="prepare",
            contract_path=request.contract_path,
            semantic_requirement_revision="R1@v1",
        )


def test_exact_judgment_set_rejects_missing_extra_duplicate_and_bad_evidence(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "notes" / "evidence.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("proof\n", encoding="utf-8")
    contract = _value(
        task_root=tmp_path,
        code_worktree=tmp_path / "code",
        memory_worktree=tmp_path / "memory",
    )
    first, second = _candidate("a.py"), _candidate("b.py")
    observation = _observation([first, second])
    first_judgment = _judgment(first, "task:notes/evidence.md")
    second_judgment = _judgment(second, "task:notes/evidence.md")
    recorded = exact_curator_judgments(
        contract, observation.source_candidates, [second_judgment, first_judgment]
    )
    assert [judgment.identity for judgment in recorded] == [first.identity, second.identity]
    assert {judgment.evidenceSha256 for judgment in recorded} == {
        hashlib.sha256(b"proof\n").hexdigest()
    }

    with pytest.raises(CuratorCoherenceError, match="exactly one judgment"):
        exact_curator_judgments(contract, observation.source_candidates, [first_judgment])
    with pytest.raises(CuratorCoherenceError, match="duplicate"):
        exact_curator_judgments(
            contract,
            observation.source_candidates,
            [first_judgment, first_judgment, second_judgment],
        )
    extra = _judgment(_candidate("extra.py"), "task:notes/evidence.md")
    with pytest.raises(CuratorCoherenceError, match="exactly one judgment"):
        exact_curator_judgments(
            contract,
            observation.source_candidates,
            [first_judgment, second_judgment, extra],
        )
    bad = second_judgment.model_copy(update={"evidenceRef": "task:notes/missing.md"})
    with pytest.raises(CuratorCoherenceError, match="existing scoped"):
        exact_curator_judgments(contract, observation.source_candidates, [first_judgment, bad])


def test_expected_source_identity_rejects_any_stale_dimension() -> None:
    request = _request()
    publish._require_expected_observation(request, _observation([]))
    for field, value in (
        ("code_candidate_tree", "9" * 40),
        ("memory_candidate_tree", "8" * 40),
        ("task_topology_fingerprint", "7" * 64),
        ("task_intent", TaskIntentIdentity(digest="8" * 64)),
        ("attestation_sha256", "6" * 64),
    ):
        observation = _observation([])
        setattr(observation, field, value)
        with pytest.raises(CuratorCoherenceError, match="changed before publish"):
            publish._require_expected_observation(request, observation)


def test_recorded_evidence_digest_rejects_a_mutated_task_artifact(tmp_path: Path) -> None:
    evidence = tmp_path / "notes" / "evidence.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("first proof\n", encoding="utf-8")
    contract = _value(
        task_root=tmp_path,
        code_worktree=tmp_path / "code",
        memory_worktree=tmp_path / "memory",
    )
    candidate = _candidate()
    recorded = exact_curator_judgments(
        contract,
        [candidate],
        [_judgment(candidate, "task:notes/evidence.md")],
    )
    record = _value(judgments=recorded)
    coherence._require_evidence_refs(contract, record)

    evidence.write_text("changed proof\n", encoding="utf-8")
    with pytest.raises(CuratorCoherenceError, match="changed during publication"):
        require_recorded_judgments_current(contract, recorded)
    with pytest.raises(CuratorCoherenceError, match="evidence bytes changed"):
        coherence._require_evidence_refs(contract, record)


def test_content_addressed_generation_is_atomic_idempotent_and_collision_safe(
    tmp_path: Path,
) -> None:
    record = tmp_path / "generations" / ("a" * 64) / "record.json"
    report = record.with_name("report.md")
    publish._publish_generation(record, report, b'{"record":1}\n', b"# report\n")
    assert record.read_bytes() == b'{"record":1}\n'
    assert report.read_bytes() == b"# report\n"
    publish._publish_generation(record, report, b'{"record":1}\n', b"# report\n")
    with pytest.raises(CuratorCoherenceError, match="different bytes"):
        publish._publish_generation(record, report, b'{"record":2}\n', b"# report\n")
    assert not list(record.parent.parent.glob(".*.tmp"))


def test_memory_quality_attestation_is_byte_stable_for_identical_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(curator_checklist, "_tracked_onboarding_paths", lambda _root: set())
    report = tmp_path / "reports" / "curator-memory-quality.md"
    checklist = curator_checklist.CuratorChecklist(
        report_path=report,
        repo_id="repo",
        code_root=tmp_path / "code",
        onboarding_root=tmp_path / "memory" / "onboarding",
        pair_identity=_pair(tmp_path),
        quality={"findingCount": 0},
        repair_findings=[],
        commit_owned_findings=[],
        missing_onboarding={"missing": []},
        stale_route_indexes=[],
        drift_rows=[],
        report_only_findings=[],
    )
    curator_checklist.write_curator_checklist(checklist)
    first = (report.read_bytes(), report.with_suffix(".json").read_bytes())
    curator_checklist.write_curator_checklist(checklist)
    second = (report.read_bytes(), report.with_suffix(".json").read_bytes())
    assert second == first
    assert b"Generated:" not in second[0]


def test_public_tool_publishes_one_live_authority_and_ignores_historical_markdown(
    tmp_path: Path,
) -> None:
    fixture = QueueFixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    paths = curator_coherence_paths(contract)
    predecessor_authority = paths.canonical.read_bytes()
    prepared = curator_coherence_payload(
        fixture.cfg,
        CuratorCoherenceRequest(
            action="prepare",
            contract_path=contract.contract_path.as_posix(),
        ),
    )
    request = CuratorCoherenceRequest(
        action="publish",
        contract_path=contract.contract_path.as_posix(),
        semantic_requirement_revision="MCAR-R02@v1",
        delivery_attempt="A002",
        judgments=[],
        expected_predecessor_digest=str(prepared["predecessorAuthorityDigest"]),
        expected_code_candidate_tree=str(prepared["codeCandidateTree"]),
        expected_memory_candidate_tree=str(prepared["memoryCandidateTree"]),
        expected_task_topology_fingerprint=str(prepared["taskTopologyFingerprint"]),
        expected_task_intent=TaskIntentIdentity.model_validate(prepared["taskIntent"]),
        expected_attestation_sha256=str(prepared["attestationSha256"]),
        freeze_snapshot=True,
        caller=DeclaredCaller(role="architect", task_document_ref=SPRINT),
    )
    published = curator_coherence_payload(fixture.cfg, request)
    assert published["ok"] is True
    assert published["state"] == "published"
    assert prepared["pairIdentity"] == published["pairIdentity"]
    assert published["semanticRequirementRevision"] == "MCAR-R02@v1"
    assert published["deliveryAttempt"] == "A002"
    assert Path(str(published["snapshotPath"])).is_file()

    assert Path(str(published["canonicalPath"])) == paths.canonical
    assert Path(str(published["recordPath"])).is_file()
    assert Path(str(published["reportPath"])).is_file()
    assert "Pair contract digest" in Path(str(published["reportPath"])).read_text(encoding="utf-8")
    assert list(paths.generations.iterdir())

    legacy = contract.task_root / "notes" / "reports"
    (legacy / f"{contract.leaf_id}-curator-report.md").write_text(
        "# obsolete stable Markdown\n", encoding="utf-8"
    )
    (legacy / f"{contract.leaf_id}-curator-report-v2.md").write_text(
        "# obsolete versioned Markdown\n", encoding="utf-8"
    )
    validated = curator_coherence_payload(
        fixture.cfg,
        CuratorCoherenceRequest(
            action="validate",
            contract_path=contract.contract_path.as_posix(),
        ),
    )
    assert validated["state"] == "valid"
    assert validated["pairIdentity"] == published["pairIdentity"]
    assert validated["recordDigest"] == published["recordDigest"]

    generation_count = len(list(paths.generations.iterdir()))
    paths.canonical.write_bytes(predecessor_authority)
    recovered = curator_coherence_payload(fixture.cfg, request)
    assert recovered["state"] == "published"
    assert recovered["recordDigest"] == published["recordDigest"]
    assert len(list(paths.generations.iterdir())) == generation_count

    replay = curator_coherence_payload(fixture.cfg, request)
    assert replay["state"] == "already-current"
    assert replay["recordDigest"] == published["recordDigest"]
    assert len(list(paths.generations.iterdir())) == generation_count


def test_prepare_and_publish_can_replace_a_malformed_authority_by_exact_cas(
    tmp_path: Path,
) -> None:
    fixture = QueueFixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    canonical = curator_coherence_paths(contract).canonical
    malformed = b"not-json\n"
    canonical.write_bytes(malformed)

    prepared = curator_coherence_payload(
        fixture.cfg,
        CuratorCoherenceRequest(
            action="prepare",
            contract_path=contract.contract_path.as_posix(),
        ),
    )
    assert prepared["predecessorAuthorityDigest"] == hashlib.sha256(malformed).hexdigest()
    request = CuratorCoherenceRequest(
        action="publish",
        contract_path=contract.contract_path.as_posix(),
        semantic_requirement_revision="MCAR-R02@v1",
        delivery_attempt="A003",
        judgments=[],
        expected_predecessor_digest=str(prepared["predecessorAuthorityDigest"]),
        expected_code_candidate_tree=str(prepared["codeCandidateTree"]),
        expected_memory_candidate_tree=str(prepared["memoryCandidateTree"]),
        expected_task_topology_fingerprint=str(prepared["taskTopologyFingerprint"]),
        expected_task_intent=TaskIntentIdentity.model_validate(prepared["taskIntent"]),
        expected_attestation_sha256=str(prepared["attestationSha256"]),
        caller=DeclaredCaller(role="architect", task_document_ref=SPRINT),
    )
    published = curator_coherence_payload(fixture.cfg, request)
    assert published["state"] == "published"
    assert published["predecessorAuthorityDigest"] == hashlib.sha256(malformed).hexdigest()
    assert (
        curator_coherence_payload(
            fixture.cfg,
            CuratorCoherenceRequest(
                action="validate",
                contract_path=contract.contract_path.as_posix(),
            ),
        )["state"]
        == "valid"
    )


def test_memory_quality_never_reports_combined_closeout_readiness_without_coherence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    contract = _value()
    scope = _value(contract=contract)
    canonical = tmp_path / "L1-curator-coherence.json"
    monkeypatch.setattr(
        controller,
        "curator_coherence_paths",
        lambda _contract: _value(canonical=canonical),
    )
    monkeypatch.setattr(
        controller,
        "require_current_curator_coherence",
        lambda _contract: (_ for _ in ()).throw(
            CuratorCoherenceError("curator-coherence-authority-missing", "missing")
        ),
    )
    response: dict[str, object] = {"ok": True, "checklistStatus": "ready-for-closeout"}
    controller._attach_coherence_readiness(scope, response)
    assert response == {
        "ok": False,
        "checklistStatus": "coherence-required",
        "qualityChecklistStatus": "ready-for-closeout",
        "closeoutReady": False,
        "coherenceCanonicalPath": canonical.as_posix(),
        "coherenceStatus": "curator-coherence-authority-missing",
        "guidance": (
            "Memory repairs are complete, but closeout is not ready: publish or refresh the "
            "exact structured curator-coherence authority with curator_coherence, then validate it."
        ),
    }


def test_closeout_memory_preflight_calls_the_same_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _value(memory_mode="external", kind="leaf", code_base_commit="a" * 40)
    validated = _value(
        coherence_record_digest="b" * 64,
        delivery_attempt="A003",
        pair_identity=None,
    )
    require_calls: list[object] = []
    monkeypatch.setattr(
        closeout,
        "accepted_closeout_memory_pair",
        lambda observed: require_calls.append(observed) or validated,
    )
    monkeypatch.setattr(
        closeout,
        "worktree_services",
        lambda: _value(memory_quality=_value(check_groups=lambda: (("citations",), ()))),
    )
    monkeypatch.setattr(closeout, "_closeout_contract_context", lambda _contract: object())
    monkeypatch.setattr(
        closeout,
        "run_memory_quality_phase",
        lambda *_args, **_kwargs: {"ok": True},
    )
    result = closeout._memory_quality_before_refresh(contract)
    assert require_calls == [contract]
    assert result["curatorCoherence"] == {
        "state": "valid",
        "recordDigest": "b" * 64,
        "deliveryAttempt": "A003",
    }
