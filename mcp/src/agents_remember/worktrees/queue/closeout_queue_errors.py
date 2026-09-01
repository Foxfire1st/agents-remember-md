"""Shared fail-closed error for closeout queue application and lifecycle services."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from agents_remember.errors import AgentsRememberError
from agents_remember.models.task_document_ref import TaskDocumentRef
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)


class CloseoutQueueError(AgentsRememberError):
    """A queue request is malformed or violates the current mechanistic facts."""

    def __init__(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


def bounded_queue_failure_detail(
    error: Exception,
    *,
    stage: str,
    side: str,
    name: str,
) -> str:
    """Serialize one stable queue failure without backend text or offending input."""

    return json.dumps(
        public_failure_evidence(
            stage=stage,
            side=side,
            name=name,
            error_type=type(error).__name__,
            observed={"state": "blocked"},
        ),
        sort_keys=True,
    )


def queue_task_ref(
    raw: TaskDocumentRef | dict[str, Any] | None,
    label: str,
) -> TaskDocumentRef:
    """Validate one request-carried task-document reference, fail closed."""

    if raw is None:
        raise CloseoutQueueError("closeout-queue-reference-required", f"{label} is required")
    if isinstance(raw, TaskDocumentRef):
        return raw
    try:
        return TaskDocumentRef.model_validate(raw)
    except ValidationError as exc:
        raise CloseoutQueueError(
            "closeout-queue-reference-invalid",
            bounded_queue_failure_detail(
                exc,
                stage="queue-request-validation",
                side="request",
                name=label,
            ),
        ) from exc
