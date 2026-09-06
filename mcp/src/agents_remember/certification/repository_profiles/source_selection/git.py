"""Observe a complete path delta through the canonical Git command owner."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.source_selection.compilation import (
    requires_source_selection,
)
from agents_remember.certification.repository_profiles.source_selection.models import (
    CandidateSourceSelection,
)
from agents_remember.errors import CertificationProfileError
from agents_remember.kernel.git_command import run_git

if TYPE_CHECKING:
    from agents_remember.certification.repository_profiles.authority import (
        AdmittedRepositoryProfile,
    )
    from agents_remember.certification.repository_profiles.models import RepositoryProfileSelection

MAX_SOURCE_SELECTION_BYTES = 16 * 1024 * 1024


def _git(root: Path, arguments: list[str]) -> str:
    result = run_git(root, arguments)
    if result.returncode != 0:
        raise ValueError("candidate source selection could not observe its exact Git authority")
    if len(result.stdout.encode()) > MAX_SOURCE_SELECTION_BYTES:
        raise ValueError("candidate source selection exceeds its observation byte bound")
    return result.stdout


def observe_candidate_source_selection(
    repository_root: Path, candidate_identity: CandidateIdentity, diff_base: str
) -> CandidateSourceSelection:
    if candidate_identity.kind != "git-tree":
        raise ValueError("source applicability requires an exact Git tree candidate")
    root = repository_root.resolve(strict=True)
    if Path(_git(root, ["rev-parse", "--show-toplevel"]).strip()).resolve() != root:
        raise ValueError("source applicability requires the actual Git repository root")
    base = _git(
        root, ["rev-parse", "--verify", "--end-of-options", diff_base + "^{commit}"]
    ).strip()
    tree = candidate_identity.value
    if _git(root, ["cat-file", "-t", tree]).strip() != "tree":
        raise ValueError("source applicability candidate identity is not a Git tree")
    base_tree = _git(root, ["rev-parse", base + "^{tree}"]).strip()
    raw = _git(
        root,
        [
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--no-renames",
            "-r",
            "-z",
            base_tree,
            tree,
            "--",
        ],
    )
    if raw and not raw.endswith("\0"):
        raise ValueError("source applicability Git census is truncated")
    paths = tuple(sorted(raw[:-1].split("\0"))) if raw else ()
    payload = {
        "schemaVersion": "candidate-source-selection/v1",
        "baseCommit": base,
        "baseTree": base_tree,
        "candidateTree": tree,
        "changedPaths": paths,
    }
    return CandidateSourceSelection.model_validate(
        {**payload, "selectionDigest": content_digest(payload)}
    )


def observe_profile_source_selection(
    admitted: AdmittedRepositoryProfile,
    selection: RepositoryProfileSelection,
    candidate_identity: CandidateIdentity,
    diff_base: str,
) -> CandidateSourceSelection | None:
    """Observe only a selected, explicitly declared source-applicability input."""
    if not requires_source_selection(admitted.canonical.profile, selection):
        return None
    try:
        return observe_candidate_source_selection(
            admitted.repository_root, candidate_identity, diff_base
        )
    except (OSError, ValueError) as error:
        raise CertificationProfileError(
            "repository source selection observation failed",
            findings=[
                {
                    "code": "source-selection-observation-failed",
                    "path": "sourceSelection",
                    "detail": str(error),
                }
            ],
        ) from error
