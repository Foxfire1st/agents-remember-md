"""Exact frozen inputs and terminal selection at the strict quality boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.errors import CertificationContractError
from agents_remember.worktrees.modules.quality.certification_evidence import (
    verify_publication_authority,
    verify_result_evidence,
)
from agents_remember.worktrees.modules.quality.certification_records import (
    PreparedCertificationRun,
    record_published_generation,
)
from agents_remember.worktrees.modules.quality.certification_terminal import (
    RecordedCertificationGeneration,
    RecordedGateTerminal,
)
from agents_remember.worktrees.modules.quality.execution.models import (
    CodeCertificationExecution,
    RetainedGateExecution,
)
from agents_remember.worktrees.modules.quality.published_manifest import (
    PublishedQualityManifest,
    parse_published_quality_manifest,
    published_manifest_payload,
)
from agents_remember.worktrees.modules.quality.report_publication_paths import (
    published_report_path_from_manifest,
)


@dataclass(frozen=True)
class SelectedCodeCertification:
    """Supplied authority and the actual operation owner's terminal CAS."""

    execution: CodeCertificationExecution
    select_terminals: Callable[[RecordedCertificationGeneration], None]
    protected_generations: Callable[[], frozenset[str]]
    authorize_start: Callable[[], None]


def record_terminal_generation(
    prepared: PreparedCertificationRun,
    manifest: PublishedQualityManifest,
    *,
    retained: tuple[RetainedGateExecution, ...] = (),
) -> RecordedCertificationGeneration:
    """Persist complete red or green catalogs before their caller propagates failure."""
    artifact = prepared.worktree_group / "reports" / manifest.result_decoder.artifactPath
    try:
        artifact = published_report_path_from_manifest(
            prepared.worktree_group / "reports", manifest, manifest.result_decoder.artifactPath
        )
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CertificationContractError(
            "repository certification has no readable terminal decoder artifact",
            ({"code": "terminal-catalog-unreadable", "path": str(artifact)},),
        ) from error
    if not isinstance(payload, dict):
        raise CertificationContractError(
            "repository certification terminal decoder must be an object",
            ({"code": "terminal-catalog-invalid", "path": str(artifact)},),
        )
    return record_published_generation(prepared, manifest, payload, retained=retained)


def require_recorded_generation(recorded: RecordedCertificationGeneration) -> None:
    refusals = recorded.as_payload()["refused"]
    if refusals:
        raise RuntimeError(f"repository certificate evidence refused: {refusals}")


def verify_selected_code_terminals(
    prepared: PreparedCertificationRun,
    terminals: tuple[RecordedGateTerminal, ...],
) -> PublishedQualityManifest:
    """Reopen supplied original references; a report pointer or result dictionary is insufficient."""
    if tuple(item.result.gate for item in terminals) != (1, 2, 3, 4):
        raise RuntimeError("selected code certification lacks its exact complete Gate 1-4 prefix")
    objects = prepared.certificate_store()
    chain = []
    for terminal in terminals:
        certificate = terminal.certificate
        reference = terminal.certificateReference
        if certificate is None or reference is None:
            raise RuntimeError("selected code certification has an uncertified terminal")
        if (
            objects.load_reference(reference) != certificate
            or objects.load_reference(terminal.resultReference) != terminal.result
        ):
            raise RuntimeError("selected code certification differs from its original stored bytes")
        publication = parse_published_quality_manifest(
            published_manifest_payload(terminal.publication)
        )
        verify_publication_authority(certificate, terminal.result, publication)
        verify_result_evidence(
            prepared.worktree_group / "reports", publication, terminal.result.railResults
        )
        chain.append(certificate)
    validate_certificate_chain(prepared.frozen_run.admission, chain)
    return terminals[-1].publication
