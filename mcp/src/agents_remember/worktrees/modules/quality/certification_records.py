"""Freeze repository certification authority and retain actual terminal gate evidence.

Preparation stores the complete canonical profile, plans, registry and original admission
before execution. Recovery accepts the supplied immutable frozen run; summary journals
remain presentation and report-retention records, never run-selection authority.

Complete green, red and interrupted catalogs produce content-addressed result manifests.
Only validated green results can produce certificates. Selected reused gates retain their
original certificate/result/publication bytes, and missing or unsupported observations
remain explicit refusals. The lifecycle owner selects returned typed object references.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from agents_remember.certification.certificate_authority import (
    compile_gate_certificate,
)
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
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
from agents_remember.certification.frozen_run.models import (
    FrozenCertificationRun,
    freeze_certification_run,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    GatePlan,
    GateResultAdmission,
    RailResult,
)
from agents_remember.certification.repository_profiles.authority import (
    AdmittedRepositoryProfile,
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.models import (
    ProfilePurpose,
    RepositoryProfilePlan,
)
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
    resolve_repository_profile_selection,
)
from agents_remember.certification.repository_profiles.source_selection.git import (
    observe_profile_source_selection,
)
from agents_remember.certification.results import (
    compile_gate_result_manifest,
)
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.worktrees.modules.quality.certification_evidence import (
    MAX_GATE_RECORD_BYTES,
    publication_binding,
    terminal_publication_binding,
    validate_gate_records,
    verify_result_evidence,
    verify_selected_publications,
)
from agents_remember.worktrees.modules.quality.certification_reuse import record_retained
from agents_remember.worktrees.modules.quality.certification_terminal import (
    GateRecordPublication,
    RecordedCertificationGeneration,
    RecordedGateTerminal,
    TerminalEvidenceMissing,
    catalog_gates,
    not_applicable_terminal_results,
    refused_record,
    terminal_results,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifest,
    parse_published_quality_manifest,
    require_real_directory_or_missing,
    require_real_file_or_missing,
)
from agents_remember.worktrees.services import worktree_services

if TYPE_CHECKING:
    from agents_remember.worktrees.modules.quality.execution.models import RetainedGateExecution

RECORDS_DIRECTORY_NAME = "certification-records"
_ADMISSION_JOURNAL_NAME = "admission.json"
_GATES_JOURNAL_NAME = "gates.json"
_OBJECTS_DIRECTORY_NAME = "objects"
_RECORD_SCHEMA = "repository-gate-certification-records/v1"
_REFUSAL_MISSING_RUN_EVIDENCE = "missing-run-evidence"


@dataclass(frozen=True)
class CertificationRunTarget:
    """The exact repository checkout a certification gate run certifies."""

    repository_id: str
    code_worktree: Path
    profile_reference: object
    worktree_group: Path
    purpose: ProfilePurpose = "closeout"


@dataclass(frozen=True)
class PreparedCertificationRun:
    """The exact admission authority frozen before Gate 1, plus journal paths."""

    worktree_group: Path
    frozen_run: FrozenCertificationRun

    @property
    def candidateTree(self) -> str:
        return self.frozen_run.repositoryPlan.candidateIdentity.value

    @property
    def lane(self) -> CertificationLane:
        run = self.frozen_run
        return CertificationLane(
            run.registry, run.certificationPlan, run.repositoryPlan, run.admission
        )

    @property
    def provenance(self) -> CreationProvenance:
        return self.frozen_run.provenance

    @property
    def frozen_reference(self) -> CertificateObjectReference:
        return self.certificate_store().reference("frozen-run", self.frozen_run.runDigest)

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
            "frozenRun": self.frozen_reference.model_dump(mode="json"),
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
    diff_base: str,
) -> PreparedCertificationRun:
    """Freeze the complete profile and registered memory authority before Gate 1.

    Repository identity never bypasses admission. An unsupported profile or unbound
    memory registry refuses through its existing authority instead of returning success.
    """
    admitted = load_repository_profile(
        target.repository_id,
        target.code_worktree,
        target.profile_reference,
    )
    repository_plan = _repository_plan(
        admitted, mode, candidate_tree, diff_base, purpose=target.purpose
    )
    provenance = _provenance(f"gate:{mode}")
    lane = compile_certification_lane(
        admitted.canonical,
        repository_plan,
        provenance=provenance,
        memory_rails=_bound_memory_rails(repository_plan.selectionId),
    )
    prepared = PreparedCertificationRun(
        worktree_group=target.worktree_group,
        frozen_run=freeze_certification_run(admitted.canonical, lane),
    )
    return _persist_admission(prepared)


def prepared_from_frozen_run(
    worktree_group: Path, run: FrozenCertificationRun
) -> PreparedCertificationRun:
    """Reopen supplied immutable authority without profile or journal rediscovery."""
    validated = FrozenCertificationRun.model_validate(run.model_dump(mode="json"))
    prepared = PreparedCertificationRun(worktree_group, validated)
    # runDigest covers the complete validated record, including provenance. The
    # store verifies that exact address and its original bytes before reopening.
    prepared.certificate_store().load_reference(prepared.frozen_reference)
    return prepared


def record_published_generation(
    prepared: PreparedCertificationRun,
    manifest: PublishedQualityManifest,
    payload: Mapping[str, object],
    *,
    retained: Sequence[RetainedGateExecution] = (),
) -> RecordedCertificationGeneration:
    """Persist complete actual terminal catalogs, including red and interrupted gates.

    payload is the parsed decoder artifact of the verified published
    generation.  Per-gate catalogs come from payload["gates"]; per-rail
    evidence/artifact bindings come from each catalog rail's evidence and
    artifacts records when the executor provides them.  A terminal gate
    without the evidence a green manifest requires is journaled as a typed
    refusal and publishes no certificate.
    """
    _require_publication_admission(prepared, manifest)
    catalog = catalog_gates(payload)
    gate_records: list[dict[str, object]] = []
    run = GateRecordPublication(manifest, [], [])
    selected = {item.certificate.semanticEnvelope.gate: item for item in retained}
    if len(selected) != len(retained):
        raise CertificationContractError(
            "duplicate retained gate", ({"code": "duplicate-retained-gate", "path": "retained"},)
        )
    for entry in catalog:
        gate = entry["gate"]
        assert isinstance(gate, int)
        if entry.get("disposition") == "reused":
            gate_records.append(
                record_retained(prepared, gate, entry, selected.pop(gate, None), run)
            )
            continue
        gate_records.append(_record_gate(prepared, gate, entry, run))
    if selected:
        raise CertificationContractError(
            "retained gates were not reported",
            ({"code": "retained-gate-catalog-mismatch", "path": "gates"},),
        )
    refused = [item for item in gate_records if item["kind"] == "refused"]
    if not any(item["refusalCode"] == "certificate-evidence-binding-mismatch" for item in refused):
        journal_gate_records(prepared.worktree_group, gate_records)
    return RecordedCertificationGeneration(tuple(run.terminals), tuple(gate_records))


def _require_publication_admission(
    prepared: PreparedCertificationRun, manifest: PublishedQualityManifest
) -> None:
    if prepared.candidateTree != manifest.candidate_tree:
        raise RuntimeError(
            "published Dagger generation certifies another candidate tree: "
            f"expected {prepared.candidateTree}, found {manifest.candidate_tree}"
        )
    repository_plan = prepared.lane.repositoryPlan
    if (
        manifest.profile_digest,
        manifest.profile_plan_digest,
        manifest.profile_selection_id,
    ) != (
        repository_plan.profileDigest,
        repository_plan.planDigest,
        repository_plan.selectionId,
    ):
        raise RuntimeError("published Dagger generation binds another admitted repository plan")


def load_execution_records(worktree_group: Path) -> dict[str, object] | None:
    """Read the admission summary for presentation; this is not recovery authority."""
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
    checked = validate_gate_records(records)
    verify_selected_publications(checked, store=certificate_store(worktree_group))
    payload = (
        json.dumps(
            {"schemaVersion": _RECORD_SCHEMA, "recordKind": "gates", "gates": list(checked)},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if len(payload.encode("utf-8")) > MAX_GATE_RECORD_BYTES:
        raise CertificationContractError(
            "certificate selection journal exceeds its byte bound",
            ({"code": "certificate-evidence-capacity", "path": str(path)},),
        )
    _atomic_text(path, payload)
    return path


def _record_gate(
    prepared: PreparedCertificationRun,
    gate: int,
    entry: Mapping[str, object],
    run: GateRecordPublication,
) -> dict[str, object]:
    disposition = entry.get("disposition")
    if not isinstance(disposition, str) or disposition not in {
        "green",
        "red",
        "interrupted",
        "not-applicable",
    }:
        return refused_record(gate, "unsupported-terminal-disposition", disposition=disposition)
    lane = prepared.lane
    gate_plan = next((item for item in lane.certificationPlan.gates if item.gate == gate), None)
    if gate_plan is None:
        return refused_record(gate, "unplanned-gate")
    try:
        if disposition == "not-applicable":
            repository_gate = next(
                (item for item in lane.repositoryPlan.gates if item.gate == gate), None
            )
            if repository_gate is None:
                raise TerminalEvidenceMissing("non-applicable terminal lacks its repository gate")
            results = not_applicable_terminal_results(
                gate_plan, repository_gate, entry, run.publication, prepared.directory.parent
            )
        else:
            results = terminal_results(gate_plan, entry.get("rails"))
        return _publish_gate_result(prepared, gate_plan, results, run, disposition=disposition)
    except TerminalEvidenceMissing as error:
        return refused_record(
            gate, _REFUSAL_MISSING_RUN_EVIDENCE, detail=str(error), disposition=disposition
        )
    except ValidationError as error:
        return refused_record(
            gate, "terminal-catalog-invalid", detail=str(error), disposition=disposition
        )
    except CertificationContractError as error:
        return refused_record(
            gate,
            str(error.findings[0].get("code") or "result-manifest-invalid"),
            detail=str(error),
            disposition=disposition,
        )


def _publish_gate_result(
    prepared: PreparedCertificationRun,
    gate_plan: GatePlan,
    results: tuple[RailResult, ...],
    run: GateRecordPublication,
    *,
    disposition: str,
) -> dict[str, object]:
    lane = prepared.lane
    gate = gate_plan.gate
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
    if disposition == "red" and manifest.disposition == "green":
        return refused_record(gate, "terminal-disposition-mismatch", disposition=disposition)
    verify_result_evidence(prepared.directory.parent, run.publication, manifest.railResults)
    store = prepared.certificate_store()
    store.publish(manifest)
    result_reference = store.reference("result-manifest", manifest.manifestDigest)
    if manifest.disposition != "green" or disposition == "interrupted":
        binding = terminal_publication_binding(
            prepared.directory, prepared.frozen_run, manifest, run.publication
        )
        run.terminals.append(
            RecordedGateTerminal(
                manifest, result_reference, parse_published_quality_manifest(binding)
            )
        )
        return {
            "kind": "terminal",
            "gate": gate,
            "disposition": "interrupted" if disposition == "interrupted" else manifest.disposition,
            "certificate": None,
            "manifest": manifest.manifestDigest,
            "publication": binding,
            "frozenRun": prepared.frozen_reference.model_dump(mode="json"),
            "refusalCode": None,
        }
    certificate = compile_gate_certificate(
        lane.admission,
        gate_plan,
        manifest,
        run.certificates,
        GateCertificateIssuanceContext(provenance=prepared.provenance),
    )
    binding = publication_binding(prepared.directory, certificate, manifest, run.publication)
    existing_path = store.exact_path("certificate", certificate.certificateDigest)
    if existing_path.exists() or existing_path.is_symlink():
        certificate = store.load(GateCertificate, certificate.certificateDigest)
    store.publish(certificate)
    run.certificates.append(certificate)
    publication = parse_published_quality_manifest(binding)
    run.terminals.append(
        RecordedGateTerminal(
            manifest,
            result_reference,
            publication,
            certificate,
            store.reference("certificate", certificate.certificateDigest),
        )
    )
    return {
        "kind": "certificate",
        "gate": gate,
        "disposition": "green",
        "certificate": certificate.certificateDigest,
        "manifest": manifest.manifestDigest,
        "publication": binding,
        "refusalCode": None,
    }


def _bound_memory_rails(profile_id: str):
    """Resolve the Gate-5 memory rails through the bound composition port.

    The worktree layer never imports memory_quality; the MCP/CLI
    composition binds the memory-rails adapter that derives the R11 Gate-5
    rails from the memory checker registries.
    """
    services = worktree_services()
    port = services.certification_memory_rails
    if port is None:
        raise CertificationContractError(
            "certification records require the bound certification-memory-rails port",
            (
                {
                    "code": "certification-memory-rails-unbound",
                    "path": "worktreeServices.certification_memory_rails",
                },
            ),
        )
    return tuple(port.memory_rails(profile_id))


def _repository_plan(
    admitted: AdmittedRepositoryProfile,
    mode: str,
    candidate_tree: str,
    diff_base: str,
    *,
    purpose: ProfilePurpose,
) -> RepositoryProfilePlan:
    selection = resolve_repository_profile_selection(
        admitted.canonical,
        purpose=purpose,
        mode=mode,  # type: ignore[arg-type]
    )
    candidate = CandidateIdentity(kind="git-tree", value=candidate_tree)
    try:
        source_selection = observe_profile_source_selection(
            admitted, selection, candidate, diff_base
        )
    except ValueError as error:
        raise CertificationContractError(
            "candidate source applicability observation refused",
            (
                {
                    "code": "candidate-source-observation-refused",
                    "path": str(admitted.repository_root),
                    "expected": {"candidateTree": candidate_tree, "diffBase": diff_base},
                    "observed": str(error),
                },
            ),
        ) from error
    return compile_repository_profile_plan(
        admitted.canonical,
        selection_id=selection.selectionId,
        candidate_identity=candidate,
        source_selection=source_selection,
    )


def _provenance(producer: str) -> CreationProvenance:
    return CreationProvenance(
        createdAt=datetime.now(UTC).isoformat(),
        producer=producer,
        evidenceRef="enclosure://reports/certification-records",
    )


def _persist_admission(prepared: PreparedCertificationRun) -> PreparedCertificationRun:
    store = prepared.certificate_store()
    admission = prepared.lane.admission
    existing_path = store.exact_path("admission", admission.admissionDigest)
    if existing_path.exists() or existing_path.is_symlink():
        original = store.load(CertificationAdmissionManifest, admission.admissionDigest)
        prepared = replace(
            prepared,
            frozen_run=freeze_certification_run(
                prepared.frozen_run.repositoryProfile,
                replace(prepared.lane, admission=original),
            ),
        )
    store.publish(prepared.lane.admission)
    store.publish(prepared.frozen_run)
    path = prepared.directory / _ADMISSION_JOURNAL_NAME
    _atomic_text(path, json.dumps(prepared.journal(), indent=2, sort_keys=True) + "\n")
    return prepared


def _atomic_text(path: Path, text: str) -> None:
    require_real_directory_or_missing(path.parent.parent, purpose="quality report root")
    require_real_directory_or_missing(path.parent, purpose="certification record root")
    require_real_file_or_missing(path, purpose="certification journal")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)


__all__ = [
    "CertificationRunTarget",
    "PreparedCertificationRun",
    "RecordedCertificationGeneration",
    "RecordedGateTerminal",
    "certificate_store",
    "load_execution_records",
    "prepare_certification_records",
    "prepared_from_frozen_run",
    "record_published_generation",
    "records_directory",
]
