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
from types import SimpleNamespace
from typing import cast
from unittest import mock

import pytest
from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.certification.certificate_models import CreationProvenance
from agents_remember.certification.certificate_store import (
    CertificateStorePolicy,
    ContentAddressedCertificateStore,
)
from agents_remember.certification.certification_lane import compile_certification_lane
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.authority import (
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.execution import (
    admit_repository_profile_execution,
)
from agents_remember.certification.repository_profiles.models import ProfileMode
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
    resolve_repository_profile_selection,
)
from agents_remember.errors import CertificationContractError
from agents_remember.memory_quality.gate_five_rails import gate_five_memory_rails
from agents_remember.models.test_evidence import _certifying_evidence_from_verified_dagger
from agents_remember.worktrees.modules.quality import certification_records, clean_executor
from agents_remember.worktrees.modules.quality import gate as code_quality_gate
from agents_remember.worktrees.modules.quality.clean_executor import (
    CleanQualityOutcome,
    ReportBindings,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
)


def _published(result):
    value = result.get("published")
    return cast(list[dict[str, object]], value)


def _refusals(result):
    value = result.get("refused")
    return cast(list[dict[str, object]], value)


_REPOSITORY_ID = "agents-remember"
_PROFILE_REFERENCE = Path("mcp/certification-profile-v1.json")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _checkout_with_profile(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "records@test.invalid")
    _git(root, "config", "user.name", "records test")
    target = root / "mcp"
    target.mkdir(parents=True, exist_ok=True)
    profile = Path(__file__).resolve().parents[1] / "certification-profile-v1.json"
    (target / "certification-profile-v1.json").write_bytes(profile.read_bytes())
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _lane_for(root: Path, mode: ProfileMode = "targeted"):
    admitted = load_repository_profile(_REPOSITORY_ID, root, _PROFILE_REFERENCE.as_posix())
    candidate_tree = _git(root, "write-tree")
    candidate = CandidateIdentity(kind="git-tree", value=candidate_tree)
    selection = resolve_repository_profile_selection(
        admitted.canonical, purpose="closeout", mode=mode
    )
    repository_plan = compile_repository_profile_plan(
        admitted.canonical, selection_id=selection.selectionId, candidate_identity=candidate
    )
    lane = compile_certification_lane(
        admitted.canonical,
        repository_plan,
        provenance=certification_records._provenance("test"),  # type: ignore[attr-defined]
        memory_rails=gate_five_memory_rails(selection.selectionId),
    )
    return admitted, lane, candidate_tree


def _gate_catalog(lane) -> list[dict[str, object]]:
    """Fixture run payload: every planned applicable rail passes and binds the
    per-rail evidence/artifact records the R11 manifest contract requires."""
    catalog: list[dict[str, object]] = []
    for gate_plan in lane.certificationPlan.gates:
        if gate_plan.gate == 5:
            continue
        rails = []
        for rail in gate_plan.rails:
            evidence = [
                {
                    "evidenceId": item.evidenceId,
                    "sha256": content_digest(
                        {"evidence": rail.identity.key, "id": item.evidenceId}
                    ),
                    "size": 64,
                    "reference": (
                        "quality-result://clean-quality-results.json"
                        f"#gates.{gate_plan.gate}.{rail.identity.key}"
                    ),
                }
                for item in rail.evidenceContract
            ]
            artifacts = [
                {
                    "artifactId": item.artifactId,
                    "sha256": content_digest(
                        {"artifact": rail.identity.key, "id": item.artifactId}
                    ),
                    "size": 64,
                    "evidenceRef": (
                        "quality-result://clean-quality-results.json"
                        f"#gates.{gate_plan.gate}.{rail.identity.key}"
                    ),
                }
                for item in rail.outputArtifacts
            ]
            rails.append(
                {
                    "identity": {
                        "railId": rail.identity.railId,
                        "version": rail.identity.version,
                    },
                    "key": rail.identity.key,
                    "gate": gate_plan.gate,
                    "posture": rail.posture,
                    "status": "pass",
                    "exitCode": 0,
                    "evidence": evidence,
                    "artifacts": artifacts,
                }
            )
        catalog.append(
            {
                "gate": gate_plan.gate,
                "applicability": "applicable",
                "started": True,
                "disposition": "green",
                "zeroStart": False,
                "laterGatesZeroStart": False,
                "rails": rails,
                "selectors": [],
            }
        )
    return catalog


def _green_outcome_factory(worktree_group: Path, lane, candidate_tree: str):
    def _outcome(request):
        with tempfile.TemporaryDirectory() as temporary:
            exported = Path(temporary)
            payload = {
                "status": "passed",
                "exitCode": 0,
                "gates": _gate_catalog(lane),
            }
            (exported / "clean-quality-results.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            admitted = load_repository_profile(
                request.repository_id, request.code_worktree, _PROFILE_REFERENCE.as_posix()
            )
            profile_execution = admit_repository_profile_execution(
                admitted,
                purpose="closeout",
                mode=request.mode,
                candidate_identity=CandidateIdentity(kind="git-tree", value=candidate_tree),
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

    return _outcome


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
                diff_base="deadbeef",
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
        admission = store.load_admission(lane.admission.admissionDigest)
        assert admission.semanticEnvelope == lane.admission.semanticEnvelope
        gates = json.loads(
            (certification_records.records_directory(group) / "gates.json").read_text()
        )
        published = [item for item in gates["gates"] if item["kind"] == "certificate"]
        refused = [item for item in gates["gates"] if item["kind"] == "refused"]
        assert len(published) == 4
        assert refused == []
        assert [item["gate"] for item in published] == [1, 2, 3, 4]
        certificates = [store.load_certificate(item["certificate"]) for item in published]
        validate_certificate_chain(admission, certificates)

    def test_gate_seam_is_idempotent_across_an_unchanged_rerun(self, tmp_path):
        _worktree, group, _lane, _cand = self._run_green_gate(tmp_path)
        journal_one = certification_records.load_execution_records(group)
        assert journal_one is not None
        store = certification_records.certificate_store(group)
        first_certs = [
            store.load_certificate(item["certificate"])
            for item in json.loads(
                (certification_records.records_directory(group) / "gates.json").read_text()
            )["gates"]
            if item["kind"] == "certificate"
        ]
        # An unchanged rerun freezes the identical admission and reuses the
        # exact content-addressed certificates (zero new gate starts).
        _w2, group_two, _l2, _c2 = self._run_green_gate(tmp_path, mode="targeted", name="rerun")
        journal_two = certification_records.load_execution_records(group_two)
        assert journal_two is not None
        assert journal_two["admissionDigest"] == journal_one["admissionDigest"]
        assert certification_records.records_directory(group_two).exists()
        assert first_certs  # content-addressed objects are byte-identical

    def test_gate_seam_without_gate_catalog_records_admission_only(self, tmp_path):
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

        with mock.patch.object(code_quality_gate, "run_clean_quality", side_effect=minimal):
            result = code_quality_gate.run_strict_code_quality_gate(
                target,
                diff_base="deadbeef",
                plan=code_quality_gate.QualityGatePlan(mode="targeted"),
            )
        assert result["passed"] is True
        journal = certification_records.load_execution_records(group)
        assert journal is not None and journal["admissionDigest"] == lane.admission.admissionDigest
        gates = json.loads(
            (certification_records.records_directory(group) / "gates.json").read_text()
        )
        assert gates["gates"] == []

    def _prepared_run(self, group, lane, candidate_tree):
        return certification_records.PreparedCertificationRun(
            worktree_group=group,
            candidateTree=candidate_tree,
            lane=lane,
            provenance=CreationProvenance(
                createdAt="2026-09-05T00:00:00+00:00",
                producer="records-edge-test",
                evidenceRef="evidence://records-edge-test",
            ),
        )

    def test_records_prepare_skips_unregistered_repositories(self, tmp_path):
        prepared = certification_records.prepare_certification_records(
            certification_records.CertificationRunTarget(
                repository_id="other-repository",
                code_worktree=tmp_path,
                profile_reference=None,
                worktree_group=tmp_path,
            ),
            mode="targeted",
            candidate_tree="a" * 40,
        )
        assert prepared is None

    def test_records_candidate_tree_mismatch_is_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        moved = replace(manifest, candidate_tree="b" * 40)
        prepared = self._prepared_run(group, lane, candidate_tree)
        with pytest.raises(RuntimeError, match="certifies another candidate tree"):
            certification_records.record_published_generation(prepared, moved, {})

    def test_records_malformed_catalog_entries_are_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        with pytest.raises(RuntimeError, match="non-object entry"):
            certification_records.record_published_generation(prepared, manifest, {"gates": [42]})
        with pytest.raises(RuntimeError, match="lacks an exact gate"):
            certification_records.record_published_generation(
                prepared, manifest, {"gates": [{"gate": True}]}
            )

    def test_records_non_green_dispositions_are_terminal_and_junk_rails_are_skipped(self, tmp_path):
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
        result = certification_records.record_published_generation(prepared, manifest, payload)
        assert _published(result) == []
        gates = json.loads(
            (certification_records.records_directory(group) / "gates.json").read_text()
        )
        assert [item["gate"] for item in gates["gates"]] == [1, 2, 3]
        # The junk rails list is skipped (no dict outcomes), so the green gate 3
        # catalog has no terminal evidence and is journaled as a typed refusal.
        assert [item["kind"] for item in gates["gates"]] == [
            "terminal",
            "terminal",
            "refused",
        ]
        assert gates["gates"][0]["disposition"] == "not-run"
        assert gates["gates"][1]["disposition"] == "not-applicable"
        assert gates["gates"][2]["refusalCode"] == "missing-run-evidence"

    def test_records_unplanned_gate_is_refused(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        result = certification_records.record_published_generation(
            prepared, manifest, {"gates": [{"gate": 6, "disposition": "green", "rails": []}]}
        )
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
        )
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
        )
        assert len(_refusals(result)) == 1
        assert _refusals(result)[0]["refusalCode"] == "undeclared-result-artifact"

    def test_records_contradictory_red_catalog_publishes_terminal_manifest(self, tmp_path):
        _worktree, group, lane, candidate_tree = self._run_green_gate(tmp_path)
        manifest = load_published_quality_manifest(group / "reports")
        prepared = self._prepared_run(group, lane, candidate_tree)
        gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == 1)
        failing_key = gate_plan.waves[-1][0].key
        rails = []
        for rail in gate_plan.rails:
            rails.append(
                {
                    "key": rail.identity.key,
                    "status": "fail" if rail.identity.key == failing_key else "pass",
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
                    "artifacts": [
                        {
                            "artifactId": item.artifactId,
                            "sha256": content_digest(
                                {"artifact": rail.identity.key, "id": item.artifactId}
                            ),
                            "size": 64,
                            "evidenceRef": "quality-result://edge",
                        }
                        for item in rail.outputArtifacts
                    ],
                }
            )
        result = certification_records.record_published_generation(
            prepared,
            manifest,
            {"gates": [{"gate": 1, "disposition": "green", "rails": rails}]},
        )
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
        with pytest.raises(RuntimeError, match="bound certification-memory-rails port"):
            certification_records.prepare_certification_records(
                certification_records.CertificationRunTarget(
                    repository_id=_REPOSITORY_ID,
                    code_worktree=_worktree,
                    profile_reference=_PROFILE_REFERENCE,
                    worktree_group=group,
                ),
                mode="targeted",
                candidate_tree=candidate_tree,
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
        )
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
        )
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
        )
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
        )
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
        )
        with pytest.raises(CertificationContractError, match="capacity"):
            certification_records.prepare_certification_records(
                certification_records.CertificationRunTarget(
                    repository_id=_REPOSITORY_ID,
                    code_worktree=_worktree,
                    profile_reference=_PROFILE_REFERENCE,
                    worktree_group=group,
                ),
                mode="targeted",
                candidate_tree="d" * 40,
            )

    def test_gate_record_helper_skips_unregistered_repositories(self, tmp_path):
        target = code_quality_gate.QualityGateTarget(
            code_worktree=tmp_path,
            worktree_group=tmp_path,
            repository_id="other-repository",
            profile_reference=None,
        )
        code_quality_gate._record_certification_generation(
            target,
            plan=code_quality_gate.QualityGatePlan(mode="targeted"),
            candidate_tree="a" * 40,
            manifest=None,
        )

    def test_gate_record_helper_refuses_unreadable_artifact(self, tmp_path, monkeypatch):
        worktree = _checkout_with_profile(tmp_path / "ar" / "code")
        group = tmp_path / "ar" / "enclosure"
        target = code_quality_gate.QualityGateTarget(
            code_worktree=worktree,
            worktree_group=group,
            repository_id=_REPOSITORY_ID,
            profile_reference=_PROFILE_REFERENCE,
        )
        _admitted, _lane, candidate_tree = _lane_for(worktree)

        def _boom(*_args, **_kwargs):
            raise OSError("artifact vanished")

        monkeypatch.setattr(code_quality_gate, "published_report_path_from_manifest", _boom)
        manifest = SimpleNamespace(result_decoder=SimpleNamespace(artifactPath="x.json"))
        with pytest.raises(RuntimeError, match="no readable decoder artifact"):
            code_quality_gate._record_certification_generation(
                target,
                plan=code_quality_gate.QualityGatePlan(mode="targeted"),
                candidate_tree=candidate_tree,
                manifest=manifest,
            )

    def test_gate_record_helper_skips_non_object_payload(self, tmp_path, monkeypatch):
        worktree = _checkout_with_profile(tmp_path / "ar2" / "code")
        group = tmp_path / "ar2" / "enclosure"
        target = code_quality_gate.QualityGateTarget(
            code_worktree=worktree,
            worktree_group=group,
            repository_id=_REPOSITORY_ID,
            profile_reference=_PROFILE_REFERENCE,
        )
        _admitted, _lane, candidate_tree = _lane_for(worktree)
        artifact = tmp_path / "artifact.json"
        artifact.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(
            code_quality_gate,
            "published_report_path_from_manifest",
            lambda *_a, **_k: artifact,
        )
        manifest = SimpleNamespace(result_decoder=SimpleNamespace(artifactPath="x.json"))
        code_quality_gate._record_certification_generation(
            target,
            plan=code_quality_gate.QualityGatePlan(mode="targeted"),
            candidate_tree=candidate_tree,
            manifest=manifest,
        )
        journal = certification_records.load_execution_records(group)
        assert journal is not None

    def test_gate_recover_path_records_green_generation(self, tmp_path):
        worktree = _checkout_with_profile(tmp_path / "rec" / "code")
        group = tmp_path / "rec" / "enclosure"
        target = code_quality_gate.QualityGateTarget(
            code_worktree=worktree,
            worktree_group=group,
            repository_id=_REPOSITORY_ID,
            profile_reference=_PROFILE_REFERENCE,
        )
        _admitted, _lane, candidate_tree = _lane_for(worktree)

        def recovered_outcome(request):
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
                )
                clean_executor._publish_reports(  # type: ignore[attr-defined]
                    exported,
                    request.worktree_group / "reports",
                    candidate_tree=candidate_tree,
                    profile_execution=profile_execution,
                    bindings=ReportBindings(
                        attestation={"kind": "recover-test"},
                        runtime_authority_digest=None,
                    ),
                )
            manifest = load_published_quality_manifest(request.worktree_group / "reports")
            evidence = _certifying_evidence_from_verified_dagger(
                candidate_tree=candidate_tree,
                result_sha256=manifest.require_file(manifest.result_decoder.artifactPath).sha256,
            )
            return CleanQualityOutcome(
                subprocess.CompletedProcess(["dagger"], 0, stdout="passed"),
                evidence,
                manifest,
            )

        with mock.patch.object(
            code_quality_gate,
            "run_clean_quality",
            side_effect=recovered_outcome,
        ):
            code_quality_gate.run_strict_code_quality_gate(
                target,
                diff_base="deadbeef",
                plan=code_quality_gate.QualityGatePlan(mode="targeted"),
            )
        # Recover the exact published generation through the real recovery entry
        # point; its success tail records the green generation too.
        result = code_quality_gate.recover_strict_code_quality_gate(
            target,
            diff_base="deadbeef",
            plan=code_quality_gate.QualityGatePlan(mode="targeted"),
            attestation={"kind": "recover-test"},
        )
        assert result is not None
        assert result["passed"] is True
        journal = certification_records.load_execution_records(group)
        assert journal is not None
