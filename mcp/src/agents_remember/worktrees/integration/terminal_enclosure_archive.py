"""Crash-safe terminal archive publication before enclosure-root deletion."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.models.lifecycles.enclosure import (
    LifecycleEnclosureLocator,
    TerminalCleanupArguments,
    TerminalCleanupOperation,
    TerminalEnclosureArchive,
    TerminalEnclosureArchiveEntry,
    TerminalEnclosureReceipt,
)
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.worktrees.integration.lifecycle.lifecycle_enclosure_adoption import (
    ADOPTION_RECEIPT,
    LifecycleEnclosureAdoptionReceipt,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_enclosure_terminal import (
    TerminalCleanupContractAuthority,
    terminal_cleanup_contract_authority,
    terminal_enclosure_archive_paths,
    validate_terminal_proof,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_lease import (
    LifecycleOperationCompatibilityError,
    require_lifecycle_operation_compatible,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleLocatorObservation,
    LifecycleOperationLocation,
    LifecycleOperationLocationError,
    TerminalLifecyclePublication,
    inspect_lifecycle_operation_locator,
    publish_terminal_lifecycle_operation_location,
    require_matching_lifecycle_operation_location,
)
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.worktree_contract import WorktreeContract

_OPERATION_RECORD = re.compile(
    r"^(?:closeout|integrate|direct-landing)-operation"
    r"(?:\.generation-[1-9][0-9]*)?\.json$"
)
_LEGACY_MISSING_INTENT_RECORD = re.compile(
    r"^(?:closeout|direct-landing)-operation"
    r"\.legacy-missing-intent-generation-[1-9][0-9]*\.json$"
)
_MAX_CANONICAL_FILES = 1024
_MAX_CANONICAL_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class _ArchiveProof:
    archive: TerminalEnclosureArchive
    archive_path: Path
    archive_sha256: str
    receipt_path: Path


@dataclass(frozen=True)
class _ArchiveRefusal:
    status: str
    detail: str
    expected: dict[str, object]
    observed: dict[str, object]
    next_action: str | None = None
    next_tool: str | None = None
    next_args: dict[str, object] | None = None


def terminal_archive_required_result(
    contract: WorktreeContract,
    *,
    operation: TerminalCleanupOperation,
    arguments: TerminalCleanupArguments,
    dry_run: bool,
) -> WorktreeCommandResult:
    """Publish or re-observe the one archive receipt required before deletion.

    The caller holds the contract lifecycle lease. The helper is deliberately idempotent:
    a crash after archive, receipt, or locator publication reuses the exact bytes, while a
    different cleanup request or changed canonical source refuses without deleting the root.
    """

    observation = inspect_lifecycle_operation_locator(
        contract.coordination_root,
        contract.contract_path,
    )
    try:
        existing = _existing_terminal_result(
            contract,
            observation,
            operation=operation,
            arguments=arguments,
            dry_run=dry_run,
        )
        if existing is not None:
            return existing
        require_lifecycle_operation_compatible(
            contract,
            operation_kind=None,
            publish_worker_exits=not dry_run,
        )
        location = require_matching_lifecycle_operation_location(contract)
        return _publish_terminal_archive(
            contract,
            location,
            operation=operation,
            arguments=arguments,
            dry_run=dry_run,
        )
    except LifecycleOperationCompatibilityError as error:
        return _archive_refusal(
            contract,
            operation,
            _ArchiveRefusal(
                status="terminal-archive-operation-active",
                detail=str(error),
                expected={"activeOperations": []},
                observed={"activeOperations": list(error.blockers)},
                next_action="worktree_status",
                next_tool="worktree_status",
                next_args=_status_args(contract),
            ),
            dry_run=dry_run,
        )
    except LifecycleOperationLocationError as error:
        return _archive_refusal(
            contract,
            operation,
            _location_error_refusal(contract, error),
            dry_run=dry_run,
        )
    except (OSError, UnicodeError, ValidationError, ValueError, RuntimeError) as error:
        return _archive_refusal(
            contract,
            operation,
            _ArchiveRefusal(
                status="terminal-archive-evidence-invalid",
                detail=(
                    "canonical terminal enclosure evidence is unreadable, unsafe, or incomplete"
                ),
                expected={
                    "locatorState": "addressable-or-terminal-archived",
                    "canonicalEvidence": (
                        "bounded readable terminal generations without live authority"
                    ),
                },
                observed={"errorType": type(error).__name__, "detail": str(error)},
                next_action="developer-decision",
            ),
            dry_run=dry_run,
        )


def _existing_terminal_result(
    contract: WorktreeContract,
    observation: LifecycleLocatorObservation,
    *,
    operation: TerminalCleanupOperation,
    arguments: TerminalCleanupArguments,
    dry_run: bool,
) -> WorktreeCommandResult | None:
    if observation.state == "terminal-archived":
        assert observation.locator is not None
        return _observe_terminal_archive(
            contract,
            operation=operation,
            arguments=arguments,
            locator=observation.locator,
            dry_run=dry_run,
        )
    if observation.state == "addressable":
        return None
    return _location_refusal(contract, operation, observation, dry_run=dry_run)


def _publish_terminal_archive(
    contract: WorktreeContract,
    location: LifecycleOperationLocation,
    *,
    operation: TerminalCleanupOperation,
    arguments: TerminalCleanupArguments,
    dry_run: bool,
) -> WorktreeCommandResult:
    archive = _build_terminal_archive(
        contract,
        location,
        operation=operation,
        arguments=arguments,
    )
    archive_text = archive.model_dump_json(indent=2) + "\n"
    archive_path, receipt_path = terminal_enclosure_archive_paths(
        contract.coordination_root,
        location.locator.publicationRequestId,
    )
    proof = _ArchiveProof(
        archive=archive,
        archive_path=archive_path,
        archive_sha256=_sha256(archive_text.encode()),
        receipt_path=receipt_path,
    )
    if dry_run:
        return _archive_result(
            contract,
            operation,
            "would-publish-terminal-archive",
            proof,
            dry_run=True,
        )
    if archive_path.exists():
        _require_accepted_archive_request(
            _read_archive(archive_path, contract.contract_path),
            location,
            operation=operation,
            arguments=arguments,
        )
    _publish_exact_text(archive_path, archive_text, owner="terminal archive")
    _require_archive_readback(contract, proof)
    terminal = _publish_terminal_receipt(contract, location, operation, proof)
    validate_terminal_proof(
        contract.coordination_root,
        contract.contract_path,
        terminal,
    )
    return _archive_result(
        contract,
        operation,
        "terminal-archive-proven",
        proof,
        dry_run=False,
    )


def _require_accepted_archive_request(
    accepted: TerminalEnclosureArchive,
    location: LifecycleOperationLocation,
    *,
    operation: TerminalCleanupOperation,
    arguments: TerminalCleanupArguments,
) -> None:
    same_generation = (
        accepted.locator == location.locator and accepted.manifest == location.manifest
    )
    if not same_generation:
        return
    observed = {
        "cleanupOperation": accepted.cleanupOperation,
        "cleanupArguments": accepted.cleanupArguments.model_dump(mode="json"),
    }
    if accepted.cleanupOperation != operation:
        raise LifecycleOperationLocationError(
            "terminal-archive-operation-conflict",
            "the accepted terminal archive belongs to a different cleanup operation",
            expected={
                "cleanupOperation": operation,
                "cleanupArguments": arguments.model_dump(mode="json"),
            },
            observed=observed,
        )
    if accepted.cleanupArguments != arguments:
        raise LifecycleOperationLocationError(
            "terminal-archive-request-conflict",
            "the accepted terminal archive belongs to different public arguments",
            expected={"cleanupArguments": arguments.model_dump(mode="json")},
            observed=observed,
        )


def _require_archive_readback(
    contract: WorktreeContract,
    proof: _ArchiveProof,
) -> None:
    observed = _read_archive(proof.archive_path, contract.contract_path)
    if observed != proof.archive:
        raise LifecycleOperationLocationError(
            "terminal-archive-conflict",
            "the external terminal archive readback contradicts the accepted request",
            expected={"cleanupRequestId": proof.archive.cleanupRequestId},
            observed={"cleanupRequestId": observed.cleanupRequestId},
        )


def _publish_terminal_receipt(
    contract: WorktreeContract,
    location: LifecycleOperationLocation,
    operation: TerminalCleanupOperation,
    proof: _ArchiveProof,
) -> LifecycleEnclosureLocator:
    terminal = LifecycleEnclosureLocator.model_validate(
        {
            **location.locator.model_dump(mode="json"),
            "state": "terminal-archived",
            "terminalArchivePath": proof.archive_path.resolve(strict=False).as_posix(),
            "terminalArchiveSha256": proof.archive_sha256,
            "terminalReceiptPath": proof.receipt_path.resolve(strict=False).as_posix(),
        }
    )
    receipt = _terminal_receipt(terminal)
    _publish_exact_text(
        proof.receipt_path,
        receipt.model_dump_json(indent=2) + "\n",
        owner="terminal receipt",
    )
    observed = _read_terminal_receipt(proof.receipt_path, contract.contract_path)
    if observed != receipt:
        raise LifecycleOperationLocationError(
            "terminal-archive-receipt-conflict",
            "the external terminal receipt readback contradicts the accepted archive",
            expected=receipt.model_dump(mode="json"),
            observed=observed.model_dump(mode="json"),
        )
    return publish_terminal_lifecycle_operation_location(
        contract,
        location,
        TerminalLifecyclePublication(
            operation=operation,
            archive_path=proof.archive_path,
            archive_sha256=proof.archive_sha256,
            receipt_path=proof.receipt_path,
        ),
    )


def _location_error_refusal(
    contract: WorktreeContract,
    error: LifecycleOperationLocationError,
) -> _ArchiveRefusal:
    if error.status in {
        "terminal-archive-operation-conflict",
        "terminal-archive-request-conflict",
    }:
        accepted = error.observed.get("cleanupOperation")
        arguments = error.observed.get("cleanupArguments")
        if accepted in {"worktree_cleanup", "worktree_abandon"} and isinstance(
            arguments,
            dict,
        ):
            return _ArchiveRefusal(
                error.status,
                error.detail,
                error.expected,
                error.observed,
                next_action=str(accepted),
                next_tool=str(accepted),
                next_args=_terminal_action_args(contract, arguments),
            )
    if error.status.startswith("terminal-archive-operation-"):
        return _ArchiveRefusal(
            error.status,
            error.detail,
            error.expected,
            error.observed,
            next_action="worktree_status",
            next_tool="worktree_status",
            next_args=_status_args(contract),
        )
    return _ArchiveRefusal(
        error.status,
        error.detail,
        error.expected,
        error.observed,
        next_action=(
            "developer-decision" if error.status.startswith("terminal-archive-") else None
        ),
    )


def _observe_terminal_archive(
    contract: WorktreeContract,
    *,
    operation: TerminalCleanupOperation,
    arguments: TerminalCleanupArguments,
    locator: LifecycleEnclosureLocator,
    dry_run: bool,
) -> WorktreeCommandResult:
    authority = terminal_cleanup_contract_authority(
        contract.coordination_root,
        contract,
        locator,
    )
    archive = authority.archive
    assert locator.terminalArchiveSha256 is not None
    assert locator.terminalReceiptPath is not None
    assert locator.terminalArchivePath is not None
    archive_path = Path(locator.terminalArchivePath)
    if archive.cleanupOperation != operation:
        raise LifecycleOperationLocationError(
            "terminal-archive-operation-conflict",
            "the terminal locator already belongs to a different cleanup operation",
            expected={
                "cleanupOperation": operation,
                "cleanupArguments": arguments.model_dump(mode="json"),
            },
            observed={
                "cleanupOperation": archive.cleanupOperation,
                "cleanupArguments": archive.cleanupArguments.model_dump(mode="json"),
            },
        )
    if archive.cleanupArguments != arguments:
        raise LifecycleOperationLocationError(
            "terminal-archive-request-conflict",
            "the terminal locator already belongs to different public arguments",
            expected={"cleanupArguments": arguments.model_dump(mode="json")},
            observed={
                "cleanupOperation": archive.cleanupOperation,
                "cleanupArguments": archive.cleanupArguments.model_dump(mode="json"),
            },
        )
    return _archive_result(
        contract,
        operation,
        "terminal-archive-proven",
        _ArchiveProof(
            archive=archive,
            archive_path=archive_path,
            archive_sha256=locator.terminalArchiveSha256,
            receipt_path=Path(locator.terminalReceiptPath),
        ),
        dry_run=dry_run,
    )


def terminal_contract_authority_if_present(
    contract: WorktreeContract,
) -> TerminalCleanupContractAuthority | None:
    """Return the strict terminal contract/archive authority, never a guessed live fallback."""

    observation = inspect_lifecycle_operation_locator(
        contract.coordination_root,
        contract.contract_path,
    )
    if observation.state != "terminal-archived":
        return None
    assert observation.locator is not None
    return terminal_cleanup_contract_authority(
        contract.coordination_root,
        contract,
        observation.locator,
    )


def _build_terminal_archive(
    contract: WorktreeContract,
    location: LifecycleOperationLocation,
    *,
    operation: TerminalCleanupOperation,
    arguments: TerminalCleanupArguments,
) -> TerminalEnclosureArchive:
    contract_bytes = _read_regular_file(contract.contract_path, owner="worktree contract")
    contract_text = contract_bytes.decode("utf-8")
    contract_sha256 = _sha256(contract_bytes)
    entries = _canonical_entries(location, operation=operation)
    request_id = _cleanup_request_id(
        location.locator.publicationRequestId,
        operation,
        arguments,
        contract_sha256,
    )
    return TerminalEnclosureArchive(
        cleanupOperation=operation,
        cleanupArguments=arguments,
        cleanupRequestId=request_id,
        locator=location.locator,
        manifest=location.manifest,
        contractPath=contract.contract_path.resolve(strict=False).as_posix(),
        contractSha256=contract_sha256,
        contractText=contract_text,
        canonicalEntries=entries,
    )


def _canonical_entries(
    location: LifecycleOperationLocation,
    *,
    operation: TerminalCleanupOperation,
) -> list[TerminalEnclosureArchiveEntry]:
    lifecycle = location.lifecycle_directory
    try:
        paths = sorted(lifecycle.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise RuntimeError("canonical lifecycle directory is unreadable") from error
    entries: list[TerminalEnclosureArchiveEntry] = []
    total_bytes = 0
    for path in paths:
        if path.name.endswith(".lock") or path.name.endswith(".log"):
            continue
        operation_record = _OPERATION_RECORD.fullmatch(path.name) is not None
        missing_intent_record = _LEGACY_MISSING_INTENT_RECORD.fullmatch(path.name) is not None
        if path.name not in {"enclosure-manifest.json", ADOPTION_RECEIPT} and not (
            operation_record or missing_intent_record
        ):
            raise RuntimeError(f"unowned artifact exists in canonical lifecycle root: {path.name}")
        payload = _read_regular_file(path, owner=f"canonical lifecycle artifact {path.name}")
        total_bytes += len(payload)
        if len(entries) >= _MAX_CANONICAL_FILES or total_bytes > _MAX_CANONICAL_BYTES:
            raise RuntimeError("canonical terminal archive exceeds its fixed file or byte bound")
        text = payload.decode("utf-8")
        if operation_record or missing_intent_record:
            record = LifecycleOperationRecord.model_validate_json(text)
            _require_archivable_operation(
                record,
                operation=operation,
                current=operation_record and ".generation-" not in path.name,
                name=path.name,
            )
        elif path.name == ADOPTION_RECEIPT:
            LifecycleEnclosureAdoptionReceipt.model_validate_json(text)
        entries.append(
            TerminalEnclosureArchiveEntry(
                relativePath=path.name,
                sha256=_sha256(payload),
                sizeBytes=len(payload),
                content=text,
            )
        )
    if not entries:
        raise RuntimeError("canonical terminal archive has no enclosure evidence")
    return entries


def _require_archivable_operation(
    record: LifecycleOperationRecord,
    *,
    operation: TerminalCleanupOperation,
    current: bool,
    name: str,
) -> None:
    _require_terminal_operation(record, name)
    _require_cleanup_retry_disposition(record, operation, current, name)
    _require_absent_worker_authority(record, name)
    _require_resolved_worker_termination(record, name)
    _require_resolved_mutations(record, name)
    _require_resolved_publications(record, name)


def _require_terminal_operation(record: LifecycleOperationRecord, name: str) -> None:
    if record.status not in {"completed", "failed", "cancelled"}:
        raise _operation_refusal(
            "terminal-archive-operation-nonterminal",
            f"{name} remains nonterminal at {record.status}",
            name=name,
            observed={"status": record.status},
        )


def _require_cleanup_retry_disposition(
    record: LifecycleOperationRecord,
    operation: TerminalCleanupOperation,
    current: bool,
    name: str,
) -> None:
    if current and operation == "worktree_cleanup" and record.status == "failed":
        raise _operation_refusal(
            "terminal-archive-operation-retryable",
            f"{name} remains retryable and cannot be collected by cleanup",
            name=name,
            observed={"status": record.status},
        )


def _require_absent_worker_authority(record: LifecycleOperationRecord, name: str) -> None:
    if record.workerPid is not None or record.workerLease is not None:
        raise _operation_refusal(
            "terminal-archive-operation-worker-active",
            f"{name} retains worker authority",
            name=name,
            observed={
                "workerPidPresent": record.workerPid is not None,
                "workerLeasePresent": record.workerLease is not None,
            },
        )


def _require_resolved_worker_termination(record: LifecycleOperationRecord, name: str) -> None:
    if record.workerTermination is not None and record.workerTermination.state != "exited":
        raise _operation_refusal(
            "terminal-archive-operation-worker-termination-active",
            f"{name} retains unresolved termination authority",
            name=name,
            observed={"workerTermination": record.workerTermination.state},
        )


def _require_resolved_mutations(record: LifecycleOperationRecord, name: str) -> None:
    if any(item.state == "mutation-intent" for item in record.mutationEvidence.values()):
        raise _operation_refusal(
            "terminal-archive-operation-mutation-ambiguous",
            f"{name} retains ambiguous Git mutation intent",
            name=name,
            observed={"mutationIntent": True},
        )


def _require_resolved_publications(record: LifecycleOperationRecord, name: str) -> None:
    _require_resolved_integration_claim(record, name)
    _require_terminal_organizational_publication(record, name)
    _require_resolved_door_publication(record, name)


def _require_resolved_integration_claim(record: LifecycleOperationRecord, name: str) -> None:
    publication = record.integrationPublication
    if publication is not None and publication.claimState == "intent":
        raise _operation_refusal(
            "terminal-archive-operation-publication-ambiguous",
            f"{name} retains an unproven integration claim publication",
            name=name,
            observed={"integrationClaimState": publication.claimState},
        )


def _require_terminal_organizational_publication(
    record: LifecycleOperationRecord,
    name: str,
) -> None:
    publication = record.integrationPublication
    if (
        publication is not None
        and publication.organizationalCompletion is not None
        and record.status != "completed"
    ):
        raise _operation_refusal(
            "terminal-archive-operation-publication-recoverable",
            f"{name} retains recoverable organizational publication intent",
            name=name,
            observed={"organizationalCompletionPresent": True},
        )


def _require_resolved_door_publication(record: LifecycleOperationRecord, name: str) -> None:
    if record.doorPublication is not None and record.doorPublication.state == "intent":
        raise _operation_refusal(
            "terminal-archive-operation-door-ambiguous",
            f"{name} retains an unproven closeout-door publication",
            name=name,
            observed={"doorPublicationState": record.doorPublication.state},
        )


def _operation_refusal(
    status: str,
    detail: str,
    *,
    name: str,
    observed: dict[str, object],
) -> LifecycleOperationLocationError:
    return LifecycleOperationLocationError(
        status,
        detail,
        expected={
            "operationRecord": name,
            "terminal": True,
            "liveAuthority": False,
            "ambiguousWal": False,
        },
        observed={"operationRecord": name, **observed},
    )


def _publish_exact_text(path: Path, text: str, *, owner: str) -> None:
    expected = text.encode("utf-8")
    if path.exists():
        observed = _read_regular_file(path, owner=owner)
        if observed != expected:
            raise LifecycleOperationLocationError(
                "terminal-archive-conflict",
                f"the existing {owner} belongs to different accepted bytes",
                expected={"sha256": _sha256(expected)},
                observed={"sha256": _sha256(observed)},
            )
    else:
        atomic_write_text(path, text)
    readback = _read_regular_file(path, owner=owner)
    if readback != expected:
        raise LifecycleOperationLocationError(
            "terminal-archive-readback-failed",
            f"the published {owner} did not read back byte-for-byte",
            expected={"sha256": _sha256(expected)},
            observed={"sha256": _sha256(readback)},
        )


def _read_regular_file(path: Path, *, owner: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"{owner} must be one regular non-symlink file")
        return path.read_bytes()
    except OSError as error:
        raise RuntimeError(f"{owner} is unreadable") from error


def _read_archive(path: Path, contract_path: Path) -> TerminalEnclosureArchive:
    try:
        return TerminalEnclosureArchive.model_validate_json(
            _read_regular_file(path, owner="terminal archive")
        )
    except (ValidationError, ValueError, RuntimeError) as error:
        raise LifecycleOperationLocationError(
            "terminal-archive-invalid",
            "the external terminal archive is unreadable or invalid",
            expected={"terminalArchivePath": path.resolve(strict=False).as_posix()},
            observed={"contractPath": contract_path.as_posix(), "errorType": type(error).__name__},
        ) from error


def _read_terminal_receipt(path: Path, contract_path: Path) -> TerminalEnclosureReceipt:
    try:
        return TerminalEnclosureReceipt.model_validate_json(
            _read_regular_file(path, owner="terminal receipt")
        )
    except (ValidationError, ValueError, RuntimeError) as error:
        raise LifecycleOperationLocationError(
            "terminal-archive-receipt-invalid",
            "the external terminal archive receipt is unreadable or invalid",
            expected={"terminalReceiptPath": path.resolve(strict=False).as_posix()},
            observed={"contractPath": contract_path.as_posix(), "errorType": type(error).__name__},
        ) from error


def _terminal_receipt(locator: LifecycleEnclosureLocator) -> TerminalEnclosureReceipt:
    assert locator.terminalArchivePath is not None
    assert locator.terminalArchiveSha256 is not None
    assert locator.terminalReceiptPath is not None
    return TerminalEnclosureReceipt(
        locatorId=locator.locatorId,
        publicationRequestId=locator.publicationRequestId,
        bindingFingerprint=locator.bindingFingerprint,
        repository=locator.repository,
        contractPath=locator.stableAddress,
        worktreeGroup=locator.worktreeGroup,
        manifestPath=locator.manifestPath,
        expectedManifestSha256=locator.expectedManifestSha256,
        expectedInitialContractSha256=locator.expectedInitialContractSha256,
        terminalArchivePath=locator.terminalArchivePath,
        terminalArchiveSha256=locator.terminalArchiveSha256,
        terminalReceiptPath=locator.terminalReceiptPath,
    )


def _cleanup_request_id(
    publication_request_id: str,
    operation: TerminalCleanupOperation,
    arguments: TerminalCleanupArguments,
    contract_sha256: str,
) -> str:
    payload = (
        f"{publication_request_id}\n{operation}\n{arguments.model_dump_json()}\n{contract_sha256}\n"
    ).encode()
    return _sha256(payload)


def _archive_result(
    contract: WorktreeContract,
    operation: TerminalCleanupOperation,
    state: str,
    proof: _ArchiveProof,
    *,
    dry_run: bool,
) -> WorktreeCommandResult:
    archive = proof.archive
    return WorktreeCommandResult(
        0,
        {
            "state": state,
            "status": state,
            "dryRun": dry_run,
            "summary": (
                "Terminal archive is proven and enclosure deletion may continue."
                if state == "terminal-archive-proven"
                else "Terminal cleanup would publish and read back the canonical enclosure archive."
            ),
            "cleanupOperation": operation,
            "cleanupArguments": archive.cleanupArguments.model_dump(mode="json"),
            "cleanupRequestId": archive.cleanupRequestId,
            "contractPath": contract.contract_path.resolve(strict=False).as_posix(),
            "archivePath": proof.archive_path.resolve(strict=False).as_posix(),
            "archiveSha256": proof.archive_sha256,
            "receiptPath": proof.receipt_path.resolve(strict=False).as_posix(),
            "canonicalEntries": [
                {
                    "relativePath": item.relativePath,
                    "sha256": item.sha256,
                    "sizeBytes": item.sizeBytes,
                }
                for item in archive.canonicalEntries
            ],
            "nextAction": operation if dry_run else None,
        },
    )


def _location_refusal(
    contract: WorktreeContract,
    operation: TerminalCleanupOperation,
    observation: LifecycleLocatorObservation,
    *,
    dry_run: bool,
) -> WorktreeCommandResult:
    next_action = (
        "worktree_enclosure_adopt"
        if observation.state == "missing"
        else "worktree_start"
        if observation.state in {"reserved", "manifest-proven"}
        else "developer-decision"
    )
    return _archive_refusal(
        contract,
        operation,
        _ArchiveRefusal(
            status=observation.status or f"operation-location-{observation.state}",
            detail=observation.detail
            or "terminal cleanup requires one exact addressable enclosure locator",
            expected={"locatorState": "addressable-or-terminal-archived"},
            observed={
                "locatorState": observation.state,
                "locatorPath": observation.path.as_posix(),
            },
            next_action=next_action,
        ),
        dry_run=dry_run,
    )


def _archive_refusal(
    contract: WorktreeContract,
    operation: TerminalCleanupOperation,
    refusal: _ArchiveRefusal,
    *,
    dry_run: bool,
) -> WorktreeCommandResult:
    action = refusal.next_action or operation
    developer_required = action == "developer-decision"
    payload: dict[str, object] = {
        "state": refusal.status,
        "status": refusal.status,
        "dryRun": dry_run,
        "summary": refusal.detail,
        "detail": refusal.detail,
        "contractPath": contract.contract_path.resolve(strict=False).as_posix(),
        "expected": refusal.expected,
        "observed": refusal.observed,
        "nextAction": action,
        "developerDecisionRequired": developer_required,
        "decisionSurface": refusal.detail if developer_required else None,
    }
    if refusal.next_tool is not None:
        payload["nextTool"] = refusal.next_tool
    if refusal.next_args is not None:
        payload["nextArgs"] = refusal.next_args
    return WorktreeCommandResult(
        2,
        payload,
    )


def _status_args(contract: WorktreeContract) -> dict[str, object]:
    return {
        "repo_id": contract.repo_name,
        "contract_path": contract.contract_path.resolve(strict=False).as_posix(),
    }


def _terminal_action_args(
    contract: WorktreeContract,
    accepted_arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "contract_path": contract.contract_path.resolve(strict=False).as_posix(),
        "dry_run": False,
        **accepted_arguments,
    }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "terminal_archive_required_result",
    "terminal_contract_authority_if_present",
]
