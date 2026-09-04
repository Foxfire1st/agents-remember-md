"""Strict, atomic enclosure-local storage for long lifecycle operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from agents_remember.controlplane.durable_store import StoreOwnership, exclusive_access
from agents_remember.errors import TaskIntentError
from agents_remember.kernel.atomic_write import atomic_write_text
from agents_remember.models.lifecycles.mutation_evidence import GitMutationEvidence
from agents_remember.models.lifecycles.operation import (
    LifecycleOperationKind,
    LifecycleOperationRecord,
    LifecycleOperationStatus,
)
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.models.task_intent import (
    require_task_intent_identity,
    task_intent_is_missing,
)
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    closeout_generation_retained,
    require_closeout_finalization_evidence,
    require_closeout_recovery_projection,
)

_OWNERSHIP = StoreOwnership(
    store="lifecycle-operation",
    writers=("mcp", "lifecycle-operation"),
    compaction_owner=None,
    rationale="the MCP starts operations and its detached worker advances the same record",
)
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_NON_RESUMABLE = frozenset({"completed", "cancelled"})
_ALLOWED: dict[LifecycleOperationStatus, frozenset[LifecycleOperationStatus]] = {
    "queued": frozenset(
        {"queued", "running", "input-required", "termination-required", "failed", "cancelled"}
    ),
    "running": frozenset(
        {
            "queued",
            "running",
            "input-required",
            "termination-required",
            "completed",
            "failed",
            "cancelled",
        }
    ),
    "input-required": frozenset({"queued", "input-required", "termination-required", "cancelled"}),
    "termination-required": frozenset(
        {
            "queued",
            "running",
            "input-required",
            "termination-required",
            "completed",
            "failed",
            "cancelled",
        }
    ),
    "completed": frozenset({"completed", "termination-required"}),
    "failed": frozenset({"failed", "termination-required", "cancelled"}),
    "cancelled": frozenset({"cancelled"}),
}
_MUTATION_ALLOWED = {
    "pre-mutation": frozenset({"pre-mutation", "mutation-intent"}),
    "mutation-intent": frozenset({"mutation-intent", "reconciled-unchanged", "commit-proven"}),
    "reconciled-unchanged": frozenset({"reconciled-unchanged"}),
    "commit-proven": frozenset({"commit-proven"}),
}


def _validate_recovery_commits_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    current_commits = current.recoveryCommits
    if current_commits is None:
        return
    if updated.recoveryCommits is None:
        raise RuntimeError("recorded lifecycle recovery commits cannot be cleared")
    for field in ("codeCommit", "memoryContentCommit", "ledgerCommit"):
        before = getattr(current_commits, field)
        after = getattr(updated.recoveryCommits, field)
        if before and after != before:
            raise RuntimeError("recorded lifecycle recovery commits can only fill empty cells")


def _validate_quality_certification_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    before = current.qualityCertification
    after = updated.qualityCertification
    if before is not None and after != before:
        raise RuntimeError("recorded integration quality certification is immutable")
    if after is not None and current.operationKind != "integrate":
        raise RuntimeError("only integration operations may record quality certification")


def _validate_integration_publication_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    before = current.integrationPublication
    after = updated.integrationPublication
    if before is not None and after != before:
        allowed_claim_proof = (
            after is not None
            and before.claimState == "intent"
            and after.claimState == "proven"
            and after.model_copy(update={"claimState": "intent", "claimTransferredAt": None})
            == before
        )
        if not allowed_claim_proof:
            raise RuntimeError("recorded integration publication intent is immutable")
    if after is not None and current.operationKind != "integrate":
        raise RuntimeError("only integration operations may record publication intent")


def _validate_organizational_repair_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    before = current.organizationalRepair
    after = updated.organizationalRepair
    if before is not None and after != before:
        raise RuntimeError("recorded organizational repair evidence is immutable")


def _validate_mutation_evidence_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    for leg, before in current.mutationEvidence.items():
        after = updated.mutationEvidence[leg]
        retry_reset = (
            before.state == "reconciled-unchanged"
            and after.state == "pre-mutation"
            and updated.mutationHistory.get(leg) == [*current.mutationHistory.get(leg, []), before]
        )
        _validate_mutation_evidence_identity(before, after, retry_reset=retry_reset)
        if retry_reset:
            continue
        if after.state not in _MUTATION_ALLOWED[before.state]:
            raise RuntimeError(
                f"invalid closeout mutation evidence transition {before.state} -> {after.state}"
            )
        if before.observed is not None and after.observed != before.observed:
            raise RuntimeError("closeout observed Git evidence is immutable once recorded")
        if (
            before.expectedOutputTree is not None
            and after.expectedOutputTree != before.expectedOutputTree
        ):
            raise RuntimeError("closeout expected output tree is immutable once recorded")
    for leg, history in current.mutationHistory.items():
        if updated.mutationHistory.get(leg, [])[: len(history)] != history:
            raise RuntimeError("closeout mutation-attempt history is append-only")


def _validate_mutation_evidence_identity(
    before: GitMutationEvidence,
    after: GitMutationEvidence,
    *,
    retry_reset: bool,
) -> None:
    if after.leg != before.leg or after.repository != before.repository:
        raise RuntimeError("closeout mutation evidence identity is immutable")
    if before.acceptedBefore is not None and after.acceptedBefore != before.acceptedBefore:
        raise RuntimeError("closeout accepted Git prestate is immutable")
    if not retry_reset and before.before is not None and after.before != before.before:
        raise RuntimeError("closeout pre-command Git evidence is immutable")


def _validate_worker_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    before = current.workerTermination
    after = updated.workerTermination
    if before is not None and not _worker_exit_was_archived(current, updated):
        _validate_worker_termination_evidence(before, after)
    _validate_worker_authority_transition(current, updated)
    history = current.workerTerminationHistory
    if updated.workerTerminationHistory[: len(history)] != history:
        raise RuntimeError("worker termination history is append-only")


def _worker_exit_was_archived(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> bool:
    before = current.workerTermination
    return (
        before is not None
        and before.state == "exited"
        and updated.workerTermination is None
        and updated.workerTerminationHistory == [*current.workerTerminationHistory, before]
    )


def _validate_worker_termination_evidence(
    before: WorkerTerminationEvidence,
    after: WorkerTerminationEvidence | None,
) -> None:
    allowed = {
        "requested": {"requested", "termination-required", "exited"},
        "termination-required": {"termination-required", "exited"},
        "exited": {"exited"},
    }
    if after is None or after.state not in allowed[before.state]:
        raise RuntimeError("worker termination evidence is monotonic")
    if (
        after.pid != before.pid
        or after.lease != before.lease
        or after.processFingerprint != before.processFingerprint
        or after.requestedAt != before.requestedAt
    ):
        raise RuntimeError("worker termination identity is immutable")


def _validate_worker_authority_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    after = updated.workerTermination
    if current.workerLease is not None and updated.workerLease not in {
        current.workerLease,
        None,
    }:
        raise RuntimeError("worker lease cannot be replaced before proven exit")
    if (
        current.workerPid is not None
        and updated.workerPid is None
        and (after is None or after.state != "exited")
    ):
        raise RuntimeError("worker authority cannot clear before proven exit")


def _validate_door_publication_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    before = current.doorPublication
    after = updated.doorPublication
    history = current.doorPublicationHistory
    if updated.doorPublicationHistory[: len(history)] != history:
        raise RuntimeError("door publication history is append-only")
    if before is None:
        return
    archived = (
        before.state == "proven"
        and after is not None
        and after.generation != before.generation
        and updated.doorPublicationHistory == [*history, before]
    )
    if archived:
        return
    if after is None:
        raise RuntimeError("door publication evidence cannot be cleared")
    if before.state == "proven" and after != before:
        raise RuntimeError("proven door publication evidence is immutable")
    if before.state == "intent" and (
        after.generation != before.generation
        or after.expectedBeforeContractSha256 != before.expectedBeforeContractSha256
        or after.expectedPublishedContractSha256 != before.expectedPublishedContractSha256
    ):
        raise RuntimeError("door publication intent identity is immutable")


def _validate_closeout_finalization_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    before = current.closeoutFinalizedContractSha256
    after = updated.closeoutFinalizedContractSha256
    if before is not None and after != before:
        raise RuntimeError("closeout finalized contract SHA-256 is immutable once recorded")
    if before is None and after is not None and updated.phase != "contract-finalization":
        raise RuntimeError(
            "closeout finalized contract SHA-256 must be introduced at contract-finalization"
        )
    require_closeout_finalization_evidence(updated)


def _validate_legacy_migration_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    if current.legacyMigration is not None and updated.legacyMigration != current.legacyMigration:
        raise RuntimeError("legacy migration proof is immutable")


def _validate_direct_ledger_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    before = current.directLandingLedgerIntent
    if before is not None and updated.directLandingLedgerIntent != before:
        raise RuntimeError("direct landing ledger intent is immutable once published")


def _validate_identity_and_evidence_transition(
    current: LifecycleOperationRecord,
    updated: LifecycleOperationRecord,
) -> None:
    """Validate the shared immutable/monotonic envelope for every store mutation."""

    if updated.recordRevision != current.recordRevision + 1:
        raise RuntimeError("lifecycle operation record revision must advance exactly once")

    immutable = (
        "schemaVersion",
        "taskId",
        "taskName",
        "contractPath",
        "operationKind",
        "generation",
        "predecessorFingerprint",
        "candidateState",
        "candidateTree",
        "taskIntent",
        "fingerprint",
        "integrationAuthority",
        "reportPath",
    )
    for field in immutable:
        if getattr(current, field) != getattr(updated, field):
            raise RuntimeError(f"lifecycle operation transition cannot change {field}")
    if updated.operationKey != current.operationKey or updated.input != current.input:
        raise RuntimeError("lifecycle operation transition cannot change its durable input")
    if current.approvalClaimed and not updated.approvalClaimed:
        raise RuntimeError("a claimed approval cannot become unclaimed")
    _validate_recovery_commits_transition(current, updated)
    _validate_quality_certification_transition(current, updated)
    _validate_integration_publication_transition(current, updated)
    _validate_organizational_repair_transition(current, updated)
    _validate_mutation_evidence_transition(current, updated)
    _validate_worker_transition(current, updated)
    _validate_door_publication_transition(current, updated)
    _validate_legacy_migration_transition(current, updated)
    _validate_direct_ledger_transition(current, updated)
    if (
        current.cancellationEvidence is not None
        and updated.cancellationEvidence != current.cancellationEvidence
    ):
        raise RuntimeError("proven cancellation evidence is immutable")
    require_closeout_recovery_projection(updated)
    _validate_closeout_finalization_transition(current, updated)
    if current.irreversibleBoundaryEntered and not updated.irreversibleBoundaryEntered:
        raise RuntimeError("an entered irreversible boundary cannot be cleared")


def _advance_record_revision(
    current: LifecycleOperationRecord,
    transformed: LifecycleOperationRecord,
) -> LifecycleOperationRecord:
    """Assign the next revision at the one canonical journal writer boundary."""

    if transformed.recordRevision != current.recordRevision:
        raise RuntimeError("lifecycle operation transforms cannot assign record revision")
    return LifecycleOperationRecord.model_validate(
        transformed.model_copy(update={"recordRevision": current.recordRevision + 1}).model_dump(
            mode="json"
        )
    )


def operation_record_path(worktree_group: Path, operation_kind: LifecycleOperationKind) -> Path:
    return worktree_group / ".lifecycle" / f"{operation_kind}-operation.json"


def operation_report_path(worktree_group: Path, operation_kind: LifecycleOperationKind) -> Path:
    return worktree_group / ".lifecycle" / f"{operation_kind}-operation.log"


JournalReadSide = Literal["current-record"]


class LifecycleOperationReadError(RuntimeError):
    """Strict-reader failure whose public projection never needs exception text."""

    def __init__(
        self,
        path: Path,
        *,
        side: JournalReadSide,
        error_type: str,
        observed: dict[str, object],
        expected: dict[str, object],
    ) -> None:
        self.path = path
        self.side = side
        self.name = path.name
        self.error_type = error_type
        self.expected = expected
        self.observed = observed
        super().__init__(f"strict lifecycle {side} is unreadable or invalid")


class LifecycleOperationSchemaError(LifecycleOperationReadError):
    """The strict normal reader observed a non-current durable schema."""

    def __init__(self, path: Path, observed_schema: object) -> None:
        self.expected_schema = "3.0"
        self.observed_schema = observed_schema
        known_schema = isinstance(observed_schema, str) and observed_schema in {"1.0", "2.0"}
        public_schema = (
            observed_schema
            if known_schema
            else {"state": "unsupported", "valueType": type(observed_schema).__name__}
        )
        super().__init__(
            path,
            side="current-record",
            error_type="LifecycleOperationSchemaError",
            expected={"state": "readable", "schemaVersion": "3.0"},
            observed={"state": "legacy-or-unsupported-schema", "schemaVersion": public_schema},
        )
        observed_label = str(observed_schema) if known_schema else "unsupported"
        self.args = (
            "unsupported lifecycle operation schema version: expected schemaVersion "
            f"3.0, observed {observed_label}; use the explicit worktree_legacy_operation bridge",
        )


_CANONICAL_RECORD_KINDS: dict[str, LifecycleOperationKind] = {
    "closeout-operation.json": "closeout",
    "integrate-operation.json": "integrate",
    "direct-landing-operation.json": "direct-landing",
}


def _require_record_matches_canonical_path(
    path: Path,
    record: LifecycleOperationRecord,
) -> None:
    expected_kind = _CANONICAL_RECORD_KINDS.get(path.name)
    if expected_kind is None or record.operationKind == expected_kind:
        return
    raise LifecycleOperationReadError(
        path,
        side="current-record",
        error_type="LifecycleOperationKindMismatch",
        expected={
            "state": "readable",
            "schemaVersion": "3.0",
            "operationKind": expected_kind,
        },
        observed={
            "state": "operation-kind-mismatch",
            "operationKind": record.operationKind,
        },
    )


@contextmanager
def lifecycle_operation_record_access(path: Path):
    """Serialize an isolated raw-record migration with the canonical store owner."""
    with exclusive_access(path, _OWNERSHIP):
        yield


class LifecycleOperationStore:
    """One validated current snapshot per task and operation kind."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> LifecycleOperationRecord | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as error:
            raise LifecycleOperationReadError(
                self.path,
                side="current-record",
                error_type=type(error).__name__,
                expected={"state": "readable", "schemaVersion": "3.0"},
                observed={"state": "unreadable"},
            ) from error
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LifecycleOperationReadError(
                self.path,
                side="current-record",
                error_type=type(error).__name__,
                expected={"state": "readable", "schemaVersion": "3.0"},
                observed={"state": "malformed-json", "sizeBytes": len(raw.encode("utf-8"))},
            ) from error
        if isinstance(payload, dict) and payload.get("schemaVersion") != "3.0":
            raise LifecycleOperationSchemaError(
                self.path,
                payload.get("schemaVersion"),
            )
        try:
            record = LifecycleOperationRecord.model_validate(payload)
        except ValidationError as error:
            raise LifecycleOperationReadError(
                self.path,
                side="current-record",
                error_type=type(error).__name__,
                expected={"state": "readable", "schemaVersion": "3.0"},
                observed={"state": "invalid-schema-3", "sizeBytes": len(raw.encode("utf-8"))},
            ) from error
        _require_record_matches_canonical_path(self.path, record)
        return record

    def observe_current(self) -> LifecycleOperationRecord | None:
        """Read the current generation under the record's sole authority lock."""

        with exclusive_access(self.path, _OWNERSHIP):
            return self.read()

    def update_if_current(
        self,
        observed: LifecycleOperationRecord,
        transform: Callable[[LifecycleOperationRecord], LifecycleOperationRecord],
    ) -> tuple[LifecycleOperationRecord, bool]:
        """Apply a transform only while the complete observed record remains current."""

        with exclusive_access(self.path, _OWNERSHIP):
            current = self.read()
            if current is None:
                raise RuntimeError(f"lifecycle operation record does not exist: {self.path}")
            if current != observed:
                return current, False
            transformed = LifecycleOperationRecord.model_validate(
                transform(current).model_dump(mode="json")
            )
            if transformed == current:
                return current, True
            updated = _advance_record_revision(current, transformed)
            self._validate_transition(current, updated)
            self._write(updated)
            return updated, True

    def create(self, record: LifecycleOperationRecord) -> tuple[LifecycleOperationRecord, bool]:
        # Validate the candidate against this store address even when a current
        # generation already exists. Otherwise a record for another lifecycle
        # plane can be mistaken for a convergent duplicate.
        _require_record_matches_canonical_path(self.path, record)
        if record.recordRevision != 1:
            raise RuntimeError("a new lifecycle operation must begin at record revision 1")
        with exclusive_access(self.path, _OWNERSHIP):
            current = self.read()
            if current is not None:
                return current, False
            self._write(record)
            return record, True

    def update(
        self,
        transform: Callable[[LifecycleOperationRecord], LifecycleOperationRecord],
    ) -> LifecycleOperationRecord:
        with exclusive_access(self.path, _OWNERSHIP):
            current = self.read()
            if current is None:
                raise RuntimeError(f"lifecycle operation record does not exist: {self.path}")
            transformed = LifecycleOperationRecord.model_validate(
                transform(current).model_dump(mode="json")
            )
            if transformed == current:
                return current
            updated = _advance_record_revision(current, transformed)
            self._validate_transition(current, updated)
            self._write(updated)
            return updated

    def resume_generation(
        self,
        transform: Callable[[LifecycleOperationRecord], LifecycleOperationRecord],
        *,
        expected_generation: int,
    ) -> tuple[LifecycleOperationRecord, bool]:
        """Explicitly resume the same generation; never replace its accepted identity."""
        with exclusive_access(self.path, _OWNERSHIP):
            current = self.read()
            if current is None or current.generation != expected_generation:
                if current is None:
                    raise RuntimeError("lifecycle operation generation does not exist")
                return current, False
            transformed = LifecycleOperationRecord.model_validate(
                transform(current).model_dump(mode="json")
            )
            # resume_generation must always revalidate the resume contract (attempt
            # increment exactly once, sanctioned status/phase, disposition identity)
            # BEFORE any no-op short-circuit; a transform that leaves the record
            # unchanged but fails the attempt guard is a contract violation, not an
            # idempotent resume.
            updated = _advance_record_revision(current, transformed)
            _validate_identity_and_evidence_transition(current, updated)
            if current.status in _NON_RESUMABLE:
                raise RuntimeError("terminal lifecycle operation cannot resume its generation")
            if updated.attempt != current.attempt + 1:
                raise RuntimeError("lifecycle operation resume must increment attempt exactly once")
            expected_status = "running" if current.operationKind == "direct-landing" else "queued"
            expected_phase = (
                "direct-preflight"
                if current.operationKind == "direct-landing"
                else "recovering-after-claim"
                if closeout_generation_retained(current)
                else "queued"
            )
            if updated.status != expected_status or updated.phase != expected_phase:
                raise RuntimeError(
                    "lifecycle operation resume must use its sanctioned status and phase"
                )
            if (
                updated.successorFingerprint != current.successorFingerprint
                or updated.generationDisposition != current.generationDisposition
            ):
                raise RuntimeError("lifecycle operation resume cannot change its disposition")
            self._write(updated)
            return updated, True

    def replace_terminal(self, candidate: LifecycleOperationRecord) -> LifecycleOperationRecord:
        """Archive one exact terminal predecessor, then atomically publish N+1."""
        with exclusive_access(self.path, _OWNERSHIP):
            current = self.read()
            if (
                current is not None
                and current.fingerprint == candidate.fingerprint
                and current.predecessorFingerprint
            ):
                return current
            if current is None or current.status not in _TERMINAL:
                raise RuntimeError("an active lifecycle operation cannot be replaced")
            if current.workerPid is not None or (
                current.workerTermination is not None
                and current.workerTermination.state != "exited"
            ):
                raise RuntimeError(
                    "terminal lifecycle generation retains unproven worker authority"
                )
            for field in ("taskId", "taskName", "contractPath", "operationKind"):
                if getattr(candidate, field) != getattr(current, field):
                    raise RuntimeError(f"a sequential lifecycle operation cannot change {field}")
            successor_revision = current.recordRevision + (
                1
                if current.operationKind in {"closeout", "direct-landing"}
                and task_intent_is_missing(current.taskIntent)
                else 2
            )
            validated = LifecycleOperationRecord.model_validate(
                candidate.model_copy(
                    update={
                        "generation": current.generation + 1,
                        "recordRevision": successor_revision,
                        "predecessorFingerprint": current.fingerprint,
                        "attempt": 1,
                    }
                ).model_dump(mode="json")
            )
            if current.operationKind in {"closeout", "direct-landing"} and task_intent_is_missing(
                current.taskIntent
            ):
                return self._retire_missing_intent_generation(current, validated)
            predecessor = LifecycleOperationRecord.model_validate(
                current.model_copy(
                    update={
                        "successorFingerprint": validated.fingerprint,
                        "recordRevision": current.recordRevision + 1,
                    }
                ).model_dump(mode="json")
            )
            self._archive_generation(predecessor)
            self._write(validated)
            return validated

    def _retire_missing_intent_generation(
        self,
        current: LifecycleOperationRecord,
        validated: LifecycleOperationRecord,
    ) -> LifecycleOperationRecord:
        """Preserve exact legacy bytes, then publish one canonical intent-bound successor."""

        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("legacy missing-intent lifecycle bytes are unreadable") from exc
        archive = self.path.with_name(
            f"{self.path.stem}.legacy-missing-intent-generation-{current.generation}.json"
        )
        created_archive = False
        if archive.exists():
            if archive.read_text(encoding="utf-8") != raw:
                raise RuntimeError("legacy missing-intent archive contradicts durable history")
        else:
            atomic_write_text(archive, raw)
            created_archive = True
        try:
            self._write(validated)
        except BaseException:
            if created_archive:
                archive.unlink(missing_ok=True)
            raise
        return validated

    def _archive_generation(self, record: LifecycleOperationRecord) -> None:
        archive = self.path.with_name(f"{self.path.stem}.generation-{record.generation}.json")
        payload = record.model_dump_json(indent=2, exclude_none=True) + "\n"
        if archive.exists():
            if archive.read_text(encoding="utf-8") != payload:
                raise RuntimeError("lifecycle generation archive contradicts durable history")
            return
        atomic_write_text(archive, payload)

    def _write(self, record: LifecycleOperationRecord) -> None:
        _OWNERSHIP.check_declared_writer()
        validated = LifecycleOperationRecord.model_validate(record.model_dump(mode="json"))
        if validated.operationKind in {"closeout", "direct-landing"}:
            try:
                require_task_intent_identity(
                    validated.taskIntent,
                    owner="lifecycle-operation",
                    next_action="retire-and-republish",
                )
            except TaskIntentError as exc:
                raise RuntimeError(f"{exc.status}: {exc.detail}") from exc
        _require_record_matches_canonical_path(self.path, validated)
        require_closeout_recovery_projection(validated)
        require_closeout_finalization_evidence(validated)
        atomic_write_text(
            self.path,
            validated.model_dump_json(indent=2, exclude_none=True) + "\n",
        )

    @staticmethod
    def _validate_transition(
        current: LifecycleOperationRecord, updated: LifecycleOperationRecord
    ) -> None:
        _validate_identity_and_evidence_transition(current, updated)
        if updated.status not in _ALLOWED[current.status]:
            raise RuntimeError(
                f"invalid lifecycle operation transition {current.status} -> {updated.status}"
            )
        if updated.attempt != current.attempt:
            raise RuntimeError("ordinary lifecycle transition cannot change attempt")
        closeout_ambiguous = current.operationKind in {"closeout", "direct-landing"} and any(
            item.state == "mutation-intent" for item in current.mutationEvidence.values()
        )
        if (
            updated.status == "cancelled"
            and closeout_ambiguous
            and updated.cancellationEvidence is None
        ):
            raise RuntimeError("a closeout operation cannot cancel with ambiguous Git intent")
        if (
            updated.status == "cancelled"
            and current.irreversibleBoundaryEntered
            and updated.cancellationEvidence is None
        ):
            raise RuntimeError(
                "an irreversible lifecycle operation needs exact unchanged cancellation evidence"
            )
