"""Dependency-owner and stale-index tests for the R06 scope compiler."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from agents_remember.memory_quality.incremental_scope.candidate import observe_git_tree_delta
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
from agents_remember.memory_quality.style.citations.resolution import Trees
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
    for repository in (code, memory):
        _git(repository, "init", "-q")
        _git(repository, "config", "user.email", "scope-edges@test.invalid")
        _git(repository, "config", "user.name", "scope edges test")
        _git(repository, "add", "-A")
        _git(repository, "commit", "-qm", "exact dependency edge fixture")
    candidate = _actual_candidate(code, memory)

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


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


def _initialize_git(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "scope-index@test.invalid")
    _git(root, "config", "user.name", "scope index test")


def _actual_candidate(code: Path, memory: Path) -> ScopeCandidateIdentity:
    original = _candidate(code, memory)
    code_base = _git(code, "rev-parse", "HEAD")
    memory_base = _git(memory, "rev-parse", "HEAD")
    pair = original.pairIdentity.model_copy(
        update={"codeBaseCommit": code_base, "memoryBaseCommit": memory_base}
    )
    return ScopeCandidateIdentity(
        pairIdentity=pair,
        code=observe_git_tree_delta(
            code,
            namespace="code",
            root=pair.codeRoot,
            base_ref=code_base,
            candidate_tree=_git(code, "write-tree"),
        ),
        memory=observe_git_tree_delta(
            memory,
            namespace="memory",
            root=pair.memoryRoot,
            base_ref=memory_base,
            candidate_tree=_git(memory, "write-tree"),
        ),
        task=original.task,
    )


def _linked_candidate(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, empty: bool = False
) -> tuple[ScopeCandidateIdentity, Trees]:
    primary = root / "primary"
    memory = root / "memory"
    _initialize_git(primary)
    _initialize_git(memory)
    (memory / "onboarding").mkdir()
    _git(memory, "commit", "--allow-empty", "-qm", "memory baseline")
    if not empty:
        (primary / "src").mkdir()
        (primary / "src/anchor.py").write_text("def scope_anchor():\n    return 1\n")
        (primary / "src/old.py").write_text("old_symbol = 1\n")
        (primary / "src/remove.py").write_text("remove_symbol = 1\n")
        (primary / ".gitignore").write_text("generated/\n")
        (primary / "generated").mkdir()
        (primary / "generated/tracked.py").write_text("tracked_ignored = 1\n")
        _git(primary, "add", "-f", "src", ".gitignore", "generated/tracked.py")
    _git(primary, "commit", "--allow-empty", "-qm", "code baseline")
    linked = root / "linked"
    _git(primary, "worktree", "add", "--detach", "-q", str(linked), "HEAD")
    assert (linked / ".git").is_file()
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
    candidate = _actual_candidate(linked, memory)
    return candidate, Trees(linked, memory, candidate_tree=candidate.code.candidateTree)


def test_source_index_accepts_real_linked_empty_candidate_without_git_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate, trees = _linked_candidate(tmp_path, monkeypatch, empty=True)
    with source_index.open_repository_index(trees) as index:
        observed = observe_source_index(candidate, index)
        assert index.files_indexed == 0
        assert observed.snapshotId == index.snapshot_id
        assert observed.candidateDigest == candidate.digest
        (trees.code_root / "new.py").write_text("new_untracked = 1\n")
        assert observe_source_index(candidate, index) == observed


def test_source_index_composes_exact_staged_git_members_with_real_candidate_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _base, trees = _linked_candidate(tmp_path, monkeypatch)
    code = trees.code_root
    (code / "generated/output.js").write_text("function scope_anchor() { return 2; }\n")
    (code / "untracked.py").write_text("untracked_symbol = 1\n")
    (code / "src/added.py").write_text("new_staged_symbol = 1\n")
    _git(code, "rm", "src/remove.py")
    _git(code, "mv", "src/old.py", "src/renamed.py")
    _git(code, "add", "src/added.py")
    candidate = _actual_candidate(code, trees.memory_root)
    assert {change.status for change in candidate.code.changes} == {"added", "deleted", "renamed"}
    exact = Trees(code, trees.memory_root, candidate_tree=candidate.code.candidateTree)
    with source_index.open_repository_index(exact) as index:
        observed = observe_source_index(candidate, index)
        assert observed.candidateDigest == candidate.digest
        assert observed.snapshotId == index.snapshot_id
        assert {relative for _, relative in source_index.code_files(exact)} == {
            ".gitignore",
            "src/anchor.py",
            "src/added.py",
            "src/renamed.py",
            "generated/tracked.py",
        }


@pytest.mark.parametrize("fault", ["dirty", "missing", "file-symlink", "parent-symlink"])
def test_source_index_observer_revalidates_tracked_bytes_and_unsafe_nodes_after_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    candidate, trees = _linked_candidate(tmp_path, monkeypatch)
    target = trees.code_root / "src/anchor.py"
    with source_index.open_repository_index(trees) as index:
        original_ready = index.paths.readiness.read_bytes()
        if fault == "dirty":
            target.write_text("def scope_anchor():\n    return 2\n")
        elif fault == "missing":
            target.unlink()
        elif fault == "file-symlink":
            outside = tmp_path / "outside.py"
            outside.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(outside)
        else:
            outside = tmp_path / "outside-src"
            target.parent.rename(outside)
            target.parent.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ScopeUnprovenError) as caught:
            observe_source_index(candidate, index)
        assert caught.value.failure.code == "source-index-candidate-mismatch"
        assert index.paths.readiness.read_bytes() == original_ready


@pytest.mark.parametrize("fault", ["ordinary-policy", "another-tree"])
def test_source_index_observer_refuses_wrong_policy_or_tree_with_identical_eligible_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    candidate, trees = _linked_candidate(tmp_path, monkeypatch)
    if fault == "ordinary-policy":
        # Use the primary checkout: its .git is a directory, so both eligible file sets match.
        code = tmp_path / "primary"
        candidate = _actual_candidate(code, trees.memory_root)
        acquired = Trees(code, trees.memory_root)
    else:
        (trees.code_root / "image.png").write_bytes(b"excluded image fixture")
        _git(trees.code_root, "add", "image.png")
        newer = _actual_candidate(trees.code_root, trees.memory_root)
        assert newer.code.candidateTree != candidate.code.candidateTree
        acquired = trees
        candidate = newer
    exact = Trees(
        acquired.code_root, acquired.memory_root, candidate_tree=candidate.code.candidateTree
    )
    assert source_index.code_files(acquired) == source_index.code_files(exact)
    with source_index.open_repository_index(acquired) as index:
        with pytest.raises(ScopeUnprovenError) as caught:
            observe_source_index(candidate, index)
        assert caught.value.failure.code == "source-index-candidate-mismatch"


@pytest.mark.parametrize("fault", ["wrong-root", "malformed-readiness"])
def test_source_index_refuses_malformed_or_wrong_root_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    candidate, trees = _linked_candidate(tmp_path, monkeypatch)
    with source_index.open_repository_index(trees) as index:
        if fault == "malformed-readiness":
            index.paths.readiness.write_text("not-json", encoding="utf-8")
            expected = "source-index-malformed"
        else:
            candidate = candidate.model_copy(
                update={"code": candidate.code.model_copy(update={"root": str(tmp_path / "other")})}
            )
            expected = "source-index-stale"
        with pytest.raises(ScopeUnprovenError) as caught:
            observe_source_index(candidate, index)
        assert caught.value.failure.code == expected
