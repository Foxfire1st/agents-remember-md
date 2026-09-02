"""Dependency-owner and stale-index tests for the R06 scope compiler."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from agents_remember.memory_quality.incremental_scope.errors import ScopeUnprovenError
from agents_remember.memory_quality.incremental_scope.models import (
    GitTreeDelta,
    ScopeCandidateIdentity,
    ScopeEdge,
    ScopeNode,
    TaskObservationPair,
    canonical_digest,
)
from agents_remember.memory_quality.incremental_scope.owners import (
    extract_citation_edges,
    extract_entity_edges,
    extract_file_sidecar_edges,
    extract_governing_route_edges,
    extract_route_index_edges,
    observe_source_index,
)
from agents_remember.memory_quality.style.citations import source_index
from agents_remember.memory_quality.style.citations.source_index_state import (
    Manifest,
    ReadyGeneration,
)
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity


def _candidate(code: Path, memory: Path) -> ScopeCandidateIdentity:
    pair = MemoryCandidatePairIdentity(
        repoId="repo",
        contractPath="/coord/tasks/repo/master/enclosures/leaf/series-contract.md",
        contractDigest="a" * 64,
        codeRoot=code.resolve().as_posix(),
        memoryRoot=memory.resolve().as_posix(),
        codeSourceBranch="main",
        codeWorkBranch="work",
        codeBaseCommit="1" * 40,
        memorySourceBranch="memory",
        memoryWorkBranch="work-memory",
        memoryBaseCommit="2" * 40,
        onboardingRoot=(memory / "onboarding").resolve().as_posix(),
        ledgerPath=(memory / "memory.md").resolve().as_posix(),
    )
    return ScopeCandidateIdentity(
        pairIdentity=pair,
        code=GitTreeDelta(
            namespace="code",
            root=pair.codeRoot,
            baseTree="3" * 40,
            candidateTree="4" * 40,
            changes=(),
        ),
        memory=GitTreeDelta(
            namespace="memory",
            root=pair.memoryRoot,
            baseTree="5" * 40,
            candidateTree="6" * 40,
            changes=(),
        ),
        task=TaskObservationPair(base=None, candidate=None),
    )


def _node(node_id: str) -> ScopeNode:
    return ScopeNode(
        nodeId=node_id,
        contentDigest=canonical_digest(node_id),
        authorityNamespace=(
            "agents-remember.git-code-tree"
            if node_id.startswith("code:")
            else "agents-remember.git-memory-tree"
        ),
        validatorVersion="fixture/v1",
        reasons=("fixture owner output",),
    )


def test_all_five_owner_extractors_emit_exact_content_addressed_edges(tmp_path: Path) -> None:
    code = tmp_path / "code"
    memory = tmp_path / "memory"
    onboarding = memory / "onboarding"
    source = code / "src/pkg/a.py"
    source.parent.mkdir(parents=True)
    source.write_text("def anchor():\n    return 1\n", encoding="utf-8")
    onboarding.mkdir(parents=True)
    (onboarding / "claims.md").write_text(
        "Claim cit:([`anchor`], src/pkg/a.py:1-2).\n",
        encoding="utf-8",
    )
    (onboarding / "entities.md").write_text(
        "\n".join(
            [
                "## Entity Fingerprints",
                "",
                "| Entity | Algorithm | Fingerprint | Evidence Paths |",
                "| --- | --- | --- | --- |",
                "| Thing | git-blob-set-sha256-v1 | abc | `src/pkg/a.py` |",
                "",
            ]
        ),
        encoding="utf-8",
    )
    nodes = {
        node.nodeId: node
        for node in (
            _node("code:src/pkg/a.py"),
            _node("memory:onboarding/src/pkg/a.py.md"),
            _node("memory:onboarding/src/overview.md"),
            _node("memory:onboarding/src/overview.index.json"),
            _node("memory:onboarding/src/pkg/overview.md"),
            _node("memory:onboarding/src/pkg/overview.index.json"),
            _node("memory:onboarding/claims.md"),
            _node("memory:onboarding/entities.md"),
        )
    }
    route_indexes: dict[str, dict[str, object]] = {
        "src/overview.index.json": {
            "route": "src",
            "overview": "src/overview.md",
            "coveredFiles": [],
            "childRoutes": [{"index": "src/pkg/overview.index.json"}],
        },
        "src/pkg/overview.index.json": {
            "route": "src/pkg",
            "overview": "src/pkg/overview.md",
            "coveredFiles": ["src/pkg/a.py"],
            "childRoutes": [],
        },
    }
    candidate = _candidate(code, memory)

    edges = (
        *extract_file_sidecar_edges(route_indexes, nodes),
        *extract_governing_route_edges(route_indexes, nodes),
        *extract_citation_edges(candidate, onboarding, nodes),
        *extract_entity_edges(onboarding, nodes),
        *extract_route_index_edges(route_indexes, nodes),
    )

    classes = {edge.edgeClass for edge in edges}
    assert classes == {
        "source-to-file-sidecar",
        "source-to-governing-route",
        "source-to-citing-memory-document",
        "source-to-entity-manifestation",
        "route-index-dependency",
    }
    assert {
        (edge.source, edge.target)
        for edge in edges
        if edge.edgeClass == "source-to-governing-route"
    } == {
        ("code:src/pkg/a.py", "memory:onboarding/src/overview.md"),
        ("code:src/pkg/a.py", "memory:onboarding/src/pkg/overview.md"),
    }
    assert all(edge.contentDigest == canonical_digest(_edge_payload(edge)) for edge in edges)


def _edge_payload(edge: ScopeEdge) -> dict[str, object]:
    value = edge.model_dump(mode="json")
    value.pop("contentDigest")
    return value


def _fake_index(
    root: Path,
    *,
    snapshot: str,
    code_root: str,
    memory_root: str,
) -> source_index.RepositoryIndex:
    cache = root / "cache"
    cache.mkdir()
    readiness = cache / "ready.json"
    manifest_path = cache / "manifest.json"
    readiness.write_text(
        ReadyGeneration(
            generation_id="b" * 64,
            snapshot_id=snapshot,
            code_root=code_root,
            memory_root=memory_root,
            files_indexed=0,
            source_bytes=0,
            database_bytes=1,
        ).to_json(),
        encoding="utf-8",
    )
    manifest_path.write_text(
        Manifest(
            code_root=code_root,
            memory_root=memory_root,
            snapshot_id=snapshot,
            source_bytes=0,
            directories=(),
            files=(),
        ).to_json(),
        encoding="utf-8",
    )
    return cast(
        source_index.RepositoryIndex,
        SimpleNamespace(
            paths=SimpleNamespace(readiness=readiness, manifest=manifest_path),
            snapshot_id=snapshot,
            files_indexed=0,
            source_bytes=0,
        ),
    )


def test_source_index_accepts_exact_empty_candidate_and_refuses_stale_population(
    tmp_path: Path,
) -> None:
    code = tmp_path / "code"
    memory = tmp_path / "memory"
    code.mkdir()
    (memory / "onboarding").mkdir(parents=True)
    candidate = _candidate(code, memory)
    index = _fake_index(
        tmp_path,
        snapshot="c" * 64,
        code_root=code.resolve().as_posix(),
        memory_root=memory.resolve().as_posix(),
    )

    observed = observe_source_index(candidate, index)
    assert observed.snapshotId == "c" * 64
    assert observed.candidateDigest == candidate.digest

    (code / "new.py").write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(ScopeUnprovenError) as caught:
        observe_source_index(candidate, index)
    assert caught.value.failure.code == "source-index-stale"


def test_source_index_refuses_malformed_or_wrong_root_generation(tmp_path: Path) -> None:
    code = tmp_path / "code"
    memory = tmp_path / "memory"
    code.mkdir()
    (memory / "onboarding").mkdir(parents=True)
    candidate = _candidate(code, memory)
    index = _fake_index(
        tmp_path,
        snapshot="d" * 64,
        code_root=(tmp_path / "other").resolve().as_posix(),
        memory_root=memory.resolve().as_posix(),
    )

    with pytest.raises(ScopeUnprovenError) as caught:
        observe_source_index(candidate, index)
    assert caught.value.failure.code == "source-index-stale"

    index.paths.readiness.write_text("not-json", encoding="utf-8")
    with pytest.raises(ScopeUnprovenError) as caught:
        observe_source_index(candidate, index)
    assert caught.value.failure.code == "source-index-malformed"
