"""CCR-R21 production certificate records for executed repository gate runs.

Every strict repository gate run that a closeout or integration transaction
invokes funnels through gate.run_strict_code_quality_gate /
gate.recover_strict_code_quality_gate.  This module is the production
certificate-record seam those entry points call:

* prepare_certification_records freezes the R21 admission authority
  (registry / certification plan / repository plan / admission manifest)
  BEFORE Gate 1 executes, for the exact candidate tree the run certifies, and
  journals it under the enclosure's reports/certification-records root.
* record_published_generation runs after a green published generation: it
  reads the verified decoder artifact, maps each terminal gate catalog into
  typed rail results + immutable GateResultManifest publication, and mints
  + persists GateCertificate objects through the content-addressed store
  only when the run payload carries the per-rail evidence/artifact bindings the
  R11 manifest contract requires.  A green catalog without that payload is a
  typed, journaled refusal (missing-run-evidence), never a synthesized
  certificate or manifest.
* load_execution_records reopens the exact frozen admission journal so an
  unchanged interruption resumes with byte-identical admission and zero gate
  reruns.

The repository gate run certifies the code candidate only (Gates 1-4).  The
Gate-5 memory domain certifies the memory candidate in the closeout Gate-5
phase against these exact Gate 1-4 certificates.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agents_remember.certification.certificate_authority import compile_gate_certificate
from agents_remember.certification.certificate_models import (
    CreationProvenance,
    GateCertificate,
    GateCertificateIssuanceContext,
)
from agents_remember.certification.certificate_store import (
    CertificateStorePolicy,
    ContentAddressedCertificateStore,
)
from agents_remember.certification.certification_lane import (
    CertificationLane,
    compile_certification_lane,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    CompiledRail,
    GatePlan,
    GateResultAdmission,
    RailArtifactResult,
    RailEvidenceReference,
    RailIdentity,
    RailResult,
    RailStatus,
    RailTerminalObservation,
)
from agents_remember.certification.repository_profiles.authority import (
    AdmittedRepositoryProfile,
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.models import (
    RepositoryProfilePlan,
)
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
    resolve_repository_profile_selection,
)
from agents_remember.certification.results import (
    build_rail_result,
    compile_gate_result_manifest,
)
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifest,
)
from agents_remember.worktrees.services import worktree_services

RECORDS_DIRECTORY_NAME = "certification-records"
_ADMISSION_JOURNAL_NAME = "admission.json"
_GATES_JOURNAL_NAME = "gates.json"
_OBJECTS_DIRECTORY_NAME = "objects"
_RECORD_SCHEMA = "repository-gate-certification-records/v1"
_REPOSITORY_WITH_MEMORY_REGISTRY = "agents-remember"
_REFUSAL_MISSING_RUN_EVIDENCE = "missing-run-evidence"


@dataclass(frozen=True)
class CertificationRunTarget:
    """The exact repository checkout a certification gate run certifies."""

    repository_id: str
    code_worktree: Path
    profile_reference: object
    worktree_group: Path


@dataclass(frozen=True)
class PreparedCertificationRun:
    """The exact admission authority frozen before Gate 1, plus journal paths."""

    worktree_group: Path
    candidateTree: str
    lane: CertificationLane
    provenance: CreationProvenance

    @property
    def directory(self) -> Path:
        return records_directory(self.worktree_group)

    @property
    def admission_digest(self) -> str:
        return self.lane.admission.admissionDigest

    def journal(self) -> dict[str, object]:
        envelope = self.lane.admission.semanticEnvelope
        return {
            "schemaVersion": _RECORD_SCHEMA,
            "candidateTree": self.candidateTree,
            "repositoryId": envelope.repositoryId,
            "profileId": self.lane.selectionId,
            "registryDigest": envelope.registryDigest,
            "certificationPlanDigest": self.lane.certificationPlan.planDigest,
            "repositoryPlanDigest": self.lane.repositoryPlan.planDigest,
            "admissionDigest": self.admission_digest,
            "createdAt": self.provenance.createdAt,
        }

    def certificate_store(self) -> ContentAddressedCertificateStore:
        return certificate_store(self.worktree_group)


def records_directory(worktree_group: Path) -> Path:
    """The one durable records root for an enclosure's repository gate runs."""
    return worktree_group / "reports" / RECORDS_DIRECTORY_NAME


def certificate_store(worktree_group: Path) -> ContentAddressedCertificateStore:
    """One operation-scoped content-addressed certificate/result store."""
    root = records_directory(worktree_group) / _OBJECTS_DIRECTORY_NAME
    policy = CertificateStorePolicy(
        scopeId=f"repository-gate-records:{worktree_group.name}",
        maxObjects=100_000,
        maxBytes=10_000_000_000,
        reclamationOwner="worktree-cleanup",
    )
    return ContentAddressedCertificateStore(root, policy)


def prepare_certification_records(
    target: CertificationRunTarget,
    *,
    mode: str,
    candidate_tree: str,
) -> PreparedCertificationRun | None:
    """Freeze the exact admission authority for the candidate before Gate 1.

    Runs only for the repository whose memory-domain Gate-5 registry is
    registered in-tree (today: agents-remember); other repositories return
    None so their profile executions are unaffected until a memory registry
    adapter is registered for them.  The freeze is deterministic: the admission
    digest binds only semantic inputs, never the creation timestamp.
    """
    if target.repository_id != _REPOSITORY_WITH_MEMORY_REGISTRY:
        return None
    admitted = load_repository_profile(
        target.repository_id,
        target.code_worktree,
        target.profile_reference,
    )
    repository_plan = _repository_plan(admitted, mode, candidate_tree)
    provenance = _provenance(f"gate:{mode}")
    lane = compile_certification_lane(
        admitted.canonical,
        repository_plan,
        provenance=provenance,
        memory_rails=_bound_memory_rails(repository_plan.selectionId),
    )
    prepared = PreparedCertificationRun(
        worktree_group=target.worktree_group,
        candidateTree=candidate_tree,
        lane=lane,
        provenance=provenance,
    )
    _persist_admission(prepared)
    return prepared


def record_published_generation(
    prepared: PreparedCertificationRun,
    manifest: PublishedQualityManifest,
    payload: Mapping[str, object],
) -> dict[str, object]:
    """Publish typed result manifests + certificates for a green generation.

    payload is the parsed decoder artifact of the verified published
    generation.  Per-gate catalogs come from payload["gates"]; per-rail
    evidence/artifact bindings come from each catalog rail's evidence and
    artifacts records when the executor provides them.  A terminal gate
    without the evidence a green manifest requires is journaled as a typed
    refusal and publishes no certificate.
    """
    if prepared.candidateTree != manifest.candidate_tree:
        raise RuntimeError(
            "published Dagger generation certifies another candidate tree: "
            f"expected {prepared.candidateTree}, found {manifest.candidate_tree}"
        )
    catalog = payload.get("gates")
    if not isinstance(catalog, list):
        journal_gate_records(prepared.worktree_group, [])
        return {"published": [], "certificates": [], "refused": []}
    gate_records: list[dict[str, object]] = []
    certificates: list[GateCertificate] = []
    for entry in catalog:
        if not isinstance(entry, dict):
            raise RuntimeError("published gate catalog contains a non-object entry")
        gate = entry.get("gate")
        if isinstance(gate, bool) or not isinstance(gate, int):
            raise RuntimeError("published gate catalog entry lacks an exact gate")
        outcomes = entry.get("rails")
        rail_outcomes: list[Mapping[str, object]] = []
        if isinstance(outcomes, list):
            for outcome in outcomes:
                if isinstance(outcome, dict):
                    rail_outcomes.append(outcome)
        gate_records.append(
            _record_gate(prepared, gate, entry.get("disposition"), rail_outcomes, certificates)
        )
    journal_gate_records(prepared.worktree_group, gate_records)
    published = [item for item in gate_records if item["kind"] == "certificate"]
    refused = [item for item in gate_records if item["kind"] == "refused"]
    return {
        "published": published,
        "certificates": [item["certificate"] for item in published],
        "refused": refused,
    }


def load_execution_records(worktree_group: Path) -> dict[str, object] | None:
    """Reopen the exact frozen admission journal or return None."""
    path = records_directory(worktree_group) / _ADMISSION_JOURNAL_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schemaVersion") != _RECORD_SCHEMA:
        return None
    return raw


def journal_gate_records(worktree_group: Path, records: Sequence[dict[str, object]]) -> Path:
    """Atomically replace the per-gate terminal record journal."""
    path = records_directory(worktree_group) / _GATES_JOURNAL_NAME
    _atomic_text(
        path,
        json.dumps(
            {"schemaVersion": _RECORD_SCHEMA, "recordKind": "gates", "gates": list(records)},
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return path


def _record_gate(
    prepared: PreparedCertificationRun,
    gate: int,
    disposition: object,
    rail_outcomes: Sequence[Mapping[str, object]],
    certificates: list[GateCertificate],
) -> dict[str, object]:
    if disposition != "green":
        return {
            "kind": "terminal",
            "gate": gate,
            "disposition": disposition,
            "certificate": None,
            "manifest": None,
            "refusalCode": None,
        }
    lane = prepared.lane
    gate_plan = next((item for item in lane.certificationPlan.gates if item.gate == gate), None)
    if gate_plan is None:
        return _refused(gate, "unplanned-gate")
    try:
        results = _terminal_results(gate_plan, rail_outcomes)
    except _TerminalEvidenceMissing as error:
        return _refused(gate, _REFUSAL_MISSING_RUN_EVIDENCE, detail=str(error))
    try:
        manifest = compile_gate_result_manifest(
            lane.registry,
            lane.certificationPlan,
            gate_plan,
            results,
            GateResultAdmission(
                profileId=lane.selectionId,
                candidateIdentity=lane.candidate,
                altitude=lane.certificationPlan.profileKind,
            ),
        )
    except CertificationContractError as error:
        code = str(error.findings[0].get("code") or "result-manifest-invalid")
        return _refused(gate, code)
    store = prepared.certificate_store()
    store.publish_result_manifest(manifest)
    if manifest.disposition != "green":
        return {
            "kind": "terminal",
            "gate": gate,
            "disposition": manifest.disposition,
            "certificate": None,
            "manifest": manifest.manifestDigest,
            "refusalCode": None,
        }
    certificate = compile_gate_certificate(
        lane.admission,
        gate_plan,
        manifest,
        certificates,
        GateCertificateIssuanceContext(provenance=prepared.provenance),
    )
    store.publish_certificate(certificate)
    certificates.append(certificate)
    return {
        "kind": "certificate",
        "gate": gate,
        "disposition": "green",
        "certificate": certificate.certificateDigest,
        "manifest": manifest.manifestDigest,
        "refusalCode": None,
    }


class _TerminalEvidenceMissing(RuntimeError):
    """A terminal gate result lacks the executor's per-rail evidence payload."""


def _refused(gate: int, code: str, *, detail: str = "") -> dict[str, object]:
    return {
        "kind": "refused",
        "gate": gate,
        "disposition": "green",
        "certificate": None,
        "manifest": None,
        "refusalCode": code,
        "refusalDetail": detail,
    }


def _terminal_results(
    gate_plan: GatePlan,
    rail_outcomes: Sequence[Mapping[str, object]],
) -> tuple[RailResult, ...]:
    """Map a terminal gate catalog into typed rail results (payload-bound).

    A green pipeline publishes pass outcomes; a contradictory catalog rail
    (fail/blocked under a green disposition, or an unknown status) is
    refused below the manifest boundary rather than invented away.  Only an
    outcome the payload records is mapped -- never a synthesized one.
    """
    catalog = {str(outcome.get("key")): outcome for outcome in rail_outcomes}
    statuses: dict[str, RailStatus] = {
        "pass": "pass",
        "fail": "fail",
        "blocked": "blocked",
        "skipped": "not-applicable",
    }
    observations: list[RailTerminalObservation] = []
    planned: dict[str, CompiledRail] = {rail.identity.key: rail for rail in gate_plan.rails}
    for rail in gate_plan.rails:
        outcome = catalog.get(rail.identity.key)
        if outcome is None:
            raise _TerminalEvidenceMissing(
                f"gate {gate_plan.gate} catalog omits rail {rail.identity.key}"
            )
        evidence = _payload_list(outcome, "evidence")
        artifacts = _payload_list(outcome, "artifacts")
        raw_status = outcome.get("status")
        raw = raw_status if isinstance(raw_status, str) else ""
        status = statuses.get(raw)
        if status is None:
            raise _TerminalEvidenceMissing(
                f"gate {gate_plan.gate} catalog rail {rail.identity.key} is not a "
                f"certifiable terminal outcome: {raw_status!r}"
            )
        observations.append(
            RailTerminalObservation(
                rail=rail.identity,
                status=status,
                code=_terminal_code(rail, outcome),
                blockedBy=_terminal_blocked_by(outcome, planned),
                artifacts=tuple(
                    RailArtifactResult(**item) for item in artifacts if isinstance(item, dict)
                ),
                evidence=tuple(
                    RailEvidenceReference(**item) for item in evidence if isinstance(item, dict)
                ),
            )
        )
    return tuple(build_rail_result(gate_plan, observation) for observation in observations)


def _terminal_code(rail: CompiledRail, outcome: Mapping[str, object]) -> str:
    """One deterministic terminal code from the payload or the rail identity."""
    failure_code = outcome.get("failureCode")
    if isinstance(failure_code, str) and failure_code:
        return failure_code
    status = outcome.get("status")
    return f"{rail.identity.railId}-{status}"


def _terminal_blocked_by(
    outcome: Mapping[str, object],
    planned: Mapping[str, CompiledRail],
) -> tuple[RailIdentity, ...]:
    """Resolve payload blockedBy keys only to planned same-gate rails."""
    if outcome.get("status") != "blocked":
        return ()
    keys = outcome.get("blockedBy")
    if not isinstance(keys, list):
        return ()
    return tuple(planned[key].identity for key in keys if isinstance(key, str) and key in planned)


def _payload_list(mapping: Mapping[str, object], name: str) -> list[object]:
    value = mapping.get(name)
    return list(value) if isinstance(value, list) else []


def _bound_memory_rails(profile_id: str):
    """Resolve the Gate-5 memory rails through the bound composition port.

    The worktree layer never imports memory_quality; the MCP/CLI
    composition binds the memory-rails adapter that derives the R11 Gate-5
    rails from the memory checker registries.
    """
    services = worktree_services()
    port = services.certification_memory_rails
    if port is None:
        raise RuntimeError(
            "certification records require the bound certification-memory-rails port; "
            "the composition layer must bind WorktreeServices with the adapter"
        )
    return tuple(port.memory_rails(profile_id))


def _repository_plan(
    admitted: AdmittedRepositoryProfile,
    mode: str,
    candidate_tree: str,
) -> RepositoryProfilePlan:
    selection = resolve_repository_profile_selection(
        admitted.canonical,
        purpose="closeout",
        mode=mode,  # type: ignore[arg-type]
    )
    return compile_repository_profile_plan(
        admitted.canonical,
        selection_id=selection.selectionId,
        candidate_identity=CandidateIdentity(kind="git-tree", value=candidate_tree),
    )


def _provenance(producer: str) -> CreationProvenance:
    return CreationProvenance(
        createdAt=datetime.now(UTC).isoformat(),
        producer=producer,
        evidenceRef="enclosure://reports/certification-records",
    )


def _persist_admission(prepared: PreparedCertificationRun) -> None:
    path = prepared.directory / _ADMISSION_JOURNAL_NAME
    _atomic_text(path, json.dumps(prepared.journal(), indent=2, sort_keys=True) + "\n")
    store = prepared.certificate_store()
    try:
        store.publish_admission(prepared.lane.admission)
    except CertificationContractError as error:
        # Provenance is excluded from the semantic digest, so an unchanged
        # rerun publishes the same admission bytes-identically under the same
        # digest; a second publication of that exact digest is idempotent.
        if _is_content_address_collision(error):
            return
        raise


def _is_content_address_collision(error: CertificationContractError) -> bool:
    return any(
        str(finding.get("code")) == "content-address-collision" for finding in error.findings
    )


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)


__all__ = [
    "CertificationRunTarget",
    "PreparedCertificationRun",
    "certificate_store",
    "load_execution_records",
    "prepare_certification_records",
    "record_published_generation",
    "records_directory",
]
