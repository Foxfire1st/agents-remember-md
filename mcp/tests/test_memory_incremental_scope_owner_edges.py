"""Owner-composition, index, route, and confinement edges for R06 scope evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from agents_remember.kernel.coordination_context.models import StorageSettings
from agents_remember.memory_quality.incremental_scope import owners
from agents_remember.memory_quality.incremental_scope.errors import ScopeUnprovenError
from agents_remember.memory_quality.incremental_scope.models import (
    GitPathChange,
    GitTreeDelta,
    ScopeCandidateIdentity,
    ScopeNode,
    SourceIndexObservation,
    TaskObservationPair,
    canonical_digest,
)
from agents_remember.memory_quality.incremental_scope.owners import (
    ContractDependencyAuthority,
    DependencyOwnerContext,
    extract_citation_edges,
    extract_entity_edges,
    extract_route_index_edges,
    observe_dependency_snapshot,
    observe_git_nodes,
)
from agents_remember.memory_quality.style.citations import source_index
from agents_remember.memory_quality.style.citations.resolution import Trees
from agents_remember.memory_quality.style.citations.source_index_state import (
    Identity,
    Manifest,
    SourceFile,
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
        reasons=("fixture",),
    )


def _reason(error: pytest.ExceptionInfo[ScopeUnprovenError]) -> str:
    return error.value.failure.code


def test_dependency_authority_composes_all_existing_owner_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = tmp_path / "code"
    memory = tmp_path / "memory"
    onboarding = memory / "onboarding"
    code.mkdir()
    onboarding.mkdir(parents=True)
    candidate = _candidate(code, memory)
    observed_calls: list[str] = []
    source = SourceIndexObservation(
        snapshotId="b" * 64,
        codeRoot=candidate.code.root,
        memoryRoot=candidate.memory.root,
        candidateDigest=candidate.digest,
    )
    monkeypatch.setattr(owners, "_require_roots", lambda *args: observed_calls.append("roots"))
    monkeypatch.setattr(owners, "observe_source_index", lambda *args: source)
    monkeypatch.setattr(
        owners,
        "observe_git_nodes",
        lambda repository, delta: (
            _node(
                f"{delta.namespace}:{'a.py' if delta.namespace == 'code' else 'onboarding/a.py.md'}"
            ),
        ),
    )
    monkeypatch.setattr(owners, "_current_route_indexes", lambda *args: {})
    for name in (
        "extract_file_sidecar_edges",
        "extract_governing_route_edges",
        "extract_citation_edges",
        "extract_entity_edges",
        "extract_route_index_edges",
    ):
        monkeypatch.setattr(owners, name, lambda *args: ())
    context = DependencyOwnerContext(
        code_repository=code,
        memory_repository=memory,
        onboarding_root=onboarding,
        repository_name="repo",
        storage=StorageSettings(),
        citation_index=cast(source_index.RepositoryIndex, SimpleNamespace()),
    )

    direct = observe_dependency_snapshot(candidate, context)
    delegated = ContractDependencyAuthority(context).observe(candidate)

    assert direct == delegated
    assert {node.nodeId for node in direct.nodes} == {
        "code:a.py",
        "memory:onboarding/a.py.md",
    }
    assert observed_calls == ["roots", "roots"]


def test_git_node_owner_reads_candidate_and_deleted_base_blobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delta = GitTreeDelta(
        namespace="code",
        root=tmp_path.resolve().as_posix(),
        baseTree="1" * 40,
        candidateTree="2" * 40,
        changes=(
            GitPathChange(status="added", newPath="new.py", newBlob="a" * 40),
            GitPathChange(status="deleted", oldPath="old.py", oldBlob="b" * 40),
        ),
    )

    def entries(repository: Path, tree: str) -> dict[str, str]:
        del repository
        return {"new.py": "a" * 40} if tree == delta.candidateTree else {"old.py": "b" * 40}

    monkeypatch.setattr(owners, "_tree_entries", entries)

    nodes = observe_git_nodes(tmp_path, delta)

    assert [node.nodeId for node in nodes] == ["code:new.py", "code:old.py"]

    missing = delta.model_copy(
        update={"changes": (GitPathChange(status="deleted", oldPath="ghost.py", oldBlob="c" * 40),)}
    )
    with pytest.raises(ScopeUnprovenError) as caught:
        observe_git_nodes(tmp_path, missing)
    assert _reason(caught) == "git-node-missing"


def test_citation_extractor_skips_unowned_documents_and_unresolved_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = tmp_path / "code"
    memory = tmp_path / "memory"
    onboarding = memory / "onboarding"
    code.mkdir()
    onboarding.mkdir(parents=True)
    (onboarding / "absent.md").write_text("claim\n", encoding="utf-8")
    (onboarding / "owned.md").write_text("claim\n", encoding="utf-8")
    candidate = _candidate(code, memory)
    citation = SimpleNamespace(path="missing.py")
    claim = SimpleNamespace(citations=(citation,))
    monkeypatch.setattr(owners.claim_reopen, "claims_in", lambda document: (None, (claim,)))
    monkeypatch.setattr(owners, "_citation_node", lambda *args: None)

    assert (
        extract_citation_edges(
            candidate,
            onboarding,
            {"memory:onboarding/owned.md": _node("memory:onboarding/owned.md")},
        )
        == ()
    )


def test_absent_entity_catalog_and_malformed_child_routes_fail_closed(tmp_path: Path) -> None:
    onboarding = tmp_path / "onboarding"
    onboarding.mkdir()
    assert extract_entity_edges(onboarding, {}) == ()
    with pytest.raises(ScopeUnprovenError) as caught:
        extract_route_index_edges(
            {
                "overview.index.json": {
                    "overview": "overview.md",
                    "coveredFiles": [],
                    "childRoutes": "not-an-array",
                }
            },
            {},
        )
    assert _reason(caught) == "route-index-malformed"


def test_current_route_indexes_accept_exact_population_and_parse_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    onboarding = tmp_path / "onboarding"
    onboarding.mkdir()
    relative = "overview.index.json"
    (onboarding / relative).write_text(
        '{"route":"","overview":"overview.md","coveredFiles":[],"childRoutes":[]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        owners,
        "build_route_indexes",
        lambda **kwargs: SimpleNamespace(stale_indexes=[], indexes=(relative,)),
    )

    documents = owners._current_route_indexes(
        tmp_path / "code",
        onboarding,
        "repo",
        StorageSettings(),
    )

    assert documents[relative]["overview"] == "overview.md"


@pytest.mark.parametrize("case", ["stale", "population", "json", "root"])
def test_current_route_indexes_refuse_owner_and_document_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    onboarding = tmp_path / "onboarding"
    onboarding.mkdir()
    relative = "overview.index.json"
    if case == "json":
        (onboarding / relative).write_text("not-json", encoding="utf-8")
    elif case == "root":
        (onboarding / relative).write_text("[]", encoding="utf-8")
    stale = [relative] if case == "stale" else []
    indexes = (relative,) if case != "population" else ("missing/overview.index.json",)
    monkeypatch.setattr(
        owners,
        "build_route_indexes",
        lambda **kwargs: SimpleNamespace(stale_indexes=stale, indexes=indexes),
    )

    with pytest.raises(ScopeUnprovenError) as caught:
        owners._current_route_indexes(
            tmp_path / "code",
            onboarding,
            "repo",
            StorageSettings(),
        )

    assert _reason(caught) in {"route-index-stale", "route-index-malformed"}


def test_tree_entries_keeps_only_recursive_blob_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "\0".join(
        (
            f"100644 blob {'a' * 40}\ta.py",
            f"040000 tree {'b' * 40}\tdirectory",
            "",
        )
    )
    monkeypatch.setattr(owners, "require_git", lambda *args: raw)
    assert owners._tree_entries(tmp_path, "c" * 40) == {"a.py": "a" * 40}
    monkeypatch.setattr(owners, "require_git", lambda *args: "")
    assert owners._tree_entries(tmp_path, "c" * 40) == {}


def test_owner_extractors_cover_repeated_source_route_and_child_populations() -> None:
    nodes = {
        node.nodeId: node
        for node in (
            _node("code:pkg/a.py"),
            _node("code:pkg/b.py"),
            _node("memory:onboarding/pkg/a.py.md"),
            _node("memory:onboarding/pkg/b.py.md"),
            _node("memory:onboarding/overview.md"),
            _node("memory:onboarding/pkg/overview.md"),
            _node("memory:onboarding/overview.index.json"),
            _node("memory:onboarding/pkg/overview.index.json"),
            _node("memory:onboarding/other/overview.index.json"),
        )
    }
    indexes: dict[str, dict[str, object]] = {
        "overview.index.json": {
            "route": "",
            "overview": "overview.md",
            "coveredFiles": [],
            "childRoutes": [
                {"index": "pkg/overview.index.json"},
                {"index": "other/overview.index.json"},
                {},
            ],
        },
        "pkg/overview.index.json": {
            "route": "pkg",
            "overview": "pkg/overview.md",
            "coveredFiles": ["pkg/a.py", "pkg/b.py", "pkg/missing.py"],
            "childRoutes": [],
        },
    }

    assert len(owners.extract_file_sidecar_edges(indexes, nodes)) == 2
    assert len(owners.extract_governing_route_edges(indexes, nodes)) == 4
    assert len(owners.extract_route_index_edges(indexes, nodes)) == 6


def _manifest(code: Path, files: tuple[SourceFile, ...]) -> Manifest:
    return Manifest(
        code_root=code.resolve().as_posix(),
        memory_root=(code.parent / "memory").resolve().as_posix(),
        snapshot_id="d" * 64,
        source_bytes=sum(file.identity.size for file in files),
        directories=(),
        files=files,
    )


def test_index_member_census_and_exact_git_blob_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = tmp_path / "code"
    memory = tmp_path / "memory"
    code.mkdir()
    (memory / "onboarding").mkdir(parents=True)
    path = code / "a.py"
    path.write_text("value = 1\n", encoding="utf-8")
    identity = Identity.read(path, "a.py")
    member = SourceFile(path, identity, hashlib.sha256(path.read_bytes()).hexdigest())
    candidate = _candidate(code, memory)
    monkeypatch.setattr(owners.source_index, "code_files", lambda trees: [(path, "a.py")])
    monkeypatch.setattr(owners, "require_git", lambda repository, args: "e" * 40)

    owners._require_index_matches_candidate(candidate, _manifest(code, (member,)))

    duplicate = _manifest(code, (member, member))
    with pytest.raises(ScopeUnprovenError) as caught:
        owners._require_index_matches_candidate(candidate, duplicate)
    assert _reason(caught) == "source-index-stale"


@pytest.mark.parametrize("case", ["unavailable", "blob", "content"])
def test_index_member_refuses_unavailable_or_mismatched_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    code = tmp_path / "code"
    memory = tmp_path / "memory"
    code.mkdir()
    (memory / "onboarding").mkdir(parents=True)
    path = code / "a.py"
    path.write_text("value = 1\n", encoding="utf-8")
    identity = Identity.read(path, "a.py")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    member = SourceFile(path, identity, "0" * 64 if case == "content" else digest)
    candidate = _candidate(code, memory)
    monkeypatch.setattr(owners.source_index, "code_files", lambda trees: [(path, "a.py")])

    def git(repository: Path, args: list[str]) -> str:
        del repository
        if case == "unavailable":
            raise OSError("gone")
        return ("1" if args[0] == "rev-parse" else "2") * 40 if case == "blob" else "1" * 40

    monkeypatch.setattr(owners, "require_git", git)
    with pytest.raises(ScopeUnprovenError) as caught:
        owners._require_index_matches_candidate(candidate, _manifest(code, (member,)))
    assert _reason(caught) == "source-index-candidate-mismatch"


def test_citation_resolution_handles_missing_memory_code_and_escape_roots(
    tmp_path: Path,
) -> None:
    code = tmp_path / "code"
    memory = tmp_path / "memory"
    outside = tmp_path / "outside.py"
    code.mkdir()
    memory.mkdir()
    outside.write_text("outside\n", encoding="utf-8")
    memory_doc = memory / "onboarding.md"
    memory_doc.write_text("memory\n", encoding="utf-8")
    code_doc = code / "a.py"
    code_doc.write_text("code\n", encoding="utf-8")

    missing = cast(
        Trees, SimpleNamespace(code_root=code, memory_root=memory, resolve=lambda path: None)
    )
    assert owners._citation_node(missing, "missing.py", {}) is None

    memory_tree = cast(
        Trees,
        SimpleNamespace(code_root=code, memory_root=memory, resolve=lambda path: memory_doc),
    )
    assert (
        owners._citation_node(
            memory_tree,
            "onboarding.md",
            {"memory:onboarding.md": _node("memory:onboarding.md")},
        )
        == "memory:onboarding.md"
    )

    code_tree = cast(
        Trees,
        SimpleNamespace(code_root=code, memory_root=memory, resolve=lambda path: code_doc),
    )
    assert owners._citation_node(code_tree, "a.py", {}) is None

    escaped = cast(
        Trees,
        SimpleNamespace(code_root=code, memory_root=memory, resolve=lambda path: outside),
    )
    with pytest.raises(ScopeUnprovenError) as caught:
        owners._citation_node(escaped, "outside.py", {})
    assert _reason(caught) == "citation-source-escape"


def test_route_strings_and_dependency_roots_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ScopeUnprovenError) as caught:
        owners._strings({"coveredFiles": ["a.py", 2]}, "coveredFiles")
    assert _reason(caught) == "route-index-malformed"

    code = tmp_path / "code"
    memory = tmp_path / "memory"
    onboarding = memory / "onboarding"
    code.mkdir()
    onboarding.mkdir(parents=True)
    candidate = _candidate(code, memory)
    owners._require_roots(candidate, code, memory, onboarding)
    with pytest.raises(ScopeUnprovenError) as caught:
        owners._require_roots(candidate, tmp_path / "other", memory, onboarding)
    assert _reason(caught) == "dependency-root-mismatch"
