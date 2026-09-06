"""Explicit frozen observation fixtures for compiler and executor contract tests."""

from __future__ import annotations

from pathlib import Path

from agents_remember.certification.digests import content_digest
from agents_remember.certification.repository_profiles.source_selection.compilation import (
    compile_source_applicability,
)
from agents_remember.certification.repository_profiles.source_selection.models import (
    CandidateSourceSelection,
    RailSourceSelection,
    SourcePathApplicability,
)

FIXTURE_BASE = "b" * 40


def source_selection_fixture(
    candidate_tree: str,
    *,
    base_commit: str = FIXTURE_BASE,
    changed_paths: tuple[str, ...] = ("scripts/e2e_harness/run.py",),
) -> CandidateSourceSelection:
    payload = {
        "schemaVersion": "candidate-source-selection/v1",
        "baseCommit": base_commit,
        "baseTree": "d" * 40,
        "candidateTree": candidate_tree,
        "changedPaths": changed_paths,
    }
    return CandidateSourceSelection.model_validate(
        {**payload, "selectionDigest": content_digest(payload)}
    )


def ambient_selection_fixture(*, applicable: bool = True) -> RailSourceSelection:
    """A deliberately declared fixture surface, never claimed as a Git observation."""
    declaration = SourcePathApplicability(
        selectorId="ambient-role-dependencies",
        version="fixture-v1",
        dependencyPrefixes=("scripts/e2e_harness/",),
        evidencePath="source-selection/ambient-role.json",
        notApplicableReason="fixture has no changed scenario source",
    )
    return compile_source_applicability(
        declaration,
        source_selection_fixture(
            "a" * 40, changed_paths=("scripts/e2e_harness/run.py",) if applicable else ()
        ),
        mode="targeted",
        profile_id="fixture",
        population="two fresh scenario replications",
    )


def write_ambient_selection(root: Path, *, applicable: bool = True) -> Path:
    target = root / "source-selection.json"
    target.write_text(ambient_selection_fixture(applicable=applicable).model_dump_json() + "\n")
    return target
