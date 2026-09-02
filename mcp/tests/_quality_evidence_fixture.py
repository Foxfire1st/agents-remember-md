"""Canonical published-quality fixture for acceptance-evidence consumer tests."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from pathlib import Path

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles import (
    AdmittedRepositoryProfileExecution,
    admit_repository_profile_execution,
    load_repository_profile,
)
from agents_remember.worktrees.modules.git import require_git
from agents_remember.worktrees.modules.quality import clean_executor as clean_quality_executor
from agents_remember.worktrees.modules.quality import gate as code_quality_gate


def _write_passing_artifacts(
    export: Path,
    profile_execution: AdmittedRepositoryProfileExecution,
) -> None:
    """Materialize exactly the admitted profile's required passing publications."""

    decoder = profile_execution.decoder
    for artifact in profile_execution.published_artifacts:
        if not artifact.required and artifact.path != decoder.artifactPath:
            continue
        destination = export / artifact.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if artifact.path == decoder.artifactPath:
            payload = {
                decoder.statusField: decoder.passedValue,
                decoder.exitCodeField: 0,
            }
            destination.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        elif artifact.mediaType == "application/json":
            destination.write_text("{}\n", encoding="utf-8")
        elif artifact.mediaType.startswith("text/"):
            destination.write_text("fixture publication\n", encoding="utf-8")
        else:
            destination.write_bytes(b"fixture publication\n")


def publish_passing_quality_gate(
    target: code_quality_gate.QualityGateTarget,
    *,
    diff_base: str = "",
    plan: code_quality_gate.QualityGatePlan | None = None,
    invocation: str = "closeout-staged",
    attestation: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Publish the same immutable evidence surface a successful mocked gate promises."""

    del invocation
    candidate_tree = require_git(target.code_worktree, ["write-tree"])
    admitted = load_repository_profile(
        target.repository_id,
        target.code_worktree,
        target.profile_reference,
    )
    profile_execution = admit_repository_profile_execution(
        admitted,
        purpose="closeout",
        mode=(plan or code_quality_gate.QualityGatePlan()).mode,
        candidate_identity=CandidateIdentity(kind="git-tree", value=candidate_tree),
    )
    with tempfile.TemporaryDirectory() as temporary:
        export = Path(temporary)
        _write_passing_artifacts(export, profile_execution)
        clean_quality_executor._publish_reports(  # pyright: ignore[reportPrivateUsage]
            export,
            target.worktree_group / "reports",
            candidate_tree=candidate_tree,
            profile_execution=profile_execution,
            attestation=attestation,
        )
    return {
        "required": True,
        "passed": True,
        "command": "fixture: published passing Dagger generation",
        "diffBase": diff_base,
        "candidateTree": candidate_tree,
    }
