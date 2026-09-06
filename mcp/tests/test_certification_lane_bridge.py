"""CCR-L28 production R11<->R22 certification-lane bridge contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from agents_remember.certification.certificate_models import CreationProvenance
from agents_remember.certification.certification_lane import (
    CertificationLane,
    admit_certification_lane,
    compile_certification_lane,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    RailAdapterDefinition,
    RailApplicability,
    RailDefinition,
    RailEvidenceContract,
    RailRuntimeInputs,
)
from agents_remember.certification.repository_profiles.authority import (
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.models import ProfileMode
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
    resolve_repository_profile_selection,
)
from agents_remember.errors import CertificationContractError
from agents_remember.memory_quality.gate_five_rails import gate_five_memory_rails
from agents_remember.models.certification.base import RailIdentity
from source_selection_test_support import source_selection_fixture

_PROFILE = Path(__file__).resolve().parents[1] / "certification-profile-v1.json"
_REPOSITORY_ID = "agents-remember"
_CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)


def _admitted(tmp_path: Path, purpose: str = "closeout", mode: ProfileMode = "targeted"):
    root = tmp_path / "profile-root"
    root.mkdir()
    (root / "certification-profile-v1.json").write_bytes(_PROFILE.read_bytes())
    return load_repository_profile(_REPOSITORY_ID, root, "certification-profile-v1.json")


def _plan(admitted, mode: ProfileMode = "targeted"):
    selection = resolve_repository_profile_selection(
        admitted.canonical, purpose="closeout", mode=mode
    )
    return compile_repository_profile_plan(
        admitted.canonical,
        selection_id=selection.selectionId,
        candidate_identity=_CANDIDATE,
        source_selection=source_selection_fixture(_CANDIDATE.value),
    )


def _provenance(marker: str = "one") -> CreationProvenance:
    return CreationProvenance(
        createdAt=f"2026-09-05T00:00:0{1 if marker == 'one' else 2}+00:00",
        producer=f"lane-{marker}",
        evidenceRef=f"evidence://lane-{marker}",
    )


def _lane(admitted, *, mode: ProfileMode = "targeted", marker: str = "one") -> CertificationLane:
    return compile_certification_lane(
        admitted.canonical,
        _plan(admitted, mode=mode),
        provenance=_provenance(marker),
        memory_rails=gate_five_memory_rails("closeout-targeted"),
    )


def test_bridge_derives_one_canonical_five_gate_authority_from_the_real_profile(
    tmp_path: Path,
) -> None:
    admitted = _admitted(tmp_path)
    repository_plan = _plan(admitted)
    lane = _lane(admitted)
    assert lane.selectionId == "closeout-targeted"
    # The R11 registry carries every admitted repository rail plus the memory rails.
    repository_rail_keys = {
        rail.identity.key
        for gate in repository_plan.gates
        if gate.applicability == "applicable"
        for rail in gate.rails
    }
    registry_rail_keys = {rail.identity.key for rail in lane.registry.registry.rails}
    assert repository_rail_keys <= registry_rail_keys
    assert len(registry_rail_keys) == len(repository_rail_keys) + len(
        gate_five_memory_rails("closeout-targeted")
    )
    # The certification plan spans all five gates with the exact rail populations.
    assert tuple(gate.gate for gate in lane.certificationPlan.gates) == (1, 2, 3, 4, 5)
    rails_per_gate = {
        gate.gate: tuple(rail.identity.key for rail in gate.rails)
        for gate in lane.certificationPlan.gates
    }
    for gate in range(1, 5):
        repository_gate = next(item for item in repository_plan.gates if item.gate == gate)
        assert rails_per_gate[gate] == tuple(rail.identity.key for rail in repository_gate.rails)
    assert len(rails_per_gate[5]) == len(gate_five_memory_rails("closeout-targeted"))
    # The admission froze the exact repository/candidate identity.
    envelope = lane.admission.semanticEnvelope
    assert envelope.repositoryId == _REPOSITORY_ID
    assert envelope.candidateCodeTree == _CANDIDATE
    assert envelope.profileId == lane.selectionId


def test_bridge_is_deterministic_and_ignores_creation_provenance(tmp_path: Path) -> None:
    admitted = _admitted(tmp_path)
    first = _lane(admitted, marker="one")
    second = _lane(admitted, marker="two")
    assert first.admission.admissionDigest == second.admission.admissionDigest
    assert first.registry.registryDigest == second.registry.registryDigest
    assert first.certificationPlan.planDigest == second.certificationPlan.planDigest
    assert first.registry == second.registry
    assert first.certificationPlan == second.certificationPlan
    # Provenance differs; the semantic envelope and digest must not.
    assert first.admission.semanticEnvelope == second.admission.semanticEnvelope
    assert first.admission.provenance != second.admission.provenance


def test_bridge_rejects_a_missing_gate_five_population(tmp_path: Path) -> None:
    admitted = _admitted(tmp_path)
    with pytest.raises(CertificationContractError) as raised:
        compile_certification_lane(
            admitted.canonical,
            _plan(admitted),
            provenance=_provenance(),
            memory_rails=(),
        )
    codes = {str(item["code"]) for item in raised.value.findings}
    assert codes == {"gate-five-memory-rails-missing"}


def test_bridge_refuses_a_not_applicable_repository_selection(tmp_path: Path) -> None:
    admitted = _admitted(tmp_path)
    selection = resolve_repository_profile_selection(
        admitted.canonical, purpose="local-precommit", mode="targeted"
    )
    plan = compile_repository_profile_plan(
        admitted.canonical,
        selection_id=selection.selectionId,
        candidate_identity=_CANDIDATE,
    )
    with pytest.raises(CertificationContractError) as raised:
        compile_certification_lane(
            admitted.canonical,
            plan,
            provenance=_provenance(),
            memory_rails=gate_five_memory_rails(selection.selectionId),
        )
    codes = {str(item["code"]) for item in raised.value.findings}
    assert codes == {"repository-gate-not-applicable"}


def test_bridge_memory_rails_are_part_of_the_registry_digest(tmp_path: Path) -> None:
    admitted = _admitted(tmp_path)
    repository_plan = _plan(admitted)
    lane = _lane(admitted)
    # The memory rails bound their registry identity: replacing the vocabulary
    # digest must change the frozen admission so a memory-domain change can never
    # masquerade as an unchanged code gate.
    other_rails = tuple(
        rail.model_copy(
            update={"adapter": rail.adapter.model_copy(update={"configurationDigest": "b" * 64})}
        )
        for rail in gate_five_memory_rails("closeout-targeted")
    )
    other = compile_certification_lane(
        admitted.canonical,
        repository_plan,
        provenance=_provenance(),
        memory_rails=other_rails,
    )
    assert other.admission.admissionDigest != lane.admission.admissionDigest


def _invalid_memory_rail(profile_id: str) -> RailDefinition:
    return RailDefinition(
        identity=RailIdentity(railId="not-gate-five", version="1.0.0"),
        gate=4,
        railClass="pre-test-quality",
        authority="repository-profile",
        ownerClass="repository-quality",
        correctiveOwner="repository-quality",
        posture="enforcing",
        orderKey="not-gate-five",
        prerequisites=(),
        requiredArtifacts=(),
        adapter=RailAdapterDefinition(
            adapterKind="container-command",
            adapterId="not-gate-five-adapter",
            configurationDigest="a" * 64,
            executionEvidence="fixture://not-gate-five",
        ),
        runtimeInputs=RailRuntimeInputs(runtimeIdentity="fixture"),
        applicability=(
            RailApplicability(
                profileId=profile_id,
                status="applicable",
                selectionIdentity="fixture",
                population="fixture",
            ),
        ),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId="fixture-evidence",
                mediaType="application/json",
                maxBytes=1024,
            ),
        ),
    )


def test_bridge_admit_currentness_reread_accepts_and_refuses_movement(tmp_path: Path) -> None:
    admitted = _admitted(tmp_path)
    repository_plan = _plan(admitted)
    memory = gate_five_memory_rails("closeout-targeted")
    lane = compile_certification_lane(
        admitted.canonical,
        repository_plan,
        provenance=_provenance("one"),
        memory_rails=memory,
    )
    # An unchanged reread returns the exact lane.
    reread = admit_certification_lane(
        admitted.canonical,
        lane,
        provenance=_provenance("one"),
        memory_rails=memory,
    )
    assert reread == lane
    # Movement (a different provenance marker) is a stale-lane refusal.
    with pytest.raises(CertificationContractError) as raised:
        admit_certification_lane(
            admitted.canonical,
            lane,
            provenance=_provenance("two"),
            memory_rails=memory,
        )
    codes = {str(item["code"]) for item in raised.value.findings}
    assert codes == {"certification-lane-mismatch"}


def test_bridge_registry_id_is_bound_when_supplied(tmp_path: Path) -> None:
    admitted = _admitted(tmp_path)
    repository_plan = _plan(admitted)
    memory = gate_five_memory_rails("closeout-targeted")
    first = compile_certification_lane(
        admitted.canonical,
        repository_plan,
        provenance=_provenance(),
        memory_rails=memory,
        registry_id="explicit-registry",
    )
    second = compile_certification_lane(
        admitted.canonical,
        repository_plan,
        provenance=_provenance(),
        memory_rails=memory,
        registry_id="explicit-registry",
    )
    assert first.registry.registryDigest == second.registry.registryDigest
    assert first.admission.admissionDigest != _lane(admitted).admission.admissionDigest


def test_bridge_refuses_an_invalid_gate_five_memory_rail(tmp_path: Path) -> None:
    admitted = _admitted(tmp_path)
    repository_plan = _plan(admitted)
    with pytest.raises(CertificationContractError) as raised:
        compile_certification_lane(
            admitted.canonical,
            repository_plan,
            provenance=_provenance(),
            memory_rails=(_invalid_memory_rail("closeout-targeted"),),
        )
    codes = {str(item["code"]) for item in raised.value.findings}
    assert codes == {"gate-five-memory-rail-invalid"}
