"""Exact publication consumption and live certificate evidence retention."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from agents_remember.certification.certificate_models import GateCertificate
from agents_remember.certification.frozen_run.models import freeze_certification_run
from agents_remember.certification.models import CandidateIdentity, GateResultManifest
from agents_remember.certification.repository_profiles.execution import (
    admit_repository_profile_execution,
)
from agents_remember.errors import CertificationContractError
from agents_remember.worktrees.modules.quality import (
    certification_evidence,
    certification_records,
    clean_executor,
    gate,
)
from agents_remember.worktrees.modules.quality.execution.models import RetainedGateExecution
from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifest,
    load_published_quality_manifest,
    parse_published_quality_manifest,
    published_manifest_payload,
    quality_generation_digest,
    quality_report_dependencies,
)
from gate_certification_test_support import (
    _PROFILE_REFERENCE,
    _checkout_with_profile,
    _gate_catalog,
    _green_outcome_factory,
    _lane_for,
)


def _digest(row, name):
    digest = row[name]
    assert isinstance(digest, str)
    return digest


def _publication(row):
    publication = row["publication"]
    assert isinstance(publication, dict)
    return publication


def _refusals(result):
    refused = result["refused"]
    assert isinstance(refused, list)
    return refused


def _arrange(tmp_path: Path):
    code = _checkout_with_profile(tmp_path / "code")
    admitted, lane, candidate = _lane_for(code)
    group = tmp_path / "enclosure"
    prepared = certification_records._persist_admission(
        certification_records.PreparedCertificationRun(
            group, freeze_certification_run(admitted.canonical, lane)
        )
    )
    execution = admit_repository_profile_execution(
        admitted,
        purpose="closeout",
        mode="targeted",
        candidate_identity=CandidateIdentity(kind="git-tree", value=candidate),
        source_selection=lane.repositoryPlan.sourceSelection,
    )
    return code, group, prepared, execution


def _publish(prepared, execution, *, nonce="first", bindings=None, transform=None):
    group = prepared.worktree_group
    exported = group.parent / "export"
    exported.mkdir(exist_ok=True)
    payload = {
        "status": "passed",
        "exitCode": 0,
        "attemptNonce": nonce,
        "gates": _gate_catalog(prepared.lane, exported),
    }
    if transform is not None:
        transform(payload)
    (exported / "clean-quality-results.json").write_text(json.dumps(payload))
    clean_executor._publish_reports(
        exported,
        group / "reports",
        candidate_tree=prepared.candidateTree,
        profile_execution=execution,
        bindings=bindings,
    )
    return load_published_quality_manifest(group / "reports"), payload


def _record(prepared, publication, payload):
    result = certification_records.record_published_generation(
        prepared, publication, payload
    ).as_payload()
    assert result["refused"] == []
    return certification_evidence.read_gate_records(prepared.directory)


def test_unchanged_certificate_reuses_original_generation_and_provenance(tmp_path):
    _code, group, prepared, execution = _arrange(tmp_path)
    first, payload = _publish(prepared, execution)
    records = _record(prepared, first, payload)
    store = prepared.certificate_store()
    original_bytes = {
        row["certificate"]: store.exact_path(
            "certificate", _digest(row, "certificate")
        ).read_bytes()
        for row in records
    }
    later_provenance = prepared.provenance.model_copy(update={"createdAt": "2099-01-01T00:00:00Z"})
    later_admission = prepared.lane.admission.model_copy(update={"provenance": later_provenance})
    later = replace(
        prepared,
        frozen_run=freeze_certification_run(
            prepared.frozen_run.repositoryProfile, replace(prepared.lane, admission=later_admission)
        ),
    )
    second, payload = _publish(later, execution, nonce="different-storage-attempt")
    assert second.generation != first.generation
    repeated = _record(later, second, payload)
    assert [row["certificate"] for row in repeated] == [row["certificate"] for row in records]
    for row in repeated:
        certificate = store.load(GateCertificate, _digest(row, "certificate"))
        result = store.load(GateResultManifest, _digest(row, "manifest"))
        retained = parse_published_quality_manifest(row["publication"])
        assert retained.generation == first.generation
        assert certificate.provenance == prepared.provenance
        assert (
            store.exact_path("certificate", certificate.certificateDigest).read_bytes()
            == original_bytes[certificate.certificateDigest]
        )
        certification_evidence.verify_result_evidence(
            group / "reports", retained, result.railResults
        )


def test_selected_certificate_generation_survives_pruning_until_selection_releases_it(tmp_path):
    _code, group, prepared, execution = _arrange(tmp_path)
    first, payload = _publish(prepared, execution)
    _record(prepared, first, payload)
    for nonce in range(5):
        _publish(prepared, execution, nonce=str(nonce))
    generations = group / "reports/.quality-report-generations"
    assert (generations / first.generation).is_dir()
    assert len(list(generations.iterdir())) <= 4
    assert certification_evidence.protected_certificate_generations(
        group / "reports", store=prepared.certificate_store()
    ) == {first.generation}
    certification_records.journal_gate_records(group, [])
    _publish(prepared, execution, nonce="released")
    assert not (generations / first.generation).exists()


@pytest.mark.parametrize("fault", ["missing", "corrupt", "oversized", "symlink", "parent-link"])
def test_actual_evidence_fault_refuses_certificate_issuance(tmp_path, fault):
    _code, group, prepared, execution = _arrange(tmp_path)
    publication, payload = _publish(prepared, execution)
    reference = payload["gates"][0]["rails"][0]["evidence"][0]["reference"]
    evidence = group / "reports/.quality-report-generations" / publication.generation / reference
    if fault == "missing":
        evidence.unlink()
    elif fault == "corrupt":
        evidence.write_bytes(b"incorrect")
    elif fault == "oversized":
        with evidence.open("ab") as target:
            target.write(b"beyond the accepted size")
    elif fault == "symlink":
        outside = tmp_path / "outside"
        outside.write_bytes(evidence.read_bytes())
        evidence.unlink()
        evidence.symlink_to(outside)
    else:
        original = evidence.parent
        outside = tmp_path / "outside-directory"
        original.rename(outside)
        original.symlink_to(outside, target_is_directory=True)
    result = certification_records.record_published_generation(
        prepared, publication, payload
    ).as_payload()
    assert result["published"] == []
    assert _refusals(result)[0]["refusalCode"] == "certificate-evidence-unavailable"
    assert not list((prepared.directory / "objects/certificate").glob("sha256/*/*.json"))


@pytest.mark.parametrize("field", ["sha256", "size", "reference"])
def test_payload_binding_must_match_accepted_inventory(tmp_path, field):
    _code, _group, prepared, execution = _arrange(tmp_path)
    publication, payload = _publish(prepared, execution)
    binding = payload["gates"][0]["rails"][0]["evidence"][0]
    binding[field] = {"sha256": "0" * 64, "size": 0, "reference": "../escape"}[field]
    result = certification_records.record_published_generation(
        prepared, publication, payload
    ).as_payload()
    assert result["published"] == []
    assert _refusals(result)[0]["refusalCode"].startswith("certificate-evidence-")


def test_reuse_does_not_substitute_current_generation_for_missing_retained_generation(tmp_path):
    _code, group, prepared, execution = _arrange(tmp_path)
    first, payload = _publish(prepared, execution)
    records = _record(prepared, first, payload)
    second, payload = _publish(prepared, execution, nonce="second")
    reference = payload["gates"][0]["rails"][0]["evidence"][0]["reference"]
    original_file = group / "reports/.quality-report-generations" / first.generation / reference
    original_file.unlink()
    result = certification_records.record_published_generation(
        prepared, second, payload
    ).as_payload()
    assert result["published"] == []
    assert _refusals(result)[0]["refusalCode"] == "certificate-evidence-unavailable"
    assert _publication(records[0])["generation"] != second.generation


def test_selected_publication_snapshot_cannot_be_rebound_to_another_generation(tmp_path):
    _code, group, prepared, execution = _arrange(tmp_path)
    first, payload = _publish(prepared, execution)
    records = list(_record(prepared, first, payload))
    _publication(records[0])["generation"] = "f" * 64
    with pytest.raises(CertificationContractError, match="certificate report evidence refused"):
        certification_records.journal_gate_records(group, records)
    assert (
        _publication(certification_evidence.read_gate_records(prepared.directory)[0])["generation"]
        == first.generation
    )


def _foreign_publication(group: Path, publication: PublishedQualityManifest, field: str):
    snapshot = published_manifest_payload(publication)
    replacements = {
        "candidateTree": "f" * 40,
        "profileDigest": "f" * 64,
        "profilePlanDigest": "f" * 64,
        "profileSelectionId": "foreign-selection",
        "executorAdapterId": "foreign-executor",
        "resultDecoder": {
            **publication.result_decoder.model_dump(mode="json"),
            "decoderId": "foreign-decoder",
        },
        "runtimeAuthorityDigest": "f" * 64,
    }
    snapshot[field] = replacements[field]
    identity = {
        name: snapshot[name]
        for name in (
            "profileDigest",
            "profilePlanDigest",
            "profileSelectionId",
            "executorAdapterId",
            "resultDecoder",
        )
    }
    candidate = snapshot["candidateTree"]
    files = snapshot["files"]
    runtime = snapshot.get("runtimeAuthorityDigest")
    assert isinstance(candidate, str) and isinstance(files, dict)
    assert runtime is None or isinstance(runtime, str)
    snapshot["dependencies"] = quality_report_dependencies(
        candidate,
        files,
        publication.attestation,
        identity,
        runtime,
    ).model_dump(mode="json")
    snapshot["generation"] = quality_generation_digest(
        {
            "candidateTree": snapshot["candidateTree"],
            **identity,
            "files": snapshot["files"],
            "dependencies": snapshot["dependencies"],
            "runtimeAuthorityDigest": snapshot.get("runtimeAuthorityDigest"),
        }
    )
    foreign = parse_published_quality_manifest(snapshot)
    generations = group / "reports/.quality-report-generations"
    shutil.copytree(generations / publication.generation, generations / foreign.generation)
    assert foreign.files == publication.files
    assert foreign.generation != publication.generation
    return foreign


def _damage_selected_snapshot(prepared, records):
    # Simulate a retained journal changed outside the validated publication owner.
    path = prepared.directory / "gates.json"
    payload = json.loads(path.read_bytes())
    payload["gates"] = records
    path.write_text(json.dumps(payload))
    return path.read_bytes()


@pytest.mark.parametrize(
    "field",
    [
        "candidateTree",
        "profileDigest",
        "profilePlanDigest",
        "profileSelectionId",
        "executorAdapterId",
        "resultDecoder",
        "runtimeAuthorityDigest",
    ],
)
def test_reuse_refuses_valid_foreign_authority_with_identical_artifact_bytes(tmp_path, field):
    _code, group, prepared, execution = _arrange(tmp_path)
    original, payload = _publish(prepared, execution)
    records = list(_record(prepared, original, payload))
    current, payload = _publish(prepared, execution, nonce="current")
    foreign = _foreign_publication(group, original, field)
    result = prepared.certificate_store().load(GateResultManifest, _digest(records[0], "manifest"))
    certification_evidence.verify_result_evidence(group / "reports", foreign, result.railResults)
    records[0]["publication"] = published_manifest_payload(foreign)
    original_journal = _damage_selected_snapshot(prepared, records)
    refusal = certification_records.record_published_generation(
        prepared, current, payload
    ).as_payload()
    assert refusal["published"] == []
    assert _refusals(refusal)[0]["refusalCode"] == "certificate-evidence-binding-mismatch"
    assert (prepared.directory / "gates.json").read_bytes() == original_journal


@pytest.mark.parametrize("field", ["candidateTree", "profileDigest", "profileSelectionId"])
def test_journal_publication_refuses_foreign_authority_without_replacing_valid_selection(
    tmp_path, field
):
    _code, group, prepared, execution = _arrange(tmp_path)
    original, payload = _publish(prepared, execution)
    records = list(_record(prepared, original, payload))
    records[0]["publication"] = published_manifest_payload(
        _foreign_publication(group, original, field)
    )
    path = prepared.directory / "gates.json"
    original_journal = path.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        certification_records.journal_gate_records(group, records)
    assert caught.value.findings[0]["code"] == "certificate-evidence-binding-mismatch"
    assert path.read_bytes() == original_journal


@pytest.mark.parametrize("field", ["candidateTree", "profileDigest", "profileSelectionId"])
def test_pruning_refuses_a_valid_foreign_selected_publication(tmp_path, field):
    _code, group, prepared, execution = _arrange(tmp_path)
    original, payload = _publish(prepared, execution)
    records = list(_record(prepared, original, payload))
    records[0]["publication"] = published_manifest_payload(
        _foreign_publication(group, original, field)
    )
    _damage_selected_snapshot(prepared, records)
    pointer = group / "reports/quality-report-set.json"
    original_pointer = pointer.read_bytes()
    with pytest.raises(CertificationContractError) as caught:
        _publish(prepared, execution, nonce="must-not-publish")
    assert caught.value.findings[0]["code"] == "certificate-evidence-binding-mismatch"
    assert pointer.read_bytes() == original_pointer


@pytest.mark.parametrize("field", ["profileDigest", "profilePlanDigest", "profileSelectionId"])
def test_current_publication_must_match_the_actual_admitted_repository_plan(tmp_path, field):
    _code, group, prepared, execution = _arrange(tmp_path)
    original, payload = _publish(prepared, execution)
    foreign = _foreign_publication(group, original, field)
    with pytest.raises(RuntimeError, match="another admitted repository plan"):
        certification_records.record_published_generation(prepared, foreign, payload).as_payload()
    assert certification_evidence.read_gate_records(prepared.directory) == ()


def test_journal_capacity_refuses_without_overwriting_the_prior_selection(tmp_path, monkeypatch):
    _code, group, prepared, execution = _arrange(tmp_path)
    publication, payload = _publish(prepared, execution)
    rows = list(_record(prepared, publication, payload))
    path = prepared.directory / "gates.json"
    original = path.read_bytes()
    monkeypatch.setattr(certification_records, "MAX_GATE_RECORD_BYTES", 10)
    with pytest.raises(CertificationContractError, match="byte bound"):
        certification_records.journal_gate_records(group, rows)
    assert path.read_bytes() == original
    with pytest.raises(CertificationContractError):
        certification_evidence.validate_gate_records(rows * 2)


@pytest.mark.parametrize("payload", [b"not-json", b"{}", b"[]"])
def test_malformed_selected_journal_is_not_treated_as_empty(tmp_path, payload):
    records = tmp_path / "records"
    records.mkdir()
    (records / "gates.json").write_bytes(payload)
    with pytest.raises(CertificationContractError):
        certification_evidence.read_gate_records(records)


@pytest.mark.parametrize("fault", ["unreadable", "oversized"])
def test_unavailable_selected_journal_refuses_publication_without_releasing_generations(
    tmp_path, monkeypatch, fault
):
    _code, group, prepared, execution = _arrange(tmp_path)
    original, payload = _publish(prepared, execution)
    _record(prepared, original, payload)
    path = prepared.directory / "gates.json"
    journal_bytes = path.read_bytes()
    pointer = group / "reports/quality-report-set.json"
    pointer_bytes = pointer.read_bytes()
    generations = group / "reports/.quality-report-generations"
    prior_generations = set(generations.iterdir())
    with monkeypatch.context() as patch:
        if fault == "unreadable":
            open_path = Path.open

            def refuse_selected_open(self, *args, **kwargs):
                if self == path:
                    raise PermissionError("selected journal permission denied")
                return open_path(self, *args, **kwargs)

            patch.setattr(Path, "open", refuse_selected_open)
            code = "certificate-evidence-journal-unreadable"
        else:
            patch.setattr(certification_evidence, "MAX_GATE_RECORD_BYTES", len(journal_bytes) - 1)
            code = "certificate-evidence-capacity"
        with pytest.raises(CertificationContractError) as caught:
            _publish(prepared, execution, nonce="must-not-replace")
        assert caught.value.findings[0]["code"] == code
    assert path.read_bytes() == journal_bytes
    assert pointer.read_bytes() == pointer_bytes
    assert set(generations.iterdir()) == prior_generations
    assert certification_evidence.protected_certificate_generations(
        group / "reports", store=prepared.certificate_store()
    ) == {original.generation}


@pytest.mark.parametrize(
    "fault,code,detail",
    [
        ("non-object", "journal-invalid", "gate record is not an object"),
        ("boolean-gate", "journal-invalid", "gate identity is invalid"),
        ("zero-gate", "journal-invalid", "gate identity is invalid"),
        ("duplicate-gate", "journal-invalid", "duplicate selected gate"),
        ("unknown-certificate-gate", "journal-invalid", "certificate gate identity is invalid"),
        ("missing-certificate", "binding-missing", "invalid selected certificate"),
        ("short-certificate", "binding-missing", "invalid selected certificate"),
        ("nonhex-manifest", "binding-missing", "invalid selected manifest"),
    ],
)
def test_invalid_selected_records_cannot_replace_a_live_journal(tmp_path, fault, code, detail):
    _code, group, prepared, execution = _arrange(tmp_path)
    publication, payload = _publish(prepared, execution)
    rows = list(_record(prepared, publication, payload))
    path = prepared.directory / "gates.json"
    original = path.read_bytes()
    if fault == "non-object":
        damaged = [cast(dict[str, object], None)]
    elif fault == "duplicate-gate":
        damaged = [rows[0], rows[0]]
    else:
        field, value = {
            "boolean-gate": ("gate", True),
            "zero-gate": ("gate", 0),
            "unknown-certificate-gate": ("gate", 6),
            "missing-certificate": ("certificate", None),
            "short-certificate": ("certificate", "a" * 63),
            "nonhex-manifest": ("manifest", "G" * 64),
        }[fault]
        damaged = [{**rows[0], field: value}]
    with pytest.raises(CertificationContractError) as caught:
        certification_records.journal_gate_records(group, damaged)
    assert caught.value.findings[0]["code"] == f"certificate-evidence-{code}"
    assert caught.value.findings[0]["detail"] == detail
    assert path.read_bytes() == original
    assert certification_evidence.protected_certificate_generations(
        group / "reports", store=prepared.certificate_store()
    ) == {publication.generation}


def test_selected_gate_cannot_claim_another_gates_certificate(tmp_path):
    _code, group, prepared, execution = _arrange(tmp_path)
    publication, payload = _publish(prepared, execution)
    rows = list(_record(prepared, publication, payload))
    path = prepared.directory / "gates.json"
    original = path.read_bytes()
    swapped = [{**rows[0], "gate": rows[1]["gate"]}]
    with pytest.raises(CertificationContractError) as caught:
        certification_records.journal_gate_records(group, swapped)
    assert caught.value.findings[0]["code"] == "certificate-evidence-binding-mismatch"
    assert caught.value.findings[0]["detail"] == "selected gate identity differs"
    assert path.read_bytes() == original


def test_reuse_refuses_certificate_rebound_to_another_existing_result(tmp_path):
    _code, _group, prepared, execution = _arrange(tmp_path)
    first, payload = _publish(prepared, execution)
    rows = list(_record(prepared, first, payload))
    current, payload = _publish(prepared, execution, nonce="current")
    assert rows[0]["manifest"] != rows[1]["manifest"]
    foreign = prepared.certificate_store().load(GateResultManifest, _digest(rows[1], "manifest"))
    assert foreign.gate == rows[1]["gate"]
    rows[0]["manifest"] = rows[1]["manifest"]
    original = _damage_selected_snapshot(prepared, rows)
    result = certification_records.record_published_generation(
        prepared, current, payload
    ).as_payload()
    assert result["published"] == []
    assert _refusals(result)[0]["refusalCode"] == "certificate-evidence-binding-mismatch"
    assert (prepared.directory / "gates.json").read_bytes() == original


def test_actual_host_gate_refuses_green_process_with_unavailable_certificate_bytes(
    tmp_path, monkeypatch
):
    code, group, prepared, _execution = _arrange(tmp_path)
    factory = _green_outcome_factory(group, prepared.lane, prepared.candidateTree)

    def corrupt(request):
        outcome = factory(request)
        manifest = outcome.manifest
        assert manifest is not None
        reference = next(path for path in manifest.files if path.startswith("rail-evidence/"))
        (group / "reports/.quality-report-generations" / manifest.generation / reference).unlink()
        return outcome

    monkeypatch.setattr(gate, "run_clean_quality", corrupt)
    monkeypatch.setattr(
        certification_records, "prepare_certification_records", lambda *args, **kwargs: prepared
    )
    with pytest.raises(RuntimeError, match="certificate evidence refused"):
        gate.run_strict_code_quality_gate(
            gate.QualityGateTarget(
                code_worktree=code,
                worktree_group=group,
                repository_id="agents-remember",
                profile_reference=_PROFILE_REFERENCE,
            ),
            diff_base="HEAD",
            plan=gate.QualityGatePlan(mode="targeted"),
        )


def test_admission_reuse_preserves_original_bytes_and_provenance(tmp_path):
    _code, _group, prepared, _execution = _arrange(tmp_path)
    first = certification_records._persist_admission(prepared)
    path = first.certificate_store().exact_path("admission", first.lane.admission.admissionDigest)
    original_bytes = path.read_bytes()
    changed = first.provenance.model_copy(update={"createdAt": "2099-01-01T00:00:00Z"})
    new_admission = first.lane.admission.model_copy(update={"provenance": changed})
    later = replace(
        first,
        frozen_run=freeze_certification_run(
            first.frozen_run.repositoryProfile, replace(first.lane, admission=new_admission)
        ),
    )
    reopened = certification_records._persist_admission(later)
    assert reopened.provenance == first.provenance
    assert reopened.lane.admission == first.lane.admission
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize("attestation", [None, {"producer": "gate-owner", "digest": "a" * 64}])
def test_exact_manifest_snapshot_roundtrips_all_generation_authority(tmp_path, attestation):
    _code, group, prepared, execution = _arrange(tmp_path)
    publication, _payload = _publish(
        prepared,
        execution,
        bindings=clean_executor.ReportBindings(
            attestation=attestation, runtime_authority_digest="c" * 64
        ),
    )
    reopened = parse_published_quality_manifest(published_manifest_payload(publication))
    assert reopened == publication
    assert reopened.attestation == attestation
    decoder = clean_executor.published_report_path_from_manifest(
        group / "reports", reopened, reopened.result_decoder.artifactPath
    )
    assert (
        hashlib.sha256(decoder.read_bytes()).hexdigest()
        == reopened.require_file(reopened.result_decoder.artifactPath).sha256
    )


@pytest.mark.parametrize("fault", ["relative-name", "generation"])
def test_unsafe_retained_locator_refuses_before_opening_any_path(tmp_path, monkeypatch, fault):
    _code, group, prepared, execution = _arrange(tmp_path)
    publication, _payload = _publish(prepared, execution)
    name = publication.result_decoder.artifactPath
    pointer = group / "reports/quality-report-set.json"
    original = pointer.read_bytes()
    if fault == "relative-name":
        name = "../outside.json"
    else:
        publication = replace(publication, generation="../outside")

    def refuse_any_open(*_args, **_kwargs):
        raise AssertionError("an unsafe locator must be refused before reading the filesystem")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", refuse_any_open)
        with pytest.raises(RuntimeError, match="published report locator is not confined"):
            clean_executor.published_report_path_from_manifest(group / "reports", publication, name)
    assert pointer.read_bytes() == original


@pytest.mark.parametrize(
    "relative",
    ["dashboard-e2e-result.json", "provider-integration-result.json", "teardown-proof.json"],
)
def test_missing_required_gate_four_artifact_preserves_only_the_green_prefix(tmp_path, relative):
    _code, group, prepared, execution = _arrange(tmp_path)
    publication, payload = _publish(prepared, execution)
    (group / "reports/.quality-report-generations" / publication.generation / relative).unlink()
    result = certification_records.record_published_generation(
        prepared, publication, payload
    ).as_payload()
    published = result["published"]
    assert isinstance(published, list)
    assert [row["gate"] for row in published] == [1, 2, 3]
    assert _refusals(result)[0]["gate"] == 4
    assert _refusals(result)[0]["refusalCode"] == "certificate-evidence-unavailable"


def test_missing_live_generation_refuses_before_creating_another_generation(tmp_path):
    _code, group, prepared, execution = _arrange(tmp_path)
    first, payload = _publish(prepared, execution)
    _record(prepared, first, payload)
    generations = group / "reports/.quality-report-generations"
    (generations / first.generation).rename(tmp_path / "retained-away")
    pointer = group / "reports/quality-report-set.json"
    original = pointer.read_bytes()
    with pytest.raises(CertificationContractError):
        _publish(prepared, execution, nonce="must-not-publish")
    assert pointer.read_bytes() == original
    assert list(generations.iterdir()) == []


@pytest.mark.parametrize("disposition", ["red", "interrupted"])
def test_complete_failed_catalog_retains_typed_manifest_and_physical_generation(
    tmp_path, disposition
):
    _code, group, prepared, execution = _arrange(tmp_path)
    failing_key = prepared.lane.certificationPlan.gates[0].waves[-1][0].key

    def fail_gate(payload):
        payload.update(status="failed", exitCode=130 if disposition == "interrupted" else 1)
        payload["gates"] = payload["gates"][:1]
        payload["gates"][0]["disposition"] = disposition
        for rail in payload["gates"][0]["rails"]:
            if rail["key"] == failing_key:
                rail.update(
                    status="fail",
                    failureCode="execution-interrupted"
                    if disposition == "interrupted"
                    else "observed-failure",
                )

    publication, payload = _publish(prepared, execution, transform=fail_gate)
    recorded = certification_records.record_published_generation(prepared, publication, payload)
    assert recorded.as_payload()["refused"] == []
    assert recorded.as_payload()["certificates"] == []
    assert len(recorded.terminals) == 1
    terminal = recorded.terminals[0]
    assert terminal.result.disposition == "red"
    assert terminal.certificate is terminal.certificateReference is None
    assert terminal.publication == publication
    assert prepared.certificate_store().load_reference(terminal.resultReference) == terminal.result
    assert len(terminal.result.railResults) == len(prepared.lane.certificationPlan.gates[0].rails)
    assert certification_evidence.protected_certificate_generations(
        group / "reports", store=prepared.certificate_store()
    ) == frozenset({publication.generation})
    _publish(prepared, execution, nonce="later")
    assert (group / "reports/.quality-report-generations" / publication.generation).is_dir()
    certification_evidence.verify_result_evidence(
        group / "reports", publication, terminal.result.railResults
    )


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "foreign",
        "unknown-status",
        "junk",
        "bad-evidence",
        "contradictory-disposition",
    ],
)
def test_invalid_failed_catalog_never_synthesizes_a_terminal_result(tmp_path, damage):
    _code, _group, prepared, execution = _arrange(tmp_path)

    def damage_catalog(payload):
        payload["gates"] = payload["gates"][:1]
        payload["gates"][0]["disposition"] = "red"
        rails = payload["gates"][0]["rails"]
        if damage == "missing":
            rails.pop()
        elif damage == "duplicate":
            rails[-1] = rails[0]
        elif damage == "foreign":
            rails[0]["key"] = "foreign@v1"
        elif damage == "unknown-status":
            rails[0]["status"] = "interrupted"
        elif damage == "junk":
            rails[0] = "not a rail observation"
        elif damage == "bad-evidence":
            rails[0]["evidence"] = ["not bounded evidence"]

    publication, payload = _publish(prepared, execution, transform=damage_catalog)
    recorded = certification_records.record_published_generation(prepared, publication, payload)
    assert recorded.terminals == ()
    assert recorded.as_payload()["certificates"] == []
    expected = {
        "bad-evidence": "terminal-catalog-invalid",
        "contradictory-disposition": "terminal-disposition-mismatch",
    }.get(damage, "missing-run-evidence")
    assert _refusals(recorded.as_payload())[0]["refusalCode"] == expected


def test_supplied_frozen_run_reconstruction_ignores_mutable_profile_and_summary(
    tmp_path, monkeypatch
):
    code, group, prepared, _execution = _arrange(tmp_path)
    original = prepared.frozen_run
    reference = prepared.frozen_reference
    (prepared.directory / "admission.json").write_text("not selection authority")
    (code / "mcp/certification-profile-v1.json").unlink()

    def forbid_reload(*args, **kwargs):
        raise AssertionError("recovery must use the supplied frozen run")

    monkeypatch.setattr(certification_records, "load_repository_profile", forbid_reload)
    restored = certification_records.prepared_from_frozen_run(group, original)
    assert restored.frozen_run == original
    assert restored.frozen_reference == reference
    assert restored.provenance == original.provenance


@pytest.mark.parametrize(
    "fault",
    ["none", "missing-selection", "started", "nonboolean", "changed-provenance", "missing-object"],
)
def test_reused_catalog_selects_original_typed_objects_without_reissuing(
    tmp_path, monkeypatch, fault
):
    _code, _group, prepared, execution = _arrange(tmp_path)
    original, payload = _publish(prepared, execution)
    first = certification_records.record_published_generation(prepared, original, payload)
    terminal = first.terminals[0]
    assert terminal.certificate is not None
    certificate = terminal.certificate
    retained = RetainedGateExecution(certificate, terminal.result, original)

    def reuse_first(payload):
        payload["gates"] = [
            {
                "gate": 1,
                "disposition": "reused",
                "started": False,
                "zeroStart": True,
                "rails": [],
                "certificateDigest": certificate.certificateDigest,
                "resultManifestDigest": terminal.result.manifestDigest,
                "originalPublication": published_manifest_payload(original),
            }
        ]

    current, payload = _publish(prepared, execution, nonce="retry", transform=reuse_first)
    selected = (retained,)
    if fault == "missing-selection":
        selected = ()
    elif fault == "started":
        payload["gates"][0]["started"] = True
    elif fault == "nonboolean":
        payload["gates"][0]["started"] = 0
    elif fault == "changed-provenance":
        changed = certificate.model_copy(
            update={
                "provenance": certificate.provenance.model_copy(
                    update={"createdAt": "2099-01-01T00:00:00Z"}
                )
            }
        )
        selected = (replace(retained, certificate=changed),)
    elif fault == "missing-object":
        prepared.certificate_store().exact_path(
            "certificate", certificate.certificateDigest
        ).unlink()

    def forbid_compile(*args, **kwargs):
        raise AssertionError("retained objects must never be reissued")

    monkeypatch.setattr(certification_records, "compile_gate_certificate", forbid_compile)
    monkeypatch.setattr(certification_records, "compile_gate_result_manifest", forbid_compile)
    recorded = certification_records.record_published_generation(
        prepared, current, payload, retained=selected
    )
    if fault == "none":
        assert recorded.terminals == (terminal,)
        assert recorded.as_payload()["refused"] == []
        assert recorded.terminals[0].publication != current
    else:
        assert recorded.terminals == ()
        assert recorded.as_payload()["refused"]
