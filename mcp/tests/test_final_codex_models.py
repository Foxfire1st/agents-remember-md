"""Frozen candidate and rail-result builders for final certification checks."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agents_remember.certification import canonicalize_registry
from agents_remember.certification.digests import content_digest
from agents_remember.certification.final_codex.models import (
    FinalCodexAttemptRecord,
    FinalCodexEnvironmentBinding,
    FinalCodexFailureRecord,
    FinalCodexPlanRecord,
    FinalCodexRepetitionIdentity,
    FinalCodexRepetitionResultDraft,
    FinalCodexRunManifest,
    FinalCodexRuntimeAuthorityBinding,
    FinalCodexTeardownRecord,
)
from agents_remember.certification.final_codex.store import (
    FinalCodexManifestStore,
    FinalCodexStorePolicy,
)
from agents_remember.certification.models import (
    ArtifactDeclaration,
    CandidateIdentity,
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
    RailPosture,
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
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import GateId, RailIdentity
from agents_remember.worktrees.modules.quality import dagger_authority as authority

CERT_PROFILE = "portable-ci"
DIGEST = "a" * 64
PREREQUISITES: dict[str, str] = {
    "suite": "lint",
    "coverage": "suite",
    "e2e": "coverage",
    "memory": "e2e",
}

CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)
OTHER_CANDIDATE = CandidateIdentity(kind="git-tree", value="d" * 40)
NOW = "2026-09-04T12:00:00+00:00"
TERMINAL = "2026-09-04T12:10:00+00:00"
SCENARIO_VERSION = "1.0.0"
PLAN_VERSION = "1.0.0"

CLASS_BY_GATE: dict[GateId, RailClass] = {
    1: "pre-test-quality",
    2: "ordinary-test-suite",
    3: "post-test-quality",
    4: "integration-test",
    5: "memory-quality",
}

DECLARATION_V1: dict[str, object] = {
    "schemaVersion": "dagger-host-declaration/v1",
    "endpoint": "container://shared-dagger-engine",
    "layerStore": "/var/lib/dagger",
    "engineVersion": "v0.21.8",
}

OTHER_ENGINE_DECLARATION_V1: dict[str, object] = {
    **DECLARATION_V1,
    "endpoint": "container://another-dagger-engine",
}


@dataclass(frozen=True)
class RailSpec:
    rail_id: str
    gate: GateId
    posture: RailPosture = "enforcing"


def identity(rail_id: str) -> RailIdentity:
    return RailIdentity(railId=rail_id, version="1.0.0")


def rail(spec: RailSpec) -> RailDefinition:
    applicable = spec.gate != 5
    required = ("suite-data",) if spec.gate == 3 else ()
    outputs = (
        (
            ArtifactDeclaration(
                artifactId="suite-data",
                schemaVersion="suite/v1",
                mediaType="application/json",
            ),
        )
        if spec.gate == 2
        else ()
    )
    return RailDefinition(
        identity=identity(spec.rail_id),
        gate=spec.gate,
        railClass=CLASS_BY_GATE[spec.gate],
        authority="memory-domain" if spec.gate == 5 else "repository-profile",
        ownerClass="portable-owner",
        correctiveOwner="portable-owner",
        posture=spec.posture,
        orderKey=spec.rail_id,
        prerequisites=tuple(
            identity(item) for item in (PREREQUISITES.get(spec.rail_id, ""),) if item
        ),
        requiredArtifacts=required,
        adapter=RailAdapterDefinition(
            adapterKind="process-adapter",
            adapterId=f"{spec.rail_id}-adapter",
            configurationDigest=DIGEST,
            executionEvidence=f"adapter://{spec.rail_id}",
        ),
        runtimeInputs=RailRuntimeInputs(runtimeIdentity="portable-runtime"),
        applicability=(
            RailApplicability(
                profileId=CERT_PROFILE,
                status="applicable" if applicable else "not-applicable",
                selectionIdentity=f"selection:{spec.rail_id}",
                population="exact population" if applicable else None,
                reason=None if applicable else "profile excludes gate-5 memory",
            ),
        ),
        evidenceContract=(
            RailEvidenceContract(
                evidenceId=f"{spec.rail_id}-evidence",
                mediaType="application/json",
                maxBytes=256,
            ),
        ),
        outputArtifacts=outputs,
    )


def scenario_registry() -> CanonicalRailRegistry:
    return canonicalize_registry(
        RailRegistry(
            registryId="portable-final-codex",
            repositoryId="sample-repository",
            profiles=(
                RegistryProfile(
                    profileId=CERT_PROFILE,
                    kind="certifying",
                    gates=(1, 2, 3, 4, 5),
                ),
            ),
            rails=tuple(
                rail(spec)
                for spec in (
                    RailSpec("advisory", 1, posture="report-only"),
                    RailSpec("lint", 1),
                    RailSpec("suite", 2),
                    RailSpec("coverage", 3),
                    RailSpec("e2e", 4),
                    RailSpec("memory", 5),
                )
            ),
        )
    )


def certifying_plan(resolved: CanonicalRailRegistry) -> CertificationPlan:
    return compile_certification_plan(
        resolved,
        profile_id=CERT_PROFILE,
        candidate_identity=CANDIDATE,
    )


def manifest_for(
    registry: CanonicalRailRegistry,
    plan: CertificationPlan,
    gate: GateId,
    *,
    red: bool = False,
    altitude: Literal["certifying", "diagnostic"] = "certifying",
) -> GateResultManifest:
    gate_plan = next(item for item in plan.gates if item.gate == gate)
    results = []
    for planned in gate_plan.rails:
        status = "not-applicable" if planned.applicability.status == "not-applicable" else "pass"
        if red and planned.posture == "enforcing":
            status = "fail"
        artifacts = tuple(
            RailArtifactResult(
                artifactId=item.artifactId,
                sha256=content_digest({"artifact": item.artifactId}),
                size=32,
                evidenceRef=f"artifact://{item.artifactId}",
            )
            for item in planned.outputArtifacts
            if status == "pass"
        )
        observation = RailTerminalObservation(
            rail=planned.identity,
            status=status,
            code=f"{planned.identity.railId}-{status}",
            artifacts=artifacts,
            evidence=tuple(
                RailEvidenceReference(
                    evidenceId=item.evidenceId,
                    sha256=content_digest({"evidence": item.evidenceId}),
                    size=32,
                    reference=f"evidence://{item.evidenceId}",
                )
                for item in planned.evidenceContract
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
            altitude=altitude,
        ),
    )


def scenario_failure(
    *,
    failure_class: str = "scenario",
    rail_id: str = "e2e",
) -> FinalCodexFailureRecord:
    """One typed failure record for a scenario or infrastructure failure."""
    evidence = (
        (
            RailEvidenceReference(
                evidenceId="checkpoint-evidence",
                sha256=content_digest({"evidence": "checkpoint"}),
                size=32,
                reference="evidence://checkpoint",
            ),
        )
        if failure_class in {"scenario", "infrastructure", "parser"}
        else ()
    )
    return FinalCodexFailureRecord(
        failureClass=failure_class,  # type: ignore[arg-type]
        code="checkpoint-failed",
        detail="checkpoint failed",
        rail=identity(rail_id) if failure_class == "scenario" else None,
        correctiveOwner="portable-owner",
        evidence=evidence,
    )


def gate4_manifest(
    registry: CanonicalRailRegistry | None = None,
    *,
    red: bool = False,
) -> GateResultManifest:
    resolved = registry if registry is not None else scenario_registry()
    return manifest_for(resolved, certifying_plan(resolved), 4, red=red)


def repetition_identity(repetition_number: int) -> FinalCodexRepetitionIdentity:
    client = content_digest(
        {
            "candidateIdentity": CANDIDATE.model_dump(mode="json"),
            "repetitionNumber": repetition_number,
        }
    )
    return FinalCodexRepetitionIdentity(
        clientIdentity=client,
        processFingerprint=f"process-{repetition_number}",
        pid=1000 + repetition_number,
    )


def fresh_identities() -> tuple[FinalCodexRepetitionIdentity, ...]:
    return (repetition_identity(1), repetition_identity(2))


def plan_record(
    *,
    registry: CanonicalRailRegistry | None = None,
    plan: CertificationPlan | None = None,
    candidate_identity: CandidateIdentity = CANDIDATE,
    scenario_version: str = SCENARIO_VERSION,
    profile_id: str = CERT_PROFILE,
) -> FinalCodexPlanRecord:
    resolved_registry = registry if registry is not None else scenario_registry()
    resolved_plan = plan if plan is not None else certifying_plan(resolved_registry)
    gate_plan = next(item for item in resolved_plan.gates if item.gate == 4)
    payload = {
        "schemaVersion": "final-codex-plan-record/v1",
        "candidateIdentity": candidate_identity.model_dump(mode="json"),
        "registryDigest": resolved_registry.registryDigest,
        "certifyingPlanDigest": resolved_plan.planDigest,
        "gate": 4,
        "gatePlanDigest": gate_plan.planDigest,
        "scenarioVersion": scenario_version,
        "profileId": profile_id,
        "planVersion": PLAN_VERSION,
    }
    return FinalCodexPlanRecord(**payload, planDigest=content_digest(payload))


def attempt_record(
    *,
    candidate: CandidateIdentity = CANDIDATE,
    attempt_number: int = 1,
    identities: tuple[FinalCodexRepetitionIdentity, ...] | None = None,
    state: Literal["reserved", "running", "terminal"] = "running",
    record: FinalCodexPlanRecord | None = None,
) -> FinalCodexAttemptRecord:
    resolved = identities if identities is not None else fresh_identities()
    resolved_record = record if record is not None else plan_record()
    payload = {
        "schemaVersion": "final-codex-attempt/v1",
        "candidateIdentity": candidate.model_dump(mode="json"),
        "attemptNumber": attempt_number,
        "registryDigest": resolved_record.registryDigest,
        "certifyingPlanDigest": resolved_record.certifyingPlanDigest,
        "gatePlanDigest": resolved_record.gatePlanDigest,
        "scenarioVersion": resolved_record.scenarioVersion,
        "planVersion": resolved_record.planVersion,
        "gate": 4,
        "profileId": resolved_record.profileId,
        "repetitionIdentities": [item.model_dump(mode="json") for item in resolved],
        "requestedAt": NOW,
        "state": state,
    }
    return FinalCodexAttemptRecord(**payload, attemptDigest=content_digest(payload))


def environment_binding(environment_identity: str = "codex-host") -> FinalCodexEnvironmentBinding:
    return FinalCodexEnvironmentBinding(
        environmentIdentity=environment_identity,
        environmentDigest=content_digest({"environmentIdentity": environment_identity}),
    )


def authority_binding(
    snapshot_digest: str = DIGEST,
    endpoint: str = "container://shared-dagger-engine",
    engine_id: str = "shared-dagger-engine",
) -> FinalCodexRuntimeAuthorityBinding:
    payload = {
        "schemaVersion": "final-codex-runtime-authority/v1",
        "snapshotDigest": snapshot_digest,
        "endpoint": endpoint,
        "engineId": engine_id,
        "layerStore": "/var/lib/dagger",
        "storeMountedPath": "/var/lib/dagger",
        "observedVersion": "dagger 0.21.8",
    }
    return FinalCodexRuntimeAuthorityBinding(
        snapshotDigest=snapshot_digest,
        endpoint=endpoint,
        engineId=engine_id,
        layerStore="/var/lib/dagger",
        storeMountedPath="/var/lib/dagger",
        observedVersion="dagger 0.21.8",
        bindingDigest=content_digest(payload),
    )


def teardown_record(
    *,
    released_owner: bool = True,
    process_clean: bool = True,
    evidence: tuple[RailEvidenceReference, ...] | None = None,
) -> FinalCodexTeardownRecord:
    return FinalCodexTeardownRecord(
        releasedOwner=released_owner,
        processClean=process_clean,
        evidence=(
            evidence
            if evidence is not None
            else (
                RailEvidenceReference(
                    evidenceId="teardown",
                    sha256=content_digest({"evidence": "teardown"}),
                    size=16,
                    reference="evidence://teardown",
                ),
            )
        ),
        at=TERMINAL,
    )


def make_draft(
    *,
    repetition_number: int,
    identity: FinalCodexRepetitionIdentity,
    disposition: str,
    record: FinalCodexPlanRecord | None = None,
    overrides: Mapping[str, object] | None = None,
) -> FinalCodexRepetitionResultDraft:
    """Build one repetition draft; overrides replace draft fields by name."""
    resolved_record = record if record is not None else plan_record()
    resolved_binding = authority_binding()
    resolved_environment = environment_binding()
    payload = {
        "schemaVersion": "final-codex-repetition-result/v1",
        "repetitionNumber": repetition_number,
        "candidateIdentity": resolved_record.candidateIdentity.model_dump(mode="json"),
        "registryDigest": resolved_record.registryDigest,
        "certifyingPlanDigest": resolved_record.certifyingPlanDigest,
        "gatePlanDigest": resolved_record.gatePlanDigest,
        "scenarioVersion": resolved_record.scenarioVersion,
        "planVersion": resolved_record.planVersion,
        "gate": resolved_record.gate,
        "profileId": resolved_record.profileId,
        "acceptanceEligible": True,
        "certifying": True,
        "retryCount": 0,
        "repetitionIdentity": identity.model_dump(mode="json"),
        "disposition": disposition,
        "failure": None,
        "environment": resolved_environment.model_dump(mode="json"),
        "runtimeAuthority": resolved_binding.model_dump(mode="json"),
        "manifest": None,
        "teardown": teardown_record().model_dump(mode="json"),
        "artifacts": [],
        "startedAt": NOW,
        "terminalAt": TERMINAL,
    }
    if overrides is not None:
        payload.update(overrides)
    return FinalCodexRepetitionResultDraft(**payload)


def make_store(root: Path) -> FinalCodexManifestStore:
    store_root = root / "final-codex"
    store_root.mkdir(parents=True, exist_ok=True)
    return FinalCodexManifestStore(
        store_root,
        FinalCodexStorePolicy(
            storeId="final-codex",
            forbiddenRoots=(root / "quality-report-generations",),
        ),
    )


def publish_run(
    store: FinalCodexManifestStore,
    *,
    attempt: FinalCodexAttemptRecord,
    drafts: tuple[FinalCodexRepetitionResultDraft, ...],
) -> FinalCodexRunManifest:
    store.reserve(attempt)
    running = store.mark_running(attempt)
    for draft in drafts:
        store.publish_repetition(running, draft)
    manifest = store.manifest(CANDIDATE)
    assert manifest is not None
    return manifest


def store_codes(error: CertificationContractError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


class FakeInspector:
    def __init__(self, *, running: bool = True, store_mounted: bool = True) -> None:
        self.running = running
        self.store_mounted = store_mounted
        self.engine_id = "shared-dagger-engine"

    def inspect(self, declaration: authority.DaggerHostDeclaration):
        if not self.running:
            return authority.InspectedRuntime(
                engine_id=self.engine_id,
                engine_running=False,
                store_mounted_path=declaration.layer_store,
                store_source="/var/lib/docker/volumes/x/_data",
                observed_version=None,
            )
        if not self.store_mounted:
            return authority.InspectedRuntime(
                engine_id=self.engine_id,
                engine_running=True,
                store_mounted_path="/var/lib/dagger-other",
                store_source="/var/lib/docker/volumes/other/_data",
                observed_version="dagger 0.21.8",
            )
        return authority.InspectedRuntime(
            engine_id=self.engine_id,
            engine_running=True,
            store_mounted_path=declaration.layer_store,
            store_source="/var/lib/docker/volumes/shared-layer-store/_data",
            observed_version="dagger 0.21.8",
        )


def engine_environ(declaration: dict[str, object]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (
            authority.HOST_DECLARATION_ENV,
            authority.HOST_REGISTRY_ROOT_ENV,
            authority.DAGGER_HOST_ENV,
        )
    }
    env[authority.HOST_DECLARATION_ENV] = json.dumps(declaration)
    return env
