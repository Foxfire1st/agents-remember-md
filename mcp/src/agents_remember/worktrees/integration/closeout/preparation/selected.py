"""One original journal-selected closeout preparation and its current owner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.preparation import CloseoutPreparationIntent
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
)


@dataclass(frozen=True)
class SelectedCloseoutPreparation:
    handoff: CloseoutCertificationHandoff
    intent: CloseoutPreparationIntent
    reference: CertificateObjectReference


ReobservePreparation = Callable[[SelectedCloseoutPreparation], SelectedCloseoutPreparation]
