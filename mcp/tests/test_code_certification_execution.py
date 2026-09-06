"""Selected R21 suffixes reopen exact original evidence before any executor starts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from agents_remember.certification.certificate_authority import compile_gate_certificate
from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    InputChangeClass,
    plan_certificate_reuse,
)
from agents_remember.certification.certificate_models import (
    GateCertificate,
    GateCertificateIssuanceContext,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CandidateIdentity, GateResultManifest
from agents_remember.certification.repository_profiles.adapters import (
    DaggerModuleExecutorAdapter,
    RepositoryExecutionRequest,
)
from agents_remember.certification.repository_profiles.canonical import (
    canonicalize_repository_profile,
)
from agents_remember.certification.repository_profiles.models import (
    GeneratedCandidateInput,
    repository_profile_digest,
)
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
)
from agents_remember.certification.repository_profiles.validation import (
    validate_repository_profile,
)
from agents_remember.errors import CertificationContractError
from agents_remember.worktrees.modules.quality import certification_records, clean_executor
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.modules.quality.certification_run import (
    SelectedCodeCertification,
    record_terminal_generation,
    verify_selected_code_terminals,
)
from agents_remember.worktrees.modules.quality.execution import retained_reports, sandbox
from agents_remember.worktrees.modules.quality.execution.models import (
    CodeCertificationExecution,
    RetainedGateExecution,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifestError,
    published_manifest_payload,
)
from repository_profile_test_support import fixture_profile
from test_clean_quality_executor import _test_authority
from test_gate_certification_evidence import _arrange, _publish, _record


def selected_execution(tmp_path: Path, first: int = 3, *, report_transform=None):
    """Use real generation publication, result compilation and certificate-store objects."""
    code, group, prepared, profile = _arrange(tmp_path)
    publication, payload = _publish(prepared, profile, transform=report_transform)
    rows = _record(prepared, publication, payload)
    store = prepared.certificate_store()
    certificates = tuple(store.load(GateCertificate, str(row["certificate"])) for row in rows)
    change_classes: dict[int, InputChangeClass] = {
        1: "gate-1-input",
        2: "gate-2-input",
        3: "gate-3-input",
        4: "gate-4-input",
    }
    changes = (
        CertificateInputChange(
            changeClass=change_classes[first], reason="test selected correction"
        ),
    )
    reuse = plan_certificate_reuse(prepared.frozen_run.admission, certificates, changes)
    retained = tuple(
        RetainedGateExecution(
            certificate,
            store.load(GateResultManifest, certificate.semanticEnvelope.resultManifestDigest),
            publication,
        )
        for certificate in certificates[: first - 1]
    )
    selected = CodeCertificationExecution(
        prepared.frozen_run, reuse, changes, certificates, retained
    )
    request = clean_executor.CleanQualityRequest(
        code,
        group,
        "agents-remember",
        Path("mcp/certification-profile-v1.json"),
        "targeted",
        "HEAD",
        execution=selected,
    )
    return selected, request, profile


def test_untrusted_digest_consistent_certificate_cannot_promote_a_red_result(
    tmp_path: Path,
) -> None:
    """A recomputed certificate hash is untrusted input, never issued green authority."""
    _code, group, prepared, profile = _arrange(tmp_path)
    green_publication, green_payload = _publish(prepared, profile)
    green = certification_records.record_published_generation(
        prepared, green_publication, green_payload
    ).terminals[0]
    assert green.certificate is not None and green.certificateReference is not None
    failing_key = prepared.lane.certificationPlan.gates[0].waves[-1][0].key

    def fail_last_wave(payload):
        payload.update(status="failed", exitCode=1)
        payload["gates"] = payload["gates"][:1]
        payload["gates"][0]["disposition"] = "red"
        for rail in payload["gates"][0]["rails"]:
            if rail["key"] == failing_key:
                rail.update(status="fail", failureCode="actual-observed-red")

    publication, payload = _publish(prepared, profile, nonce="red", transform=fail_last_wave)
    recorded = certification_records.record_published_generation(prepared, publication, payload)
    assert len(recorded.terminals) == 1
    red = recorded.terminals[0]
    assert red.result.disposition == "red" and red.certificate is None
    with pytest.raises(CertificationContractError) as refused:
        compile_gate_certificate(
            prepared.frozen_run.admission,
            prepared.lane.certificationPlan.gates[0],
            red.result,
            (),
            GateCertificateIssuanceContext(provenance=prepared.provenance),
        )
    assert refused.value.findings[0]["code"] == "non-green-result-manifest"

    # Deliberately supply an invalid cross-object claim. Canonical parsing and a
    # matching content digest cannot establish that this certificate was issued.
    envelope = {
        **green.certificate.semanticEnvelope.model_dump(mode="json"),
        "resultManifestDigest": red.result.manifestDigest,
    }
    untrusted = GateCertificate.model_validate(
        {
            **green.certificate.model_dump(mode="json"),
            "semanticEnvelope": envelope,
            "certificateDigest": content_digest(envelope),
        }
    )
    reuse = plan_certificate_reuse(prepared.frozen_run.admission, (untrusted,), ())
    assert reuse.firstGateToRun == 2 and reuse.reusedCertificates == (untrusted.identity,)
    selected = CodeCertificationExecution(
        prepared.frozen_run,
        reuse,
        (),
        (untrusted,),
        (RetainedGateExecution(untrusted, red.result, publication),),
    )
    journal = prepared.directory / "gates.json"
    pointer = group / "reports/quality-report-set.json"
    before = journal.read_bytes(), pointer.read_bytes()
    with pytest.raises(ValueError, match="retained execution result must be originally green"):
        selected.validate()
    assert (journal.read_bytes(), pointer.read_bytes()) == before
    store = prepared.certificate_store()
    assert not store.exact_path("certificate", untrusted.certificateDigest).exists()
    assert store.load_reference(green.certificateReference) == green.certificate
    assert store.load_reference(red.resultReference) == red.result


@pytest.mark.parametrize(
    "fault,detail",
    [
        ("missing-gate", "exact complete Gate 1-4 prefix"),
        ("reordered", "exact complete Gate 1-4 prefix"),
        ("red-result", "uncertified terminal"),
        ("missing-reference", "uncertified terminal"),
        ("certificate-provenance", "original stored bytes"),
        ("result-reference", "original stored bytes"),
    ],
)
def test_selected_code_terminal_verification_refuses_incomplete_or_rebound_originals(
    tmp_path: Path, fault: str, detail: str
) -> None:
    _code, group, prepared, execution = _arrange(tmp_path)
    publication, payload = _publish(prepared, execution)
    recorded = certification_records.record_published_generation(prepared, publication, payload)
    original = recorded.terminals
    assert verify_selected_code_terminals(prepared, original) == publication
    terminals = original
    if fault == "missing-gate":
        terminals = original[:-1]
    elif fault == "reordered":
        terminals = (original[1], original[0], *original[2:])
    elif fault == "red-result":
        failing_key = prepared.lane.certificationPlan.gates[0].waves[-1][0].key

        def fail_last_wave(payload):
            payload.update(status="failed", exitCode=1)
            payload["gates"] = payload["gates"][:1]
            payload["gates"][0]["disposition"] = "red"
            for rail in payload["gates"][0]["rails"]:
                if rail["key"] == failing_key:
                    rail.update(status="fail", failureCode="actual-observed-red")

        red_publication, red_payload = _publish(
            prepared, execution, nonce="red", transform=fail_last_wave
        )
        red = certification_records.record_published_generation(
            prepared, red_publication, red_payload
        )
        assert len(red.terminals) == 1 and red.terminals[0].result.disposition == "red"
        terminals = (*red.terminals, *original[1:])
    elif fault == "missing-reference":
        terminals = (replace(original[0], certificateReference=None), *original[1:])
    elif fault == "certificate-provenance":
        certificate = original[0].certificate
        assert certificate is not None
        changed = GateCertificate.model_validate(
            {
                **certificate.model_dump(mode="json"),
                "provenance": {
                    **certificate.provenance.model_dump(mode="json"),
                    "evidenceRef": "fixture://different-original-provenance",
                },
            }
        )
        assert changed.certificateDigest == certificate.certificateDigest
        terminals = (replace(original[0], certificate=changed), *original[1:])
    else:
        terminals = (
            replace(original[0], resultReference=original[1].resultReference),
            *original[1:],
        )
    journal = prepared.directory / "gates.json"
    original_journal = journal.read_bytes()
    pointer = group / "reports/quality-report-set.json"
    original_pointer = pointer.read_bytes()
    with pytest.raises(RuntimeError, match=detail):
        verify_selected_code_terminals(prepared, terminals)
    assert journal.read_bytes() == original_journal
    assert pointer.read_bytes() == original_pointer
    store = prepared.certificate_store()
    for terminal in original:
        assert terminal.certificateReference is not None
        assert store.load_reference(terminal.certificateReference) == terminal.certificate
        assert store.load_reference(terminal.resultReference) == terminal.result


@pytest.mark.parametrize("diff_base", ["", " HEAD", "HEAD\n"])
def test_selected_execution_manifest_refuses_missing_or_noncanonical_comparison_authority(
    tmp_path: Path, diff_base: str
) -> None:
    selected, request, _profile = selected_execution(tmp_path)
    original = selected.payload(diff_base=request.diff_base)
    with pytest.raises(ValueError, match="exact selected comparison commit"):
        selected.payload(diff_base=diff_base)
    assert selected.payload(diff_base=request.diff_base) == original


def test_selected_sandbox_rejects_profile_growth_before_admission(tmp_path: Path) -> None:
    selected, request, _profile = selected_execution(tmp_path)
    prepared = clean_executor._prepare_sandbox(request)
    assert request.profile_reference is not None
    profile = prepared.root / "source" / request.profile_reference
    original = profile.read_bytes()
    with profile.open("ab") as output:
        output.write(b" " * (sandbox.MAX_PROFILE_BYTES + 1 - len(original)))
    assert profile.stat().st_size == sandbox.MAX_PROFILE_BYTES + 1
    with pytest.raises(
        ValueError, match="selected candidate profile exceeds its authority byte bound"
    ):
        sandbox._admit_prepared_profile(request, prepared)
    assert selected == request.execution
    assert (request.code_worktree / request.profile_reference).read_bytes() == original
    assert not (prepared.root / "manifest.json").exists()


def test_selected_sandbox_refuses_real_equal_tree_base_movement_before_transport(
    tmp_path: Path,
) -> None:
    selected, request, _profile = selected_execution(tmp_path)
    prepared = clean_executor._prepare_sandbox(request)
    execution = sandbox._admit_prepared_profile(request, prepared)
    frozen = execution.plan.sourceSelection
    assert frozen is not None
    source = prepared.root / "source"
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Sandbox Fixture",
            "-c",
            "user.email=sandbox@fixture.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "Move comparison ref without changing candidate bytes",
        ],
        cwd=source,
        check=True,
        capture_output=True,
    )
    observed = sandbox.observe_profile_source_selection(
        execution.admitted, execution.selection, execution.plan.candidateIdentity, request.diff_base
    )
    assert observed is not None and observed.baseCommit != frozen.baseCommit
    assert (observed.baseTree, observed.candidateTree, observed.changedPaths) == (
        frozen.baseTree,
        frozen.candidateTree,
        frozen.changedPaths,
    )
    with (
        patch.object(
            sandbox, "snapshot_retained_reports", wraps=sandbox.snapshot_retained_reports
        ) as retain,
        pytest.raises(ValueError, match="frozen comparison authority"),
    ):
        sandbox._write_sandbox_manifest(request, prepared, execution, _test_authority())
    retain.assert_not_called()
    assert not (prepared.root / "manifest.json").exists()
    assert not (prepared.root / "retained-reports").exists()
    assert selected.run.repositoryPlan.sourceSelection == frozen


@pytest.mark.parametrize("first", (1, 2, 3, 4))
def test_exact_suffix_reuses_original_objects_and_only_selected_files(tmp_path, first):
    selected, request, _profile = selected_execution(tmp_path, first)
    selected.validate()
    before = tuple(item.payload() for item in selected.retained)
    destination = tmp_path / "retained"
    files = retained_reports.snapshot_retained_reports(
        selected,
        reports=request.worktree_group / "reports",
        destination=destination,
    )
    assert selected.first_gate == first
    assert tuple(item.payload() for item in selected.retained) == before
    assert destination.exists() is (first > 1)
    assert "clean-quality-results.json" not in {item.path for item in files}
    for item in files:
        assert (destination / item.path).stat().st_size == item.size
    if first == 3:
        assert {"coverage.data", "coverage.json", "dashboard-coverage.json"} <= {
            item.path for item in files
        }
        assert not any("python-crap" in item.path for item in files)


@pytest.mark.parametrize("first", (None, 5))
def test_zero_code_start_decisions_refuse_before_authority_or_sandbox(tmp_path, first):
    selected, request, _profile = selected_execution(tmp_path)
    reuse = selected.reuse_plan.model_copy(
        update={"firstGateToRun": first, "zeroGateStarts": first is None},
    )
    request = replace(request, execution=replace(selected, reuse_plan=reuse))
    with (
        patch.object(clean_executor, "admit_dagger_authority") as authority,
        patch.object(clean_executor, "_prepare_sandbox") as prepare,
        pytest.raises(ValueError, match="zero code gate starts"),
    ):
        clean_executor.run_clean_quality(request)
    authority.assert_not_called()
    prepare.assert_not_called()


@pytest.mark.parametrize("fault", ("chain", "recovery", "prefix", "result", "publication"))
def test_selected_identity_conflicts_never_resolve_a_report_path(tmp_path, monkeypatch, fault):
    selected, request, _profile = selected_execution(tmp_path)
    if fault == "chain":
        selected = replace(selected, certificates=selected.certificates[1:])
    elif fault == "recovery":
        reuse = selected.reuse_plan.model_copy(update={"firstGateToRun": 2})
        selected = replace(selected, reuse_plan=reuse)
    elif fault == "prefix":
        selected = replace(selected, retained=selected.retained[:1])
    elif fault == "result":
        selected = replace(
            selected,
            retained=(
                replace(selected.retained[0], result=selected.retained[1].result),
                *selected.retained[1:],
            ),
        )
    else:
        selected = replace(
            selected,
            retained=(
                replace(
                    selected.retained[0],
                    publication=replace(selected.retained[0].publication, generation="f" * 64),
                ),
                *selected.retained[1:],
            ),
        )

    def no_open(*args, **kwargs):
        raise AssertionError("invalid selection must refuse before source acquisition")

    monkeypatch.setattr(retained_reports, "published_report_path_from_manifest", no_open)
    with pytest.raises((ValueError, RuntimeError)):
        retained_reports.snapshot_retained_reports(
            selected, reports=request.worktree_group / "reports", destination=tmp_path / "snapshot"
        )
    assert not (tmp_path / "snapshot").exists()


@pytest.mark.parametrize("fault", ("missing", "corrupt", "symlink", "parent-symlink"))
def test_missing_unsafe_or_corrupt_original_gate_two_artifact_refuses(tmp_path, fault):
    selected, request, _profile = selected_execution(tmp_path)
    root = (
        request.worktree_group
        / "reports/.quality-report-generations"
        / selected.retained[1].publication.generation
    )
    target = root / "coverage.json"
    if fault == "missing":
        target.unlink()
    elif fault == "corrupt":
        target.write_bytes(b"not the certified coverage")
    elif fault == "symlink":
        original = tmp_path / "outside.json"
        target.rename(original)
        target.symlink_to(original)
    else:
        original = tmp_path / "outside-evidence"
        (root / "rail-evidence").rename(original)
        (root / "rail-evidence").symlink_to(original, target_is_directory=True)
    expected_error = (
        RuntimeError if fault in {"missing", "corrupt"} else PublishedQualityManifestError
    )
    with pytest.raises(expected_error):
        retained_reports.snapshot_retained_reports(
            selected, reports=request.worktree_group / "reports", destination=tmp_path / "snapshot"
        )
    assert not (tmp_path / "snapshot").exists()


def test_transport_population_bound_precedes_any_file_read(tmp_path, monkeypatch):
    selected, request, _profile = selected_execution(tmp_path)
    monkeypatch.setattr(retained_reports, "MAX_RETAINED_FILES", 1)

    def no_read(*args, **kwargs):
        raise AssertionError("capacity admission must precede any source read")

    monkeypatch.setattr(retained_reports, "published_report_path_from_manifest", no_read)
    with pytest.raises(ValueError, match="bound"):
        retained_reports.snapshot_retained_reports(
            selected, reports=request.worktree_group / "reports", destination=tmp_path / "snapshot"
        )


def test_source_mutation_after_resolution_does_not_enter_snapshot(tmp_path, monkeypatch):
    selected, request, _profile = selected_execution(tmp_path)
    resolve = retained_reports.published_report_path_from_manifest

    def mutate_after_proof(reports, publication, relative):
        path = resolve(reports, publication, relative)
        if relative == "coverage.json":
            path.write_bytes(b"changed after accepted resolution")
        return path

    monkeypatch.setattr(retained_reports, "published_report_path_from_manifest", mutate_after_proof)
    with pytest.raises(ValueError, match="changed while snapshotting"):
        retained_reports.snapshot_retained_reports(
            selected, reports=request.worktree_group / "reports", destination=tmp_path / "snapshot"
        )
    assert not (tmp_path / "snapshot/coverage.json").exists()


def test_selected_sandbox_uses_frozen_profile_and_binds_exact_base(tmp_path, monkeypatch):
    selected, request, _profile = selected_execution(tmp_path)
    prepared = clean_executor._prepare_sandbox(request)

    def no_reload(*args, **kwargs):
        raise AssertionError("selected route cannot reload a profile")

    monkeypatch.setattr(sandbox, "load_repository_profile", no_reload)
    admitted = sandbox._admit_prepared_profile(request, prepared)
    assert admitted.plan == selected.run.repositoryPlan
    assert admitted.admitted.canonical == selected.run.repositoryProfile
    with pytest.raises(ValueError, match="exact executor candidate"):
        sandbox._admit_prepared_profile(request, replace(prepared, candidate_tree="f" * 40))
    payload = selected.payload(diff_base=request.diff_base)
    assert payload["diffBase"] == "HEAD"
    assert payload["executionDigest"] == content_digest(
        {key: value for key, value in payload.items() if key != "executionDigest"}
    )
    assert payload["retained"] == [item.payload() for item in selected.retained]


def test_adapter_transports_only_explicitly_declared_retained_directory(tmp_path):
    _selected, _request, profile = selected_execution(tmp_path)
    request = RepositoryExecutionRequest(
        tmp_path / "source",
        tmp_path / "bundle",
        tmp_path / "manifest",
        "targeted",
        tmp_path / "export",
        retained_reports=tmp_path / "retained",
    )
    command = DaggerModuleExecutorAdapter().command(profile.executor, request)
    assert f"--retained-reports={request.retained_reports}" in command
    assert not any(item.startswith("--diff-base") for item in command)
    assert not any(
        item.startswith("--retained-reports")
        for item in DaggerModuleExecutorAdapter().command(
            profile.executor, replace(request, retained_reports=None)
        )
    )
    with pytest.raises(ValueError, match="does not declare"):
        DaggerModuleExecutorAdapter().command(
            profile.executor.model_copy(update={"retainedReportsArgument": None}), request
        )


def test_generated_declarations_bind_only_the_check_rail_gate_semantics():

    original = fixture_profile()
    declaration = GeneratedCandidateInput(
        inputId="projection",
        checkRail=next(rail.identity for rail in original.rails if rail.gate == 1),
        sourceScopes=("schema",),
        generatedScopes=("generated",),
    )
    updated = original.model_copy(update={"generatedInputs": (declaration,)})
    updated = updated.model_copy(update={"profileDigest": repository_profile_digest(updated)})
    plans = [
        compile_repository_profile_plan(
            canonicalize_repository_profile(profile),
            selection_id="closeout-targeted",
            candidate_identity=CandidateIdentity(kind="git-tree", value="c" * 40),
        )
        for profile in (original, updated)
    ]
    assert plans[0].gates[0].planDigest != plans[1].gates[0].planDigest
    assert [gate.planDigest for gate in plans[0].gates[1:]] == [
        gate.planDigest for gate in plans[1].gates[1:]
    ]
    node = next(
        node
        for node in plans[1].gates[0].semanticInputs
        if node.inputKind == "generated-candidate-input"
    )
    assert node.contentDigest == content_digest(declaration)
    assert node.consumingGates == (1,)
    assert original.generatedInputs == ()


@pytest.mark.parametrize("fault", ("later-gate", "duplicate", "unsafe-source", "duplicate-scope"))
def test_generated_input_declarations_refuse_ambiguous_or_unsafe_authority(fault):

    profile = fixture_profile()
    if fault in {"unsafe-source", "duplicate-scope"}:
        with pytest.raises(ValueError):
            GeneratedCandidateInput(
                inputId="projection",
                checkRail=profile.rails[0].identity,
                sourceScopes=("../schema",) if fault == "unsafe-source" else ("schema", "schema"),
                generatedScopes=("generated",),
            )
        return
    declaration = GeneratedCandidateInput(
        inputId="projection",
        checkRail=next(
            rail.identity
            for rail in profile.rails
            if rail.gate == (2 if fault == "later-gate" else 1)
        ),
        sourceScopes=("schema",),
        generatedScopes=("generated",),
    )
    profile = profile.model_copy(
        update={
            "generatedInputs": (declaration,)
            if fault == "later-gate"
            else (declaration, declaration)
        }
    )
    profile = profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})
    report = validate_repository_profile(canonicalize_repository_profile(profile))
    expected = (
        "generated-input-check-rail-invalid"
        if fault == "later-gate"
        else "duplicate-generated-input"
    )
    assert expected in {finding.code for finding in report.findings}


def test_real_coverage_above_old_transport_cap_uses_frozen_publication_bound(tmp_path):
    coverage_size = 64 * 1024 * 1024 + 1

    def large_coverage(payload):
        target = tmp_path / "export/coverage.data"
        with target.open("wb") as stream:
            stream.truncate(coverage_size)
        hasher = hashlib.sha256()
        with target.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                hasher.update(block)
        for gate in payload["gates"]:
            for rail in gate["rails"]:
                for artifact in rail["artifacts"]:
                    if artifact["evidenceRef"] == "coverage.data":
                        artifact.update(sha256=hasher.hexdigest(), size=coverage_size)

    selected, request, _profile = selected_execution(tmp_path, report_transform=large_coverage)
    files = retained_reports.snapshot_retained_reports(
        selected,
        reports=request.worktree_group / "reports",
        destination=tmp_path / "retained-large",
    )
    coverage = next(item for item in files if item.path == "coverage.data")
    declared = next(
        item
        for item in selected.run.repositoryProfile.profile.publishedArtifacts
        if item.path == coverage.path
    )
    assert declared.maxBytes >= coverage_size
    assert coverage.size == coverage_size
    assert (tmp_path / "retained-large/coverage.data").stat().st_size == coverage_size


@pytest.mark.parametrize("cancel", [False, True])
def test_current_operation_authority_is_rechecked_after_snapshot_before_executor(tmp_path, cancel):
    selected, request, _profile = selected_execution(tmp_path)
    observed = []

    class CancellationObserved(RuntimeError):
        pass

    def authorize():
        sandbox_root = clean_executor.clean_sandbox_root(request.worktree_group)
        manifest = json.loads((sandbox_root / "manifest.json").read_bytes())
        assert (sandbox_root / "candidate.bundle").is_file()
        assert manifest["codeExecution"]["run"]["runDigest"] == selected.run.runDigest
        assert manifest["candidateTree"] == selected.run.repositoryPlan.candidateIdentity.value
        observed.append("current-authority")
        if cancel:
            raise CancellationObserved("operation was cancelled during snapshot preparation")

    def execute(command, source, env):
        assert observed == ["current-authority"]
        observed.append("executor")
        return subprocess.CompletedProcess(command, 17, "actual injected executor refusal", "")

    request = replace(request, authorize_start=authorize)
    if cancel:
        with pytest.raises(CancellationObserved, match="cancelled during snapshot"):
            clean_executor.run_clean_quality(
                request,
                runner=execute,
                executor_resolver=lambda _env: "dagger",
                authority=_test_authority(),
            )
        assert observed == ["current-authority"]
    else:
        result = clean_executor.run_clean_quality(
            request,
            runner=execute,
            executor_resolver=lambda _env: "dagger",
            authority=_test_authority(),
        )
        assert result.returncode == 17
        assert observed == ["current-authority", "executor"]


def _record_bytes(group: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in certification_records.records_directory(group).rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("diff_base", ["", " HEAD", "HEAD\n"])
def test_clean_selected_run_refuses_noncanonical_base_before_sandbox(tmp_path, diff_base):
    _selected, request, _profile = selected_execution(tmp_path)
    records = _record_bytes(request.worktree_group)
    pointer = request.worktree_group / "reports/quality-report-set.json"
    before_pointer = pointer.read_bytes()
    runner = Mock()
    with pytest.raises(ValueError, match="explicit acceptance comparison commit"):
        clean_executor.run_clean_quality(
            replace(request, diff_base=diff_base), runner=runner, authority=_test_authority()
        )
    runner.assert_not_called()
    assert not clean_executor.clean_sandbox_root(request.worktree_group).exists()
    assert _record_bytes(request.worktree_group) == records
    assert pointer.read_bytes() == before_pointer


def test_clean_selected_run_requires_launch_owner_after_real_preparation(tmp_path):
    selected, request, _profile = selected_execution(tmp_path)
    records = _record_bytes(request.worktree_group)
    pointer = request.worktree_group / "reports/quality-report-set.json"
    before_pointer = pointer.read_bytes()
    assert request.authorize_start is None
    runner = Mock()
    with pytest.raises(ValueError, match="requires current operation launch authority"):
        clean_executor.run_clean_quality(
            request,
            runner=runner,
            executor_resolver=lambda _env: "dagger",
            authority=_test_authority(),
        )
    runner.assert_not_called()
    root = clean_executor.clean_sandbox_root(request.worktree_group)
    manifest = json.loads((root / "manifest.json").read_bytes())
    assert manifest["codeExecution"]["run"]["runDigest"] == selected.run.runDigest
    assert (root / "candidate.bundle").is_file()
    assert _record_bytes(request.worktree_group) == records
    assert pointer.read_bytes() == before_pointer


@pytest.mark.parametrize("fault", ["index-moved", "mode-moved"])
def test_strict_selected_gate_refuses_drift_before_executor_or_terminal_selection(tmp_path, fault):
    selected, request, _profile = selected_execution(tmp_path)
    target = code_quality_gate.QualityGateTarget(
        request.code_worktree,
        request.worktree_group,
        request.repository_id,
        request.profile_reference,
    )
    select, authorize = Mock(), Mock()
    binding = SelectedCodeCertification(
        selected,
        select,
        lambda: frozenset(item.publication.generation for item in selected.retained),
        authorize,
    )
    if fault == "index-moved":
        (request.code_worktree / "after-selection.txt").write_text("new staged input\n")
        subprocess.run(
            ["git", "add", "after-selection.txt"],
            cwd=request.code_worktree,
            check=True,
            capture_output=True,
        )
        plan = code_quality_gate.QualityGatePlan(mode="targeted", selected=binding)
        detail = "does not name the staged candidate"
    else:
        plan = code_quality_gate.QualityGatePlan(mode="full", selected=binding)
        detail = "quality mode differs from the selected frozen profile plan"
    before = _record_bytes(request.worktree_group)
    pointer = request.worktree_group / "reports/quality-report-set.json"
    before_pointer = pointer.read_bytes()
    with (
        patch.object(code_quality_gate, "run_clean_quality") as executor,
        pytest.raises(RuntimeError, match=detail),
    ):
        code_quality_gate.run_strict_code_quality_gate(
            target, diff_base=request.diff_base, plan=plan
        )
    executor.assert_not_called()
    select.assert_not_called()
    authorize.assert_not_called()
    assert _record_bytes(request.worktree_group) == before
    assert pointer.read_bytes() == before_pointer


@pytest.mark.parametrize("fault", ["duplicate-selection", "omitted-retained-gate"])
def test_physical_suffix_catalog_refuses_duplicate_or_unreported_original_prefix(tmp_path, fault):
    selected, request, profile = selected_execution(tmp_path)
    prepared = certification_records.prepared_from_frozen_run(request.worktree_group, selected.run)

    def suffix(payload):
        reused = [
            {
                "gate": item.certificate.semanticEnvelope.gate,
                "disposition": "reused",
                "started": False,
                "zeroStart": True,
                "rails": [],
                "certificateDigest": item.certificate.certificateDigest,
                "resultManifestDigest": item.result.manifestDigest,
                "originalPublication": published_manifest_payload(item.publication),
            }
            for item in selected.retained
        ]
        payload["gates"] = (
            reused[:1]
            if fault == "omitted-retained-gate"
            else [*reused, *payload["gates"][len(reused) :]]
        )

    publication, _payload = _publish(prepared, profile, nonce=fault, transform=suffix)
    before = _record_bytes(request.worktree_group)
    pointer = request.worktree_group / "reports/quality-report-set.json"
    before_pointer = pointer.read_bytes()
    retained = selected.retained
    if fault == "duplicate-selection":
        retained = (*retained, retained[0])
    expected = (
        "duplicate-retained-gate"
        if fault == "duplicate-selection"
        else "retained-gate-catalog-mismatch"
    )
    with pytest.raises(CertificationContractError) as refused:
        record_terminal_generation(prepared, publication, retained=retained)
    assert refused.value.findings[0]["code"] == expected
    assert _record_bytes(request.worktree_group) == before
    assert pointer.read_bytes() == before_pointer
    selected.validate()


def test_invalid_selected_generation_protection_refuses_before_publication_or_pruning(tmp_path):
    selected, request, profile = selected_execution(tmp_path)
    prepared = certification_records.prepared_from_frozen_run(request.worktree_group, selected.run)
    pointer = request.worktree_group / "reports/quality-report-set.json"
    before_pointer = pointer.read_bytes()
    generations = request.worktree_group / "reports/.quality-report-generations"
    before_generations = {path.name for path in generations.iterdir()}
    before = _record_bytes(request.worktree_group)
    calls = []

    def malformed_selection() -> frozenset[str]:
        calls.append("read-selected-generation")
        return frozenset({selected.retained[0].publication.generation[:-1]})

    with pytest.raises(ValueError, match="frozen set of generation digests"):
        _publish(
            prepared,
            profile,
            nonce="must-not-publish",
            bindings=clean_executor.ReportBindings(
                attestation=None,
                runtime_authority_digest=None,
                protected_generations=malformed_selection,
            ),
        )
    assert calls == ["read-selected-generation"]
    assert pointer.read_bytes() == before_pointer
    assert {path.name for path in generations.iterdir()} == before_generations
    assert _record_bytes(request.worktree_group) == before
    selected.validate()
