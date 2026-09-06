"""Closed contracts for the final real-Codex Gate-4 certification lane.

CCR-R14@v3 requires exactly two fresh independent no-retry certifying
repetitions of the exact candidate's canonical scenario before one bound
Gate-4 certificate can publish.  The records here mirror the immutable
frozen-contract style of the certification domain and make the R14 semantics
structural rather than conventional:

* every terminal repetition result carries acceptanceEligible=true and
  certifying=true structural literals and binds the exact candidate, the exact
  compiled plan identities, one frozen R12 host runner/store snapshot, and a
  per-repetition fresh client/process identity;
* retry is disabled structurally: a repetition slot is either a first fresh
  run or does not exist, retryCount is a fixed zero literal, and no record
  can carry a retry provenance or a successor slot for the same plan;
* one passing repetition can never compensate for a failed, aborted, timed
  out, retried, stale, or authority-mismatched repetition, because the run
  manifest disposition is derived only from both immutable results;
* every terminal outcome carries the teardown and process-cleanliness record;
  infrastructure/parser failures remain typed hard failures and never pass;
* diagnostic evidence (CCR-R13) has no shape that can enter this lane, and no
  projection path here can satisfy or be satisfied by diagnostic evidence.

The vocabulary is repository-neutral.  FrozenContractModel forbids extra
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
    RailIdentity,
    SemanticText,
)

FinalCodexDisposition = Literal["pass", "fail", "aborted", "hard-failure"]
FinalCodexFailureClass = Literal["scenario", "infrastructure", "parser"]
FinalCodexAttemptState = Literal["reserved", "running", "terminal"]

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
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

CERTIFYING_GATE: Literal[4] = 4
REPETITION_COUNT = 2


class FinalCodexArtifact(FrozenContractModel):
    """One content-addressed artifact in the isolated final-codex namespace."""

    artifactId: str = Field(pattern=_ID_PATTERN, max_length=128)
    sha256: str = Field(pattern=_DIGEST_PATTERN)
    size: int = Field(ge=0, le=1_000_000_000_000)
    evidenceRef: SemanticText = Field(max_length=16384)
    namespace: Literal["final-codex"] = "final-codex"


class FinalCodexFailureRecord(FrozenContractModel):
    """The exact typed failure a non-passing final-codex terminal reports."""

    failureClass: FinalCodexFailureClass
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


class FinalCodexTeardownRecord(FrozenContractModel):
    """Terminalization facts: exact-owner release plus process cleanliness."""

    releasedOwner: bool
    processClean: bool
    evidence: tuple[RailEvidenceReference, ...] = Field(default_factory=tuple, max_length=128)
    at: SemanticTimestamp = Field(max_length=128)

    @model_validator(mode="after")
    def _require_release_shape(self) -> Self:
        if self.releasedOwner and not self.evidence:
            raise ValueError("an owner release requires bounded teardown evidence")
        if not self.processClean and not self.evidence:
            raise ValueError("a process-cleanliness failure requires bounded evidence")
        return self


class FinalCodexRuntimeAuthorityBinding(FrozenContractModel):
    """The exact frozen R12 host runner/store snapshot a certifying run binds.

    This is a binding, never an authority of its own: the endpoint, engine, and
    layer-store identities are copied from the immutable snapshot admitted by
    the trusted R12 launcher and are never selected, replaced, or provisioned
    here.
    """

    schemaVersion: Literal["final-codex-runtime-authority/v1"] = "final-codex-runtime-authority/v1"
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


class FinalCodexEnvironmentBinding(FrozenContractModel):
    """One frozen environment identity bound to the exact candidate and plan."""

    environmentIdentity: SemanticText = Field(max_length=512)
    environmentDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"environmentDigest"})
        if self.environmentDigest != content_digest(payload):
            raise ValueError("environment binding digest does not match its identity")
        return self


class FinalCodexRepetitionIdentity(FrozenContractModel):
    """One fresh client/process identity for one certifying repetition.

    The two certifying repetitions are independent only when their client and
    process identities are distinct; the run manifest validator enforces that
    no identity is shared between slots.
    """

    clientIdentity: SemanticText = Field(max_length=512)
    processFingerprint: SemanticText = Field(max_length=512)
    pid: int = Field(ge=1)

    @property
    def key(self) -> tuple[str, str, int]:
        return self.clientIdentity, self.processFingerprint, self.pid


class FinalCodexPlanRecord(FrozenContractModel):
    """One immutable certifying-plan identity a two-repetition run binds.

    The record projects the exact R11 canonical scenario gate at certifying
    altitude for the exact candidate: it names the registry, the exact
    certifying plan compiled from it, the Gate-4 plan digest, the frozen
    scenario/prompt version, and the repository profile identity every attempt
    and repetition binds.
    """

    schemaVersion: Literal["final-codex-plan-record/v1"] = "final-codex-plan-record/v1"
    candidateIdentity: CandidateIdentity
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certifyingPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    gate: Literal[4] = CERTIFYING_GATE
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    scenarioVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    planVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    planDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"planDigest"})
        if self.planDigest != content_digest(payload):
            raise ValueError("final-codex plan record digest does not match its content")
        return self


class FinalCodexAttemptRecord(FrozenContractModel):
    """One immutable in-flight or terminal reservation for a two-repetition run.

    The reservation binds the exact candidate, the exact plans, and the two
    repetition identities before any scenario step starts.  Retry is disabled:
    an attempt has no successor link and the lane store refuses a second live
    attempt for the same plan.  A code/config/runtime repair changes the plan
    identity and therefore admits a genuinely fresh attempt.
    """

    schemaVersion: Literal["final-codex-attempt/v1"] = "final-codex-attempt/v1"
    candidateIdentity: CandidateIdentity
    attemptNumber: int = Field(ge=1)
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certifyingPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    scenarioVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    planVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    gate: Literal[4] = CERTIFYING_GATE
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    repetitionIdentities: tuple[FinalCodexRepetitionIdentity, ...] = Field(
        min_length=REPETITION_COUNT,
        max_length=REPETITION_COUNT,
    )
    requestedAt: SemanticTimestamp = Field(max_length=128)
    state: FinalCodexAttemptState = "reserved"
    attemptDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"attemptDigest"})
        if self.attemptDigest != content_digest(payload):
            raise ValueError("final-codex attempt digest does not match its content")
        return self

    @model_validator(mode="after")
    def _require_fresh_repetitions(self) -> Self:
        keys = [identity.key for identity in self.repetitionIdentities]
        if len(set(keys)) != REPETITION_COUNT:
            raise ValueError("the two certifying repetitions must carry fresh distinct identities")
        return self


def _require_outcome_shape(
    value: FinalCodexRepetitionResult | FinalCodexRepetitionResultDraft,
) -> None:
    _require_manifest_carrier(value)
    _require_failure_carrier(value)
    _require_manifest_binding(value)


def _require_manifest_carrier(
    value: FinalCodexRepetitionResult | FinalCodexRepetitionResultDraft,
) -> None:
    if value.disposition in {"pass", "fail"}:
        if value.manifest is None:
            raise ValueError("pass and fail outcomes require the complete result manifest")
        expected = "green" if value.disposition == "pass" else "red"
        if value.manifest.disposition != expected:
            raise ValueError("outcome disposition must match its complete result manifest")
        if value.manifest.profileKind != "certifying" or value.manifest.altitude != "certifying":
            raise ValueError("certifying outcomes may only embed certifying-altitude manifests")
        if value.manifest.gate != 4:
            raise ValueError("final-codex outcomes may only embed the Gate-4 result manifest")
    elif value.manifest is not None:
        raise ValueError("aborted and hard-failure outcomes never embed a result manifest")


def _require_failure_carrier(
    value: FinalCodexRepetitionResult | FinalCodexRepetitionResultDraft,
) -> None:
    if value.disposition == "fail":
        if value.failure is None or value.failure.failureClass != "scenario":
            raise ValueError("a failed repetition requires its exact scenario failure")
    elif value.disposition == "hard-failure":
        if value.failure is None or value.failure.failureClass not in {
            "infrastructure",
            "parser",
        }:
            raise ValueError("hard failures require a typed infrastructure/parser failure")
    elif value.failure is not None:
        raise ValueError("passing and aborted outcomes carry no failure record")


def _require_manifest_binding(
    value: FinalCodexRepetitionResult | FinalCodexRepetitionResultDraft,
) -> None:
    manifest = value.manifest
    if manifest is None:
        return
    if manifest.gate != value.gate:
        raise ValueError("result manifest must belong to the exact Gate-4 plan")
    if manifest.candidateIdentity != value.candidateIdentity:
        raise ValueError("result manifest must bind the exact candidate identity")
    if manifest.profileId != value.profileId:
        raise ValueError("result manifest must bind the exact repository profile")


class FinalCodexRepetitionResultDraft(FrozenContractModel):
    """A terminal repetition outcome before the store binds its chain identity."""

    schemaVersion: Literal["final-codex-repetition-result/v1"] = "final-codex-repetition-result/v1"
    repetitionNumber: int = Field(ge=1, le=REPETITION_COUNT)
    candidateIdentity: CandidateIdentity
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certifyingPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    scenarioVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    planVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    gate: Literal[4] = CERTIFYING_GATE
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    acceptanceEligible: Literal[True] = True
    certifying: Literal[True] = True
    retryCount: Literal[0] = 0
    repetitionIdentity: FinalCodexRepetitionIdentity
    disposition: FinalCodexDisposition
    failure: FinalCodexFailureRecord | None = None
    environment: FinalCodexEnvironmentBinding
    runtimeAuthority: FinalCodexRuntimeAuthorityBinding
    manifest: GateResultManifest | None = None
    teardown: FinalCodexTeardownRecord
    artifacts: tuple[FinalCodexArtifact, ...] = Field(default_factory=tuple, max_length=256)
    startedAt: SemanticTimestamp = Field(max_length=128)
    terminalAt: SemanticTimestamp = Field(max_length=128)

    @model_validator(mode="after")
    def _verify_draft(self) -> Self:
        _require_outcome_shape(self)
        return self


class FinalCodexRepetitionResult(FrozenContractModel):
    """One immutable terminal result for one fresh certifying repetition."""

    schemaVersion: Literal["final-codex-repetition-result/v1"] = "final-codex-repetition-result/v1"
    resultId: str = Field(pattern=_DIGEST_PATTERN)
    repetitionNumber: int = Field(ge=1, le=REPETITION_COUNT)
    candidateIdentity: CandidateIdentity
    registryDigest: str = Field(pattern=_DIGEST_PATTERN)
    certifyingPlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    gatePlanDigest: str = Field(pattern=_DIGEST_PATTERN)
    scenarioVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    planVersion: SemanticText = Field(pattern=_VERSION_PATTERN)
    gate: Literal[4] = CERTIFYING_GATE
    profileId: str = Field(pattern=_ID_PATTERN, max_length=128)
    acceptanceEligible: Literal[True] = True
    certifying: Literal[True] = True
    retryCount: Literal[0] = 0
    repetitionIdentity: FinalCodexRepetitionIdentity
    disposition: FinalCodexDisposition
    failure: FinalCodexFailureRecord | None = None
    environment: FinalCodexEnvironmentBinding
    runtimeAuthority: FinalCodexRuntimeAuthorityBinding
    manifest: GateResultManifest | None = None
    teardown: FinalCodexTeardownRecord
    artifacts: tuple[FinalCodexArtifact, ...] = Field(default_factory=tuple, max_length=256)
    predecessorDigest: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    startedAt: SemanticTimestamp = Field(max_length=128)
    terminalAt: SemanticTimestamp = Field(max_length=128)
    resultDigest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def _verify_digest(self) -> Self:
        payload = self.model_dump(mode="json", exclude={"resultDigest"})
        if self.resultDigest != content_digest(payload):
            raise ValueError("final-codex repetition result digest does not match its content")
        return self

    @model_validator(mode="after")
    def _verify_outcome(self) -> Self:
        _require_outcome_shape(self)
        return self


class FinalCodexRunManifest(FrozenContractModel):
    """One stable immutable manifest of a two-repetition Gate-4 run.

    The manifest carries the reserved attempt and the two terminal repetition
    results in fixed slot order.  Its derived aggregate is green only when
    both repetitions pass with distinct fresh identities under the exact same
    plan and runtime authority; any other composition keeps the lane red, so
    one pass can never compensate for the other.
    """

    schemaVersion: Literal["final-codex-run-manifest/v1"] = "final-codex-run-manifest/v1"
    candidateIdentity: CandidateIdentity
    attempt: FinalCodexAttemptRecord
    repetitions: tuple[FinalCodexRepetitionResult, ...] = Field(max_length=REPETITION_COUNT)
    manifestDigest: str = Field(pattern=_DIGEST_PATTERN)

    @property
    def aggregate(self) -> Literal["green", "red"]:
        if len(self.repetitions) != REPETITION_COUNT:
            return "red"
        if any(item.disposition != "pass" for item in self.repetitions):
            return "red"
        if any(item.retryCount != 0 for item in self.repetitions):
            return "red"
        return "green"

    @property
    def complete(self) -> bool:
        return len(self.repetitions) == REPETITION_COUNT

    @model_validator(mode="after")
    def _verify_manifest(self) -> Self:
        _require_manifest_attempt(self)
        _require_manifest_repetitions(self)
        payload = self.model_dump(mode="json", exclude={"manifestDigest"})
        if self.manifestDigest != content_digest(payload):
            raise ValueError("final-codex run manifest digest does not match its content")
        return self


def _require_manifest_attempt(manifest: FinalCodexRunManifest) -> None:
    if manifest.attempt.candidateIdentity != manifest.candidateIdentity:
        raise ValueError("the attempt must bind the manifest candidate")
    if manifest.attempt.state == "terminal" and len(manifest.repetitions) != REPETITION_COUNT:
        raise ValueError("a terminal attempt requires both terminal repetition results")
    if manifest.attempt.state == "reserved" and manifest.repetitions:
        raise ValueError("a reserved attempt cannot carry published repetition results")


def _require_manifest_repetitions(manifest: FinalCodexRunManifest) -> None:
    if not manifest.repetitions:
        return
    expected = tuple(range(1, len(manifest.repetitions) + 1))
    if tuple(item.repetitionNumber for item in manifest.repetitions) != expected:
        raise ValueError("certifying repetitions must be a gapless prefix of the two fixed slots")
    if any(item.candidateIdentity != manifest.candidateIdentity for item in manifest.repetitions):
        raise ValueError("every repetition result must bind the manifest candidate")
    expected = (
        manifest.attempt.registryDigest,
        manifest.attempt.certifyingPlanDigest,
        manifest.attempt.gatePlanDigest,
        manifest.attempt.scenarioVersion,
        manifest.attempt.planVersion,
        manifest.attempt.gate,
        manifest.attempt.profileId,
    )
    for result in manifest.repetitions:
        observed = (
            result.registryDigest,
            result.certifyingPlanDigest,
            result.gatePlanDigest,
            result.scenarioVersion,
            result.planVersion,
            result.gate,
            result.profileId,
        )
        if observed != expected:
            raise ValueError("a repetition result must bind the exact attempt plan identity")
    expected_keys = tuple(item.key for item in manifest.attempt.repetitionIdentities)
    observed_keys = tuple(item.repetitionIdentity.key for item in manifest.repetitions)
    if observed_keys != expected_keys[: len(observed_keys)]:
        raise ValueError(
            "repetition results must bind the reserved fresh identities in exact slot order"
        )
    if len(manifest.repetitions) == REPETITION_COUNT:
        authority = {
            (item.runtimeAuthority.snapshotDigest, item.runtimeAuthority.bindingDigest)
            for item in manifest.repetitions
        }
        if len(authority) != 1:
            raise ValueError(
                "both certifying repetitions must share one exact frozen runtime authority"
            )
        environments = {item.environment.environmentDigest for item in manifest.repetitions}
        if len(environments) != 1:
            raise ValueError("both certifying repetitions must share one exact environment")


__all__ = [
    "CERTIFYING_GATE",
    "REPETITION_COUNT",
    "FinalCodexArtifact",
    "FinalCodexAttemptRecord",
    "FinalCodexAttemptState",
    "FinalCodexDisposition",
    "FinalCodexEnvironmentBinding",
    "FinalCodexFailureClass",
    "FinalCodexFailureRecord",
    "FinalCodexPlanRecord",
    "FinalCodexRepetitionIdentity",
    "FinalCodexRepetitionResult",
    "FinalCodexRepetitionResultDraft",
    "FinalCodexRunManifest",
    "FinalCodexRuntimeAuthorityBinding",
    "FinalCodexTeardownRecord",
]
