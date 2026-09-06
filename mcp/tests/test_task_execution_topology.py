from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig, RepositoryScope
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.tasks import (
    TaskDocument,
)

REPOSITORY = "agents-remember"
SPRINT = TaskDocumentRef(repository=REPOSITORY, path="sprint/task.json")
MASTER_A = TaskDocumentRef(repository=REPOSITORY, path="master-a/task.json")
MASTER_B = TaskDocumentRef(repository=REPOSITORY, path="master-b/task.json")
MASTER_C = TaskDocumentRef(repository=REPOSITORY, path="master-c/task.json")


def _config(coordination_root: Path, code_repository: Path) -> McpRuntimeConfig:
    scope = RepositoryScope(REPOSITORY, code_repository)
    return cast(
        McpRuntimeConfig,
        SimpleNamespace(
            coordination_root=coordination_root,
            repositories={REPOSITORY: scope},
        ),
    )


def _master(
    *,
    identity: str,
    orchestrates: list[str] | None = None,
    execution_nature: str | None = None,
    execution_graph: dict[str, Any] | None = None,
    title: str | None = None,
) -> TaskDocument:
    return TaskDocument.model_validate(
        {
            "id": identity,
            "slug": identity,
            "title": title or identity,
            "kind": "master",
            "repo": REPOSITORY,
            "type": "Master",
            "createdAt": "2026-08-15T00:00:00+00:00",
            "orchestrates": orchestrates or [],
            "executionNature": execution_nature,
            "executionGraph": execution_graph,
        }
    )
