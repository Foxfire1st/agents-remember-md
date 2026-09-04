"""Fully standalone CCR-R17 model, freeze, reducer, and report edge tests.

These tests drive the defensive branches that scenario tests leave cold: model
validators that refuse malformed records, freeze comparability refusals, the
reducer fold and refusal paths, and digest self-verification refusals.  All
fixtures are constructed here; nothing is shared with another suite.
"""

from __future__ import annotations

from typing import Any, Literal, cast

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CertificationContractFinding,
    GateId,
    RailIdentity,
)
from agents_remember.certification.replay.compare import (
    ReplayComparisonInput,
    ReplayComparisonReport,
)
from agents_remember.certification.replay.freeze import (
    ReplayComparabilityReport,
    ReplayFreeze,
    ReplayFreezeInput,
    ReplayFreezeInputChange,
    ReplayPopulation,
    compare_replay_freezes,
    compile_replay_freeze,
    compile_replay_population,
    dated_supplement_rows,
    frozen_generation_rows,
    require_append_only_population,
    tail_generation_rows,
)
from agents_remember.certification.replay.measure import measure_replay_run
from agents_remember.certification.replay.models import (
    CatalogRailRecord,
    GateRunMeasurement,
    PopulationGeneration,
    ReplayLegIdentity,
    ReplayProfileSnapshot,
    ReplayRailPlacement,
    ReplayScenarioEvidence,
    ReplayScenarioExpectation,
    RunMeasurement,
    ScenarioOutcome,
    SpanCategoryTotals,
    SpanReduction,
)
from agents_remember.certification.replay.scenarios import evaluate_all_replay_scenarios
from agents_remember.certification.replay.spans import analyze_span_categories
from agents_remember.certification.telemetry.models import (
    CatalogCounts,
    TelemetryEvent,
    TelemetrySpan,
)
from agents_remember.errors import CertificationContractError

_DIGEST = "a" * 64
_GIT = "c" * 40

_ALL_CATEGORIES: tuple[str, ...] = (
    "dagger-environment-setup",
    "test-execution",
    "post-test-scoring",
    "clean-room-api-provider",
    "memory-work",
    "waiting",
    "repair",
    "operator-attention",
    "finalization",
)


def _rail(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def _catalog_record(rail_id: str, status: str) -> CatalogRailRecord:
    return CatalogRailRecord(
        rail=_rail(rail_id),
        resultId=_DIGEST,
        status=status,  # type: ignore[arg-type]
        posture="enforcing",
    )


def _category_totals(category: str, span_count: int = 0) -> SpanCategoryTotals:
    return SpanCategoryTotals(
        category=category,  # type: ignore[arg-type]
        wallMillis=0,
        activeMillis=0,
        spanCount=span_count,
    )


def _span_reduction_payload(
    categories: tuple[SpanCategoryTotals, ...],
    *,
    gross_wall: int = 0,
    gross_active: int = 0,
    span_count: int = 0,
) -> dict[str, Any]:
    return {
        "categories": [item.model_dump(mode="json") for item in categories],
        "grossWallMillis": gross_wall,
        "grossActiveMillis": gross_active,
        "spanCount": span_count,
    }


def _freeze(**overrides: Any) -> ReplayFreeze:
    base: dict[str, Any] = {
        "sourceRevision": "ar/replay@tip",
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


def _gate(gate: int, **changes: Any) -> GateRunMeasurement:
    return GateRunMeasurement(gate=cast(GateId, gate)).model_copy(update=changes)


def _run(
    *,
    gates: list[GateRunMeasurement] | None = None,
    role: Literal["baseline", "treatment"] = "treatment",
    **overrides: Any,
) -> RunMeasurement:
    ordered = gates or [_gate(g) for g in (1, 2, 3, 4, 5)]
    fields: dict[str, Any] = {
        "executionId": "edge-run",
        "leg": ReplayLegIdentity(role=role, freezeDigest=_DIGEST),
        "gates": tuple(ordered),
        "spans": analyze_span_categories([]),
        "admitted": True,
        "admissionRefused": False,
        "finalizationStarted": False,
        "finalizationResumed": False,
        "finalizationCompleted": False,
        "operationTerminalClass": None,
        "certificatePublishCount": 0,
        "certificateReuseCount": 0,
        "certificateInvalidationCount": 0,
    }
    fields.update(overrides)
    draft = RunMeasurement.model_construct(measurementDigest="", **fields)
    payload = draft.model_dump(mode="json", exclude={"measurementDigest"})
    digest = content_digest(payload)
    return RunMeasurement.model_validate({**payload, "measurementDigest": digest})


def _evidence(treatment: RunMeasurement, **overrides: Any) -> ReplayScenarioEvidence:
    base: dict[str, Any] = {"treatment": treatment}
    base.update(overrides)
    return ReplayScenarioEvidence(**base)  # type: ignore[arg-type]


def test_semantic_text_refuses_blank_or_padded_values() -> None:
    for bad in ("", "  ", " padded "):
        with pytest.raises(ValueError):
            ReplayScenarioExpectation(
                scenarioId=bad,  # type: ignore[arg-type]
                title="a title",
                requirement="some requirement",
            )
    ok = ReplayScenarioExpectation(
        scenarioId="r17-scenario-01",
        title="a title",
        requirement="some requirement",
    )
    assert ok.scenarioId == "r17-scenario-01"


def test_scenario_outcome_refuses_green_with_findings() -> None:
    finding = CertificationContractFinding(
        code="spurious",
        path="outcome",
        detail="a green outcome cannot carry findings",
    )
    with pytest.raises(ValueError):
        ScenarioOutcome(
            scenarioId="r17-scenario-01",
            state="green",
            findings=(finding,),
        )


def test_scenario_outcome_refuses_non_green_without_findings() -> None:
    with pytest.raises(ValueError):
        ScenarioOutcome(scenarioId="r17-scenario-01", state="red", findings=())


def test_span_category_totals_refuse_active_above_wall() -> None:
    with pytest.raises(ValueError):
        SpanCategoryTotals(
            category="waiting",
            wallMillis=10,
            activeMillis=11,
            spanCount=1,
        )


def test_span_reduction_refuses_duplicate_categories() -> None:
    categories = tuple(_category_totals("waiting") for _ in range(9))
    with pytest.raises(ValueError):
        SpanReduction(
            categories=categories,
            grossWallMillis=0,
            grossActiveMillis=0,
            spanCount=0,
            reductionDigest=content_digest(_span_reduction_payload(categories)),
        )


def test_span_reduction_refuses_wrong_span_count() -> None:
    categories = tuple(_category_totals(category, span_count=1) for category in _ALL_CATEGORIES)
    with pytest.raises(ValueError):
        SpanReduction(
            categories=categories,
            grossWallMillis=0,
            grossActiveMillis=0,
            spanCount=0,
            reductionDigest=content_digest(_span_reduction_payload(categories, span_count=9)),
        )


def test_span_reduction_refuses_tampered_digest() -> None:
    reduction = analyze_span_categories(
        [
            TelemetrySpan(
                spanKind="waiting",
                startedAt="2026-09-03T00:00:00+02:00",
                startedEpochMillis=0,
                wallMillis=5,
                activeMillis=2,
            )
        ]
    )
    payload = reduction.model_dump(mode="json", exclude={"reductionDigest"})
    with pytest.raises(ValueError):
        SpanReduction.model_validate({**payload, "reductionDigest": "f" * 64})


def test_gate_measurement_refuses_started_count_without_start() -> None:
    with pytest.raises(ValueError):
        GateRunMeasurement(gate=1, started=False, startedCount=1)


def test_gate_measurement_refuses_blocked_and_started_together() -> None:
    with pytest.raises(ValueError):
        GateRunMeasurement(gate=1, started=True, blocked=True, decision="blocked")


def test_gate_measurement_refuses_zero_start_evidence_when_started() -> None:
    with pytest.raises(ValueError):
        GateRunMeasurement(gate=1, started=True, zeroStartEvidence=True)


def test_gate_measurement_refuses_counts_without_disposition() -> None:
    with pytest.raises(ValueError):
        GateRunMeasurement(
            gate=1,
            lastCatalogDisposition="none",
            lastCatalogCounts=CatalogCounts(
                passed=0,
                failed=0,
                blocked=0,
                notApplicable=0,
                reportOnly=0,
            ),
        )


def test_gate_measurement_refuses_catalog_without_disposition() -> None:
    with pytest.raises(ValueError):
        GateRunMeasurement(
            gate=1,
            lastCatalogDisposition="none",
            lastCatalog=(_catalog_record("ruff", "pass"),),
        )


def test_gate_measurement_rail_properties_partition_catalog() -> None:
    measured = GateRunMeasurement(
        gate=1,
        started=True,
        lastCatalogDisposition="red",
        lastCatalogCounts=None,
        lastCatalog=(
            _catalog_record("pyright", "fail"),
            _catalog_record("file-size", "blocked"),
            _catalog_record("ruff", "pass"),
        ),
    )
    assert {rail.railId for rail in measured.failedRails} == {"pyright"}
    assert {rail.railId for rail in measured.blockedRails} == {"file-size"}
    assert {rail.railId for rail in measured.terminalRails} == {
        "pyright",
        "file-size",
        "ruff",
    }


def test_run_measurement_refuses_wrong_gate_order() -> None:
    gates = [_gate(1), _gate(2), _gate(3), _gate(4), _gate(4)]
    with pytest.raises(ValueError):
        _run(gates=gates)


def test_run_measurement_refuses_admitted_and_refused() -> None:
    with pytest.raises(ValueError):
        _run(admitted=True, admissionRefused=True)


def test_run_measurement_refuses_tampered_digest() -> None:
    run = _run()
    payload = run.model_dump(mode="json", exclude={"measurementDigest"})
    with pytest.raises(ValueError):
        RunMeasurement.model_validate({**payload, "measurementDigest": "f" * 64})


def test_rail_placement_refuses_class_gate_mismatch() -> None:
    with pytest.raises(ValueError):
        ReplayRailPlacement(
            gate=3,
            rail=_rail("python-crap"),
            railClass="ordinary-test-suite",
        )


def test_evidence_placement_helpers_filter_by_gate_and_rail() -> None:
    treatment = _run()
    placement = ReplayRailPlacement(
        gate=2,
        rail=_rail("python-suite"),
        railClass="ordinary-test-suite",
    )
    peer = ReplayRailPlacement(
        gate=1,
        rail=_rail("node-lint"),
        railClass="pre-test-quality",
    )
    evidence = _evidence(
        treatment,
        railPlacements=(placement,),
        peerPlacements=((peer,),),
    )
    assert evidence.placements_for_gate(2) == (placement,)
    assert evidence.placements_for_gate(1) == ()
    assert evidence.placements_for_rail(_rail("python-suite")) == (placement,)
    assert evidence.placements_for_rail(_rail("none")) == ()
    assert evidence.baseline_placements() == (peer,)
    assert _evidence(treatment).baseline_placements() == ()


def test_profile_snapshot_placements_for_gate() -> None:
    profile = ReplayProfileSnapshot(
        repositoryId="fixture-node",
        toolIdentity="node",
        frameworkContract="framework/v1",
        placements=(
            ReplayRailPlacement(
                gate=1,
                rail=_rail("node-lint"),
                railClass="pre-test-quality",
            ),
        ),
    )
    assert len(profile.placements_for_gate(1)) == 1
    assert profile.placements_for_gate(2) == ()


def test_comparability_report_refuses_comparable_with_change() -> None:
    change = ReplayFreezeInputChange(
        changeClass="source",
        reason="changed",
    )
    with pytest.raises(ValueError):
        ReplayComparabilityReport(
            baselineFreezeDigest=_DIGEST,
            treatmentFreezeDigest=_DIGEST,
            comparable=True,
            changes=(change,),
        )


def test_comparability_report_refuses_incomparable_without_change() -> None:
    with pytest.raises(ValueError):
        ReplayComparabilityReport(
            baselineFreezeDigest=_DIGEST,
            treatmentFreezeDigest="b" * 64,
            comparable=False,
            changes=(),
            findings=(),
        )


def test_candidate_digest_change_is_a_source_change() -> None:
    baseline = _freeze()
    treatment = _freeze(candidateDigest="b" * 64)
    report = compare_replay_freezes(baseline, treatment)
    assert not report.comparable
    assert any(change.changeClass == "source" for change in report.changes)


def _population_payload(rows: tuple[PopulationGeneration, ...]) -> dict[str, Any]:
    return {
        "schemaVersion": "measured-replay-population/v1",
        "generations": [row.model_dump(mode="json") for row in rows],
    }


def test_population_refuses_duplicate_generations() -> None:
    rows = (
        PopulationGeneration(generation=1, stratum="frozen-original", sourceIdentity="row"),
        PopulationGeneration(generation=1, stratum="frozen-original", sourceIdentity="row"),
    )
    with pytest.raises(ValueError):
        ReplayPopulation.model_validate(
            {
                **_population_payload(rows),
                "populationDigest": content_digest(_population_payload(rows)),
            }
        )


def test_population_refuses_tampered_digest() -> None:
    rows = (PopulationGeneration(generation=1, stratum="frozen-original", sourceIdentity="row"),)
    with pytest.raises(ValueError):
        ReplayPopulation.model_validate(
            {
                **_population_payload(rows),
                "populationDigest": "f" * 64,
            }
        )


def test_population_stratum_helpers_route_by_stratum() -> None:
    population = compile_replay_population(
        [
            PopulationGeneration(generation=1, stratum="frozen-original", sourceIdentity="f"),
            PopulationGeneration(generation=9, stratum="post-analysis-tail", sourceIdentity="t"),
            PopulationGeneration(generation=14, stratum="dated-supplement", sourceIdentity="d"),
        ]
    )
    assert [row.generation for row in population.stratum("frozen-original")] == [1]
    assert [row.generation for row in frozen_generation_rows(population)] == [1]
    assert [row.generation for row in tail_generation_rows(population)] == [9]
    assert [row.generation for row in dated_supplement_rows(population)] == [14]
    assert population.stratum("incident-baseline") == ()


def test_append_only_refuses_new_non_supplement_row() -> None:
    base = compile_replay_population(
        [PopulationGeneration(generation=1, stratum="frozen-original", sourceIdentity="row")]
    )
    successor = compile_replay_population(
        [
            PopulationGeneration(generation=1, stratum="frozen-original", sourceIdentity="row"),
            PopulationGeneration(generation=2, stratum="frozen-original", sourceIdentity="row"),
        ]
    )
    with pytest.raises(CertificationContractError):
        require_append_only_population(base, successor)


def test_comparison_report_refuses_tampered_digest() -> None:
    baseline = _freeze()
    treatment = _freeze()
    comparability = compare_replay_freezes(baseline, treatment)
    run = _run()
    evidence = _evidence(run)
    outcomes = evaluate_all_replay_scenarios(evidence)
    inputs = ReplayComparisonInput(
        baselineFreeze=baseline,
        treatmentFreeze=treatment,
        comparability=comparability,
        baselineMeasurement=run,
        treatmentMeasurement=run,
        evidence=evidence,
    )
    payload = ReplayComparisonReport.model_construct(
        baselineFreeze=inputs.baselineFreeze,
        treatmentFreeze=inputs.treatmentFreeze,
        comparability=inputs.comparability,
        population=inputs.population,
        baselineLegDigest=run.leg.freezeDigest,
        treatmentLegDigest=run.leg.freezeDigest,
        scenarioOutcomes=outcomes,
        note=inputs.note,
        reportDigest="",
    ).model_dump(mode="json")
    with pytest.raises(ValueError):
        ReplayComparisonReport.model_validate({**payload, "reportDigest": "f" * 64})


def _raw_event(revision: int, kind: str, **fields: Any) -> Any:
    base: dict[str, Any] = {
        "schemaVersion": "closeout-telemetry-event/v1",
        "executionKind": "closeout-generation",
        "executionId": "edge-run",
        "eventRevision": revision,
        "operationKind": "closeout",
        "generation": 13,
        "diagnosticNonce": None,
        "eventKind": kind,
        "occurredAt": "2026-09-03T00:00:00+02:00",
        "candidate": {"kind": "git-tree", "value": _GIT},
        "profileId": "portable-ci",
        "gatePlanDigest": _DIGEST,
        "gate": fields.pop("gate", None),
        "rail": None,
        "runtime": None,
        "evidence": (),
        "spans": (),
        "certificateDisposition": fields.pop("certificateDisposition", "not-applicable"),
        "certificateId": fields.pop("certificateId", None),
        "gateResultManifestId": fields.pop("gateResultManifestId", None),
        "message": None,
        **fields,
    }
    return TelemetryEvent.model_construct(**base)


def _reducer_leg() -> ReplayLegIdentity:
    return ReplayLegIdentity(role="treatment", freezeDigest=_DIGEST)


def test_reducer_refuses_empty_export() -> None:
    with pytest.raises(CertificationContractError):
        measure_replay_run([], leg=_reducer_leg())


def test_reducer_requires_gate_identity_on_gate_events() -> None:
    event = _raw_event(1, "gate-started", gate=None)
    with pytest.raises(CertificationContractError):
        measure_replay_run([event], leg=_reducer_leg())


def test_reducer_refuses_diagnostic_envelope() -> None:
    event = _raw_event(1, "diagnostic-started", executionKind="diagnostic-run")
    with pytest.raises(CertificationContractError):
        measure_replay_run([event], leg=_reducer_leg())
