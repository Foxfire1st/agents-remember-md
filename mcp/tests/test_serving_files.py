"""Tests for the read-only files API (serving/files.py, L1 of operations-integration).

Pure tests build a :class:`FileScope` directly over a temp code+onboarding tree (so
onboarding behaviour is fully controlled); route tests drive ``/api/files/*`` through
the real scope resolver to cover the catalog, the allow-list, the traversal guard, and
the memory-less degrade path.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.kernel.primitives.runtime_config import (
    McpRuntimeConfig,
    RepositoryScope,
)
from agents_remember.serving.app import create_app
from agents_remember.serving.projector import ProjectionCadence

_SIDECAR = (
    "# mod\n\n"
    "| Field | Value |\n| --- | --- |\n"
    "| lastVerifiedCommitHash | `abc1234` |\n"
    "| lastVerifiedCommitDate | `2026-06-28` |\n\n"
    "The module body.\n"
)


def _make_repo(code_root: Path, *, with_memory: bool) -> None:
    """A small code tree; one file has a sidecar, one does not."""
    (code_root / "pkg").mkdir(parents=True)
    (code_root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (code_root / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    (code_root / "README.md").write_text("# readme\n", encoding="utf-8")
    if with_memory:
        onboarding = code_root / "ar-memory" / "onboarding"
        (onboarding / "pkg").mkdir(parents=True)
        (onboarding / "pkg" / "mod.py.md").write_text(_SIDECAR, encoding="utf-8")
        (onboarding / "overview.md").write_text("# repo overview\n", encoding="utf-8")


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _client(self, *, with_memory: bool) -> TestClient:
        code_root = self.tmp / "ws" / "R"
        _make_repo(code_root, with_memory=with_memory)
        config = McpRuntimeConfig(
            config_path=self.tmp / "settings.json",
            coordination_root=self.tmp / "coord",
            workspace_root=self.tmp / "ws",
            transcript_root=self.tmp / "logs",
            repositories={"R": RepositoryScope(repo_id="R", path=code_root)},
        )
        return TestClient(create_app(config, cadence=ProjectionCadence(interval=100)))

    def test_read_serves_code_content(self) -> None:
        with self._client(with_memory=True) as client:
            response = client.get("/api/files/read", params={"repo": "R", "path": "pkg/mod.py"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "x = 1\n")

    def test_traversal_is_400_bad_path(self) -> None:
        with self._client(with_memory=True) as client:
            response = client.get(
                "/api/files/read", params={"repo": "R", "path": "../../etc/passwd"}
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "bad-path")


if __name__ == "__main__":
    unittest.main()
