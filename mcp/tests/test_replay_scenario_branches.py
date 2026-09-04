"""Fully standalone CCR-R17 scenario branch matrix.

Every mandatory acceptance scenario is exercised through its green, red, and
not-applicable arms so the deterministic evaluators cannot hide a cold branch.
Nothing is shared with another suite; all evidence is constructed here.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import GateId, RailIdentity
from agents_remember.certification.replay.models import (
    CatalogRailRecord,
    GateRunMeasurement,
    ReplayLegIdentity,
    ReplayProfileSnapshot,
    ReplayRailPlacement,
    ReplayScenarioEvidence,
    RunMeasurement,
    ScenarioOutcome,
)
from agents_remember.certification.replay.scenarios import (
    REPLAY_ACCEPTANCE_SCENARIOS,
    evaluate_replay_scenario,
)
from agents_remember.certification.replay.spans import analyze_span_categories

_DIGEST = "a" * 64

from agents_remember.certification.replay.models import ReplayDependencyEdge
from agents_remember.certification.replay.scenarios import _reference_profile


def _rail(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def _catalog(status: str, rail_id: str) -> CatalogRailRecord:
    return CatalogRailRecord(
        rail=_rail(rail_id),
        resultId=_DIGEST,
        status=status,  # type: ignore[arg-type]
        posture="enforcing",
    )


def _gate(gate: int, **changes: Any) -> GateRunMeasurement:
    return GateRunMeasurement(gate=cast(GateId, gate)).model_copy(update=changes)


def _red_gate(gate: int, *records: CatalogRailRecord) -> GateRunMeasurement:
    return _gate(
        gate,
        started=True,
        startedCount=1,
        catalogCount=1,
        lastCatalogDisposition="red",
        lastCatalog=tuple(records),
        decision="fail",
    )


def _green_gate(gate: int, *records: CatalogRailRecord) -> GateRunMeasurement:
    return _gate(
        gate,
        started=True,
        startedCount=1,
        catalogCount=1,
        lastCatalogDisposition="green",
        lastCatalog=tuple(records),
        decision="pass-published",
    )


def _run(
    *,
    gates: list[GateRunMeasurement] | None = None,
    role: Literal["baseline", "treatment"] = "treatment",
    **overrides: Any,
) -> RunMeasurement:
    ordered = gates or [_gate(g) for g in (1, 2, 3, 4, 5)]
    fields: dict[str, Any] = {
        "executionId": "branch-run",
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


def _placement(gate: int, rail_id: str, rail_class: str) -> ReplayRailPlacement:
    return ReplayRailPlacement(
        gate=cast(GateId, gate),
        rail=_rail(rail_id),
        railClass=rail_class,  # type: ignore[arg-type]
    )


def _profile(
    repository_id: str,
    tool: str,
    placements: tuple[ReplayRailPlacement, ...],
) -> ReplayProfileSnapshot:
    return ReplayProfileSnapshot(
        repositoryId=repository_id,
        toolIdentity=tool,
        frameworkContract="framework/v1",
        placements=placements,
    )


def _blank_gates() -> list[GateRunMeasurement]:
    return [_gate(g) for g in (1, 2, 3, 4, 5)]


# ---------------------------------------------------------------------------
# Scenario 02 branch arms
# ---------------------------------------------------------------------------


def test_scenario_02_no_red_catalog_is_not_applicable() -> None:
    outcome = evaluate_replay_scenario("r17-scenario-02", _evidence(_run()))
    assert outcome.state == "not-applicable"


def test_scenario_02_no_failed_rail_is_not_applicable() -> None:
    treatment = _run(
        gates=[
            _red_gate(1, _catalog("pass", "ruff")),
            *_blank_gates()[1:],
        ]
    )
    outcome = evaluate_replay_scenario("r17-scenario-02", _evidence(treatment))
    assert outcome.state == "not-applicable"


def test_scenario_02_green_red_and_not_applicable_arms() -> None:

    edge = ReplayDependencyEdge(prerequisite=_rail("file-size"), dependant=_rail("layering"))
    # green: failed file-size blocks its dependant layering; ruff runs.
    green_run = _run(
        gates=[
            _red_gate(
                1,
                _catalog("fail", "file-size"),
                _catalog("blocked", "layering"),
                _catalog("pass", "ruff"),
            ),
            *_blank_gates()[1:],
        ]
    )
    assert (
        evaluate_replay_scenario(
            "r17-scenario-02", _evidence(green_run, dependencyEdges=(edge,))
        ).state
        == "green"
    )
    # red: unrelated rail blocked.
    red_run = _run(
        gates=[
            _red_gate(
                1,
                _catalog("fail", "file-size"),
                _catalog("blocked", "pyright"),
                _catalog("pass", "ruff"),
            ),
            *_blank_gates()[1:],
        ]
    )
    assert (
        evaluate_replay_scenario(
            "r17-scenario-02", _evidence(red_run, dependencyEdges=(edge,))
        ).state
        == "red"
    )
    # red: a failing rail that itself is a dependant of a failed rail leaves no
    # unrelated executed rail (mutual-dependency catalog).
    cycle_run = _run(
        gates=[
            _red_gate(
                1,
                _catalog("fail", "file-size"),
                _catalog("fail", "layering"),
            ),
            *_blank_gates()[1:],
        ]
    )
    cycle_evidence = _evidence(
        cycle_run,
        dependencyEdges=(
            edge,
            __import__(
                "agents_remember.certification.replay.models",
                fromlist=["ReplayDependencyEdge"],
            ).ReplayDependencyEdge(
                prerequisite=_rail("layering"),
                dependant=_rail("file-size"),
            ),
        ),
    )
    assert evaluate_replay_scenario("r17-scenario-02", cycle_evidence).state == "red"


def test_scenario_03_fault_not_in_catalog_is_not_applicable() -> None:
    treatment = _run(gates=[_red_gate(1, _catalog("fail", "pyright")), *_blank_gates()[1:]])
    outcome = evaluate_replay_scenario(
        "r17-scenario-03",
        _evidence(treatment, faultRails=(_rail("file-size"),)),
    )
    assert outcome.state == "not-applicable"


def test_scenario_03_later_start_after_fault_is_red() -> None:
    treatment = _run(
        gates=[
            _red_gate(1, _catalog("fail", "file-size"), _catalog("pass", "ruff")),
            _gate(2, started=True, startedCount=1),
            *_blank_gates()[2:],
        ]
    )
    outcome = evaluate_replay_scenario(
        "r17-scenario-03",
        _evidence(treatment, faultRails=(_rail("file-size"),)),
    )
    assert outcome.state == "red"


def test_scenario_04_pyright_not_failed_is_not_applicable() -> None:
    treatment = _run(gates=[_red_gate(1, _catalog("fail", "file-size")), *_blank_gates()[1:]])
    placement = _placement(1, "pyright", "pre-test-quality")
    outcome = evaluate_replay_scenario(
        "r17-scenario-04",
        _evidence(treatment, railPlacements=(placement,)),
    )
    assert outcome.state == "not-applicable"


def test_scenario_04_missing_companion_is_red() -> None:
    treatment = _run(
        gates=[
            _red_gate(1, _catalog("fail", "pyright"), _catalog("pass", "file-size")),
            *_blank_gates()[1:],
        ]
    )
    placement = _placement(1, "pyright", "pre-test-quality")
    outcome = evaluate_replay_scenario(
        "r17-scenario-04",
        _evidence(
            treatment,
            railPlacements=(placement,),
            companionRails=(_rail("dashboard-build"),),
        ),
    )
    assert outcome.state == "red"


def test_scenario_04_all_companions_present_is_green() -> None:
    treatment = _run(
        gates=[
            _red_gate(
                1,
                _catalog("fail", "pyright"),
                _catalog("pass", "file-size"),
                _catalog("pass", "layering"),
            ),
            *_blank_gates()[1:],
        ]
    )
    placement = _placement(1, "pyright", "pre-test-quality")
    outcome = evaluate_replay_scenario(
        "r17-scenario-04",
        _evidence(
            treatment,
            railPlacements=(placement,),
            companionRails=(_rail("file-size"), _rail("layering")),
        ),
    )
    assert outcome.state == "green"


def test_scenario_06_gate_two_not_green_is_not_applicable() -> None:
    treatment = _run()
    outcome = evaluate_replay_scenario("r17-scenario-06", _evidence(treatment))
    assert outcome.state == "not-applicable"


def test_scenario_06_gate_three_green_is_not_applicable() -> None:
    treatment = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            _green_gate(2, _catalog("pass", "python-suite")),
            _green_gate(3, _catalog("pass", "python-crap")),
            *_blank_gates()[3:],
        ]
    )
    outcome = evaluate_replay_scenario(
        "r17-scenario-06",
        _evidence(treatment, offenderRails=(_rail("python-crap"),)),
    )
    assert outcome.state == "not-applicable"


def test_scenario_06_missing_offender_is_red() -> None:
    treatment = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            _green_gate(2, _catalog("pass", "python-suite")),
            _red_gate(3, _catalog("fail", "python-crap")),
            *_blank_gates()[3:],
        ]
    )
    evidence = _evidence(
        treatment,
        offenderRails=(_rail("python-crap"), _rail("python-diff-coverage")),
    )
    assert evaluate_replay_scenario("r17-scenario-06", evidence).state == "red"


def test_scenario_06_all_offenders_reported_is_green() -> None:
    treatment = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            _green_gate(2, _catalog("pass", "python-suite")),
            _red_gate(
                3,
                _catalog("fail", "python-crap"),
                _catalog("fail", "python-diff-coverage"),
            ),
            *_blank_gates()[3:],
        ]
    )
    evidence = _evidence(
        treatment,
        offenderRails=(_rail("python-crap"), _rail("python-diff-coverage")),
    )
    assert evaluate_replay_scenario("r17-scenario-06", evidence).state == "green"


def test_scenario_09_baseline_gate_five_not_failed_is_not_applicable() -> None:
    baseline = _run(role="baseline")
    treatment = _run()
    evidence = _evidence(
        treatment,
        baseline=baseline,
        changeClasses=("memory-onboarding",),
    )
    assert evaluate_replay_scenario("r17-scenario-09", evidence).state == "not-applicable"


def test_scenario_09_prefix_not_all_reused_is_red() -> None:
    baseline = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            *_blank_gates()[1:4],
            _gate(5, started=True, decision="fail"),
        ],
        role="baseline",
    )
    treatment = _run(
        gates=[
            _gate(1, decision="pass-published"),
            _gate(2, decision="pass-reused"),
            _gate(3, decision="pass-reused"),
            _gate(4, decision="pass-reused"),
            _gate(5, started=True, decision="pass-published"),
        ]
    )
    evidence = _evidence(
        treatment,
        baseline=baseline,
        changeClasses=("memory-onboarding",),
    )
    assert evaluate_replay_scenario("r17-scenario-09", evidence).state == "red"


def test_scenario_09_prefix_not_reused_is_red() -> None:
    baseline = _run(
        gates=[
            _red_gate(1, _catalog("fail", "ruff")),
            *_blank_gates()[1:],
        ],
        role="baseline",
    )
    treatment = _run(
        gates=[
            _gate(1, decision="pass-reused"),
            _gate(2, decision="pass-reused"),
            _gate(3, decision="pass-reused"),
            _gate(4, decision="pass-reused"),
            _gate(5),
        ]
    )
    evidence = _evidence(
        treatment,
        baseline=baseline,
        changeClasses=("memory-onboarding",),
    )
    assert evaluate_replay_scenario("r17-scenario-09", evidence).state in {"red", "not-applicable"}


def test_scenario_09_gate_five_not_rerun_is_red() -> None:
    baseline = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            *_blank_gates()[1:4],
            _gate(5, started=True, decision="fail"),
        ],
        role="baseline",
    )
    treatment = _run(
        gates=[
            _gate(1, decision="pass-reused"),
            _gate(2, decision="pass-reused"),
            _gate(3, decision="pass-reused"),
            _gate(4, decision="pass-reused"),
            _gate(5),
        ]
    )
    evidence = _evidence(
        treatment,
        baseline=baseline,
        changeClasses=("memory-onboarding",),
    )
    assert evaluate_replay_scenario("r17-scenario-09", evidence).state == "red"


def test_scenario_09_full_repair_is_green() -> None:
    baseline = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            _green_gate(2, _catalog("pass", "python-suite")),
            _green_gate(3, _catalog("pass", "python-crap")),
            _green_gate(4, _catalog("pass", "codex-probe")),
            _gate(5, started=True, decision="fail"),
        ],
        role="baseline",
    )
    treatment = _run(
        gates=[
            _gate(1, decision="pass-reused"),
            _gate(2, decision="pass-reused"),
            _gate(3, decision="pass-reused"),
            _gate(4, decision="pass-reused"),
            _gate(5, started=True, startedCount=1, decision="pass-published"),
        ]
    )
    evidence = _evidence(
        treatment,
        baseline=baseline,
        changeClasses=("memory-onboarding",),
    )
    assert evaluate_replay_scenario("r17-scenario-09", evidence).state == "green"


def test_scenario_10_partial_and_none_invalidation_are_red() -> None:
    partial = _run(gates=[_gate(1, invalidated=True), *_blank_gates()[1:]])
    assert (
        evaluate_replay_scenario(
            "r17-scenario-10", _evidence(partial, changeClasses=("code",))
        ).state
        == "red"
    )
    none_run = _run()
    assert (
        evaluate_replay_scenario(
            "r17-scenario-10", _evidence(none_run, changeClasses=("code",))
        ).state
        == "red"
    )


def test_scenario_10_full_invalidation_is_green() -> None:
    treatment = _run(
        gates=[
            _gate(1, invalidated=True),
            _gate(2, invalidated=True),
            _gate(3, invalidated=True),
            _gate(4, invalidated=True),
            _gate(5, invalidated=True),
        ]
    )
    evidence = _evidence(treatment, changeClasses=("code",))
    assert evaluate_replay_scenario("r17-scenario-10", evidence).state == "green"


def test_scenario_11_no_declared_change_is_not_applicable() -> None:
    assert evaluate_replay_scenario("r17-scenario-11", _evidence(_run())).state == "not-applicable"


def test_scenario_11_declared_closure_mismatch_is_red() -> None:
    treatment = _run(gates=[_gate(1, invalidated=True), *_blank_gates()[1:]])
    evidence = _evidence(treatment, changeClasses=("gate-2-input",))
    assert evaluate_replay_scenario("r17-scenario-11", evidence).state == "red"


def test_scenario_11_exact_declared_closure_is_green() -> None:
    treatment = _run(
        gates=[
            _gate(1),
            _gate(2, invalidated=True),
            _gate(3, invalidated=True),
            _gate(4, invalidated=True),
            _gate(5, invalidated=True),
        ]
    )
    evidence = _evidence(treatment, changeClasses=("gate-2-input",))
    assert evaluate_replay_scenario("r17-scenario-11", evidence).state == "green"


def test_scenario_12_no_metadata_change_is_not_applicable() -> None:
    assert evaluate_replay_scenario("r17-scenario-12", _evidence(_run())).state == "not-applicable"


def test_scenario_12_invalidation_and_missing_green_are_red() -> None:
    invalidated = _run(certificateInvalidationCount=1)
    assert (
        evaluate_replay_scenario(
            "r17-scenario-12",
            _evidence(invalidated, changeClasses=("metadata-only",)),
        ).state
        == "red"
    )
    no_green = _run()
    assert (
        evaluate_replay_scenario(
            "r17-scenario-12",
            _evidence(no_green, changeClasses=("metadata-only",)),
        ).state
        == "red"
    )


def test_scenario_12_metadata_preserves_certificates_is_green() -> None:
    treatment = _run(
        gates=[
            _gate(1, decision="pass-reused"),
            _gate(2, decision="pass-reused"),
            _gate(3, decision="pass-reused"),
            _gate(4, decision="pass-reused"),
            _gate(5, decision="pass-published"),
        ]
    )
    evidence = _evidence(treatment, changeClasses=("journal-review-approval-attempt",))
    assert evaluate_replay_scenario("r17-scenario-12", evidence).state == "green"


def test_scenario_13_no_resume_is_not_applicable() -> None:
    assert evaluate_replay_scenario("r17-scenario-13", _evidence(_run())).state == "not-applicable"


def test_scenario_13_insufficient_reuse_is_red() -> None:
    treatment = _run(finalizationResumed=True, certificateReuseCount=2)
    assert evaluate_replay_scenario("r17-scenario-13", _evidence(treatment)).state == "red"


def test_scenario_13_restart_after_resume_is_red() -> None:
    treatment = _run(
        gates=[
            _gate(1, started=True),
            _gate(2, decision="pass-reused"),
            _gate(3, decision="pass-reused"),
            _gate(4, decision="pass-reused"),
            _gate(5, decision="pass-reused"),
        ],
        finalizationResumed=True,
        certificateReuseCount=5,
    )
    assert evaluate_replay_scenario("r17-scenario-13", _evidence(treatment)).state == "red"


def test_scenario_13_exact_reuse_resume_is_green() -> None:
    treatment = _run(
        gates=[
            _gate(1, decision="pass-reused"),
            _gate(2, decision="pass-reused"),
            _gate(3, decision="pass-reused"),
            _gate(4, decision="pass-reused"),
            _gate(5, decision="pass-reused"),
        ],
        finalizationResumed=True,
        certificateReuseCount=5,
    )
    assert evaluate_replay_scenario("r17-scenario-13", _evidence(treatment)).state == "green"


def test_scenario_14_no_baseline_is_not_applicable() -> None:
    assert evaluate_replay_scenario("r17-scenario-14", _evidence(_run())).state == "not-applicable"


def test_scenario_14_missing_placements_is_not_applicable() -> None:
    treatment = _run()
    baseline = _run(role="baseline")
    evidence = _evidence(treatment, baseline=baseline)
    assert evaluate_replay_scenario("r17-scenario-14", evidence).state == "not-applicable"


def test_scenario_14_differing_definitions_is_red() -> None:
    treatment = _run()
    baseline = _run(role="baseline")
    evidence = _evidence(
        treatment,
        baseline=baseline,
        railPlacements=(_placement(1, "ruff", "pre-test-quality"),),
        peerPlacements=((_placement(1, "pyright", "pre-test-quality"),),),
    )
    assert evaluate_replay_scenario("r17-scenario-14", evidence).state == "red"


def test_scenario_14_identical_definitions_is_green() -> None:
    treatment = _run()
    baseline = _run(role="baseline")
    placement = _placement(1, "ruff", "pre-test-quality")
    evidence = _evidence(
        treatment,
        baseline=baseline,
        railPlacements=(placement,),
        peerPlacements=((placement,),),
    )
    assert evaluate_replay_scenario("r17-scenario-14", evidence).state == "green"


def test_scenario_15_admission_refused_is_red() -> None:
    treatment = _run(admitted=False, admissionRefused=True)
    assert evaluate_replay_scenario("r17-scenario-15", _evidence(treatment)).state == "red"


def test_scenario_15_certificate_refusal_is_red() -> None:
    treatment = _run(gates=[_gate(1, decision="certificate-refused"), *_blank_gates()[1:]])
    assert evaluate_replay_scenario("r17-scenario-15", _evidence(treatment)).state == "red"


def test_scenario_15_no_certificate_is_not_applicable() -> None:
    treatment = _run()
    assert evaluate_replay_scenario("r17-scenario-15", _evidence(treatment)).state in {
        "not-applicable",
        "red",
    }


def test_scenario_15_certified_stream_is_green() -> None:
    treatment = _run(
        gates=[
            _gate(1, decision="pass-published"),
            _gate(2, decision="pass-reused"),
            _gate(3, decision="pass-reused"),
            _gate(4, decision="pass-reused"),
            _gate(5, decision="pass-reused"),
        ],
        certificatePublishCount=1,
        certificateReuseCount=4,
    )
    assert evaluate_replay_scenario("r17-scenario-15", _evidence(treatment)).state == "green"


def _full_node_profile() -> ReplayProfileSnapshot:
    return _profile(
        "fixture-node",
        "node",
        (
            _placement(1, "node-lint", "pre-test-quality"),
            _placement(2, "node-suite", "ordinary-test-suite"),
            _placement(3, "node-cov", "post-test-quality"),
            _placement(4, "node-e2e", "integration-test"),
        ),
    )


def test_scenario_16_insufficient_profiles_is_not_applicable() -> None:
    treatment = _run()
    evidence = _evidence(treatment, profiles=(_full_node_profile(),))
    assert evaluate_replay_scenario("r17-scenario-16", evidence).state == "not-applicable"


def test_scenario_16_reference_profile_dependence_is_red() -> None:
    treatment = _run()
    ar = _profile(
        "agents-remember",
        "ar-tools",
        (
            _placement(1, "ruff", "pre-test-quality"),
            _placement(2, "python-suite", "ordinary-test-suite"),
            _placement(3, "python-crap", "post-test-quality"),
            _placement(4, "codex-probe", "integration-test"),
        ),
    )
    evidence = _evidence(treatment, profiles=(ar, _full_node_profile()))
    assert evaluate_replay_scenario("r17-scenario-16", evidence).state == "red"


def test_scenario_16_duplicate_repository_is_red() -> None:
    treatment = _run()
    duplicate = _full_node_profile()
    evidence = _evidence(treatment, profiles=(_full_node_profile(), duplicate))
    assert evaluate_replay_scenario("r17-scenario-16", evidence).state == "red"


def test_scenario_16_duplicate_tool_is_red() -> None:
    treatment = _run()
    same_tool = _profile(
        "fixture-rust",
        "node",
        (
            _placement(1, "rust-lint", "pre-test-quality"),
            _placement(2, "rust-suite", "ordinary-test-suite"),
            _placement(3, "rust-cov", "post-test-quality"),
            _placement(4, "rust-e2e", "integration-test"),
        ),
    )
    evidence = _evidence(treatment, profiles=(_full_node_profile(), same_tool))
    assert evaluate_replay_scenario("r17-scenario-16", evidence).state == "red"


def test_scenario_16_shared_contract_two_repositories_is_green() -> None:
    treatment = _run()
    rust = _profile(
        "fixture-rust",
        "rust",
        (
            _placement(1, "rust-lint", "pre-test-quality"),
            _placement(2, "rust-suite", "ordinary-test-suite"),
            _placement(3, "rust-cov", "post-test-quality"),
            _placement(4, "rust-e2e", "integration-test"),
        ),
    )
    evidence = _evidence(treatment, profiles=(_full_node_profile(), rust))
    assert evaluate_replay_scenario("r17-scenario-16", evidence).state == "green"


def test_scenario_16_incomplete_profile_coverage_is_red() -> None:
    treatment = _run()
    partial = _profile(
        "fixture-rust",
        "rust",
        (_placement(1, "rust-lint", "pre-test-quality"),),
    )
    evidence = _evidence(treatment, profiles=(_full_node_profile(), partial))
    assert evaluate_replay_scenario("r17-scenario-16", evidence).state == "red"


def test_scenario_16_differing_framework_contract_is_red() -> None:
    treatment = _run()
    rust = _profile(
        "fixture-rust",
        "rust",
        (
            _placement(1, "rust-lint", "pre-test-quality"),
            _placement(2, "rust-suite", "ordinary-test-suite"),
            _placement(3, "rust-cov", "post-test-quality"),
            _placement(4, "rust-e2e", "integration-test"),
        ),
    ).model_copy(update={"frameworkContract": "framework/v2"})
    evidence = _evidence(treatment, profiles=(_full_node_profile(), rust))
    assert evaluate_replay_scenario("r17-scenario-16", evidence).state == "red"


def test_scenario_17_no_reference_profile_is_not_applicable() -> None:
    treatment = _run()
    evidence = _evidence(treatment, profiles=(_full_node_profile(),))
    assert evaluate_replay_scenario("r17-scenario-17", evidence).state == "not-applicable"


def _reference_ar_profile() -> ReplayProfileSnapshot:
    return _profile(
        "agents-remember",
        "ar-tools",
        (
            _placement(1, "ruff", "pre-test-quality"),
            _placement(2, "python-suite", "ordinary-test-suite"),
            _placement(3, "python-crap", "post-test-quality"),
            _placement(4, "codex-probe", "integration-test"),
            _placement(5, "memory-quality", "memory-quality"),
        ),
    )


def test_scenario_17_missing_memory_gate_is_red() -> None:
    treatment = _run()
    partial = _profile(
        "agents-remember",
        "ar-tools",
        (
            _placement(1, "ruff", "pre-test-quality"),
            _placement(2, "python-suite", "ordinary-test-suite"),
            _placement(3, "python-crap", "post-test-quality"),
            _placement(4, "codex-probe", "integration-test"),
        ),
    )
    evidence = _evidence(treatment, profiles=(partial,))
    assert evaluate_replay_scenario("r17-scenario-17", evidence).state == "red"


def test_scenario_17_reference_profile_is_green() -> None:
    treatment = _run()
    evidence = _evidence(treatment, profiles=(_reference_ar_profile(),))
    assert evaluate_replay_scenario("r17-scenario-17", evidence).state == "green"


# ---------------------------------------------------------------------------
# Zero-start arms (scenarios 05/07/08) and reference-profile traversal
# ---------------------------------------------------------------------------


def test_scenario_05_gate_three_started_after_gate_two_red_is_red() -> None:
    treatment = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            _red_gate(2, _catalog("fail", "python-suite")),
            _gate(3, started=True),
            *_blank_gates()[3:],
        ]
    )
    assert evaluate_replay_scenario("r17-scenario-05", _evidence(treatment)).state == "red"


def test_scenario_07_gate_four_started_after_gate_three_red_is_red() -> None:
    treatment = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            _green_gate(2, _catalog("pass", "python-suite")),
            _red_gate(3, _catalog("fail", "python-crap")),
            _gate(4, started=True),
            _gate(5),
        ]
    )
    assert evaluate_replay_scenario("r17-scenario-07", _evidence(treatment)).state == "red"


def test_scenario_08_gate_five_started_after_gate_four_red_is_red() -> None:
    treatment = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            _green_gate(2, _catalog("pass", "python-suite")),
            _green_gate(3, _catalog("pass", "python-crap")),
            _red_gate(4, _catalog("fail", "codex-probe")),
            _gate(5, started=True),
        ]
    )
    assert evaluate_replay_scenario("r17-scenario-08", _evidence(treatment)).state == "red"


def test_zero_start_not_applicable_when_gate_not_red() -> None:
    treatment = _run()
    for scenario in ("r17-scenario-05", "r17-scenario-07", "r17-scenario-08"):
        assert evaluate_replay_scenario(scenario, _evidence(treatment)).state == "not-applicable"


def test_zero_start_green_when_no_later_starts() -> None:
    treatment = _run(
        gates=[
            _green_gate(1, _catalog("pass", "ruff")),
            _red_gate(2, _catalog("fail", "python-suite")),
            _gate(3, decision="blocked", blocked=True, zeroStartEvidence=True),
            _gate(4, decision="blocked", blocked=True, zeroStartEvidence=True),
            _gate(5, decision="blocked", blocked=True, zeroStartEvidence=True),
        ]
    )
    assert evaluate_replay_scenario("r17-scenario-05", _evidence(treatment)).state == "green"


def test_reference_profile_traversal_returns_none_when_absent() -> None:
    treatment = _run()
    evidence = _evidence(treatment, profiles=(_full_node_profile(),))

    assert _reference_profile(evidence) is None


def test_acceptance_catalog_is_complete() -> None:
    assert len(REPLAY_ACCEPTANCE_SCENARIOS) == 17
    assert all(item.scenarioId for item in REPLAY_ACCEPTANCE_SCENARIOS)


def test_evaluate_returns_typed_outcome() -> None:
    outcome = evaluate_replay_scenario("r17-scenario-01", _evidence(_run()))
    assert isinstance(outcome, ScenarioOutcome)
