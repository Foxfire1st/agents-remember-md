"""Validate zero-start catalog rows against caller-selected original gate objects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.errors import CertificationContractError
from agents_remember.worktrees.modules.quality.certification_evidence import (
    verify_publication_authority,
    verify_result_evidence,
)
from agents_remember.worktrees.modules.quality.certification_terminal import (
    GateRecordPublication,
    RecordedGateTerminal,
    refused_record,
)
from agents_remember.worktrees.modules.quality.published_manifest import published_manifest_payload

if TYPE_CHECKING:
    from agents_remember.worktrees.modules.quality.certification_records import (
        PreparedCertificationRun,
    )
    from agents_remember.worktrees.modules.quality.execution.models import RetainedGateExecution


def record_retained(
    prepared: PreparedCertificationRun,
    gate: int,
    entry: Mapping[str, object],
    retained: RetainedGateExecution | None,
    run: GateRecordPublication,
) -> dict[str, object]:
    """Reuse only caller-selected original objects and their exact source publication."""
    if retained is None:
        return refused_record(gate, "retained-gate-selection-missing")
    certificate, result, publication = retained.certificate, retained.result, retained.publication
    expected = (
        [],
        certificate.certificateDigest,
        result.manifestDigest,
        published_manifest_payload(publication),
    )
    actual = tuple(
        entry.get(key)
        for key in (
            "rails",
            "certificateDigest",
            "resultManifestDigest",
            "originalPublication",
        )
    )
    if (
        entry.get("started") is not False
        or entry.get("zeroStart") is not True
        or actual != expected
        or certificate.semanticEnvelope.gate != gate
    ):
        return refused_record(gate, "retained-gate-catalog-mismatch")
    try:
        validate_certificate_chain(prepared.lane.admission, (*run.certificates, certificate))
        verify_publication_authority(certificate, result, publication)
        verify_result_evidence(prepared.directory.parent, publication, result.railResults)
        store = prepared.certificate_store()
        certificate_reference = store.reference("certificate", certificate.certificateDigest)
        result_reference = store.reference("result-manifest", result.manifestDigest)
        if (
            store.load_reference(certificate_reference) != certificate
            or store.load_reference(result_reference) != result
        ):
            return refused_record(gate, "retained-gate-original-object-mismatch")
    except CertificationContractError as error:
        return refused_record(gate, str(error.findings[0].get("code")), detail=str(error))
    run.certificates.append(certificate)
    run.terminals.append(
        RecordedGateTerminal(
            result, result_reference, publication, certificate, certificate_reference
        )
    )
    return {
        "kind": "certificate",
        "gate": gate,
        "disposition": "green",
        "certificate": certificate.certificateDigest,
        "manifest": result.manifestDigest,
        "publication": published_manifest_payload(publication),
        "refusalCode": None,
    }
