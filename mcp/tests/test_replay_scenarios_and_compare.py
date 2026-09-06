"""Fully standalone CCR-R17 acceptance-scenario and comparison-report tests.

The seventeen mandatory acceptance scenarios are projected over measured replay
evidence only; scenarios that need a pair or a repository profile return
not-applicable when that evidence is absent and never fabricate a green.
Numeric reduction thresholds are out of approved scope and are never asserted.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.certification.replay.compare import (
    ReplayComparisonInput,
    ReplayComparisonReport,
    build_replay_comparison_report,
)
from agents_remember.certification.replay.freeze import (
    ReplayFreeze,
    ReplayFreezeInput,
    compare_replay_freezes,
    compile_replay_freeze,
    compile_replay_population,
)
from agents_remember.certification.replay.models import (
    CatalogRailRecord,
    GateRunMeasurement,
    PopulationGeneration,
    ReplayLegIdentity,
    ReplayProfileSnapshot,
    ReplayRailPlacement,
    ReplayScenarioEvidence,
    RunMeasurement,
)
from agents_remember.certification.replay.scenarios import (
    REPLAY_ACCEPTANCE_SCENARIOS,
    evaluate_all_replay_scenarios,
    evaluate_replay_scenario,
)
from agents_remember.certification.replay.spans import analyze_span_categories
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import GateId, RailIdentity

_DIGEST = "a" * 64
_IDENTIFIER = "x" * 40


def _rail(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def _catalog(status: str, rail_id: str) -> CatalogRailRecord:
    return CatalogRailRecord(
        rail=_rail(rail_id),
        resultId=_DIGEST,
        status=status,  # type: ignore[arg-type]
        posture="enforcing",
    )


def _freeze(**overrides: object) -> ReplayFreeze:
    base = {
        "sourceRevision": "pre-commit",
        "candidateDigest": _DIGEST,
        "profileId": "agents-remember-certification",
        "profileDigest": _DIGEST,
        "planDigest": _DIGEST,
        "configurationDigest": _DIGEST,
        "runtimeIdentity": "cpython-3.13.15",
        "toolchainDigest": _DIGEST,
        "executorDigest": _DIGEST,
        "imageDigest": _DIGEST,
        "machineClass": "x86_64-linux",
        "instrumentationOnly": True,
        "measurementSchema": "measured-replay/v1",
    }
    base.update(overrides)
    return compile_replay_freeze(ReplayFreezeInput(**base))  # type: ignore[arg-type]


def _gate(
    gate: int,
    **changes: Any,
) -> GateRunMeasurement:
    base = GateRunMeasurement(gate=cast(GateId, gate))
    return base.model_copy(update=changes)


def _catalog_gate(gate: int, records: list[CatalogRailRecord]) -> GateRunMeasurement:
    red = any(record.status in {"fail", "blocked"} for record in records)
    return _gate(
        gate,
        started=True,
        startedCount=1,
        catalogCount=1,
        lastCatalogDisposition="red" if red else "green",
        lastCatalog=tuple(records),
        decision="fail" if red else "pass-published",
    )


def _spans_empty() -> Any:
    return analyze_span_categories([])


def _run_measurement(
    *,
    gates: list[GateRunMeasurement] | None = None,
    leg: ReplayLegIdentity | None = None,
    **overrides: Any,
) -> RunMeasurement:
    ordered = gates if gates is not None else [_gate(g) for g in (1, 2, 3, 4, 5)]
    identity = leg or ReplayLegIdentity(role="treatment", freezeDigest=_DIGEST)
    draft = RunMeasurement.model_construct(
        executionId="replay-run",
        leg=identity,
        admitted=True,
        gates=tuple(ordered),
        spans=_spans_empty(),
        measurementDigest="",
        **overrides,
    )
    payload = draft.model_dump(mode="json", exclude={"measurementDigest"})
    digest = _sha(payload)
    return RunMeasurement.model_validate({**payload, "measurementDigest": digest})


def _sha(value: object) -> str:
    return content_digest(value)  # type: ignore[arg-type]


def _evidence(treatment: RunMeasurement, **overrides: Any) -> ReplayScenarioEvidence:
    base: dict[str, Any] = {"treatment": treatment}
    base.update(overrides)
    return ReplayScenarioEvidence(**base)  # type: ignore[arg-type]


def test_acceptance_catalog_holds_seventeen_scenarios() -> None:
    assert len(REPLAY_ACCEPTANCE_SCENARIOS) == 17
    assert {item.scenarioId for item in REPLAY_ACCEPTANCE_SCENARIOS} == {
        f"r17-scenario-{number:02d}" for number in range(1, 18)
    }
    for item in REPLAY_ACCEPTANCE_SCENARIOS:
        assert item.title
        assert item.requirement


def test_evaluate_all_returns_ordered_machine_readable_outcomes() -> None:
    treatment = _run_measurement()
    outcomes = evaluate_all_replay_scenarios(_evidence(treatment))
    assert len(outcomes) == 17
    assert [item.scenarioId for item in outcomes] == [
        f"r17-scenario-{number:02d}" for number in range(1, 18)
    ]
    assert all(isinstance(item.state, str) for item in outcomes)
    assert all(item.state in {"green", "red", "refused", "not-applicable"} for item in outcomes)


def test_unknown_scenario_refuses() -> None:
    treatment = _run_measurement()
    with pytest.raises(CertificationContractError):
        evaluate_replay_scenario("r17-scenario-99", _evidence(treatment))


def test_scenario_01_two_independent_gate_one_failures_in_one_catalog() -> None:
    treatment = _run_measurement(
        gates=[
            _catalog_gate(
                1,
                [
                    _catalog("fail", "pyright"),
                    _catalog("fail", "file-size"),
                    _catalog("pass", "layering"),
                ],
            ),
            _gate(2),
            _gate(3),
            _gate(4),
            _gate(5),
        ]
    )
    outcome = evaluate_replay_scenario("r17-scenario-01", _evidence(treatment))
    assert outcome.state == "green"
    scarce = _run_measurement(
        gates=[
            _catalog_gate(1, [_catalog("fail", "pyright"), _catalog("pass", "layering")]),
            _gate(2),
            _gate(3),
            _gate(4),
            _gate(5),
        ]
    )
    assert evaluate_replay_scenario("r17-scenario-01", _evidence(scarce)).state == "red"


def test_scenario_03_file_size_fault_produces_zero_later_starts() -> None:
    treatment = _run_measurement(
        gates=[
            _catalog_gate(1, [_catalog("fail", "file-size"), _catalog("pass", "ruff")]),
            _gate(2, blocked=True, zeroStartEvidence=True, decision="blocked"),
            _gate(3, blocked=True, zeroStartEvidence=True, decision="blocked"),
            _gate(4, blocked=True, zeroStartEvidence=True, decision="blocked"),
            _gate(5, blocked=True, zeroStartEvidence=True, decision="blocked"),
        ]
    )
    evidence = _evidence(treatment, faultRails=(_rail("file-size"),))
    assert evaluate_replay_scenario("r17-scenario-03", evidence).state == "green"
    # A later start after the fault contradicts zero-start evidence.
    leaked = _run_measurement(
        gates=[
            _catalog_gate(1, [_catalog("fail", "file-size")]),
            _gate(2, started=True, startedCount=1),
            _gate(3),
            _gate(4),
            _gate(5),
        ]
    )
    leaked_evidence = _evidence(leaked, faultRails=(_rail("file-size"),))
    assert evaluate_replay_scenario("r17-scenario-03", leaked_evidence).state == "red"


def test_scenario_05_gate_two_failure_blocks_later_gates() -> None:
    treatment = _run_measurement(
        gates=[
            _catalog_gate(1, [_catalog("pass", "ruff")]),
            _catalog_gate(2, [_catalog("fail", "python-suite")]),
            _gate(3, blocked=True, zeroStartEvidence=True, decision="blocked"),
            _gate(4, blocked=True, zeroStartEvidence=True, decision="blocked"),
            _gate(5, blocked=True, zeroStartEvidence=True, decision="blocked"),
        ]
    )
    assert evaluate_replay_scenario("r17-scenario-05", _evidence(treatment)).state == "green"


def test_scenario_09_memory_repair_reuses_gates_one_to_four() -> None:
    baseline = _run_measurement(
        gates=[
            _catalog_gate(1, [_catalog("pass", "ruff")]),
            _catalog_gate(2, [_catalog("pass", "python-suite")]),
            _catalog_gate(3, [_catalog("pass", "python-crap")]),
            _catalog_gate(4, [_catalog("pass", "codex-probe")]),
            _gate(5, started=True, startedCount=1, decision="fail"),
        ],
        leg=ReplayLegIdentity(role="baseline", freezeDigest=_DIGEST),
    )
    treatment = _run_measurement(
        gates=[
            _gate(1, decision="pass-reused"),
            _gate(2, decision="pass-reused"),
            _gate(3, decision="pass-reused"),
            _gate(4, decision="pass-reused"),
            _gate(5, started=True, startedCount=1, decision="pass-published"),
        ]
    )
    evidence = _evidence(treatment, baseline=baseline, changeClasses=("memory-onboarding",))
    assert evaluate_replay_scenario("r17-scenario-09", evidence).state == "green"
    assert (
        evaluate_replay_scenario("r17-scenario-09", _evidence(treatment)).state == "not-applicable"
    )


def test_scenario_10_code_change_invalidates_every_gate() -> None:
    treatment = _run_measurement(
        gates=[
            _gate(1, invalidated=True),
            _gate(2, invalidated=True),
            _gate(3, invalidated=True),
            _gate(4, invalidated=True),
            _gate(5, invalidated=True),
        ],
        certificateInvalidationCount=5,
    )
    evidence = _evidence(treatment, changeClasses=("code",))
    assert evaluate_replay_scenario("r17-scenario-10", evidence).state == "green"
    partial = _run_measurement(
        gates=[
            _gate(1, invalidated=True),
            _gate(2),
            _gate(3),
            _gate(4),
            _gate(5),
        ],
        certificateInvalidationCount=1,
    )
    assert (
        evaluate_replay_scenario(
            "r17-scenario-10", _evidence(partial, changeClasses=("code",))
        ).state
        == "red"
    )


def test_scenario_16_repository_generic_profiles_share_contract() -> None:
    treatment = _run_measurement()
    fixture_a = ReplayProfileSnapshot(
        repositoryId="fixture-node",
        toolIdentity="node-playwright",
        frameworkContract="framework-contract/v1",
        placements=(
            ReplayRailPlacement(gate=1, rail=_rail("node-lint"), railClass="pre-test-quality"),
            ReplayRailPlacement(gate=2, rail=_rail("node-suite"), railClass="ordinary-test-suite"),
            ReplayRailPlacement(gate=3, rail=_rail("node-coverage"), railClass="post-test-quality"),
            ReplayRailPlacement(gate=4, rail=_rail("node-e2e"), railClass="integration-test"),
        ),
    )
    fixture_b = ReplayProfileSnapshot(
        repositoryId="fixture-rust",
        toolIdentity="rust-cargo",
        frameworkContract="framework-contract/v1",
        placements=(
            ReplayRailPlacement(gate=1, rail=_rail("rust-lint"), railClass="pre-test-quality"),
            ReplayRailPlacement(gate=2, rail=_rail("rust-suite"), railClass="ordinary-test-suite"),
            ReplayRailPlacement(gate=3, rail=_rail("rust-coverage"), railClass="post-test-quality"),
            ReplayRailPlacement(gate=4, rail=_rail("rust-e2e"), railClass="integration-test"),
        ),
    )
    evidence = _evidence(treatment, profiles=(fixture_a, fixture_b))
    assert evaluate_replay_scenario("r17-scenario-16", evidence).state == "green"
    # A fixture that leans on the Agents Remember reference is not generic proof.
    ar_profile = fixture_a.model_copy(
        update={"repositoryId": "agents-remember", "toolIdentity": "agents-remember"}
    )
    non_generic = _evidence(treatment, profiles=(ar_profile, fixture_b))
    assert evaluate_replay_scenario("r17-scenario-16", non_generic).state == "red"


def test_scenario_17_reference_profile_places_rails_by_class() -> None:
    treatment = _run_measurement()
    reference = ReplayProfileSnapshot(
        repositoryId="agents-remember",
        toolIdentity="agents-remember",
        frameworkContract="framework-contract/v1",
        placements=(
            ReplayRailPlacement(gate=1, rail=_rail("ruff"), railClass="pre-test-quality"),
            ReplayRailPlacement(
                gate=2, rail=_rail("python-suite"), railClass="ordinary-test-suite"
            ),
            ReplayRailPlacement(gate=3, rail=_rail("python-crap"), railClass="post-test-quality"),
            ReplayRailPlacement(gate=4, rail=_rail("codex-probe"), railClass="integration-test"),
            ReplayRailPlacement(gate=5, rail=_rail("memory-quality"), railClass="memory-quality"),
        ),
    )
    evidence = _evidence(treatment, profiles=(reference,))
    assert evaluate_replay_scenario("r17-scenario-17", evidence).state == "green"
    assert evaluate_replay_scenario("r17-scenario-17", _evidence(treatment)).state in {
        "not-applicable",
        "refused",
    }


def test_comparison_report_binds_identities_and_scenarios() -> None:
    baseline_freeze = _freeze()
    treatment_freeze = _freeze()

    comparability = compare_replay_freezes(baseline_freeze, treatment_freeze)
    population = compile_replay_population(
        [
            PopulationGeneration(
                generation=generation,
                stratum="frozen-original",
                sourceIdentity="frozen",
            )
            for generation in range(1, 9)
        ]
    )
    treatment = _run_measurement()
    baseline = _run_measurement(leg=ReplayLegIdentity(role="baseline", freezeDigest=_DIGEST))
    evidence = _evidence(treatment, baseline=baseline)
    inputs = ReplayComparisonInput(
        baselineFreeze=baseline_freeze,
        treatmentFreeze=treatment_freeze,
        comparability=comparability,
        baselineMeasurement=baseline,
        treatmentMeasurement=treatment,
        evidence=evidence,
        population=population,
        note="raw measurements only",
    )
    report = build_replay_comparison_report(inputs)
    assert isinstance(report, ReplayComparisonReport)
    assert report.comparability.comparable
    assert len(report.scenarioOutcomes) == 17
    assert report.baselineLegDigest == report.treatmentLegDigest
    assert report.reportDigest == report.reportDigest


def test_comparison_report_refuses_when_pair_incomparable() -> None:
    baseline_freeze = _freeze()
    treatment_freeze = _freeze(sourceRevision="ar/other@tip")

    comparability = compare_replay_freezes(baseline_freeze, treatment_freeze)
    assert not comparability.comparable
    treatment = _run_measurement()
    baseline = _run_measurement(leg=ReplayLegIdentity(role="baseline", freezeDigest=_DIGEST))
    evidence = _evidence(treatment, baseline=baseline)
    inputs = ReplayComparisonInput(
        baselineFreeze=baseline_freeze,
        treatmentFreeze=treatment_freeze,
        comparability=comparability,
        baselineMeasurement=baseline,
        treatmentMeasurement=treatment,
        evidence=evidence,
    )
    report = build_replay_comparison_report(inputs)
    assert not report.comparability.comparable
    assert len(report.comparability.changes) >= 1
