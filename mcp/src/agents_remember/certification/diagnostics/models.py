"""Closed contracts for one optional non-certifying diagnostic E2E lane.

CCR-R13 allows a developer or architect to run at most one real-Codex
replication of the canonical ARSPAWN scenario as explicitly non-certifying
diagnostic evidence after the exact candidate's R12 Gates 1-3 are green.  The
records here make the separation structural rather than conventional:

* every result and artifact carries acceptanceEligible=false and
  certifying=false and binds the exact candidate, diagnostic plan version,
  environment identity, R12 runtime-authority snapshot digest, and the R16
  diagnostic nonce;
* no record can name a delivery attempt, closeout operation generation, door
  claim, or accepted certification state (the closed shape simply has no such
  field, and every promotion path in the compiler refuses);
* pass/fail scenario outcomes embed the complete diagnostic-altitude gate
  result manifest, aborted and hard-failure outcomes carry teardown evidence
  but never a pass;
* infrastructure/parser failures remain a typed hard diagnostic failure.

The vocabulary is repository-neutral and mirrors the immutable frozen-model
style of the certification domain.  FrozenContractModel forbids extra
fields and every digest-bearing record verifies its own content digest.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CandidateIdentity,
    GateResultManifest,
    RailEvidenceReference,
)
from agents_remember.models.certification.base import (
    FrozenContractModel,
    GateId,
    RailIdentity,
    SemanticText,
)

DiagnosticDisposition = Literal["pass", "fail", "aborted", "hard-failure"]
DiagnosticFailureClass = Literal["scenario", "infrastructure", "parser"]
DiagnosticAttemptState = Literal["reserved", "running", "terminal"]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_NONCE_PATTERN = r"^[0-9a-f]{16,128}$"
_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_VERSION_PATTERN = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-((?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _require_semantic_text(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("semantic text must be nonblank and unpadded")
    return value


SemanticTimestamp = Annotated[str, AfterValidator(_require_semantic_text)]


class DiagnosticArtifact(FrozenContractModel):
    """One content-addressed artifact in the isolated diagnostic namespace."""

    artifactId: str = Field(pattern=_ID_PATTERN, max_length=128)
    sha256: str = Field(pattern=_DIGEST_PATTERN)
    size: int = Field(ge=0, le=1_000_000_000_000)
    evidenceRef: SemanticText = Field(max_length=16384)
    namespace: Literal["diagnostic"] = "diagnostic"


class DiagnosticFailureRecord(FrozenContractModel):
    """The exact typed failure a non-passing diagnostic terminal reports."""

    failureClass: DiagnosticFailureClass
    code: str = Field(pattern=_ID_PATTERN, max_length=128)
    detail: SemanticText = Field(max_length=16384)
    rail: RailIdentity | None = None
    correctiveOwner: str = Field(pattern=_ID_PATTERN, max_length=128)
    evidence: tuple[RailEvidenceReference, ...] = Field(default_factory=tuple, max_length=128)

    @model_validator(mode="after")
    def _require_failure_shape(self) -> Self:
        if self.failureClass == "scenario" and self.rail is None:
            raise ValueError("a scenario failure must name the exact failing checkpoint rail")
        if self.failureClass in {"infrastructure", "parser"} and not self.evidence:
            raise ValueError("infrastructure and parser failures require bounded evidence")
        return self


class DiagnosticTeardownRecord(FrozenContractModel):
    """Terminalization facts: evidence plus exact-owner release semantics."""

    releasedOwner: bool
    evidence: tuple[RailEvidenceReference, ...] = Field(default_factory=tuple, max_length=128)
    at: SemanticTimestamp = Field(max_length=128)

    @model_validator(mode="after")
    def _require_release_shape(self) -> Self:
        if self.releasedOwner and not self.evidence:
            raise ValueError("an owner release requires bounded teardown evidence")
        return self


class DiagnosticRuntimeAuthorityBinding(FrozenContractModel):
    """The exact frozen R12 host runner/store snapshot a result is bound to.

    This is a binding, never an authority of its own: the endpoint, engine, and
    layer-store identities are copied from the immutable snapshot admitted by
    the trusted R12 launcher and are never selected, replaced, or provisioned
    here.
    """

    schemaVersion: Literal["diagnostic-runtime-authority/v1"] = "diagnostic-runtime-authority/v1"
    snapshotDigest: str = Field(pattern=_DIGEST_PATTERN)
    endpoint: SemanticText = Field(max_length=512)
    engineId: SemanticText = Field(max_length=512)
    layerStore: SemanticText = Field(max_length=1024)
    storeMountedPath: SemanticText = Field(max_length=1024)
    observedVersion: SemanticText | None = Field(default=None, max_length=256)
    bindingDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"bindingDigest"})
        if self.bindingDigest != content_digest(payload):
            raise ValueError("runtime authority binding digest does not match its content")
        return self


class DiagnosticEnvironmentBinding(FrozenContractModel):
    """One frozen environment identity bound to the exact candidate and plan."""

    environmentIdentity: SemanticText = Field(max_length=512)
    environmentDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"environmentDigest"})
        if self.environmentDigest != content_digest(payload):
            raise ValueError("environment binding digest does not match its identity")
        return self


class DiagnosticAttemptRecord(FrozenContractModel):
    """One immutable in-flight or terminal attempt reservation slot.

    The reservation binds the attempt number, the R16 diagnostic nonce, the
    exact candidate, and the exact plans before any scenario step starts, so a
    repeated execution can only ever append a new attempt and never rewrite or
    promote a prior one.
    """

    schemaVersion: Literal["diagnostic-attempt/v1"] = "diagnostic-attempt/v1"
    candidateIdentity: CandidateIdentity
    attemptNumber: int = Field(ge=1)
    diagnosticNonce: str = Field(pattern=_NONCE_PATTERN)
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certifyingPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    diagnosticPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    planVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    gate: GateId
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    requestedAt: SemanticTimestamp = Field(max_length=128)
    state: DiagnosticAttemptState = "reserved"
    attemptDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"attemptDigest"})
        if self.attemptDigest != content_digest(payload):
            raise ValueError("diagnostic attempt digest does not match its content")
        return self


class DiagnosticPlanRecord(FrozenContractModel):
    """One immutable diagnostic-plan identity for durable attempt binding.

    The record projects the R11 canonical scenario rail at diagnostic altitude:
    it names the canonical registry, the exact certifying plan and the exact
    diagnostic plan compiled from it, the scenario gate, the scenario gate-plan
    digest, and the frozen plan version every attempt and result binds.
    """

    schemaVersion: Literal["diagnostic-plan-record/v1"] = "diagnostic-plan-record/v1"
    candidateIdentity: CandidateIdentity
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certifyingPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    diagnosticPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    gate: GateId
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    planVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    planDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"planDigest"})
        if self.planDigest != content_digest(payload):
            raise ValueError("diagnostic plan record digest does not match its content")
        return self


def _require_outcome_shape(
    value: DiagnosticRunResult | DiagnosticRunResultDraft,
) -> None:
    _require_manifest_carrier(value)
    _require_failure_carrier(value)
    _require_manifest_binding(value)


def _require_manifest_carrier(
    value: DiagnosticRunResult | DiagnosticRunResultDraft,
) -> None:
    if value.disposition in {"pass", "fail"}:
        if value.manifest is None:
            raise ValueError("pass and fail outcomes require the complete result manifest")
        expected = "green" if value.disposition == "pass" else "red"
        if value.manifest.disposition != expected:
            raise ValueError("outcome disposition must match its complete result manifest")
        if value.manifest.altitude != "diagnostic":
            raise ValueError("diagnostic outcomes may only embed diagnostic-altitude manifests")
    elif value.manifest is not None:
        raise ValueError("aborted and hard-failure outcomes never embed a result manifest")


def _require_failure_carrier(
    value: DiagnosticRunResult | DiagnosticRunResultDraft,
) -> None:
    if value.disposition == "fail":
        if value.failure is None or value.failure.failureClass != "scenario":
            raise ValueError("a failed replication requires its exact scenario failure")
    elif value.disposition == "hard-failure":
        if value.failure is None or value.failure.failureClass not in {
            "infrastructure",
            "parser",
        }:
            raise ValueError("hard failures require a typed infrastructure/parser failure")
    elif value.failure is not None:
        raise ValueError("passing and aborted outcomes carry no failure record")


def _require_manifest_binding(
    value: DiagnosticRunResult | DiagnosticRunResultDraft,
) -> None:
    manifest = value.manifest
    if manifest is None:
        return
    if manifest.gate != value.gate:
        raise ValueError("result manifest must belong to the exact diagnostic gate")
    if manifest.candidateIdentity != value.candidateIdentity:
        raise ValueError("result manifest must bind the exact candidate identity")


class DiagnosticRunResultDraft(FrozenContractModel):
    """A terminal outcome before the store binds its chain identity.

    The store derives the predecessor digest from the newest retained terminal
    result and then computes the immutable result digest, so a draft carries
    every semantic field but no chain identity of its own.
    """

    schemaVersion: Literal["diagnostic-run-result/v1"] = "diagnostic-run-result/v1"
    attemptNumber: int = Field(ge=1)
    candidateIdentity: CandidateIdentity
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certifyingPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    diagnosticPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    planVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    gate: GateId
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    diagnosticNonce: str = Field(pattern=_NONCE_PATTERN)
    acceptanceEligible: Literal[False] = False
    certifying: Literal[False] = False
    disposition: DiagnosticDisposition
    failure: DiagnosticFailureRecord | None = None
    environment: DiagnosticEnvironmentBinding
    runtimeAuthority: DiagnosticRuntimeAuthorityBinding
    manifest: GateResultManifest | None = None
    teardown: DiagnosticTeardownRecord
    artifacts: tuple[DiagnosticArtifact, ...] = Field(default_factory=tuple, max_length=256)
    startedAt: SemanticTimestamp = Field(max_length=128)
    terminalAt: SemanticTimestamp = Field(max_length=128)

    @model_validator(mode="after")
    def _verify_draft(self) -> Self:
        _require_outcome_shape(self)
        return self


class DiagnosticRunResult(FrozenContractModel):
    """One immutable terminal diagnostic result for one exact replication.

    Shape rules encode CCR-R13 eligibility separation:

    * acceptanceEligible and certifying are structural false
      literals, so no caller can flip a diagnostic result into an accepted or
      certifying one without constructing a different closed type;
    * a pass/fail outcome embeds the complete diagnostic-altitude gate result
      manifest; aborted and hard-failure outcomes never embed a manifest;
    * hard failures are typed infrastructure/parser failures, and every
      terminal outcome carries the teardown record.
    """

    schemaVersion: Literal["diagnostic-run-result/v1"] = "diagnostic-run-result/v1"
    resultId: str = Field(pattern=_DIGEST_PATTERN)
    attemptNumber: int = Field(ge=1)
    candidateIdentity: CandidateIdentity
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certifyingPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    diagnosticPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    planVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    gate: GateId
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    diagnosticNonce: str = Field(pattern=_NONCE_PATTERN)
    acceptanceEligible: Literal[False] = False
    certifying: Literal[False] = False
    disposition: DiagnosticDisposition
    failure: DiagnosticFailureRecord | None = None
    environment: DiagnosticEnvironmentBinding
    runtimeAuthority: DiagnosticRuntimeAuthorityBinding
    manifest: GateResultManifest | None = None
    teardown: DiagnosticTeardownRecord
    artifacts: tuple[DiagnosticArtifact, ...] = Field(default_factory=tuple, max_length=256)
    predecessorDigest: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    startedAt: SemanticTimestamp = Field(max_length=128)
    terminalAt: SemanticTimestamp = Field(max_length=128)
    resultDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"resultDigest"})
        if self.resultDigest != content_digest(payload):
            raise ValueError("diagnostic result digest does not match its content")
        return self

    @model_validator(mode="after")
    def _verify_outcome(self) -> Self:
        _require_outcome_shape(self)
        return self


class DiagnosticRunManifest(FrozenContractModel):
    """One stable manifest of immutable attempts for one exact candidate.

    The manifest is a projection over the durable attempt journal: results are
    ordered by attempt number, predecessor links are revalidated on every read,
    and the current terminal result is always the newest terminal result for
    the exact candidate.  A historical pass can never override a newer failure
    because selection is derived, never rewritten.
    """

    schemaVersion: Literal["diagnostic-run-manifest/v1"] = "diagnostic-run-manifest/v1"
    candidateIdentity: CandidateIdentity
    attempts: tuple[DiagnosticAttemptRecord, ...] = Field(default_factory=tuple, max_length=4096)
    results: tuple[DiagnosticRunResult, ...] = Field(default_factory=tuple, max_length=4096)
    manifestDigest: str = Field(pattern=_DIGEST_PATTERN)

    @property
    def newestTerminal(self) -> DiagnosticRunResult | None:
        return self.results[-1] if self.results else None

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        _require_manifest_attempt_shape(self)
        _require_manifest_result_chain(self)
        _require_manifest_terminal_cross_check(self)
        payload = self.model_dump(mode="json", exclude={"manifestDigest"})
        if self.manifestDigest != content_digest(payload):
            raise ValueError("diagnostic run manifest digest does not match its content")
        return self


def _require_manifest_attempt_shape(manifest: DiagnosticRunManifest) -> None:
    numbers = tuple(item.attemptNumber for item in manifest.attempts)
    if numbers != tuple(range(1, len(numbers) + 1)):
        raise ValueError("diagnostic attempts must be a gapless attempt-number prefix")
    if any(item.candidateIdentity != manifest.candidateIdentity for item in manifest.attempts):
        raise ValueError("diagnostic attempts must bind the manifest candidate")
    if len({item.diagnosticNonce for item in manifest.attempts}) != len(manifest.attempts):
        raise ValueError("diagnostic attempt nonces must be unique")


def _require_manifest_result_chain(manifest: DiagnosticRunManifest) -> None:
    numbers = tuple(item.attemptNumber for item in manifest.results)
    if numbers != tuple(range(1, len(numbers) + 1)):
        raise ValueError("diagnostic results must be a gapless attempt-number prefix")
    if any(item.candidateIdentity != manifest.candidateIdentity for item in manifest.results):
        raise ValueError("diagnostic results must bind the manifest candidate")
    for previous, current in zip(manifest.results, manifest.results[1:], strict=False):
        if current.predecessorDigest != previous.resultId:
            raise ValueError("diagnostic result chain predecessor link is broken")


def _require_manifest_terminal_cross_check(manifest: DiagnosticRunManifest) -> None:
    by_number = {item.attemptNumber: item for item in manifest.attempts}
    for result in manifest.results:
        attempt = by_number.get(result.attemptNumber)
        if attempt is None:
            raise ValueError("diagnostic result has no matching attempt reservation")
        if attempt.state != "terminal":
            raise ValueError("a terminal result requires its attempt slot to be terminal")
        if attempt.diagnosticNonce != result.diagnosticNonce:
            raise ValueError("a diagnostic result must bind its exact attempt nonce")


__all__ = [
    "DiagnosticArtifact",
    "DiagnosticAttemptRecord",
    "DiagnosticAttemptState",
    "DiagnosticDisposition",
    "DiagnosticEnvironmentBinding",
    "DiagnosticFailureClass",
    "DiagnosticFailureRecord",
    "DiagnosticPlanRecord",
    "DiagnosticRunManifest",
    "DiagnosticRunResult",
    "DiagnosticRunResultDraft",
    "DiagnosticRuntimeAuthorityBinding",
    "DiagnosticTeardownRecord",
]
