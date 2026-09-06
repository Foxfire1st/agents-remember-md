"""Public admission and actual worker execution of selected certification authorities.

Only process launch, the Dagger subprocess boundary, and downstream continuation are
injected. Linked Git repositories, profile admission, immutable publication, terminal
selection, and OperationRuntime execute their production owners.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from unittest import mock

import pytest
from agents_remember.application import worktree_tools
from agents_remember.application.lifecycle.certification_refusal import (
    certification_admission_refusal,
)
from agents_remember.application.lifecycle.lifecycle_operation_worker import (
    OperationRuntime,
    execute_operation,
)
from agents_remember.certification.certificate_models import (
    CreationProvenance,
    GateFiveSemanticInputs,
)
from agents_remember.certification.repository_profiles.authority import load_repository_profile
from agents_remember.certification.repository_profiles.canonical import repository_profile_digest
from agents_remember.certification.repository_profiles.models import (
    GeneratedCandidateInput,
    RepositoryCertificationProfile,
)
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.memory_quality.incremental_scope.candidate import observe_contract_task
from agents_remember.models.closeout.input import CloseoutCorrectedCall, CloseoutMessageInput
from agents_remember.models.test_evidence import _certifying_evidence_from_verified_dagger
from agents_remember.tasks import read_task_doc, write_task_doc
from agents_remember.tasks.document_refs import ResolvedTaskDocument
from agents_remember.worktrees.closeout_input import normalize_closeout_input
from agents_remember.worktrees.integration.closeout.certification import (
    admission as selected_admission,
)
from agents_remember.worktrees.integration.closeout.certification import (
    execution as selected_execution,
)
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
)
from agents_remember.worktrees.integration.closeout.certification.observation import (
    CandidateObservationRequest,
    observe_certification_candidate,
)
from agents_remember.worktrees.integration.closeout.certification.selection import (
    require_selected_certification,
)
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
    operation_record_path,
)
from agents_remember.worktrees.modules import closeout as legacy_closeout
from agents_remember.worktrees.modules.args import WorktreeArgs
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.modules.quality import clean_executor, gate
from agents_remember.worktrees.modules.quality.certification_records import (
    certificate_store,
    records_directory,
)
from agents_remember.worktrees.modules.quality.execution import sandbox
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
    published_manifest_payload,
)
from agents_remember.worktrees.queue.closeout_staged_quality import prepare_staged_code
from agents_remember.worktrees.route_review import (
    RouteReviewError,
    build_route_review,
    require_current_route_review,
)
from agents_remember.worktrees.services import bind_worktree_services, worktree_services
from agents_remember.worktrees.worktree_contract import WorktreeContract, load_contract
from gate_certification_test_support import _gate_catalog
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    RUST_FIXTURE,
    FixtureRepository,
    install_fixture_profile,
)
from test_closeout_queue import MASTER_A, NOW, QueueFixture, _leaf
from test_worktree_support import git


def _fixture(
    root: Path,
    profile: FixtureRepository = NODE_FIXTURE,
    *,
    profile_reference: Path = AGENTS_REMEMBER_PROFILE_REFERENCE,
    candidate_file: tuple[str, str] | None = None,
) -> QueueFixture:
    fixture = QueueFixture(root, memory_mode="internal")
    contract = fixture.contracts[MASTER_A]
    installed = install_fixture_profile(contract.code_worktree, contract.repo_name, profile)
    declared = RepositoryCertificationProfile.model_validate_json(installed.read_bytes())
    # Reused results retain up to three complete original publication manifests;
    # measured suffix catalogs reach 12,795 bytes rather than the fresh fixture's 4 KiB.
    declared = declared.model_copy(
        update={
            "publishedArtifacts": tuple(
                item.model_copy(update={"maxBytes": 16 * 1024})
                if item.path == "result.json"
                else item
                for item in declared.publishedArtifacts
            )
        }
    )
    declared = declared.model_copy(update={"profileDigest": repository_profile_digest(declared)})
    installed.write_text(declared.model_dump_json())
    if profile_reference != AGENTS_REMEMBER_PROFILE_REFERENCE:
        configured = contract.code_worktree / profile_reference
        configured.parent.mkdir(parents=True, exist_ok=True)
        installed.rename(configured)
        settings = json.loads(fixture.config_path.read_text())
        settings["repositories"][contract.repo_name]["certificationProfile"] = (
            profile_reference.as_posix()
        )
        fixture.config_path.write_text(json.dumps(settings))
    git(contract.code_worktree, "add", "-A")
    if candidate_file is not None:
        name, contents = candidate_file
        (contract.code_worktree / name).write_text(contents, encoding="utf-8")
    _review_and_declare(fixture)
    return fixture


def _review_and_declare(fixture: QueueFixture) -> None:
    """Bind the real route-review owner and door to this fixture's changed candidate."""
    contract = fixture.contracts[MASTER_A]
    slug = Path(fixture.leaf_refs[MASTER_A].path).stem
    write_task_doc(contract.task_root, _leaf(contract, slug))
    fixture.declare(MASTER_A)


def _apply(fixture: QueueFixture) -> dict[str, Any]:
    return worktree_tools.worktree_closeout_apply_tool(
        load_config(fixture.config_path),
        fixture.contracts[MASTER_A].contract_path.as_posix(),
        worktree_tools.CloseoutCommitMessages(code="certify exact candidate"),
        worktree_tools.CloseoutApproval(intent_note="exercise explicit fixture closeout"),
    )


def _store(contract: WorktreeContract) -> LifecycleOperationStore:
    return LifecycleOperationStore(operation_record_path(contract.worktree_group, "closeout"))


def _git_state(contract: WorktreeContract) -> tuple[str, str, bytes]:
    index = Path(git(contract.code_worktree, "rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = contract.code_worktree / index
    return (
        git(contract.code_worktree, "rev-parse", "HEAD"),
        git(contract.code_worktree, "ls-files", "--stage"),
        index.read_bytes(),
    )


def _install_hook(contract: WorktreeContract, body: str) -> None:
    hook = Path(git(contract.code_worktree, "rev-parse", "--git-path", "hooks/pre-commit"))
    if not hook.is_absolute():
        hook = contract.code_worktree / hook
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n" + body)
    hook.chmod(0o755)


@pytest.mark.parametrize(
    "fault,code",
    [
        ("missing", "profile-authority-missing"),
        ("malformed", "profile-json-invalid"),
        ("ambiguous", "duplicate-profile-selection"),
    ],
)
def test_public_profile_refusal_precedes_staging_hook_and_operation_publication(
    tmp_path: Path, fault: str, code: str
) -> None:
    fixture = QueueFixture(tmp_path, memory_mode="internal")
    contract = fixture.contracts[MASTER_A]
    install_fixture_profile(contract.code_worktree, contract.repo_name, NODE_FIXTURE)
    git(contract.code_worktree, "add", "-A")
    path = contract.code_worktree / AGENTS_REMEMBER_PROFILE_REFERENCE
    if fault == "missing":
        settings = json.loads(fixture.config_path.read_text())
        settings["repositories"][contract.repo_name].pop("certificationProfile")
        fixture.config_path.write_text(json.dumps(settings))
    elif fault == "malformed":
        path.write_text("{broken profile\n")
    else:
        profile = RepositoryCertificationProfile.model_validate_json(path.read_bytes())
        profile = profile.model_copy(
            update={"selections": (*profile.selections, profile.selections[0])}
        )
        profile = profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})
        path.write_text(profile.model_dump_json())
    # Declare once, after the exact faulty candidate exists; no live door is replaced.
    _review_and_declare(fixture)
    contract = fixture.contracts[MASTER_A]
    _install_hook(contract, "printf 'unexpected hook' > unexpected-hook.txt\n")
    before = _git_state(contract)
    contract_bytes = contract.contract_path.read_bytes()
    with (
        mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch,
        mock.patch.object(gate, "run_clean_quality") as execute,
    ):
        result = _apply(fixture)
    assert result["ok"] is False
    assert result["status"] == "certification-admission-refused"
    assert result["gateStarts"] == 0
    assert code in {item["code"] for item in result["findings"]}
    assert not (contract.code_worktree / "unexpected-hook.txt").exists()
    assert _git_state(contract) == before
    assert contract.contract_path.read_bytes() == contract_bytes
    assert _store(contract).read() is None
    assert not records_directory(contract.worktree_group).exists()
    launch.assert_not_called()
    execute.assert_not_called()


def test_public_conflicted_index_refuses_without_resolving_or_replacing_it(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    blob = git(contract.code_worktree, "hash-object", "feature.txt")
    subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=contract.code_worktree,
        input=f"0 {'0' * 40}\tfeature.txt\n100644 {blob} 1\tfeature.txt\n"
        f"100644 {blob} 2\tfeature.txt\n100644 {blob} 3\tfeature.txt\n",
        text=True,
        capture_output=True,
        check=True,
    )
    before = _git_state(contract)
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        result = _apply(fixture)
    assert result["status"] == "certification-admission-refused"
    assert result["findings"][0]["code"] == "candidate-preparation-refused"
    assert "unmerged" in str(result["findings"][0]["observed"])
    assert _git_state(contract) == before
    assert _store(contract).read() is None
    launch.assert_not_called()


def test_actual_strict_hook_mutation_refuses_before_frozen_admission_or_launch(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    _install_hook(contract, "printf 'hook changed candidate\\n' >> feature.txt\n")
    head = git(contract.code_worktree, "rev-parse", "HEAD")
    before = (contract.code_worktree / "feature.txt").read_bytes()
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        result = _apply(fixture)
    assert result["ok"] is False
    assert result["findings"][0]["code"] == "candidate-preparation-refused"
    assert "pre-commit hook changed" in str(result["findings"][0]["observed"])
    assert (
        contract.code_worktree / "feature.txt"
    ).read_bytes() == before + b"hook changed candidate\n"
    assert git(contract.code_worktree, "rev-parse", "HEAD") == head
    assert _store(contract).read() is None
    launch.assert_not_called()


def test_generated_candidate_is_bound_as_unknown_without_regeneration_or_commit(
    tmp_path: Path,
) -> None:
    fixture = QueueFixture(tmp_path, memory_mode="internal")
    contract = fixture.contracts[MASTER_A]
    path = contract.code_worktree / AGENTS_REMEMBER_PROFILE_REFERENCE
    profile = RepositoryCertificationProfile.model_validate_json(path.read_bytes())
    declaration = GeneratedCandidateInput(
        inputId="fixture-projection",
        checkRail=next(rail.identity for rail in profile.rails if rail.gate == 1),
        sourceScopes=("feature.txt",),
        generatedScopes=("generated.txt",),
    )
    profile = profile.model_copy(update={"generatedInputs": (declaration,)})
    profile = profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})
    path.write_text(profile.model_dump_json())
    generated = contract.code_worktree / "generated.txt"
    generated.write_bytes(b"deliberately unverified generated bytes\n")
    git(contract.code_worktree, "add", "-A")
    _review_and_declare(fixture)
    contract = fixture.contracts[MASTER_A]
    original = generated.read_bytes()
    before_head = git(contract.code_worktree, "rev-parse", "HEAD")
    candidate = git(contract.code_worktree, "write-tree")
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        result = _apply(fixture)
    assert result["ok"] is True
    assert result["state"] == "queued"
    record = _store(contract).read()
    assert record is not None and record.certification is not None
    selected = require_selected_certification(load_contract(contract.contract_path), record)
    assert (
        selected.admission.lifecycle.semanticEnvelope.candidate.generatedArtifactStatus == "unknown"
    )
    assert selected.authorities.semanticEnvelope.generated.candidateTree == candidate
    assert json.loads(
        selected.authorities.semanticEnvelope.generated.declarations.canonicalBytes
    ) == [declaration.model_dump(mode="json")]
    assert selected.run.repositoryProfile.profile.generatedInputs == (declaration,)
    assert selected.recovery.semanticEnvelope.reusePlan.firstGateToRun == 1
    assert generated.read_bytes() == original
    assert git(contract.code_worktree, "rev-parse", "HEAD") == before_head
    launch.assert_called_once()


def _export_record(root: Path, relative: str) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"isolated executor fixture: {relative}\n".encode()
    path.write_bytes(payload)
    return {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}


def _mark_gate_red(catalog: dict[str, object]) -> None:
    rails = catalog["rails"]
    assert isinstance(rails, list)
    for rail in rails:
        if rail["status"] == "pass":
            rail.update(status="fail", exitCode=1)
    catalog["disposition"] = "red"


def _executor(
    profile: FixtureRepository,
    calls: list[clean_executor.CleanQualityRequest],
    *,
    fail_gate: int | None = None,
    interrupt_gate: int | None = None,
    transform_terminal: Callable[[dict[str, Any]], None] | None = None,
):
    def execute(request: clean_executor.CleanQualityRequest) -> clean_executor.CleanQualityOutcome:
        assert request.execution is not None
        request.execution.validate()
        prepared = clean_executor._prepare_sandbox(request)
        admitted_execution = sandbox._admit_prepared_profile(request, prepared)
        assert request.authorize_start is not None
        request.authorize_start()
        calls.append(request)
        run = request.execution.run
        exported = request.worktree_group / "fixture-export"
        artifact_paths = {
            profile.suite_artifact: profile.suite_publication,
            profile.coverage_artifact: profile.coverage_publication,
        }
        for declared in run.repositoryProfile.profile.publishedArtifacts:
            _export_record(exported, declared.path)
        catalogs = []
        for catalog in _gate_catalog(run, exported, supplemental_artifact_paths=artifact_paths):
            planned_gate = catalog["gate"]
            assert isinstance(planned_gate, int)
            if planned_gate < request.execution.first_gate:
                retained = request.execution.retained[planned_gate - 1]
                catalogs.append(
                    {
                        "gate": planned_gate,
                        "disposition": "reused",
                        "started": False,
                        "zeroStart": True,
                        "rails": [],
                        "certificateDigest": retained.certificate.certificateDigest,
                        "resultManifestDigest": retained.result.manifestDigest,
                        "originalPublication": published_manifest_payload(retained.publication),
                    }
                )
                continue
            if fail_gate is not None and planned_gate > fail_gate:
                break
            if interrupt_gate is not None and planned_gate > interrupt_gate:
                break
            if planned_gate == fail_gate:
                _mark_gate_red(catalog)
            if planned_gate == interrupt_gate:
                catalog["disposition"] = "interrupted"
            catalogs.append(catalog)
        exit_code = 1 if fail_gate is not None or interrupt_gate is not None else 0
        decoder = admitted_execution.decoder
        terminal = {
            decoder.statusField: decoder.failedValue if exit_code else decoder.passedValue,
            decoder.exitCodeField: exit_code,
            "gates": catalogs,
        }
        if transform_terminal is not None:
            transform_terminal(terminal)
        (exported / decoder.artifactPath).write_text(json.dumps(terminal))
        assert admitted_execution.admitted.canonical == run.repositoryProfile
        clean_executor._publish_reports(
            exported,
            request.worktree_group / "reports",
            candidate_tree=run.repositoryPlan.candidateIdentity.value,
            profile_execution=admitted_execution,
            bindings=clean_executor.ReportBindings(
                attestation=request.attestation,
                runtime_authority_digest=None,
                protected_generations=request.protected_generations,
            ),
        )
        manifest = load_published_quality_manifest(request.worktree_group / "reports")
        evidence = _certifying_evidence_from_verified_dagger(
            candidate_tree=manifest.candidate_tree,
            result_sha256=manifest.require_file(manifest.result_decoder.artifactPath).sha256,
        )
        return clean_executor.CleanQualityOutcome(
            subprocess.CompletedProcess(
                ["isolated-executor-adapter"], exit_code, stdout=f"fixture exit {exit_code}\n"
            ),
            evidence,
            manifest,
        )

    return execute


@pytest.mark.parametrize("damage", ["empty", "duplicate"])
def test_public_worker_refuses_malformed_complete_gate_catalog_without_selecting_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    owner = runtime.start()
    assert owner.certification is not None
    original_selection = owner.certification
    original_head = git(contract.code_worktree, "rev-parse", "HEAD")
    calls: list[clean_executor.CleanQualityRequest] = []

    def damage_catalog(terminal: dict[str, Any]) -> None:
        terminal["gates"] = [] if damage == "empty" else [terminal["gates"][0]] * 2

    monkeypatch.setattr(
        gate,
        "run_clean_quality",
        _executor(NODE_FIXTURE, calls, transform_terminal=damage_catalog),
    )
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(owner, runtime)
    assert refused.value.findings[0]["code"] == (
        "missing-run-evidence" if damage == "empty" else "terminal-catalog-invalid"
    )
    current = store.read()
    assert current is not None and current.certification == original_selection
    assert (
        require_selected_certification(load_contract(contract.contract_path), current).terminals
        == ()
    )
    assert len(calls) == 1
    assert git(contract.code_worktree, "rev-parse", "HEAD") == original_head
    publication = load_published_quality_manifest(contract.worktree_group / "reports")
    exported = (
        contract.worktree_group / "reports/.quality-report-generations" / publication.generation
    )
    actual = json.loads((exported / publication.result_decoder.artifactPath).read_bytes())
    assert len(actual["gates"]) == (0 if damage == "empty" else 2)


def test_public_worker_selects_complete_red_terminal_before_propagating_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    before_head = git(contract.code_worktree, "rev-parse", "HEAD")
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    running = runtime.start()
    calls: list[clean_executor.CleanQualityRequest] = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls, fail_gate=2))
    with pytest.raises(RuntimeError, match="code-quality gate failed"):
        execute_operation(running, runtime)
    current = store.read()
    assert current is not None and current.status != "completed"
    selected = require_selected_certification(load_contract(contract.contract_path), current)
    assert [(item.result.gate, item.result.disposition) for item in selected.terminals] == [
        (1, "green"),
        (2, "red"),
    ]
    red = selected.terminals[-1]
    assert red.certificate is red.certificateReference is None
    assert (
        certificate_store(contract.worktree_group).load_reference(red.resultReference) == red.result
    )
    assert all(item.status == "fail" for item in red.result.railResults)
    decoder = (
        contract.worktree_group
        / "reports/.quality-report-generations"
        / red.publication.generation
        / "result.json"
    )
    payload = json.loads(decoder.read_bytes())
    assert [item["gate"] for item in payload["gates"]] == [1, 2]
    assert payload["gates"][-1]["disposition"] == "red"
    assert len(calls) == 1
    assert git(contract.code_worktree, "rev-parse", "HEAD") == before_head


def test_worker_cancellation_refuses_selected_authority_before_any_gate_start(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    owner = runtime.start()
    cancelled = store.update(lambda record: record.model_copy(update={"cancelRequested": True}))
    with (
        mock.patch.object(gate, "run_clean_quality") as execute,
        pytest.raises(CertificationContractError) as refused,
    ):
        execute_operation(owner, runtime)
    assert refused.value.findings[0]["code"] == "certification-worker-no-longer-current"
    assert store.read() == cancelled
    execute.assert_not_called()


class _MemoryBoundary:
    def __init__(self) -> None:
        self.received: list[CloseoutCertificationHandoff] = []

    def observe_memory(
        self, handoff: CloseoutCertificationHandoff
    ) -> GateFiveSemanticInputs | None:
        del handoff
        return None

    def run_memory(self, handoff: CloseoutCertificationHandoff) -> WorktreeCommandResult:
        self.received.append(handoff)
        return WorktreeCommandResult(1, {"state": "fixture-memory-owner-pending"})

    def finalize(self, handoff: CloseoutCertificationHandoff) -> WorktreeCommandResult:
        del handoff
        raise AssertionError("Gate 5 has no accepted terminal; finalization must not start")


@pytest.mark.parametrize("profile", [NODE_FIXTURE, RUST_FIXTURE], ids=["node", "rust"])
def test_public_worker_selects_real_code_terminals_then_resumes_only_the_memory_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: FixtureRepository
) -> None:
    fixture = _fixture(tmp_path, profile)
    contract = fixture.contracts[MASTER_A]
    before_head = git(contract.code_worktree, "rev-parse", "HEAD")
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        result = _apply(fixture)
    assert result["ok"] is True and result["state"] == "queued"
    launch.assert_called_once()
    store = _store(contract)
    queued = store.read()
    assert queued is not None and queued.certification is not None
    selected = require_selected_certification(load_contract(contract.contract_path), queued)
    assert selected.run.repositoryProfile.profile.repositoryId == contract.repo_name
    assert (
        next(
            rail.execution.command
            for rail in selected.run.repositoryProfile.profile.rails
            if rail.gate == 1
        )
        == profile.gate_one_command
    )
    objects = certificate_store(contract.worktree_group)
    assert objects.load_reference(queued.certification.frozenRun) == selected.run
    original_run = objects.exact_path("frozen-run", selected.run.runDigest).read_bytes()
    runtime = OperationRuntime(store)
    running = runtime.start()
    calls: list[clean_executor.CleanQualityRequest] = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(profile, calls))
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(running, runtime)
    assert refused.value.findings[0]["code"] == "certification-continuation-unbound"
    assert len(calls) == 1 and calls[0].execution is not None
    assert calls[0].execution.run == selected.run
    current = store.read()
    assert current is not None and current.status == "running" and current.certification is not None
    after = require_selected_certification(load_contract(contract.contract_path), current)
    assert [item.result.gate for item in after.terminals] == [1, 2, 3, 4]
    assert all(item.certificate is not None for item in after.terminals)
    assert after.recovery.semanticEnvelope.reusePlan.firstGateToRun == 5
    assert objects.exact_path("frozen-run", selected.run.runDigest).read_bytes() == original_run
    for item in after.terminals:
        assert objects.load_reference(item.resultReference) == item.result
        assert item.certificateReference is not None
        assert objects.load_reference(item.certificateReference) == item.certificate
    memory = _MemoryBoundary()
    bind_worktree_services(replace(worktree_services(), certification_continuation=memory))
    execute_operation(current, runtime)
    assert len(calls) == 1
    assert len(memory.received) == 1
    assert memory.received[0].selected.run == selected.run
    assert memory.received[0].selected.recovery.semanticEnvelope.reusePlan.firstGateToRun == 5
    assert memory.received[0].store is store
    terminal = store.read()
    assert terminal is not None and terminal.status != "completed"
    assert (
        terminal.result is not None and terminal.result["state"] == "fixture-memory-owner-pending"
    )
    assert git(contract.code_worktree, "rev-parse", "HEAD") == before_head


@pytest.mark.parametrize("profile", [NODE_FIXTURE, RUST_FIXTURE], ids=["node", "rust"])
def test_public_configured_profile_reaches_exact_selected_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: FixtureRepository
) -> None:
    profile_reference = Path("quality/configured-profile.json")
    fixture = _fixture(tmp_path, profile, profile_reference=profile_reference)
    contract = fixture.contracts[MASTER_A]
    configured = load_repository_profile(
        contract.repo_name, contract.code_worktree, profile_reference
    )
    assert not (contract.code_worktree / AGENTS_REMEMBER_PROFILE_REFERENCE).exists()
    with mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch:
        assert _apply(fixture)["state"] == "queued"
    launch.assert_called_once()
    store = _store(contract)
    queued = store.read()
    assert queued is not None and queued.certification is not None
    selected = require_selected_certification(load_contract(contract.contract_path), queued)
    assert selected.run.repositoryProfile == configured.canonical
    objects = certificate_store(contract.worktree_group)
    original_run = objects.exact_path("frozen-run", selected.run.runDigest).read_bytes()
    calls: list[clean_executor.CleanQualityRequest] = []
    execute = _executor(profile, calls)
    admit = sandbox._admit_prepared_profile

    def admit_exact_sandbox(request: clean_executor.CleanQualityRequest, prepared):
        assert request.profile_reference == profile_reference
        admitted = admit(request, prepared)
        assert admitted.plan == selected.run.repositoryPlan
        assert admitted.admitted.canonical == selected.run.repositoryProfile
        assert admitted.admitted.source_path == prepared.root / "source" / profile_reference
        assert admitted.admitted.source_sha256 == configured.source_sha256
        return admitted

    monkeypatch.setattr(sandbox, "_admit_prepared_profile", admit_exact_sandbox)
    monkeypatch.setattr(
        sandbox,
        "load_repository_profile",
        mock.Mock(
            side_effect=AssertionError("selected sandbox must not reload its frozen profile")
        ),
    )
    monkeypatch.setattr(gate, "run_clean_quality", execute)
    memory = _MemoryBoundary()
    bind_worktree_services(replace(worktree_services(), certification_continuation=memory))
    runtime = OperationRuntime(store)
    execute_operation(runtime.start(), runtime)
    assert len(calls) == 1 and calls[0].execution is not None
    assert calls[0].execution.run == selected.run
    assert calls[0].profile_reference == profile_reference
    assert len(memory.received) == 1 and memory.received[0].selected.run == selected.run
    assert objects.exact_path("frozen-run", selected.run.runDigest).read_bytes() == original_run


@pytest.mark.parametrize("outcome", ["green", "red", "interrupted"])
def test_presentation_write_failure_preserves_selected_original_terminal_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    running = runtime.start()
    calls: list[clean_executor.CleanQualityRequest] = []
    execute = _executor(
        NODE_FIXTURE,
        calls,
        fail_gate=2 if outcome == "red" else None,
        interrupt_gate=1 if outcome == "interrupted" else None,
    )
    monkeypatch.setattr(gate, "run_clean_quality", execute)
    original_writer = gate._write_test_results_report

    def interrupted_presentation(_report):
        raise OSError("presentation file cannot be replaced")

    monkeypatch.setattr(gate, "_write_test_results_report", interrupted_presentation)
    with pytest.raises(OSError, match="presentation file cannot be replaced"):
        execute_operation(running, runtime)
    current = store.read()
    assert current is not None and current.certification is not None
    selected = require_selected_certification(load_contract(contract.contract_path), current)
    assert [item.result.gate for item in selected.terminals] == (
        [1, 2, 3, 4] if outcome == "green" else [1, 2] if outcome == "red" else [1]
    )
    assert len(calls) == 1
    objects = certificate_store(contract.worktree_group)
    for terminal in selected.terminals:
        assert objects.load_reference(terminal.resultReference) == terminal.result
    assert current.status != "completed"
    if outcome == "green":
        original_refs = current.certification.terminals
        monkeypatch.setattr(gate, "_write_test_results_report", original_writer)
        with pytest.raises(CertificationContractError) as refused:
            execute_operation(current, runtime)
        assert refused.value.findings[0]["code"] == "certification-continuation-unbound"
        resumed = store.read()
        assert resumed is not None and resumed.certification is not None
        assert resumed.certification.terminals == original_refs
        assert len(calls) == 1
    else:
        assert selected.terminals[-1].certificate is None


def test_cancellation_after_expensive_readback_prevents_actual_executor_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    running = runtime.start()
    original = selected_execution.validate_selected_currentness

    def cancel_after_readback(current_contract, operation_input, record):
        loaded = original(current_contract, operation_input, record)
        store.update(lambda current: current.model_copy(update={"cancelRequested": True}))
        return loaded

    monkeypatch.setattr(selected_execution, "validate_selected_currentness", cancel_after_readback)
    calls: list[clean_executor.CleanQualityRequest] = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(running, runtime)
    assert refused.value.findings[0]["code"] == "certification-selection-observation-moved"
    assert calls == []
    current = store.read()
    assert current is not None and current.cancelRequested and current.certification is not None
    assert current.certification.terminals == ()


def test_cancellation_during_green_prefix_readback_prevents_memory_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    running = runtime.start()
    calls: list[clean_executor.CleanQualityRequest] = []
    monkeypatch.setattr(gate, "run_clean_quality", _executor(NODE_FIXTURE, calls))
    with pytest.raises(CertificationContractError) as unbound:
        execute_operation(running, runtime)
    assert unbound.value.findings[0]["code"] == "certification-continuation-unbound"
    current = store.read()
    assert current is not None and current.certification is not None
    selected = require_selected_certification(load_contract(contract.contract_path), current)
    assert selected.recovery.semanticEnvelope.reusePlan.firstGateToRun == 5
    original_terminals = current.certification.terminals
    memory = _MemoryBoundary()
    bind_worktree_services(replace(worktree_services(), certification_continuation=memory))
    original = selected_execution.validate_selected_currentness

    def cancel_after_readback(current_contract, operation_input, record):
        loaded = original(current_contract, operation_input, record)
        store.update(lambda owner: owner.model_copy(update={"cancelRequested": True}))
        return loaded

    monkeypatch.setattr(selected_execution, "validate_selected_currentness", cancel_after_readback)
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(current, runtime)
    assert refused.value.findings[0]["code"] == "certification-selection-observation-moved"
    assert memory.received == []
    assert len(calls) == 1
    terminal = store.read()
    assert terminal is not None and terminal.cancelRequested and terminal.certification is not None
    assert terminal.certification.terminals == original_terminals


def test_legacy_internal_leaf_route_refuses_before_claim_based_recovery(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    before = _git_state(contract)
    args = WorktreeArgs(
        contract_path=contract.contract_path,
        approval_claimed=True,
        operation_progress=lambda *_: None,
    )
    with (
        mock.patch.object(legacy_closeout, "_recover_closeout_finalization") as recover,
        pytest.raises(CertificationContractError) as refused,
    ):
        legacy_closeout.closeout_result(args, contract)
    assert refused.value.findings[0]["code"] == "selected-closeout-operation-required"
    assert _git_state(contract) == before
    recover.assert_not_called()


@pytest.mark.parametrize("red_gate", [1, 2, 3, 4])
def test_unchanged_selected_red_refuses_public_retry_and_direct_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, red_gate: int
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    owner = runtime.start()
    calls: list[clean_executor.CleanQualityRequest] = []
    if red_gate > 1:
        # Publish a real R11 interrupted catalog and certify its actual green prefix.
        monkeypatch.setattr(
            gate, "run_clean_quality", _executor(NODE_FIXTURE, calls, interrupt_gate=red_gate)
        )
        with pytest.raises(RuntimeError, match="code-quality gate failed"):
            execute_operation(owner, runtime)
        interrupted = store.read()
        assert interrupted is not None
        selected = require_selected_certification(
            load_contract(contract.contract_path), interrupted
        )
        assert [
            item.result.gate for item in selected.terminals if item.certificate is not None
        ] == list(range(1, red_gate))
        assert selected.terminals[-1].certificate is None
        owner = interrupted
    monkeypatch.setattr(
        gate, "run_clean_quality", _executor(NODE_FIXTURE, calls, fail_gate=red_gate)
    )
    with pytest.raises(RuntimeError, match="code-quality gate failed") as failed:
        execute_operation(owner, runtime)
    red = store.read()
    assert red is not None and red.certification is not None
    loaded = require_selected_certification(load_contract(contract.contract_path), red)
    assert loaded.terminals[-1].result.gate == red_gate
    assert loaded.terminals[-1].result.disposition == "red"
    # This exact equality previously bypassed the red check at executor entry.
    assert (
        tuple(
            item.certificate.identity for item in loaded.terminals if item.certificate is not None
        )
        == loaded.recovery.semanticEnvelope.reusePlan.reusedCertificates
    )
    assert loaded.recovery.semanticEnvelope.reusePlan.firstGateToRun == red_gate
    selected_bytes = store.path.read_bytes()
    before_calls = len(calls)
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(red, runtime)
    assert refused.value.findings[0]["code"] == "unchanged-red-recovery"
    assert len(calls) == before_calls
    assert store.path.read_bytes() == selected_bytes
    runtime.fail(failed.value)
    terminal = store.read()
    assert terminal is not None and terminal.status == "failed"
    terminal_bytes = store.path.read_bytes()
    with (
        mock.patch.object(
            selected_admission,
            "prepare_staged_code",
            side_effect=AssertionError("unchanged red retry ran strict preparation"),
        ),
        mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch,
    ):
        result = _apply(fixture)
    assert result["ok"] is False
    assert result["gateStarts"] == 0
    assert "unchanged-red-recovery" in {item["code"] for item in result["findings"]}
    launch.assert_not_called()
    assert len(calls) == before_calls
    assert store.path.read_bytes() == terminal_bytes
    reopened = store.read()
    assert reopened is not None and reopened.certification == red.certification
    assert (reopened.operationKey, reopened.generation, reopened.attempt) == (
        red.operationKey,
        red.generation,
        red.attempt,
    )


def _change_route_review(fixture: QueueFixture, change: str) -> RouteReviewError:
    contract = fixture.contracts[MASTER_A]
    reference = fixture.leaf_refs[MASTER_A]
    path = fixture.tasks / reference.path
    document = read_task_doc(path)
    previous = document.routeReview
    assert previous is not None
    review = None
    if change == "blocked":
        review = build_route_review(
            contract,
            ResolvedTaskDocument(ref=reference, path=path, document=document),
            {
                "verdict": "block",
                "verdictRef": previous.verdictRef,
                "routes": [
                    {"route": item.route, "verdict": "block", "evidenceRef": item.evidenceRef}
                    for item in previous.routes
                ],
            },
        )
    write_task_doc(contract.task_root, document.model_copy(update={"routeReview": review}))
    with pytest.raises(RouteReviewError) as refused:
        require_current_route_review(contract)
    return refused.value


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [("removed", "route-review-required"), ("blocked", "route-review-blocked")],
)
def test_route_review_changed_during_executor_refuses_selection_and_memory_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, expected_code: str
) -> None:
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    runtime = OperationRuntime(store)
    running = runtime.start()
    assert running.certification is not None and not running.certification.terminals
    before_git = _git_state(contract)
    before_task = observe_contract_task(contract)
    calls: list[clean_executor.CleanQualityRequest] = []
    executor = _executor(NODE_FIXTURE, calls)
    memory = _MemoryBoundary()
    bind_worktree_services(replace(worktree_services(), certification_continuation=memory))
    owner_refusals: list[RouteReviewError] = []

    def change_after_executor(request: clean_executor.CleanQualityRequest):
        outcome = executor(request)
        owner_refusals.append(_change_route_review(fixture, change))
        return outcome

    monkeypatch.setattr(gate, "run_clean_quality", change_after_executor)
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(running, runtime)
    assert refused.value.findings[0] == {
        "code": expected_code,
        "path": "routeReview",
        "detail": str(owner_refusals[0]),
    }
    assert isinstance(refused.value.__cause__, RouteReviewError)
    assert len(calls) == 1 and memory.received == []
    current = store.read()
    assert current is not None and current.status != "completed"
    assert current.certification == running.certification
    assert current.mutationEvidence == running.mutationEvidence
    assert current.recoveryCommits is None
    after_task = observe_contract_task(contract)
    assert (after_task.taskIntent, after_task.semanticTopologyDigest) == (
        before_task.taskIntent,
        before_task.semanticTopologyDigest,
    )
    assert _git_state(contract) == before_git


def _replace_with_valid_generic_profile(contract: WorktreeContract, original_digest: str) -> str:
    installed = install_fixture_profile(contract.code_worktree, contract.repo_name, RUST_FIXTURE)
    replacement = load_repository_profile(
        contract.repo_name, contract.code_worktree, AGENTS_REMEMBER_PROFILE_REFERENCE
    ).canonical
    declared = RepositoryCertificationProfile.model_validate_json(installed.read_bytes())
    assert declared.profileDigest == repository_profile_digest(declared)
    assert replacement.profileDigest != original_digest
    return replacement.profileDigest


@pytest.mark.parametrize("timing", ("before-worker", "during-executor"))
def test_valid_profile_change_after_queue_refuses_terminal_selection_and_memory_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timing: str
) -> None:
    fixture = _fixture(tmp_path, NODE_FIXTURE)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    queued = store.read()
    assert queued is not None and queued.certification is not None
    assert queued.certification.terminals == ()
    selected = require_selected_certification(load_contract(contract.contract_path), queued)
    objects = certificate_store(contract.worktree_group)
    frozen_path = objects.exact_path("frozen-run", selected.run.runDigest)
    frozen_bytes = frozen_path.read_bytes()
    before_git = _git_state(contract)
    contract_bytes = contract.contract_path.read_bytes()
    replacements: list[str] = []

    def replace_with_valid_profile() -> None:
        replacements.append(
            _replace_with_valid_generic_profile(
                contract, selected.run.repositoryProfile.profileDigest
            )
        )

    if timing == "before-worker":
        replace_with_valid_profile()
    runtime = OperationRuntime(store)
    running = runtime.start()
    calls: list[clean_executor.CleanQualityRequest] = []
    executor = _executor(NODE_FIXTURE, calls)
    memory = _MemoryBoundary()
    bind_worktree_services(replace(worktree_services(), certification_continuation=memory))

    def change_after_executor(request: clean_executor.CleanQualityRequest):
        outcome = executor(request)
        replace_with_valid_profile()
        return outcome

    execute = mock.Mock(side_effect=change_after_executor)
    monkeypatch.setattr(gate, "run_clean_quality", execute)
    with pytest.raises(CertificationContractError) as refused:
        execute_operation(running, runtime)
    assert len(replacements) == 1
    assert refused.value.findings == (
        {
            "code": "selected-profile-moved",
            "path": "candidate",
            "expected": selected.run.repositoryProfile.profileDigest,
            "observed": replacements[0],
        },
    )
    assert len(calls) == execute.call_count == int(timing == "during-executor")
    assert memory.received == []
    current = store.read()
    assert current is not None and current.status == "running"
    assert (current.operationKey, current.generation, current.attempt, current.fingerprint) == (
        running.operationKey,
        running.generation,
        running.attempt,
        running.fingerprint,
    )
    assert current.certification == queued.certification
    assert current.input == queued.input and current.candidateTree == queued.candidateTree
    assert current.mutationEvidence == running.mutationEvidence
    assert current.recoveryCommits is None
    assert (
        require_selected_certification(load_contract(contract.contract_path), current).terminals
        == ()
    )
    assert frozen_path.read_bytes() == frozen_bytes
    assert contract.contract_path.read_bytes() == contract_bytes
    assert _git_state(contract) == before_git


def test_initial_selection_callback_refuses_an_already_selected_real_generation(tmp_path: Path):
    """The store's selection component cannot replace a public admission's real selection."""
    fixture = _fixture(tmp_path)
    contract = fixture.contracts[MASTER_A]
    with mock.patch.object(lifecycle_operations, "launch_detached_worker"):
        assert _apply(fixture)["state"] == "queued"
    store = _store(contract)
    queued = store.read()
    assert queued is not None and queued.certification is not None
    journal_bytes = store.path.read_bytes()
    selection = mock.Mock(return_value=queued.certification)
    with pytest.raises(RuntimeError, match="initial certification requires an unselected"):
        store._with_initial_certification(queued, selection)
    selection.assert_not_called()
    assert store.read() == queued
    assert store.path.read_bytes() == journal_bytes


def test_admission_refusal_preserves_binary_findings_through_json_transport() -> None:
    expected = b"\x00\xfforiginal"
    observed = b"\x80changed"
    error = CertificationContractError(
        "original evidence differs",
        ({"code": "evidence-mismatch", "expected": expected, "observed": {"chunks": (observed,)}},),
    )
    response = json.loads(json.dumps(certification_admission_refusal("closeout", error)))
    assert response["ok"] is False and response["gateStarts"] == 0
    assert response["status"] == "certification-admission-refused"
    assert len(response["findings"]) == 1
    finding = response["findings"][0]
    assert finding["code"] == "evidence-mismatch"
    assert finding["expected"]["encoding"] == "hex"
    assert bytes.fromhex(finding["expected"]["value"]) == expected
    chunk = finding["observed"]["chunks"][0]
    assert chunk["encoding"] == "hex" and bytes.fromhex(chunk["value"]) == observed


@pytest.mark.parametrize("action,disposition", [("defer", "deferred"), ("withdraw", "withdrawn")])
def test_public_closeout_refuses_an_actually_deferred_or_withdrawn_door(
    tmp_path: Path, action: Literal["defer", "withdraw"], disposition: str
) -> None:
    fixture = _fixture(tmp_path)
    fixture.mutate(action, candidate=fixture.leaf_refs[MASTER_A])
    contract = load_contract(fixture.contracts[MASTER_A].contract_path)
    assert contract.closeout_door is not None
    assert contract.closeout_door.disposition == disposition
    before = _git_state(contract)[:2]
    contract_bytes = contract.contract_path.read_bytes()
    with (
        mock.patch.object(lifecycle_operations, "launch_detached_worker") as launch,
        mock.patch.object(gate, "run_clean_quality") as execute,
    ):
        response = _apply(fixture)
    assert response["ok"] is False and response["gateStarts"] == 0
    assert response["status"] == "certification-admission-refused"
    assert response["findings"] == [
        {
            "code": "candidate-door-not-admissible",
            "path": "candidate",
            "expected": ["waiting", "claimed"],
            "observed": disposition,
        }
    ]
    launch.assert_not_called()
    execute.assert_not_called()
    assert _store(contract).read() is None
    assert _git_state(contract)[:2] == before
    assert contract.contract_path.read_bytes() == contract_bytes


def test_candidate_observer_refuses_a_real_leaf_before_door_declaration(tmp_path: Path) -> None:
    fixture = QueueFixture(tmp_path, memory_mode="internal")
    contract = load_contract(fixture.contracts[MASTER_A].contract_path)
    assert contract.kind == "leaf" and contract.leaf_id and contract.closeout_door is None
    profile = load_repository_profile(
        contract.repo_name, contract.code_worktree, AGENTS_REMEMBER_PROFILE_REFERENCE
    )
    effective = normalize_closeout_input(
        contract,
        CloseoutMessageInput(code="certify exact candidate"),
        route="worktree",
        corrected_call=CloseoutCorrectedCall(
            tool="worktree_closeout_apply", arguments={"contract_path": str(contract.contract_path)}
        ),
    )
    preparation = prepare_staged_code(
        gate.QualityGateTarget(
            contract.code_worktree,
            contract.worktree_group,
            contract.repo_name,
            AGENTS_REMEMBER_PROFILE_REFERENCE,
        )
    )
    request = CandidateObservationRequest(
        contract,
        effective,
        profile.canonical,
        preparation,
        CreationProvenance(
            createdAt=NOW, producer="no-door-observer-test", evidenceRef=str(contract.contract_path)
        ),
    )
    before = _git_state(contract)
    contract_bytes = contract.contract_path.read_bytes()
    with pytest.raises(CertificationContractError) as refused:
        observe_certification_candidate(request)
    assert refused.value.findings[0]["code"] == "candidate-authority-invalid"
    assert _git_state(contract) == before
    assert contract.contract_path.read_bytes() == contract_bytes
    assert _store(contract).read() is None
    assert not records_directory(contract.worktree_group).exists()
