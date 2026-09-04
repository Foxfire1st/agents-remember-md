"""Durable, content-addressed owner of one two-repetition Gate-4 run.

CCR-R14@v3 requires one stable certifying run per exact candidate that holds
exactly two fresh no-retry certifying repetitions in fixed slot order.  The
store keeps every attempt and repetition result inside one candidate manifest
file.  Each mutation is an atomic read-modify-write with a compare after the
write, so a concurrent publisher either converges on the newest state or fails
closed with a typed CAS refusal.

Rules encoded here:

* retry is disabled: a live (reserved or running) attempt refuses any second
  admission, and a terminal attempt that binds the same plan identity refuses
  a successor attempt for the same candidate (a code/config/runtime repair
  changes the plan identity, and a code repair also changes the candidate);
* repetition results are immutable and occupy the exact ordered slots one and
  two; no slot can be rewritten, reordered, or promoted, and a crash can only
  resume publishing the remaining slot against the frozen reservation;
* the attempt transitions to terminal only when both fresh repetitions have
  published, so a partial run can never be converted into a pass;
* earlier results are never deleted, reset, or mutated into a different
  disposition.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Never

from agents_remember.certification.digests import content_digest
from agents_remember.certification.final_codex.models import (
    REPETITION_COUNT,
    FinalCodexAttemptRecord,
    FinalCodexAttemptState,
    FinalCodexRepetitionResult,
    FinalCodexRepetitionResultDraft,
    FinalCodexRunManifest,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    CertificationContractFinding,
)
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.atomic_write import atomic_write_bytes

SCHEMA_VERSION = "final-codex-run-manifest/v1"
_CAS_RETRIES = 5


@dataclass(frozen=True)
class FinalCodexStorePolicy:
    """Isolation and durability policy for one final-codex manifest store."""

    storeId: str = "final-codex"
    forbiddenRoots: tuple[Path, ...] = ()
    casRetries: int = _CAS_RETRIES


class FinalCodexManifestStore:
    """One isolated durable namespace of candidate final-codex run manifests."""

    def __init__(self, root: Path, policy: FinalCodexStorePolicy) -> None:
        self._root = root
        self._policy = policy
        self._require_isolated_namespace()

    # ------------------------------------------------------------------ read

    def manifest(self, candidate: CandidateIdentity) -> FinalCodexRunManifest | None:
        """Read and fully revalidate the stable run manifest for one candidate."""

        return self._read_manifest(candidate)

    def live_attempt(self, candidate: CandidateIdentity) -> FinalCodexAttemptRecord | None:
        manifest = self._read_manifest(candidate)
        if manifest is None:
            return None
        if manifest.attempt.state == "terminal":
            return None
        return manifest.attempt

    def attempt_number(self, candidate: CandidateIdentity) -> int:
        manifest = self._read_manifest(candidate)
        if manifest is None:
            return 1
        if manifest.attempt.state == "terminal":
            return manifest.attempt.attemptNumber + 1
        return manifest.attempt.attemptNumber

    # ------------------------------------------------------------------ write

    def reserve(self, attempt: FinalCodexAttemptRecord) -> None:
        """Reserve one two-repetition run; refuse a live or same-plan retry."""

        def transform(current: FinalCodexRunManifest | None) -> FinalCodexRunManifest:
            _require_candidate(current, attempt.candidateIdentity)
            if current is not None:
                if current.attempt.state != "terminal":
                    _raise_store(
                        "final-codex reservation refused",
                        "final-codex-already-in-flight",
                        "attempts",
                        "another final-codex run is live for this exact candidate",
                    )
                if _same_plan_identity(current.attempt, attempt):
                    _raise_store(
                        "final-codex reservation refused",
                        "final-codex-retry-disabled",
                        "attempts",
                        (
                            "a certifying repetition is never retried: the exact plan identity "
                            "already has a terminal run for this candidate"
                        ),
                    )
                if attempt.attemptNumber != current.attempt.attemptNumber + 1:
                    _raise_store(
                        "final-codex reservation refused",
                        "final-codex-attempt-number-mismatch",
                        "attempts",
                        "a successor attempt must occupy the exact next attempt number",
                    )
            elif attempt.attemptNumber != 1:
                _raise_store(
                    "final-codex reservation refused",
                    "final-codex-attempt-number-mismatch",
                    "attempts",
                    "the first attempt must occupy attempt number one",
                )
            manifest = _manifest(attempt.candidateIdentity, attempt, ())
            return manifest

        self._update(attempt.candidateIdentity, transform)

    def mark_running(self, attempt: FinalCodexAttemptRecord) -> FinalCodexAttemptRecord:
        """Transition the exact reserved attempt to the running state."""

        def transform(current: FinalCodexRunManifest | None) -> FinalCodexRunManifest:
            current = _require_existing_manifest(current)
            _require_candidate(current, attempt.candidateIdentity)
            return _manifest(
                attempt.candidateIdentity,
                _advance_attempt_state(current.attempt, attempt, "running"),
                current.repetitions,
            )

        self._update(attempt.candidateIdentity, transform)
        manifest = self._read_manifest(attempt.candidateIdentity)
        assert manifest is not None
        if manifest.attempt.state != "running":
            _raise_store(
                "final-codex manifest write refused",
                "final-codex-attempt-not-running",
                f"attempts.{attempt.attemptNumber}",
                "the reserved attempt did not transition to running",
            )
        return manifest.attempt

    def publish_repetition(
        self,
        attempt: FinalCodexAttemptRecord,
        draft: FinalCodexRepetitionResultDraft,
    ) -> FinalCodexRepetitionResult:
        """Atomically bind the chain identity and publish one repetition slot.

        Refuses a slot that is already published, a draft that does not bind
        the exact running attempt, an out-of-order slot, and a draft whose
        fresh identity differs from the reservation.  Publishing the second
        slot terminalizes the attempt.
        """

        def transform(current: FinalCodexRunManifest | None) -> FinalCodexRunManifest:
            current = _require_existing_manifest(current)
            _require_candidate(current, attempt.candidateIdentity)
            if current.attempt.state != "running":
                _raise_store(
                    "final-codex publication refused",
                    "final-codex-attempt-not-running",
                    f"attempts.{attempt.attemptNumber}",
                    "only the exact running attempt may publish repetition results",
                )
            _require_draft_binding(current.attempt, draft)
            published = {item.repetitionNumber for item in current.repetitions}
            if draft.repetitionNumber in published:
                _raise_store(
                    "final-codex publication refused",
                    "final-codex-repetition-already-published",
                    f"repetitions.{draft.repetitionNumber}",
                    "a certifying repetition slot is never rewritten",
                )
            if draft.repetitionNumber != len(current.repetitions) + 1:
                _raise_store(
                    "final-codex publication refused",
                    "final-codex-repetition-out-of-order",
                    f"repetitions.{draft.repetitionNumber}",
                    "certifying repetitions must publish in exact fixed slot order",
                )
            repetitions = (*current.repetitions, _finalize_result(draft))
            state: FinalCodexAttemptState = (
                "terminal" if len(repetitions) == REPETITION_COUNT else "running"
            )
            updated_attempt = _advance_attempt_state(current.attempt, attempt, state)
            return _manifest(attempt.candidateIdentity, updated_attempt, repetitions)

        self._update(attempt.candidateIdentity, transform)
        manifest = self._read_manifest(attempt.candidateIdentity)
        assert manifest is not None
        return manifest.repetitions[-1]

    # ------------------------------------------------------------------ internals

    def _update(
        self,
        candidate: CandidateIdentity,
        transform: Callable[[FinalCodexRunManifest | None], FinalCodexRunManifest],
    ) -> None:
        path = self._manifest_path(candidate)
        for _ in range(max(1, self._policy.casRetries)):
            current = self._read_manifest(candidate)
            updated = transform(current)
            payload = _canonical_bytes(updated)
            atomic_write_bytes(path, payload)
            if _read_bytes(path) == payload:
                return
        _raise_store(
            "final-codex manifest write refused",
            "final-codex-manifest-cas-collision",
            path.as_posix(),
            "concurrent manifest writers did not converge on one stable state",
        )

    def _read_manifest(self, candidate: CandidateIdentity) -> FinalCodexRunManifest | None:
        path = self._manifest_path(candidate)
        payload = _read_bytes(path)
        if payload is None:
            return None
        try:
            raw = json.loads(payload.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("manifest root must be a JSON object")
            return FinalCodexRunManifest(**raw)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            _raise_store(
                "final-codex manifest read refused",
                "final-codex-manifest-corrupt",
                path.as_posix(),
                f"stored manifest failed closed revalidation: {error}",
            )

    def _manifest_path(self, candidate: CandidateIdentity) -> Path:
        return self._candidate_dir(candidate) / "manifest.json"

    def _candidate_dir(self, candidate: CandidateIdentity) -> Path:
        key = content_digest({"kind": candidate.kind, "value": candidate.value})
        return self._root / key

    def _require_isolated_namespace(self) -> None:
        root = self._root.resolve()
        for forbidden in self._policy.forbiddenRoots:
            if not forbidden:
                continue
            resolved = Path(forbidden).resolve()
            if root == resolved or root.is_relative_to(resolved) or resolved.is_relative_to(root):
                _raise_store(
                    "final-codex namespace refused",
                    "final-codex-namespace-collision",
                    root.as_posix(),
                    (
                        "the final-codex store root overlaps a forbidden certifying or "
                        "diagnostic quality-report manifest root"
                    ),
                )


def _require_candidate(
    current: FinalCodexRunManifest | None,
    candidate: CandidateIdentity,
) -> None:
    if current is not None and current.candidateIdentity != candidate:
        _raise_store(
            "final-codex manifest write refused",
            "final-codex-manifest-candidate-mismatch",
            "candidateIdentity",
            "the manifest file belongs to a different exact candidate",
        )


def _same_plan_identity(
    terminal: FinalCodexAttemptRecord,
    attempt: FinalCodexAttemptRecord,
) -> bool:
    return (
        terminal.registryDigest == attempt.registryDigest
        and terminal.certifyingPlanDigest == attempt.certifyingPlanDigest
        and terminal.gatePlanDigest == attempt.gatePlanDigest
        and terminal.scenarioVersion == attempt.scenarioVersion
        and terminal.planVersion == attempt.planVersion
        and terminal.gate == attempt.gate
        and terminal.profileId == attempt.profileId
    )


def _require_draft_binding(
    attempt: FinalCodexAttemptRecord,
    draft: FinalCodexRepetitionResultDraft,
) -> None:
    expected = (
        attempt.candidateIdentity,
        attempt.registryDigest,
        attempt.certifyingPlanDigest,
        attempt.gatePlanDigest,
        attempt.scenarioVersion,
        attempt.planVersion,
        attempt.gate,
        attempt.profileId,
    )
    observed = (
        draft.candidateIdentity,
        draft.registryDigest,
        draft.certifyingPlanDigest,
        draft.gatePlanDigest,
        draft.scenarioVersion,
        draft.planVersion,
        draft.gate,
        draft.profileId,
    )
    if observed != expected:
        _raise_store(
            "final-codex publication refused",
            "final-codex-draft-attempt-mismatch",
            "result",
            "the terminal draft must bind the exact attempt reservation identity",
        )
    reserved_keys = tuple(item.key for item in attempt.repetitionIdentities)
    if draft.repetitionIdentity.key != reserved_keys[draft.repetitionNumber - 1]:
        _raise_store(
            "final-codex publication refused",
            "final-codex-draft-identity-mismatch",
            f"repetitions.{draft.repetitionNumber}.repetitionIdentity",
            "the draft must bind the reserved fresh identity for its exact slot",
        )


def _advance_attempt_state(
    item: FinalCodexAttemptRecord,
    attempt: FinalCodexAttemptRecord,
    state: FinalCodexAttemptState,
) -> FinalCodexAttemptRecord:
    if item.attemptNumber != attempt.attemptNumber:
        _raise_store(
            "final-codex manifest write refused",
            "final-codex-attempt-number-mismatch",
            f"attempts.{item.attemptNumber}",
            "the manifest attempt does not match the reserved attempt number",
        )
    if item.state == state or _allowed_transition(item.state, state):
        updated = item.model_copy(update={"state": state})
        payload = updated.model_dump(mode="json", exclude={"attemptDigest"})
        return updated.model_copy(update={"attemptDigest": content_digest(payload)})
    _raise_store(
        "final-codex manifest write refused",
        "final-codex-attempt-state-transition",
        f"attempts.{item.attemptNumber}",
        f"attempt state cannot move from {item.state!r} to {state!r}",
    )


def _allowed_transition(
    current: FinalCodexAttemptState,
    target: FinalCodexAttemptState,
) -> bool:
    return (current, target) in {("reserved", "running"), ("running", "terminal")}


def _finalize_result(draft: FinalCodexRepetitionResultDraft) -> FinalCodexRepetitionResult:
    payload = {**draft.model_dump(mode="json"), "predecessorDigest": ""}
    result_id = content_digest(payload)
    final_payload = {**payload, "resultId": result_id}
    return FinalCodexRepetitionResult(
        **final_payload,
        resultDigest=content_digest(final_payload),
    )


def _require_existing_manifest(
    current: FinalCodexRunManifest | None,
) -> FinalCodexRunManifest:
    if current is None:
        _raise_store(
            "final-codex manifest write refused",
            "final-codex-manifest-missing",
            "manifest",
            "a live attempt requires an existing stable run manifest",
        )
    return current


def _manifest(
    candidate: CandidateIdentity,
    attempt: FinalCodexAttemptRecord,
    repetitions: Iterable[FinalCodexRepetitionResult],
) -> FinalCodexRunManifest:
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "candidateIdentity": candidate.model_dump(mode="json"),
        "attempt": attempt.model_dump(mode="json"),
        "repetitions": [item.model_dump(mode="json") for item in repetitions],
    }
    return FinalCodexRunManifest(**payload, manifestDigest=content_digest(payload))


def _canonical_bytes(manifest: FinalCodexRunManifest) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _raise_store(detail: str, code: str, path: str, message: str) -> Never:
    finding = CertificationContractFinding(code=code, path=path, detail=message)
    raise CertificationContractError(
        f"{detail}: {message}",
        (finding.model_dump(mode="json"),),
    )


__all__ = [
    "FinalCodexManifestStore",
    "FinalCodexStorePolicy",
]
