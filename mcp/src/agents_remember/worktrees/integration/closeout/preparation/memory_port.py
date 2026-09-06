"""Typed production boundary between private code preparation and memory certification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from agents_remember.certification.certificate_models import GateFiveSemanticInputs
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.prepared_memory import PreparedMemoryCandidate

if TYPE_CHECKING:
    from agents_remember.worktrees.integration.closeout.certification.execution import (
        CloseoutCertificationHandoff,
    )


@dataclass(frozen=True)
class PreparedMemoryCertificationRequest:
    """Exact selected prefix, live owner and reprovable physical read view."""

    handoff: CloseoutCertificationHandoff
    candidate: PreparedMemoryCandidate


@dataclass(frozen=True)
class PreparedMemoryCertificationResult:
    """Original selected authorities for the actual realized memory-content tree.

    The caller must load and validate these originals through their owning store,
    reobserve the exact current pair and verify the resulting selected journal.
    A constructed response alone cannot authorize any publication leg.
    """

    handoff: CloseoutCertificationHandoff
    candidate: PreparedMemoryCandidate
    memoryInputs: GateFiveSemanticInputs
    resultManifest: CertificateObjectReference
    certificate: CertificateObjectReference


class PreparedMemoryCertificationPort(Protocol):
    """Real memory producer; missing composition is an explicit prepublication refusal."""

    def observe(
        self, request: PreparedMemoryCertificationRequest
    ) -> GateFiveSemanticInputs | None: ...

    def certify(
        self, request: PreparedMemoryCertificationRequest
    ) -> PreparedMemoryCertificationResult: ...
