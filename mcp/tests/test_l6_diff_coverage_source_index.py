"""L6 closeout coverage tests for citation source-index edge branches."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

import pytest

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.memory_quality.style.citations import source_index, source_index_state
from agents_remember.memory_quality.style.citations.resolution import Trees
from agents_remember.memory_quality.style.citations.source_index import (
    SourceIndexError,
    SourceTreeChangedError,
    _publish_generation,
    _reclaim_legacy_cache_roots,
    _reclaim_legacy_root,
    _reclamation_time_remaining,
    _remove_cache_tree,
    _stable_read,
    _tree_state,
    _validate,
    _validate_temporary_database,
)
from agents_remember.memory_quality.style.citations.source_index_state import (
    Identity,
    check_source_bounds,
)


@pytest.fixture
def env(tmp_path: Path):
    code = tmp_path / "code"
    memory = tmp_path / "memory"
    cache = tmp_path / "cache"
    code.mkdir()
    memory.mkdir()
    (code / "a.py").write_text("x = 1\n", encoding="utf-8")
    with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(cache)}):
        yield Trees(code_root=code, memory_root=memory), tmp_path


class TestCloseAndPaths:
    def test_close_idempotent(self, env) -> None:
        trees, _ = env
        with source_index.open_repository_index(trees) as index:
            index.close()
            index.close()

    def test_cache_root_inside_code_raises(self, env) -> None:
        trees, _ = env
        with (
            mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(trees.code_root / "cache")}),
            pytest.raises(SourceIndexError, match="must stay outside code root"),
        ):
            source_index.cache_paths(trees)


class TestOpenAndValidate:
    def test_expected_snapshot_with_integrity_raises(self, env) -> None:
        trees, _ = env
        with pytest.raises(SourceIndexError, match="cannot request per-command integrity"):
            source_index.open_repository_index(
                trees, expected_snapshot="a" * 64, verify_integrity=True
            )

    def test_validate_root_mismatch_stale(self, env) -> None:
        trees, tmp = env
        source_index.build_repository_index(trees)
        paths = source_index.cache_paths(trees)
        manifest = source_index.Manifest.from_json(paths.manifest)
        other = Trees(code_root=tmp / "other", memory_root=trees.memory_root)
        validation = _validate(manifest, other, source_index.IndexMetrics(), check_content=False)
        assert validation.stale is True

    def test_build_and_publish_exhausts_attempts(self, env) -> None:
        trees, _ = env
        with (
            mock.patch.object(
                source_index,
                "_build_once",
                side_effect=[SourceTreeChangedError("a"), SourceTreeChangedError("b")],
            ),
            pytest.raises(SourceTreeChangedError, match="b"),
        ):
            source_index._build_and_publish(
                source_index.cache_paths(trees), trees, source_index.IndexMetrics()
            )


class TestBuildAndPublishBranches:
    def test_exceeds_index_bytes(self, env) -> None:
        trees, _ = env
        source_index.build_repository_index(trees)
        paths = source_index.cache_paths(trees)
        manifest = source_index.Manifest.from_json(paths.manifest)
        readiness = source_index._ready_generation(paths, trees)
        database = Path(paths.slot) / "big.tmp"
        database.write_bytes(b"x" * 1024)
        with (
            mock.patch.object(source_index, "MAX_INDEX_BYTES", 1),
            pytest.raises(SourceIndexError, match="exceeds its"),
        ):
            _publish_generation(
                paths,
                database,
                manifest,
                readiness,
                cast(
                    source_index.PublicationBoundary,
                    SimpleNamespace(trees=trees, state=None, metrics=source_index.IndexMetrics()),
                ),
            )

    def test_validate_temporary_database_failure(self, env, tmp_path: Path) -> None:
        trees, _ = env
        source_index.build_repository_index(trees)
        paths = source_index.cache_paths(trees)
        readiness = source_index._ready_generation(paths, trees)
        bad = tmp_path / "bad"
        bad.mkdir()
        with pytest.raises(SourceIndexError, match="temporary generation failed validation"):
            _validate_temporary_database(bad, readiness)


class TestTreeAndBounds:
    def test_tree_state_skips_memory_inside_code(self, env) -> None:
        trees, _ = env
        inner = trees.code_root / "memory"
        inner.mkdir()
        (inner / "x.py").write_text("x\n", encoding="utf-8")
        nested = Trees(code_root=trees.code_root, memory_root=inner)
        state = _tree_state(nested, source_index.IndexMetrics())
        assert all("memory/x.py" not in f.identity.path for f in state.files)

    def test_stable_read_mismatch(self, env) -> None:
        trees, _ = env
        path = trees.code_root / "a.py"
        identity = Identity.read(path, "a.py")
        wrong = replace(identity, size=identity.size + 1)
        with pytest.raises(SourceTreeChangedError, match="changed before"):
            _stable_read(path, wrong)

    def test_stable_read_changed_while_read(self, env) -> None:
        trees, _ = env
        path = trees.code_root / "a.py"
        identity = Identity.read(path, "a.py")
        changed = replace(identity, size=identity.size + 1)
        with (
            mock.patch.object(
                source_index.Identity,
                "read",
                side_effect=[identity, changed],
            ),
            pytest.raises(SourceTreeChangedError, match="changed while"),
        ):
            _stable_read(path, identity)

    def test_check_source_bounds(self, env) -> None:
        trees, _ = env
        path = trees.code_root / "a.py"
        identity = Identity.read(path, "a.py")
        with (
            mock.patch.object(source_index_state, "MAX_SOURCE_FILES", 0),
            pytest.raises(SourceIndexError, match="file cap"),
        ):
            check_source_bounds((identity,))
        with (
            mock.patch.object(source_index_state, "MAX_SOURCE_BYTES", 1),
            pytest.raises(SourceIndexError, match="byte cap"),
        ):
            check_source_bounds((identity,))


class TestReclamation:
    def test_reclaim_legacy_cache_roots(self, env) -> None:
        _trees, tmp = env
        parent = tmp / "reclaim"
        parent.mkdir()
        legacy = parent / "citation-source-index-v1"
        (legacy / "slot-0").mkdir(parents=True)
        (legacy / "slot-0" / "index.lock").write_text("", encoding="utf-8")
        (parent / ".citation-source-index-v1.reclaim.abc").write_text("", encoding="utf-8")
        current = parent / "citation-source-index-v2"
        current.mkdir()
        _reclaim_legacy_cache_roots(current)
        assert not legacy.exists()
        assert not (parent / ".citation-source-index-v1.reclaim.abc").exists()

    def test_reclaim_legacy_root_oserror(self, env) -> None:
        _trees, tmp = env
        root = tmp / "legacy"
        root.mkdir()
        with (
            mock.patch.object(source_index, "atomic_replace", side_effect=OSError("boom")),
            pytest.raises(SourceIndexError, match="cannot reclaim legacy citation cache"),
        ):
            _reclaim_legacy_root(root, 0)

    def test_remove_cache_tree(self, tmp_path: Path) -> None:
        file = tmp_path / "f"
        file.write_text("x", encoding="utf-8")
        _remove_cache_tree(file)
        assert not file.exists()
        directory = tmp_path / "d"
        directory.mkdir()
        (directory / "x").write_text("x", encoding="utf-8")
        _remove_cache_tree(directory)
        assert not directory.exists()
        with mock.patch.object(source_index.shutil, "rmtree", side_effect=OSError("boom")):
            directory.mkdir()
            with pytest.raises(SourceIndexError, match="cannot reclaim legacy citation cache"):
                _remove_cache_tree(directory)

    def test_reclamation_time_remaining(self) -> None:
        with pytest.raises(SourceIndexError, match="timed out before legacy"):
            _reclamation_time_remaining(0, Path("x"))
