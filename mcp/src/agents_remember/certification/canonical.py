"""Deterministic canonicalization of rail registry contributions."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from agents_remember.certification.digests import content_digest
from agents_remember.certification.limits import (
    REGISTRY_VALIDATION_WORK_BUDGET,
    admit_registry_canonicalization,
)
from agents_remember.certification.models import (
    CanonicalRailRegistry,
    RailDefinition,
    RailRegistry,
    RegistryProfile,
)
from agents_remember.errors import CertificationContractError


@dataclass(frozen=True)
class _NormalizedRail:
    definition: RailDefinition
    digest: str


@dataclass(frozen=True)
class _NormalizedProfile:
    definition: RegistryProfile
    digest: str


def canonicalize_registry(registry: RailRegistry) -> CanonicalRailRegistry:
    """Sort declarations and collapse byte-identical rail registrations."""

    admission = admit_registry_canonicalization(registry)
    if not admission.within_budget:
        observed = max(
            admission.validation_floor_units,
            admission.canonicalization_membership_units,
            admission.canonicalization_digest_units,
        )
        raise CertificationContractError(
            "rail registry admission failed before canonicalization",
            (
                {
                    "code": "registry-validation-budget-exceeded",
                    "path": "registry",
                    "detail": (
                        "registry canonicalization was refused before normalization or digest "
                        f"allocation: {observed} units exceed the "
                        f"{REGISTRY_VALIDATION_WORK_BUDGET} unit boundary"
                    ),
                },
            ),
        )
    normalized_rails = _canonical_rails(admission.rails)
    normalized_profiles = _canonical_profiles(admission.profiles)
    normalized = registry.model_copy(
        update={
            "profiles": normalized_profiles,
            "rails": normalized_rails,
        }
    )
    digest = content_digest({"registry": normalized.model_dump(mode="json")})
    return CanonicalRailRegistry(registry=normalized, registryDigest=digest)


def _canonical_rails(
    rails: tuple[RailDefinition, ...],
) -> tuple[RailDefinition, ...]:
    grouped: dict[str, dict[str, _NormalizedRail]] = defaultdict(dict)
    for rail in rails:
        normalized = _normalize_rail(rail)
        item = _NormalizedRail(normalized, content_digest(normalized))
        grouped[normalized.identity.key][item.digest] = item
    entries = (item for identity in sorted(grouped) for item in grouped[identity].values())
    return tuple(item.definition for item in sorted(entries, key=_rail_sort_key))


def _canonical_profiles(
    profiles: tuple[RegistryProfile, ...],
) -> tuple[RegistryProfile, ...]:
    grouped: dict[str, dict[str, _NormalizedProfile]] = defaultdict(dict)
    for profile in profiles:
        normalized = _normalize_profile(profile)
        item = _NormalizedProfile(normalized, content_digest(normalized))
        grouped[normalized.profileId][item.digest] = item
    return tuple(
        item.definition
        for identity in sorted(grouped)
        for item in sorted(grouped[identity].values(), key=lambda entry: entry.digest)
    )


def _normalize_rail(rail: RailDefinition) -> RailDefinition:
    return rail.model_copy(
        update={
            "prerequisites": _sorted_contract_items(
                rail.prerequisites,
                identity=lambda item: item.key,
            ),
            "requiredArtifacts": tuple(sorted(rail.requiredArtifacts)),
            "applicability": _sorted_contract_items(
                rail.applicability,
                identity=lambda item: item.profileId,
            ),
            "evidenceContract": _sorted_contract_items(
                rail.evidenceContract,
                identity=lambda item: item.evidenceId,
            ),
            "outputArtifacts": _sorted_contract_items(
                rail.outputArtifacts,
                identity=lambda item: item.artifactId,
            ),
        }
    )


def _sorted_contract_items[ContractItem: BaseModel](
    items: tuple[ContractItem, ...],
    *,
    identity: Callable[[ContractItem], str],
) -> tuple[ContractItem, ...]:
    decorated = ((identity(item), content_digest(item), item) for item in items)
    return tuple(item for _, _, item in sorted(decorated, key=lambda entry: entry[:2]))


def _normalize_profile(profile: RegistryProfile) -> RegistryProfile:
    return profile.model_copy(update={"gates": tuple(sorted(profile.gates))})


def _rail_sort_key(item: _NormalizedRail) -> tuple[object, ...]:
    rail = item.definition
    return (
        rail.gate,
        rail.orderKey,
        rail.identity.railId,
        rail.identity.version,
        item.digest,
    )
