"""Verify the repository-profile decision before starting ambient replications."""

from __future__ import annotations

from pathlib import Path

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.source_selection.git import (
    observe_candidate_source_selection,
)
from agents_remember.certification.repository_profiles.source_selection.models import (
    RailSourceSelection,
)
from agents_remember.certification.repository_profiles.source_selection.reader import (
    read_rail_source_selection,
)


def admitted_selection(
    path: Path, repository: Path, candidate_tree: str, *, mode: str, diff_base: str
) -> RailSourceSelection:
    decision = read_rail_source_selection(path)
    if decision.declaration.selectorId != "ambient-role-dependencies" or decision.mode != mode:
        raise ValueError("ambient source selection belongs to another scenario or mode")
    if decision.applicability.status != "applicable":
        raise ValueError("a non-applicable ambient scenario must not start")
    observed = observe_candidate_source_selection(
        repository, CandidateIdentity(kind="git-tree", value=candidate_tree), diff_base
    )
    if observed != decision.sourceSelection:
        raise ValueError("ambient source selection differs from the actual candidate and base")
    return decision
