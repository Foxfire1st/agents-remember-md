"""The complete authority retained before any certification gate starts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.certificate_admission import compile_certification_admission
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    CreationProvenance,
)
from agents_remember.certification.certification_lane import CertificationLane
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CanonicalRailRegistry, CertificationPlan
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    RepositoryProfilePlan,
)
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import FrozenContractModel


class FrozenCertificationRun(FrozenContractModel):
    """Retain original profile, plans, admission and creation evidence together.

    ``runDigest`` identifies this complete retained record, including its original
    provenance. It is not a gate-certificate semantic identity: those existing
    digests continue to exclude creation evidence and lifecycle generations.
    """

    schemaVersion: Literal["closeout-frozen-certification-run/v1"] = (
        "closeout-frozen-certification-run/v1"
    )
    registry: CanonicalRailRegistry
    certificationPlan: CertificationPlan
    repositoryProfile: CanonicalRepositoryCertificationProfile
    repositoryPlan: RepositoryProfilePlan
    admission: CertificationAdmissionManifest
    provenance: CreationProvenance
    runDigest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _verify_authority(self) -> Self:
        try:
            expected = compile_certification_admission(
                self.registry,
                self.certificationPlan,
                self.repositoryProfile,
                self.repositoryPlan,
                provenance=self.provenance,
            )
        except CertificationContractError as error:
            raise ValueError(
                f"frozen certification run authority is not aligned: {error}"
            ) from error
        if self.admission != expected:
            raise ValueError("frozen certification run must retain its exact original admission")
        if self.runDigest != content_digest(self.model_dump(mode="json", exclude={"runDigest"})):
            raise ValueError("frozen certification run digest does not match its complete record")
        return self


def freeze_certification_run(
    repository_profile: CanonicalRepositoryCertificationProfile,
    lane: CertificationLane,
) -> FrozenCertificationRun:
    """Retain the admitted lane using its original creation provenance."""

    payload = {
        "schemaVersion": "closeout-frozen-certification-run/v1",
        "registry": lane.registry.model_dump(mode="json"),
        "certificationPlan": lane.certificationPlan.model_dump(mode="json"),
        "repositoryProfile": repository_profile.model_dump(mode="json"),
        "repositoryPlan": lane.repositoryPlan.model_dump(mode="json"),
        "admission": lane.admission.model_dump(mode="json"),
        "provenance": lane.admission.provenance.model_dump(mode="json"),
    }
    return FrozenCertificationRun.model_validate({**payload, "runDigest": content_digest(payload)})
