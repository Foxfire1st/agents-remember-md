"""Admission of one exact repository profile selection for executor use."""

from __future__ import annotations

from dataclasses import dataclass

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.authority import (
    AdmittedRepositoryProfile,
)
from agents_remember.certification.repository_profiles.models import (
    DaggerModuleExecutorDefinition,
    JsonExitStatusDecoderDefinition,
    ProfileMode,
    ProfilePurpose,
    PublishedArtifactDefinition,
    RepositoryProfilePlan,
    RepositoryProfileSelection,
)
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
    resolve_repository_profile_selection,
)


@dataclass(frozen=True)
class AdmittedRepositoryProfileExecution:
    """Exact profile bytes, plan, adapter, and decoder admitted for one candidate."""

    admitted: AdmittedRepositoryProfile
    selection: RepositoryProfileSelection
    plan: RepositoryProfilePlan
    executor: DaggerModuleExecutorDefinition
    decoder: JsonExitStatusDecoderDefinition
    published_artifacts: tuple[PublishedArtifactDefinition, ...]


def admit_repository_profile_execution(
    admitted: AdmittedRepositoryProfile,
    *,
    purpose: ProfilePurpose,
    mode: ProfileMode,
    candidate_identity: CandidateIdentity,
) -> AdmittedRepositoryProfileExecution:
    """Admit one exact candidate/profile execution without naming repository commands."""

    selection = resolve_repository_profile_selection(
        admitted.canonical,
        purpose=purpose,
        mode=mode,
    )
    plan = compile_repository_profile_plan(
        admitted.canonical,
        selection_id=selection.selectionId,
        candidate_identity=candidate_identity,
    )
    executors = {
        definition.adapterId: definition
        for definition in admitted.canonical.profile.executorAdapters
    }
    decoders = {
        definition.decoderId: definition for definition in admitted.canonical.profile.resultDecoders
    }
    applicable_gates = {gate.gate for gate in selection.gates if gate.status == "applicable"}
    return AdmittedRepositoryProfileExecution(
        admitted=admitted,
        selection=selection,
        plan=plan,
        executor=executors[selection.executorAdapterId],
        decoder=decoders[selection.resultDecoderId],
        published_artifacts=tuple(
            artifact
            for artifact in admitted.canonical.profile.publishedArtifacts
            if applicable_gates.intersection(artifact.publisherGates)
        ),
    )


__all__ = [
    "AdmittedRepositoryProfileExecution",
    "admit_repository_profile_execution",
]
