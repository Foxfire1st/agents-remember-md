"""Optional-lane readiness projection for one non-certifying diagnostic.

CCR-R13 keeps the optional diagnostic lane explicitly separable from the R09
closeout readiness vocabulary: before any request the lane projects
not-requested-optional and no diagnostic artifact, Dagger owner, or
telemetry envelope is fabricated; after a request the lane projects the newest
terminal result for the exact candidate with immutable predecessor links.

Rules encoded here:

* not-requested-optional can only ever be projected from an empty manifest
  and can never erase a requested failure (an empty-lane projection for a
  candidate that has attempts is refused);
* a current requested failure or abort blocks final certification until a
  newer terminal result for the same candidate passes, or a changed candidate
  receives its own disposition; historical passes can never override a newer
  failure because the newest terminal result is selected, never rewritten;
* no number of diagnostic passes can satisfy or be promoted into R14: a pass
  never blocks, but it never marks certification ready either.
"""

from __future__ import annotations

from typing import Literal, Never, Self

from pydantic import Field, model_validator

from agents_remember.certification.diagnostics.models import (
    DiagnosticPlanRecord,
    DiagnosticRunResult,
)
from agents_remember.certification.diagnostics.store import DiagnosticManifestStore
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CandidateIdentity, CertificationContractFinding
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import FrozenContractModel

DiagnosticLaneDisposition = Literal[
    "not-requested-optional",
    "running",
    "pass",
    "fail",
    "aborted",
    "hard-failure",
]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_DISPOSITION_BY_RESULT: dict[str, DiagnosticLaneDisposition] = {
    "pass": "pass",
    "fail": "fail",
    "aborted": "aborted",
    "hard-failure": "hard-failure",
}


class DiagnosticLaneProjection(FrozenContractModel):
    """One exact optional-lane readiness projection for an exact candidate."""

    schemaVersion: Literal["diagnostic-lane-projection/v1"] = "diagnostic-lane-projection/v1"
    candidateIdentity: CandidateIdentity
    requested: bool
    disposition: DiagnosticLaneDisposition
    currentForPlan: bool
    newestResult: DiagnosticRunResult | None = None
    blockingCertification: bool
    projectionDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_shape(self) -> Self:
        if self.disposition == "not-requested-optional":
            if self.requested or self.newestResult is not None or self.blockingCertification:
                raise ValueError(
                    "not-requested-optional cannot coexist with any requested evidence"
                )
        elif self.disposition == "running":
            if self.newestResult is not None:
                raise ValueError("a running lane with no newest terminal cannot carry a result")
        else:
            _require_terminal_projection(self)
        payload = self.model_dump(mode="json", exclude={"projectionDigest"})
        if self.projectionDigest != content_digest(payload):
            raise ValueError("diagnostic lane projection digest does not match its content")
        return self


def _require_terminal_projection(projection: DiagnosticLaneProjection) -> None:
    result = projection.newestResult
    if result is None or _DISPOSITION_BY_RESULT[result.disposition] != projection.disposition:
        raise ValueError("lane disposition must mirror the newest terminal diagnostic result")
    expected = result.disposition != "pass" or not projection.currentForPlan
    if projection.blockingCertification != expected:
        raise ValueError(
            "blockingCertification must follow the newest terminal result disposition "
            "and plan currentness"
        )


def project_diagnostic_lane(
    store: DiagnosticManifestStore,
    candidate: CandidateIdentity,
    *,
    current_plan: DiagnosticPlanRecord | None = None,
) -> DiagnosticLaneProjection:
    """Project the newest terminal result for one exact candidate.

    Returns not-requested-optional only for a candidate whose manifest is
    completely empty.  A live attempt with no terminal result projects
    running.  A current_plan that differs from the newest result's plan
    identity marks the result stale (currentForPlan=false) and blocks
    certification until a newer result binds the current plan.
    """

    manifest = store.manifest(candidate)
    if manifest is None:
        return _projection(
            candidate,
            requested=False,
            disposition="not-requested-optional",
            current_for_plan=True,
        )
    newest = manifest.newestTerminal
    if newest is None:
        if store.live_attempt(candidate) is None:
            _raise_lane(
                "diagnostic lane projection refused",
                "diagnostic-lane-not-optional",
                "manifest",
                "an attempt exists but no terminal result; not-requested-optional was erased",
            )
        return _projection(
            candidate,
            requested=True,
            disposition="running",
            current_for_plan=True,
        )
    current = current_plan is None or _result_is_current(newest, current_plan)
    return _terminal_projection(candidate, newest, current_for_plan=current)


def diagnostic_blocks_certification(
    store: DiagnosticManifestStore,
    candidate: CandidateIdentity,
    *,
    current_plan: DiagnosticPlanRecord | None = None,
) -> bool:
    """True when the newest terminal diagnostic result blocks final certification."""

    projection = project_diagnostic_lane(store, candidate, current_plan=current_plan)
    return projection.blockingCertification


def diagnostic_never_satisfies_certification(
    store: DiagnosticManifestStore,
    candidate: CandidateIdentity,
) -> bool:
    """Diagnostic passes can never satisfy or be promoted into R14.

    This is a stable false: the R14 final proof requires its own two fresh
    certifying replications, and no diagnostic evidence is consumed by it.
    """

    del store, candidate
    return False


def _terminal_projection(
    candidate: CandidateIdentity,
    result: DiagnosticRunResult,
    *,
    current_for_plan: bool,
) -> DiagnosticLaneProjection:
    return _projection(
        candidate,
        requested=True,
        disposition=_DISPOSITION_BY_RESULT[result.disposition],
        current_for_plan=current_for_plan,
        newest_result=result,
    )


def _result_is_current(
    result: DiagnosticRunResult,
    plan: DiagnosticPlanRecord,
) -> bool:
    return (
        result.candidateIdentity == plan.candidateIdentity
        and result.registryDigest == plan.registryDigest
        and result.certifyingPlanDigest == plan.certifyingPlanDigest
        and result.diagnosticPlanDigest == plan.diagnosticPlanDigest
        and result.planVersion == plan.planVersion
        and result.gate == plan.gate
        and result.profileId == plan.profileId
    )


def _projection(
    candidate: CandidateIdentity,
    *,
    requested: bool,
    disposition: DiagnosticLaneDisposition,
    current_for_plan: bool,
    newest_result: DiagnosticRunResult | None = None,
) -> DiagnosticLaneProjection:
    blocking = newest_result is not None and (
        not current_for_plan or newest_result.disposition != "pass"
    )
    payload = {
        "schemaVersion": "diagnostic-lane-projection/v1",
        "candidateIdentity": candidate.model_dump(mode="json"),
        "requested": requested,
        "disposition": disposition,
        "currentForPlan": current_for_plan,
        "newestResult": newest_result.model_dump(mode="json")
        if newest_result is not None
        else None,
        "blockingCertification": blocking,
    }
    return DiagnosticLaneProjection(**payload, projectionDigest=content_digest(payload))


def _raise_lane(detail: str, code: str, path: str, message: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=message)
    raise CertificationContractError(
        f"{detail}: {message}",
        (finding.model_dump(mode="json"),),
    )


__all__ = [
    "DiagnosticLaneDisposition",
    "DiagnosticLaneProjection",
    "diagnostic_blocks_certification",
    "diagnostic_never_satisfies_certification",
    "project_diagnostic_lane",
]
