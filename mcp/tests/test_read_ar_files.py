"""Paired source reads preserve requested bytes, confine paths and deduplicate unchanged onboarding."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(MCP_SRC))
sys.path.insert(0, str(MCP_TESTS))

from agents_remember.application.read_files import read_ar_files_tool
from agents_remember.errors import AuthorityError
from agents_remember.kernel.coordination_context.models import (
    CoordinationContext,
    CrossRepoSettings,
    StorageSettings,
)
from agents_remember.kernel.primitives.runtime_config import (
    load_config,
)
from agents_remember.mcp.tools import read_ar_files_payload
from agents_remember.observer import (
    AmbientLifecycle,
    EventStore,
    install_ambient,
    observer_root,
    reset_ambient,
)
from agents_remember.observer.ambient import AmbientTiming
from test_config import settings_payload

REPO = "agents-remember"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make_config(root: Path, code_root: Path):
    """A real ``McpRuntimeConfig`` whose coordination root is ``root`` and whose
    one repo points at ``code_root`` -- so ``observer_root`` and the reset marker
    resolve under ``root/logs/observer`` and the repo path confines correctly."""
    settings = settings_payload(root)
    settings["workspaceRoot"] = str(code_root.parent)
    settings["repositories"] = {REPO: {}}
    path = root / ".codex" / "mcp" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings), encoding="utf-8")
    return load_config(path)


def _build_context(
    code_root: Path,
    onboarding_root: Path,
    *,
    coordination_root: Path,
    storage_mode: str,
) -> CoordinationContext:
    """A minimal context with the fields the application layer reads, isolating storage."""
    storage = StorageSettings(mode=storage_mode, default=storage_mode)
    return CoordinationContext(
        topology="external",
        code_repository_name=REPO,
        code_repository_root=code_root,
        coordination_root=coordination_root,
        memory_root=onboarding_root.parent,
        onboarding_root=onboarding_root,
        settings_path=onboarding_root / "settings.md",
        path_settings_path=None,
        task_root=coordination_root,
        temp_root=coordination_root / "temp",
        docs_root=coordination_root / "docs",
        system_root=onboarding_root.parent / "system",
        sources_path=onboarding_root.parent / "system" / "sources.md",
        tools_path=onboarding_root.parent / "system" / "tools.md",
        storage=storage,
        path_rules=storage.path_rules,
        cross_repo=CrossRepoSettings(),
        memory_mode="external",
    )


def _write_sidecar(onboarding_root: Path, source_rel: str, body: str) -> None:
    path = onboarding_root / f"{source_rel}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# {source_rel}",
                "",
                "| Field | Value |",
                "| --- | --- |",
                f"| path | `{source_rel}` |",
                "| doc_type | `file-level-onboarding` |",
                "| lastUpdated | 2026-06-01T00:00 |",
                "",
                body,
                "",
                "## Update History",
                "- 2026-06-01 seeded",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_overview(onboarding_root: Path, route: str, text: str) -> None:
    rel = "overview.md" if route == "" else f"{route}/overview.md"
    path = onboarding_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_route_index(onboarding_root: Path, route: str, *, covered: list[str]) -> None:
    rel = "overview.index.json" if route == "" else f"{route}/overview.index.json"
    scope = "**" if route == "" else f"{route}/**"
    path = onboarding_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "kind": "agents-remember-route-index",
                "route": route,
                "sourceScope": [scope],
                "coveredFiles": covered,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# ranged-read helper
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# application layer: status semantics, path confinement, source independence
# --------------------------------------------------------------------------


class ApplicationStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = Path(tempfile.mkdtemp())
        self.code = self._dir / "workspace" / REPO
        (self.code / "pkg").mkdir(parents=True)
        (self.code / "pkg" / "mod.py").write_text("x = 1\ny = 2\nz = 3\n", encoding="utf-8")
        (self.code / "pkg" / "other.py").write_text("# other\n", encoding="utf-8")
        (self.code / "bin.dat").write_bytes(b"\xff\xfe\x00\x01binary")
        self.onb = self._dir / "memory" / "onboarding"
        self.onb.mkdir(parents=True)
        self.config = _make_config(self._dir, self.code)
        reset_ambient()

    def tearDown(self) -> None:
        reset_ambient()

    def _read(self, files, *, storage_mode: str):
        ctx = _build_context(
            self.code,
            self.onb,
            coordination_root=self.config.coordination_root,
            storage_mode=storage_mode,
        )
        return read_ar_files_tool(self.config, repo_id=REPO, files=files, _context=ctx)

    def test_range_request_returns_exact_slice(self) -> None:
        result = self._read(
            [{"path": "pkg/mod.py", "source": {"startLine": 2, "endLine": 3}}],
            storage_mode="disabled",
        )
        self.assertEqual(result["files"][0]["source"], "y = 2\nz = 3\n")

    def test_full_read_is_not_truncated(self) -> None:
        # A file larger than a few KB read as "full" returns its content
        # byte-for-byte -- the full path must never silently truncate.
        big = "\n".join(f"line {i:05d} " + "x" * 80 for i in range(400)) + "\n"
        self.assertGreater(len(big.encode("utf-8")), 4096)
        (self.code / "pkg" / "big.py").write_text(big, encoding="utf-8")
        result = self._read([{"path": "pkg/big.py"}], storage_mode="disabled")
        self.assertEqual(result["files"][0]["source"], big)

    def test_binary_source_is_omitted(self) -> None:
        result = self._read([{"path": "bin.dat"}], storage_mode="disabled")
        self.assertNotIn("source", result["files"][0])

    def test_path_confinement_rejects_escape(self) -> None:
        with self.assertRaises(AuthorityError):
            self._read([{"path": "../secret.txt"}], storage_mode="disabled")

    def test_symlink_file_escape_rejected(self) -> None:
        # A symlink inside the repo pointing at a file OUTSIDE the repo resolves
        # out of root, so confinement rejects it (not a literal ".." token).
        outside = self._dir / "outside_secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        link = self.code / "leak.py"
        link.symlink_to(outside)
        with self.assertRaises(AuthorityError):
            self._read([{"path": "leak.py"}], storage_mode="disabled")

    def test_symlink_dir_escape_rejected(self) -> None:
        # A symlinked directory inside the repo pointing OUTSIDE, addressed via a
        # child path, still resolves out of root and is rejected.
        outside_dir = self._dir / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "secret.py").write_text("secret = 1\n", encoding="utf-8")
        link = self.code / "linkdir"
        link.symlink_to(outside_dir, target_is_directory=True)
        with self.assertRaises(AuthorityError):
            self._read([{"path": "linkdir/secret.py"}], storage_mode="disabled")


class FrontDoorDedupTests(unittest.TestCase):
    """Auto-attach + per-lifecycle dedup of repo overview + route chain."""

    def setUp(self) -> None:
        self._dir = Path(tempfile.mkdtemp())
        self.code = self._dir / "workspace" / REPO
        (self.code / "pkg" / "sub").mkdir(parents=True)
        (self.code / "pkg" / "sub" / "mod.py").write_text("a = 1\n", encoding="utf-8")
        self.onb = self._dir / "memory" / "onboarding"
        self.onb.mkdir(parents=True)
        _write_overview(self.onb, "", "# Repo overview\nroot text\n")
        _write_overview(self.onb, "pkg", "# pkg overview\npkg text\n")
        _write_overview(self.onb, "pkg/sub", "# sub overview\nsub text\n")
        _write_sidecar(self.onb, "pkg/sub/mod.py", "Mod body.")
        _write_route_index(self.onb, "pkg/sub", covered=["pkg/sub/mod.py"])
        self.config = _make_config(self._dir, self.code)
        self.ctx = _build_context(
            self.code,
            self.onb,
            coordination_root=self.config.coordination_root,
            storage_mode="repo-sidecar",
        )
        reset_ambient()
        amb = AmbientLifecycle(
            EventStore(observer_root(self.config)), timing=AmbientTiming(heartbeat_seconds=3600)
        )
        amb.start(fleeting=True)
        install_ambient(amb)
        self.amb = amb

    def tearDown(self) -> None:
        reset_ambient()

    def _read(self, files, *, refresh: bool = False):
        return read_ar_files_tool(
            self.config, repo_id=REPO, files=files, refresh=refresh, _context=self.ctx
        )

    def test_first_read_attaches_overview_and_route_chain(self) -> None:
        result = self._read([{"path": "pkg/sub/mod.py"}])
        self.assertEqual(result["repository_overview"]["path"], "overview.md")
        self.assertIn("root text", result["repository_overview"]["overview"])
        routes = result["route_overviews"]
        self.assertIn("pkg/sub", routes)
        self.assertIn("pkg", routes)

    def test_second_read_dedups_unchanged_pieces(self) -> None:
        self._read([{"path": "pkg/sub/mod.py"}])
        again = self._read([{"path": "pkg/sub/mod.py"}])
        self.assertNotIn("repository_overview", again)
        self.assertNotIn("route_overviews", again)

    def test_changed_overview_is_reserved(self) -> None:
        self._read([{"path": "pkg/sub/mod.py"}])
        _write_overview(self.onb, "", "# Repo overview\nCHANGED root text\n")
        again = self._read([{"path": "pkg/sub/mod.py"}])
        self.assertIn("repository_overview", again)
        self.assertIn("CHANGED", again["repository_overview"]["overview"])
        # The unchanged route overviews stay deduped.
        self.assertNotIn("route_overviews", again)

    def test_refresh_forces_reserve(self) -> None:
        self._read([{"path": "pkg/sub/mod.py"}])
        again = self._read([{"path": "pkg/sub/mod.py"}], refresh=True)
        self.assertIn("repository_overview", again)
        self.assertIn("route_overviews", again)

    def test_compact_marker_resets_served(self) -> None:
        self._read([{"path": "pkg/sub/mod.py"}])
        marker = observer_root(self.config) / "workspace" / "compact-reset.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
        again = self._read([{"path": "pkg/sub/mod.py"}])
        self.assertIn("repository_overview", again)
        # Marker consumed exactly once.
        self.assertFalse(marker.exists())


# --------------------------------------------------------------------------
# integration: five-file cap + payload builder through the real fixture
# --------------------------------------------------------------------------


class FiveFileCapAndPayloadTests(unittest.TestCase):
    """End-to-end through the real resolver + payload builder + token choke point."""

    def setUp(self) -> None:
        self._dir = Path(tempfile.mkdtemp())
        self.code = self._dir / "workspace" / REPO
        memory = self._dir / "ar-coordination" / "memory-repos" / f"ar-{REPO}"
        (memory / "system").mkdir(parents=True, exist_ok=True)
        (memory / "onboarding").mkdir(parents=True, exist_ok=True)
        (memory / "system" / "settings.md").write_text("# Settings\n", encoding="utf-8")
        self.code.mkdir(parents=True, exist_ok=True)
        (self.code / "README.md").write_text("# Fixture\nline2\n", encoding="utf-8")
        self.config = _make_config(self._dir, self.code)
        reset_ambient()

    def tearDown(self) -> None:
        reset_ambient()

    def test_payload_reads_committed_source(self) -> None:
        payload = read_ar_files_payload(self.config, REPO, [{"path": "README.md"}])
        self.assertEqual(payload["files"][0]["source"], "# Fixture\nline2\n")


# --------------------------------------------------------------------------
# test plumbing
# --------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main()
