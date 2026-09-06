"""CCR-R13 run controller that consumes the exact trusted R12 host authority.

The diagnostic lane is an optional one-replication real-Codex E2E that may run
only after the exact candidate's R12 Gates 1-3 are green.  This module owns the
run control: it composes the durable diagnostic manifest store and plan
projection (agents_remember.certification.diagnostics) with the R12
trusted launcher so every Dagger-backed diagnostic execution freezes one
existing connection-only runner/store snapshot and registers itself as a live
owner through the exact same authority closeout and integration consume.

There is deliberately no engine selection, private provisioning, profile
override, or runner retirement here: admission, snapshot freezing, owner
registration, transition barriers, and exact-owner release are all delegated
to the R12 authority functions, and this module never deletes the reusable
layer store or retires a runner still owned by another operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Never, Protocol

from agents_remember.certification.diagnostics.models import (
    DiagnosticAttemptRecord,
    DiagnosticEnvironmentBinding,
    DiagnosticFailureRecord,
    DiagnosticPlanRecord,
    DiagnosticRunResult,
    DiagnosticRunResultDraft,
    DiagnosticRuntimeAuthorityBinding,
    DiagnosticTeardownRecord,
)
from agents_remember.certification.diagnostics.planning import compile_diagnostic_plan
from agents_remember.certification.diagnostics.store import DiagnosticManifestStore
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CandidateIdentity,
    CanonicalRailRegistry,
    CertificationContractFinding,
    CertificationPlan,
    GatePlan,
    GateResultAdmission,
    GateResultManifest,
    RailEvidenceReference,
    RailTerminalObservation,
)
from agents_remember.certification.results import (
    build_rail_result,
    compile_gate_result_manifest,
)
from agents_remember.certification.telemetry.adapters import (
    TelemetryExecutionContext,
    compile_diagnostic_started,
    compile_diagnostic_terminal,
)
from agents_remember.certification.telemetry.models import TelemetryEvent
from agents_remember.errors import (
    AgentsRememberError,
    CertificationContractError,
    DaggerRuntimeAuthorityError,
)
from agents_remember.models.certification.base import GateId
from agents_remember.worktrees.modules.quality.dagger_authority import (
    AdmittedDaggerAuthority,
    AuthorityRegistry,
    DaggerAuthoritySnapshot,
    DaggerHostDeclaration,
    DaggerOwner,
    EngineInspectionProtocol,
    admit_dagger_authority,
    authority_environment,
    current_process_fingerprint,
    owner_identity,
    release_dagger_authority,
    reuse_authority_for_continuation,
)

DiagnosticGate = Literal[4]
SCENARIO_GATE: DiagnosticGate = 4
OPERATION_KIND = "diagnostic"
_DIGEST = r"^[0-9a-f]{64}$"


class DiagnosticHardFailure(AgentsRememberError):
    """A typed infrastructure or parser failure with bounded evidence.

    Infrastructure and parser failures remain typed hard diagnostic failures:
    they produce a hard-failure result with teardown evidence and never a
    pass, and they still release only the attempt's own runtime owner.
    """

    def __init__(
        self,
        detail: str,
        *,
        failure: DiagnosticFailureRecord,
        teardown_evidence: Sequence[RailEvidenceReference] = (),
    ) -> None:
        self.failure = failure
        self.teardown_evidence = tuple(teardown_evidence)
        super().__init__(detail)


@dataclass(frozen=True)
class ScenarioReplicationEvidence:
    """One complete scenario replication: full catalog plus teardown facts."""

    checkpointObservations: tuple[RailTerminalObservation, ...]
    teardownEvidence: tuple[RailEvidenceReference, ...] = ()


class ScenarioReplicationRunner(Protocol):
    """Executes exactly one replication of the canonical scenario catalog."""

    def run_once(
        self,
        *,
        gate_plan: GatePlan,
        environment: Mapping[str, str],
        snapshot: DaggerAuthoritySnapshot,
    ) -> ScenarioReplicationEvidence:
        """Run the canonical rails once and return the complete observations."""
        raise NotImplementedError


@dataclass(frozen=True)
class DiagnosticRunSpec:
    """The exact compiled plan identity one diagnostic attempt replicates."""

    candidateIdentity: CandidateIdentity
    registry: CanonicalRailRegistry
    certifyingPlan: CertificationPlan
    diagnosticPlan: CertificationPlan
    gatePlan: GatePlan
    planRecord: DiagnosticPlanRecord
    environmentIdentity: str = "codex-host"
    profileId: str = ""
    gate: GateId = SCENARIO_GATE
    planVersion: str = "1.0.0"

    @property
    def railCount(self) -> int:
        return len(self.gatePlan.rails)


@dataclass(frozen=True)
class DiagnosticEngineOptions:
    """Injectable engine dependencies; defaults are the production ones."""

    environ: Mapping[str, str] | None = None
    inspector: EngineInspectionProtocol | None = None
    clock: Callable[[], str] | None = None
    nonce_source: Callable[[], str] | None = None
    on_telemetry: Callable[[TelemetryEvent], None] | None = None


@dataclass(frozen=True)
class DiagnosticAttempt:
    """One live diagnostic attempt bound to its frozen authority snapshot."""

    spec: DiagnosticRunSpec
    record: DiagnosticAttemptRecord
    admitted: AdmittedDaggerAuthority

    @property
    def snapshot(self) -> DaggerAuthoritySnapshot:
        return self.admitted.snapshot


class DiagnosticExecutionEngine:
    """Run control for at most one replication per diagnostic attempt."""

    def __init__(
        self,
        *,
        store: DiagnosticManifestStore,
        registry: AuthorityRegistry,
        options: DiagnosticEngineOptions | None = None,
    ) -> None:
        resolved = options or DiagnosticEngineOptions()
        self._store = store
        self._registry = registry
        self._environ = dict(resolved.environ) if resolved.environ is not None else dict(os.environ)
        self._inspector = resolved.inspector
        self._clock = resolved.clock or _utc_now
        self._nonce_source = resolved.nonce_source or _entropy
        self._on_telemetry = resolved.on_telemetry

    # ------------------------------------------------------------------ public

    def admit(
        self,
        spec: DiagnosticRunSpec,
        gateGreenManifests: Sequence[GateResultManifest],
        *,
        continuation_snapshot: DaggerAuthoritySnapshot | None = None,
    ) -> DiagnosticAttempt:
        """Freeze the R12 authority and reserve one diagnostic attempt.

        Refuses before any scenario step when the exact candidate's Gates 1-3
        are not all green, when an authority-transition barrier is live, when
        the host declaration is missing/malformed/provisioning-capable, when
        ambient or profile selection conflicts, or when another diagnostic
        attempt is already live for the candidate.
        """

        _require_gates_one_to_three_green(spec.candidateIdentity, gateGreenManifests)
        attempt_number = self._store.next_attempt_number(spec.candidateIdentity)
        nonce = self._nonce_for(spec, attempt_number)
        record = self._attempt_record(spec, attempt_number, nonce, state="reserved")
        admitted = self._admit_authority(record, continuation_snapshot)
        try:
            self._store.reserve(record)
            running = self._store.mark_running(record)
        except CertificationContractError:
            release_dagger_authority(admitted, registry=self._registry)
            raise
        attempt = DiagnosticAttempt(spec=spec, record=running, admitted=admitted)
        self._emit_started(attempt)
        return attempt

    def run(
        self,
        attempt: DiagnosticAttempt,
        runner: ScenarioReplicationRunner,
    ) -> DiagnosticRunResult:
        """Execute at most one replication and terminalize the exact owner.

        A runner raising DiagnosticHardFailure terminalizes as a
        typed hard diagnostic failure.  A completed catalog terminalizes pass or fail
        from its complete diagnostic-altitude manifest.  Retrying after a
        failure creates a new diagnostic result only; it never promotes.
        """

        self._require_live_attempt(attempt)
        try:
            evidence = runner.run_once(
                gate_plan=attempt.spec.gatePlan,
                environment=attempt.admitted.environment,
                snapshot=attempt.snapshot,
            )
        except DiagnosticHardFailure as error:
            return self._terminalize_hard_failure(attempt, error)
        return self._terminalize_catalog(attempt, evidence)

    def abort(
        self,
        attempt: DiagnosticAttempt,
        *,
        teardownEvidence: Sequence[RailEvidenceReference],
    ) -> DiagnosticRunResult:
        """Terminalize an interrupted attempt: teardown evidence, no pass."""

        self._require_live_attempt(attempt)
        teardown = DiagnosticTeardownRecord(
            releasedOwner=True,
            evidence=tuple(teardownEvidence),
            at=self._clock(),
        )
        draft = self._draft(attempt, disposition="aborted", teardown=teardown)
        return self._publish(attempt, draft)

    # ------------------------------------------------------------------ admit helpers

    def _admit_authority(
        self,
        record: DiagnosticAttemptRecord,
        continuation_snapshot: DaggerAuthoritySnapshot | None,
    ) -> AdmittedDaggerAuthority:
        candidate = record.candidateIdentity
        scope = f"{candidate.kind}:{candidate.value}"
        generation = f"attempt:{record.attemptNumber}:{record.diagnosticNonce}"

        def owner_factory(snapshot: DaggerAuthoritySnapshot) -> DaggerOwner:
            return DaggerOwner(
                owner_id=owner_identity(
                    operation_kind=OPERATION_KIND,
                    scope=scope,
                    generation=generation,
                    authority_digest=snapshot.snapshot_digest,
                ),
                authority_digest=snapshot.snapshot_digest,
                operation_kind=OPERATION_KIND,
                scope=scope,
                generation=generation,
                pid=os.getpid(),
                process_fingerprint=current_process_fingerprint(),
                started_at=self._clock(),
            )

        if continuation_snapshot is not None:
            owner = owner_factory(continuation_snapshot)
            reuse_authority_for_continuation(
                continuation_snapshot,
                registry=self._registry,
                owner=owner,
            )
            declaration = DaggerHostDeclaration(
                schema_version=continuation_snapshot.schema_version,  # type: ignore[arg-type]
                endpoint=continuation_snapshot.endpoint,
                layer_store=continuation_snapshot.layer_store,
                engine_version=continuation_snapshot.engine_version,
                source=f"snapshot:{continuation_snapshot.snapshot_digest}",
            )
            environment = authority_environment(
                continuation_snapshot,
                base=dict(self._environ),
            )
            return AdmittedDaggerAuthority(
                snapshot=continuation_snapshot,
                declaration=declaration,
                environment=environment,
                owner=owner,
            )
        try:
            return admit_dagger_authority(
                environ=self._environ,
                inspector=self._inspector,
                registry=self._registry,
                owner_factory=owner_factory,
            )
        except DaggerRuntimeAuthorityError as error:
            raise DiagnosticAdmissionRefused(error) from error

    def _require_live_attempt(self, attempt: DiagnosticAttempt) -> None:
        """Refuse a second replication or abort on an already-terminal attempt."""
        live = self._store.live_attempt(attempt.record.candidateIdentity)
        if (
            live is None
            or live.attemptNumber != attempt.record.attemptNumber
            or live.diagnosticNonce != attempt.record.diagnosticNonce
        ):
            _raise_diagnostic(
                "diagnostic execution refused",
                "diagnostic-attempt-not-running",
                f"attempts.{attempt.record.attemptNumber}",
                "an attempt may execute at most one replication before terminalization",
            )

    # ------------------------------------------------------------------ terminal helpers

    def _terminalize_catalog(
        self,
        attempt: DiagnosticAttempt,
        evidence: ScenarioReplicationEvidence,
    ) -> DiagnosticRunResult:
        spec = attempt.spec
        results = tuple(
            build_rail_result(spec.gatePlan, observation)
            for observation in evidence.checkpointObservations
        )
        manifest = compile_gate_result_manifest(
            spec.registry,
            spec.diagnosticPlan,
            spec.gatePlan,
            results,
            GateResultAdmission(
                profileId=spec.planRecord.profileId,
                candidateIdentity=spec.candidateIdentity,
                altitude="diagnostic",
            ),
        )
        disposition = manifest.disposition
        failure = None
        if disposition == "red":
            # A red disposition is recomputed by the GateResultManifest validator
            # from this exact rail catalog, so an enforcing failed/blocked rail
            # necessarily exists here; selecting it directly keeps that invariant
            # instead of guarding an unreachable branch.
            failed = next(
                result
                for result in manifest.railResults
                if result.posture == "enforcing" and result.status in {"fail", "blocked"}
            )
            failure = DiagnosticFailureRecord(
                failureClass="scenario",
                code=failed.code,
                detail="scenario checkpoint failed",
                rail=failed.rail,
                correctiveOwner=failed.correctiveOwner,
                evidence=tuple(failed.evidence),
            )
        teardown = DiagnosticTeardownRecord(
            releasedOwner=True,
            evidence=evidence.teardownEvidence,
            at=self._clock(),
        )
        draft = self._draft(
            attempt,
            disposition="pass" if disposition == "green" else "fail",
            teardown=teardown,
            manifest=manifest,
            failure=failure,
        )
        return self._publish(attempt, draft)

    def _terminalize_hard_failure(
        self,
        attempt: DiagnosticAttempt,
        error: DiagnosticHardFailure,
    ) -> DiagnosticRunResult:
        failure = error.failure
        teardown = DiagnosticTeardownRecord(
            releasedOwner=True,
            evidence=error.teardown_evidence,
            at=self._clock(),
        )
        draft = self._draft(
            attempt,
            disposition="hard-failure",
            teardown=teardown,
            failure=failure,
        )
        return self._publish(attempt, draft)

    def _publish(
        self,
        attempt: DiagnosticAttempt,
        draft: DiagnosticRunResultDraft,
    ) -> DiagnosticRunResult:
        result = self._store.publish_terminal(attempt.record, draft)
        release_dagger_authority(attempt.admitted, registry=self._registry)
        self._emit_terminal(attempt, result)
        return result

    # ------------------------------------------------------------------ record + draft helpers

    def _attempt_record(
        self,
        spec: DiagnosticRunSpec,
        attempt_number: int,
        nonce: str,
        *,
        state: Literal["reserved", "running", "terminal"],
    ) -> DiagnosticAttemptRecord:
        payload = {
            "schemaVersion": "diagnostic-attempt/v1",
            "candidateIdentity": spec.candidateIdentity.model_dump(mode="json"),
            "attemptNumber": attempt_number,
            "diagnosticNonce": nonce,
            "registryDigest": spec.registry.registryDigest,
            "certifyingPlanDigest": spec.certifyingPlan.planDigest,
            "diagnosticPlanDigest": spec.diagnosticPlan.planDigest,
            "planVersion": spec.planRecord.planVersion,
            "gate": spec.gate,
            "profileId": spec.planRecord.profileId,
            "requestedAt": self._clock(),
            "state": state,
        }
        return DiagnosticAttemptRecord(**payload, attemptDigest=content_digest(payload))

    def _draft(
        self,
        attempt: DiagnosticAttempt,
        *,
        disposition: Literal["pass", "fail", "aborted", "hard-failure"],
        teardown: DiagnosticTeardownRecord,
        manifest: GateResultManifest | None = None,
        failure: DiagnosticFailureRecord | None = None,
    ) -> DiagnosticRunResultDraft:
        spec = attempt.spec
        record = attempt.record
        snapshot = attempt.snapshot
        environment = DiagnosticEnvironmentBinding(
            environmentIdentity=spec.environmentIdentity,
            environmentDigest=content_digest({"environmentIdentity": spec.environmentIdentity}),
        )
        runtime = DiagnosticRuntimeAuthorityBinding(
            snapshotDigest=snapshot.snapshot_digest,
            endpoint=snapshot.endpoint,
            engineId=snapshot.engine_id,
            layerStore=snapshot.layer_store,
            storeMountedPath=snapshot.store_mounted_path,
            observedVersion=snapshot.observed_version,
            bindingDigest=content_digest(
                {
                    "schemaVersion": "diagnostic-runtime-authority/v1",
                    "snapshotDigest": snapshot.snapshot_digest,
                    "endpoint": snapshot.endpoint,
                    "engineId": snapshot.engine_id,
                    "layerStore": snapshot.layer_store,
                    "storeMountedPath": snapshot.store_mounted_path,
                    "observedVersion": snapshot.observed_version,
                }
            ),
        )
        payload = {
            "schemaVersion": "diagnostic-run-result/v1",
            "attemptNumber": record.attemptNumber,
            "candidateIdentity": record.candidateIdentity.model_dump(mode="json"),
            "registryDigest": record.registryDigest,
            "certifyingPlanDigest": record.certifyingPlanDigest,
            "diagnosticPlanDigest": record.diagnosticPlanDigest,
            "planVersion": record.planVersion,
            "gate": record.gate,
            "profileId": record.profileId,
            "diagnosticNonce": record.diagnosticNonce,
            "acceptanceEligible": False,
            "certifying": False,
            "disposition": disposition,
            "failure": failure.model_dump(mode="json") if failure is not None else None,
            "environment": environment.model_dump(mode="json"),
            "runtimeAuthority": runtime.model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json") if manifest is not None else None,
            "teardown": teardown.model_dump(mode="json"),
            "artifacts": [],
            "startedAt": record.requestedAt,
            "terminalAt": self._clock(),
        }
        return DiagnosticRunResultDraft(**payload)

    # ------------------------------------------------------------------ telemetry + nonce helpers

    def _nonce_for(self, spec: DiagnosticRunSpec, attempt_number: int) -> str:
        seed = {
            "candidateIdentity": spec.candidateIdentity.model_dump(mode="json"),
            "diagnosticPlanDigest": spec.diagnosticPlan.planDigest,
            "attemptNumber": attempt_number,
            "entropy": self._nonce_source(),
        }
        encoded = json.dumps(
            seed,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _emit_started(self, attempt: DiagnosticAttempt) -> None:
        if self._on_telemetry is None:
            return
        ctx = _telemetry_context(attempt, revision=1, now=self._clock())
        event = compile_diagnostic_started(
            ctx,
            plan_digest=attempt.spec.diagnosticPlan.planDigest,
            plan_version=attempt.spec.planRecord.planVersion,
            rail_count=attempt.spec.railCount,
        )
        self._on_telemetry(event)

    def _emit_terminal(
        self,
        attempt: DiagnosticAttempt,
        result: DiagnosticRunResult,
    ) -> None:
        if self._on_telemetry is None:
            return
        disposition = "pass" if result.disposition == "pass" else "fail"
        if result.disposition == "aborted":
            disposition = "aborted"
        ctx = _telemetry_context(attempt, revision=2, now=self._clock())
        event = compile_diagnostic_terminal(
            ctx,
            result_id=result.resultId,
            disposition=disposition,  # type: ignore[arg-type]
        )
        self._on_telemetry(event)


@dataclass(frozen=True)
class DiagnosticRunOptions:
    """Scenario options frozen into the compiled diagnostic plan record."""

    environment_identity: str = "codex-host"
    plan_version: str = "1.0.0"
    gate: GateId = SCENARIO_GATE


def build_diagnostic_run_spec(
    registry: CanonicalRailRegistry,
    *,
    profile_id: str,
    certifying_plan: CertificationPlan,
    candidate_identity: CandidateIdentity,
    options: DiagnosticRunOptions | None = None,
) -> DiagnosticRunSpec:
    """Compile the exact diagnostic plan record for one scenario gate."""

    resolved = options or DiagnosticRunOptions()
    diagnostic_plan = compile_diagnostic_plan(
        registry,
        profile_id=profile_id,
        candidate_identity=candidate_identity,
        certifying_plan=certifying_plan,
        gate=resolved.gate,
    )
    gate_plan = next(item for item in diagnostic_plan.gates if item.gate == resolved.gate)
    payload = {
        "schemaVersion": "diagnostic-plan-record/v1",
        "candidateIdentity": candidate_identity.model_dump(mode="json"),
        "registryDigest": registry.registryDigest,
        "certifyingPlanDigest": certifying_plan.planDigest,
        "diagnosticPlanDigest": diagnostic_plan.planDigest,
        "profileId": profile_id,
        "gate": resolved.gate,
        "gatePlanDigest": gate_plan.planDigest,
        "planVersion": resolved.plan_version,
    }
    plan_record = DiagnosticPlanRecord(**payload, planDigest=content_digest(payload))
    return DiagnosticRunSpec(
        candidateIdentity=candidate_identity,
        registry=registry,
        certifyingPlan=certifying_plan,
        diagnosticPlan=diagnostic_plan,
        gatePlan=gate_plan,
        planRecord=plan_record,
        environmentIdentity=resolved.environment_identity,
        profileId=profile_id,
        gate=resolved.gate,
        planVersion=resolved.plan_version,
    )


def require_gates_one_to_three_green(
    candidate: CandidateIdentity,
    gateManifests: Sequence[GateResultManifest],
) -> None:
    """Require the exact R12 Gates 1-3 to be green for the exact candidate."""

    _require_gates_one_to_three_green(candidate, gateManifests)


def _require_gates_one_to_three_green(
    candidate: CandidateIdentity,
    gateManifests: Sequence[GateResultManifest],
) -> None:
    ordered = tuple(gateManifests)
    if len(ordered) != 3 or tuple(item.gate for item in ordered) != (1, 2, 3):
        _raise_diagnostic(
            "diagnostic prerequisites refused",
            "diagnostic-prerequisite-incomplete",
            "gateManifests",
            "diagnostics require the exact complete certifying Gate 1-3 manifests",
        )
    for manifest in ordered:
        if manifest.candidateIdentity != candidate:
            _raise_diagnostic(
                "diagnostic prerequisites refused",
                "diagnostic-prerequisite-candidate-mismatch",
                f"gates.{manifest.gate}",
                "every green gate manifest must bind the exact candidate",
            )
        if manifest.profileKind != "certifying" or manifest.altitude != "certifying":
            _raise_diagnostic(
                "diagnostic prerequisites refused",
                "diagnostic-prerequisite-not-certifying",
                f"gates.{manifest.gate}",
                "diagnostic prerequisites must come from the certifying altitude",
            )
        if manifest.disposition != "green":
            _raise_diagnostic(
                "diagnostic prerequisites refused",
                "diagnostic-prerequisite-not-green",
                f"gates.{manifest.gate}",
                "a diagnostic may run only after the exact candidate's R12 Gates 1-3 are green",
            )


class DiagnosticAdmissionRefused(CertificationContractError):
    """The R12 trusted launcher refused diagnostic authority admission."""

    def __init__(self, cause: DaggerRuntimeAuthorityError) -> None:
        self.cause = cause
        super().__init__(str(cause), cause.findings)


def _telemetry_context(
    attempt: DiagnosticAttempt,
    *,
    revision: int,
    now: str,
) -> TelemetryExecutionContext:
    return TelemetryExecutionContext(
        executionKind="diagnostic-run",
        executionId=attempt.record.diagnosticNonce,
        eventRevision=revision,
        diagnosticNonce=attempt.record.diagnosticNonce,
        candidate=attempt.record.candidateIdentity,
        profileId=attempt.record.profileId,
        occurredAt=now,
    )


def _raise_diagnostic(detail: str, code: str, path: str, message: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=message)
    raise CertificationContractError(
        f"{detail}: {message}",
        (finding.model_dump(mode="json"),),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _entropy() -> str:
    return secrets.token_hex(32)


__all__ = [
    "SCENARIO_GATE",
    "DiagnosticAdmissionRefused",
    "DiagnosticAttempt",
    "DiagnosticEngineOptions",
    "DiagnosticExecutionEngine",
    "DiagnosticHardFailure",
    "DiagnosticRunOptions",
    "DiagnosticRunSpec",
    "ScenarioReplicationEvidence",
    "ScenarioReplicationRunner",
    "build_diagnostic_run_spec",
    "require_gates_one_to_three_green",
]
