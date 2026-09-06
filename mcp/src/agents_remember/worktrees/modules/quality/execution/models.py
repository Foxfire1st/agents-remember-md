"""One selected R21 decision with its original, complete predecessor objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from agents_remember.certification.certificate_invalidation import (
    CertificateInputChange,
    CertificateReusePlan,
    plan_certificate_reuse,
)
from agents_remember.certification.certificate_models import GateCertificate
from agents_remember.certification.digests import content_digest
from agents_remember.certification.frozen_run.models import FrozenCertificationRun
from agents_remember.certification.models import GateResultManifest
from agents_remember.worktrees.modules.quality.certification_evidence import (
    verify_publication_authority,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifest,
    parse_published_quality_manifest,
    published_manifest_payload,
)


@dataclass(frozen=True)
class RetainedGateExecution:
    certificate: GateCertificate
    result: GateResultManifest
    publication: PublishedQualityManifest

    def payload(self) -> dict[str, object]:
        return {
            "certificate": self.certificate.model_dump(mode="json"),
            "result": self.result.model_dump(mode="json"),
            "publication": published_manifest_payload(self.publication),
        }


@dataclass(frozen=True)
class CodeCertificationExecution:
    """Exact selected objects; lifecycle selection itself remains the journal owner's job."""

    run: FrozenCertificationRun
    reuse_plan: CertificateReusePlan
    input_changes: tuple[CertificateInputChange, ...]
    certificates: tuple[GateCertificate, ...]
    retained: tuple[RetainedGateExecution, ...]

    def validate(self) -> None:
        first = self.first_gate
        # Validate serialized bytes as well: model_copy and mutable publication mappings
        # must not let a caller bypass the canonical contracts at this launch boundary.
        FrozenCertificationRun.model_validate(self.run.model_dump(mode="json"))
        CertificateReusePlan.model_validate(self.reuse_plan.model_dump(mode="json"))
        changes = tuple(
            CertificateInputChange.model_validate(item.model_dump(mode="json"))
            for item in self.input_changes
        )
        chain = tuple(
            GateCertificate.model_validate(item.model_dump(mode="json"))
            for item in self.certificates
        )
        if self.reuse_plan != plan_certificate_reuse(self.run.admission, chain, changes):
            raise ValueError("selected decision differs from canonical R21 reuse planning")
        expected = chain[: first - 1]
        if (
            tuple(item.certificate for item in self.retained) != expected
            or len(expected) != first - 1
        ):
            raise ValueError("retained execution objects must equal the exact reused prefix")
        for item in self.retained:
            GateResultManifest.model_validate(item.result.model_dump(mode="json"))
            parse_published_quality_manifest(published_manifest_payload(item.publication))
            verify_publication_authority(item.certificate, item.result, item.publication)
            if item.result.disposition != "green":
                raise ValueError("retained execution result must be originally green")

    @property
    def first_gate(self) -> Literal[1, 2, 3, 4]:
        first = self.reuse_plan.firstGateToRun
        if first not in (1, 2, 3, 4):
            raise ValueError("selected recovery has zero code gate starts; Dagger must not launch")
        return cast(Literal[1, 2, 3, 4], first)

    def payload(self, *, diff_base: str) -> dict[str, object]:
        self.validate()
        if not diff_base or diff_base != diff_base.strip():
            raise ValueError("diff_base must name the exact selected comparison commit")
        payload = {
            "schemaVersion": "code-certification-execution/v1",
            "diffBase": diff_base,
            "run": self.run.model_dump(mode="json"),
            "reusePlan": self.reuse_plan.model_dump(mode="json"),
            "inputChanges": [item.model_dump(mode="json") for item in self.input_changes],
            "certificates": [item.model_dump(mode="json") for item in self.certificates],
            "retained": [item.payload() for item in self.retained],
        }
        return {**payload, "executionDigest": content_digest(payload)}
