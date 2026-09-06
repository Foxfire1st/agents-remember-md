"""Isolated schema-1 inspect, migration, and evidence-gated archive bridge."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from agents_remember.errors import TaskIntentError
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.models.closeout.input import (
    EffectiveCloseoutInput,
    EnabledCloseoutLeg,
    NotApplicableCloseoutLeg,
)
from agents_remember.models.lifecycles.legacy import LegacyCloseoutMigrationProof
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    GatePolicyRuleSnapshot,
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.models.lifecycles.operation_kinds import LifecycleOperationKind
from agents_remember.worktrees.integration.closeout.task_intent_identity import (
    contract_task_intent,
)
from agents_remember.worktrees.integration.legacy.legacy_operation_archive import (
    finish_archive_unlink as _finish_archive_unlink,
)
from agents_remember.worktrees.integration.legacy.legacy_operation_archive import (
    publish_archive as _publish_archive,
)
from agents_remember.worktrees.integration.legacy.legacy_operation_archive import (
    read_publication_bytes as _read_publication_bytes,
)
from agents_remember.worktrees.integration.legacy.legacy_operation_archive import (
    terminal_archive_evidence as _terminal_archive_evidence,
)
from agents_remember.worktrees.integration.legacy.legacy_operation_authority import (
    legacy_lifecycle_lease,
    legacy_pre_adoption,
    require_explicit_bridge_compatible,
    revalidated_legacy_target,
)
from agents_remember.worktrees.integration.legacy.legacy_operation_failures import (
    LegacyBridgeError,
    LegacyIoFailure,
    legacy_io_error,
)
from agents_remember.worktrees.integration.legacy.legacy_operation_public import (
    LEGACY_BRIDGE_REMOVAL_CONDITION,
    legacy_amendment_digests,
    legacy_archive_response,
)
from agents_remember.worktrees.integration.legacy.legacy_operation_schema import (
    LegacyArchive,
    LegacyCloseoutInput,
    LegacyRecoveryCommits,
    LegacySchemaOneRecord,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    operation_key,
    operation_state_fingerprint,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_projection import (
    operation_projection,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    lifecycle_operation_record_access,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)
from agents_remember.worktrees.integration.mutation_evidence import (
    initial_closeout_mutation_evidence,
)
from agents_remember.worktrees.modules.git import (
    current_branch,
    head_commit,
    require_git,
)
from agents_remember.worktrees.worktree_contract import WorktreeContract

LegacyOperationCommandAction = Literal["inspect", "migrate", "archive"]


@dataclass(frozen=True)
class LegacyOperationCommand:
    operation_kind: LifecycleOperationKind
    action: LegacyOperationCommandAction
    expected_digest: str = ""
    memory_commit_message: str | None = None
    ledger_commit_message: str | None = None
    audit_reason: str = ""
    dry_run: bool = False


@dataclass(frozen=True)
class _MigrationValues:
    original: bytes
    digest: str
    memory_commit_message: str | None
    ledger_commit_message: str | None
    audit_reason: str


@dataclass(frozen=True)
class _LegacyPublication:
    path: Path
    archive_path: Path
    original: bytes
    digest: str


def legacy_operation_action(
    contract: WorktreeContract,
    *,
    request: LegacyOperationCommand,
    revalidate_contract: Callable[[], WorktreeContract],
) -> dict[str, object]:
    """Inspect or atomically replace one task-addressed legacy record."""
    operation_kind = request.operation_kind
    pre_adoption = legacy_pre_adoption(contract)
    if request.action == "inspect" or request.dry_run:
        # Read-only evidence never creates a durable lock/receipt/index artifact. Apply
        # revalidates the returned digest under both lifecycle and record serialization.
        current, target = revalidated_legacy_target(
            contract,
            operation_kind,
            pre_adoption=pre_adoption,
            revalidate_contract=revalidate_contract,
        )
        if request.action != "inspect":
            require_explicit_bridge_compatible(
                current,
                target,
                operation_kind,
                publish_worker_exits=False,
            )
        return _legacy_operation_action_locked(current, target.path, request)
    with legacy_lifecycle_lease(contract, pre_adoption=pre_adoption):
        current, target = revalidated_legacy_target(
            contract,
            operation_kind,
            pre_adoption=pre_adoption,
            revalidate_contract=revalidate_contract,
        )
        require_explicit_bridge_compatible(
            current,
            target,
            operation_kind,
            publish_worker_exits=True,
        )
        with lifecycle_operation_record_access(target.path):
            return _legacy_operation_action_locked(current, target.path, request)


def legacy_bridge_removal_guard(coordination_root: Path) -> dict[str, object]:
    """Measure whether the bounded bridge can be removed after its release window."""
    recognized = 0
    migrated = 0
    active_migrated = 0
    worktrees = coordination_root / "worktrees"
    for path in worktrees.rglob("*-operation.json") if worktrees.exists() else ():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("schemaVersion") == "1.0":
            try:
                LegacySchemaOneRecord.model_validate(payload)
            except ValidationError:
                continue
            recognized += 1
            continue
        if payload.get("schemaVersion") != "3.0":
            continue
        try:
            record = LifecycleOperationRecord.model_validate(payload)
        except ValidationError:
            continue
        if record.legacyMigration is None:
            continue
        migrated += 1
        if record.status != "completed":
            active_migrated += 1
    return {
        "recognizedSchemaOne": recognized,
        "canonicalMigrated": migrated,
        "nonterminalMigrated": active_migrated,
        "releaseWindowRequired": True,
        "eligibleAfterReleaseWindow": recognized == 0 and active_migrated == 0,
        "condition": LEGACY_BRIDGE_REMOVAL_CONDITION,
    }


def _legacy_operation_action_locked(
    contract: WorktreeContract,
    path: Path,
    request: LegacyOperationCommand,
) -> dict[str, object]:
    operation_kind = request.operation_kind
    archive_path = _archive_path(path)
    if not path.exists():
        if archive_path.exists():
            return _completed_archive_response(contract, archive_path, request)
        raise LegacyBridgeError(
            "legacy-operation-missing",
            "no lifecycle operation exists for this canonical task and kind",
        )
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        if archive_path.exists():
            return _completed_archive_response(contract, archive_path, request)
        raise LegacyBridgeError(
            "legacy-operation-missing",
            "the lifecycle operation disappeared before its exact bytes were read",
        ) from None
    except OSError as exc:
        raise legacy_io_error(
            "legacy-operation-unreadable",
            "legacy operation bytes are unreadable",
            failure=LegacyIoFailure("legacy-record-read", "journal", path.name, exc),
            next_action=request.action if request.action != "inspect" else "developer-decision",
        ) from exc
    digest = hashlib.sha256(original).hexdigest()
    if archive_path.exists():
        return _pending_archive_response(
            contract,
            path,
            archive_path,
            digest,
            request,
        )
    current = _current_migration(original)
    if current is not None:
        _require_current_migration_identity(contract, operation_kind, current)
        result = _existing_migration_response(contract, current, request)
    else:
        legacy = _parse_legacy(original)
        _require_canonical_identity(contract, operation_kind, legacy)
        if request.action == "inspect":
            result = _inspection(contract, legacy, digest)
        else:
            _require_apply_arguments(request.expected_digest, digest, request.audit_reason)
            publication = _LegacyPublication(path, archive_path, original, digest)
            result = (
                _archive_legacy(contract, legacy, request, publication)
                if request.action == "archive"
                else _migrate_legacy(contract, legacy, request, publication)
            )
    return result


def _pending_archive_response(
    contract: WorktreeContract,
    path: Path,
    archive_path: Path,
    digest: str,
    request: LegacyOperationCommand,
) -> dict[str, object]:
    archive = _read_archive(archive_path)
    _require_archive_identity(contract, request.operation_kind, archive)
    if archive.originalSha256 != digest:
        raise LegacyBridgeError(
            "legacy-archive-conflict",
            "existing archive receipt belongs to different original bytes",
            expected={"legacyDigest": digest},
            observed={"legacyDigest": archive.originalSha256},
        )
    if request.action != "archive":
        raise LegacyBridgeError(
            "legacy-archive-publication-pending",
            "archive receipt is durable; finish the task-addressed archive action",
            next_action="archive",
        )
    _require_archive_replay(archive, request)
    if not request.dry_run:
        _finish_archive_unlink(path, archive, archive_path)
    return legacy_archive_response(archive, archive_path, dry_run=request.dry_run)


def _completed_archive_response(
    contract: WorktreeContract,
    archive_path: Path,
    request: LegacyOperationCommand,
) -> dict[str, object]:
    archive = _read_archive(archive_path)
    _require_archive_identity(contract, request.operation_kind, archive)
    if request.action == "inspect":
        return legacy_archive_response(archive, archive_path)
    if request.action != "archive":
        raise LegacyBridgeError(
            "legacy-record-already-archived",
            "the legacy operation is already terminally archived",
        )
    _require_archive_replay(archive, request)
    return legacy_archive_response(archive, archive_path, dry_run=request.dry_run)


def _archive_legacy(
    contract: WorktreeContract,
    legacy: LegacySchemaOneRecord,
    request: LegacyOperationCommand,
    publication: _LegacyPublication,
) -> dict[str, object]:
    archive = LegacyArchive(
        originalBytesBase64=base64.b64encode(publication.original).decode("ascii"),
        originalSha256=publication.digest,
        operationKind=legacy.operationKind,
        taskId=legacy.taskId,
        taskName=legacy.taskName,
        contractPath=legacy.contractPath,
        status=legacy.status,
        phase=legacy.phase,
        auditReason=request.audit_reason.strip(),
        terminalEvidence=_terminal_archive_evidence(contract, legacy),
        archivedAt=_stamp(),
    )
    if not request.dry_run:
        _publish_archive(
            publication.path,
            publication.archive_path,
            archive,
            original=publication.original,
        )
    return legacy_archive_response(
        archive,
        publication.archive_path,
        dry_run=request.dry_run,
    )


def _migrate_legacy(
    contract: WorktreeContract,
    legacy: LegacySchemaOneRecord,
    request: LegacyOperationCommand,
    publication: _LegacyPublication,
) -> dict[str, object]:
    migrated = _migrated_closeout_record(
        contract,
        legacy,
        _MigrationValues(
            publication.original,
            publication.digest,
            request.memory_commit_message,
            request.ledger_commit_message,
            request.audit_reason,
        ),
    )
    migrated_text = migrated.model_dump_json(indent=2, exclude_none=True) + "\n"
    if not request.dry_run:
        try:
            atomic_write_text(publication.path, migrated_text)
        except OSError as exc:
            observed = _read_publication_bytes(publication.path)
            if observed == migrated_text.encode("utf-8"):
                pass
            elif observed == publication.original:
                raise LegacyBridgeError(
                    "legacy-migration-publication-interrupted",
                    "canonical migration publication left the exact original bytes unchanged",
                    expected={
                        "originalDigest": publication.digest,
                        "migratedDigest": hashlib.sha256(migrated_text.encode("utf-8")).hexdigest(),
                    },
                    observed={"recordDigest": publication.digest},
                    next_action="migrate",
                ) from exc
            else:
                raise LegacyBridgeError(
                    "legacy-migration-publication-conflict",
                    "migration publication produced a third record byte state",
                    expected={
                        "originalDigest": publication.digest,
                        "migratedDigest": hashlib.sha256(migrated_text.encode("utf-8")).hexdigest(),
                    },
                    observed={
                        "recordDigest": hashlib.sha256(observed or b"").hexdigest(),
                        "recordPresent": observed is not None,
                    },
                ) from exc
        observed = _read_publication_bytes(publication.path)
        if observed != migrated_text.encode("utf-8"):
            raise LegacyBridgeError(
                "legacy-migration-publication-conflict",
                "migration publication did not preserve the exact canonical bytes",
                observed={
                    "recordDigest": hashlib.sha256(observed or b"").hexdigest(),
                    "recordPresent": observed is not None,
                },
            )
    return {
        "state": "would-migrate" if request.dry_run else "migrated",
        "legacyDigest": publication.digest,
        "contractPath": contract.contract_path.as_posix(),
        "operationKind": request.operation_kind,
        "lifecycleOperation": operation_projection(migrated, contract=contract).model_dump(
            mode="json", exclude_none=True
        ),
        "removalCondition": LEGACY_BRIDGE_REMOVAL_CONDITION,
        "removalGuard": legacy_bridge_removal_guard(contract.coordination_root),
        "dryRun": request.dry_run,
    }


def _current_migration(original: bytes) -> LifecycleOperationRecord | None:
    try:
        payload = json.loads(original)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schemaVersion") != "3.0":
        return None
    try:
        record = LifecycleOperationRecord.model_validate(payload)
    except ValidationError:
        return None
    return record if record.legacyMigration is not None else None


def _existing_migration_response(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    request: LegacyOperationCommand,
) -> dict[str, object]:
    proof = record.legacyMigration
    assert proof is not None
    if request.action == "archive":
        raise LegacyBridgeError(
            "legacy-record-already-migrated",
            "a canonical migrated generation must use normal recovery controls",
            next_action="recover",
        )
    if request.action == "migrate" and request.expected_digest != proof.originalSha256:
        raise LegacyBridgeError(
            "legacy-digest-changed",
            "migrate retry does not name the preserved original digest",
            expected={"legacyDigest": proof.originalSha256},
            observed={"legacyDigest": request.expected_digest},
            next_action="recover",
        )
    if request.action == "migrate" and (
        (request.memory_commit_message or "").strip() != proof.memoryCommitMessage
        or (request.ledger_commit_message or "").strip() != proof.ledgerCommitMessage
        or request.audit_reason.strip() != proof.auditReason
    ):
        raise LegacyBridgeError(
            "legacy-migration-amendment-refused",
            "migrate retry cannot change accepted fill messages or audit reason",
            expected=legacy_amendment_digests(
                proof.memoryCommitMessage,
                proof.ledgerCommitMessage,
                proof.auditReason,
            ),
            observed=legacy_amendment_digests(
                (request.memory_commit_message or "").strip(),
                (request.ledger_commit_message or "").strip(),
                request.audit_reason.strip(),
            ),
            next_action="recover",
        )
    return {
        "state": "migrated",
        "legacyDigest": proof.originalSha256,
        "contractPath": contract.contract_path.as_posix(),
        "operationKind": record.operationKind,
        "lifecycleOperation": operation_projection(record, contract=contract).model_dump(
            mode="json", exclude_none=True
        ),
        "removalCondition": LEGACY_BRIDGE_REMOVAL_CONDITION,
        "removalGuard": legacy_bridge_removal_guard(contract.coordination_root),
        "dryRun": request.dry_run,
    }


def _require_current_migration_identity(
    contract: WorktreeContract,
    operation_kind: LifecycleOperationKind,
    record: LifecycleOperationRecord,
) -> None:
    expected = {
        "taskId": contract.task_id,
        "taskName": contract.task_name,
        "contractPath": contract.contract_path.as_posix(),
        "operationKind": operation_kind,
    }
    observed = {
        "taskId": record.taskId,
        "taskName": record.taskName,
        "contractPath": record.contractPath,
        "operationKind": record.operationKind,
    }
    if expected != observed:
        raise LegacyBridgeError(
            "legacy-migration-identity-mismatch",
            "migrated record does not bind the requested canonical task and kind",
            expected=expected,
            observed=observed,
        )


def _parse_legacy(original: bytes) -> LegacySchemaOneRecord:
    try:
        payload = json.loads(original)
        return LegacySchemaOneRecord.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise LegacyBridgeError(
            "legacy-envelope-unsupported",
            "record is not the exact supported schema-1 lifecycle envelope",
            observed={
                "failure": public_failure_evidence(
                    stage="legacy-envelope-parse",
                    side="journal",
                    name="current-operation.json",
                    error_type=type(exc).__name__,
                    expected={"schemaVersion": "1.0"},
                    observed={"state": "invalid"},
                )
            },
        ) from exc


def _require_canonical_identity(
    contract: WorktreeContract,
    operation_kind: LifecycleOperationKind,
    legacy: LegacySchemaOneRecord,
) -> None:
    expected = {
        "taskId": contract.task_id,
        "taskName": contract.task_name,
        "contractPath": contract.contract_path.as_posix(),
        "operationKind": operation_kind,
    }
    observed = {
        "taskId": legacy.taskId,
        "taskName": legacy.taskName,
        "contractPath": legacy.contractPath,
        "operationKind": legacy.operationKind,
    }
    if expected != observed:
        raise LegacyBridgeError(
            "legacy-identity-mismatch",
            "legacy record does not bind the canonical task, contract, and kind",
            expected=expected,
            observed=observed,
        )
    expected_key = operation_key(
        contract.contract_path,
        legacy.operationKind,
        legacy.fingerprint,
    )
    if legacy.operationKey != expected_key:
        public_identity = {
            "taskId": contract.task_id,
            "taskName": contract.task_name,
            "contractPath": contract.contract_path.as_posix(),
            "operationKind": legacy.operationKind,
            "generation": 1,
        }
        raise LegacyBridgeError(
            "legacy-operation-key-mismatch",
            "legacy operation key does not derive from canonical contract, kind, and fingerprint",
            expected={**public_identity, "operationKey": expected_key},
            observed={**public_identity, "operationKey": legacy.operationKey},
        )


def _inspection(
    contract: WorktreeContract,
    legacy: LegacySchemaOneRecord,
    digest: str,
) -> dict[str, object]:
    migratable, migration_reason = _migration_classification(contract, legacy)
    archivable, archive_reason = _archive_classification(contract, legacy)
    return {
        "state": "inspected",
        "contractPath": contract.contract_path.as_posix(),
        "operationKind": legacy.operationKind,
        "legacyDigest": digest,
        "migratable": migratable,
        "migrationReason": migration_reason,
        "archivable": archivable,
        "archiveReason": archive_reason,
        "removalCondition": LEGACY_BRIDGE_REMOVAL_CONDITION,
        "removalGuard": legacy_bridge_removal_guard(contract.coordination_root),
        "dryRun": True,
    }


def _migration_classification(
    contract: WorktreeContract,
    legacy: LegacySchemaOneRecord,
) -> tuple[bool, str]:
    try:
        _require_migratable_closeout(contract, legacy)
    except LegacyBridgeError as exc:
        return False, exc.detail
    return True, "exact schema-1 closeout incident; explicit unfinished messages required"


def _archive_classification(
    contract: WorktreeContract,
    legacy: LegacySchemaOneRecord,
) -> tuple[bool, str]:
    try:
        _terminal_archive_evidence(contract, legacy)
    except LegacyBridgeError as exc:
        return False, exc.detail
    return True, "terminal/no-live-authority Git and contract evidence is exact"


def _migrated_closeout_record(
    contract: WorktreeContract,
    legacy: LegacySchemaOneRecord,
    values: _MigrationValues,
) -> LifecycleOperationRecord:
    legacy_input, commits, repository, ref, code_tree = _require_migratable_closeout(
        contract, legacy
    )
    memory_message = _required_fill("memory_commit_message", values.memory_commit_message)
    ledger_message = _required_fill("ledger_commit_message", values.ledger_commit_message)
    effective = EffectiveCloseoutInput(
        route="worktree",
        contractKind="leaf",
        memoryMode=contract.memory_mode,
        code=NotApplicableCloseoutLeg(reason="verified-existing legacy code output"),
        memory=EnabledCloseoutLeg(
            reason="unfinished external-memory content from schema-1 closeout",
            message=memory_message,
        ),
        ledger=EnabledCloseoutLeg(
            reason="unfinished ledger mapping from schema-1 closeout",
            message=ledger_message,
        ),
    )
    operation_input = CloseoutOperationInput(
        configPath=legacy_input.configPath,
        contractPath=legacy_input.contractPath,
        effectiveInput=effective,
        approvalNote=legacy_input.approvalNote.strip(),
        gatePolicy=_legacy_gate_policy(legacy_input),
    )
    proof = LegacyCloseoutMigrationProof(
        originalBytesBase64=base64.b64encode(values.original).decode("ascii"),
        originalSha256=values.digest,
        legacyOperationKey=legacy.operationKey,
        legacyFingerprint=legacy.fingerprint,
        legacyCandidateState=legacy.candidateState,
        legacyCandidateTree=legacy.candidateTree or "",
        legacyCodeCommitMessage=legacy_input.codeCommitMessage.strip(),
        legacyApprovalNote=legacy_input.approvalNote.strip(),
        memoryCommitMessage=memory_message,
        ledgerCommitMessage=ledger_message,
        auditReason=values.audit_reason.strip(),
        codeRepository=repository.as_posix(),
        codeRef=ref,
        codeCommit=commits.codeCommit,
        codeTree=code_tree,
        provenAt=_stamp(),
    )
    try:
        intent = contract_task_intent(contract)
    except TaskIntentError as exc:
        raise LegacyBridgeError(
            exc.status,
            exc.detail,
            next_action=exc.next_action or "task_doc",
        ) from exc
    return LifecycleOperationRecord(
        taskId=legacy.taskId,
        taskName=legacy.taskName,
        contractPath=legacy.contractPath,
        operationKind="closeout",
        candidateState=legacy.candidateState,
        candidateTree=legacy.candidateTree,
        taskIntent=intent,
        fingerprint=legacy.fingerprint,
        operationKey=legacy.operationKey,
        input=operation_input,
        status="input-required",
        phase="recovering-after-claim",
        queuedAt=legacy.queuedAt,
        startedAt=legacy.startedAt,
        heartbeatAt=legacy.heartbeatAt,
        currentCommand="recover exact migrated schema-1 closeout generation",
        reportPath=legacy.reportPath,
        guidance="Use the advertised recover action; the legacy code output is immutable.",
        irreversibleBoundaryEntered=True,
        approvalClaimed=True,
        mutationEvidence=initial_closeout_mutation_evidence(contract, effective),
        recoveryCommits=LifecycleOperationRecoveryCommits(codeCommit=commits.codeCommit),
        attempt=legacy.attempt,
        legacyMigration=proof,
    )


def _legacy_gate_policy(
    legacy_input: LegacyCloseoutInput,
) -> list[GatePolicyRuleSnapshot]:
    return [
        GatePolicyRuleSnapshot.model_validate(rule.model_dump()) for rule in legacy_input.gatePolicy
    ]


def _require_migratable_closeout(
    contract: WorktreeContract,
    legacy: LegacySchemaOneRecord,
) -> tuple[LegacyCloseoutInput, LegacyRecoveryCommits, Path, str, str]:
    if legacy.operationKind != "closeout":
        raise LegacyBridgeError(
            "legacy-migration-kind-unsupported",
            "only the exact schema-1 closeout incident is migratable",
        )
    try:
        legacy_input = LegacyCloseoutInput.model_validate(legacy.input)
    except ValidationError as exc:
        raise LegacyBridgeError(
            "legacy-closeout-input-unsupported",
            "legacy closeout input is not the exact known envelope",
            observed={
                "failure": public_failure_evidence(
                    stage="legacy-closeout-input-parse",
                    side="journal",
                    name="input",
                    error_type=type(exc).__name__,
                    expected={"envelope": "schema-1-closeout-v1"},
                    observed={"state": "invalid"},
                )
            },
        ) from exc
    commits = legacy.recoveryCommits
    exact_incident = all(
        (
            contract.kind == "leaf",
            contract.memory_mode == "external",
            operation_state_fingerprint(contract) == legacy.candidateState,
            legacy.candidateTree is not None,
            legacy.status == "input-required",
            legacy.phase == "contract-finalization",
            legacy.irreversibleBoundaryEntered,
            legacy.approvalClaimed,
            legacy.workerPid is None,
            bool(legacy_input.codeCommitMessage.strip()),
            not legacy_input.memoryCommitMessage.strip(),
            not legacy_input.ledgerCommitMessage.strip(),
            bool(legacy_input.approvalNote.strip()),
            commits is not None,
            bool(commits and commits.codeCommit),
            not bool(commits and commits.memoryContentCommit),
            not bool(commits and commits.ledgerCommit),
        )
    )
    if not exact_incident or commits is None or legacy.candidateTree is None:
        raise LegacyBridgeError(
            "legacy-closeout-not-migratable",
            "schema-1 record is not the exact code-proven, blank-unfinished-cell incident",
            next_action="developer-decision",
        )
    repository = contract.code_worktree
    ref = require_git(repository, ["symbolic-ref", "--quiet", "HEAD"])
    expected_ref = f"refs/heads/{contract.code_work_branch}"
    code_head = head_commit(repository)
    code_tree = require_git(repository, ["rev-parse", f"{code_head}^{{tree}}"])
    if (
        current_branch(repository) != contract.code_work_branch
        or ref != expected_ref
        or code_head != commits.codeCommit
        or code_tree != legacy.candidateTree
    ):
        raise LegacyBridgeError(
            "legacy-code-output-mismatch",
            "live task ref does not prove the legacy code output and accepted tree",
            expected={
                "codeRef": expected_ref,
                "codeCommit": commits.codeCommit,
                "codeTree": legacy.candidateTree,
            },
            observed={"codeRef": ref, "codeCommit": code_head, "codeTree": code_tree},
        )
    return legacy_input, commits, repository, ref, code_tree


def _require_apply_arguments(expected_digest: str, actual: str, audit_reason: str) -> None:
    if expected_digest != actual:
        raise LegacyBridgeError(
            "legacy-digest-changed",
            "legacy bytes changed after inspection",
            expected={"legacyDigest": expected_digest},
            observed={"legacyDigest": actual},
        )
    if not audit_reason.strip():
        raise LegacyBridgeError(
            "legacy-audit-reason-required",
            "legacy migrate/archive requires a nonblank audit reason",
        )


def _required_fill(field: str, value: str | None) -> str:
    normalized = value.strip() if value is not None else ""
    if not normalized:
        raise LegacyBridgeError(
            "legacy-fill-required",
            f"{field} must explicitly fill its proven-empty unfinished cell",
            next_action="migrate",
        )
    return normalized


def _require_archive_identity(
    contract: WorktreeContract,
    operation_kind: LifecycleOperationKind,
    archive: LegacyArchive,
) -> None:
    expected = {
        "taskId": contract.task_id,
        "taskName": contract.task_name,
        "contractPath": contract.contract_path.as_posix(),
        "operationKind": operation_kind,
    }
    observed = {
        "taskId": archive.taskId,
        "taskName": archive.taskName,
        "contractPath": archive.contractPath,
        "operationKind": archive.operationKind,
    }
    if expected != observed:
        raise LegacyBridgeError(
            "legacy-archive-identity-mismatch",
            "archive receipt does not bind the requested canonical task and kind",
            expected=expected,
            observed=observed,
        )


def _require_archive_replay(
    archive: LegacyArchive,
    request: LegacyOperationCommand,
) -> None:
    if request.expected_digest != archive.originalSha256:
        raise LegacyBridgeError(
            "legacy-digest-changed",
            "archive retry does not name the preserved original digest",
            expected={"legacyDigest": archive.originalSha256},
            observed={"legacyDigest": request.expected_digest},
            next_action="archive",
        )
    if request.audit_reason.strip() != archive.auditReason:
        raise LegacyBridgeError(
            "legacy-archive-amendment-refused",
            "archive retry cannot change the accepted audit reason",
            next_action="archive",
        )


def _archive_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.schema-1-archive.json")


def _read_archive(path: Path) -> LegacyArchive:
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise legacy_io_error(
            "legacy-archive-invalid",
            "legacy archive evidence is unreadable",
            failure=LegacyIoFailure("legacy-archive-read", "archive", path.name, exc),
        ) from exc
    try:
        return LegacyArchive.model_validate_json(payload)
    except ValidationError as exc:
        raise legacy_io_error(
            "legacy-archive-invalid",
            "legacy archive evidence does not satisfy its bounded schema",
            failure=LegacyIoFailure("legacy-archive-validate", "archive", path.name, exc),
            observed={"state": "invalid", "size": len(payload.encode("utf-8"))},
        ) from exc


def _stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
