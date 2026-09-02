"""Compile immutable Gate 1-4 plan contributions from one admitted profile."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from typing import Never

from pydantic import BaseModel

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CandidateIdentity,
    CompiledRail,
    RailAdapterDefinition,
    RailApplicability,
    RegistryValidationFinding,
    canonical_execution_waves,
)
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    ProfileMode,
    ProfilePurpose,
    RepositoryGateId,
    RepositoryGatePlan,
    RepositoryGateSelection,
    RepositoryProfilePlan,
    RepositoryProfileSelection,
    RepositoryRailDefinition,
    RepositorySemanticInputNode,
    repository_gate_plan_digest,
)
from agents_remember.certification.repository_profiles.validation import (
    validate_repository_profile,
)
from agents_remember.errors import CertificationProfileError


def resolve_repository_profile_selection(
    canonical: CanonicalRepositoryCertificationProfile,
    *,
    purpose: ProfilePurpose,
    mode: ProfileMode,
) -> RepositoryProfileSelection:
    """Resolve the sole declared purpose/mode selection without repository conventions."""

    matches = tuple(
        selection
        for selection in canonical.profile.selections
        if selection.purpose == purpose and selection.mode == mode
    )
    if len(matches) != 1:
        _raise_profile_error(
            "repository profile selection authority failed",
            (
                RegistryValidationFinding(
                    code=(
                        "required-profile-selection-missing"
                        if not matches
                        else "ambiguous-profile-selection"
                    ),
                    path=f"selections.{purpose}.{mode}",
                    detail="one exact purpose/mode selection is required",
                ),
            ),
        )
    return matches[0]


def compile_repository_profile_plan(
    canonical: CanonicalRepositoryCertificationProfile,
    *,
    selection_id: str,
    candidate_identity: CandidateIdentity,
) -> RepositoryProfilePlan:
    """Compile exact Gates 1-4 without claiming the framework-owned Gate 5."""

    report = validate_repository_profile(canonical)
    if not report.ok:
        _raise_profile_error("repository profile admission failed", report.findings)
    selection = next(
        (item for item in canonical.profile.selections if item.selectionId == selection_id),
        None,
    )
    if selection is None:
        _raise_profile_error(
            "repository profile selection failed",
            (
                RegistryValidationFinding(
                    code="unknown-profile-selection",
                    path=f"selections.{selection_id}",
                    detail="selected repository profile is not declared",
                ),
            ),
        )
    rail_catalog = {rail.identity.key: rail for rail in canonical.profile.rails}
    gate_plans = tuple(
        _compile_gate_plan(
            canonical,
            selection,
            gate,
            rail_catalog,
            candidate_identity,
        )
        for gate in selection.gates
    )
    payload = {
        "schemaVersion": "repository-profile-plan/v1",
        "profileDigest": canonical.profileDigest,
        "candidateIdentity": candidate_identity.model_dump(mode="json"),
        "selectionId": selection.selectionId,
        "purpose": selection.purpose,
        "mode": selection.mode,
        "gates": [gate.model_dump(mode="json") for gate in gate_plans],
    }
    return RepositoryProfilePlan(**payload, planDigest=content_digest(payload))


def admit_repository_profile_plan(
    canonical: CanonicalRepositoryCertificationProfile,
    plan: RepositoryProfilePlan,
    *,
    selection_id: str,
    candidate_identity: CandidateIdentity,
) -> RepositoryProfilePlan:
    """Require caller-held plan bytes to equal the sole canonical compilation."""

    expected = compile_repository_profile_plan(
        canonical,
        selection_id=selection_id,
        candidate_identity=candidate_identity,
    )
    if plan != expected:
        _raise_profile_error(
            "repository profile plan admission failed",
            (
                RegistryValidationFinding(
                    code="repository-profile-plan-not-authorized",
                    path="plan",
                    detail="plan bytes differ from the selected profile and candidate",
                ),
            ),
        )
    return plan


def _compile_gate_plan(
    canonical: CanonicalRepositoryCertificationProfile,
    selection: RepositoryProfileSelection,
    gate: RepositoryGateSelection,
    rail_catalog: dict[str, RepositoryRailDefinition],
    candidate_identity: CandidateIdentity,
) -> RepositoryGatePlan:
    selected_definitions = tuple(rail_catalog[identity.key] for identity in gate.railIds)
    rails = tuple(_compile_rail(rail, selection, gate) for rail in selected_definitions)
    rails = tuple(
        sorted(
            rails,
            key=lambda rail: (
                rail.orderKey,
                rail.identity.railId,
                rail.identity.version,
                rail.definitionDigest,
            ),
        )
    )
    waves = canonical_execution_waves(rails)
    semantic_inputs = (
        _compile_semantic_inputs(
            canonical,
            selection,
            gate,
            selected_definitions,
            _selector_consuming_gates(canonical),
        )
        if gate.status == "applicable"
        else ()
    )
    payload = {
        "schemaVersion": "repository-gate-plan/v1",
        "profileDigest": canonical.profileDigest,
        "candidateIdentity": candidate_identity.model_dump(mode="json"),
        "selectionId": selection.selectionId,
        "purpose": selection.purpose,
        "mode": selection.mode,
        "gate": gate.gate,
        "gatePrerequisites": tuple(range(1, gate.gate)),
        "applicability": gate.status,
        "reason": gate.reason,
        "rails": [rail.model_dump(mode="json") for rail in rails],
        "semanticInputs": [item.model_dump(mode="json") for item in semantic_inputs],
        "waves": [[identity.model_dump(mode="json") for identity in wave] for wave in waves],
    }
    return RepositoryGatePlan(**payload, planDigest=repository_gate_plan_digest(payload))


def _compile_rail(
    rail: RepositoryRailDefinition,
    selection: RepositoryProfileSelection,
    gate: RepositoryGateSelection,
) -> CompiledRail:
    execution = rail.execution
    return CompiledRail(
        identity=rail.identity,
        definitionDigest=content_digest(rail),
        gate=rail.gate,
        railClass=rail.railClass,
        authority="repository-profile",
        ownerClass=rail.ownerClass,
        correctiveOwner=rail.correctiveOwner,
        posture=rail.posture,
        orderKey=rail.orderKey,
        prerequisites=rail.prerequisites,
        requiredArtifacts=rail.requiredArtifacts,
        adapter=RailAdapterDefinition(
            adapterKind=execution.adapterKind,
            adapterId=execution.adapterId,
            configurationDigest=content_digest(execution),
            executionEvidence=execution.executionEvidence,
        ),
        runtimeInputs=rail.runtimeInputs,
        applicability=RailApplicability(
            profileId=selection.selectionId,
            status="applicable",
            selectionIdentity=gate.selectionIdentity,
            population=gate.population,
        ),
        evidenceContract=rail.evidenceContract,
        outputArtifacts=rail.outputArtifacts,
    )


def _compile_semantic_inputs(
    canonical: CanonicalRepositoryCertificationProfile,
    selection: RepositoryProfileSelection,
    gate: RepositoryGateSelection,
    rails: tuple[RepositoryRailDefinition, ...],
    selector_gates: Mapping[str, tuple[RepositoryGateId, ...]],
) -> tuple[RepositorySemanticInputNode, ...]:
    profile = canonical.profile
    nodes: list[RepositorySemanticInputNode] = []
    for rail in rails:
        nodes.extend(
            (
                _semantic_node(
                    "rail-execution",
                    rail.identity.key,
                    (rail.gate,),
                    rail.execution,
                ),
                _semantic_node(
                    "rail-runtime",
                    rail.identity.key,
                    (rail.gate,),
                    rail.runtimeInputs,
                ),
            )
        )
    referenced_selectors = {
        rail.execution.scopeProviderId
        for rail in rails
        if rail.execution.scopeProviderId is not None
    }
    nodes.extend(
        _semantic_node(
            "selector",
            selector.selectorId,
            selector_gates[selector.selectorId],
            selector,
        )
        for selector in profile.selectors
        if selector.selectorId in referenced_selectors
    )
    executor = next(
        item for item in profile.executorAdapters if item.adapterId == selection.executorAdapterId
    )
    if gate.gate in executor.consumingGates:
        nodes.append(
            _semantic_node(
                "executor-adapter",
                executor.adapterId,
                executor.consumingGates,
                executor,
            )
        )
    referenced_decoders = {selection.resultDecoderId} | {
        rail.execution.resultDecoderId for rail in rails
    }
    nodes.extend(
        _semantic_node(
            "result-decoder",
            decoder.decoderId,
            decoder.consumingGates,
            decoder,
        )
        for decoder in profile.resultDecoders
        if decoder.decoderId in referenced_decoders and gate.gate in decoder.consumingGates
    )
    nodes.extend(
        _semantic_node(
            "publication-policy",
            artifact.path,
            artifact.publisherGates,
            artifact,
        )
        for artifact in profile.publishedArtifacts
        if gate.gate in artifact.publisherGates
    )
    catalog = {(node.inputKind, node.inputId): node for node in nodes}
    if len(catalog) != len(nodes):
        raise ValueError("repository semantic input compilation produced duplicate identities")
    return tuple(
        sorted(
            catalog.values(),
            key=lambda item: (item.inputKind, item.inputId, item.contentDigest),
        )
    )


def _semantic_node(
    kind,
    identity: str,
    consuming_gates: tuple[RepositoryGateId, ...],
    content: BaseModel,
) -> RepositorySemanticInputNode:
    payload = content.model_dump(mode="json")
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return RepositorySemanticInputNode(
        inputKind=kind,
        inputId=identity,
        consumingGates=consuming_gates,
        contentDigest=content_digest(payload),
        canonicalJson=canonical_json,
    )


def _selector_consuming_gates(
    canonical: CanonicalRepositoryCertificationProfile,
) -> dict[str, tuple[RepositoryGateId, ...]]:
    observed: defaultdict[str, set[RepositoryGateId]] = defaultdict(set)
    for rail in canonical.profile.rails:
        if rail.execution.scopeProviderId is not None:
            observed[rail.execution.scopeProviderId].add(rail.gate)
    return {selector_id: tuple(sorted(gates)) for selector_id, gates in observed.items()}


def _raise_profile_error(
    detail: str,
    findings: tuple[RegistryValidationFinding, ...],
) -> Never:
    raise CertificationProfileError(
        detail,
        [finding.model_dump(mode="json") for finding in findings],
    )


__all__ = [
    "admit_repository_profile_plan",
    "compile_repository_profile_plan",
    "resolve_repository_profile_selection",
]
