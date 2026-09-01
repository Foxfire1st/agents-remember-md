"""One immutable observation of closeout projection source state."""

from __future__ import annotations

from dataclasses import dataclass

from agents_remember.controlplane.closeout_queue_records import CloseoutProjectionBuild
from agents_remember.controlplane.closeout_queue_store import ProjectionSourceIdentity
from agents_remember.models.closeout.projection import (
    CloseoutProjectionMember,
    ProjectionSourceClassification,
)
from agents_remember.models.task_document_ref import TaskDocumentRef


@dataclass(frozen=True)
class ProjectionSourceSnapshot:
    """One exact current source census; old projection rows are never an input."""

    identity: ProjectionSourceIdentity
    classification: ProjectionSourceClassification | None
    members: tuple[CloseoutProjectionMember, ...]
    captured_at: str

    def build(self, sprint_ref: TaskDocumentRef) -> CloseoutProjectionBuild | None:
        fingerprint = self.identity.fingerprint
        classification = self.classification
        if not self.identity.readable or fingerprint is None or classification is None:
            return None
        return CloseoutProjectionBuild(
            sprintTaskDocumentRef=sprint_ref,
            sourceFingerprint=fingerprint,
            sourceClassification=classification,
            members=list(self.members),
            builtAt=self.captured_at,
        )


__all__ = ["ProjectionSourceSnapshot"]
