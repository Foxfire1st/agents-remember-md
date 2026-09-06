"""Freeze one fresh or explicitly selected execution in its exact candidate sandbox."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.authority import (
    MAX_PROFILE_BYTES,
    AdmittedRepositoryProfile,
    load_repository_profile,
    resolve_repository_profile_path,
)
from agents_remember.certification.repository_profiles.execution import (
    AdmittedRepositoryProfileExecution,
    admit_repository_profile_execution,
)
from agents_remember.certification.repository_profiles.planning import (
    resolve_repository_profile_selection,
)
from agents_remember.certification.repository_profiles.source_selection.git import (
    observe_profile_source_selection,
)
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.worktrees.modules.quality.dagger_authority import AdmittedDaggerAuthority
from agents_remember.worktrees.modules.quality.execution.retained_reports import (
    snapshot_retained_reports,
)

if TYPE_CHECKING:
    from agents_remember.worktrees.modules.quality.clean_executor import (
        CleanQualityRequest,
        _PreparedSandbox,
    )
    from agents_remember.worktrees.modules.quality.execution.models import (
        CodeCertificationExecution,
    )


def _admit_prepared_profile(
    request: CleanQualityRequest,
    prepared: _PreparedSandbox,
) -> AdmittedRepositoryProfileExecution:
    if request.execution is not None:
        return _selected_profile(request, prepared, request.execution)
    admitted = load_repository_profile(
        request.repository_id,
        prepared.root / "source",
        request.profile_reference,
    )
    candidate = CandidateIdentity(kind="git-tree", value=prepared.candidate_tree)
    selection = resolve_repository_profile_selection(
        admitted.canonical, purpose="closeout", mode=request.mode
    )
    source_selection = observe_profile_source_selection(
        admitted, selection, candidate, request.diff_base
    )
    return admit_repository_profile_execution(
        admitted,
        purpose="closeout",
        mode=request.mode,
        candidate_identity=candidate,
        source_selection=source_selection,
    )


def _selected_profile(
    request: CleanQualityRequest,
    prepared: _PreparedSandbox,
    selected: CodeCertificationExecution,
) -> AdmittedRepositoryProfileExecution:
    selected.validate()
    frozen = selected.run
    plan = frozen.repositoryPlan
    source = prepared.root / "source"
    profile = frozen.repositoryProfile.profile
    if (
        plan.candidateIdentity != CandidateIdentity(kind="git-tree", value=prepared.candidate_tree)
        or plan.mode != request.mode
        or profile.repositoryId != request.repository_id
    ):
        raise ValueError("frozen certification run differs from the exact executor candidate")
    source_path = resolve_repository_profile_path(source, request.profile_reference)
    # Resolve the declared candidate file solely for raw-byte provenance. All semantic
    # authorities below come from the selected frozen run; no reload/reselection occurs.
    with source_path.open("rb") as stream:
        raw = stream.read(MAX_PROFILE_BYTES + 1)
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError("selected candidate profile exceeds its authority byte bound")
    admitted = AdmittedRepositoryProfile(
        request.repository_id,
        source,
        source_path,
        hashlib.sha256(raw).hexdigest(),
        frozen.repositoryProfile,
    )
    selection = next(item for item in profile.selections if item.selectionId == plan.selectionId)
    applicable = {gate.gate for gate in selection.gates if gate.status == "applicable"}
    return AdmittedRepositoryProfileExecution(
        admitted,
        selection,
        plan,
        next(
            item
            for item in profile.executorAdapters
            if item.adapterId == selection.executorAdapterId
        ),
        next(
            item for item in profile.resultDecoders if item.decoderId == selection.resultDecoderId
        ),
        tuple(
            item
            for item in profile.publishedArtifacts
            if applicable.intersection(item.publisherGates)
        ),
    )


def _write_sandbox_manifest(
    request: CleanQualityRequest,
    prepared: _PreparedSandbox,
    execution: AdmittedRepositoryProfileExecution,
    authority: AdmittedDaggerAuthority,
) -> None:
    source = prepared.root / "source"
    observed = observe_profile_source_selection(
        execution.admitted, execution.selection, execution.plan.candidateIdentity, request.diff_base
    )
    if observed != execution.plan.sourceSelection:
        raise ValueError("prepared source selection differs from the frozen comparison authority")
    diff_base = observed.baseCommit if observed is not None else request.diff_base
    manifest = {
        "schemaVersion": "repository-certification-admission/v1",
        "head": prepared.head,
        "stagedOverlaySha256": prepared.staged_overlay_sha256,
        "source": request.code_worktree.as_posix(),
        "bundleSha256": prepared.bundle_sha256,
        "candidateTree": prepared.candidate_tree,
        "diffBase": diff_base,
        "profile": {
            "configuredReference": request.profile_reference.as_posix()
            if request.profile_reference is not None
            else None,
            "sourcePath": execution.admitted.source_path.relative_to(source).as_posix(),
            "sourceSha256": execution.admitted.source_sha256,
            "profileDigest": execution.admitted.canonical.profileDigest,
        },
        "profilePlan": execution.plan.model_dump(mode="json"),
        "executorAdapter": execution.executor.model_dump(mode="json"),
        "resultDecoder": execution.decoder.model_dump(mode="json"),
        "runtimeAuthority": authority.snapshot.as_manifest(),
        "publishedArtifacts": [
            artifact.model_dump(mode="json") for artifact in execution.published_artifacts
        ],
    }
    if request.execution is not None:
        files = snapshot_retained_reports(
            request.execution,
            reports=request.worktree_group / "reports",
            destination=prepared.root / "retained-reports",
        )
        manifest["codeExecution"] = request.execution.payload(diff_base=diff_base)
        manifest["retainedReports"] = [item.payload() for item in files]
    atomic_write_text(
        prepared.root / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
