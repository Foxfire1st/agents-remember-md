"""Registry authority, boundedness, and scaling contracts for certification plans."""

from __future__ import annotations

import pytest
from agents_remember.certification import (
    compile_certification_plan,
    compile_gate_result_manifest,
)
from agents_remember.certification.limits import (
    REGISTRY_VALIDATION_WORK_BUDGET,
    measure_registry_validation_work,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    CertificationPlan,
    CompiledRail,
    GatePlan,
    GateResultAdmission,
    canonical_execution_waves,
)
from agents_remember.errors import CertificationContractError
from certification_registry_test_support import (
    _CANDIDATE,
    ObservationSpec,
    RailSpec,
    _artifact_query_cross_product_registry,
    _canonical_registry,
    _finding_codes,
    _gate,
    _plan,
    _portable_specs,
    _rebuild_certification_plan,
    _rebuild_gate_plan,
    _registry,
    _result,
)


def _forge_gate_catalog(gate: GatePlan, rails: tuple[CompiledRail, ...]) -> GatePlan:
    ordered = tuple(
        sorted(
            rails,
            key=lambda rail: (
                rail.orderKey,
                rail.identity.railId,
                rail.identity.version,
                rail.definitionDigest,
            ),
        )
    )
    waves = canonical_execution_waves(ordered)
    return _rebuild_gate_plan(
        gate,
        rails=[rail.model_dump(mode="json") for rail in ordered],
        waves=[[identity.model_dump(mode="json") for identity in wave] for wave in waves],
    )


def _replace_gate(plan: CertificationPlan, gate: GatePlan) -> CertificationPlan:
    gates = tuple(gate if item.gate == gate.gate else item for item in plan.gates)
    return _rebuild_certification_plan(plan, gates)


def _pass_results(gate: GatePlan):
    return tuple(_result(gate, ObservationSpec(rail.identity.railId)) for rail in gate.rails)


def _certifying_admission() -> GateResultAdmission:
    return GateResultAdmission(
        profileId="portable-ci",
        candidateIdentity=_CANDIDATE,
        altitude="certifying",
    )


def test_result_publication_refuses_deleted_inserted_and_substituted_plan_catalogs() -> None:
    canonical = _canonical_registry()
    plan = _plan()
    gate = _gate(plan, 1)

    deleted_gate = _forge_gate_catalog(
        gate,
        tuple(rail for rail in gate.rails if rail.identity.railId != "types"),
    )
    inserted_registry = _registry((*_portable_specs(), RailSpec("inserted", 1)))
    inserted = next(
        rail
        for rail in _gate(_plan(inserted_registry), 1).rails
        if rail.identity.railId == "inserted"
    )
    inserted_gate = _forge_gate_catalog(gate, (*gate.rails, inserted))
    changed_lint = gate.rails[0].model_copy(
        update={
            "adapter": gate.rails[0].adapter.model_copy(
                update={"executionEvidence": "adapter://substituted"}
            )
        }
    )
    substituted_gate = _forge_gate_catalog(
        gate,
        tuple(
            changed_lint if rail.identity == changed_lint.identity else rail for rail in gate.rails
        ),
    )

    for forged_gate in (deleted_gate, inserted_gate, substituted_gate):
        forged_plan = _replace_gate(plan, forged_gate)
        with pytest.raises(CertificationContractError) as caught:
            compile_gate_result_manifest(
                canonical,
                forged_plan,
                forged_gate,
                _pass_results(forged_gate),
                _certifying_admission(),
            )
        assert _finding_codes(caught.value) == {"certification-plan-not-authorized"}


def test_result_publication_binds_the_external_candidate_identity() -> None:
    canonical = _canonical_registry()
    foreign_candidate = CandidateIdentity(kind="content-digest", value="f" * 64)
    foreign_plan = compile_certification_plan(
        canonical,
        profile_id="portable-ci",
        candidate_identity=foreign_candidate,
    )
    gate = _gate(foreign_plan, 1)

    with pytest.raises(CertificationContractError) as caught:
        compile_gate_result_manifest(
            canonical,
            foreign_plan,
            gate,
            _pass_results(gate),
            _certifying_admission(),
        )

    assert _finding_codes(caught.value) == {"certification-plan-not-authorized"}


def test_hostile_artifact_query_product_refuses_before_query_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _artifact_query_cross_product_registry(512, 128)

    def unexpected_query_allocation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("query cross-product was allocated before budget refusal")

    monkeypatch.setattr(
        "agents_remember.certification.limits._artifact_reachability_queries",
        unexpected_query_allocation,
    )
    work = measure_registry_validation_work(registry)
    assert not work.reachability_complete
    assert work.reachability_query_count == 512 * 128
    assert work.reachability_operation_units == 0
    assert work.total_units > REGISTRY_VALIDATION_WORK_BUDGET

    with pytest.raises(CertificationContractError) as caught:
        _canonical_registry(registry)
    assert _finding_codes(caught.value) == {"registry-validation-budget-exceeded"}
