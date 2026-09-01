"""Exhaustive semantic-finding edges for the canonical certification registry."""

from __future__ import annotations

from agents_remember.certification import canonicalize_registry, validate_registry
from agents_remember.certification.digests import content_digest
from agents_remember.certification.limits import (
    REGISTRY_VALIDATION_WORK_BUDGET,
)
from agents_remember.certification.models import (
    CanonicalRailRegistry,
    RailApplicability,
    RailRegistry,
    RegistryProfile,
)
from certification_registry_test_support import (
    RailSpec,
    _rail,
    _raw_declaration_overflow_registry,
    _registry,
)


def _codes(registry: RailRegistry) -> set[str]:
    return {finding.code for finding in validate_registry(canonicalize_registry(registry)).findings}


def test_validation_budget_refusal_publishes_one_typed_finding() -> None:
    over = _raw_declaration_overflow_registry()
    payload = {"registry": over.model_dump(mode="json")}
    canonical = CanonicalRailRegistry(
        registry=over,
        registryDigest=content_digest(payload),
    )

    report = validate_registry(canonical)

    assert not report.ok
    assert tuple(finding.code for finding in report.findings) == (
        "registry-validation-budget-exceeded",
    )
    assert str(REGISTRY_VALIDATION_WORK_BUDGET + 1) in report.findings[0].detail


def test_profiles_report_duplicate_gates_and_every_empty_gate() -> None:
    duplicate = _registry(
        (RailSpec("lint", 1),),
        profile=RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1, 1)),
    )
    assert "duplicate-profile-gate" in _codes(duplicate)

    empty_gate = _registry(
        (RailSpec("lint", 1),),
        profile=RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1, 2)),
    )
    assert "profile-gate-empty" in _codes(empty_gate)


def test_duplicate_rail_members_are_reported_by_field() -> None:
    registry = _registry()
    lint = registry.rails[0]
    duplicate_evidence = lint.model_copy(
        update={"evidenceContract": (lint.evidenceContract[0], lint.evidenceContract[0])}
    )
    changed = registry.model_copy(update={"rails": (duplicate_evidence, *registry.rails[1:])})

    report = validate_registry(canonicalize_registry(changed))

    matching = [finding for finding in report.findings if finding.code == "duplicate-rail-field"]
    assert len(matching) == 1
    assert matching[0].path.endswith(".evidenceContract")


def test_unknown_applicability_profile_refuses_and_skips_profile_iteration() -> None:
    registry = _registry()
    foreign = RailApplicability(
        profileId="foreign",
        status="applicable",
        selectionIdentity="selection:foreign",
        population="foreign population",
    )
    lint = registry.rails[0].model_copy(update={"applicability": (foreign,)})
    changed = registry.model_copy(update={"rails": (lint, *registry.rails[1:])})

    assert "unknown-profile" in _codes(changed)


def test_prerequisite_must_exist_in_every_consumer_profile() -> None:
    portable = RegistryProfile(profileId="portable", kind="diagnostic", gates=(1,))
    secondary = RegistryProfile(profileId="secondary", kind="diagnostic", gates=(1,))
    prerequisite = _rail(RailSpec("prerequisite", 1, profile_id="portable"))
    consumer = _rail(
        RailSpec(
            "consumer",
            1,
            prerequisites=("prerequisite",),
            profile_id="portable",
        )
    )
    consumer = consumer.model_copy(
        update={
            "applicability": (
                *consumer.applicability,
                RailApplicability(
                    profileId="secondary",
                    status="applicable",
                    selectionIdentity="selection:secondary",
                    population="secondary population",
                ),
            )
        }
    )
    registry = RailRegistry(
        registryId="portable-closeout",
        repositoryId="sample-repository",
        profiles=(portable, secondary),
        rails=(prerequisite, consumer),
    )

    assert "profile-prerequisite-missing" in _codes(registry)


def test_artifact_identity_has_exactly_one_producer() -> None:
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1,))
    registry = _registry(
        (
            RailSpec("producer-a", 1, output_artifacts=("shared-artifact",)),
            RailSpec("producer-b", 1, output_artifacts=("shared-artifact",)),
        ),
        profile=profile,
    )

    assert "duplicate-artifact-producer" in _codes(registry)


def test_gate_three_without_gate_two_artifact_is_reported() -> None:
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1, 2, 3))
    registry = _registry(
        (
            RailSpec("lint", 1),
            RailSpec("suite", 2, prerequisites=("lint",), output_artifacts=("suite-data",)),
            RailSpec("coverage", 3, prerequisites=("suite",)),
        ),
        profile=profile,
    )

    assert "post-test-artifact-missing" in _codes(registry)


def test_earlier_gate_cannot_consume_a_later_gate_artifact() -> None:
    profile = RegistryProfile(profileId="portable-ci", kind="diagnostic", gates=(1, 2))
    registry = _registry(
        (
            RailSpec(
                "consumer",
                1,
                prerequisites=("producer",),
                required_artifacts=("later-artifact",),
            ),
            RailSpec("producer", 2, output_artifacts=("later-artifact",)),
        ),
        profile=profile,
    )

    codes = _codes(registry)
    assert "later-gate-prerequisite" in codes
    assert "later-gate-artifact" in codes
