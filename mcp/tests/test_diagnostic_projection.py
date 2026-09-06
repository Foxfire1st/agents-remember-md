"""Standalone CCR-R13 optional-lane readiness projection tests.

The optional lane projects not-requested-optional before any request, the
newest terminal result after a request, and blocking facts that a requested
failure or abort can never erase.  Historical passes cannot override a newer
failure, no diagnostic pass can satisfy or promote into R14, and a plan change
stales the newest result until a newer result binds the current plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agents_remember.certification import canonicalize_registry
from agents_remember.certification.certificate_models import CandidateIdentity
from agents_remember.certification.diagnostics.models import (
    DiagnosticAttemptRecord,
    DiagnosticEnvironmentBinding,
    DiagnosticFailureRecord,
    DiagnosticPlanRecord,
    DiagnosticRunResultDraft,
    DiagnosticRuntimeAuthorityBinding,
    DiagnosticTeardownRecord,
)
from agents_remember.certification.diagnostics.planning import compile_diagnostic_plan
from agents_remember.certification.diagnostics.projection import (
    diagnostic_blocks_certification,
    diagnostic_never_satisfies_certification,
    project_diagnostic_lane,
)
from agents_remember.certification.diagnostics.store import (
    DiagnosticManifestStore,
    DiagnosticStorePolicy,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CanonicalRailRegistry,
    CertificationPlan,
    GateResultAdmission,
    GateResultManifest,
    RailAdapterDefinition,
    RailApplicability,
    RailArtifactResult,
    RailClass,
    RailDefinition,
    RailEvidenceContract,
    RailEvidenceReference,
    RailRegistry,
    RailRuntimeInputs,
    RailTerminalObservation,
    RegistryProfile,
)
from agents_remember.certification.planning import compile_certification_plan
from agents_remember.certification.results import (
    build_rail_result,
    compile_gate_result_manifest,
)
from agents_remember.models.certification.base import GateId, RailIdentity

CERT_PROFILE = "portable-ci"
DIAG_PROFILE = "diagnostic-ci"
DIGEST = "a" * 64
PREREQUISITES: dict[str, str] = {
    "suite": "lint",
    "coverage": "suite",
    "e2e": "coverage",
    "memory": "e2e",
}

CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)
OTHER_CANDIDATE = CandidateIdentity(kind="git-tree", value="d" * 40)
NONCE_ONE = "1" * 64
NONCE_TWO = "2" * 64
NONCE_THREE = "3" * 64
CLASS_BY_GATE: dict[GateId, RailClass] = {
    1: "pre-test-quality",
    2: "ordinary-test-suite",
    3: "post-test-quality",
    4: "integration-test",
    5: "memory-quality",
}


def _identity(name: str) -> RailIdentity:
    return RailIdentity(railId=name, version="1.0.0")


def _rail(name: str, gate: GateId) -> RailDefinition:
    rows = []
    for profile_id in (CERT_PROFILE, DIAG_PROFILE):
        if gate == 5 and profile_id == DIAG_PROFILE:
            continue
        applicable = gate != 5
        rows.append(
            RailApplicability(
                profileId=profile_id,
                status="applicable" if applicable else "not-applicable",
                selectionIdentity=f"selection:{name}",
                population="exact population" if applicable else None,
                reason=None if applicable else "profile excludes gate-5 memory",
            )
        )
    required = ("suite-data",) if gate == 3 else ()
    outputs = (
        (
            ArtifactDeclaration(
                artifactId="suite-data",
                schemaVersion="suite/v1",
                mediaType="application/json",
            ),
        )
        if gate == 2
        else ()
    )
    return RailDefinition(
        identity=_identity(name),
        gate=gate,
        railClass=CLASS_BY_GATE[gate],
        authority="memory-domain" if gate == 5 else "repository-profile",
        ownerClass="portable-owner",
        correctiveOwner="portable-owner",
        posture="enforcing",
        orderKey=name,
        prerequisites=tuple(_identity(item) for item in (PREREQUISITES.get(name, ""),) if item),
        requiredArtifacts=required,
        adapter=RailAdapterDefinition(
            adapterKind="process-adapter",
            adapterId=f"{name}-adapter",
            configurationDigest=DIGEST,
            executionEvidence=f"adapter://{name}",
        ),
        runtimeInputs=RailRuntimeInputs(runtimeIdentity="portable-runtime"),
        applicability=tuple(rows),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId=f"{name}-evidence",
                mediaType="application/json",
                maxBytes=256,
            ),
        ),
        outputArtifacts=outputs,
    )


def scenario_registry() -> CanonicalRailRegistry:
    rail_pairs: list[tuple[str, GateId]] = [
        ("lint", 1),
        ("suite", 2),
        ("coverage", 3),
        ("e2e", 4),
        ("memory", 5),
    ]
    return canonicalize_registry(
        RailRegistry(
            registryId="portable-diagnostics",
            repositoryId="sample-repository",
            profiles=(
                RegistryProfile(
                    profileId=CERT_PROFILE,
                    kind="certifying",
                    gates=(1, 2, 3, 4, 5),
                ),
                RegistryProfile(
                    profileId=DIAG_PROFILE,
                    kind="diagnostic",
                    gates=(1, 2, 3, 4),
                ),
            ),
            rails=tuple(_rail(name, gate) for name, gate in rail_pairs),
        )
    )


def certifying_plan(
    resolved: CanonicalRailRegistry,
    candidate: CandidateIdentity = CANDIDATE,
) -> CertificationPlan:
    return compile_certification_plan(
        resolved,
        profile_id=CERT_PROFILE,
        candidate_identity=candidate,
    )


def diagnostic_plan(
    resolved: CanonicalRailRegistry,
    candidate: CandidateIdentity = CANDIDATE,
) -> CertificationPlan:
    return compile_diagnostic_plan(
        resolved,
        profile_id=DIAG_PROFILE,
        candidate_identity=candidate,
        certifying_plan=certifying_plan(resolved, candidate),
        gate=4,
    )


def manifest_for(
    registry: CanonicalRailRegistry,
    plan: CertificationPlan,
    gate: GateId,
    *,
    red: bool = False,
) -> GateResultManifest:
    gate_plan = next(item for item in plan.gates if item.gate == gate)
    results = []
    for rail in gate_plan.rails:
        status = "not-applicable" if rail.applicability.status == "not-applicable" else "pass"
        if red and rail.posture == "enforcing":
            status = "fail"
        artifacts = tuple(
            RailArtifactResult(
                artifactId=item.artifactId,
                sha256=content_digest({"artifact": item.artifactId}),
                size=32,
                evidenceRef=f"artifact://{item.artifactId}",
            )
            for item in rail.outputArtifacts
            if status == "pass"
        )
        observation = RailTerminalObservation(
            rail=rail.identity,
            status=status,
            code=f"{rail.identity.railId}-{status}",
            artifacts=artifacts,
            evidence=tuple(
                RailEvidenceReference(
                    evidenceId=item.evidenceId,
                    sha256=content_digest({"evidence": item.evidenceId}),
                    size=32,
                    reference=f"evidence://{item.evidenceId}",
                )
                for item in rail.evidenceContract
            ),
        )
        results.append(build_rail_result(gate_plan, observation))
    return compile_gate_result_manifest(
        registry,
        plan,
        gate_plan,
        results,
        GateResultAdmission(
            profileId=plan.profileId,
            candidateIdentity=plan.candidateIdentity,
            altitude=plan.profileKind,
        ),
    )


@dataclass(frozen=True)
class LaneScenario:
    """One candidate lane binding a store, its digests, and scenario manifests."""

    store: DiagnosticManifestStore
    candidate: CandidateIdentity
    registryDigest: str
    planDigest: str
    certifyingPlanDigest: str
    green: GateResultManifest
    red: GateResultManifest


def make_lane(tmp_path: Path) -> DiagnosticManifestStore:
    return DiagnosticManifestStore(
        tmp_path / "diagnostics",
        DiagnosticStorePolicy(forbiddenRoots=(tmp_path / "quality-report",)),
    )


def scenario(
    store: DiagnosticManifestStore,
    candidate: CandidateIdentity = CANDIDATE,
) -> LaneScenario:
    resolved = scenario_registry()
    diag = diagnostic_plan(resolved, candidate)
    certifying = certifying_plan(resolved, candidate)
    return LaneScenario(
        store=store,
        candidate=candidate,
        registryDigest=resolved.registryDigest,
        planDigest=diag.planDigest,
        certifyingPlanDigest=certifying.planDigest,
        green=manifest_for(resolved, diag, 4),
        red=manifest_for(resolved, diag, 4, red=True),
    )


def attempt_digest(
    candidate: CandidateIdentity,
    number: int,
    nonce: str,
    ctx: LaneScenario,
) -> str:
    return content_digest(
        {
            "schemaVersion": "diagnostic-attempt/v1",
            "candidateIdentity": candidate.model_dump(mode="json"),
            "attemptNumber": number,
            "diagnosticNonce": nonce,
            "registryDigest": ctx.registryDigest,
            "certifyingPlanDigest": ctx.certifyingPlanDigest,
            "diagnosticPlanDigest": ctx.planDigest,
            "planVersion": "1.0.0",
            "gate": 4,
            "profileId": DIAG_PROFILE,
            "requestedAt": "2026-09-04T00:00:00Z",
            "state": "running",
        }
    )


def draft_payload(
    ctx: LaneScenario,
    disposition: Literal["pass", "fail", "aborted", "hard-failure"],
    number: int,
    nonce: str,
) -> dict[str, Any]:
    failure = None
    if disposition == "fail":
        failure = DiagnosticFailureRecord(
            failureClass="scenario",
            code="checkpoint-mismatch",
            detail="scenario checkpoint failed",
            rail=_identity("e2e"),
            correctiveOwner="portable-owner",
            evidence=(
                RailEvidenceReference(
                    evidenceId="e2e-evidence",
                    sha256=content_digest({"evidence": "e2e"}),
                    size=32,
                    reference="evidence://e2e",
                ),
            ),
        )
    elif disposition == "hard-failure":
        failure = DiagnosticFailureRecord(
            failureClass="infrastructure",
            code="engine-lost",
            detail="runner infrastructure failure",
            correctiveOwner="dagger-owner",
            evidence=(
                RailEvidenceReference(
                    evidenceId="e2e-evidence",
                    sha256=content_digest({"evidence": "e2e"}),
                    size=32,
                    reference="evidence://e2e",
                ),
            ),
        )
    manifest = None
    if disposition == "pass":
        manifest = ctx.green
    elif disposition == "fail":
        manifest = ctx.red
    environment = DiagnosticEnvironmentBinding(
        environmentIdentity="codex-host",
        environmentDigest=content_digest({"environmentIdentity": "codex-host"}),
    )
    authority_payload = {
        "schemaVersion": "diagnostic-runtime-authority/v1",
        "snapshotDigest": "d" * 64,
        "endpoint": "container://shared-dagger-engine",
        "engineId": "shared-dagger-engine",
        "layerStore": "/var/lib/dagger",
        "storeMountedPath": "/var/lib/dagger",
        "observedVersion": "dagger 0.21.8",
    }
    runtime = DiagnosticRuntimeAuthorityBinding(
        snapshotDigest="d" * 64,
        endpoint="container://shared-dagger-engine",
        engineId="shared-dagger-engine",
        layerStore="/var/lib/dagger",
        storeMountedPath="/var/lib/dagger",
        observedVersion="dagger 0.21.8",
        bindingDigest=content_digest(authority_payload),
    )
    teardown = DiagnosticTeardownRecord(
        releasedOwner=True,
        evidence=(
            RailEvidenceReference(
                evidenceId="teardown",
                sha256=content_digest({"evidence": "teardown"}),
                size=16,
                reference="evidence://teardown",
            ),
        ),
        at="2026-09-04T00:10:00Z",
    )
    return {
        "schemaVersion": "diagnostic-run-result/v1",
        "attemptNumber": number,
        "candidateIdentity": ctx.candidate.model_dump(mode="json"),
        "registryDigest": ctx.registryDigest,
        "certifyingPlanDigest": ctx.certifyingPlanDigest,
        "diagnosticPlanDigest": ctx.planDigest,
        "planVersion": "1.0.0",
        "gate": 4,
        "profileId": DIAG_PROFILE,
        "diagnosticNonce": nonce,
        "acceptanceEligible": False,
        "certifying": False,
        "disposition": disposition,
        "failure": failure.model_dump(mode="json") if failure is not None else None,
        "environment": environment.model_dump(mode="json"),
        "runtimeAuthority": runtime.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json") if manifest is not None else None,
        "teardown": teardown.model_dump(mode="json"),
        "artifacts": [],
        "startedAt": "2026-09-04T00:00:00Z",
        "terminalAt": "2026-09-04T00:10:00Z",
    }


def publish_terminal(
    ctx: LaneScenario,
    disposition: Literal["pass", "fail", "aborted", "hard-failure"],
    attempt_id: tuple[int, str],
):
    """Reserve, run, and terminalize one attempt inside the candidate lane."""
    number, nonce = attempt_id
    candidate = ctx.candidate
    reservation = DiagnosticAttemptRecord(
        schemaVersion="diagnostic-attempt/v1",
        candidateIdentity=candidate,
        attemptNumber=number,
        diagnosticNonce=nonce,
        registryDigest=ctx.registryDigest,
        certifyingPlanDigest=ctx.certifyingPlanDigest,
        diagnosticPlanDigest=ctx.planDigest,
        planVersion="1.0.0",
        gate=4,
        profileId=DIAG_PROFILE,
        requestedAt="2026-09-04T00:00:00Z",
        state="running",
        attemptDigest=attempt_digest(candidate, number, nonce, ctx),
    )
    ctx.store.reserve(reservation)
    running = ctx.store.mark_running(reservation)
    payload = draft_payload(ctx, disposition, number, nonce)
    terminal = DiagnosticRunResultDraft(**payload)
    return ctx.store.publish_terminal(running, terminal)


class DiagnosticLaneProjectionTests:
    def test_never_requested_projects_not_requested_optional(self, tmp_path: Path) -> None:
        store = make_lane(tmp_path)
        projection = project_diagnostic_lane(store, CANDIDATE)
        assert projection.disposition == "not-requested-optional"
        assert projection.requested is False
        assert projection.newestResult is None
        assert projection.blockingCertification is False
        assert diagnostic_blocks_certification(store, CANDIDATE) is False
        # no artifact, owner, or telemetry evidence was fabricated
        assert not (tmp_path / "diagnostics").exists() or not list(
            (tmp_path / "diagnostics").iterdir()
        )

    def test_running_attempt_projects_running_without_erasing(self, tmp_path: Path) -> None:
        store = make_lane(tmp_path)
        ctx = scenario(store)
        reservation = DiagnosticAttemptRecord(
            schemaVersion="diagnostic-attempt/v1",
            candidateIdentity=CANDIDATE,
            attemptNumber=1,
            diagnosticNonce=NONCE_ONE,
            registryDigest=ctx.registryDigest,
            certifyingPlanDigest=ctx.certifyingPlanDigest,
            diagnosticPlanDigest=ctx.planDigest,
            planVersion="1.0.0",
            gate=4,
            profileId=DIAG_PROFILE,
            requestedAt="2026-09-04T00:00:00Z",
            state="running",
            attemptDigest=attempt_digest(CANDIDATE, 1, NONCE_ONE, ctx),
        )
        store.reserve(reservation)
        projection = project_diagnostic_lane(store, CANDIDATE)
        assert projection.requested is True
        assert projection.disposition == "running"
        assert projection.newestResult is None
        assert projection.blockingCertification is False

    def test_aborted_and_hard_failure_results_block_certification(self, tmp_path: Path) -> None:
        store = make_lane(tmp_path)
        ctx = scenario(store)
        publish_terminal(ctx, "aborted", (1, NONCE_ONE))
        assert diagnostic_blocks_certification(store, CANDIDATE) is True
        publish_terminal(ctx, "hard-failure", (2, NONCE_TWO))
        projection = project_diagnostic_lane(store, CANDIDATE)
        assert projection.disposition == "hard-failure"
        assert projection.blockingCertification is True
        # the abort is retained immutably as the earlier terminal result
        assert projection.newestResult is not None
        assert projection.newestResult.attemptNumber == 2

    def test_historical_pass_can_never_override_a_newer_failure(self, tmp_path: Path) -> None:
        store = make_lane(tmp_path)
        ctx = scenario(store)
        publish_terminal(ctx, "pass", (1, NONCE_ONE))
        assert diagnostic_blocks_certification(store, CANDIDATE) is False
        publish_terminal(ctx, "fail", (2, NONCE_TWO))
        projection = project_diagnostic_lane(store, CANDIDATE)
        assert projection.disposition == "fail"
        assert projection.blockingCertification is True
        # a later pass for the same candidate unblocks again
        publish_terminal(ctx, "pass", (3, NONCE_THREE))
        assert diagnostic_blocks_certification(store, CANDIDATE) is False
        assert project_diagnostic_lane(store, CANDIDATE).disposition == "pass"

    def test_pass_never_satisfies_or_promotes_into_r14(self, tmp_path: Path) -> None:
        store = make_lane(tmp_path)
        ctx = scenario(store)
        publish_terminal(ctx, "pass", (1, NONCE_ONE))
        projection = project_diagnostic_lane(store, CANDIDATE)
        assert projection.blockingCertification is False
        assert diagnostic_never_satisfies_certification(store, CANDIDATE) is False

    def test_plan_change_stales_the_newest_result_until_a_newer_binding(
        self, tmp_path: Path
    ) -> None:
        store = make_lane(tmp_path)
        ctx = scenario(store)
        publish_terminal(ctx, "pass", (1, NONCE_ONE))
        changed_payload = {
            "schemaVersion": "diagnostic-plan-record/v1",
            "candidateIdentity": CANDIDATE.model_dump(mode="json"),
            "registryDigest": ctx.registryDigest,
            "certifyingPlanDigest": ctx.certifyingPlanDigest,
            "diagnosticPlanDigest": ctx.planDigest,
            "profileId": DIAG_PROFILE,
            "gate": 4,
            "gatePlanDigest": DIGEST,
            "planVersion": "2.0.0",
        }
        changed_plan = DiagnosticPlanRecord(
            **changed_payload,
            planDigest=content_digest(changed_payload),
        )
        stale = project_diagnostic_lane(store, CANDIDATE, current_plan=changed_plan)
        assert stale.currentForPlan is False
        assert stale.blockingCertification is True

    def test_not_requested_optional_never_erases_a_requested_failure(self, tmp_path: Path) -> None:
        store = make_lane(tmp_path)
        ctx = scenario(store)
        publish_terminal(ctx, "fail", (1, NONCE_ONE))
        projection = project_diagnostic_lane(store, CANDIDATE)
        assert projection.disposition == "fail"
        assert projection.requested is True
        # a different, never-requested candidate still starts optional
        other = project_diagnostic_lane(store, OTHER_CANDIDATE)
        assert other.disposition == "not-requested-optional"

    def test_candidates_keep_separate_lanes(self, tmp_path: Path) -> None:
        store = make_lane(tmp_path)
        ctx = scenario(store, OTHER_CANDIDATE)
        publish_terminal(ctx, "fail", (1, NONCE_ONE))
        assert diagnostic_blocks_certification(store, OTHER_CANDIDATE) is True
        assert diagnostic_blocks_certification(store, CANDIDATE) is False
