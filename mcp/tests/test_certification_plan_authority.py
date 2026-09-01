"""Registry authority, boundedness, and scaling contracts for certification plans."""

from __future__ import annotations

from time import perf_counter
from typing import Literal

import agents_remember.certification.canonical as certification_canonical
import pytest
from agents_remember.certification import (
    canonicalize_registry,
    compile_certification_plan,
    compile_gate_result_manifest,
    validate_registry,
)
from agents_remember.certification.limits import (
    REGISTRY_VALIDATION_WORK_BUDGET,
    admit_registry_canonicalization,
    measure_registry_validation_work,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    CertificationPlan,
    CompiledRail,
    GatePlan,
    GateResultAdmission,
    RegistryProfile,
    canonical_execution_waves,
)
from agents_remember.errors import CertificationContractError
from certification_registry_test_support import (
    _CANDIDATE,
    ObservationSpec,
    RailSpec,
    _artifact_chain_registry,
    _artifact_query_cross_product_registry,
    _canonical_registry,
    _dense_self_artifact_registry,
    _distinct_artifact_chain_registry,
    _finding_codes,
    _gate,
    _gate_one_graph_registry,
    _pad_evidence_work_units,
    _pad_inert_output_declarations,
    _plan,
    _portable_specs,
    _rebuild_certification_plan,
    _rebuild_gate_plan,
    _registry,
    _result,
    _self_artifact_registry,
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


def test_many_distinct_artifact_producers_have_linear_operations_and_storage() -> None:
    totals: dict[int, int] = {}
    for size in (1024, 2048, 4096):
        registry = _distinct_artifact_chain_registry(size)
        work = measure_registry_validation_work(registry)
        query_count = (size * 2) // 3

        assert work.within_budget
        assert work.reachability_query_count == query_count
        assert work.reachability_operation_units == query_count * 2
        assert work.reachability_max_search_identities == 2
        assert work.reachability_digest_index_units == 3 * size
        assert work.reachability_producer_catalog_units == 2 * query_count
        assert work.reachability_query_answer_units == 2 * query_count
        assert work.reachability_graph_units == 2 * size - 1
        assert work.reachability_storage_units == 5 * size + 2 * query_count + 1
        assert validate_registry(_canonical_registry(registry)).ok
        totals[size] = work.total_units

    assert totals[4096] < totals[1024] * 5


def test_one_shared_artifact_producer_has_one_bounded_forward_search() -> None:
    totals: dict[int, int] = {}
    for size in (1024, 2048, 4096):
        registry = _artifact_chain_registry(size)
        work = measure_registry_validation_work(registry)

        assert work.within_budget
        assert work.reachability_query_count == size - 1
        assert work.reachability_operation_units == 2 * (size - 1)
        assert work.reachability_max_search_identities == size + 1
        assert work.reachability_digest_index_units == 3 * size
        assert work.reachability_producer_catalog_units == 2
        assert work.reachability_query_answer_units == 2 * (size - 1)
        assert work.reachability_graph_units == 2 * size - 1
        assert work.reachability_storage_units == 8 * size - 2
        assert validate_registry(_canonical_registry(registry)).ok
        totals[size] = work.total_units

    assert totals[4096] < totals[1024] * 5


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


def test_zero_query_unconsumed_output_skips_every_producer_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    registry = _registry(
        tuple(
            RailSpec(
                f"unconsumed-{index:04d}",
                1,
                output_artifacts=("unconsumed-artifact",) if index == 0 else (),
            )
            for index in range(1024)
        ),
        profile=profile,
    )
    allocations = {"search-census": 0, "query-catalog": 0}

    def unexpected_search_census(*_args: object, **_kwargs: object) -> object:
        allocations["search-census"] += 1
        raise AssertionError("zero-query input allocated a search producer census")

    def unexpected_query_catalog(*_args: object, **_kwargs: object) -> object:
        allocations["query-catalog"] += 1
        raise AssertionError("zero-query input allocated an artifact producer catalog")

    monkeypatch.setattr(
        "agents_remember.certification.limits._search_state_upper_bound",
        unexpected_search_census,
    )
    monkeypatch.setattr(
        "agents_remember.certification.limits._artifact_reachability_queries",
        unexpected_query_catalog,
    )

    base_admission = admit_registry_canonicalization(registry)
    exact = _pad_evidence_work_units(
        registry,
        REGISTRY_VALIDATION_WORK_BUDGET - base_admission.validation_floor_units,
    )
    admission = admit_registry_canonicalization(exact)
    work = measure_registry_validation_work(exact)

    assert (admission.declaration_units, admission.cross_reference_units) == (130048, 1024)
    assert admission.validation_floor_units == REGISTRY_VALIDATION_WORK_BUDGET
    assert admission.within_budget
    assert work.total_units == REGISTRY_VALIDATION_WORK_BUDGET
    assert work.reachability_complete
    assert work.reachability_query_count == 0
    assert work.reachability_operation_units == 0
    assert work.reachability_storage_units == 0
    assert (
        work.reachability_digest_index_units,
        work.reachability_producer_catalog_units,
        work.reachability_query_answer_units,
        work.reachability_graph_units,
        work.reachability_max_search_identities,
    ) == (0, 0, 0, 0, 0)
    assert validate_registry(canonicalize_registry(exact)).ok

    over = _pad_evidence_work_units(exact, 1)
    over_admission = admit_registry_canonicalization(over)
    over_work = measure_registry_validation_work(over)
    assert over_admission.validation_floor_units == REGISTRY_VALIDATION_WORK_BUDGET + 1
    assert not over_admission.within_budget
    assert over_work.total_units == REGISTRY_VALIDATION_WORK_BUDGET + 1
    assert not over_work.within_budget
    assert over_work.reachability_query_count == 0
    assert over_work.reachability_operation_units == 0
    assert over_work.reachability_storage_units == 0
    with pytest.raises(CertificationContractError) as caught:
        canonicalize_registry(over)
    assert _finding_codes(caught.value) == {"registry-validation-budget-exceeded"}
    assert allocations == {"search-census": 0, "query-catalog": 0}


def test_exact_self_query_and_dense_graph_peaks_are_fully_censused() -> None:
    cases = (
        (_self_artifact_registry(1024), (3072, 2048, 2048, 1024, 0, 7168)),
        (_dense_self_artifact_registry(252), (756, 2, 2, 31878, 0, 32636)),
    )
    for registry, expected in cases:
        work = measure_registry_validation_work(registry)
        observed = (
            work.reachability_digest_index_units,
            work.reachability_producer_catalog_units,
            work.reachability_query_answer_units,
            work.reachability_graph_units,
            work.reachability_max_search_identities,
            work.reachability_storage_units,
        )
        assert observed == expected
        assert validate_registry(canonicalize_registry(registry)).ok

    self_registry = cases[0][0]
    self_work = measure_registry_validation_work(self_registry)
    exact_self = _pad_evidence_work_units(
        self_registry,
        REGISTRY_VALIDATION_WORK_BUDGET - self_work.total_units,
    )
    assert admit_registry_canonicalization(exact_self).within_budget
    assert measure_registry_validation_work(exact_self).total_units == (
        REGISTRY_VALIDATION_WORK_BUDGET
    )
    assert validate_registry(canonicalize_registry(exact_self)).ok

    dense_registry = cases[1][0]
    dense_with_outputs = _pad_inert_output_declarations(dense_registry, 2167)
    dense_work = measure_registry_validation_work(dense_with_outputs)
    assert REGISTRY_VALIDATION_WORK_BUDGET - dense_work.total_units == 32004
    exact_dense = _pad_evidence_work_units(dense_with_outputs, 32004)
    assert admit_registry_canonicalization(exact_dense).within_budget
    exact_dense_work = measure_registry_validation_work(exact_dense)
    assert exact_dense_work.total_units == REGISTRY_VALIDATION_WORK_BUDGET
    assert exact_dense_work.reachability_storage_units == 32636
    assert validate_registry(canonicalize_registry(exact_dense)).ok


@pytest.mark.parametrize("shape", ["chain", "broad"])
def test_wave_compilation_scales_for_chain_and_broad_catalogs(
    shape: Literal["chain", "broad"],
) -> None:
    elapsed: dict[int, float] = {}
    for size in (1024, 2048, 4096):
        registry = (
            _gate_one_graph_registry(size, dense=False)
            if shape == "chain"
            else _broad_registry(size)
        )
        started = perf_counter()
        plan = _plan(registry)
        elapsed[size] = perf_counter() - started
        assert sum(len(wave) for wave in _gate(plan, 1).waves) == size

    assert elapsed[4096] < max(4.0, elapsed[1024] * 12)


def _broad_registry(size: int):
    specs = tuple(RailSpec(f"broad-{index:04d}", 1) for index in range(size))
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    return _registry(specs, profile=profile)


def test_registry_work_budget_accepts_exact_cap_and_refuses_cap_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary_shape = _artifact_chain_registry(4096)
    boundary_work = measure_registry_validation_work(boundary_shape)
    exact = _pad_evidence_work_units(
        boundary_shape,
        REGISTRY_VALIDATION_WORK_BUDGET - boundary_work.total_units,
    )
    exact_work = measure_registry_validation_work(exact)
    assert exact_work.total_units == REGISTRY_VALIDATION_WORK_BUDGET
    assert validate_registry(_canonical_registry(exact)).ok

    over = _pad_evidence_work_units(exact, 1)
    over_admission = admit_registry_canonicalization(over)
    assert over_admission.validation_floor_units == REGISTRY_VALIDATION_WORK_BUDGET + 1
    assert not over_admission.within_budget

    def unexpected_canonicalization(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("cap-plus-one reached normalization or digest allocation")

    monkeypatch.setattr(
        certification_canonical,
        "_normalize_rail",
        unexpected_canonicalization,
    )
    monkeypatch.setattr(
        certification_canonical,
        "content_digest",
        unexpected_canonicalization,
    )
    with pytest.raises(CertificationContractError) as caught:
        canonicalize_registry(over)
    assert _finding_codes(caught.value) == {"registry-validation-budget-exceeded"}


def test_canonicalizer_reuses_normalized_digests_after_exact_deduplication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    duplicated = registry.model_copy(
        update={
            "profiles": (*registry.profiles, registry.profiles[0]),
            "rails": (*registry.rails, registry.rails[0]),
        }
    )
    admission = admit_registry_canonicalization(duplicated)
    original_digest = certification_canonical.content_digest
    calls = 0

    def counted_digest(value: object) -> str:
        nonlocal calls
        calls += 1
        return original_digest(value)  # type: ignore[arg-type]

    monkeypatch.setattr(certification_canonical, "content_digest", counted_digest)
    canonical = canonicalize_registry(duplicated)

    assert len(canonical.registry.profiles) == len(registry.profiles)
    assert len(canonical.registry.rails) == len(registry.rails)
    assert calls == admission.canonicalization_digest_units - 1


def test_budget_has_measured_repository_scale_headroom() -> None:
    rail_ids = tuple(f"operation-{index:02d}" for index in range(22))
    specs = tuple(
        RailSpec(rail_id, 1, prerequisites=rail_ids[:index])
        for index, rail_id in enumerate(rail_ids)
    )
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    repository_scale = measure_registry_validation_work(_registry(specs, profile=profile))

    assert repository_scale.total_units == 552
    assert REGISTRY_VALIDATION_WORK_BUDGET // repository_scale.total_units == 237
