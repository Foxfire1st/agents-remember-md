"""Compile one declared rail's applicability before any repository rail starts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import RailApplicability
from agents_remember.certification.repository_profiles.source_selection.models import (
    CandidateSourceSelection,
    RailSourceSelection,
    SourcePathApplicability,
    selected_paths,
    selection_identity,
)

if TYPE_CHECKING:
    from agents_remember.certification.repository_profiles.models import (
        RepositoryCertificationProfile,
        RepositoryProfileSelection,
    )


def compile_source_applicability(
    declaration: SourcePathApplicability,
    source: CandidateSourceSelection,
    *,
    mode: str,
    profile_id: str,
    population: str,
) -> RailSourceSelection:
    selected = selected_paths(declaration, source.changedPaths)
    applicable = mode == "full" or bool(selected)
    applicability = RailApplicability(
        profileId=profile_id,
        status="applicable" if applicable else "not-applicable",
        selectionIdentity=selection_identity(declaration, source, mode),
        population=population if applicable else None,
        reason=None if applicable else declaration.notApplicableReason,
    )
    payload = {
        "schemaVersion": "rail-source-selection/v1",
        "declaration": declaration.model_dump(mode="json"),
        "sourceSelection": source.model_dump(mode="json"),
        "mode": mode,
        "selectedPaths": selected,
        "applicability": applicability.model_dump(mode="json"),
    }
    return RailSourceSelection.model_validate(
        {**payload, "decisionDigest": content_digest(payload)}
    )


def requires_source_selection(
    profile: RepositoryCertificationProfile, selection: RepositoryProfileSelection
) -> bool:
    selected_keys = {identity.key for gate in selection.gates for identity in gate.railIds}
    return any(
        rail.sourceApplicability is not None and rail.identity.key in selected_keys
        for rail in profile.rails
    )
