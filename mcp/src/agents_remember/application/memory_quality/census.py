"""Publish complete candidate census evidence before certification admission."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from agents_remember.application.memory_scope import MemoryScope
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.kernel.git_command import run_git
from agents_remember.memory_quality.integrity.onboarding_drift_check.discovery import (
    normalize_overview_route,
    parse_table_metadata_text,
)
from agents_remember.memory_quality.memory_census import build_memory_census
from agents_remember.models.lifecycles.curator_coherence import CuratorSourceCandidate
from agents_remember.models.lifecycles.memory_census import MemoryCensusResult, MemoryCensusRow
from agents_remember.worktrees.integration.closeout.memory_census_scope import (
    MemoryCensusScope,
    capture_memory_census_scope,
)
from agents_remember.worktrees.worktree_contract import load_contract


@dataclass(frozen=True)
class PreparedMemoryCensus:
    scope: MemoryCensusScope
    result: MemoryCensusResult


def prepare_memory_census(scope: MemoryScope) -> PreparedMemoryCensus | None:
    """Derive task-owned structural work without requiring any gate result."""
    if scope.pair_identity is None:
        return None
    contract = load_contract(Path(scope.pair_identity.contractPath))
    candidate = capture_memory_census_scope(contract)
    return PreparedMemoryCensus(
        candidate,
        build_memory_census(candidate, settings=scope.context.storage),
    )


def publish_memory_census(
    prepared: PreparedMemoryCensus,
    *,
    detail_limit: int,
) -> dict[str, object]:
    """Retain the full exact worklist and return bounded, noncertifying diagnostics."""
    scope = prepared.scope
    contract = load_contract(Path(scope.pair_identity.contractPath))
    current = capture_memory_census_scope(contract, code_input=scope.code_input)
    if current != scope:
        raise RuntimeError("memory census candidate changed before report publication")
    result = prepared.result
    payload = {
        "schemaVersion": "ar-memory-census/v1",
        "scope": scope.to_payload(),
        "census": result.model_dump(mode="json"),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path = contract.worktree_group / "reports" / "memory-census.json"
    atomic_write_text(path, text)
    limit = max(0, min(detail_limit, 100))
    return {
        "status": "blocked" if result.blockers else "ready-for-adjudication",
        "certified": False,
        "rowCount": len(result.rows),
        "blockerCount": len(result.blockers),
        "unonboardedCount": len(result.unonboarded),
        "rows": [row.model_dump(mode="json") for row in result.rows[:limit]],
        "blockers": [row.model_dump(mode="json") for row in result.blockers[:limit]],
        "unonboarded": list(result.unonboarded[:limit]),
        "reportPath": path.as_posix(),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def census_curator_candidates(prepared: PreparedMemoryCensus) -> tuple[CuratorSourceCandidate, ...]:
    """Adapt every governed identity exactly once into the existing adjudication authority."""
    pair = prepared.scope.pair_identity
    prefix = Path(pair.onboardingRoot).relative_to(Path(pair.memoryRoot)).as_posix() + "/"
    candidates = []
    for row in prepared.result.rows:
        path = row.identity.memoryRootRelativePath
        if not path.startswith(prefix):
            raise ValueError("governed artifact cannot use the onboarding-relative curator address")
        source = _curator_source(prepared, row)
        fields = {
            "sourceFile": source,
            "onboardingFile": path[len(prefix) :],
            "classification": row.identity.artifactType,
        }
        candidate = CuratorSourceCandidate(**fields)
        if candidate.model_dump(mode="json") != fields:
            raise ValueError("governed census identity cannot be represented without normalization")
        candidates.append(candidate)
    identities = [candidate.identity for candidate in candidates]
    if len(identities) != len(set(identities)):
        raise ValueError("governed census rows collapse to an ambiguous curator identity")
    return tuple(candidates)


def _curator_source(prepared: PreparedMemoryCensus, row: MemoryCensusRow) -> str:
    identity = row.identity
    if identity.artifactType == "entity-row":
        return f"entity:{identity.subIdentity}"
    if identity.artifactType == "inline-onboarding":
        if len(row.sourcePaths) != 1:
            raise ValueError("inline census identity requires exactly one canonical source")
        return row.sourcePaths[0]
    scope = prepared.scope
    tree = (
        scope.memory_candidate_tree
        if row.expectedFinalPresence == "present"
        else scope.memory_baseline_commit
    )
    result = run_git(
        Path(scope.pair_identity.memoryRoot),
        ["show", f"{tree}:{identity.memoryRootRelativePath}"],
    )
    if result.returncode != 0:
        raise ValueError("cannot read exact governed artifact for curator candidate mapping")
    metadata = parse_table_metadata_text(result.stdout)
    if identity.artifactType == "route-overview":
        source = normalize_overview_route(metadata.get("sourceRoute", "."))
    else:
        source = metadata.get("path", "")
    if not source:
        raise ValueError("governed artifact lacks its canonical source identity")
    return source
