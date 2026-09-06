from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.providers.context import (
    CGC_REQUIREMENTS,
    CgcRepo,
    ContextProviderError,
    apply_cgc_cgcignore_patch,
    assert_no_source_provider_artifacts,
    cgc_runtime_layout,
    ensure_cgc_runtime_layout,
    source_provider_artifacts,
)


class ContextProviderLayoutTests(unittest.TestCase):
    def test_cgc_layout_ignores_host_falkordb_environment_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            code_root = root / "repos" / "My App"
            layout = cgc_runtime_layout(
                CgcRepo(
                    coordination_root=root / "ar-coordination",
                    repo_id="My App",
                    code_repo_root=code_root,
                ),
            )

            with patch.dict(
                os.environ,
                {"FALKORDB_HOST": "192.0.2.10", "FALKORDB_PORT": "26379"},
            ):
                env = layout.env()

            self.assertEqual(env["FALKORDB_HOST"], "127.0.0.1")
            self.assertEqual(env["FALKORDB_PORT"], "6379")

    def test_ensure_cgc_runtime_layout_writes_pinned_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            layout = cgc_runtime_layout(
                CgcRepo(
                    coordination_root=root / "ar-coordination",
                    repo_id="agents-remember",
                    code_repo_root=root / "agents-remember",
                    cgcignore_patterns=("tools/ffmpeg/",),
                ),
            )

            layout.code_repo_root.mkdir(parents=True)
            (layout.code_repo_root / ".gitignore").write_text(
                "/samples\n.tmp_drt\n", encoding="utf-8"
            )
            layout.env_file.parent.mkdir(parents=True)
            layout.env_file.write_text("DEFAULT_DATABASE=kuzudb\n", encoding="utf-8")

            ensure_cgc_runtime_layout(layout)

            self.assertEqual(
                layout.requirements_file.read_text(encoding="utf-8"),
                "\n".join(CGC_REQUIREMENTS) + "\n",
            )
            self.assertIn(
                "database: falkordb-remote", layout.config_file.read_text(encoding="utf-8")
            )
            env_text = layout.env_file.read_text(encoding="utf-8")
            self.assertIn("DEFAULT_DATABASE=falkordb-remote", env_text)
            self.assertNotIn("CGC_RUNTIME_DB_TYPE=", env_text)
            self.assertNotIn("FALKORDB_HOST=", env_text)
            self.assertNotIn("FALKORDB_PORT=", env_text)
            self.assertNotIn("FALKORDB_GRAPH_NAME=", env_text)
            self.assertNotIn("PYTHONIOENCODING=", env_text)
            self.assertNotIn("USERPROFILE=", env_text)
            cgcignore_text = layout.cgcignore_path.read_text(encoding="utf-8")
            self.assertTrue(cgcignore_text.startswith("# Managed by Agents Remember"))
            self.assertIn("# Inherited from source .gitignore", cgcignore_text)
            self.assertIn("/samples", cgcignore_text)
            self.assertIn(".tmp_drt", cgcignore_text)
            self.assertIn("tools/ffmpeg/", cgcignore_text)
            # L12: the live `cgc watch` reads the HOME-scoped GLOBAL context file, so the
            # enrichment must be materialized there too — byte-identical to the runtime copy.
            global_cgcignore = (
                layout.run_root / "home" / ".codegraphcontext" / "global" / ".cgcignore"
            )
            self.assertEqual(global_cgcignore.read_text(encoding="utf-8"), cgcignore_text)
            self.assertTrue(layout.logs_root.is_dir())
            self.assertTrue(layout.run_root.is_dir())

    def test_detects_forbidden_source_provider_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code_root = Path(tmp) / "repo"
            code_root.mkdir()
            self.assertEqual(source_provider_artifacts(code_root), [])

            (code_root / ".cgcignore").write_text("# generated\n", encoding="utf-8")
            self.assertEqual(
                [path.name for path in source_provider_artifacts(code_root)], [".cgcignore"]
            )

            with self.assertRaises(ContextProviderError):
                assert_no_source_provider_artifacts(code_root)

    def test_patch_rejects_unexpected_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cgcignore.py"
            target.write_text("def build_ignore_spec():\n    pass\n", encoding="utf-8")

            with self.assertRaises(ContextProviderError):
                apply_cgc_cgcignore_patch(target)


if __name__ == "__main__":
    unittest.main()
