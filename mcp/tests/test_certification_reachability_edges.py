"""Focused refusal and graph-search edges for certification registry admission."""

from __future__ import annotations

import agents_remember.certification.limits as certification_limits
import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.certification.limits import (
    REGISTRY_VALIDATION_WORK_BUDGET,
    admit_registry_canonicalization,
    measure_registry_validation_work,
)
from certification_registry_test_support import (
    _artifact_chain_registry,
    _artifact_query_cross_product_registry,
    _distinct_artifact_chain_registry,
    _raw_declaration_overflow_registry,
)


def _accept_reservation(_size: int) -> bool:
    return True


def _reject_reservation(_size: int) -> bool:
    return False


def test_prospective_budget_is_the_preallocation_refusal_and_valid_answers_survive() -> None:
    registry = _distinct_artifact_chain_registry(6)
    work = measure_registry_validation_work(registry)
    producer_digest = content_digest(registry.rails[0])
    consumer_digest = content_digest(registry.rails[1])

    assert work.within_budget
    assert work.depends_on(consumer_digest, producer_digest)
    with pytest.raises(ValueError, match="pair was not part"):
        work.depends_on("0" * 64, "1" * 64)

    refused = measure_registry_validation_work(_artifact_query_cross_product_registry(512, 128))
    assert not refused.within_budget
    assert refused.reachability_operation_units == 0
    with pytest.raises(ValueError, match="unavailable for a refused registry"):
        refused.depends_on(consumer_digest, producer_digest)


def test_raw_declaration_overflow_refuses_before_cross_reference_or_graph_work() -> None:
    over = _raw_declaration_overflow_registry()

    admission = admit_registry_canonicalization(over)
    work = measure_registry_validation_work(over)

    assert admission.declaration_units == REGISTRY_VALIDATION_WORK_BUDGET + 1
    assert admission.cross_reference_units == 0
    assert not admission.within_budget
    assert work.declaration_units == REGISTRY_VALIDATION_WORK_BUDGET + 1
    assert work.cross_reference_units == 0
    assert work.reachability_operation_units == 0
    assert work.reachability_storage_units == 0
    assert not work.reachability_complete


def test_identical_artifact_query_is_idempotent_before_shared_promotion() -> None:
    singleton: dict[str, str] = {}
    shared: dict[str, dict[str, None]] = {}

    certification_limits._add_artifact_query(singleton, shared, "consumer-a", "producer")
    certification_limits._add_artifact_query(singleton, shared, "consumer-a", "producer")
    assert singleton == {"producer": "consumer-a"}
    assert shared == {}

    certification_limits._add_artifact_query(singleton, shared, "consumer-b", "producer")
    certification_limits._add_artifact_query(singleton, shared, "consumer-c", "producer")
    assert singleton == {}
    assert shared == {"producer": {"consumer-a": None, "consumer-b": None, "consumer-c": None}}


def test_singleton_resolver_publishes_incomplete_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        certification_limits,
        "_search_prerequisites",
        lambda *_args: (False, False),
    )

    work = measure_registry_validation_work(_distinct_artifact_chain_registry(3))

    assert not work.reachability_complete
    assert work.reachability_query_count == 2
    assert work.reachability_operation_units == 0
    assert work.reachability_storage_units > 0


def test_shared_resolver_publishes_incomplete_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        certification_limits,
        "_search_dependants",
        lambda *_args: (False, set()),
    )

    work = measure_registry_validation_work(_artifact_chain_registry(3))

    assert not work.reachability_complete
    assert work.reachability_query_count == 2
    assert work.reachability_operation_units == 0
    assert work.reachability_storage_units > 0


def test_prerequisite_search_refuses_each_budget_boundary() -> None:
    assert certification_limits._search_prerequisites(
        "consumer",
        "producer",
        {},
        _reject_reservation,
        _accept_reservation,
    ) == (False, False)
    assert certification_limits._search_prerequisites(
        "consumer",
        "producer",
        {},
        _accept_reservation,
        _reject_reservation,
    ) == (False, False)

    calls = 0

    def reject_candidate_reservation(_size: int) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    assert certification_limits._search_prerequisites(
        "consumer",
        "producer",
        {"consumer": ["middle"]},
        _accept_reservation,
        reject_candidate_reservation,
    ) == (False, False)


def test_prerequisite_search_covers_new_nodes_cycles_and_exhaustion() -> None:
    assert certification_limits._search_prerequisites(
        "consumer",
        "producer",
        {"consumer": ["middle"], "middle": []},
        _accept_reservation,
        _accept_reservation,
    ) == (True, False)
    assert certification_limits._search_prerequisites(
        "consumer",
        "producer",
        {"consumer": ["middle"], "middle": ["consumer"]},
        _accept_reservation,
        _accept_reservation,
    ) == (True, False)


def test_dependant_search_handles_self_targets_and_each_budget_boundary() -> None:
    self_target = {"producer": None}
    assert certification_limits._search_dependants(
        "producer",
        self_target,
        {},
        _accept_reservation,
        _accept_reservation,
    ) == (True, set())
    assert certification_limits._search_dependants(
        "producer",
        {"consumer": None},
        {},
        _reject_reservation,
        _accept_reservation,
    ) == (False, set())
    assert certification_limits._search_dependants(
        "producer",
        {"consumer": None},
        {},
        _accept_reservation,
        _reject_reservation,
    ) == (False, set())

    calls = 0

    def reject_candidate_reservation(_size: int) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    assert certification_limits._search_dependants(
        "producer",
        {"consumer": None},
        {"producer": ["consumer"]},
        _accept_reservation,
        reject_candidate_reservation,
    ) == (False, set())


def test_dependant_search_is_cycle_safe_and_retains_unreachable_targets() -> None:
    assert certification_limits._search_dependants(
        "producer",
        {"missing": None},
        {"producer": ["consumer"], "consumer": ["producer"]},
        _accept_reservation,
        _accept_reservation,
    ) == (True, {"missing"})
