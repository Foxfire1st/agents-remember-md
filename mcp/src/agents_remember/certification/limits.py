"""Deterministic admission budgets for registry canonicalization and validation."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    RailDefinition,
    RailRegistry,
    RegistryProfile,
)

# One unit is one bounded declaration/reference visit, reachability operation,
# or retained relationship-identity membership. Canonicalization and validation
# each refuse at this boundary before allocating their expensive phase.
REGISTRY_VALIDATION_WORK_BUDGET = 131_072

_ReserveOperation = Callable[[int], bool]
_ReserveSearchState = Callable[[int], bool]
_AnswerKey = tuple[str, str]


@dataclass(frozen=True)
class RegistryCanonicalizationAdmission:
    """Typed pre-canonicalization account over exact raw contributions."""

    declaration_units: int
    cross_reference_units: int
    validation_floor_units: int
    canonicalization_membership_units: int
    canonicalization_digest_units: int
    _profiles: tuple[RegistryProfile, ...] = field(repr=False, compare=False)
    _rails: tuple[RailDefinition, ...] = field(repr=False, compare=False)

    @property
    def within_budget(self) -> bool:
        return all(
            units <= REGISTRY_VALIDATION_WORK_BUDGET
            for units in (
                self.validation_floor_units,
                self.canonicalization_membership_units,
                self.canonicalization_digest_units,
            )
        )

    @property
    def profiles(self) -> tuple[RegistryProfile, ...]:
        return self._profiles

    @property
    def rails(self) -> tuple[RailDefinition, ...]:
        return self._rails


@dataclass(frozen=True)
class RegistryValidationWork:
    """Complete bounded work account prepared before semantic validation."""

    declaration_units: int
    cross_reference_units: int
    reachability_operation_units: int
    reachability_storage_units: int
    reachability_complete: bool
    reachability_query_count: int
    reachability_max_search_identities: int
    reachability_digest_index_units: int
    reachability_producer_catalog_units: int
    reachability_query_answer_units: int
    reachability_graph_units: int
    _reachability_answers: Mapping[_AnswerKey, bool] = field(
        repr=False,
        compare=False,
    )

    @property
    def total_units(self) -> int:
        return (
            self.declaration_units
            + self.cross_reference_units
            + self.reachability_operation_units
            + self.reachability_storage_units
        )

    @property
    def within_budget(self) -> bool:
        return self.reachability_complete and self.total_units <= REGISTRY_VALIDATION_WORK_BUDGET

    def depends_on(self, consumer_digest: str, prerequisite_digest: str) -> bool:
        """Return one exact-definition reachability answer for an admitted registry."""

        if not self.within_budget:
            raise ValueError("reachability answers are unavailable for a refused registry")
        try:
            return self._reachability_answers[(consumer_digest, prerequisite_digest)]
        except KeyError as error:
            raise ValueError("reachability pair was not part of registry admission") from error


@dataclass(frozen=True)
class _ReachabilityStorageCensus:
    digest_index_units: int
    producer_catalog_units: int
    query_answer_units: int
    graph_units: int

    @property
    def query_construction_peak(self) -> int:
        return self.digest_index_units + self.producer_catalog_units + self.query_answer_units

    @property
    def graph_resolution_base(self) -> int:
        return self.digest_index_units + self.query_answer_units + self.graph_units

    def peak(self, search_identities: int) -> int:
        return max(
            self.query_construction_peak,
            self.graph_resolution_base + search_identities,
        )


@dataclass(frozen=True)
class _ReachabilityMeasurement:
    operation_units: int
    storage_units: int
    complete: bool
    query_count: int
    max_search_identities: int
    census: _ReachabilityStorageCensus
    answers: Mapping[_AnswerKey, bool]


@dataclass
class _ReachabilityBudget:
    fixed_units: int
    census: _ReachabilityStorageCensus
    operation_units: int = 0
    max_search_identities: int = 0

    def reserve_search_state(self, search_identity_count: int) -> bool:
        return self._reserve(search_identity_count, 0)

    def reserve_operation(self, search_identity_count: int) -> bool:
        return self._reserve(search_identity_count, 1)

    def _reserve(self, search_identity_count: int, operation_increment: int) -> bool:
        next_max = max(self.max_search_identities, search_identity_count)
        next_operations = self.operation_units + operation_increment
        next_storage = self.census.peak(next_max)
        self.operation_units = next_operations
        self.max_search_identities = next_max
        return self.fixed_units + next_storage + next_operations <= REGISTRY_VALIDATION_WORK_BUDGET


@dataclass(frozen=True)
class _ReachabilityContext:
    registry: RailRegistry
    digests: tuple[str, ...]
    variants: dict[str, list[str]]
    budget: _ReachabilityBudget


def admit_registry_canonicalization(
    registry: RailRegistry,
) -> RegistryCanonicalizationAdmission:
    """Bound raw normalization/digest work before canonicalization allocates it."""

    raw_declaration_units = _declaration_units(registry.profiles, registry.rails)
    if raw_declaration_units > REGISTRY_VALIDATION_WORK_BUDGET:
        return RegistryCanonicalizationAdmission(
            declaration_units=raw_declaration_units,
            cross_reference_units=0,
            validation_floor_units=raw_declaration_units,
            canonicalization_membership_units=raw_declaration_units,
            canonicalization_digest_units=0,
            _profiles=registry.profiles,
            _rails=registry.rails,
        )
    profiles = _deduplicate_exact_profiles(registry.profiles)
    rails = _deduplicate_exact_rails(registry.rails)
    declaration_units = _declaration_units(profiles, rails)
    profile_counts = Counter(profile.profileId for profile in profiles)
    rail_counts = Counter(rail.identity.key for rail in rails)
    artifact_counts = Counter(
        artifact.artifactId for rail in rails for artifact in rail.outputArtifacts
    )
    cross_reference_units = sum(
        _rail_cross_reference_units(
            rail,
            profile_counts=profile_counts,
            rail_counts=rail_counts,
            artifact_counts=artifact_counts,
        )
        for rail in rails
    )
    query_upper_bound = _query_upper_bound(rails, artifact_counts)
    edge_upper_bound = _edge_upper_bound(rails, rail_counts)
    identity_count = len(rail_counts)
    artifact_count = len(artifact_counts)
    reachability_floor = 0
    if query_upper_bound:
        prospective = _storage_census(
            rails,
            identity_count=identity_count,
            artifact_count=artifact_count,
            query_count=query_upper_bound,
            edge_count=edge_upper_bound,
        )
        search_upper_bound = _search_state_upper_bound(
            rails,
            rail_counts,
            artifact_counts,
        )
        reachability_floor = prospective.peak(search_upper_bound)
    # The complete registry digest is computed once for construction and once
    # by the immutable model's self-verification validator.
    canonicalization_digest_units = 2 + len(profiles) + len(rails)
    canonicalization_digest_units += sum(_rail_nested_digest_units(rail) for rail in rails)
    return RegistryCanonicalizationAdmission(
        declaration_units=declaration_units,
        cross_reference_units=cross_reference_units,
        validation_floor_units=(declaration_units + cross_reference_units + reachability_floor),
        canonicalization_membership_units=raw_declaration_units,
        canonicalization_digest_units=canonicalization_digest_units,
        _profiles=profiles,
        _rails=rails,
    )


def measure_registry_validation_work(registry: RailRegistry) -> RegistryValidationWork:
    """Measure and cap every relationship operation before findings are allocated."""

    declaration_units = _declaration_units(registry.profiles, registry.rails)
    if declaration_units > REGISTRY_VALIDATION_WORK_BUDGET:
        return _refused_work(declaration_units, 0)
    profile_counts = Counter(profile.profileId for profile in registry.profiles)
    rail_counts = Counter(rail.identity.key for rail in registry.rails)
    artifact_counts = Counter(
        artifact.artifactId for rail in registry.rails for artifact in rail.outputArtifacts
    )
    cross_reference_units = sum(
        _rail_cross_reference_units(
            rail,
            profile_counts=profile_counts,
            rail_counts=rail_counts,
            artifact_counts=artifact_counts,
        )
        for rail in registry.rails
    )
    fixed_units = declaration_units + cross_reference_units
    if fixed_units > REGISTRY_VALIDATION_WORK_BUDGET:
        return _refused_work(declaration_units, cross_reference_units)

    query_upper_bound = _query_upper_bound(registry.rails, artifact_counts)
    if query_upper_bound:
        search_upper_bound = _search_state_upper_bound(
            registry.rails,
            rail_counts,
            artifact_counts,
        )
        prospective = _storage_census(
            registry.rails,
            identity_count=len(rail_counts),
            artifact_count=len(artifact_counts),
            query_count=query_upper_bound,
            edge_count=_edge_upper_bound(registry.rails, rail_counts),
        )
    else:
        # No consumer/producer relationship exists, so constructing a producer
        # census before the declaration indexes are released would be pure overhead.
        search_upper_bound = 0
        prospective = _ReachabilityStorageCensus(0, 0, 0, 0)
    # Declaration indexes are bounded by declaration_units and are released
    # before any query, digest index, or graph is allocated.
    del profile_counts, rail_counts, artifact_counts

    reachability = _measure_reachability(
        registry,
        fixed_units=fixed_units,
        prospective=prospective,
        search_upper_bound=search_upper_bound,
    )
    return RegistryValidationWork(
        declaration_units=declaration_units,
        cross_reference_units=cross_reference_units,
        reachability_operation_units=reachability.operation_units,
        reachability_storage_units=reachability.storage_units,
        reachability_complete=reachability.complete,
        reachability_query_count=reachability.query_count,
        reachability_max_search_identities=reachability.max_search_identities,
        reachability_digest_index_units=reachability.census.digest_index_units,
        reachability_producer_catalog_units=reachability.census.producer_catalog_units,
        reachability_query_answer_units=reachability.census.query_answer_units,
        reachability_graph_units=reachability.census.graph_units,
        _reachability_answers=reachability.answers,
    )


def _refused_work(declaration_units: int, cross_reference_units: int) -> RegistryValidationWork:
    return RegistryValidationWork(
        declaration_units=declaration_units,
        cross_reference_units=cross_reference_units,
        reachability_operation_units=0,
        reachability_storage_units=0,
        reachability_complete=False,
        reachability_query_count=0,
        reachability_max_search_identities=0,
        reachability_digest_index_units=0,
        reachability_producer_catalog_units=0,
        reachability_query_answer_units=0,
        reachability_graph_units=0,
        _reachability_answers=MappingProxyType({}),
    )


def _deduplicate_exact_profiles(
    profiles: tuple[RegistryProfile, ...],
) -> tuple[RegistryProfile, ...]:
    return tuple(dict.fromkeys(profiles))


def _deduplicate_exact_rails(
    rails: tuple[RailDefinition, ...],
) -> tuple[RailDefinition, ...]:
    return tuple(dict.fromkeys(rails))


def _rail_declaration_units(rail: RailDefinition) -> int:
    return sum(
        len(items)
        for items in (
            rail.prerequisites,
            rail.requiredArtifacts,
            rail.applicability,
            rail.evidenceContract,
            rail.outputArtifacts,
        )
    )


def _declaration_units(
    profiles: tuple[RegistryProfile, ...],
    rails: tuple[RailDefinition, ...],
) -> int:
    return (
        len(profiles)
        + len(rails)
        + sum(len(profile.gates) for profile in profiles)
        + sum(_rail_declaration_units(rail) for rail in rails)
    )


def _rail_nested_digest_units(rail: RailDefinition) -> int:
    return sum(
        len(items)
        for items in (
            rail.prerequisites,
            rail.applicability,
            rail.evidenceContract,
            rail.outputArtifacts,
        )
    )


def _rail_cross_reference_units(
    rail: RailDefinition,
    *,
    profile_counts: Counter[str],
    rail_counts: Counter[str],
    artifact_counts: Counter[str],
) -> int:
    return (
        sum(profile_counts[item.profileId] for item in rail.applicability)
        + sum(rail_counts[item.key] for item in rail.prerequisites)
        + sum(artifact_counts[item] for item in rail.requiredArtifacts)
    )


def _measure_reachability(
    registry: RailRegistry,
    *,
    fixed_units: int,
    prospective: _ReachabilityStorageCensus,
    search_upper_bound: int,
) -> _ReachabilityMeasurement:
    query_upper_bound = prospective.query_answer_units // 2
    if not query_upper_bound:
        return _empty_reachability()

    preallocation_storage = prospective.peak(search_upper_bound)
    if fixed_units + preallocation_storage > REGISTRY_VALIDATION_WORK_BUDGET:
        return _ReachabilityMeasurement(
            operation_units=0,
            storage_units=preallocation_storage,
            complete=False,
            query_count=query_upper_bound,
            max_search_identities=0,
            census=prospective,
            answers=MappingProxyType({}),
        )

    digests = tuple(content_digest(rail) for rail in registry.rails)
    variants = _rail_variants(registry, digests)
    singleton_queries, shared_queries, query_count, producer_catalog_units = (
        _artifact_reachability_queries(registry, digests)
    )
    census = _ReachabilityStorageCensus(
        digest_index_units=len(digests) + len(variants) + sum(map(len, variants.values())),
        producer_catalog_units=producer_catalog_units,
        query_answer_units=2 * query_count,
        graph_units=prospective.graph_units,
    )
    budget = _ReachabilityBudget(fixed_units=fixed_units, census=census)
    context = _ReachabilityContext(registry, digests, variants, budget)
    answers: dict[_AnswerKey, bool] = {}
    if not _resolve_singleton_queries(
        context,
        singleton_queries,
        answers,
    ):
        return _incomplete_reachability(budget, query_count)
    if not _resolve_shared_queries(
        context,
        shared_queries,
        answers,
    ):
        return _incomplete_reachability(budget, query_count)
    return _ReachabilityMeasurement(
        operation_units=budget.operation_units,
        storage_units=census.peak(budget.max_search_identities),
        complete=True,
        query_count=query_count,
        max_search_identities=budget.max_search_identities,
        census=census,
        answers=MappingProxyType(answers),
    )


def _empty_reachability() -> _ReachabilityMeasurement:
    census = _ReachabilityStorageCensus(0, 0, 0, 0)
    return _ReachabilityMeasurement(0, 0, True, 0, 0, census, MappingProxyType({}))


def _query_upper_bound(
    rails: tuple[RailDefinition, ...],
    artifact_counts: Counter[str],
) -> int:
    return sum(
        artifact_counts[artifact_id] for rail in rails for artifact_id in rail.requiredArtifacts
    )


def _edge_upper_bound(
    rails: tuple[RailDefinition, ...],
    rail_counts: Counter[str],
) -> int:
    return sum(rail_counts[item.key] for rail in rails for item in rail.prerequisites)


def _search_state_upper_bound(
    rails: tuple[RailDefinition, ...],
    rail_counts: Counter[str],
    artifact_counts: Counter[str],
) -> int:
    producers: dict[str, Counter[str]] = defaultdict(Counter)
    for rail in rails:
        for artifact in rail.outputArtifacts:
            producers[artifact.artifactId][rail.identity.key] += 1
    nontrivial_queries = 0
    for rail in rails:
        for artifact_id in rail.requiredArtifacts:
            nontrivial_queries += artifact_counts[artifact_id]
            candidates = producers.get(artifact_id)
            if candidates is not None and rail_counts[rail.identity.key] == 1:
                nontrivial_queries -= candidates[rail.identity.key]
    if not nontrivial_queries:
        return 0
    return nontrivial_queries + 2 * len(rails)


def _storage_census(
    rails: tuple[RailDefinition, ...],
    *,
    identity_count: int,
    artifact_count: int,
    query_count: int,
    edge_count: int,
) -> _ReachabilityStorageCensus:
    rail_count = len(rails)
    output_count = sum(len(rail.outputArtifacts) for rail in rails)
    return _ReachabilityStorageCensus(
        digest_index_units=2 * rail_count + identity_count,
        producer_catalog_units=artifact_count + output_count,
        query_answer_units=2 * query_count,
        graph_units=rail_count + edge_count,
    )


def _rail_variants(
    registry: RailRegistry,
    digests: tuple[str, ...],
) -> dict[str, list[str]]:
    variants: dict[str, list[str]] = defaultdict(list)
    for rail, digest in zip(registry.rails, digests, strict=True):
        variants[rail.identity.key].append(digest)
    return dict(variants)


def _artifact_reachability_queries(
    registry: RailRegistry,
    digests: tuple[str, ...],
) -> tuple[dict[str, str], dict[str, dict[str, None]], int, int]:
    producers: dict[str, dict[str, None]] = defaultdict(dict)
    for rail, digest in zip(registry.rails, digests, strict=True):
        for artifact in rail.outputArtifacts:
            producers[artifact.artifactId][digest] = None

    singleton_queries: dict[str, str] = {}
    shared_queries: dict[str, dict[str, None]] = {}
    for rail, consumer_digest in zip(registry.rails, digests, strict=True):
        for artifact_id in rail.requiredArtifacts:
            for producer_digest in producers.get(artifact_id, ()):
                _add_artifact_query(
                    singleton_queries,
                    shared_queries,
                    consumer_digest,
                    producer_digest,
                )
    query_count = len(singleton_queries) + sum(map(len, shared_queries.values()))
    producer_catalog_units = len(producers) + sum(map(len, producers.values()))
    return singleton_queries, shared_queries, query_count, producer_catalog_units


def _add_artifact_query(
    singleton_queries: dict[str, str],
    shared_queries: dict[str, dict[str, None]],
    consumer_digest: str,
    producer_digest: str,
) -> None:
    shared_consumers = shared_queries.get(producer_digest)
    if shared_consumers is not None:
        shared_consumers[consumer_digest] = None
        return
    existing = singleton_queries.get(producer_digest)
    if existing is None:
        singleton_queries[producer_digest] = consumer_digest
        return
    if existing == consumer_digest:
        return
    del singleton_queries[producer_digest]
    shared_queries[producer_digest] = {existing: None, consumer_digest: None}


def _resolve_singleton_queries(
    context: _ReachabilityContext,
    queries: dict[str, str],
    answers: dict[_AnswerKey, bool],
) -> bool:
    if not queries:
        return True
    incoming = _incoming_edges(context.registry, context.digests, context.variants)
    while queries:
        prerequisite, consumer = queries.popitem()
        complete, reachable = _search_prerequisites(
            consumer,
            prerequisite,
            incoming,
            context.budget.reserve_search_state,
            context.budget.reserve_operation,
        )
        if not complete:
            return False
        answers[(consumer, prerequisite)] = reachable
    return True


def _resolve_shared_queries(
    context: _ReachabilityContext,
    queries: dict[str, dict[str, None]],
    answers: dict[_AnswerKey, bool],
) -> bool:
    if not queries:
        return True
    dependants = _dependant_edges(context.registry, context.digests, context.variants)
    while queries:
        prerequisite, consumers = queries.popitem()
        complete, remaining = _search_dependants(
            prerequisite,
            consumers,
            dependants,
            context.budget.reserve_search_state,
            context.budget.reserve_operation,
        )
        if not complete:
            return False
        while consumers:
            consumer, _ = consumers.popitem()
            answers[(consumer, prerequisite)] = consumer not in remaining
    return True


def _incomplete_reachability(
    budget: _ReachabilityBudget,
    query_count: int,
) -> _ReachabilityMeasurement:
    return _ReachabilityMeasurement(
        operation_units=budget.operation_units,
        storage_units=budget.census.peak(budget.max_search_identities),
        complete=False,
        query_count=query_count,
        max_search_identities=budget.max_search_identities,
        census=budget.census,
        answers=MappingProxyType({}),
    )


def _incoming_edges(
    registry: RailRegistry,
    digests: tuple[str, ...],
    variants: dict[str, list[str]],
) -> dict[str, list[str]]:
    incoming = {digest: [] for digest in digests}
    for rail, digest in zip(registry.rails, digests, strict=True):
        for prerequisite in rail.prerequisites:
            incoming[digest].extend(variants.get(prerequisite.key, ()))
    return incoming


def _dependant_edges(
    registry: RailRegistry,
    digests: tuple[str, ...],
    variants: dict[str, list[str]],
) -> dict[str, list[str]]:
    dependants = {digest: [] for digest in digests}
    for rail, consumer_digest in zip(registry.rails, digests, strict=True):
        for prerequisite in rail.prerequisites:
            for prerequisite_digest in variants.get(prerequisite.key, ()):
                dependants[prerequisite_digest].append(consumer_digest)
    return dependants


def _search_prerequisites(
    consumer: str,
    prerequisite: str,
    incoming: dict[str, list[str]],
    reserve_search_state: _ReserveSearchState,
    reserve_operation: _ReserveOperation,
) -> tuple[bool, bool]:
    if consumer == prerequisite:
        return True, True
    if not reserve_search_state(2):
        return False, False
    pending = deque((consumer,))
    seen = {consumer}
    while pending:
        if not reserve_operation(len(seen) + len(pending)):
            return False, False
        current = pending.popleft()
        for candidate in incoming.get(current, ()):
            growth = 2 * int(candidate not in seen and candidate != prerequisite)
            if not reserve_operation(len(seen) + len(pending) + growth):
                return False, False
            if candidate == prerequisite:
                return True, True
            if candidate not in seen:
                seen.add(candidate)
                pending.append(candidate)
    return True, False


def _search_dependants(
    prerequisite: str,
    targets: Mapping[str, None],
    dependants: dict[str, list[str]],
    reserve_search_state: _ReserveSearchState,
    reserve_operation: _ReserveOperation,
) -> tuple[bool, set[str]]:
    remaining_count = len(targets) - int(prerequisite in targets)
    if not remaining_count:
        return True, set()
    if not reserve_search_state(remaining_count + 2):
        return False, set()
    remaining = set(targets)
    remaining.discard(prerequisite)
    pending = deque((prerequisite,))
    seen = {prerequisite}
    while pending and remaining:
        state_size = len(remaining) + len(seen) + len(pending)
        if not reserve_operation(state_size):
            return False, set()
        current = pending.popleft()
        for candidate in dependants.get(current, ()):
            growth = 2 * int(candidate not in seen)
            shrink = int(candidate in remaining)
            next_size = len(remaining) + len(seen) + len(pending) + growth - shrink
            if not reserve_operation(next_size):
                return False, set()
            if candidate in seen:
                continue
            seen.add(candidate)
            remaining.discard(candidate)
            pending.append(candidate)
    return True, remaining
