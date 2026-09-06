"""Focused contracts for the repository-neutral closeout rail registry."""

from __future__ import annotations

import pytest
from agents_remember.certification import (
    build_rail_result,
    canonicalize_registry,
    compile_certification_plan,
    validate_registry,
)
from agents_remember.certification.models import (
    RailArtifactResult,
    RailTerminalObservation,
    RegistryProfile,
)
from agents_remember.errors import CertificationContractError
from certification_registry_test_support import (
    _CANDIDATE,
    _DIGEST,
    ObservationSpec,
    RailSpec,
    _finding_codes,
    _gate,
    _identity,
    _manifest,
    _plan,
    _portable_specs,
    _registry,
    _result,
)


def test_non_agents_remember_registry_compiles_one_deterministic_plan_per_gate() -> None:
    registry = _registry()
    canonical = canonicalize_registry(registry)
    reversed_registry = registry.model_copy(
        update={
            "profiles": (registry.profiles[0].model_copy(update={"gates": (2, 4, 1, 3, 5)}),),
            "rails": tuple(reversed(registry.rails)),
        }
    )

    assert canonical.registry.repositoryId == "sample-repository"
    assert canonical.registryDigest == canonicalize_registry(reversed_registry).registryDigest
    plan = compile_certification_plan(
        canonical,
        profile_id="portable-ci",
        candidate_identity=_CANDIDATE,
    )
    assert tuple(item.gate for item in plan.gates) == (1, 2, 3, 4, 5)
    assert len({item.planDigest for item in plan.gates}) == 5
    assert tuple(item.gatePrerequisites for item in plan.gates) == (
        (),
        (1,),
        (1, 2),
        (1, 2, 3),
        (1, 2, 3, 4),
    )
    assert all(item.model_config.get("frozen") is True for item in plan.gates)


def test_canonicalization_deduplicates_identical_rails_but_rejects_conflicts() -> None:
    registry = _registry()
    identical = registry.model_copy(update={"rails": (*registry.rails, registry.rails[0])})
    canonical_identical = canonicalize_registry(identical)
    assert len(canonical_identical.registry.rails) == len(registry.rails)
    conflicting = registry.rails[0].model_copy(update={"orderKey": "conflicting-order"})
    canonical_conflict = canonicalize_registry(
        registry.model_copy(update={"rails": (*registry.rails, conflicting)})
    )
    report = validate_registry(canonical_conflict)
    assert "duplicate-rail-identity" in {item.code for item in report.findings}


def test_registry_validation_reports_independent_graph_and_classification_failures() -> None:
    specs = list(_portable_specs())
    rails = list(_registry().rails)
    rails[0] = rails[0].model_copy(update={"gate": 2, "prerequisites": (_identity("types"),)})
    rails[1] = rails[1].model_copy(update={"prerequisites": (_identity("package"),)})
    rails[2] = rails[2].model_copy(
        update={"prerequisites": (_identity("types"), _identity("lint"))}
    )
    coverage_index = next(i for i, spec in enumerate(specs) if spec.rail_id == "coverage")
    rails[coverage_index] = rails[coverage_index].model_copy(
        update={"requiredArtifacts": ("missing-suite-artifact",)}
    )
    e2e_index = next(i for i, spec in enumerate(specs) if spec.rail_id == "clean-room")
    rails.append(rails[e2e_index].model_copy(update={"orderKey": "conflicting-e2e"}))
    canonical = canonicalize_registry(_registry().model_copy(update={"rails": tuple(rails)}))

    codes = {item.code for item in validate_registry(canonical).findings}

    assert {
        "dependency-cycle",
        "duplicate-rail-identity",
        "later-gate-prerequisite",
        "post-test-artifact-missing",
        "undeclared-artifact",
        "wrong-gate-classification",
    } <= codes


def test_gate_manifest_keeps_failed_and_independent_siblings_and_blocks_only_dependant() -> None:
    plan = _plan()
    gate_plan = _gate(plan, 1)
    results = (
        _result(gate_plan, ObservationSpec("lint", status="fail")),
        _result(gate_plan, ObservationSpec("package", status="blocked", blocked_by=("lint",))),
        _result(gate_plan, ObservationSpec("types")),
    )

    manifest = _manifest(
        plan,
        gate_plan,
        results,
        altitude="certifying",
    )

    by_id = {result.rail.railId: result for result in manifest.railResults}
    assert manifest.disposition == "red"
    assert set(by_id) == {"lint", "package", "types"}
    assert by_id["types"].status == "pass"
    assert by_id["package"].status == "blocked"
    assert tuple(item.railId for item in by_id["package"].blockedBy) == ("lint",)


def test_passing_suite_requires_declared_artifacts_and_bounded_evidence() -> None:
    plan = _plan()
    gate_plan = _gate(plan, 2)
    missing_artifact = _result(
        gate_plan,
        ObservationSpec("suite", include_artifacts=False),
    )

    with pytest.raises(CertificationContractError) as missing:
        _manifest(
            plan,
            gate_plan,
            (missing_artifact,),
            altitude="certifying",
        )
    assert "required-result-artifact-missing" in _finding_codes(missing.value)

    oversize = _result(gate_plan, ObservationSpec("suite", evidence_size=129))
    with pytest.raises(CertificationContractError) as bounded:
        _manifest(plan, gate_plan, (oversize,), altitude="certifying")
    assert "result-evidence-oversize" in _finding_codes(bounded.value)


def test_diagnostic_result_cannot_be_promoted_to_certifying_altitude() -> None:
    profile = RegistryProfile(profileId="diagnostic", kind="diagnostic", gates=(1,))
    specs = (
        RailSpec("report", 1, profile_id="diagnostic", posture="report-only"),
        RailSpec(
            "optional",
            1,
            profile_id="diagnostic",
            applicability="not-applicable",
        ),
    )
    registry = _registry(specs, profile=profile)
    plan = _plan(registry)
    gate_plan = _gate(plan, 1)
    results = (
        _result(gate_plan, ObservationSpec("optional", status="not-applicable")),
        _result(gate_plan, ObservationSpec("report", status="fail")),
    )

    diagnostic = _manifest(
        plan,
        gate_plan,
        results,
        altitude="diagnostic",
        registry=registry,
    )
    assert diagnostic.disposition == "green"

    with pytest.raises(CertificationContractError) as caught:
        _manifest(
            plan,
            gate_plan,
            results,
            altitude="certifying",
            registry=registry,
        )
    assert "diagnostic-promotion" in _finding_codes(caught.value)


def test_result_refuses_evidence_and_artifacts_outside_the_plan_contract() -> None:
    plan = _plan()
    gate_plan = _gate(plan, 2)
    unexpected_evidence = _result(
        gate_plan,
        ObservationSpec("suite", evidence_id="foreign-evidence"),
    )
    with pytest.raises(CertificationContractError) as evidence_error:
        _manifest(
            plan,
            gate_plan,
            (unexpected_evidence,),
            altitude="certifying",
        )
    assert {
        "result-evidence-missing",
        "undeclared-result-evidence",
    } <= _finding_codes(evidence_error.value)

    ordinary = _result(gate_plan, ObservationSpec("suite"))
    forged_observation = RailTerminalObservation(
        rail=ordinary.rail,
        status="pass",
        code="suite-pass",
        artifacts=(
            *ordinary.artifacts,
            RailArtifactResult(
                artifactId="foreign-artifact",
                sha256=_DIGEST,
                size=1,
                evidenceRef="artifact://foreign",
            ),
        ),
        evidence=ordinary.evidence,
    )
    forged = build_rail_result(gate_plan, forged_observation)
    with pytest.raises(CertificationContractError) as artifact_error:
        _manifest(plan, gate_plan, (forged,), altitude="certifying")
    assert "undeclared-result-artifact" in _finding_codes(artifact_error.value)


def test_two_independent_failures_remain_visible_with_only_the_dependant_blocked() -> None:
    plan = _plan()
    gate_plan = _gate(plan, 1)
    results = (
        _result(gate_plan, ObservationSpec("lint", status="fail")),
        _result(gate_plan, ObservationSpec("package", status="blocked", blocked_by=("lint",))),
        _result(gate_plan, ObservationSpec("types", status="fail")),
    )

    manifest = _manifest(
        plan,
        gate_plan,
        results,
        altitude="certifying",
    )

    by_id = {result.rail.railId: result for result in manifest.railResults}
    assert manifest.disposition == "red"
    assert {identity for identity, result in by_id.items() if result.status == "fail"} == {
        "lint",
        "types",
    }
    assert by_id["package"].status == "blocked"


def test_report_only_result_cannot_turn_an_enforcing_failure_green() -> None:
    profile = RegistryProfile(profileId="diagnostic", kind="diagnostic", gates=(1,))
    specs = (
        RailSpec("enforcing", 1, profile_id="diagnostic"),
        RailSpec("report", 1, profile_id="diagnostic", posture="report-only"),
    )
    registry = _registry(specs, profile=profile)
    plan = _plan(registry)
    gate_plan = _gate(plan, 1)
    manifest = _manifest(
        plan,
        gate_plan,
        (
            _result(gate_plan, ObservationSpec("enforcing", status="fail")),
            _result(gate_plan, ObservationSpec("report", status="pass")),
        ),
        altitude="diagnostic",
        registry=registry,
    )

    assert manifest.disposition == "red"
    assert {result.rail.railId for result in manifest.railResults} == {
        "enforcing",
        "report",
    }
