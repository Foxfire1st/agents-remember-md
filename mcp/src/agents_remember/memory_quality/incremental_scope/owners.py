"""Read-only adapters over the existing dependency fact owners."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

from agents_remember.kernel.coordination_context.models import StorageSettings
from agents_remember.kernel.route_index import build_route_indexes
from agents_remember.memory_quality.integrity.onboarding_drift_check.entities import (
    parse_entity_fingerprint_rows,
)
from agents_remember.memory_quality.style.citations import claim_reopen, source_index
from agents_remember.memory_quality.style.citations.resolution import Trees
from agents_remember.memory_quality.style.citations.source_index_state import (
    Manifest as CitationIndexManifest,
)
from agents_remember.memory_quality.style.citations.source_index_state import ReadyGeneration
from agents_remember.worktrees.modules.git import require_git

from .compiler import build_dependency_snapshot, dependency_edge
from .errors import ScopeFailure, ScopeUnprovenError
from .models import (
    DependencySnapshot,
    EdgeClass,
    GitTreeDelta,
    ScopeCandidateIdentity,
    ScopeEdge,
    ScopeNode,
    SourceIndexObservation,
    canonical_digest,
)


@dataclass(frozen=True)
class DependencyOwnerContext:
    code_repository: Path
    memory_repository: Path
    onboarding_root: Path
    repository_name: str
    storage: StorageSettings
    citation_index: source_index.RepositoryIndex


@dataclass(frozen=True)
class ContractDependencyAuthority:
    """The one production adapter allowed to compose the five owner populations."""

    context: DependencyOwnerContext

    def observe(self, candidate: ScopeCandidateIdentity) -> DependencySnapshot:
        return observe_dependency_snapshot(candidate, self.context)


def observe_dependency_snapshot(
    candidate: ScopeCandidateIdentity,
    context: DependencyOwnerContext,
) -> DependencySnapshot:
    """Compose exact outputs from Git, route, citation, entity, and index owners."""

    _require_roots(
        candidate,
        context.code_repository,
        context.memory_repository,
        context.onboarding_root,
    )
    index_observation = observe_source_index(candidate, context.citation_index)
    nodes = {
        node.nodeId: node
        for node in (
            *observe_git_nodes(context.code_repository, candidate.code),
            *observe_git_nodes(context.memory_repository, candidate.memory),
        )
    }
    route_indexes = _current_route_indexes(
        context.code_repository,
        context.onboarding_root,
        context.repository_name,
        context.storage,
    )
    edges = [
        *extract_file_sidecar_edges(route_indexes, nodes),
        *extract_governing_route_edges(route_indexes, nodes),
        *extract_citation_edges(candidate, context.onboarding_root, nodes),
        *extract_entity_edges(context.onboarding_root, nodes),
        *extract_route_index_edges(route_indexes, nodes),
    ]
    return build_dependency_snapshot(
        candidate=candidate,
        source_index=index_observation,
        nodes=nodes.values(),
        edges=edges,
    )


def observe_source_index(
    candidate: ScopeCandidateIdentity,
    index: source_index.RepositoryIndex,
) -> SourceIndexObservation:
    """Bind one leased existing source-index generation to the exact candidate roots."""

    try:
        ready = ReadyGeneration.from_json(index.paths.readiness)
        manifest = CitationIndexManifest.from_json(index.paths.manifest)
    except (OSError, ValueError) as exc:
        _refuse(
            "source-index-malformed",
            f"citation source-index generation is unreadable: {type(exc).__name__}",
            snapshot=index.snapshot_id,
        )
    expected = (
        candidate.code.root,
        candidate.memory.root,
        index.snapshot_id,
    )
    observed = (ready.code_root, ready.memory_root, ready.snapshot_id)
    if (
        observed != expected
        or manifest.snapshot_id != ready.snapshot_id
        or manifest.code_root != ready.code_root
        or manifest.memory_root != ready.memory_root
        or ready.files_indexed != len(manifest.files)
        or ready.source_bytes != manifest.source_bytes
        or index.files_indexed != ready.files_indexed
        or index.source_bytes != ready.source_bytes
    ):
        _refuse(
            "source-index-stale",
            "citation source-index generation does not match exact candidate roots",
            snapshot=ready.snapshot_id,
        )
    if (
        index.candidate_tree != candidate.code.candidateTree
        or ready.candidate_tree != candidate.code.candidateTree
        or manifest.candidate_tree != candidate.code.candidateTree
    ):
        _refuse(
            "source-index-candidate-mismatch",
            "citation source-index selection does not name the exact Git candidate tree",
            snapshot=ready.snapshot_id,
        )
    _require_index_matches_candidate(candidate, manifest)
    return SourceIndexObservation(
        snapshotId=ready.snapshot_id,
        codeRoot=ready.code_root,
        memoryRoot=ready.memory_root,
        candidateDigest=candidate.digest,
    )


def observe_git_nodes(repository: Path, delta: GitTreeDelta) -> tuple[ScopeNode, ...]:
    """Read content addresses from exact Git trees, never mtimes or caller filenames."""

    candidate = _tree_entries(repository, delta.candidateTree)
    base = _tree_entries(repository, delta.baseTree)
    paths = set(candidate)
    for change in delta.changes:
        if change.oldPath is not None:
            paths.add(change.oldPath)
        if change.newPath is not None:
            paths.add(change.newPath)
    authority = f"agents-remember.git-{delta.namespace}-tree"
    nodes = []
    for path in sorted(paths):
        blob = candidate.get(path) or base.get(path)
        if blob is None:
            _refuse("git-node-missing", "Git diff root has no blob in either exact tree", node=path)
        nodes.append(
            ScopeNode(
                nodeId=f"{delta.namespace}:{path}",
                contentDigest=canonical_digest({"gitBlob": blob}),
                authorityNamespace=authority,
                validatorVersion=delta.ownerVersion,
                reasons=("exact Git tree member",),
            )
        )
    return tuple(nodes)


def extract_file_sidecar_edges(
    route_indexes: dict[str, dict[str, object]],
    nodes: dict[str, ScopeNode],
) -> tuple[ScopeEdge, ...]:
    """Use canonical route-index coverage to map sources to their 1:1 sidecars."""

    pairs: set[tuple[str, str]] = set()
    for document in route_indexes.values():
        for source in _strings(document, "coveredFiles"):
            target = f"memory:onboarding/{source}.md"
            if f"code:{source}" in nodes and target in nodes:
                pairs.add((f"code:{source}", target))
    return _edges("source-to-file-sidecar", pairs, "canonical coveredFiles sidecar mapping")


def extract_governing_route_edges(
    route_indexes: dict[str, dict[str, object]],
    nodes: dict[str, ScopeNode],
) -> tuple[ScopeEdge, ...]:
    """Map every indexed source to the complete existing governing overview chain."""

    overview_by_route = {
        str(document["route"]): f"memory:onboarding/{document['overview']}"
        for document in route_indexes.values()
    }
    pairs: set[tuple[str, str]] = set()
    for document in route_indexes.values():
        for source in _strings(document, "coveredFiles"):
            for route in _governing_routes(source, overview_by_route):
                target = overview_by_route[route]
                if f"code:{source}" in nodes and target in nodes:
                    pairs.add((f"code:{source}", target))
    return _edges("source-to-governing-route", pairs, "canonical governing route chain")


def extract_citation_edges(
    candidate: ScopeCandidateIdentity,
    onboarding_root: Path,
    nodes: dict[str, ScopeNode],
) -> tuple[ScopeEdge, ...]:
    """Parse existing citation grammar and map each exact source to its citing document."""

    trees = Trees(
        code_root=Path(candidate.code.root),
        memory_root=Path(candidate.memory.root),
        candidate_tree=candidate.code.candidateTree,
    )
    pairs: set[tuple[str, str]] = set()
    for document in sorted(onboarding_root.rglob("*.md")):
        relative = document.relative_to(Path(candidate.memory.root)).as_posix()
        target = f"memory:{relative}"
        if target not in nodes:
            continue
        _, claims = claim_reopen.claims_in(document)
        for claim in claims:
            for citation in claim.citations:
                source = _citation_node(trees, citation.path, nodes)
                if source is not None:
                    pairs.add((source, target))
    return _edges(
        "source-to-citing-memory-document",
        pairs,
        "canonical table/prose citation source",
    )


def extract_entity_edges(
    onboarding_root: Path,
    nodes: dict[str, ScopeNode],
) -> tuple[ScopeEdge, ...]:
    """Map entity fingerprint evidence paths to the repository entity catalog."""

    catalog = onboarding_root / "entities.md"
    target = "memory:onboarding/entities.md"
    if not catalog.is_file() or target not in nodes:
        return ()
    pairs = {
        (f"code:{path}", target)
        for row in parse_entity_fingerprint_rows(catalog)
        for path in row.evidence_paths
        if f"code:{path}" in nodes
    }
    return _edges("source-to-entity-manifestation", pairs, "entity fingerprint evidence path")


def extract_route_index_edges(
    route_indexes: dict[str, dict[str, object]],
    nodes: dict[str, ScopeNode],
) -> tuple[ScopeEdge, ...]:
    """Bind overview, sidecar, and child-index inputs to their generated route index."""

    pairs: set[tuple[str, str]] = set()
    for index_rel, document in route_indexes.items():
        target = f"memory:onboarding/{index_rel}"
        inputs = [f"memory:onboarding/{document['overview']}"]
        inputs.extend(
            f"memory:onboarding/{source}.md" for source in _strings(document, "coveredFiles")
        )
        children = document.get("childRoutes", [])
        if not isinstance(children, list):
            _refuse("route-index-malformed", "route index childRoutes must be an array")
        for child in cast(list[object], children):
            if isinstance(child, dict) and isinstance(child.get("index"), str):
                inputs.append(f"memory:onboarding/{child['index']}")
        pairs.update((source, target) for source in inputs if source in nodes and target in nodes)
    return _edges("route-index-dependency", pairs, "canonical route-index input")


def _current_route_indexes(
    code_root: Path,
    onboarding_root: Path,
    repository: str,
    storage: StorageSettings,
) -> dict[str, dict[str, object]]:
    result = build_route_indexes(
        code_root=code_root,
        onboarding_root=onboarding_root,
        repository=repository,
        storage=storage,
        dry_run=True,
    )
    if result.stale_indexes:
        _refuse(
            "route-index-stale",
            f"canonical route index owner reports stale indexes: {result.stale_indexes[:3]}",
            owner="agents-remember.route-index",
        )
    expected = set(result.indexes)
    actual = {
        path.relative_to(onboarding_root).as_posix()
        for path in onboarding_root.rglob("overview.index.json")
        if path.is_file()
    }
    if actual != expected:
        _refuse(
            "route-index-stale",
            "canonical route-index population has missing or extra documents",
            owner="agents-remember.route-index",
        )
    documents: dict[str, dict[str, object]] = {}
    for relative in result.indexes:
        try:
            payload = json.loads((onboarding_root / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _refuse("route-index-malformed", f"route index is unreadable: {type(exc).__name__}")
        if not isinstance(payload, dict):
            _refuse("route-index-malformed", "route index root must be an object")
        documents[relative] = payload
    return documents


def _tree_entries(repository: Path, tree: str) -> dict[str, str]:
    raw = require_git(repository, ["ls-tree", "-r", "-z", tree])
    entries: dict[str, str] = {}
    records = raw.removesuffix("\0").split("\0") if raw else ()
    for record in records:
        metadata, path = record.split("\t", 1)
        _, kind, object_id = metadata.split(" ", 2)
        if kind == "blob":
            entries[path] = object_id
    return entries


def _require_index_matches_candidate(
    candidate: ScopeCandidateIdentity,
    manifest: CitationIndexManifest,
) -> None:
    """Cross-check the index owner's file census against the exact Git candidate tree."""

    trees = Trees(
        code_root=Path(candidate.code.root),
        memory_root=Path(candidate.memory.root),
        candidate_tree=candidate.code.candidateTree,
    )
    try:
        current = {relative: absolute for absolute, relative in source_index.code_files(trees)}
    except (OSError, RuntimeError, ValueError) as exc:
        _refuse(
            "source-index-candidate-mismatch",
            f"citation source candidate census is unavailable: {type(exc).__name__}",
            snapshot=manifest.snapshot_id,
        )
    indexed = {one.identity.path: one for one in manifest.files}
    if set(current) != set(indexed) or len(indexed) != len(manifest.files):
        _refuse(
            "source-index-stale",
            "citation source-index file population differs from its canonical current census",
            snapshot=manifest.snapshot_id,
        )
    for relative, path in sorted(current.items()):
        try:
            candidate_blob = require_git(
                Path(candidate.code.root),
                ["rev-parse", f"{candidate.code.candidateTree}:{relative}"],
            )
            current_blob = require_git(
                Path(candidate.code.root),
                ["hash-object", "--", relative],
            )
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, RuntimeError, ValueError) as exc:
            _refuse(
                "source-index-candidate-mismatch",
                f"source-index member is unavailable from the exact Git tree: {type(exc).__name__}",
                node=relative,
                snapshot=manifest.snapshot_id,
            )
        if candidate_blob != current_blob or content != indexed[relative].content_sha256:
            _refuse(
                "source-index-candidate-mismatch",
                "citation source-index member differs from the exact Git candidate tree",
                node=relative,
                snapshot=manifest.snapshot_id,
            )


def _edges(
    edge_class: EdgeClass,
    pairs: Iterable[tuple[str, str]],
    reason: str,
) -> tuple[ScopeEdge, ...]:
    return tuple(
        dependency_edge(
            source,
            target,
            edge_class,
            (reason,),
        )
        for source, target in sorted(set(pairs))
    )


def _governing_routes(source: str, routes: dict[str, str]) -> tuple[str, ...]:
    parts = source.split("/")[:-1]
    candidates = ["/".join(parts[:length]) for length in range(len(parts), -1, -1)]
    return tuple(route for route in candidates if route in routes)


def _citation_node(trees: Trees, path: str, nodes: dict[str, ScopeNode]) -> str | None:
    resolved = trees.resolve(path)
    if resolved is None:
        return None
    code = Path(trees.code_root).resolve()
    memory = Path(trees.memory_root).resolve()
    exact = resolved.resolve()
    if exact.is_relative_to(code):
        node = f"code:{exact.relative_to(code).as_posix()}"
    elif exact.is_relative_to(memory):
        node = f"memory:{exact.relative_to(memory).as_posix()}"
    else:
        _refuse("citation-source-escape", "citation source escaped exact candidate roots")
    return node if node in nodes else None


def _strings(document: dict[str, object], key: str) -> tuple[str, ...]:
    value = document.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _refuse("route-index-malformed", f"route index {key} must be a string array")
    return tuple(cast(list[str], value))


def _require_roots(
    candidate: ScopeCandidateIdentity,
    code_repository: Path,
    memory_repository: Path,
    onboarding_root: Path,
) -> None:
    observed = (
        code_repository.resolve().as_posix(),
        memory_repository.resolve().as_posix(),
        onboarding_root.resolve().as_posix(),
    )
    expected = (
        candidate.code.root,
        candidate.memory.root,
        candidate.pairIdentity.onboardingRoot,
    )
    if observed != expected:
        _refuse("dependency-root-mismatch", "dependency owners received roots from another pair")


def _refuse(
    code: str,
    detail: str,
    *,
    node: str | None = None,
    snapshot: str | None = None,
    owner: str | None = None,
) -> NoReturn:
    raise ScopeUnprovenError(
        ScopeFailure(code=code, detail=detail, node=node, snapshot=snapshot, owner=owner)
    )


__all__ = [
    "ContractDependencyAuthority",
    "DependencyOwnerContext",
    "extract_citation_edges",
    "extract_entity_edges",
    "extract_file_sidecar_edges",
    "extract_governing_route_edges",
    "extract_route_index_edges",
    "observe_dependency_snapshot",
    "observe_git_nodes",
    "observe_source_index",
]
