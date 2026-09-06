"""Actual journal-selected integration execution, original bytes, and interrupted suffixes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.errors import CertificationContractError
from agents_remember.models.lifecycles.integration_certification import (
    IntegrationCertificationSelection,
)
from agents_remember.models.lifecycles.operation import (
    IntegrationQualityCertification,
    LifecycleOperationRecord,
)
from agents_remember.worktrees.integration import certification as integration_certification
from agents_remember.worktrees.integration import integration_quality
from agents_remember.worktrees.integration.certification import (
    IntegrationCertificationOwner,
    IntegrationCertificationRequest,
)
from agents_remember.worktrees.integration.integration_quality_checkout import (
    integration_quality_checkout,
)
from agents_remember.worktrees.integration.organizational_completion import (
    OrganizationalCompletionPlan,
)
from agents_remember.worktrees.integration.organizational_completion_integration import (
    preview_organizational_completion,
)
from agents_remember.worktrees.modules.git import repository_identity
from agents_remember.worktrees.modules.quality import gate
from agents_remember.worktrees.modules.quality.certification_records import (
    certificate_store,
    records_directory,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
)
from integration_certification_test_support import IntegrationFixture, integration_fixture
from organizational_completion_test_support import OrganizationalCompletionFixture
from pydantic import ValidationError
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    RUST_FIXTURE,
    FixtureRepository,
)
from test_closeout_certification_entrypoint import _executor
from test_worktree_integrate_quality_gate import integration_contract
from test_worktree_support import git


def run_integration(fixture: IntegrationFixture):
    return integration_quality.run_integration_quality_gate(
        fixture.contract,
        profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
        owner=fixture.owner,
    )


@contextmanager
def _integration_request(fixture: IntegrationFixture) -> Iterator[IntegrationCertificationRequest]:
    contract = fixture.contract
    settings = integration_quality.quality_gate_settings(contract)
    with integration_quality_checkout(contract, commit=contract.code_commit) as checkout:
        yield IntegrationCertificationRequest(
            contract,
            gate.QualityGateTarget(
                checkout,
                contract.worktree_group,
                contract.repo_name,
                AGENTS_REMEMBER_PROFILE_REFERENCE,
            ),
            gate.QualityGatePlan(mode="full", memory_cap_bytes=settings.memory_cap_bytes),
            fixture.owner,
            None,
        )


@pytest.mark.parametrize("profile", [NODE_FIXTURE, RUST_FIXTURE], ids=["node", "rust"])
def test_full_integration_selects_originals_before_execution_and_ignores_latest_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: FixtureRepository,
) -> None:
    fixture = integration_fixture(tmp_path, profile, contract_factory=integration_contract)
    contract = fixture.contract
    calls = []
    emit = _executor(profile, calls)

    def execute(request):
        record = fixture.owner.store.read()
        assert record is not None and record.integrationCertification is not None
        assert record.integrationAuthority is not None
        assert Path(record.integrationAuthority.codeRepository) == repository_identity(
            contract.code_repo_path
        )
        assert record.integrationAuthority.codeRepository != contract.code_repo_path.as_posix()
        assert request.execution is not None
        assert (
            certificate_store(contract.worktree_group).load_reference(
                record.integrationCertification.frozenRun
            )
            == request.execution.run
        )
        return emit(request)

    monkeypatch.setattr(gate, "run_clean_quality", execute)
    first = run_integration(fixture)
    assert first.result["passed"] is True
    record = fixture.owner.store.read()
    assert record is not None and record.integrationCertification is not None
    selected = record.integrationCertification
    assert tuple(item.gate for item in selected.terminals) == (1, 2, 3, 4)
    assert all(item.certificate is not None for item in selected.terminals)
    objects = certificate_store(contract.worktree_group)
    frozen = objects.exact_path("frozen-run", selected.frozenRun.semanticDigest).read_bytes()
    original_result = Path(str(first.result["publishedResultPath"]))
    original = original_result.read_bytes()
    pointer = contract.worktree_group / "reports/quality-report-set.json"
    pointer.write_text("not authority: a rotated or damaged latest pointer\n")
    (records_directory(contract.worktree_group) / "admission.json").write_text("not authority\n")
    second = run_integration(fixture)
    assert second.result["publishedResultPath"] == first.result["publishedResultPath"]
    assert second.result["reportPath"] == first.result["reportPath"]
    assert len(calls) == 1
    assert original_result.read_bytes() == original
    assert (
        objects.exact_path("frozen-run", selected.frozenRun.semanticDigest).read_bytes() == frozen
    )
    assert fixture.owner.store.read() == record
    assert (
        git(contract.code_repo_path, "rev-parse", contract.code_source_branch)
        == contract.code_base_commit
    )


@pytest.mark.parametrize(
    "fault", ["result-bytes", "certificate-bytes", "profile-reference", "owner", "cancel"]
)
def test_selected_integration_refuses_drift_without_falling_back_to_another_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    fixture = integration_fixture(tmp_path, contract_factory=integration_contract)
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, []))
    completed = run_integration(fixture)
    record = fixture.owner.store.read()
    assert record is not None and record.integrationCertification is not None
    reference = AGENTS_REMEMBER_PROFILE_REFERENCE
    if fault == "result-bytes":
        Path(str(completed.result["publishedResultPath"])).write_text("changed original result\n")
    elif fault == "certificate-bytes":
        certificate = record.integrationCertification.terminals[0].certificate
        assert certificate is not None
        certificate_store(fixture.contract.worktree_group).exact_path(
            "certificate", certificate.semanticDigest
        ).write_text("changed original certificate\n")
    elif fault == "profile-reference":
        reference = Path("another-profile.json")
    elif fault == "owner":
        wrong = fixture.owner.record.model_copy(update={"generation": record.generation + 1})
        fixture = replace(fixture, owner=IntegrationCertificationOwner(wrong, fixture.owner.store))
    else:
        fixture.owner.store.update(lambda value: value.model_copy(update={"cancelRequested": True}))
    with (
        mock.patch.object(gate, "run_clean_quality") as execute,
        pytest.raises(integration_quality.IntegrationQualityFailure),
    ):
        integration_quality.run_integration_quality_gate(
            fixture.contract,
            profile_reference=reference,
            owner=fixture.owner,
        )
    execute.assert_not_called()


def test_interrupted_integration_reuses_original_prefix_and_preserves_interrupted_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = integration_fixture(tmp_path, contract_factory=integration_contract)
    calls = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls, interrupt_gate=2))
    with pytest.raises(integration_quality.IntegrationQualityFailure):
        run_integration(fixture)
    first = fixture.owner.store.read()
    assert first is not None and first.integrationCertification is not None
    original = first.integrationCertification
    assert [item.gate for item in original.terminals] == [1, 2]
    assert (
        original.terminals[0].certificate is not None and original.terminals[1].certificate is None
    )
    original_manifest = json.loads(original.terminals[1].publication.canonicalBytes)
    original_path = (
        fixture.contract.worktree_group
        / "reports/.quality-report-generations"
        / original_manifest["generation"]
        / "result.json"
    )
    original_bytes = original_path.read_bytes()
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    assert run_integration(fixture).result["passed"] is True
    assert [request.execution.first_gate for request in calls] == [1, 2]
    current = fixture.owner.store.read()
    assert current is not None and current.integrationCertification is not None
    selected = current.integrationCertification
    assert selected.frozenRun == original.frozenRun
    assert selected.terminals[0] == original.terminals[0]
    assert selected.terminalHistory == (original.terminals[1],)
    assert original_path.read_bytes() == original_bytes
    assert tuple(item.gate for item in selected.terminals) == (1, 2, 3, 4)


def test_red_integration_is_retained_and_refuses_an_unchanged_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = integration_fixture(tmp_path, contract_factory=integration_contract)
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, [], fail_gate=2))
    with pytest.raises(integration_quality.IntegrationQualityFailure):
        run_integration(fixture)
    record = fixture.owner.store.read()
    assert record is not None and record.integrationCertification is not None
    red = record.integrationCertification.terminals[-1]
    assert red.certificate is None and red.gate == 2
    manifest = load_published_quality_manifest(fixture.contract.worktree_group / "reports")
    assert manifest.generation == json.loads(red.publication.canonicalBytes)["generation"]
    with (
        mock.patch.object(gate, "run_clean_quality") as execute,
        pytest.raises(integration_quality.IntegrationQualityFailure) as caught,
    ):
        run_integration(fixture)
    cause = caught.value.__cause__
    assert isinstance(cause, CertificationContractError)
    assert "integration-unchanged-red-refused" in str(cause.findings)
    execute.assert_not_called()
    assert fixture.owner.store.read() == record


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("frozen-kind", "requires an exact frozen-run reference"),
        ("gate-gap", "require one exact code gate prefix"),
        ("uncertified-prefix", "cannot select a later gate after an uncertified terminal"),
        ("foreign-predecessor", "retains originals within its exact generation"),
        ("certified-history", "history contains only interrupted attempts"),
        ("duplicate-history", "history cannot repeat an original attempt"),
    ],
)
def test_integration_selection_schema_refuses_malformed_original_reference_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str, message: str
) -> None:
    fixture = integration_fixture(tmp_path, contract_factory=integration_contract)
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, [], interrupt_gate=2))
    with pytest.raises(integration_quality.IntegrationQualityFailure):
        run_integration(fixture)
    store = fixture.owner.store
    original = store.read()
    assert original is not None and original.integrationCertification is not None
    original_bytes = store.path.read_bytes()
    payload = original.integrationCertification.model_dump(mode="json")
    if damage == "frozen-kind":
        payload["frozenRun"]["kind"] = "admission"
    elif damage == "gate-gap":
        payload["terminals"] = payload["terminals"][1:]
    elif damage == "uncertified-prefix":
        payload["terminals"][0]["certificate"] = None
    elif damage == "foreign-predecessor":
        # This untrusted wire claim must fail before any alleged foreign object is read.
        reference = payload["frozenRun"]
        payload["terminals"][0]["reusedFrom"] = {
            "operationKey": original.operationKey,
            "generation": original.generation + 1,
            "frozenRun": reference,
            "candidateAuthorities": {**reference, "kind": "candidate-authorities"},
            "lifecycleAdmission": {**reference, "kind": "lifecycle-admission"},
        }
    elif damage == "certified-history":
        payload["terminalHistory"] = [payload["terminals"][0]]
    else:
        payload["terminalHistory"] = [payload["terminals"][1]] * 2
    with pytest.raises(ValidationError, match=message):
        IntegrationCertificationSelection.model_validate(payload)
    assert store.read() == original
    assert store.path.read_bytes() == original_bytes


def test_integration_store_refuses_rewritten_selection_and_erased_interruption_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = integration_fixture(tmp_path, contract_factory=integration_contract)
    calls = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls, interrupt_gate=2))
    with pytest.raises(integration_quality.IntegrationQualityFailure):
        run_integration(fixture)
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    assert run_integration(fixture).result["passed"] is True
    assert [request.execution.first_gate for request in calls] == [1, 2]
    store = fixture.owner.store
    original = store.read()
    assert original is not None and original.integrationCertification is not None
    selected = original.integrationCertification
    assert len(selected.terminalHistory) == 1
    original_bytes = store.path.read_bytes()
    for proposed, message in (
        (None, "requires its live operation owner"),
        (
            selected.model_copy(update={"profileReference": "another-profile.json"}),
            "frozen authority cannot be replaced",
        ),
        (
            selected.model_copy(update={"terminals": selected.terminals[:-1]}),
            "can only append an exact suffix",
        ),
        (
            selected.model_copy(update={"terminalHistory": ()}),
            "must preserve its original terminal",
        ),
    ):
        with pytest.raises(RuntimeError, match=message):
            store.update(
                lambda current, selection=proposed: current.model_copy(
                    update={"integrationCertification": selection}
                )
            )
        assert store.read() == original
        assert store.path.read_bytes() == original_bytes
    foreign = selected.model_copy(update={"generation": original.generation + 1})
    with pytest.raises(ValidationError, match="must name its exact operation generation"):
        store.update(
            lambda current: current.model_copy(update={"integrationCertification": foreign})
        )
    assert store.read() == original
    assert store.path.read_bytes() == original_bytes

    cancelled = store.update(lambda current: current.model_copy(update={"cancelRequested": True}))
    cancelled_bytes = store.path.read_bytes()
    with pytest.raises(RuntimeError, match="requires its live operation owner"):
        store.update(lambda current: current.model_copy(update={"integrationCertification": None}))
    assert store.read() == cancelled
    assert store.path.read_bytes() == cancelled_bytes


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("unselected", "integration-certification-selection-missing"),
        ("changed-index", "integration-certification-candidate-mismatch"),
    ],
)
def test_integration_readback_refuses_missing_selection_or_changed_candidate_before_publication(
    tmp_path: Path, fault: str, code: str
) -> None:
    fixture = integration_fixture(tmp_path, contract_factory=integration_contract)
    store = fixture.owner.store
    original = store.read()
    original_bytes = store.path.read_bytes()
    with _integration_request(fixture) as request:
        if fault == "changed-index":
            (request.target.code_worktree / "not-admitted.txt").write_text("changed candidate\n")
            git(request.target.code_worktree, "add", "not-admitted.txt")
        with pytest.raises(CertificationContractError) as caught:
            integration_certification.load_integration_certification(request)
    assert caught.value.findings[0]["code"] == code
    assert store.read() == original
    assert store.path.read_bytes() == original_bytes


def test_integration_selection_loses_real_cas_to_cancellation_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = integration_fixture(tmp_path, contract_factory=integration_contract)
    store = fixture.owner.store
    compare_and_swap = store.update_if_current
    cancellation_bytes = []

    def cancel_before_cas(observed, transform):
        store.update(lambda current: current.model_copy(update={"cancelRequested": True}))
        cancellation_bytes.append(store.path.read_bytes())
        return compare_and_swap(observed, transform)

    monkeypatch.setattr(store, "update_if_current", cancel_before_cas)
    with (
        _integration_request(fixture) as request,
        pytest.raises(CertificationContractError) as caught,
    ):
        integration_certification.prepare_integration_certification(request)
    assert caught.value.findings[0]["code"] == "integration-certification-cas-lost"
    assert len(cancellation_bytes) == 1
    current = store.read()
    assert current is not None and current.cancelRequested
    assert current.integrationCertification is None
    assert store.path.read_bytes() == cancellation_bytes[0]


def test_integration_readback_rejects_a_real_frozen_run_from_another_candidate(
    tmp_path: Path,
) -> None:
    fixture = integration_fixture(tmp_path / "selected", contract_factory=integration_contract)
    other = integration_fixture(
        tmp_path / "other", RUST_FIXTURE, contract_factory=integration_contract
    )
    with _integration_request(other) as request:
        other_run = integration_certification.prepare_integration_certification(
            request
        ).prepared.frozen_run
    with _integration_request(fixture) as request:
        original = integration_certification.prepare_integration_certification(request)
        objects = certificate_store(fixture.contract.worktree_group)
        objects.publish(other_run)
        other_reference = objects.reference("frozen-run", other_run.runDigest)
        assert (
            other_run.repositoryPlan.candidateIdentity
            != original.prepared.frozen_run.repositoryPlan.candidateIdentity
        )
        # Corrupt only the selected journal pointer while keeping both original objects intact.
        store = fixture.owner.store
        payload = original.record.model_dump(mode="json")
        payload["integrationCertification"]["frozenRun"] = other_reference.model_dump(mode="json")
        damaged_bytes = (json.dumps(payload, sort_keys=True) + "\n").encode()
        store.path.write_bytes(damaged_bytes)
        with pytest.raises(CertificationContractError) as caught:
            integration_certification.load_integration_certification(request)
        assert caught.value.findings[0]["code"] == "integration-certification-run-mismatch"
        assert store.path.read_bytes() == damaged_bytes
        assert objects.load_reference(original.state.frozenRun) == original.prepared.frozen_run
        assert objects.load_reference(other_reference) == other_run


def test_integration_history_cannot_relabel_an_original_red_gate_as_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = integration_fixture(tmp_path, contract_factory=integration_contract)
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, [], fail_gate=2))
    with pytest.raises(integration_quality.IntegrationQualityFailure):
        run_integration(fixture)
    store = fixture.owner.store
    original = store.read()
    assert original is not None and original.integrationCertification is not None
    red = original.integrationCertification.terminals[-1]
    assert red.gate == 2 and red.certificate is None
    payload = original.model_dump(mode="json")
    selected = payload["integrationCertification"]
    selected["terminalHistory"] = [selected["terminals"][-1]]
    damaged_bytes = (json.dumps(payload, sort_keys=True) + "\n").encode()
    store.path.write_bytes(damaged_bytes)
    with (
        mock.patch.object(gate, "run_clean_quality") as execute,
        pytest.raises(integration_quality.IntegrationQualityFailure) as caught,
    ):
        run_integration(fixture)
    cause = caught.value.__cause__
    assert isinstance(cause, CertificationContractError)
    assert cause.findings[0]["code"] == "integration-terminal-not-interrupted"
    execute.assert_not_called()
    assert store.path.read_bytes() == damaged_bytes


@contextmanager
def _organizational_fixture() -> Iterator[tuple[IntegrationFixture, OrganizationalCompletionPlan]]:
    """Use the existing real final-leaf/task/journal fixture before full integration quality."""
    owner = OrganizationalCompletionFixture()
    owner.setUp()
    try:
        contract = owner._certified_contract(final=True)
        completion = preview_organizational_completion(contract)
        assert completion is not None
        store, runtime, record = owner._integration_runtime(contract)
        yield (
            IntegrationFixture(contract, IntegrationCertificationOwner(record, store), runtime),
            completion,
        )
    finally:
        try:
            owner.doCleanups()
        finally:
            owner.tearDown()


def _run_completion(fixture: IntegrationFixture, completion: OrganizationalCompletionPlan):
    return integration_quality.run_integration_quality_gate(
        fixture.contract,
        completion=completion,
        profile_reference=AGENTS_REMEMBER_PROFILE_REFERENCE,
        owner=fixture.owner,
    )


def _completion_boundary(fixture: IntegrationFixture, completion: OrganizationalCompletionPlan):
    contract = fixture.contract
    assert contract.memory_repo_path is not None
    return (
        contract.contract_path.read_bytes(),
        completion.master_path.read_bytes(),
        git(contract.code_repo_path, "for-each-ref", "--format=%(refname) %(objectname)"),
        git(contract.memory_repo_path, "for-each-ref", "--format=%(refname) %(objectname)"),
    )


def _original_proof_bytes(fixture: IntegrationFixture):
    """Observe actual selected objects and physical report files without synthesizing evidence."""
    record = fixture.owner.store.read()
    assert record is not None and record.integrationCertification is not None
    selected = record.integrationCertification
    objects = certificate_store(fixture.contract.worktree_group)
    references = [selected.frozenRun]
    for terminal in selected.terminals:
        assert terminal.certificate is not None
        references.extend((terminal.result, terminal.certificate))
    reports = fixture.contract.worktree_group / "reports"
    return (
        selected,
        tuple(
            objects.exact_path(item.kind, item.semanticDigest).read_bytes() for item in references
        ),
        {
            path.relative_to(reports).as_posix(): path.read_bytes()
            for path in reports.rglob("*")
            if path.is_file()
        },
    )


def test_completion_refuses_wrong_original_publication_attestation_before_terminal_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _organizational_fixture() as (fixture, completion):
        calls = []
        before = _completion_boundary(fixture, completion)
        selected_bytes = []
        emit = _executor(NODE_FIXTURE, calls)

        def wrong_attestation(request):
            assert request.attestation is not None
            selected_bytes.append(fixture.owner.store.path.read_bytes())
            # Fault only the publication transport: the real completion fingerprint stays intact.
            changed = {**request.attestation, "executor": "untrusted-publication-producer"}
            return emit(replace(request, attestation=changed))

        monkeypatch.setattr(gate, "run_clean_quality", wrong_attestation)
        with pytest.raises(integration_quality.IntegrationQualityFailure) as caught:
            _run_completion(fixture, completion)
        cause = caught.value.__cause__
        assert isinstance(cause, CertificationContractError)
        assert cause.findings[0]["code"] == "integration-certification-publication-mismatch"
        expected = cause.findings[0]["expected"]
        observed = cause.findings[0]["observed"]
        assert isinstance(expected, Mapping) and isinstance(observed, Mapping)
        assert expected["completionFingerprint"] == completion.fingerprint
        assert observed["executor"] == "untrusted-publication-producer"
        assert len(calls) == len(selected_bytes) == 1
        current = fixture.owner.store.read()
        assert current is not None and current.integrationCertification is not None
        assert current.integrationCertification.terminals == ()
        assert current.qualityCertification is None
        assert fixture.owner.store.path.read_bytes() == selected_bytes[0]
        manifest = load_published_quality_manifest(fixture.contract.worktree_group / "reports")
        assert manifest.attestation is not None
        assert manifest.attestation["executor"] == "untrusted-publication-producer"
        assert _completion_boundary(fixture, completion) == before


def test_completion_cancellation_at_final_cas_preserves_selected_original_gate_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _organizational_fixture() as (fixture, completion):
        calls = []
        monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
        before = _completion_boundary(fixture, completion)
        store = fixture.owner.store
        compare_and_swap = store.update_if_current
        cancellations = []
        originals = []

        def cancel_completion(observed, transform):
            proposed = transform(observed)
            if observed.qualityCertification is None and proposed.qualityCertification is not None:
                assert observed.integrationCertification is not None
                assert tuple(item.gate for item in observed.integrationCertification.terminals) == (
                    1,
                    2,
                    3,
                    4,
                )
                originals.append(_original_proof_bytes(fixture))
                store.update(lambda current: current.model_copy(update={"cancelRequested": True}))
                cancellations.append(store.path.read_bytes())
            return compare_and_swap(observed, lambda _: proposed)

        monkeypatch.setattr(store, "update_if_current", cancel_completion)
        with pytest.raises(integration_quality.IntegrationQualityFailure) as caught:
            _run_completion(fixture, completion)
        cause = caught.value.__cause__
        assert isinstance(cause, CertificationContractError)
        assert cause.findings[0]["code"] == "integration-completion-cas-lost"
        assert len(calls) == len(cancellations) == len(originals) == 1
        current = store.read()
        assert current is not None and current.cancelRequested is True
        assert current.qualityCertification is None
        assert store.path.read_bytes() == cancellations[0]
        assert _original_proof_bytes(fixture) == originals[0]
        assert _completion_boundary(fixture, completion) == before


def test_completion_refuses_another_real_completed_runs_original_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    with _organizational_fixture() as (fixture, completion):
        accepted = _run_completion(fixture, completion)
        assert accepted.certification is not None
        before = fixture.owner.store.path.read_bytes()
        boundary = _completion_boundary(fixture, completion)
        originals = _original_proof_bytes(fixture)
        with _organizational_fixture() as (other, other_completion):
            foreign = _run_completion(other, other_completion)
            assert foreign.certification is not None
            assert foreign.certification.frozenRun != accepted.certification.frozenRun
            other_before = other.owner.store.path.read_bytes()
            with _integration_request(fixture) as base_request:
                request = replace(
                    base_request,
                    attestation=integration_quality._quality_attestation(
                        completion,
                        fixture.contract,
                        base_request.plan,
                    ),
                )
                with pytest.raises(CertificationContractError) as caught:
                    integration_certification.select_completed_integration(
                        request, foreign.certification
                    )
            assert caught.value.findings[0]["code"] == "integration-completion-reference-mismatch"
            assert other.owner.store.path.read_bytes() == other_before
        assert len(calls) == 2
        assert fixture.owner.store.path.read_bytes() == before
        assert _original_proof_bytes(fixture) == originals
        assert _completion_boundary(fixture, completion) == boundary


@pytest.mark.parametrize(
    "fault",
    ["wrong-reference-kind", "reordered-prefix", "missing-certificate", "unselected-completion"],
)
def test_completed_quality_wire_requires_original_full_prefix_and_journal_selection(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    with _organizational_fixture() as (fixture, completion):
        calls = []
        monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
        outcome = _run_completion(fixture, completion)
        assert outcome.certification is not None
        before = fixture.owner.store.path.read_bytes()
        record = fixture.owner.store.read()
        assert record is not None and record.qualityCertification == outcome.certification
        payload = outcome.certification.model_dump(mode="json")
        message = "requires its original full code prefix"
        if fault == "wrong-reference-kind":
            payload["frozenRun"] = payload["terminals"][0]["certificate"]
        elif fault == "reordered-prefix":
            payload["terminals"][:2] = reversed(payload["terminals"][:2])
        elif fault == "missing-certificate":
            payload["terminals"][-1]["certificate"] = None
            message = "requires all four original certificates"
        if fault == "unselected-completion":
            wire_record = record.model_dump(mode="json")
            wire_record["integrationCertification"] = None
            with pytest.raises(
                ValidationError, match="requires journal-selected original references"
            ):
                LifecycleOperationRecord.model_validate(wire_record)
        else:
            with pytest.raises(ValidationError, match=message):
                IntegrationQualityCertification.model_validate(payload)
        assert len(calls) == 1
        assert fixture.owner.store.path.read_bytes() == before
        assert fixture.owner.store.read() == record


@pytest.mark.parametrize("fault", ["completionFingerprint", "codeCommit", "candidateTree"])
def test_first_completion_selection_refuses_untrusted_metadata_with_genuine_originals(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    with _organizational_fixture() as (fixture, completion):
        calls = []
        monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
        select = integration_certification.select_completed_integration
        before = _completion_boundary(fixture, completion)
        attempts = []

        def substitute_metadata(request, genuine):
            record = fixture.owner.store.read()
            assert record is not None and record.qualityCertification is None
            assert record.integrationCertification is not None
            assert genuine.frozenRun == record.integrationCertification.frozenRun
            assert genuine.terminals == record.integrationCertification.terminals
            payload = genuine.model_dump(mode="json")
            original = payload[fault]
            changed = ("0" if original[0] != "0" else "1") + original[1:]
            payload[fault] = payload["attestation"][fault] = changed
            # This is hostile input at the selection boundary, not a claimed generated certificate.
            untrusted = IntegrationQualityCertification.model_validate(payload)
            assert untrusted.result == genuine.result
            assert untrusted.resultSha256 == genuine.resultSha256
            assert (
                untrusted.frozenRun == genuine.frozenRun
                and untrusted.terminals == genuine.terminals
            )
            attempts.append((fixture.owner.store.path.read_bytes(), _original_proof_bytes(fixture)))
            select(request, untrusted)

        monkeypatch.setattr(
            integration_quality, "select_completed_integration", substitute_metadata
        )
        with pytest.raises(integration_quality.IntegrationQualityFailure) as caught:
            _run_completion(fixture, completion)
        cause = caught.value.__cause__
        assert isinstance(cause, CertificationContractError)
        assert cause.findings[0]["code"] == "integration-completion-candidate-mismatch"
        assert len(calls) == len(attempts) == 1
        assert fixture.owner.store.path.read_bytes() == attempts[0][0]
        assert _original_proof_bytes(fixture) == attempts[0][1]
        current = fixture.owner.store.read()
        assert current is not None and current.qualityCertification is None
        assert _completion_boundary(fixture, completion) == before


@pytest.mark.parametrize(
    "fault", ["completionFingerprint", "diffBase", "memoryCapBytes", "codeCommit"]
)
def test_first_completion_store_write_refuses_foreign_binding_with_genuine_originals(
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    with _organizational_fixture() as (fixture, completion):
        calls = []
        monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
        before = _completion_boundary(fixture, completion)
        attempts = []

        def substitute_store_write(_request, genuine):
            record = fixture.owner.store.read()
            assert record is not None and record.qualityCertification is None
            genuine_bytes = genuine.model_dump_json()
            payload = genuine.model_dump(mode="json")
            original = payload["attestation"][fault]
            if fault == "memoryCapBytes":
                changed = str(int(original or "0") + 1024 * 1024 * 1024)
                payload["attestation"][fault] = changed
                payload["result"]["memoryCap"] = {"capBytes": int(changed)}
                payload["result"]["memoryPolicy"]["mode"] = "explicit-cap"
            else:
                changed = ("0" if original[0] != "0" else "1") + original[1:]
                payload["attestation"][fault] = changed
                if fault in {"completionFingerprint", "codeCommit"}:
                    payload[fault] = changed
                else:
                    payload["result"][fault] = changed
            payload["resultSha256"] = hashlib.sha256(
                json.dumps(payload["result"], sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            # Canonical shape alone must not authorize hostile metadata over real gate originals.
            untrusted = IntegrationQualityCertification.model_validate(payload)
            assert untrusted.attestation[fault] != genuine.attestation[fault]
            assert (
                untrusted.frozenRun == genuine.frozenRun
                and untrusted.terminals == genuine.terminals
            )
            if fault in {"completionFingerprint", "codeCommit"}:
                assert untrusted.result == genuine.result
                assert untrusted.resultSha256 == genuine.resultSha256
            else:
                assert untrusted.result != genuine.result
                assert untrusted.resultSha256 != genuine.resultSha256
            assert genuine.model_dump_json() == genuine_bytes
            attempts.append((fixture.owner.store.path.read_bytes(), _original_proof_bytes(fixture)))
            fixture.owner.store.update(
                lambda current: current.model_copy(update={"qualityCertification": untrusted}),
            )

        monkeypatch.setattr(
            integration_quality, "select_completed_integration", substitute_store_write
        )
        message = (
            "selected completion code commit"
            if fault == "codeCommit"
            else "selected completion fingerprint"
        )
        with pytest.raises(ValidationError, match=message):
            _run_completion(fixture, completion)
        assert len(calls) == len(attempts) == 1
        assert fixture.owner.store.path.read_bytes() == attempts[0][0]
        assert _original_proof_bytes(fixture) == attempts[0][1]
        current = fixture.owner.store.read()
        assert current is not None and current.qualityCertification is None
        assert _completion_boundary(fixture, completion) == before


@pytest.mark.parametrize("missing", ["diffBase", "memoryCapBytes"])
def test_first_completion_selection_refuses_incomplete_attestation_before_store_write(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    with _organizational_fixture() as (fixture, completion):
        calls = []
        monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
        select = integration_certification.select_completed_integration
        before = _completion_boundary(fixture, completion)
        attempts = []

        def omit_attestation_field(request, genuine):
            record = fixture.owner.store.read()
            assert record is not None and record.qualityCertification is None
            assert record.integrationCertification is not None
            assert genuine.frozenRun == record.integrationCertification.frozenRun
            assert genuine.terminals == record.integrationCertification.terminals
            genuine_bytes = genuine.model_dump_json()
            attestation = dict(genuine.attestation)
            del attestation[missing]
            # Exercise unvalidated caller input, preserving the separately issued genuine proof.
            untrusted = genuine.model_copy(update={"attestation": attestation})
            assert missing not in untrusted.attestation
            assert untrusted.result == genuine.result
            assert untrusted.resultSha256 == genuine.resultSha256
            assert (
                untrusted.frozenRun == genuine.frozenRun
                and untrusted.terminals == genuine.terminals
            )
            assert genuine.model_dump_json() == genuine_bytes
            attempts.append((fixture.owner.store.path.read_bytes(), _original_proof_bytes(fixture)))
            select(request, untrusted)

        monkeypatch.setattr(
            integration_quality, "select_completed_integration", omit_attestation_field
        )
        with pytest.raises(integration_quality.IntegrationQualityFailure) as caught:
            _run_completion(fixture, completion)
        cause = caught.value.__cause__
        assert isinstance(cause, CertificationContractError)
        assert cause.findings[0]["code"] == "integration-completion-candidate-mismatch"
        assert len(calls) == len(attempts) == 1
        assert fixture.owner.store.path.read_bytes() == attempts[0][0]
        assert _original_proof_bytes(fixture) == attempts[0][1]
        current = fixture.owner.store.read()
        assert current is not None and current.qualityCertification is None
        assert _completion_boundary(fixture, completion) == before


def test_completed_selection_is_idempotent_and_refuses_a_replay_result_as_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _organizational_fixture() as (fixture, completion):
        calls = []
        monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
        first = _run_completion(fixture, completion)
        assert first.certification is not None
        before = fixture.owner.store.path.read_bytes()
        originals = _original_proof_bytes(fixture)
        boundary = _completion_boundary(fixture, completion)
        with _integration_request(fixture) as base_request:
            request = replace(
                base_request,
                attestation=integration_quality._quality_attestation(
                    completion,
                    fixture.contract,
                    base_request.plan,
                ),
            )
            integration_certification.select_completed_integration(request, first.certification)
            assert fixture.owner.store.path.read_bytes() == before
            assert _original_proof_bytes(fixture) == originals
            replay = _run_completion(fixture, completion)
            assert replay.certification == first.certification
            assert replay.result["reusedCertification"] is True
            assert replay.result != first.result
            selected = integration_certification.load_integration_certification(request)
            assert request.attestation is not None
            proposed = integration_quality._certification(
                completion,
                replay.result,
                attestation=request.attestation,
                selection=selected.state,
            )
            assert proposed.frozenRun == first.certification.frozenRun
            assert proposed.terminals == first.certification.terminals
            assert proposed.attestation == first.certification.attestation
            assert proposed.resultSha256 != first.certification.resultSha256
            with pytest.raises(CertificationContractError) as caught:
                integration_certification.select_completed_integration(request, proposed)
        assert caught.value.findings[0]["code"] == "integration-completion-already-selected"
        assert len(calls) == 1
        assert fixture.owner.store.path.read_bytes() == before
        assert _original_proof_bytes(fixture) == originals
        assert _completion_boundary(fixture, completion) == boundary


def test_interrupted_catalog_mutation_after_verified_path_read_refuses_before_reexecution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = integration_fixture(tmp_path, contract_factory=integration_contract)
    calls = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls, interrupt_gate=2))
    with pytest.raises(integration_quality.IntegrationQualityFailure):
        run_integration(fixture)
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    assert run_integration(fixture).result["passed"] is True
    assert [request.execution.first_gate for request in calls] == [1, 2]
    store = fixture.owner.store
    record = store.read()
    assert record is not None and record.integrationCertification is not None
    assert len(record.integrationCertification.terminalHistory) == 1
    historical = record.integrationCertification.terminalHistory[0]
    manifest = json.loads(historical.publication.canonicalBytes)
    objects = certificate_store(fixture.contract.worktree_group)
    historical_result = objects.exact_path(historical.result.kind, historical.result.semanticDigest)
    historical_bytes = historical_result.read_bytes()
    before = store.path.read_bytes()
    originals = _original_proof_bytes(fixture)
    verified_path = integration_certification.published_report_path_from_manifest
    mutations = []

    def mutate_verified_decoder(reports, publication, name):
        path = verified_path(reports, publication, name)
        assert publication.generation == manifest["generation"]
        assert name == publication.result_decoder.artifactPath
        assert isinstance(json.loads(path.read_bytes()), dict)
        # Change the real file only after its actual publication owner verified the original bytes.
        path.write_bytes(b"[]\n")
        mutations.append(path)
        return path

    monkeypatch.setattr(
        integration_certification,
        "published_report_path_from_manifest",
        mutate_verified_decoder,
    )
    with (
        mock.patch.object(gate, "run_clean_quality") as execute,
        pytest.raises(integration_quality.IntegrationQualityFailure) as caught,
    ):
        run_integration(fixture)
    cause = caught.value.__cause__
    assert isinstance(cause, CertificationContractError)
    assert cause.findings[0]["code"] == "integration-interruption-catalog-invalid"
    execute.assert_not_called()
    assert len(calls) == 2 and len(mutations) == 1
    assert store.path.read_bytes() == before and store.read() == record
    assert historical_result.read_bytes() == historical_bytes
    assert mutations[0].read_bytes() == b"[]\n"
    after = _original_proof_bytes(fixture)
    assert after[:2] == originals[:2]
    relative = mutations[0].relative_to(fixture.contract.worktree_group / "reports").as_posix()
    assert after[2] == {**originals[2], relative: b"[]\n"}
