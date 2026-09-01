"""Focused contracts for the repository-neutral closeout rail registry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import agents_remember.certification.validation as certification_validation
import pytest
from agents_remember.certification import (
    build_rail_result,
    canonicalize_registry,
    compile_certification_plan,
    validate_registry,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CandidateIdentity,
    RailAdapterDefinition,
    RailApplicability,
    RailArtifactResult,
    RailDefinition,
    RailEvidenceContract,
    RailEvidenceReference,
    RailIdentity,
    RailRegistry,
    RailResult,
    RailRuntimeInputs,
    RailTerminalObservation,
    RegistryProfile,
    RegistryValidationFinding,
)
from agents_remember.errors import CertificationContractError
from certification_registry_test_support import (
    _CANDIDATE,
    _DIGEST,
    ObservationSpec,
    RailSpec,
    _finding_codes,
    _gate,
    _gate_one_graph_registry,
    _identity,
    _manifest,
    _plan,
    _portable_specs,
    _rail,
    _rebuild_certification_plan,
    _rebuild_gate_plan,
    _registry,
    _result,
)
from pydantic import ValidationError


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


def test_candidate_identity_does_not_assume_one_repository_hash_algorithm() -> None:
    canonical = canonicalize_registry(_registry())
    git_tree = CandidateIdentity(kind="git-tree", value="d" * 40)

    plan = compile_certification_plan(
        canonical,
        profile_id="portable-ci",
        candidate_identity=git_tree,
    )

    assert plan.candidateIdentity == git_tree
    assert all(gate.candidateIdentity == git_tree for gate in plan.gates)


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


@pytest.mark.parametrize(
    "version",
    [
        "0.0.0",
        "1.2.3-alpha.1",
        "1.2.3-0.3.7",
        "1.2.3-x.7.z.92",
        "1.2.3+build.5",
        "1.2.3-alpha.1+001",
        f"1.2.3+{'a' * 256}",
    ],
)
def test_rail_identity_accepts_complete_semver_2_versions(version: str) -> None:
    assert RailIdentity(railId="portable", version=version).version == version


@pytest.mark.parametrize(
    "version",
    [
        "01.0.0",
        "1.01.0",
        "1.0.01",
        "1.0",
        "1.0.0-",
        "1.0.0+",
        "1.0.0-01",
        "1.0.0-..",
        "1.0.0-alpha.",
        "1.0.0-alpha..1",
        "1.0.0+build..1",
        "1.0.0+build_1",
        "v1.0.0",
    ],
)
def test_rail_identity_rejects_invalid_semver_without_normalizing(version: str) -> None:
    with pytest.raises(ValidationError):
        RailIdentity(railId="portable", version=version)


def test_conflicting_rail_and_profile_variants_keep_all_inner_findings() -> None:
    registry = _registry()
    valid_rail = registry.rails[0]
    invalid_rail = valid_rail.model_copy(
        update={
            "gate": 2,
            "railClass": "pre-test-quality",
            "authority": "memory-domain",
            "prerequisites": (_identity("missing-prerequisite"),),
            "runtimeInputs": RailRuntimeInputs(),
            "outputArtifacts": (),
        }
    )
    valid_profile = registry.profiles[0]
    invalid_profile = RegistryProfile(
        profileId=valid_profile.profileId,
        kind="certifying",
        gates=(1,),
    )
    canonical = canonicalize_registry(
        registry.model_copy(
            update={
                "profiles": (valid_profile, invalid_profile),
                "rails": (*registry.rails, invalid_rail),
            }
        )
    )

    report = validate_registry(canonical)
    codes = {item.code for item in report.findings}

    assert {
        "duplicate-profile-identity",
        "duplicate-rail-identity",
        "incomplete-certifying-profile",
        "missing-prerequisite",
        "runtime-inputs-missing",
        "suite-artifact-missing",
        "wrong-gate-authority",
        "wrong-gate-classification",
    } <= codes
    rail_variant_findings = {
        item.code: item.path
        for item in report.findings
        if item.code
        in {
            "missing-prerequisite",
            "runtime-inputs-missing",
            "suite-artifact-missing",
            "wrong-gate-authority",
            "wrong-gate-classification",
        }
    }
    assert all("rails.lint@1.0.0.definitions." in path for path in rail_variant_findings.values())
    profile_finding = next(
        item for item in report.findings if item.code == "incomplete-certifying-profile"
    )
    assert "profiles.portable-ci.definitions." in profile_finding.path


@pytest.mark.parametrize("invalid_first", [False, True])
def test_conflicting_consumer_variants_keep_exact_artifact_findings(
    invalid_first: bool,
) -> None:
    producer = _rail(RailSpec("producer", 1, output_artifacts=("artifact",)))
    valid = _rail(
        RailSpec(
            "consumer",
            1,
            prerequisites=("producer",),
            required_artifacts=("artifact",),
        )
    )
    invalid = valid.model_copy(
        update={
            "orderKey": "consumer-without-prerequisite",
            "prerequisites": (),
        }
    )
    consumers = (invalid, valid) if invalid_first else (valid, invalid)
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    registry = RailRegistry(
        registryId="conflicting-consumers",
        repositoryId="sample-repository",
        profiles=(profile,),
        rails=(producer, *consumers),
    )

    report = validate_registry(canonicalize_registry(registry))
    duplicate = [item for item in report.findings if item.code == "duplicate-rail-identity"]
    artifact = [item for item in report.findings if item.code == "artifact-prerequisite-missing"]

    assert len(duplicate) == 1
    assert len(artifact) == 1
    assert f"rails.consumer@1.0.0.definitions.{content_digest(invalid)}" in artifact[0].path


def test_canonicalization_deduplicates_identical_profile_declarations() -> None:
    registry = _registry()
    canonical = canonicalize_registry(
        registry.model_copy(update={"profiles": (*registry.profiles, registry.profiles[0])})
    )
    assert len(canonical.registry.profiles) == 1


@pytest.mark.parametrize("gate_prerequisites", [(), (1,)])
def test_gate_plan_rejects_recomputed_digest_with_missing_gate_barriers(
    gate_prerequisites: tuple[int, ...],
) -> None:
    gate_plan = _gate(_plan(), 3)
    with pytest.raises(ValidationError):
        _rebuild_gate_plan(gate_plan, gatePrerequisites=gate_prerequisites)


def test_gate_plan_rejects_recomputed_digest_with_delayed_or_empty_waves() -> None:
    gate_plan = _gate(_plan(), 1)
    first_wave = gate_plan.waves[0]
    assert len(first_wave) == 2
    delayed = (
        (first_wave[0],),
        (first_wave[1],),
        *gate_plan.waves[1:],
    )
    empty_leading = ((), *gate_plan.waves)

    for waves in (delayed, empty_leading):
        serialized = [[identity.model_dump(mode="json") for identity in wave] for wave in waves]
        with pytest.raises(ValidationError):
            _rebuild_gate_plan(gate_plan, waves=serialized)


def test_gate_plan_rejects_recomputed_digest_with_reordered_rail_catalog() -> None:
    gate_plan = _gate(_plan(), 1)
    reversed_rails = [rail.model_dump(mode="json") for rail in reversed(gate_plan.rails)]
    with pytest.raises(ValidationError):
        _rebuild_gate_plan(gate_plan, rails=reversed_rails)


def test_recomputed_diagnostic_plan_cannot_remove_an_earlier_gate() -> None:
    profile = RegistryProfile(profileId="diagnostic", kind="diagnostic", gates=(1, 2))
    specs = (
        RailSpec("lint", 1, profile_id="diagnostic"),
        RailSpec(
            "suite",
            2,
            prerequisites=("lint",),
            output_artifacts=("suite-data",),
            profile_id="diagnostic",
        ),
    )
    registry = _registry(specs, profile=profile)
    plan = _plan(registry)

    with pytest.raises(ValidationError):
        _rebuild_certification_plan(plan, (_gate(plan, 2),))


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


def test_applicable_rail_cannot_depend_on_profile_inapplicable_prerequisite() -> None:
    specs = (
        RailSpec("optional", 1, applicability="not-applicable"),
        RailSpec("consumer", 1, prerequisites=("optional",)),
    )
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    report = validate_registry(canonicalize_registry(_registry(specs, profile=profile)))
    assert "inapplicable-prerequisite" in {item.code for item in report.findings}


def test_diagnostic_profile_cannot_skip_an_earlier_gate_barrier() -> None:
    profile = RegistryProfile(profileId="diagnostic", kind="diagnostic", gates=(3,))
    specs = (
        RailSpec(
            "coverage",
            3,
            profile_id="diagnostic",
            required_artifacts=("missing-suite-data",),
        ),
    )
    report = validate_registry(canonicalize_registry(_registry(specs, profile=profile)))
    assert "profile-gate-prefix-missing" in {item.code for item in report.findings}


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


def test_gate_manifest_refuses_result_omission() -> None:
    plan = _plan()
    gate_plan = _gate(plan, 1)
    results = (
        _result(gate_plan, ObservationSpec("lint")),
        _result(gate_plan, ObservationSpec("package")),
    )

    with pytest.raises(CertificationContractError) as caught:
        _manifest(plan, gate_plan, results, altitude="certifying")

    assert "rail-result-omitted" in _finding_codes(caught.value)


@pytest.mark.parametrize(
    ("observations", "expected_code"),
    [
        (
            (
                ObservationSpec("lint", status="fail"),
                ObservationSpec("package"),
                ObservationSpec("types"),
            ),
            "dependent-result-not-blocked",
        ),
        (
            (
                ObservationSpec("lint"),
                ObservationSpec("package"),
                ObservationSpec("types", status="blocked", blocked_by=("lint",)),
            ),
            "spurious-blocked-result",
        ),
    ],
)
def test_gate_manifest_refuses_incorrect_blocking(
    observations: tuple[ObservationSpec, ...],
    expected_code: str,
) -> None:
    plan = _plan()
    gate_plan = _gate(plan, 1)
    results = tuple(_result(gate_plan, observation) for observation in observations)

    with pytest.raises(CertificationContractError) as caught:
        _manifest(plan, gate_plan, results, altitude="certifying")

    assert expected_code in _finding_codes(caught.value)


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


@pytest.mark.parametrize("invalid", ["", " ", " padded", "padded "])
def test_exact_semantic_text_bindings_reject_blank_or_padded_values(invalid: str) -> None:
    base_rail = _rail(RailSpec("portable", 1))
    constructors = (
        lambda: CandidateIdentity(kind="content-digest", value=invalid),
        lambda: ArtifactDeclaration(
            artifactId="artifact",
            schemaVersion=invalid,
            mediaType="application/json",
        ),
        lambda: ArtifactDeclaration(
            artifactId="artifact",
            schemaVersion="artifact/v1",
            mediaType=invalid,
        ),
        lambda: RailAdapterDefinition(
            adapterKind="process-adapter",
            adapterId="portable-adapter",
            configurationDigest=_DIGEST,
            executionEvidence=invalid,
        ),
        lambda: RailRuntimeInputs(runtimeIdentity=invalid),
        lambda: RailEvidenceContract(
            evidenceId="evidence",
            mediaType=invalid,
            maxBytes=1,
        ),
        lambda: RailApplicability(
            profileId="portable-ci",
            status="applicable",
            selectionIdentity=invalid,
            population="population",
        ),
        lambda: RailApplicability(
            profileId="portable-ci",
            status="applicable",
            selectionIdentity="selection",
            population=invalid,
        ),
        lambda: RailApplicability(
            profileId="portable-ci",
            status="not-applicable",
            selectionIdentity="selection",
            reason=invalid,
        ),
        lambda: RailDefinition.model_validate(
            {**base_rail.model_dump(mode="json"), "orderKey": invalid}
        ),
        lambda: RailArtifactResult(
            artifactId="artifact",
            sha256=_DIGEST,
            size=1,
            evidenceRef=invalid,
        ),
        lambda: RailEvidenceReference(
            evidenceId="evidence",
            sha256=_DIGEST,
            size=1,
            reference=invalid,
        ),
        lambda: RegistryValidationFinding(
            code="invalid-contract",
            path=invalid,
            detail="detail",
        ),
        lambda: RegistryValidationFinding(
            code="invalid-contract",
            path="path",
            detail=invalid,
        ),
    )

    for constructor in constructors:
        with pytest.raises(ValidationError):
            constructor()


def test_blank_result_references_cannot_publish_a_recomputed_green_result() -> None:
    gate_plan = _gate(_plan(), 2)
    ordinary = _result(gate_plan, ObservationSpec("suite"))

    artifact_payload = ordinary.model_dump(mode="json", exclude={"resultDigest"})
    artifact_payload["artifacts"][0]["evidenceRef"] = " "
    with pytest.raises(ValidationError):
        RailResult.model_validate(
            {**artifact_payload, "resultDigest": content_digest(artifact_payload)}
        )

    evidence_payload = ordinary.model_dump(mode="json", exclude={"resultDigest"})
    evidence_payload["evidence"][0]["reference"] = " evidence://suite "
    with pytest.raises(ValidationError):
        RailResult.model_validate(
            {**evidence_payload, "resultDigest": content_digest(evidence_payload)}
        )


def test_under_budget_invalid_registry_returns_every_finding() -> None:
    rail_count = 64
    rails = tuple(
        _rail(RailSpec(f"wide-{index:04d}", 1)).model_copy(
            update={
                "railClass": "ordinary-test-suite",
                "authority": "memory-domain",
                "prerequisites": (_identity(f"missing-{index:04d}"),),
                "requiredArtifacts": (f"artifact-{index:04d}",),
                "runtimeInputs": RailRuntimeInputs(),
            }
        )
        for index in range(rail_count)
    )
    registry = RailRegistry(
        registryId="maximum-invalid-registry",
        repositoryId="sample-repository",
        profiles=(RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,)),),
        rails=rails,
    )

    report = validate_registry(canonicalize_registry(registry))
    expected_codes = {
        "missing-prerequisite",
        "runtime-inputs-missing",
        "undeclared-artifact",
        "wrong-gate-authority",
        "wrong-gate-classification",
    }

    assert len(report.findings) == rail_count * len(expected_codes)
    assert {item.code for item in report.findings} == expected_codes
    assert all(
        sum(item.code == code for item in report.findings) == rail_count for code in expected_codes
    )


@pytest.mark.parametrize(
    ("dense", "sizes"),
    [(False, (64, 256)), (True, (32, 128))],
)
def test_zero_artifact_linear_and_dense_graphs_skip_dependency_closure(
    monkeypatch: pytest.MonkeyPatch,
    dense: bool,
    sizes: tuple[int, int],
) -> None:
    def unexpected_reachability(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("zero-artifact graph requested dependency reachability")

    monkeypatch.setattr(
        "agents_remember.certification.limits._artifact_reachability_queries",
        unexpected_reachability,
    )
    for size in sizes:
        report = validate_registry(
            canonicalize_registry(_gate_one_graph_registry(size, dense=dense))
        )
        assert report.ok


def test_dependency_reachability_is_cycle_safe_and_retains_one_answer_per_query() -> None:
    registry = _gate_one_graph_registry(8, dense=True)
    rails = list(registry.rails)
    rails[0] = rails[0].model_copy(update={"prerequisites": (rails[-1].identity,)})
    rails[0] = rails[0].model_copy(
        update={
            "outputArtifacts": (
                ArtifactDeclaration(
                    artifactId="cycle-artifact",
                    schemaVersion="artifact/v1",
                    mediaType="application/json",
                ),
            )
        }
    )
    rails[-1] = rails[-1].model_copy(update={"requiredArtifacts": ("cycle-artifact",)})
    work = certification_validation.measure_registry_validation_work(
        registry.model_copy(update={"rails": tuple(rails)})
    )

    assert work.within_budget
    assert work.reachability_query_count == 1
    assert len(work._reachability_answers) == 1
    assert work.reachability_max_search_identities <= 8
    consumer_digest = content_digest(rails[-1])
    producer_digest = content_digest(rails[0])
    assert work.depends_on(consumer_digest, producer_digest)
    assert work.depends_on(consumer_digest, producer_digest)
    with pytest.raises(TypeError):
        cast(dict[tuple[str, str], bool], work._reachability_answers)[
            (consumer_digest, producer_digest)
        ] = False


def test_certification_error_findings_are_a_deeply_immutable_snapshot() -> None:
    source: list[dict[str, object]] = [
        {
            "code": "original",
            "nested": {"items": [{"value": "original"}]},
        }
    ]
    error = CertificationContractError("failed", source)
    source[0]["code"] = "source-mutated"
    source_nested = cast(dict[str, object], source[0]["nested"])
    source_items = cast(list[dict[str, object]], source_nested["items"])
    source_items[0]["value"] = "source-mutated"

    stored_top = error.findings[0]
    stored_nested = cast(Mapping[str, object], stored_top["nested"])
    stored_items = cast(tuple[object, ...], stored_nested["items"])
    stored_item = cast(Mapping[str, object], stored_items[0])
    assert stored_top["code"] == "original"
    assert stored_item["value"] == "original"

    with pytest.raises(TypeError):
        cast(dict[str, object], stored_top)["code"] = "direct-mutation"
    with pytest.raises(TypeError):
        cast(dict[str, object], stored_item)["value"] = "nested-mutation"
    with pytest.raises(AttributeError):
        object.__setattr__(error, "findings", ())


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
