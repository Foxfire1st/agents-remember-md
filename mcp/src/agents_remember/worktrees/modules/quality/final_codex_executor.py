"""CCR-R14 run controller that consumes the exact trusted R12 host authority.

The final real-codex lane is the certifying Gate-4 proof: after the exact
candidate's R12 Gates 1-3 are green, it runs exactly two fresh independent
certifying repetitions of the canonical scenario rails with retry disabled,
fresh client/process state per repetition, exact accepted scenario version,
bounded evidence, and an explicit result for each.  One passing repetition can
never compensate for the other.  This module owns the run control: it composes
the durable final-codex manifest store and projection
(agents_remember.certification.final_codex) with the R12 trusted launcher so
every Dagger-backed certifying repetition freezes one existing
connection-only runner/store snapshot and registers itself as a live owner
through the exact same authority closeout and integration consume.

There is deliberately no engine selection, private provisioning, profile
override, or runner retirement here: admission, snapshot freezing, owner
registration, transition barriers, and exact-owner release are all delegated
to the R12 authority functions, and this module never deletes the reusable
layer store or retires a runner still owned by another operation.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Never, Protocol

from agents_remember.certification.digests import content_digest
from agents_remember.certification.final_codex.models import (
    CERTIFYING_GATE,
    FinalCodexAttemptRecord,
    FinalCodexEnvironmentBinding,
    FinalCodexFailureRecord,
    FinalCodexPlanRecord,
    FinalCodexRepetitionIdentity,
    FinalCodexRepetitionResult,
    FinalCodexRepetitionResultDraft,
    FinalCodexRunManifest,
    FinalCodexRuntimeAuthorityBinding,
    FinalCodexTeardownRecord,
)
from agents_remember.certification.final_codex.planning import (
    compile_final_codex_plan_record,
    final_codex_gate_plan,
    require_gates_one_to_three_green,
)
from agents_remember.certification.final_codex.store import FinalCodexManifestStore
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
from agents_remember.errors import (
    AgentsRememberError,
    CertificationContractError,
    DaggerRuntimeAuthorityError,
)
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

OPERATION_KIND = "final-codex"
REPETITION_COUNT = 2


class FinalCodexHardFailure(AgentsRememberError):
    """A typed infrastructure or parser failure with bounded evidence.

    Infrastructure and parser failures remain typed hard certifying failures:
    they produce a hard-failure repetition result with teardown evidence and
    never a pass, they make the whole Gate-4 run red, and they still release
    only the run's own runtime owner.
    """

    def __init__(
        self,
        detail: str,
        *,
        failure: FinalCodexFailureRecord,
        teardown_evidence: Sequence[RailEvidenceReference] = (),
        process_clean: bool = False,
    ) -> None:
        self.failure = failure
        self.teardown_evidence = tuple(teardown_evidence)
        self.process_clean = process_clean
        super().__init__(detail)


@dataclass(frozen=True)
class FinalCodexScenarioEvidence:
    """One complete fresh scenario repetition: full catalog plus teardown facts."""

    checkpointObservations: tuple[RailTerminalObservation, ...]
    teardownEvidence: tuple[RailEvidenceReference, ...] = ()
    processClean: bool = True


class FinalCodexScenarioRunner(Protocol):
    """Executes exactly one fresh certifying repetition of the canonical rails."""

    def run_once(
        self,
        *,
        gate_plan: GatePlan,
        environment: Mapping[str, str],
        snapshot: DaggerAuthoritySnapshot,
        repetitionIdentity: FinalCodexRepetitionIdentity,
    ) -> FinalCodexScenarioEvidence:
        """Run the canonical rails once with fresh state and return observations."""
        raise NotImplementedError


@dataclass(frozen=True)
class FinalCodexRunSpec:
    """The exact compiled plan identity one two-repetition run certifies."""

    candidateIdentity: CandidateIdentity
    registry: CanonicalRailRegistry
    certifyingPlan: CertificationPlan
    gatePlan: GatePlan
    planRecord: FinalCodexPlanRecord
    environmentIdentity: str = "codex-host"
    profileId: str = ""
    gate: Literal[4] = CERTIFYING_GATE
    planVersion: str = "1.0.0"

    @property
    def railCount(self) -> int:
        return len(self.gatePlan.rails)


@dataclass(frozen=True)
class FinalCodexEngineOptions:
    """Injectable engine dependencies; defaults are the production ones."""

    environ: Mapping[str, str] | None = None
    inspector: EngineInspectionProtocol | None = None
    clock: Callable[[], str] | None = None
    nonce_source: Callable[[], str] | None = None


@dataclass(frozen=True)
class FinalCodexAttempt:
    """One live two-repetition run bound to its frozen authority snapshot."""

    spec: FinalCodexRunSpec
    record: FinalCodexAttemptRecord
    admitted: AdmittedDaggerAuthority

    @property
    def snapshot(self) -> DaggerAuthoritySnapshot:
        return self.admitted.snapshot


class FinalCodexExecutionEngine:
    """Run control for exactly two fresh no-retry certifying repetitions."""

    def __init__(
        self,
        *,
        store: FinalCodexManifestStore,
        registry: AuthorityRegistry,
        options: FinalCodexEngineOptions | None = None,
    ) -> None:
        resolved = options or FinalCodexEngineOptions()
        self._store = store
        self._registry = registry
        self._environ = dict(resolved.environ) if resolved.environ is not None else dict(os.environ)
        self._inspector = resolved.inspector
        self._clock = resolved.clock or _utc_now
        self._nonce_source = resolved.nonce_source or _entropy

    # ------------------------------------------------------------------ public

    def admit(
        self,
        spec: FinalCodexRunSpec,
        gateGreenManifests: Sequence[GateResultManifest],
        *,
        continuation_snapshot: DaggerAuthoritySnapshot | None = None,
    ) -> FinalCodexAttempt:
        """Freeze the R12 authority and reserve the two-repetition run.

        Refuses before any scenario step when the exact candidate's Gates 1-3
        are not all green against the exact frozen plan, when an
        authority-transition barrier is live, when the host declaration is
        missing/malformed/provisioning-capable, when ambient or profile
        selection conflicts, when another run is already live for the
        candidate, or when a terminal run already certifies the exact same
        plan identity (retry disabled).
        """

        require_gates_one_to_three_green(
            spec.candidateIdentity,
            gateGreenManifests,
            certifying_plan=spec.certifyingPlan,
        )
        attempt_number = self._store.attempt_number(spec.candidateIdentity)
        identities = tuple(
            self._identity_for(spec, attempt_number, repetition_number)
            for repetition_number in (1, 2)
        )
        record = self._attempt_record(spec, attempt_number, identities, state="reserved")
        admitted = self._admit_authority(record, continuation_snapshot)
        try:
            self._store.reserve(record)
            running = self._store.mark_running(record)
        except CertificationContractError:
            release_dagger_authority(admitted, registry=self._registry)
            raise
        return FinalCodexAttempt(spec=spec, record=running, admitted=admitted)

    def run(
        self,
        attempt: FinalCodexAttempt,
        runner: FinalCodexScenarioRunner,
    ) -> FinalCodexRunManifest:
        """Execute both fresh certifying repetitions and terminalize the run.

        Each repetition runs exactly once with fresh state through the supplied
        runner; retry is never consulted.  A runner raising FinalCodexHardFailure
        terminalizes the interrupted slot as a typed hard failure and aborts
        the remaining slot with teardown evidence; the run never fabricates a
        pass.  The run terminalizes only after both repetition slots publish,
        and the aggregate stays red unless both repetitions passed.
        """

        for index in (1, 2):
            self._require_live_attempt(attempt)
            self._run_repetition(attempt, runner=runner, repetition_number=index)
        manifest = self._store.manifest(attempt.record.candidateIdentity)
        assert manifest is not None
        release_dagger_authority(attempt.admitted, registry=self._registry)
        return manifest

    def abort(
        self,
        attempt: FinalCodexAttempt,
        *,
        teardownEvidence: Sequence[RailEvidenceReference],
        processClean: bool = False,
    ) -> FinalCodexRunManifest:
        """Terminalize an interrupted run: teardown evidence, never a pass.

        Publishes an aborted result for the next unpublished slot and releases
        only the run's exact owner.  A slot that already has a terminal result
        is never rewritten.
        """

        self._require_live_attempt(attempt)
        manifest = self._store.manifest(attempt.record.candidateIdentity)
        assert manifest is not None
        published = {item.repetitionNumber for item in manifest.repetitions}
        for slot in (1, 2):
            if slot in published:
                continue
            draft = self._aborted_draft(
                attempt,
                repetition_number=slot,
                teardown=teardownEvidence,
                process_clean=processClean,
            )
            self._store.publish_repetition(attempt.record, draft)
        final = self._store.manifest(attempt.record.candidateIdentity)
        release_dagger_authority(attempt.admitted, registry=self._registry)
        assert final is not None
        return final

    # ------------------------------------------------------------------ admit helpers

    def _admit_authority(
        self,
        record: FinalCodexAttemptRecord,
        continuation_snapshot: DaggerAuthoritySnapshot | None,
    ) -> AdmittedDaggerAuthority:
        candidate = record.candidateIdentity
        scope = f"{candidate.kind}:{candidate.value}"
        first_identity = record.repetitionIdentities[0].clientIdentity[:16]
        generation = f"attempt:{record.attemptNumber}:{first_identity}"

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
            raise FinalCodexAdmissionRefused(error) from error

    def _require_live_attempt(self, attempt: FinalCodexAttempt) -> None:
        """Refuse a second run or abort on an already-terminal attempt."""
        live = self._store.live_attempt(attempt.record.candidateIdentity)
        if (
            live is None
            or live.attemptNumber != attempt.record.attemptNumber
            or live.attemptDigest != attempt.record.attemptDigest
        ):
            _raise_lane(
                "final-codex execution refused",
                "final-codex-attempt-not-running",
                f"attempts.{attempt.record.attemptNumber}",
                "an attempt may execute only its reserved two fresh repetitions",
            )

    # ------------------------------------------------------------------ repetition helpers

    def _run_repetition(
        self,
        attempt: FinalCodexAttempt,
        *,
        runner: FinalCodexScenarioRunner,
        repetition_number: int,
    ) -> FinalCodexRepetitionResult:
        identity = attempt.record.repetitionIdentities[repetition_number - 1]
        try:
            evidence = runner.run_once(
                gate_plan=attempt.spec.gatePlan,
                environment=attempt.admitted.environment,
                snapshot=attempt.snapshot,
                repetitionIdentity=identity,
            )
        except FinalCodexHardFailure as error:
            return self._publish_hard_failure(attempt, repetition_number, error)
        return self._publish_catalog(attempt, repetition_number, evidence)

    def _publish_catalog(
        self,
        attempt: FinalCodexAttempt,
        repetition_number: int,
        evidence: FinalCodexScenarioEvidence,
    ) -> FinalCodexRepetitionResult:
        spec = attempt.spec
        results = tuple(
            build_rail_result(spec.gatePlan, observation)
            for observation in evidence.checkpointObservations
        )
        manifest = compile_gate_result_manifest(
            spec.registry,
            spec.certifyingPlan,
            spec.gatePlan,
            results,
            GateResultAdmission(
                profileId=attempt.spec.planRecord.profileId,
                candidateIdentity=attempt.spec.candidateIdentity,
                altitude="certifying",
            ),
        )
        disposition = manifest.disposition
        failure = None
        if disposition == "red":
            failed = next(
                result
                for result in manifest.railResults
                if result.posture == "enforcing" and result.status in {"fail", "blocked"}
            )
            failure = FinalCodexFailureRecord(
                failureClass="scenario",
                code=failed.code,
                detail="scenario checkpoint failed",
                rail=failed.rail,
                correctiveOwner=failed.correctiveOwner,
                evidence=tuple(failed.evidence),
            )
        teardown = FinalCodexTeardownRecord(
            releasedOwner=True,
            processClean=evidence.processClean,
            evidence=evidence.teardownEvidence,
            at=self._clock(),
        )
        payload = self._draft_payload(
            attempt,
            repetition_number=repetition_number,
            disposition="pass" if disposition == "green" else "fail",
            teardown=teardown,
        )
        payload["manifest"] = manifest.model_dump(mode="json")
        if failure is not None:
            payload["failure"] = failure.model_dump(mode="json")
        return self._store.publish_repetition(
            attempt.record, FinalCodexRepetitionResultDraft(**payload)
        )

    def _publish_hard_failure(
        self,
        attempt: FinalCodexAttempt,
        repetition_number: int,
        error: FinalCodexHardFailure,
    ) -> FinalCodexRepetitionResult:
        teardown = FinalCodexTeardownRecord(
            releasedOwner=True,
            processClean=error.process_clean,
            evidence=error.teardown_evidence,
            at=self._clock(),
        )
        payload = self._draft_payload(
            attempt,
            repetition_number=repetition_number,
            disposition="hard-failure",
            teardown=teardown,
        )
        payload["failure"] = error.failure.model_dump(mode="json")
        return self._store.publish_repetition(
            attempt.record, FinalCodexRepetitionResultDraft(**payload)
        )

    def _aborted_draft(
        self,
        attempt: FinalCodexAttempt,
        *,
        repetition_number: int,
        teardown: Sequence[RailEvidenceReference],
        process_clean: bool,
    ) -> FinalCodexRepetitionResultDraft:
        record = FinalCodexTeardownRecord(
            releasedOwner=True,
            processClean=process_clean,
            evidence=tuple(teardown),
            at=self._clock(),
        )
        payload = self._draft_payload(
            attempt,
            repetition_number=repetition_number,
            disposition="aborted",
            teardown=record,
        )
        return FinalCodexRepetitionResultDraft(**payload)

    # ------------------------------------------------------------------ record + draft helpers

    def _identity_for(
        self,
        spec: FinalCodexRunSpec,
        attempt_number: int,
        repetition_number: int,
    ) -> FinalCodexRepetitionIdentity:
        seed = {
            "candidateIdentity": spec.candidateIdentity.model_dump(mode="json"),
            "certifyingPlanDigest": spec.certifyingPlan.planDigest,
            "attemptNumber": attempt_number,
            "repetitionNumber": repetition_number,
            "entropy": self._nonce_source(),
        }
        client = content_digest(seed)
        return FinalCodexRepetitionIdentity(
            clientIdentity=client,
            processFingerprint=f"{os.getpid()}:{client[:16]}",
            pid=os.getpid(),
        )

    def _attempt_record(
        self,
        spec: FinalCodexRunSpec,
        attempt_number: int,
        identities: tuple[FinalCodexRepetitionIdentity, ...],
        *,
        state: Literal["reserved", "running", "terminal"],
    ) -> FinalCodexAttemptRecord:
        payload = {
            "schemaVersion": "final-codex-attempt/v1",
            "candidateIdentity": spec.candidateIdentity.model_dump(mode="json"),
            "attemptNumber": attempt_number,
            "registryDigest": spec.registry.registryDigest,
            "certifyingPlanDigest": spec.certifyingPlan.planDigest,
            "gatePlanDigest": spec.gatePlan.planDigest,
            "scenarioVersion": spec.planRecord.scenarioVersion,
            "planVersion": spec.planRecord.planVersion,
            "gate": spec.gate,
            "profileId": spec.planRecord.profileId,
            "repetitionIdentities": [item.model_dump(mode="json") for item in identities],
            "requestedAt": self._clock(),
            "state": state,
        }
        return FinalCodexAttemptRecord(**payload, attemptDigest=content_digest(payload))

    def _draft_payload(
        self,
        attempt: FinalCodexAttempt,
        *,
        repetition_number: int,
        disposition: Literal["pass", "fail", "aborted", "hard-failure"],
        teardown: FinalCodexTeardownRecord,
    ) -> dict[str, Any]:
        spec = attempt.spec
        record = attempt.record
        snapshot = attempt.snapshot
        identity = record.repetitionIdentities[repetition_number - 1]
        environment = FinalCodexEnvironmentBinding(
            environmentIdentity=spec.environmentIdentity,
            environmentDigest=content_digest({"environmentIdentity": spec.environmentIdentity}),
        )
        runtime = FinalCodexRuntimeAuthorityBinding(
            snapshotDigest=snapshot.snapshot_digest,
            endpoint=snapshot.endpoint,
            engineId=snapshot.engine_id,
            layerStore=snapshot.layer_store,
            storeMountedPath=snapshot.store_mounted_path,
            observedVersion=snapshot.observed_version,
            bindingDigest=content_digest(
                {
                    "schemaVersion": "final-codex-runtime-authority/v1",
                    "snapshotDigest": snapshot.snapshot_digest,
                    "endpoint": snapshot.endpoint,
                    "engineId": snapshot.engine_id,
                    "layerStore": snapshot.layer_store,
                    "storeMountedPath": snapshot.store_mounted_path,
                    "observedVersion": snapshot.observed_version,
                }
            ),
        )
        return {
            "schemaVersion": "final-codex-repetition-result/v1",
            "repetitionNumber": repetition_number,
            "candidateIdentity": record.candidateIdentity.model_dump(mode="json"),
            "registryDigest": record.registryDigest,
            "certifyingPlanDigest": record.certifyingPlanDigest,
            "gatePlanDigest": record.gatePlanDigest,
            "scenarioVersion": record.scenarioVersion,
            "planVersion": record.planVersion,
            "gate": record.gate,
            "profileId": record.profileId,
            "acceptanceEligible": True,
            "certifying": True,
            "retryCount": 0,
            "repetitionIdentity": identity.model_dump(mode="json"),
            "disposition": disposition,
            "failure": None,
            "environment": environment.model_dump(mode="json"),
            "runtimeAuthority": runtime.model_dump(mode="json"),
            "manifest": None,
            "teardown": teardown.model_dump(mode="json"),
            "artifacts": [],
            "startedAt": record.requestedAt,
            "terminalAt": self._clock(),
        }


@dataclass(frozen=True)
class FinalCodexRunOptions:
    """Scenario options frozen into the compiled final-codex plan record."""

    environment_identity: str = "codex-host"
    scenario_version: str = "1.0.0"
    plan_version: str = "1.0.0"


def build_final_codex_run_spec(
    registry: CanonicalRailRegistry,
    *,
    certifying_plan: CertificationPlan,
    candidate_identity: CandidateIdentity,
    options: FinalCodexRunOptions | None = None,
) -> FinalCodexRunSpec:
    """Compile the exact two-repetition run spec for the Gate-4 scenario."""

    resolved = options or FinalCodexRunOptions()
    plan_record = compile_final_codex_plan_record(
        registry,
        certifying_plan=certifying_plan,
        candidate_identity=candidate_identity,
        scenario_version=resolved.scenario_version,
        plan_version=resolved.plan_version,
    )
    gate_plan = final_codex_gate_plan(plan_record, certifying_plan)
    return FinalCodexRunSpec(
        candidateIdentity=candidate_identity,
        registry=registry,
        certifyingPlan=certifying_plan,
        gatePlan=gate_plan,
        planRecord=plan_record,
        environmentIdentity=resolved.environment_identity,
        profileId=plan_record.profileId,
        gate=plan_record.gate,
        planVersion=plan_record.planVersion,
    )


class FinalCodexAdmissionRefused(CertificationContractError):
    """The R12 trusted launcher refused final-codex authority admission."""

    def __init__(self, cause: DaggerRuntimeAuthorityError) -> None:
        self.cause = cause
        super().__init__(str(cause), cause.findings)


def _raise_lane(detail: str, code: str, path: str, message: str) -> Never:
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
    "OPERATION_KIND",
    "FinalCodexAdmissionRefused",
    "FinalCodexAttempt",
    "FinalCodexEngineOptions",
    "FinalCodexExecutionEngine",
    "FinalCodexHardFailure",
    "FinalCodexRunOptions",
    "FinalCodexRunSpec",
    "FinalCodexScenarioEvidence",
    "FinalCodexScenarioRunner",
    "build_final_codex_run_spec",
]
