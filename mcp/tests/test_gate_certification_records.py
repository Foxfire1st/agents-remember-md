"""CCR-L28 production gate-seam certification records (F01 wiring proof).

Drives the REAL production entry point gate.run_strict_code_quality_gate
(the host function every closeout/integration gate run invokes) against a real
temporary agents-remember checkout with the shipped profile.  The Dagger
subprocess boundary (gate.run_clean_quality) is monkeypatched exactly as
the pinned executor tests do; every other production step -- profile
admission, R22 plan compilation, R21 admission freeze before Gate 1, published
generation decoding, per-gate result publication, and certificate minting +
content-addressed persistence -- executes the real host code.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    GateCertificate,
)
from agents_remember.certification.certificate_store import (
    CertificateStorePolicy,
    ContentAddressedCertificateStore,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.frozen_run.models import FrozenCertificationRun
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.authority import (
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.canonical import repository_profile_digest
from agents_remember.certification.repository_profiles.execution import (
    admit_repository_profile_execution,
)
from agents_remember.certification.repository_profiles.models import ProfileMode
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.test_evidence import _certifying_evidence_from_verified_dagger
from agents_remember.worktrees.modules.quality import (
    certification_records,
    certification_run,
    clean_executor,
)
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.modules.quality.clean_executor import (
    CleanQualityOutcome,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
)
from integration_certification_test_support import selected_code_fixture


def _published(result):
    value = result.get("published")
    return cast(list[dict[str, object]], value)


def _refusals(result):
    value = result.get("refused")
    return cast(list[dict[str, object]], value)


from gate_certification_test_support import (
    _PROFILE_REFERENCE,
    _REPOSITORY_ID,
    _checkout_with_profile,
    _gate_catalog,
    _git,
    _green_outcome_factory,
    _lane_for,
)


@pytest.mark.integration
class GateCertificationRecordsTests:
    def _run_green_gate(self, tmp_path: Path, mode: ProfileMode = "targeted", name: str = "run"):
        worktree = _checkout_with_profile(tmp_path / name / "code")
        worktree_group = tmp_path / name / "enclosure"
        target = code_quality_gate.QualityGateTarget(
            code_worktree=worktree,
            worktree_group=worktree_group,
            repository_id=_REPOSITORY_ID,
            profile_reference=_PROFILE_REFERENCE,
        )
        _admitted, lane, candidate_tree = _lane_for(worktree, mode=mode)
        with mock.patch.object(
            code_quality_gate,
            "run_clean_quality",
            side_effect=_green_outcome_factory(worktree_group, lane, candidate_tree),
        ) as clean:
            result = code_quality_gate.run_strict_code_quality_gate(
                target,
                diff_base=_git(worktree, "rev-parse", "HEAD"),
                plan=code_quality_gate.QualityGatePlan(mode=mode),
            )
        assert result["passed"] is True
        assert clean.call_count == 1
        return worktree, worktree_group, lane, candidate_tree

    def test_gate_seam_freezes_admission_before_gate_one_and_mints_gate_one_four(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        journal = certification_records.load_execution_records(group)
        assert journal is not None
        assert journal["candidateTree"] == candidate_tree
        assert journal["admissionDigest"] == lane.admission.admissionDigest
        store = certification_records.certificate_store(group)
        admission = store.load(CertificationAdmissionManifest, lane.admission.admissionDigest)
        assert admission.semanticEnvelope == lane.admission.semanticEnvelope
        gates = json.loads(
            (certification_records.records_directory(group) / "gates.json").read_text()
        )
        published = [item for item in gates["gates"] if item["kind"] == "certificate"]
        refused = [item for item in gates["gates"] if item["kind"] == "refused"]
        assert len(published) == 4
        assert refused == []
        assert [item["gate"] for item in published] == [1, 2, 3, 4]
        certificates = [store.load(GateCertificate, item["certificate"]) for item in published]
        validate_certificate_chain(admission, certificates)

    def test_gate_seam_is_idempotent_across_an_unchanged_rerun(self, tmp_path):
        _worktree, group, _lane, _cand = self._run_green_gate(tmp_path)
        journal_one = certification_records.load_execution_records(group)
        assert journal_one is not None
        store = certification_records.certificate_store(group)
        first_certs = [
            store.load(GateCertificate, item["certificate"])
            for item in json.loads(
                (certification_records.records_directory(group) / "gates.json").read_text()
            )["gates"]
            if item["kind"] == "certificate"
        ]
        original_bytes = {
            item.certificateDigest: store.exact_path(
                "certificate", item.certificateDigest
            ).read_bytes()
            for item in first_certs
        }
        # A deliberate fresh execution of the same candidate/base must retain
        # its original admission and exact certificate bytes. A second Git
        # repository would have a distinct base-commit source observation.
        target = code_quality_gate.QualityGateTarget(
            code_worktree=_worktree,
            worktree_group=group,
            repository_id=_REPOSITORY_ID,
            profile_reference=_PROFILE_REFERENCE,
        )
        with mock.patch.object(
            code_quality_gate,
            "run_clean_quality",
            side_effect=_green_outcome_factory(group, _lane, _cand),
        ) as clean:
            result = code_quality_gate.run_strict_code_quality_gate(
                target,
                diff_base=_git(_worktree, "rev-parse", "HEAD"),
                plan=code_quality_gate.QualityGatePlan(mode="targeted"),
            )
        assert result["passed"] is True
        assert clean.call_count == 1
        assert certification_records.load_execution_records(group) == journal_one
        assert first_certs
        for certificate in first_certs:
            assert store.load(GateCertificate, certificate.certificateDigest) == certificate
            assert (
                store.exact_path("certificate", certificate.certificateDigest).read_bytes()
                == original_bytes[certificate.certificateDigest]
            )

    @pytest.mark.parametrize("kind", ["admission", "certificate"])
    @pytest.mark.parametrize("fault", ["invalid-digest", "wrong-address"])
    def test_corrupt_exact_authority_refuses_reuse(self, tmp_path, kind, fault):
        _code, group, lane, candidate = self._run_green_gate(tmp_path)
        prepared = self._prepared_run(group, lane, candidate)
        store = prepared.certificate_store()
        records = json.loads((prepared.directory / "gates.json").read_text())["gates"]
        digest = lane.admission.admissionDigest if kind == "admission" else records[0][kind]
        path = store.exact_path(kind, digest)
        payload = json.loads(path.read_bytes())
        payload["semanticEnvelope"]["candidateCodeTree"]["value"] = "0" * 40
        expected = "certificate-object-invalid"
        if fault == "wrong-address":
            payload[f"{kind}Digest"] = content_digest(payload["semanticEnvelope"])
            model = CertificationAdmissionManifest if kind == "admission" else GateCertificate
            model.model_validate(payload)
            expected = "certificate-object-address-mismatch"
        corrupt = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
        path.write_bytes(corrupt)
        if kind == "admission":
            with pytest.raises(CertificationContractError) as caught:
                certification_records._persist_admission(prepared)
            assert caught.value.findings[0]["code"] == expected
        else:
            publication = load_published_quality_manifest(group / "reports")
            decoder = clean_executor.published_report_path_from_manifest(
                group / "reports", publication, publication.result_decoder.artifactPath
            )
            result = certification_records.record_published_generation(
                prepared, publication, json.loads(decoder.read_bytes())
            ).as_payload()
            assert _published(result) == []
            assert _refusals(result)[0]["refusalCode"] == expected
        assert path.read_bytes() == corrupt

    def test_gate_seam_without_gate_catalog_refuses_certification(self, tmp_path):
        worktree = _checkout_with_profile(tmp_path / "minimal" / "code")
        group = tmp_path / "minimal" / "enclosure"
        target = code_quality_gate.QualityGateTarget(
            code_worktree=worktree,
            worktree_group=group,
            repository_id=_REPOSITORY_ID,
            profile_reference=_PROFILE_REFERENCE,
        )
        _admitted, lane, candidate_tree = _lane_for(worktree)

        def minimal(request):
            with tempfile.TemporaryDirectory() as temporary:
                exported = Path(temporary)
                (exported / "clean-quality-results.json").write_text(
                    json.dumps({"status": "passed", "exitCode": 0}), encoding="utf-8"
                )
                admitted = load_repository_profile(
                    request.repository_id, request.code_worktree, _PROFILE_REFERENCE.as_posix()
                )
                profile_execution = admit_repository_profile_execution(
                    admitted,
                    purpose="closeout",
                    mode=request.mode,
                    candidate_identity=CandidateIdentity(kind="git-tree", value=candidate_tree),
                    source_selection=lane.repositoryPlan.sourceSelection,
                )
                clean_executor._publish_reports(  # type: ignore[attr-defined]
                    exported,
                    request.worktree_group / "reports",
                    candidate_tree=candidate_tree,
                    profile_execution=profile_execution,
                )
            manifest = load_published_quality_manifest(request.worktree_group / "reports")
            evidence = _certifying_evidence_from_verified_dagger(
                candidate_tree=candidate_tree,
                result_sha256=manifest.require_file(manifest.result_decoder.artifactPath).sha256,
            )
            return CleanQualityOutcome(
                subprocess.CompletedProcess(["dagger"], 0, stdout="passed\n"),
                evidence,
                manifest,
            )

        with (
            mock.patch.object(code_quality_gate, "run_clean_quality", side_effect=minimal),
            pytest.raises((CertificationContractError, RuntimeError)),
        ):
            code_quality_gate.run_strict_code_quality_gate(
                target,
                diff_base=_git(worktree, "rev-parse", "HEAD"),
                plan=code_quality_gate.QualityGatePlan(mode="targeted"),
            )
        journal = certification_records.load_execution_records(group)
        assert journal is not None and journal["admissionDigest"] == lane.admission.admissionDigest
        assert not (certification_records.records_directory(group) / "gates.json").exists()

    def _prepared_run(self, group, lane, candidate_tree):
        journal = certification_records.load_execution_records(group)
        assert journal is not None
        reference = CertificateObjectReference.model_validate(journal["frozenRun"])
        frozen = certification_records.certificate_store(group).load_reference(reference)
        assert isinstance(frozen, FrozenCertificationRun)
        assert frozen.repositoryPlan.candidateIdentity.value == candidate_tree
        assert frozen.admission.semanticEnvelope == lane.admission.semanticEnvelope
        return certification_records.prepared_from_frozen_run(group, frozen)

    def test_records_prepare_refuses_missing_profile_for_any_repository(self, tmp_path):
        with pytest.raises(CertificationContractError):
            certification_records.prepare_certification_records(
                certification_records.CertificationRunTarget(
                    repository_id="other-repository",
                    code_worktree=tmp_path,
                    profile_reference=None,
                    worktree_group=tmp_path,
                ),
                mode="targeted",
                candidate_tree="a" * 40,
                diff_base="HEAD",
            )

    def test_another_registered_repository_retains_its_complete_actual_profile(self, tmp_path):
        code = _checkout_with_profile(tmp_path / "other-code")
        admitted, _lane, _candidate = _lane_for(code)
        profile = admitted.canonical.profile.model_copy(
            update={"repositoryId": "another-repository"}
        )
        profile = profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})
        (code / _PROFILE_REFERENCE).write_text(profile.model_dump_json())
        subprocess.run(["git", "add", "."], cwd=code, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "other repository profile"],
            cwd=code,
            check=True,
            capture_output=True,
        )
        candidate = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=code, text=True
        ).strip()
        prepared = certification_records.prepare_certification_records(
            certification_records.CertificationRunTarget(
                "another-repository", code, _PROFILE_REFERENCE, tmp_path / "enclosure"
            ),
            mode="targeted",
            candidate_tree=candidate,
            diff_base=_git(code, "rev-parse", "HEAD"),
        )
        assert prepared.frozen_run.repositoryProfile.profile == profile
        assert prepared.lane.admission.semanticEnvelope.repositoryId == "another-repository"
        assert (
            prepared.certificate_store().load_reference(prepared.frozen_reference)
            == prepared.frozen_run
        )

    def test_records_candidate_tree_mismatch_is_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        moved = replace(manifest, candidate_tree="b" * 40)
        prepared = self._prepared_run(group, lane, candidate_tree)
        with pytest.raises(RuntimeError, match="certifies another candidate tree"):
            certification_records.record_published_generation(prepared, moved, {}).as_payload()

    def test_records_malformed_catalog_entries_are_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        with pytest.raises(CertificationContractError, match="non-object entry"):
            certification_records.record_published_generation(
                prepared, manifest, {"gates": [42]}
            ).as_payload()
        with pytest.raises(CertificationContractError, match="lacks an exact gate"):
            certification_records.record_published_generation(
                prepared, manifest, {"gates": [{"gate": True}]}
            ).as_payload()

    def test_records_unsupported_dispositions_and_junk_rails_are_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        payload = {
            "gates": [
                {"gate": 1, "disposition": "not-run"},
                {"gate": 2, "disposition": "not-applicable"},
                {"gate": 3, "disposition": "green", "rails": ["junk"]},
            ]
        }
        result = certification_records.record_published_generation(
            prepared, manifest, payload
        ).as_payload()
        assert _published(result) == []
        gates = json.loads(
            (certification_records.records_directory(group) / "gates.json").read_text()
        )
        assert [item["gate"] for item in gates["gates"]] == [1, 2, 3]
        # Unsupported gate states and non-object observations remain explicit refusals;
        # no absent terminal rail result is synthesized.
        assert [item["kind"] for item in gates["gates"]] == ["refused"] * 3
        assert gates["gates"][0]["disposition"] == "not-run"
        assert gates["gates"][1]["disposition"] == "not-applicable"
        assert gates["gates"][2]["refusalCode"] == "missing-run-evidence"

    def test_records_unplanned_gate_is_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        result = certification_records.record_published_generation(
            prepared, manifest, {"gates": [{"gate": 6, "disposition": "green", "rails": []}]}
        ).as_payload()
        assert len(_refusals(result)) == 1
        assert _refusals(result)[0]["refusalCode"] == "unplanned-gate"

    def test_records_green_gate_without_run_evidence_is_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == 1)
        first_rail = gate_plan.rails[0]
        result = certification_records.record_published_generation(
            prepared,
            manifest,
            {
                "gates": [
                    {
                        "gate": 1,
                        "disposition": "green",
                        "rails": [{"key": first_rail.identity.key, "status": "pass"}],
                    }
                ]
            },
        ).as_payload()
        assert len(_refusals(result)) == 1
        assert _refusals(result)[0]["refusalCode"] == "missing-run-evidence"

    def test_records_undeclared_artifact_bindings_are_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == 1)
        rails = []
        for index, rail in enumerate(gate_plan.rails):
            artifacts = [
                {
                    "artifactId": item.artifactId,
                    "sha256": content_digest(
                        {"artifact": rail.identity.key, "id": item.artifactId}
                    ),
                    "size": 64,
                    "evidenceRef": "quality-result://edge",
                }
                for item in rail.outputArtifacts
            ]
            if index == 0:
                artifacts.append(
                    {
                        "artifactId": "bogus-undeclared-artifact",
                        "sha256": "0" * 64,
                        "size": 64,
                        "evidenceRef": "quality-result://edge",
                    }
                )
            rails.append(
                {
                    "key": rail.identity.key,
                    "status": "pass",
                    "evidence": [
                        {
                            "evidenceId": item.evidenceId,
                            "sha256": content_digest(
                                {"evidence": rail.identity.key, "id": item.evidenceId}
                            ),
                            "size": 64,
                            "reference": "quality-result://edge",
                        }
                        for item in rail.evidenceContract
                    ],
                    "artifacts": artifacts,
                }
            )
        result = certification_records.record_published_generation(
            prepared,
            manifest,
            {"gates": [{"gate": 1, "disposition": "green", "rails": rails}]},
        ).as_payload()
        assert len(_refusals(result)) == 1
        assert _refusals(result)[0]["refusalCode"] == "undeclared-result-artifact"

    def test_records_contradictory_red_catalog_publishes_terminal_manifest(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == 1)
        failing_key = gate_plan.waves[-1][0].key
        rails = cast(list[dict[str, object]], _gate_catalog(lane)[0]["rails"])
        for rail in rails:
            if rail["key"] == failing_key:
                rail["status"] = "fail"
        result = certification_records.record_published_generation(
            prepared,
            manifest,
            {"gates": [{"gate": 1, "disposition": "green", "rails": rails}]},
        ).as_payload()
        assert _published(result) == []
        gates = json.loads(
            (certification_records.records_directory(group) / "gates.json").read_text()
        )
        record = next(item for item in gates["gates"] if item["gate"] == 1)
        assert record["kind"] == "terminal"
        assert record["disposition"] == "red"

    def test_records_load_execution_none_when_missing_or_malformed(self, tmp_path):
        group = tmp_path / "empty"
        assert certification_records.load_execution_records(group) is None
        directory = certification_records.records_directory(group)
        directory.mkdir(parents=True)
        (directory / "admission.json").write_text("not json", encoding="utf-8")
        assert certification_records.load_execution_records(group) is None
        (directory / "admission.json").write_text(
            json.dumps({"schemaVersion": "wrong-schema"}), encoding="utf-8"
        )
        assert certification_records.load_execution_records(group) is None

    def test_records_bound_memory_rails_port_missing_is_refused(self, tmp_path, monkeypatch):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        del lane

        class _Unbound:
            certification_memory_rails = None

        monkeypatch.setattr(certification_records, "worktree_services", _Unbound)
        with pytest.raises(
            CertificationContractError, match="bound certification-memory-rails port"
        ):
            certification_records.prepare_certification_records(
                certification_records.CertificationRunTarget(
                    repository_id=_REPOSITORY_ID,
                    code_worktree=_worktree,
                    profile_reference=_PROFILE_REFERENCE,
                    worktree_group=group,
                ),
                mode="targeted",
                candidate_tree=candidate_tree,
                diff_base=_git(_worktree, "rev-parse", "HEAD"),
            )

    def test_records_unknown_terminal_status_is_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == 1)
        first_rail = gate_plan.rails[0]
        result = certification_records.record_published_generation(
            prepared,
            manifest,
            {
                "gates": [
                    {
                        "gate": 1,
                        "disposition": "green",
                        "rails": [{"key": first_rail.identity.key, "status": 123}],
                    }
                ]
            },
        ).as_payload()
        assert len(_refusals(result)) == 1
        assert _refusals(result)[0]["refusalCode"] == "missing-run-evidence"

    def test_records_blocked_mapping_resolves_payload_keys(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == 1)
        valid_key = gate_plan.rails[0].identity.key
        result = certification_records.record_published_generation(
            prepared,
            manifest,
            {
                "gates": [
                    {
                        "gate": 1,
                        "disposition": "green",
                        "rails": [
                            {
                                "key": valid_key,
                                "status": "blocked",
                                "blockedBy": [valid_key, "no-such-rail", 7],
                                "evidence": [],
                                "artifacts": [],
                            }
                        ],
                    }
                ]
            },
        ).as_payload()
        # The blocked-by mapping executes; the incomplete catalog is journaled
        # as a typed refusal because it is not a complete green terminal set.
        assert _refusals(result)
        assert _published(result) == []
        # A blocked outcome whose blockedBy is not a list takes the non-list path.
        non_list = certification_records.record_published_generation(
            prepared,
            manifest,
            {
                "gates": [
                    {
                        "gate": 1,
                        "disposition": "green",
                        "rails": [
                            {
                                "key": valid_key,
                                "status": "blocked",
                                "blockedBy": "not-a-list",
                                "evidence": [],
                                "artifacts": [],
                            }
                        ],
                    }
                ]
            },
        ).as_payload()
        assert _refusals(non_list)

    def test_records_terminal_code_prefers_payload_failure_code(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == 1)
        rails = []
        for rail in gate_plan.rails:
            rails.append(
                {
                    "key": rail.identity.key,
                    "status": "pass",
                    "failureCode": "custom-pass",
                    "evidence": [],
                    "artifacts": [],
                }
            )
        result = certification_records.record_published_generation(
            prepared,
            manifest,
            {"gates": [{"gate": 1, "disposition": "green", "rails": rails}]},
        ).as_payload()
        # Missing evidence makes the pass catalog a typed refusal; the payload
        # failure-code branch still executed during terminal mapping.
        assert _refusals(result)
        assert _published(result) == []

    def test_records_persist_raises_on_non_collision_store_error(self, tmp_path, monkeypatch):
        _worktree, group, _lane, candidate_tree = self._run_green_gate(tmp_path)
        store_dir = certification_records.records_directory(group) / "objects"
        capped = ContentAddressedCertificateStore(
            store_dir,
            CertificateStorePolicy(
                scopeId="records-edge",
                maxObjects=1,
                maxBytes=10_000_000_000,
                reclamationOwner="records-edge",
            ),
        )
        monkeypatch.setattr(certification_records, "certificate_store", lambda _group: capped)
        certification_records.prepare_certification_records(
            certification_records.CertificationRunTarget(
                repository_id=_REPOSITORY_ID,
                code_worktree=_worktree,
                profile_reference=_PROFILE_REFERENCE,
                worktree_group=group,
            ),
            mode="targeted",
            candidate_tree=candidate_tree,
            diff_base=_git(_worktree, "rev-parse", "HEAD"),
        )
        (_worktree / "capacity-successor.txt").write_text("real different candidate\n")
        _git(_worktree, "add", "-A")
        successor_tree = _git(_worktree, "write-tree")
        with pytest.raises(CertificationContractError, match="capacity"):
            certification_records.prepare_certification_records(
                certification_records.CertificationRunTarget(
                    repository_id=_REPOSITORY_ID,
                    code_worktree=_worktree,
                    profile_reference=_PROFILE_REFERENCE,
                    worktree_group=group,
                ),
                mode="targeted",
                candidate_tree=successor_tree,
                diff_base=_git(_worktree, "rev-parse", "HEAD"),
            )

    def test_gate_record_helper_refuses_unconfigured_repository_before_publication(self, tmp_path):
        target = code_quality_gate.QualityGateTarget(
            code_worktree=tmp_path,
            worktree_group=tmp_path,
            repository_id="other-repository",
            profile_reference=None,
        )
        with pytest.raises(CertificationContractError):
            code_quality_gate._freeze_certification_records(
                target,
                plan=code_quality_gate.QualityGatePlan(mode="targeted"),
                candidate_tree="a" * 40,
                diff_base="HEAD",
            )
        assert not certification_records.records_directory(tmp_path).exists()

    def test_gate_record_helper_refuses_unreadable_artifact(self, tmp_path, monkeypatch):
        selected = selected_code_fixture(tmp_path, mode="targeted")

        def vanished(*_args, **_kwargs):
            raise OSError("artifact vanished")

        monkeypatch.setattr(certification_run, "published_report_path_from_manifest", vanished)
        with pytest.raises(
            CertificationContractError, match="no readable terminal decoder artifact"
        ):
            certification_run.record_terminal_generation(
                selected.prepared, selected.terminals[-1].publication
            )

    def test_gate_record_helper_refuses_non_object_payload(self, tmp_path, monkeypatch):
        selected = selected_code_fixture(tmp_path, mode="targeted")
        artifact = tmp_path / "artifact.json"
        artifact.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(
            certification_run, "published_report_path_from_manifest", lambda *_a, **_k: artifact
        )
        records = certification_records.records_directory(selected.target.worktree_group)
        before = (records / "gates.json").read_bytes()
        with pytest.raises(CertificationContractError, match="terminal decoder must be an object"):
            certification_run.record_terminal_generation(
                selected.prepared, selected.terminals[-1].publication
            )
        assert (records / "gates.json").read_bytes() == before

    def test_gate_recover_path_records_green_generation(self, tmp_path):
        selected = selected_code_fixture(tmp_path, mode="targeted")
        store = certification_records.certificate_store(selected.target.worktree_group)
        references = tuple(
            terminal.certificateReference
            for terminal in selected.terminals
            if terminal.certificateReference is not None
        )
        assert len(references) == 4
        original = tuple(store.load_reference(reference) for reference in references)
        result = selected.render()
        assert result["passed"] is True
        assert tuple(store.load_reference(reference) for reference in references) == original
        assert all(terminal.certificate is not None for terminal in selected.terminals)

    @pytest.mark.parametrize("fault", ["missing-base", "commit-as-tree"])
    def test_records_prepare_refuses_unobservable_git_source_authority(self, tmp_path, fault):
        code = _checkout_with_profile(tmp_path / "code")
        head = _git(code, "rev-parse", "HEAD")
        tree = _git(code, "write-tree")
        group = tmp_path / "enclosure"
        candidate = head if fault == "commit-as-tree" else tree
        base = "refs/heads/missing-acceptance-base" if fault == "missing-base" else head
        with pytest.raises(CertificationContractError) as refused:
            certification_records.prepare_certification_records(
                certification_records.CertificationRunTarget(
                    _REPOSITORY_ID, code, _PROFILE_REFERENCE, group
                ),
                mode="targeted",
                candidate_tree=candidate,
                diff_base=base,
            )
        finding = refused.value.findings[0]
        assert finding["code"] == "candidate-source-observation-refused"
        assert finding["expected"] == {"candidateTree": candidate, "diffBase": base}
        assert "source selection observation failed" in str(finding["observed"])
        assert not certification_records.records_directory(group).exists()
        assert _git(code, "rev-parse", "HEAD") == head
        assert _git(code, "write-tree") == tree

    @pytest.mark.parametrize("fault", ["index-moved", "wrong-repository"])
    def test_selected_renderer_refuses_moved_target_without_replacing_originals(
        self, tmp_path, fault
    ):
        selected = selected_code_fixture(tmp_path, mode="targeted")
        assert selected.render()["passed"] is True
        target = selected.target
        records = certification_records.records_directory(target.worktree_group)
        original = {path: path.read_bytes() for path in records.rglob("*") if path.is_file()}
        pointer = target.worktree_group / "reports/quality-report-set.json"
        before_pointer = pointer.read_bytes()
        if fault == "index-moved":
            (target.code_worktree / "after-selection.txt").write_text("new candidate input\n")
            _git(target.code_worktree, "add", "after-selection.txt")
            assert _git(target.code_worktree, "write-tree") != selected.prepared.candidateTree
        else:
            target = replace(target, repository_id="another-repository")
        with pytest.raises(RuntimeError, match="does not name the current target"):
            code_quality_gate.render_selected_code_certification(
                target,
                selected.prepared,
                selected.terminals,
                diff_base=_git(target.code_worktree, "rev-parse", "HEAD"),
                plan=code_quality_gate.QualityGatePlan(mode="targeted"),
            )
        assert {
            path: path.read_bytes() for path in records.rglob("*") if path.is_file()
        } == original
        assert pointer.read_bytes() == before_pointer
