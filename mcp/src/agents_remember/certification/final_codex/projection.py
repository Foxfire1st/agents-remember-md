"""Readiness projection for the two-fresh-no-retry Gate-4 lane.

CCR-R14@v3 makes the two fresh independent no-retry certifying repetitions the
only evidence that can satisfy the final real-codex lane, and it forbids any
R13 diagnostic evidence from blocking or promoting into it.  Rules encoded
here:

* an empty lane projects not-started: no scenario step, no owner, and no
  certificate can exist for it;
* a live attempt with no terminal repetitions projects running;
* a terminal run is two-fresh-pass only when its aggregate is green (both
  repetitions passed with distinct fresh identities and retryCount zero) and
  it is current for the frozen plan;
* any red, partial, aborted, hard-failure, or stale run blocks certificate
  readiness and can never be compensated by the other repetition;
* diagnostic evidence never satisfies this lane: it neither unblocks nor
  blocks here, exactly as CCR-R13 projects it.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.final_codex.models import (
    FinalCodexPlanRecord,
    FinalCodexRunManifest,
)
from agents_remember.certification.final_codex.store import FinalCodexManifestStore
from agents_remember.certification.models import CandidateIdentity, FrozenContractModel

FinalCodexLaneDisposition = Literal[
    "not-started",
    "running",
    "two-fresh-pass",
    "red",
    "stale",
]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class FinalCodexLaneProjection(FrozenContractModel):
    """One exact readiness projection of the certifying Gate-4 lane."""

    schemaVersion: Literal["final-codex-lane-projection/v1"] = "final-codex-lane-projection/v1"
    candidateIdentity: CandidateIdentity
    disposition: FinalCodexLaneDisposition
    manifest: FinalCodexRunManifest | None = None
    certificateReady: bool
    projectionDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_shape(self) -> Self:
        if self.disposition == "not-started":
            if self.manifest is not None or self.certificateReady:
                raise ValueError("a not-started lane cannot carry a run manifest or readiness")
        elif self.disposition == "running":
            if self.manifest is None or self.manifest.complete:
                raise ValueError("a running lane requires an incomplete live attempt")
        else:
            _require_terminal_projection(self)
        payload = self.model_dump(mode="json", exclude={"projectionDigest"})
        if self.projectionDigest != content_digest(payload):
            raise ValueError("final-codex lane projection digest does not match its content")
        return self


def _require_terminal_projection(projection: FinalCodexLaneProjection) -> None:
    manifest = projection.manifest
    if manifest is None or not manifest.complete or manifest.attempt.state != "terminal":
        raise ValueError("a terminal lane projection requires a complete terminal run manifest")
    if projection.disposition == "two-fresh-pass":
        expected = manifest.aggregate == "green" and projection.certificateReady
        if not expected:
            raise ValueError(
                "two-fresh-pass requires both fresh green repetitions and certificate readiness"
            )
    elif projection.disposition in {"red", "stale"}:
        if projection.certificateReady:
            raise ValueError("a red or stale lane can never be certificate-ready")
    else:
        raise ValueError(f"unknown terminal lane disposition {projection.disposition!r}")


def project_final_codex_lane(
    store: FinalCodexManifestStore,
    candidate: CandidateIdentity,
    *,
    current_plan: FinalCodexPlanRecord | None = None,
) -> FinalCodexLaneProjection:
    """Project the newest run manifest for one exact candidate.

    Returns not-started only for a candidate whose manifest is completely
    empty.  A live attempt with no complete run projects running.  A terminal
    run that is green and current projects two-fresh-pass; a red terminal run
    projects red and blocks; a current_plan that differs from the run's plan
    identity marks the run stale until a newer run binds the current plan.
    """

    manifest = store.manifest(candidate)
    if manifest is None:
        return _projection(candidate, "not-started", None, certificate_ready=False)
    if not manifest.complete:
        return _projection(candidate, "running", manifest, certificate_ready=False)
    current = current_plan is None or _run_is_current(manifest, current_plan)
    if not current:
        return _projection(candidate, "stale", manifest, certificate_ready=False)
    if manifest.aggregate == "green":
        return _projection(candidate, "two-fresh-pass", manifest, certificate_ready=True)
    return _projection(candidate, "red", manifest, certificate_ready=False)


def final_codex_certificate_ready(
    store: FinalCodexManifestStore,
    candidate: CandidateIdentity,
    *,
    current_plan: FinalCodexPlanRecord | None = None,
) -> bool:
    """True only when the exact two-fresh-pass lane is ready for its certificate."""

    projection = project_final_codex_lane(store, candidate, current_plan=current_plan)
    return projection.certificateReady


def _run_is_current(
    manifest: FinalCodexRunManifest,
    plan: FinalCodexPlanRecord,
) -> bool:
    attempt = manifest.attempt
    return (
        attempt.candidateIdentity == plan.candidateIdentity
        and attempt.registryDigest == plan.registryDigest
        and attempt.certifyingPlanDigest == plan.certifyingPlanDigest
        and attempt.gatePlanDigest == plan.gatePlanDigest
        and attempt.scenarioVersion == plan.scenarioVersion
        and attempt.planVersion == plan.planVersion
        and attempt.gate == plan.gate
        and attempt.profileId == plan.profileId
    )


def _projection(
    candidate: CandidateIdentity,
    disposition: FinalCodexLaneDisposition,
    manifest: FinalCodexRunManifest | None,
    *,
    certificate_ready: bool,
) -> FinalCodexLaneProjection:
    payload = {
        "schemaVersion": "final-codex-lane-projection/v1",
        "candidateIdentity": candidate.model_dump(mode="json"),
        "disposition": disposition,
        "manifest": manifest.model_dump(mode="json") if manifest is not None else None,
        "certificateReady": certificate_ready,
    }
    return FinalCodexLaneProjection(**payload, projectionDigest=content_digest(payload))


__all__ = [
    "FinalCodexLaneDisposition",
    "FinalCodexLaneProjection",
    "final_codex_certificate_ready",
    "project_final_codex_lane",
]
