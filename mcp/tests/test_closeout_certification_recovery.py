"""Owner-derived R05 recovery decisions through real plan/result/certificate compilers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.certification.certificate_admission import admitted_gate_identity
from agents_remember.certification.certificate_authority import compile_gate_certificate
from agents_remember.certification.certificate_invalidation import (
    classify_certificate_invalidation,
    plan_certificate_reuse,
)
from agents_remember.certification.certificate_models import (
    CoherenceSubrecordIdentity,
    CreationProvenance,
    GateCertificate,
    GateCertificateIssuanceContext,
    GateFiveSemanticInputs,
)
from agents_remember.certification.certification_lane import compile_certification_lane
from agents_remember.certification.digests import content_digest
from agents_remember.certification.frozen_run.models import (
    freeze_certification_run,
)
from agents_remember.certification.lifecycle_models import (
    ExactCandidateObservation,
    LifecycleAdmissionManifest,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    GateResultAdmission,
    GateResultManifest,
    RailResult,
)
from agents_remember.certification.repository_profiles.canonical import (
    canonicalize_repository_profile,
    repository_profile_digest,
)
from agents_remember.certification.repository_profiles.models import RepositoryCertificationProfile
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
)
from agents_remember.certification.results import compile_gate_result_manifest
from agents_remember.errors import CertificationContractError
from agents_remember.memory_quality.gate_five_rails import gate_five_memory_rails
from agents_remember.models.certification.base import GateId, RailIdentity
from agents_remember.models.certification.corrective import (
    CorrectiveInputChange,
    RedCatalogDisposition,
)
from agents_remember.models.lifecycles.certification import (
    OperationCertificationState,
    SelectedRecoveryDecision,
)
from agents_remember.worktrees.integration.closeout.certification.recovery import (
    RecoveryInputSnapshot,
    build_prior_red_context,
    derive_certificate_input_changes,
)
from agents_remember.worktrees.integration.closeout.certification.selection import (
    require_selected_certification,
    select_certification_state,
    select_recorded_terminals,
)
from agents_remember.worktrees.modules.quality import certification_records
from agents_remember.worktrees.modules.quality.execution import retained_reports
from agents_remember.worktrees.modules.quality.execution.models import RetainedGateExecution
from agents_remember.worktrees.modules.quality.published_manifest import (
    parse_published_quality_manifest,
    published_manifest_payload,
    quality_generation_digest,
    quality_report_dependencies,
)
from certification_registry_test_support import (
    ObservationSpec,
    _result,
)
from repository_profile_test_support import fixture_profile
from test_code_certification_execution import selected_execution
from test_operation_certification_selection import _fixture as _operation_fixture
from test_operation_certification_selection import _publish as _publish_operation


def _snapshot(
    profile_value: RepositoryCertificationProfile | None = None,
    *,
    tree: str = "c" * 40,
    marker: str = "original",
) -> RecoveryInputSnapshot:
    profile = canonicalize_repository_profile(profile_value or fixture_profile())
    plan = compile_repository_profile_plan(
        profile,
        selection_id="closeout-targeted",
        candidate_identity=CandidateIdentity(kind="git-tree", value=tree),
    )
    lane = compile_certification_lane(
        profile,
        plan,
        provenance=CreationProvenance(
            createdAt="2026-09-06T00:00:00+00:00",
            producer="recovery-input-fixture",
            evidenceRef=f"fixture://{marker}",
        ),
        memory_rails=gate_five_memory_rails(plan.selectionId),
    )
    run = freeze_certification_run(profile, lane)
    observation = ExactCandidateObservation(
        taskId="fixture-recovery-leaf",
        contractPath="/isolated/fixture/contract.md",
        lifecycleAuthorityDigest="a" * 64,
        repositoryId=profile.profile.repositoryId,
        sourceBranchRef="refs/heads/fixture-master",
        sourceBranchTip="b" * 40,
        candidateCodeTree=plan.candidateIdentity,
        topologyIdentityDigest="c" * 64,
        taskIntentIdentityDigest="d" * 64,
        normalizedCommitIntentDigest="e" * 64,
        mutationAuthorityDigest="f" * 64,
        sourceAuthorityDigest="1" * 64,
        worktreeRuleDigest="2" * 64,
        generatedInputsDigest="3" * 64,
        mutationAuthorityStatus="valid",
        sourceAuthorityStatus="valid",
        branchAuthorityStatus="valid",
        worktreeStatus="admissible",
        generatedArtifactStatus="unknown",
    )
    return RecoveryInputSnapshot(run, observation)


def _changed_profile(gate: GateId, *, runtime: bool = False) -> RepositoryCertificationProfile:
    profile = fixture_profile()
    rails = tuple(
        rail.model_copy(
            update={
                "runtimeInputs": rail.runtimeInputs.model_copy(
                    update={"runtimeIdentity": "changed-runtime"}
                )
            }
        )
        if rail.gate == gate and runtime
        else rail.model_copy(
            update={
                "execution": rail.execution.model_copy(
                    update={"timeoutSeconds": rail.execution.timeoutSeconds + 1}
                )
            }
        )
        if rail.gate == gate
        else rail
        for rail in profile.rails
    )
    changed = profile.model_copy(update={"rails": rails, "profileDigest": "0" * 64})
    return changed.model_copy(update={"profileDigest": repository_profile_digest(changed)})


def _memory(marker: str = "e") -> GateFiveSemanticInputs:
    return GateFiveSemanticInputs(
        memoryTree=CandidateIdentity(kind="git-tree", value=marker * 40),
        affectedClosurePlanDigest="a" * 64,
        memoryCheckerRegistryDigest="b" * 64,
        coherenceSubrecords=(
            CoherenceSubrecordIdentity(subrecordId="coherence", contentDigest="c" * 64),
        ),
        candidatePairAuthorityDigest="d" * 64,
    )


def _catalog(
    snapshot: RecoveryInputSnapshot,
    gate_id: GateId,
    *,
    red: bool = False,
) -> GateResultManifest:
    run = snapshot.run
    gate = run.certificationPlan.gates[gate_id - 1]
    results = tuple(
        _result(
            gate,
            ObservationSpec(
                rail.identity.railId,
                status="blocked"
                if red and rail.identity.railId == "dependant"
                else "fail"
                if red
                else "pass",
                blocked_by=("static-quality",)
                if red and rail.identity.railId == "dependant"
                else (),
                include_artifacts=not red,
            ),
        )
        for rail in gate.rails
    )
    return compile_gate_result_manifest(
        run.registry,
        run.certificationPlan,
        gate,
        results,
        GateResultAdmission(
            candidateIdentity=run.certificationPlan.candidateIdentity,
            profileId=run.certificationPlan.profileId,
            altitude="certifying",
        ),
    )


def _chain(snapshot: RecoveryInputSnapshot, through: GateId = 5) -> tuple[GateCertificate, ...]:
    certificates: list[GateCertificate] = []
    for gate in snapshot.run.certificationPlan.gates[:through]:
        certificates.append(
            compile_gate_certificate(
                snapshot.run.admission,
                gate,
                _catalog(snapshot, gate.gate),
                certificates,
                GateCertificateIssuanceContext(
                    provenance=snapshot.run.provenance,
                    gateFiveInputs=snapshot.memory_inputs if gate.gate == 5 else None,
                ),
            )
        )
    return tuple(certificates)


@pytest.mark.parametrize("gate", [1, 2, 3, 4])
def test_actual_gate_input_delta_selects_only_its_required_suffix(gate: GateId) -> None:
    before = replace(_snapshot(), memory_inputs=_memory())
    after = replace(_snapshot(_changed_profile(gate)), memory_inputs=_memory())
    certificates = _chain(before)
    changes = derive_certificate_input_changes(before, after)
    reuse = plan_certificate_reuse(
        after.run.admission, certificates, changes, gate_five_inputs=after.memory_inputs
    )
    assert classify_certificate_invalidation(changes).invalidatedGates == tuple(range(gate, 6))
    assert reuse.firstGateToRun == gate
    assert reuse.reusedCertificates == tuple(item.identity for item in certificates[: gate - 1])
    assert (
        before.run.admission.semanticEnvelope.candidateCodeTree
        == after.run.admission.semanticEnvelope.candidateCodeTree
    )


def test_code_change_invalidates_all_gates_and_does_not_reuse_old_certificates() -> None:
    before = replace(_snapshot(), memory_inputs=_memory())
    after = replace(_snapshot(tree="d" * 40), memory_inputs=_memory())
    changes = derive_certificate_input_changes(before, after)
    reuse = plan_certificate_reuse(
        after.run.admission, _chain(before), changes, gate_five_inputs=after.memory_inputs
    )
    assert any(item.changeClass == "code" for item in changes)
    assert reuse.firstGateToRun == 1 and not reuse.reusedCertificates


def test_unchanged_interruption_reuses_the_original_exact_certificates() -> None:
    snapshot = replace(_snapshot(), memory_inputs=_memory())
    certificates = _chain(snapshot)
    changes = derive_certificate_input_changes(snapshot, snapshot)
    assert tuple(item.changeClass for item in changes) == ("unchanged-interruption",)
    reuse = plan_certificate_reuse(
        snapshot.run.admission, certificates, changes, gate_five_inputs=snapshot.memory_inputs
    )
    assert reuse.reusedCertificates == tuple(item.identity for item in certificates)
    assert reuse.firstGateToRun is None and reuse.zeroGateStarts


def _red_profile() -> RepositoryCertificationProfile:
    profile = fixture_profile()
    root = profile.rails[0]
    additions = tuple(
        root.model_copy(
            update={
                "identity": RailIdentity(railId=name, version="1.0.0"),
                "orderKey": name,
                "prerequisites": (root.identity,) if name == "dependant" else (),
                "execution": root.execution.model_copy(update={"adapterId": f"{name}-adapter"}),
            }
        )
        for name in ("independent", "dependant")
    )
    selections = tuple(
        selection.model_copy(
            update={
                "gates": tuple(
                    gate.model_copy(
                        update={"railIds": (*gate.railIds, *(item.identity for item in additions))}
                    )
                    if gate.gate == 1
                    else gate
                    for gate in selection.gates
                )
            }
        )
        for selection in profile.selections
    )
    changed = profile.model_copy(
        update={
            "rails": (*profile.rails, *additions),
            "selections": selections,
            "profileDigest": "0" * 64,
        }
    )
    return changed.model_copy(update={"profileDigest": repository_profile_digest(changed)})


def _dispositions(
    prior: RecoveryInputSnapshot, current: RecoveryInputSnapshot, catalog: GateResultManifest
) -> tuple[RedCatalogDisposition, ...]:
    """Explicit fixture judgments; production receives these from the corrective owner."""
    root = next(result for result in catalog.railResults if result.rail.railId == "static-quality")
    change = CorrectiveInputChange(
        inputKind="candidate-code-tree",
        inputId="candidate",
        beforeDigest=prior.candidate.candidateCodeTree.value,
        afterDigest=current.candidate.candidateCodeTree.value,
    )
    return tuple(
        RedCatalogDisposition(
            rail=result.rail,
            priorStatus="blocked" if result.status == "blocked" else "fail",
            priorResultDigest=result.resultDigest,
            correctiveOwner=result.correctiveOwner,
            disposition="repaired-root" if result.status == "blocked" else "direct-repair",
            repairedRoot=root.rail if result.status == "blocked" else None,
            changedInputs=() if result.status == "blocked" else (change,),
            rationale="Fixture corrective owner verified this exact code repair or direct blocked root.",
        )
        for result in catalog.railResults
        if result.status in {"fail", "blocked"}
    )


def _reordered_catalog(catalog: GateResultManifest) -> GateResultManifest:
    """Untrusted canonical JSON with valid hashes still needs the plan's rail ordering."""
    assert len(catalog.railResults) > 1
    payload = catalog.model_dump(mode="json", exclude={"manifestDigest"})
    payload["railResults"].reverse()
    reordered = GateResultManifest.model_validate(
        {**payload, "manifestDigest": content_digest(payload)}
    )
    assert reordered.railResults == tuple(reversed(catalog.railResults))
    assert reordered.manifestDigest != catalog.manifestDigest
    return reordered


@pytest.mark.integration
@pytest.mark.parametrize(
    "fault,code",
    [
        ("lifecycle-profile", "selected-admission-recompile-mismatch"),
        ("foreign-recovery", "selected-recovery-mismatch"),
        ("terminal-order", "selected-gate-result-mismatch"),
    ],
)
def test_selection_recompiles_untrusted_proposals_before_selecting_original_references(
    tmp_path: Path, fault: str, code: str
) -> None:
    fixture = _operation_fixture(tmp_path / "current")
    extra_originals: dict[Path, bytes] = {}
    if fault == "terminal-order":
        recorded = _publish_operation(fixture, tmp_path / "export", outcome="red")
        fixture = replace(
            fixture,
            record=select_recorded_terminals(
                fixture.contract, fixture.store, fixture.record, recorded
            ),
        )
        pointer = fixture.contract.worktree_group / "reports/quality-report-set.json"
        extra_originals[pointer] = pointer.read_bytes()
    original = fixture.record.certification
    assert original is not None
    objects = fixture.frozen.prepared.certificate_store()
    references = (
        original.frozenRun,
        original.candidateAuthorities,
        original.lifecycleAdmission,
        *(decision.reference for decision in original.recoveryDecisions),
        *(terminal.result for terminal in original.terminals),
    )
    originals = {
        objects.exact_path(ref.kind, ref.semanticDigest): objects.exact_path(
            ref.kind, ref.semanticDigest
        ).read_bytes()
        for ref in references
    }
    before = fixture.store.path.read_bytes()
    payload = original.model_dump(mode="json")
    if fault == "lifecycle-profile":
        lifecycle = fixture.frozen.admission.lifecycle
        envelope = {**lifecycle.semanticEnvelope.model_dump(mode="json"), "profileId": "foreign"}
        untrusted = LifecycleAdmissionManifest.model_validate(
            {
                **lifecycle.model_dump(mode="json"),
                "semanticEnvelope": envelope,
                "admissionDigest": content_digest(envelope),
            }
        )
        # Publishing a parsed object does not authorize its selection as this run's admission.
        objects.publish(untrusted)
        payload["lifecycleAdmission"] = objects.reference(
            "lifecycle-admission", untrusted.admissionDigest
        ).model_dump(mode="json")
    elif fault == "foreign-recovery":
        other = _operation_fixture(tmp_path / "other")
        extra_originals[other.store.path] = other.store.path.read_bytes()
        recovery = other.frozen.recovery
        assert recovery.semanticEnvelope.lifecycleAdmissionDigest != (
            fixture.frozen.admission.lifecycle.admissionDigest
        )
        objects.publish(recovery)
        payload["recoveryDecisions"] = [
            SelectedRecoveryDecision(
                reference=objects.reference("recovery", recovery.recoveryDigest)
            ).model_dump(mode="json")
        ]
    else:
        terminal = original.terminals[0]
        assert terminal.certificate is None
        result = objects.load(GateResultManifest, terminal.result.semanticDigest)
        assert result.disposition == "red"
        reordered = _reordered_catalog(result)
        objects.publish(reordered)
        payload["terminals"][0]["result"] = objects.reference(
            "result-manifest", reordered.manifestDigest
        ).model_dump(mode="json")
    proposal = OperationCertificationState.model_validate(payload)
    with pytest.raises(CertificationContractError) as caught:
        select_certification_state(fixture.contract, fixture.store, fixture.record, proposal)
    assert caught.value.findings[0]["code"] == code
    assert fixture.store.path.read_bytes() == before
    assert fixture.store.read() == fixture.record
    assert all(path.read_bytes() == raw for path, raw in {**originals, **extra_originals}.items())
    assert require_selected_certification(fixture.contract, fixture.record).state == original


def test_prior_red_requires_every_independent_root_and_blocked_dependant() -> None:
    prior, current = _snapshot(_red_profile()), _snapshot(_red_profile(), tree="d" * 40)
    catalog = _catalog(prior, 1, red=True)
    dispositions = _dispositions(prior, current, catalog)
    context = build_prior_red_context(prior.run, current, catalog, dispositions, ())
    assert context.priorAdmission is prior.run.admission and context.priorCatalog is catalog
    assert context.dispositions == dispositions
    for omitted in dispositions:
        with pytest.raises(CertificationContractError) as caught:
            build_prior_red_context(
                prior.run,
                current,
                catalog,
                tuple(item for item in dispositions if item != omitted),
                (),
            )
        assert caught.value.findings[0]["code"] == "prior-red-disposition-catalog-incomplete"
        assert caught.value.findings[0]["gateStarts"] == 0


def test_prior_red_gate_two_requires_the_exact_original_gate_one_certificate() -> None:
    prior, current = _snapshot(), _snapshot(_changed_profile(2))
    catalog = _catalog(prior, 2, red=True)
    result = catalog.railResults[0]
    old = admitted_gate_identity(prior.run.admission, 2)
    new = {
        item.key: item for item in admitted_gate_identity(current.run.admission, 2).semanticInputs
    }
    changed = next(
        item for item in old.semanticInputs if item.contentDigest != new[item.key].contentDigest
    )
    dispositions = (
        RedCatalogDisposition(
            rail=result.rail,
            priorStatus="fail",
            priorResultDigest=result.resultDigest,
            correctiveOwner=result.correctiveOwner,
            disposition="direct-repair",
            changedInputs=(
                CorrectiveInputChange(
                    inputKind=changed.inputKind,
                    inputId=changed.inputId,
                    beforeDigest=changed.contentDigest,
                    afterDigest=new[changed.key].contentDigest,
                ),
            ),
            rationale="Fixture owner corrected the exact declared Gate-2 execution input.",
        ),
    )
    prefix = _chain(prior, through=1)
    assert (
        build_prior_red_context(prior.run, current, catalog, dispositions, prefix).priorCatalog
        == catalog
    )
    for wrong_prefix in ((), _chain(_snapshot(tree="d" * 40), through=1)):
        with pytest.raises(CertificationContractError):
            build_prior_red_context(prior.run, current, catalog, dispositions, wrong_prefix)


def _inventory_snapshot(publication, files):
    """Build digest-valid untrusted metadata using the canonical manifest owner."""
    payload = published_manifest_payload(publication)
    payload["files"] = files
    identity = {
        key: payload[key]
        for key in (
            "profileDigest",
            "profilePlanDigest",
            "profileSelectionId",
            "executorAdapterId",
            "resultDecoder",
        )
    }
    dependencies = quality_report_dependencies(
        publication.candidate_tree,
        files,
        publication.attestation,
        identity,
        publication.runtime_authority_digest,
    ).model_dump(mode="json")
    payload["dependencies"] = dependencies
    payload["generation"] = quality_generation_digest(
        {
            "candidateTree": publication.candidate_tree,
            **identity,
            "files": files,
            "dependencies": dependencies,
            "runtimeAuthorityDigest": publication.runtime_authority_digest,
        }
    )
    return parse_published_quality_manifest(payload)


def _retained_inventory_case(tmp_path: Path, fault: str):
    selected, request, _profile = selected_execution(tmp_path)
    run = selected.run
    store = certification_records.certificate_store(request.worktree_group)
    results = [
        store.load(GateResultManifest, item.semanticEnvelope.resultManifestDigest)
        for item in selected.certificates
    ]
    publications = [item.publication for item in selected.retained]
    changes: dict[int, tuple[str, str, str, int]] = {}
    if fault in {"duplicate", "conflict"}:
        path = "clean-quality-results.json"
        original = publications[0].require_file(path)
        changes[0] = ("evidence", path, original.sha256, original.size)
        changes[1] = (
            "evidence",
            path,
            "f" * 64 if fault == "conflict" else original.sha256,
            original.size,
        )
    elif fault in {"undeclared", "wrong-producer"}:
        path = "undeclared-producer.log" if fault == "undeclared" else "coverage.json"
        original = results[0].railResults[0].evidence[0]
        changes[0] = ("evidence", path, original.sha256, original.size)
    elif fault == "size":
        path = "coverage.json"
        declared = next(
            item for item in run.repositoryProfile.profile.publishedArtifacts if item.path == path
        )
        original = publications[1].require_file(path)
        changes[1] = ("artifacts", path, original.sha256, declared.maxBytes + 1)
    elif fault == "inventory":
        path = results[0].railResults[0].evidence[0].reference
        files = published_manifest_payload(publications[0])["files"]
        assert isinstance(files, dict)
        files[path] = {**files[path], "sha256": "f" * 64}
        publications[0] = _inventory_snapshot(publications[0], files)
    else:
        raise AssertionError(f"unknown inventory fault: {fault}")
    for gate_index, (field, path, digest, size) in changes.items():
        rails = list(results[gate_index].railResults)
        rail_index = next(
            index
            for index, rail in enumerate(rails)
            if field == "evidence" or any(item.evidenceRef == path for item in rail.artifacts)
        )
        payload = rails[rail_index].model_dump(mode="json")
        member = (
            payload[field][0]
            if field == "evidence"
            else next(item for item in payload[field] if item["evidenceRef"] == path)
        )
        member.update(
            {
                "reference" if field == "evidence" else "evidenceRef": path,
                "sha256": digest,
                "size": size,
            }
        )
        payload["resultDigest"] = content_digest(
            {key: value for key, value in payload.items() if key != "resultDigest"}
        )
        rails[rail_index] = RailResult.model_validate(payload)
        results[gate_index] = compile_gate_result_manifest(
            run.registry,
            run.certificationPlan,
            run.certificationPlan.gates[gate_index],
            rails,
            GateResultAdmission(
                profileId=run.certificationPlan.profileId,
                candidateIdentity=run.certificationPlan.candidateIdentity,
                altitude=run.certificationPlan.profileKind,
            ),
        )
        files = published_manifest_payload(publications[gate_index])["files"]
        assert isinstance(files, dict)
        files[path] = {"sha256": digest, "size": size}
        publications[gate_index] = _inventory_snapshot(publications[gate_index], files)
    certificates: list[GateCertificate] = []
    for gate_plan, result in zip(run.certificationPlan.gates, results, strict=False):
        certificate = compile_gate_certificate(
            run.admission,
            gate_plan,
            result,
            certificates,
            GateCertificateIssuanceContext(provenance=run.provenance),
        )
        store.publish(result)
        store.publish(certificate)
        certificates.append(certificate)
    repaired = replace(
        selected,
        certificates=tuple(certificates),
        reuse_plan=plan_certificate_reuse(run.admission, certificates, selected.input_changes),
        retained=tuple(
            RetainedGateExecution(certificates[index], results[index], publication)
            for index, publication in enumerate(publications)
        ),
    )
    # Every deliberate input reaches the actual transport guard as a schema-valid,
    # compiler-issued prefix; no selection validator or filesystem resolver is replaced.
    repaired.validate()
    return repaired, request


@pytest.mark.parametrize(
    "fault,detail",
    [
        ("undeclared", "exact frozen producer publication"),
        ("wrong-producer", "exact frozen producer publication"),
        ("size", "frozen publication byte bound"),
        ("inventory", "original publication inventory"),
        ("conflict", "conflicting producer paths"),
    ],
)
def test_retained_report_inventory_refuses_mismatched_evidence_before_any_source_read(
    tmp_path: Path, fault: str, detail: str
) -> None:
    selected, request = _retained_inventory_case(tmp_path, fault)
    destination = tmp_path / "rejected-retained"
    with (
        mock.patch.object(
            retained_reports,
            "published_report_path_from_manifest",
            wraps=retained_reports.published_report_path_from_manifest,
        ) as open_report,
        pytest.raises(ValueError, match=detail),
    ):
        retained_reports.snapshot_retained_reports(
            selected,
            reports=request.worktree_group / "reports",
            destination=destination,
        )
    open_report.assert_not_called()
    assert not destination.exists()
