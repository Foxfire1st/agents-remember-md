"""HTTP proofs for the confined task-local ``requirements/`` artifact root."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from agents_remember.errors import AuthorityError
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, RepositoryScope
from agents_remember.kernel.sidecar_pairing import confine_non_symlink_rel
from agents_remember.serving.app import create_app
from agents_remember.serving.projector import ProjectionCadence
from agents_remember.tasks import TaskDocument, write_task_doc

_MASTER = "260831_master"
_DOCUMENT = f"{_MASTER}/task.json"


class RequirementRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.code_root = self.tmp / "ws" / "R"
        self.code_root.mkdir(parents=True)
        (self.code_root / "README.md").write_text("# repo\n", encoding="utf-8")
        self.coord = self.tmp / "coord"
        self.master_root = self.coord / "tasks" / "R" / _MASTER
        write_task_doc(
            self.master_root,
            TaskDocument.model_validate(
                {
                    "id": "T",
                    "slug": "task",
                    "title": "Master",
                    "kind": "master",
                    "repo": "R",
                    "createdAt": "2026-09-02T10:00",
                }
            ),
        )
        other = self.coord / "tasks" / "R" / "other"
        write_task_doc(
            other,
            TaskDocument.model_validate(
                {
                    "id": "O",
                    "slug": "task",
                    "title": "Other",
                    "kind": "master",
                    "repo": "R",
                    "createdAt": "2026-09-02T10:01",
                }
            ),
        )
        self.requirements = self.master_root / "requirements"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _client(self) -> TestClient:
        config = McpRuntimeConfig(
            config_path=self.tmp / "settings.json",
            coordination_root=self.coord,
            workspace_root=self.tmp / "ws",
            transcript_root=self.tmp / "logs",
            repositories={"R": RepositoryScope(repo_id="R", path=self.code_root)},
        )
        return TestClient(create_app(config, cadence=ProjectionCadence(interval=100)))

    def _params(self, **extra: str) -> dict[str, str]:
        return {"repo": "R", "master": _MASTER, "document": _DOCUMENT, **extra}

    def _seed(self) -> bytes:
        self.requirements.mkdir(parents=True)
        raw = b"# Requirement\n\nExact packet bytes.\n"
        (self.requirements / "CCR-R23-v1.md").write_bytes(raw)
        (self.requirements / "nested").mkdir()
        (self.requirements / "nested" / "proof.md").write_text("# proof\n", encoding="utf-8")
        (self.requirements / "README.txt").write_text("not a packet\n", encoding="utf-8")
        return raw

    def test_list_registers_only_exact_markdown_packets(self) -> None:
        raw = self._seed()
        with self._client() as client:
            response = client.get("/api/requirements/list", params=self._params())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["registered"])
        self.assertEqual(
            [row["address"] for row in body["requirements"]],
            ["requirements/CCR-R23-v1.md", "requirements/nested/proof.md"],
        )
        self.assertEqual(body["requirements"][0]["size"], len(raw))
        self.assertEqual(body["requirements"][0]["sha256"], hashlib.sha256(raw).hexdigest())

    def test_read_returns_exact_packet_content_and_canonical_address(self) -> None:
        raw = self._seed()
        with self._client() as client:
            response = client.get(
                "/api/requirements/read", params=self._params(path="CCR-R23-v1.md")
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["content"].encode(), raw)
        self.assertEqual(body["path"], "CCR-R23-v1.md")
        self.assertEqual(body["address"], "requirements/CCR-R23-v1.md")

    def test_absent_root_is_valid_empty_registration(self) -> None:
        with self._client() as client:
            response = client.get("/api/requirements/list", params=self._params())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["requirements"], [])
        self.assertFalse(response.json()["registered"])

    def test_missing_packet_is_not_found(self) -> None:
        self._seed()
        with self._client() as client:
            response = client.get("/api/requirements/read", params=self._params(path="missing.md"))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["status"], "not-found")

    def test_read_from_absent_root_and_markdown_named_directory_are_not_found(self) -> None:
        with self._client() as client:
            absent = client.get("/api/requirements/read", params=self._params(path="missing.md"))
        self.assertEqual(absent.status_code, 404)

        self.requirements.mkdir()
        (self.requirements / "directory.md").mkdir()
        with self._client() as client:
            directory = client.get(
                "/api/requirements/read", params=self._params(path="directory.md")
            )
        self.assertEqual(directory.status_code, 404)

    def test_traversal_and_absolute_client_paths_are_refused(self) -> None:
        self._seed()
        paths = ["../task.json", "/etc/passwd", r"C:\Windows\win.ini"]
        with self._client() as client:
            responses = [
                client.get("/api/requirements/read", params=self._params(path=path))
                for path in paths
            ]
        self.assertEqual([response.status_code for response in responses], [400, 400, 400])
        self.assertEqual({response.json()["status"] for response in responses}, {"bad-path"})

    def test_malformed_and_unsupported_paths_are_refused(self) -> None:
        self._seed()
        with self._client() as client:
            empty = client.get("/api/requirements/read", params=self._params(path=""))
            unsupported = client.get(
                "/api/requirements/read", params=self._params(path="README.txt")
            )
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(unsupported.status_code, 400)

    def test_repository_master_document_context_mismatch_is_refused(self) -> None:
        self._seed()
        with self._client() as client:
            wrong_master = client.get(
                "/api/requirements/list",
                params={"repo": "R", "master": _MASTER, "document": "other/task.json"},
            )
            missing_doc = client.get(
                "/api/requirements/list",
                params={"repo": "R", "master": _MASTER, "document": f"{_MASTER}/ghost.json"},
            )
            unknown_repo = client.get(
                "/api/requirements/list",
                params={"repo": "ghost", "master": _MASTER, "document": _DOCUMENT},
            )
        self.assertEqual(wrong_master.status_code, 400)
        self.assertEqual(wrong_master.json()["status"], "bad-context")
        self.assertEqual(missing_doc.status_code, 400)
        self.assertEqual(unknown_repo.status_code, 404)

    def test_malformed_and_noncanonical_context_selectors_are_refused(self) -> None:
        self._seed()
        selectors = [
            self._params(master="nested/master"),
            self._params(document="../task.json"),
            self._params(document=_DOCUMENT.replace("/", "\\")),
        ]
        with self._client() as client:
            responses = [
                client.get("/api/requirements/list", params=selector) for selector in selectors
            ]
        self.assertEqual([response.status_code for response in responses], [400, 400, 400])
        self.assertEqual({response.json()["status"] for response in responses}, {"bad-context"})

    def test_symlink_root_is_refused(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("secret\n", encoding="utf-8")
        os.symlink(outside, self.requirements)
        with self._client() as client:
            response = client.get("/api/requirements/list", params=self._params())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "bad-path")

    def test_escaping_child_and_in_root_alias_are_both_refused(self) -> None:
        self._seed()
        outside = self.tmp / "secret.md"
        outside.write_text("secret\n", encoding="utf-8")
        os.symlink(outside, self.requirements / "escape.md")
        os.symlink(self.requirements / "CCR-R23-v1.md", self.requirements / "alias.md")
        with self._client() as client:
            listed = client.get("/api/requirements/list", params=self._params())
            escaped = client.get("/api/requirements/read", params=self._params(path="escape.md"))
            aliased = client.get("/api/requirements/read", params=self._params(path="alias.md"))
        self.assertEqual(listed.status_code, 400)
        self.assertEqual(escaped.status_code, 400)
        self.assertEqual(aliased.status_code, 400)

    def test_inventory_bounds_fail_closed(self) -> None:
        self.requirements.mkdir()
        cursor = self.requirements
        for index in range(9):
            cursor /= f"level-{index}"
            cursor.mkdir()
        (cursor / "too-deep.md").write_text("# deep\n", encoding="utf-8")
        with self._client() as client:
            too_deep = client.get("/api/requirements/list", params=self._params())
        self.assertEqual(too_deep.status_code, 400)

        for child in list(self.requirements.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
        (self.requirements / "one.md").write_text("# one\n", encoding="utf-8")
        (self.requirements / "two.md").write_text("# two\n", encoding="utf-8")
        with (
            patch("agents_remember.serving.requirements._MAX_INVENTORY_FILES", 1),
            self._client() as client,
        ):
            too_many = client.get("/api/requirements/list", params=self._params())
        self.assertEqual(too_many.status_code, 400)

    def test_confinement_rejects_symlink_roots_and_failed_containment_proof(self) -> None:
        real = self.tmp / "packets"
        real.mkdir()
        (real / "packet.md").write_text("# packet\n", encoding="utf-8")
        alias = self.tmp / "packets-alias"
        os.symlink(real, alias)
        with self.assertRaises(AuthorityError):
            confine_non_symlink_rel(alias, "packet.md")
        with (
            patch("agents_remember.kernel.sidecar_pairing.path_is_relative_to", return_value=False),
            self.assertRaises(AuthorityError),
        ):
            confine_non_symlink_rel(real, "packet.md")

        self._seed()
        with (
            patch("agents_remember.serving.requirements.path_is_relative_to", return_value=False),
            self._client() as client,
        ):
            response = client.get("/api/requirements/list", params=self._params())
        self.assertEqual(response.status_code, 400)

    def test_surface_is_get_only(self) -> None:
        self._seed()
        with self._client() as client:
            response = client.post(
                "/api/requirements/read", params=self._params(path="CCR-R23-v1.md")
            )
        self.assertEqual(response.status_code, 405)


if __name__ == "__main__":
    unittest.main()
