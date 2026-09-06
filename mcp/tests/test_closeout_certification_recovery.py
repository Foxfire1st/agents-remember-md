"""Owner-derived R05 recovery decisions through real plan/result/certificate compilers."""

from __future__ import annotations

import hashlib
import json
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
    FrozenCertificationRun,
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
    recovery_memory_inputs,
    require_selected_certification,
    retain_memory_inputs,
    select_certification_state,
    select_recorded_terminals,
)
from agents_remember.worktrees.modules.quality import certification_evidence, certification_records
from agents_remember.worktrees.modules.quality.certification_terminal import terminal_results
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
    _canonical_registry,
    _gate,
    _plan,
    _result,
)
from pydantic import ValidationError
from repository_profile_test_support import fixture_profile
from test_code_certification_execution import selected_execution
from test_gate_certification_evidence import (
    _arrange,
    _damage_selected_snapshot,
    _foreign_publication,
    _publish,
)
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


@pytest.mark.parametrize("gate", [2, 4])
def test_runtime_change_uses_actual_declared_consuming_gate(gate: GateId) -> None:
    before, after = _snapshot(), _snapshot(_changed_profile(gate, runtime=True))
    changes = derive_certificate_input_changes(before, after)
    runtime = next(
        item for item in changes if item.changeClass == "runtime-toolchain-executor-image"
    )
    assert runtime.consumingGates == (gate,)
    assert classify_certificate_invalidation(changes).invalidatedGates == tuple(range(gate, 6))


def test_code_change_invalidates_all_gates_and_does_not_reuse_old_certificates() -> None:
    before = replace(_snapshot(), memory_inputs=_memory())
    after = replace(_snapshot(tree="d" * 40), memory_inputs=_memory())
    changes = derive_certificate_input_changes(before, after)
    reuse = plan_certificate_reuse(
        after.run.admission, _chain(before), changes, gate_five_inputs=after.memory_inputs
    )
    assert any(item.changeClass == "code" for item in changes)
    assert reuse.firstGateToRun == 1 and not reuse.reusedCertificates


@pytest.mark.parametrize("mode", ["memory", "coherence"])
def test_actual_memory_port_changes_retain_the_original_code_prefix(mode: str) -> None:
    before = replace(_snapshot(), memory_inputs=_memory())
    current_memory = (
        _memory("f")
        if mode == "memory"
        else _memory().model_copy(
            update={
                "coherenceSubrecords": (
                    CoherenceSubrecordIdentity(subrecordId="coherence", contentDigest="e" * 64),
                )
            }
        )
    )
    after = replace(before, memory_inputs=current_memory)
    certificates = _chain(before)
    changes = derive_certificate_input_changes(before, after)
    reuse = plan_certificate_reuse(
        after.run.admission, certificates, changes, gate_five_inputs=current_memory
    )
    assert reuse.firstGateToRun == 5
    assert reuse.reusedCertificates == tuple(item.identity for item in certificates[:4])
    if mode == "coherence":
        assert changes[0].affectedGateFiveSubrecords == ("coherence",)


@pytest.mark.parametrize(
    "field", ["topologyIdentityDigest", "taskIntentIdentityDigest", "normalizedCommitIntentDigest"]
)
def test_metadata_and_unconsumed_task_changes_only_revalidate_finalization(field: str) -> None:
    before = replace(_snapshot(), memory_inputs=_memory())
    after = replace(before, candidate=before.candidate.model_copy(update={field: "9" * 64}))
    changes = derive_certificate_input_changes(before, after)
    reuse = plan_certificate_reuse(
        after.run.admission, _chain(before), changes, gate_five_inputs=after.memory_inputs
    )
    assert reuse.firstGateToRun is None
    assert reuse.zeroGateStarts and reuse.finalizationRevalidationRequired
    assert classify_certificate_invalidation(changes).invalidatedGates == ()


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


def test_absent_memory_observation_cannot_authorize_gate_five_reuse() -> None:
    snapshot = _snapshot()
    with_memory = replace(snapshot, memory_inputs=_memory())
    changes = derive_certificate_input_changes(snapshot, snapshot)
    reuse = plan_certificate_reuse(snapshot.run.admission, _chain(with_memory), changes)
    assert reuse.firstGateToRun == 5
    with pytest.raises(CertificationContractError) as caught:
        derive_certificate_input_changes(with_memory, snapshot)
    assert caught.value.findings[0]["code"] == "recovery-memory-inputs-incomplete"


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


def test_prior_red_catalog_refuses_digest_valid_noncanonical_rail_order() -> None:
    prior, current = _snapshot(_red_profile()), _snapshot(_red_profile(), tree="d" * 40)
    catalog = _catalog(prior, 1, red=True)
    dispositions = _dispositions(prior, current, catalog)
    before = prior.run.model_dump_json(), current.run.model_dump_json(), catalog.model_dump_json()
    reordered = _reordered_catalog(catalog)
    with pytest.raises(CertificationContractError) as caught:
        build_prior_red_context(prior.run, current, reordered, dispositions, ())
    assert caught.value.findings[0]["code"] == "prior-red-catalog-mismatch"
    assert caught.value.findings[0]["gateStarts"] == 0
    assert (
        prior.run.model_dump_json(),
        current.run.model_dump_json(),
        catalog.model_dump_json(),
    ) == before
    assert build_prior_red_context(prior.run, current, catalog, dispositions, ()).priorCatalog == (
        catalog
    )


def test_recovery_snapshot_refuses_recreated_admission_provenance() -> None:
    snapshot = _snapshot()
    original = snapshot.run.model_dump_json()
    admission = type(snapshot.run.admission).model_validate(
        {
            **snapshot.run.admission.model_dump(mode="json"),
            "provenance": {
                **snapshot.run.admission.provenance.model_dump(mode="json"),
                "evidenceRef": "fixture://recreated-admission-provenance",
            },
        }
    )
    assert admission.admissionDigest == snapshot.run.admission.admissionDigest
    # model_copy is an untrusted input route, never an assertion of frozen-run validity.
    changed = snapshot.run.model_copy(update={"admission": admission})
    changed = changed.model_copy(
        update={"runDigest": content_digest(changed.model_dump(mode="json", exclude={"runDigest"}))}
    )
    with pytest.raises(ValueError, match="must retain its exact original admission"):
        FrozenCertificationRun.model_validate(changed.model_dump(mode="json"))
    with pytest.raises(CertificationContractError) as caught:
        derive_certificate_input_changes(snapshot, replace(snapshot, run=changed))
    assert caught.value.findings[0]["code"] == "recovery-frozen-admission-mismatch"
    assert caught.value.findings[0]["gateStarts"] == 0
    assert snapshot.run.model_dump_json() == original
    assert derive_certificate_input_changes(snapshot, snapshot)[0].changeClass == (
        "unchanged-interruption"
    )


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


@pytest.mark.parametrize("fault", ["malformed-json", "closed-shape", "noncanonical", "wrong-hash"])
def test_untrusted_retained_memory_inputs_refuse_without_replacing_original_selection(
    tmp_path: Path, fault: str
) -> None:
    fixture = _operation_fixture(tmp_path)
    original = fixture.record.certification
    assert original is not None and original.terminals == ()
    objects = fixture.frozen.prepared.certificate_store()
    references = (
        original.frozenRun,
        original.candidateAuthorities,
        original.lifecycleAdmission,
        *(decision.reference for decision in original.recoveryDecisions),
    )
    originals = {
        objects.exact_path(ref.kind, ref.semanticDigest): objects.exact_path(
            ref.kind, ref.semanticDigest
        ).read_bytes()
        for ref in references
    }
    before = fixture.store.path.read_bytes()
    # These compiler inputs are deliberately untrusted transport data, not a current
    # memory observation or a Gate-5 certificate. The original R21 object stays intact.
    inputs = _memory()
    retained = retain_memory_inputs(inputs).model_dump(mode="json")
    if fault == "malformed-json":
        encoded = "{"
    elif fault == "closed-shape":
        encoded = json.dumps({**inputs.model_dump(mode="json"), "untrustedExtra": True})
    elif fault == "noncanonical":
        encoded = json.dumps(inputs.model_dump(mode="json"), sort_keys=True, indent=2)
    else:
        encoded = retained["canonicalBytes"]
    retained["canonicalBytes"] = encoded
    retained["contentSha256"] = (
        "0" * 64 if fault == "wrong-hash" else hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    )
    payload = original.model_dump(mode="json")
    payload["recoveryDecisions"][0]["memoryInputs"] = retained
    assert payload["recoveryDecisions"][0]["reference"] == (
        original.recoveryDecisions[0].reference.model_dump(mode="json")
    )
    if fault == "wrong-hash":
        with pytest.raises(ValidationError, match="retained certification bytes"):
            OperationCertificationState.model_validate(payload)
    else:
        proposal = OperationCertificationState.model_validate(payload)
        with pytest.raises(CertificationContractError) as caught:
            select_certification_state(fixture.contract, fixture.store, fixture.record, proposal)
        assert caught.value.findings[0]["code"] == (
            "selected-memory-inputs-noncanonical"
            if fault == "noncanonical"
            else "selected-memory-inputs-invalid"
        )
    assert fixture.store.path.read_bytes() == before
    assert fixture.store.read() == fixture.record
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert require_selected_certification(fixture.contract, fixture.record).state == original


def test_retained_memory_codec_roundtrips_compiler_inputs_without_selecting_them(
    tmp_path: Path,
) -> None:
    fixture = _operation_fixture(tmp_path)
    selected = fixture.record.certification
    assert selected is not None
    original = selected.recoveryDecisions[0]
    before = fixture.store.path.read_bytes()
    # This proves the codec only; the compiler fixture is never selected as live memory.
    inputs = _memory()
    retained = retain_memory_inputs(inputs)
    decision = SelectedRecoveryDecision.model_validate(
        {**original.model_dump(mode="json"), "memoryInputs": retained.model_dump(mode="json")}
    )
    reopened = recovery_memory_inputs(decision)
    assert reopened == inputs
    assert reopened is not None and retain_memory_inputs(reopened) == retained
    assert decision.reference == original.reference
    assert recovery_memory_inputs(original) is None
    assert fixture.store.path.read_bytes() == before
    assert require_selected_certification(fixture.contract, fixture.record).state == selected


def test_recovery_wire_history_preserves_recurrent_observations_and_refuses_adjacent_duplicates(
    tmp_path: Path,
) -> None:
    fixture = _operation_fixture(tmp_path)
    original = fixture.record.certification
    assert original is not None
    reference = original.recoveryDecisions[0].reference
    objects = fixture.frozen.prepared.certificate_store()
    proof = objects.exact_path(reference.kind, reference.semanticDigest)
    proof_before = proof.read_bytes()
    journal_before = fixture.store.path.read_bytes()
    # These compiler-only observations exercise chronological wire shape. Neither
    # history is selected into the journal or asserted to certify current memory.
    first, second = (
        SelectedRecoveryDecision(reference=reference, memoryInputs=retain_memory_inputs(inputs))
        for inputs in (_memory("e"), _memory("f"))
    )
    assert first != second
    payload = original.model_dump(mode="json")
    payload["recoveryDecisions"] = [
        decision.model_dump(mode="json") for decision in (first, second, first)
    ]
    chronological = OperationCertificationState.model_validate(payload)
    assert chronological.recoveryDecisions == (first, second, first)
    assert tuple(decision.reference for decision in chronological.recoveryDecisions) == (
        reference,
        reference,
        reference,
    )
    assert chronological.terminals == original.terminals
    payload["recoveryDecisions"] = [first.model_dump(mode="json"), first.model_dump(mode="json")]
    with pytest.raises(
        ValidationError, match="adjacent recovery decisions cannot be selected twice"
    ):
        OperationCertificationState.model_validate(payload)
    assert proof.read_bytes() == proof_before
    assert fixture.store.path.read_bytes() == journal_before
    assert fixture.store.read() == fixture.record
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


@pytest.mark.parametrize(
    "damage",
    [
        "incomplete-catalog",
        "wrong-catalog",
        "wrong-prior-admission",
        "diagnostic",
        "missing",
        "empty-dispositions",
        "duplicate",
        "wrong-root",
        "unchanged",
    ],
)
def test_prior_red_recovery_refuses_unproven_or_incomplete_authority(damage: str) -> None:
    prior, current = _snapshot(_red_profile()), _snapshot(_red_profile(), tree="d" * 40)
    catalog = _catalog(prior, 1, red=True)
    dispositions = _dispositions(prior, current, catalog)
    if damage == "incomplete-catalog":
        payload = catalog.model_dump(mode="json", exclude={"manifestDigest"})
        payload["railResults"] = payload["railResults"][:1]
        catalog = GateResultManifest.model_validate(
            {**payload, "manifestDigest": content_digest(payload)}
        )
    elif damage == "wrong-catalog":
        catalog = _catalog(current, 1, red=True)
    elif damage == "diagnostic":
        payload = catalog.model_dump(mode="json", exclude={"manifestDigest"})
        payload.update(profileKind="diagnostic", altitude="diagnostic")
        catalog = GateResultManifest.model_validate(
            {**payload, "manifestDigest": content_digest(payload)}
        )
    elif damage == "empty-dispositions":
        dispositions = ()
    elif damage == "duplicate":
        dispositions = (*dispositions, dispositions[0])
    elif damage == "wrong-root":
        dispositions = tuple(
            item.model_copy(update={"repairedRoot": item.rail})
            if item.disposition == "repaired-root"
            else item
            for item in dispositions
        )
    elif damage == "unchanged":
        current = prior
    with pytest.raises(CertificationContractError) as caught:
        build_prior_red_context(
            current.run if damage == "wrong-prior-admission" else prior.run,
            current,
            None if damage == "missing" else catalog,
            dispositions,
            (),
        )
    assert all(item["gateStarts"] == 0 for item in caught.value.findings)


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


@pytest.mark.parametrize("field", ["contractPath", "repositoryId", "candidateCodeTree"])
def test_recovery_observations_must_bind_the_selected_run_and_task(field: str) -> None:
    snapshot = _snapshot()
    changed: object = "/other/contract.md" if field == "contractPath" else "other-repository"
    if field == "candidateCodeTree":
        changed = CandidateIdentity(kind="git-tree", value="d" * 40)
    current = replace(snapshot, candidate=snapshot.candidate.model_copy(update={field: changed}))
    with pytest.raises(CertificationContractError):
        derive_certificate_input_changes(snapshot, current)


def _physical_red_terminal(tmp_path: Path):
    """Publish a real complete red catalog and retain its exact typed store objects."""
    _code, group, prepared, execution = _arrange(tmp_path)
    failing_key = prepared.lane.certificationPlan.gates[0].waves[-1][0].key

    def fail_last_wave(payload):
        payload.update(status="failed", exitCode=1)
        payload["gates"] = payload["gates"][:1]
        payload["gates"][0]["disposition"] = "red"
        for rail in payload["gates"][0]["rails"]:
            if rail["key"] == failing_key:
                rail.update(status="fail", failureCode="retained-observed-failure")

    publication, payload = _publish(prepared, execution, transform=fail_last_wave)
    recorded = certification_records.record_published_generation(prepared, publication, payload)
    assert len(recorded.terminals) == 1 and recorded.as_payload()["refused"] == []
    terminal = recorded.terminals[0]
    assert terminal.result.disposition == "red" and terminal.certificate is None
    return group, prepared, execution, terminal, fail_last_wave


@pytest.mark.parametrize(
    "fault", ["gate", "missing-frozen", "wrong-kind", "foreign-plan", "corrupt-object"]
)
def test_red_terminal_journal_refuses_invalid_original_authority_without_replacing_selection(
    tmp_path: Path, fault: str
) -> None:
    group, prepared, _execution, terminal, _transform = _physical_red_terminal(tmp_path)
    journal = prepared.directory / "gates.json"
    original = journal.read_bytes()
    pointer = group / "reports/quality-report-set.json"
    pointer_bytes = pointer.read_bytes()
    rows = list(certification_evidence.read_gate_records(prepared.directory))
    row = rows[0]
    expected = "certificate-evidence-binding-mismatch"
    if fault == "gate":
        row["gate"] = 2
    elif fault == "missing-frozen":
        row.pop("frozenRun")
        expected = "certificate-evidence-binding-missing"
    elif fault == "wrong-kind":
        row["frozenRun"] = terminal.resultReference.model_dump(mode="json")
    elif fault == "foreign-plan":
        row["publication"] = published_manifest_payload(
            _foreign_publication(group, terminal.publication, "profilePlanDigest")
        )
    else:
        store = prepared.certificate_store()
        frozen_path = store.exact_path("frozen-run", prepared.frozen_run.runDigest)
        # A real object from a different closed schema cannot become a frozen run even
        # when copied to the selected address; the canonical store refuses first.
        frozen_path.write_bytes(
            store.exact_path("result-manifest", terminal.result.manifestDigest).read_bytes()
        )
        expected = "certificate-object-invalid"
    with pytest.raises(CertificationContractError) as refused:
        certification_records.journal_gate_records(group, rows)
    assert refused.value.findings[0]["code"] == expected
    assert journal.read_bytes() == original
    assert pointer.read_bytes() == pointer_bytes
    certification_evidence.verify_result_evidence(
        group / "reports", terminal.publication, terminal.result.railResults
    )


@pytest.mark.parametrize("fault", ["none", "gate", "runtime"])
def test_repeated_red_result_keeps_original_generation_or_refuses_changed_execution(
    tmp_path: Path, fault: str
) -> None:
    group, prepared, execution, terminal, transform = _physical_red_terminal(tmp_path)
    later, payload = _publish(prepared, execution, nonce="later-red", transform=transform)
    assert later.generation != terminal.publication.generation
    rows = list(certification_evidence.read_gate_records(prepared.directory))
    if fault == "gate":
        rows[0]["gate"] = 2
    elif fault == "runtime":
        rows[0]["publication"] = published_manifest_payload(
            _foreign_publication(group, terminal.publication, "runtimeAuthorityDigest")
        )
    if fault != "none":
        _damage_selected_snapshot(prepared, rows)
    journal = prepared.directory / "gates.json"
    original = journal.read_bytes()
    if fault == "none":
        recorded = certification_records.record_published_generation(prepared, later, payload)
        assert recorded.as_payload()["refused"] == []
        assert recorded.terminals == (terminal,)
        assert recorded.terminals[0].resultReference == terminal.resultReference
        assert recorded.terminals[0].publication != later
    else:
        with pytest.raises(CertificationContractError) as refused:
            certification_evidence.terminal_publication_binding(
                prepared.directory, prepared.frozen_run, terminal.result, later
            )
        assert refused.value.findings[0]["code"] == "certificate-evidence-binding-mismatch"
        assert refused.value.findings[0]["detail"] == "selected terminal execution differs"
        assert journal.read_bytes() == original
    assert prepared.certificate_store().load_reference(terminal.resultReference) == terminal.result
    certification_evidence.verify_result_evidence(
        group / "reports", terminal.publication, terminal.result.railResults
    )


@pytest.mark.parametrize("blockers", ["actual", "missing", "foreign"])
def test_terminal_catalog_resolves_only_actual_failed_same_gate_prerequisites(
    blockers: str,
) -> None:
    plan = _plan()
    gate_plan = _gate(plan, 1)
    outcomes = []
    for rail in gate_plan.rails:
        is_dependent = rail.identity.railId == "package"
        result = _result(
            gate_plan,
            ObservationSpec(
                rail.identity.railId,
                status="blocked"
                if is_dependent
                else "fail"
                if rail.identity.railId == "lint"
                else "pass",
                blocked_by=("lint",) if is_dependent else (),
            ),
        )
        row = {
            "key": rail.identity.key,
            "status": result.status,
            "evidence": [item.model_dump(mode="json") for item in result.evidence],
            "artifacts": [item.model_dump(mode="json") for item in result.artifacts],
        }
        if is_dependent and blockers != "missing":
            row["blockedBy"] = ["lint@1.0.0"] if blockers == "actual" else [None, "foreign@1.0.0"]
        outcomes.append(row)
    if blockers != "actual":
        with pytest.raises(ValueError, match="blocked rail result requires blockedBy"):
            terminal_results(gate_plan, outcomes)
        return
    results = terminal_results(gate_plan, outcomes)
    dependent = next(item for item in results if item.rail.railId == "package")
    assert tuple(item.railId for item in dependent.blockedBy) == ("lint",)
    admission = GateResultAdmission(
        profileId=plan.profileId,
        candidateIdentity=plan.candidateIdentity,
        altitude=plan.profileKind,
    )
    manifest = compile_gate_result_manifest(
        _canonical_registry(), plan, gate_plan, results, admission
    )
    assert manifest.disposition == "red"


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


def test_repeated_identical_retained_evidence_has_one_physical_transport_member(
    tmp_path: Path,
) -> None:
    selected, request = _retained_inventory_case(tmp_path, "duplicate")
    destination = tmp_path / "deduplicated-retained"
    files = retained_reports.snapshot_retained_reports(
        selected,
        reports=request.worktree_group / "reports",
        destination=destination,
    )
    repeated = [item for item in files if item.path == "clean-quality-results.json"]
    assert len(repeated) == 1
    item = repeated[0]
    assert (
        sum(
            1
            for gate in selected.retained
            for rail in gate.result.railResults
            for evidence in rail.evidence
            if evidence.reference == item.path
        )
        == 2
    )
    original = (
        request.worktree_group
        / "reports/.quality-report-generations"
        / item.publication.generation
        / item.path
    )
    assert (destination / item.path).read_bytes() == original.read_bytes()
