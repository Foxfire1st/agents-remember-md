"""Exact report-generation bindings selected by the existing gate-record journal."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Never

from agents_remember.certification.certificate_models import GateCertificate
from agents_remember.certification.certificate_store import ContentAddressedCertificateStore
from agents_remember.certification.models import GateResultManifest, RailResult
from agents_remember.errors import CertificationContractError
from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifest,
    parse_published_quality_manifest,
    published_manifest_payload,
    require_real_directory_or_missing,
    require_real_file_or_missing,
)
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)

GATE_RECORD_SCHEMA = "repository-gate-certification-records/v1"
GATE_RECORD_NAME = "gates.json"
MAX_GATE_RECORD_BYTES = 8 * 1024 * 1024
MAX_GATE_RECORDS = 5


def read_gate_records(records_root: Path) -> tuple[dict[str, object], ...]:
    """Read only the selected bounded journal, never scan certificate/report history."""
    path = records_root / GATE_RECORD_NAME
    require_real_directory_or_missing(records_root, purpose="certification records")
    require_real_file_or_missing(path, purpose="selected gate records")
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_GATE_RECORD_BYTES + 1)
    except FileNotFoundError:
        return ()
    except OSError as error:
        _refuse("certificate-evidence-journal-unreadable", str(error))
    if len(raw) > MAX_GATE_RECORD_BYTES:
        _refuse("certificate-evidence-capacity", "selected gate journal exceeds its byte bound")
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        _refuse("certificate-evidence-journal-invalid", str(error))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "recordKind", "gates"}
        or payload.get("schemaVersion") != GATE_RECORD_SCHEMA
        or payload.get("recordKind") != "gates"
    ):
        _refuse("certificate-evidence-journal-invalid", "unexpected selected-journal schema")
    return validate_gate_records(payload.get("gates"))


def validate_gate_records(value: object) -> tuple[dict[str, object], ...]:
    """Bound selection population and require an exact manifest for each live certificate."""
    if not isinstance(value, (list, tuple)) or len(value) > MAX_GATE_RECORDS:
        _refuse("certificate-evidence-capacity", "selected gate population is invalid")
    records: list[dict[str, object]] = []
    seen: set[int] = set()
    for row in value:
        if not isinstance(row, dict):
            _refuse("certificate-evidence-journal-invalid", "gate record is not an object")
        gate = row.get("gate")
        if isinstance(gate, bool) or not isinstance(gate, int) or gate < 1:
            _refuse("certificate-evidence-journal-invalid", "gate identity is invalid")
        if gate in seen:
            _refuse("certificate-evidence-journal-invalid", "duplicate selected gate")
        seen.add(gate)
        if row.get("kind") == "certificate":
            if gate not in range(1, 6):
                _refuse(
                    "certificate-evidence-journal-invalid", "certificate gate identity is invalid"
                )
            _record_publication(row)
        records.append(row)
    return tuple(records)


def protected_certificate_generations(
    reports: Path, *, store: ContentAddressedCertificateStore
) -> frozenset[str]:
    """Selected certificates pin exact generations until journal replacement or cleanup."""
    records = read_gate_records(reports / "certification-records")
    publications = verify_selected_publications(records, store=store)
    generations = frozenset(publication.generation for publication in publications)
    for generation in generations:
        root = reports / ".quality-report-generations" / generation
        require_real_directory_or_missing(root, purpose="retained certificate generation")
        if not root.is_dir():
            _refuse("certificate-evidence-generation-missing", generation)
    return generations


def verify_selected_publications(
    records: Sequence[Mapping[str, object]], *, store: ContentAddressedCertificateStore
) -> tuple[PublishedQualityManifest, ...]:
    """Cross-bind selections to their exact stored objects before publishing or pruning."""
    selected: list[PublishedQualityManifest] = []
    for row in records:
        if row.get("kind") != "certificate":
            continue
        publication = _record_publication(row)
        certificate = store.load_certificate(str(row["certificate"]))
        result = store.load_result_manifest(str(row["manifest"]))
        if row["gate"] != certificate.semanticEnvelope.gate:
            _refuse("certificate-evidence-binding-mismatch", "selected gate identity differs")
        _verify_publication_authority(certificate, result, publication)
        selected.append(publication)
    return tuple(selected)


def publication_binding(
    records_root: Path,
    certificate: GateCertificate,
    result: GateResultManifest,
    publication: PublishedQualityManifest,
) -> dict[str, object]:
    """Retain the original selected generation for a semantically identical certificate."""
    _verify_publication_authority(certificate, result, publication)
    for row in read_gate_records(records_root):
        if row.get("certificate") != certificate.certificateDigest:
            continue
        if row.get("manifest") != result.manifestDigest:
            _refuse("certificate-evidence-binding-mismatch", "selected result identity differs")
        retained = _record_publication(row)
        _verify_publication_authority(certificate, result, retained)
        if row["gate"] != certificate.semanticEnvelope.gate or _execution_authority(
            retained
        ) != _execution_authority(publication):
            _refuse("certificate-evidence-binding-mismatch", "selected execution authority differs")
        publication = retained
        break
    verify_result_evidence(records_root.parent, publication, result.railResults)
    return published_manifest_payload(publication)


def _verify_publication_authority(
    certificate: GateCertificate,
    result: GateResultManifest,
    publication: PublishedQualityManifest,
) -> None:
    envelope = certificate.semanticEnvelope
    expected = (
        envelope.gate,
        envelope.resultManifestDigest,
        envelope.candidateCodeTree,
        envelope.registryDigest,
        envelope.gatePlanDigest,
    )
    actual = (
        result.gate,
        result.manifestDigest,
        result.candidateIdentity,
        result.registryDigest,
        result.gatePlanDigest,
    )
    if actual != expected or (
        publication.candidate_tree,
        publication.profile_digest,
        publication.profile_selection_id,
    ) != (
        envelope.candidateCodeTree.value,
        envelope.admittedProfileDigest,
        result.profileId,
    ):
        _refuse(
            "certificate-evidence-binding-mismatch", "publication certificate authority differs"
        )


def _execution_authority(publication: PublishedQualityManifest) -> tuple[object, ...]:
    """Execution identity excludes physical generation, report bytes and audit provenance."""
    return (
        publication.candidate_tree,
        publication.profile_digest,
        publication.profile_plan_digest,
        publication.profile_selection_id,
        publication.executor_adapter_id,
        publication.result_decoder,
        publication.runtime_authority_digest,
    )


def verify_result_evidence(
    reports: Path,
    publication: PublishedQualityManifest,
    results: Sequence[RailResult],
) -> None:
    """Open every emitted binding through its one accepted immutable generation."""
    for result in results:
        for evidence in result.evidence:
            _verify_reference(
                reports, publication, evidence.reference, evidence.sha256, evidence.size
            )
        for artifact in result.artifacts:
            _verify_reference(
                reports, publication, artifact.evidenceRef, artifact.sha256, artifact.size
            )


def _verify_reference(
    reports: Path, publication: PublishedQualityManifest, reference: str, digest: str, size: int
) -> None:
    try:
        inventory = publication.require_file(reference)
        if inventory.sha256 != digest or inventory.size != size:
            _refuse("certificate-evidence-binding-mismatch", reference)
        published_report_path_from_manifest(reports, publication, reference)
    except (OSError, RuntimeError, ValueError) as error:
        if isinstance(error, CertificationContractError):
            raise
        _refuse("certificate-evidence-unavailable", f"{reference}: {error}")


def _record_publication(row: Mapping[str, object]) -> PublishedQualityManifest:
    for field in ("certificate", "manifest"):
        value = row.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            _refuse("certificate-evidence-binding-missing", f"invalid selected {field}")
    try:
        return parse_published_quality_manifest(row.get("publication"))
    except (TypeError, ValueError, RuntimeError) as error:
        _refuse("certificate-evidence-binding-missing", str(error))


def _refuse(code: str, detail: str) -> Never:
    raise CertificationContractError(
        "certificate report evidence refused",
        ({"code": code, "path": "reports/certification-records/gates.json", "detail": detail},),
    )
