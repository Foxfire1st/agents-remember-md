"""Exhaustive graph and classification validation for rail registries."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from agents_remember.certification.digests import content_digest
from agents_remember.certification.limits import (
    REGISTRY_VALIDATION_WORK_BUDGET,
    RegistryValidationWork,
    measure_registry_validation_work,
)
from agents_remember.certification.models import (
    CanonicalRailRegistry,
    RailDefinition,
    RegistryProfile,
    RegistryValidationFinding,
    RegistryValidationReport,
)
from agents_remember.models.certification.base import GateId

_CLASS_GATE: dict[str, GateId] = {
    "pre-test-quality": 1,
    "ordinary-test-suite": 2,
    "post-test-quality": 3,
    "integration-test": 4,
    "memory-quality": 5,
}
_CERTIFYING_GATES: tuple[GateId, ...] = (1, 2, 3, 4, 5)
_ProfileCatalog = dict[str, tuple[RegistryProfile, ...]]
_RailCatalog = dict[str, tuple[RailDefinition, ...]]
_ProfileDigests = dict[int, str]
_RailDigests = dict[int, str]


@dataclass(frozen=True)
class _RegistryIndex:
    profiles: _ProfileCatalog
    rails: _RailCatalog
    profile_digests: _ProfileDigests
    rail_digests: _RailDigests
    populated_gates: dict[str, frozenset[GateId]]

    def profile_path(self, profile: RegistryProfile) -> str:
        base = f"profiles.{profile.profileId}"
        if len(self.profiles[profile.profileId]) == 1:
            return base
        return f"{base}.definitions.{self.profile_digests[id(profile)]}"

    def rail_path(self, rail: RailDefinition) -> str:
        base = f"rails.{rail.identity.key}"
        if len(self.rails[rail.identity.key]) == 1:
            return base
        return f"{base}.definitions.{self.rail_digests[id(rail)]}"


def validate_registry(registry: CanonicalRailRegistry) -> RegistryValidationReport:
    """Return every independently discoverable registry admission finding."""

    work = measure_registry_validation_work(registry.registry)
    if not work.within_budget:
        finding = _finding(
            "registry-validation-budget-exceeded",
            "registry",
            "registry validation was refused before semantic finding materialization: "
            f"{work.total_units} work units exceed the "
            f"{REGISTRY_VALIDATION_WORK_BUDGET} unit budget",
        )
        return RegistryValidationReport(
            registryDigest=registry.registryDigest,
            findings=(finding,),
        )

    findings: list[RegistryValidationFinding] = []
    profiles = _profiles(registry, findings)
    rails = _rails(registry, findings)
    index = _RegistryIndex(
        profiles=profiles,
        rails=rails,
        profile_digests={
            id(profile): content_digest(profile)
            for declarations in profiles.values()
            for profile in declarations
        },
        rail_digests={
            id(rail): content_digest(rail)
            for declarations in rails.values()
            for rail in declarations
        },
        populated_gates=_populated_gates(rails),
    )
    _validate_profiles(index, findings)
    _validate_rails(index, findings)
    _validate_artifacts(index, work, findings)
    _validate_cycles(rails, findings)
    return RegistryValidationReport(
        registryDigest=registry.registryDigest,
        findings=tuple(sorted(findings, key=lambda item: (item.code, item.path, item.detail))),
    )


def _finding(code: str, path: str, detail: str) -> RegistryValidationFinding:
    return RegistryValidationFinding(code=code, path=path, detail=detail)


def _profiles(
    registry: CanonicalRailRegistry,
    findings: list[RegistryValidationFinding],
) -> _ProfileCatalog:
    grouped: dict[str, list[RegistryProfile]] = defaultdict(list)
    for profile in registry.registry.profiles:
        grouped[profile.profileId].append(profile)
    for profile_id, declarations in grouped.items():
        if len(declarations) > 1:
            findings.append(
                _finding(
                    "duplicate-profile-identity",
                    f"profiles.{profile_id}",
                    "profile identity is declared more than once",
                )
            )
    return {profile_id: tuple(declarations) for profile_id, declarations in grouped.items()}


def _rails(
    registry: CanonicalRailRegistry,
    findings: list[RegistryValidationFinding],
) -> _RailCatalog:
    grouped: dict[str, list[RailDefinition]] = defaultdict(list)
    for rail in registry.registry.rails:
        grouped[rail.identity.key].append(rail)
    for identity, declarations in grouped.items():
        if len(declarations) > 1:
            findings.append(
                _finding(
                    "duplicate-rail-identity",
                    f"rails.{identity}",
                    "one rail identity has conflicting canonical definitions",
                )
            )
    return {identity: tuple(declarations) for identity, declarations in grouped.items()}


def _validate_profiles(
    index: _RegistryIndex,
    findings: list[RegistryValidationFinding],
) -> None:
    for declarations in index.profiles.values():
        for profile in declarations:
            path = index.profile_path(profile)
            _validate_profile_gates(profile, path, findings)
            _validate_profile_population(profile, path, index.populated_gates, findings)


def _validate_profile_gates(
    profile: RegistryProfile,
    path: str,
    findings: list[RegistryValidationFinding],
) -> None:
    gate_path = f"{path}.gates"
    if len(profile.gates) != len(set(profile.gates)):
        findings.append(
            _finding(
                "duplicate-profile-gate",
                gate_path,
                "profile gate declarations must be unique",
            )
        )
    if profile.kind == "certifying" and tuple(sorted(profile.gates)) != _CERTIFYING_GATES:
        findings.append(
            _finding(
                "incomplete-certifying-profile",
                gate_path,
                "certifying profiles must declare all five gates",
            )
        )
    selected_gates = set(profile.gates)
    missing_prefix = {
        prerequisite
        for gate in selected_gates
        for prerequisite in range(1, gate)
        if prerequisite not in selected_gates
    }
    if missing_prefix:
        findings.append(
            _finding(
                "profile-gate-prefix-missing",
                gate_path,
                "later gates require all earlier gate barriers: "
                + ", ".join(str(item) for item in sorted(missing_prefix)),
            )
        )


def _validate_profile_population(
    profile: RegistryProfile,
    path: str,
    populated_gates: dict[str, frozenset[GateId]],
    findings: list[RegistryValidationFinding],
) -> None:
    populated = populated_gates.get(profile.profileId, frozenset())
    for gate in sorted(set(profile.gates) - populated):
        findings.append(
            _finding(
                "profile-gate-empty",
                f"{path}.gates.{gate}",
                "every declared gate requires an applicable or explicit not-applicable rail",
            )
        )


def _populated_gates(rails: _RailCatalog) -> dict[str, frozenset[GateId]]:
    populated: dict[str, set[GateId]] = defaultdict(set)
    for declarations in rails.values():
        for rail in declarations:
            for applicability in rail.applicability:
                populated[applicability.profileId].add(rail.gate)
    return {profile_id: frozenset(gates) for profile_id, gates in populated.items()}


def _validate_rails(
    index: _RegistryIndex,
    findings: list[RegistryValidationFinding],
) -> None:
    for declarations in index.rails.values():
        for rail in declarations:
            _validate_rail(rail, index, findings)


def _validate_rail(
    rail: RailDefinition,
    index: _RegistryIndex,
    findings: list[RegistryValidationFinding],
) -> None:
    path = index.rail_path(rail)
    expected_gate = _CLASS_GATE[rail.railClass]
    if rail.gate != expected_gate:
        findings.append(
            _finding(
                "wrong-gate-classification",
                f"{path}.gate",
                f"{rail.railClass} belongs to Gate {expected_gate}, not Gate {rail.gate}",
            )
        )
    expected_authority = "memory-domain" if rail.gate == 5 else "repository-profile"
    if rail.authority != expected_authority:
        findings.append(
            _finding(
                "wrong-gate-authority",
                f"{path}.authority",
                f"Gate {rail.gate} requires {expected_authority} authority",
            )
        )
    _validate_unique_fields(rail, path, findings)
    _validate_applicability(rail, index, path, findings)
    _validate_dependencies(rail, index, path, findings)
    _validate_runtime_inputs(rail, path, findings)
    if rail.gate == 2 and not rail.outputArtifacts:
        findings.append(
            _finding(
                "suite-artifact-missing",
                f"{path}.outputArtifacts",
                "an ordinary suite rail must declare its exact output artifacts",
            )
        )


def _validate_runtime_inputs(
    rail: RailDefinition,
    path: str,
    findings: list[RegistryValidationFinding],
) -> None:
    applicable = any(item.status == "applicable" for item in rail.applicability)
    declared = any(
        value is not None and bool(value.strip())
        for value in rail.runtimeInputs.model_dump().values()
    )
    if applicable and not declared:
        findings.append(
            _finding(
                "runtime-inputs-missing",
                f"{path}.runtimeInputs",
                "an applicable rail must declare the runtime inputs it consumes",
            )
        )


def _validate_unique_fields(
    rail: RailDefinition,
    path: str,
    findings: list[RegistryValidationFinding],
) -> None:
    fields = {
        "prerequisites": [item.key for item in rail.prerequisites],
        "requiredArtifacts": list(rail.requiredArtifacts),
        "applicability": [item.profileId for item in rail.applicability],
        "evidenceContract": [item.evidenceId for item in rail.evidenceContract],
        "outputArtifacts": [item.artifactId for item in rail.outputArtifacts],
    }
    for field, values in fields.items():
        duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
        if duplicates:
            findings.append(
                _finding(
                    "duplicate-rail-field",
                    f"{path}.{field}",
                    f"duplicate declarations: {', '.join(duplicates)}",
                )
            )


def _validate_applicability(
    rail: RailDefinition,
    index: _RegistryIndex,
    path: str,
    findings: list[RegistryValidationFinding],
) -> None:
    for applicability in rail.applicability:
        declarations = index.profiles.get(applicability.profileId)
        if declarations is None:
            findings.append(
                _finding(
                    "unknown-profile",
                    f"{path}.applicability.{applicability.profileId}",
                    "rail applicability names an undeclared profile",
                )
            )
            continue
        for profile in declarations:
            if rail.gate not in profile.gates:
                findings.append(
                    _finding(
                        "profile-gate-mismatch",
                        f"{path}.applicability.{applicability.profileId}.definitions."
                        f"{index.profile_digests[id(profile)]}",
                        f"profile definition does not admit Gate {rail.gate}",
                    )
                )


def _validate_dependencies(
    rail: RailDefinition,
    index: _RegistryIndex,
    path: str,
    findings: list[RegistryValidationFinding],
) -> None:
    applicability_profiles = {item.profileId for item in rail.applicability}
    for prerequisite in rail.prerequisites:
        declarations = index.rails.get(prerequisite.key)
        if declarations is None:
            findings.append(
                _finding(
                    "missing-prerequisite",
                    f"{path}.prerequisites.{prerequisite.key}",
                    "rail prerequisite is not declared",
                )
            )
            continue
        for dependency in declarations:
            dependency_path = (
                f"{path}.prerequisites.{prerequisite.key}.definitions."
                f"{index.rail_digests[id(dependency)]}"
            )
            if dependency.gate > rail.gate:
                findings.append(
                    _finding(
                        "later-gate-prerequisite",
                        dependency_path,
                        "a rail cannot depend on a later certification gate",
                    )
                )
            dependency_profiles = {item.profileId for item in dependency.applicability}
            missing_profiles = sorted(applicability_profiles - dependency_profiles)
            if missing_profiles:
                findings.append(
                    _finding(
                        "profile-prerequisite-missing",
                        dependency_path,
                        "prerequisite is absent from profiles: " + ", ".join(missing_profiles),
                    )
                )
            _validate_dependency_applicability(
                rail,
                dependency,
                dependency_path,
                findings,
            )


def _validate_dependency_applicability(
    rail: RailDefinition,
    dependency: RailDefinition,
    dependency_path: str,
    findings: list[RegistryValidationFinding],
) -> None:
    dependency_by_profile = {item.profileId: item.status for item in dependency.applicability}
    invalid_profiles = sorted(
        item.profileId
        for item in rail.applicability
        if item.status == "applicable"
        and dependency_by_profile.get(item.profileId) == "not-applicable"
    )
    if invalid_profiles:
        findings.append(
            _finding(
                "inapplicable-prerequisite",
                dependency_path,
                "applicable rail depends on a non-applicable rail in profiles: "
                + ", ".join(invalid_profiles),
            )
        )


def _validate_artifacts(
    index: _RegistryIndex,
    work: RegistryValidationWork,
    findings: list[RegistryValidationFinding],
) -> None:
    producers = _artifact_producers(index, findings)
    for declarations in index.rails.values():
        for rail in declarations:
            _validate_rail_artifacts(
                rail,
                index,
                producers,
                work,
                findings,
            )


def _artifact_producers(
    index: _RegistryIndex,
    findings: list[RegistryValidationFinding],
) -> dict[str, list[RailDefinition]]:
    producers: dict[str, list[RailDefinition]] = defaultdict(list)
    for declarations in index.rails.values():
        for rail in declarations:
            for artifact in rail.outputArtifacts:
                producers[artifact.artifactId].append(rail)
    for artifact_id, declarations in producers.items():
        identities = {(rail.identity.key, index.rail_digests[id(rail)]) for rail in declarations}
        if len(identities) > 1:
            findings.append(
                _finding(
                    "duplicate-artifact-producer",
                    f"artifacts.{artifact_id}",
                    "an artifact identity must have one producing rail",
                )
            )
    return producers


def _validate_rail_artifacts(
    rail: RailDefinition,
    index: _RegistryIndex,
    producers: dict[str, list[RailDefinition]],
    work: RegistryValidationWork,
    findings: list[RegistryValidationFinding],
) -> None:
    path = index.rail_path(rail)
    if not rail.requiredArtifacts:
        if rail.gate == 3:
            findings.append(
                _finding(
                    "post-test-artifact-missing",
                    f"{path}.requiredArtifacts",
                    "a post-test rail must name an artifact from Gate 2",
                )
            )
        return
    gate_two_input = False
    for artifact_id in rail.requiredArtifacts:
        candidates = producers.get(artifact_id, [])
        artifact_path = f"{path}.requiredArtifacts.{artifact_id}"
        if not candidates:
            findings.append(
                _finding(
                    "undeclared-artifact",
                    artifact_path,
                    "required artifact has no declared producer",
                )
            )
            continue
        unique_candidates = {
            (producer.identity.key, index.rail_digests[id(producer)]): producer
            for producer in candidates
        }
        for (_, producer_digest), producer in sorted(unique_candidates.items()):
            gate_two_input = (
                _validate_artifact_candidate(
                    rail,
                    producer,
                    work.depends_on(
                        index.rail_digests[id(rail)],
                        producer_digest,
                    ),
                    f"{artifact_path}.producers.{producer_digest}",
                    findings,
                )
                or gate_two_input
            )
    if rail.gate == 3 and not gate_two_input:
        findings.append(
            _finding(
                "post-test-artifact-missing",
                f"{path}.requiredArtifacts",
                "a post-test rail must name an artifact from Gate 2",
            )
        )


def _validate_artifact_candidate(
    consumer: RailDefinition,
    producer: RailDefinition,
    producer_is_prerequisite: bool,
    path: str,
    findings: list[RegistryValidationFinding],
) -> bool:
    if not producer_is_prerequisite:
        findings.append(
            _finding(
                "artifact-prerequisite-missing",
                path,
                "artifact producer must be in the rail dependency closure",
            )
        )
    if producer.gate > consumer.gate:
        findings.append(
            _finding(
                "later-gate-artifact",
                path,
                "a rail cannot consume a later-gate artifact",
            )
        )
    return producer.gate == 2


def _validate_cycles(
    rails: _RailCatalog,
    findings: list[RegistryValidationFinding],
) -> None:
    incoming, dependants = _dependency_edges(rails)
    resolved = _resolve_acyclic_nodes(incoming, dependants)
    unresolved = sorted(set(rails) - resolved)
    if unresolved:
        findings.append(
            _finding(
                "dependency-cycle",
                "rails",
                "dependency graph contains a cyclic blocked set: " + ", ".join(unresolved),
            )
        )


def _dependency_edges(
    rails: _RailCatalog,
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    incoming = {
        identity: {
            item.key for rail in declarations for item in rail.prerequisites if item.key in rails
        }
        for identity, declarations in rails.items()
    }
    dependants: dict[str, set[str]] = defaultdict(set)
    for candidate, prerequisites in incoming.items():
        for prerequisite in prerequisites:
            dependants[prerequisite].add(candidate)
    return incoming, dependants


def _resolve_acyclic_nodes(
    incoming: dict[str, set[str]],
    dependants: dict[str, set[str]],
) -> set[str]:
    ready = deque(sorted(identity for identity, edges in incoming.items() if not edges))
    resolved: set[str] = set()
    while ready:
        identity = ready.popleft()
        resolved.add(identity)
        for candidate in sorted(dependants[identity]):
            incoming[candidate].discard(identity)
            if not incoming[candidate]:
                ready.append(candidate)
    return resolved
