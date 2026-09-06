"""Closed references to exact canonical bytes in the certificate store."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agents_remember.models.certification.base import FrozenContractModel

CertificateObjectKind = Literal[
    "admission",
    "result-manifest",
    "certificate",
    "finalization",
    "frozen-run",
    "candidate-authorities",
    "lifecycle-admission",
    "prior-red-disposition",
    "recovery",
    "preparation-intent",
    "prepared-output",
]


class CertificateObjectReference(FrozenContractModel):
    """Select an exact object without replacing its original provenance.

    The semantic digest selects its existing kind/address. The byte digest and
    size bind the complete canonical stored representation, including original
    creation evidence. Store location and lifecycle selection remain owned by
    the caller; this reference confers no journal or execution authority.
    """

    schemaVersion: Literal["closeout-certificate-object-reference/v1"] = (
        "closeout-certificate-object-reference/v1"
    )
    kind: CertificateObjectKind
    semanticDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contentSha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sizeBytes: int = Field(strict=True, gt=0, le=10_000_000_000)
