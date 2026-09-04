"""Durable, content-addressed owner of one stable diagnostic manifest.

CCR-R13 requires one stable diagnostic manifest per exact candidate that
selects the newest terminal result while retaining immutable predecessor
links, in an isolated namespace that can never overwrite or satisfy the
certifying quality-report manifest.

The store keeps every attempt and terminal result inside one candidate
manifest file.  Each mutation is an atomic read-modify-write with a compare
after the write, so a concurrent publisher either converges on the newest
state or fails closed with a typed CAS refusal.  Earlier results are never
deleted, rewritten, reset to not-requested-optional, or mutated into
certification: they remain immutable records whose digests and chain are
revalidated on every read.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never

from agents_remember.certification.diagnostics.models import (
    DiagnosticAttemptRecord,
    DiagnosticAttemptState,
    DiagnosticRunManifest,
    DiagnosticRunResult,
    DiagnosticRunResultDraft,
)
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import (
    CandidateIdentity,
    CertificationContractFinding,
)
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.atomic_write import atomic_write_bytes

SCHEMA_VERSION = "diagnostic-run-manifest/v1"
_CAS_RETRIES = 5


@dataclass(frozen=True)
class DiagnosticStorePolicy:
    """Isolation and durability policy for one diagnostic manifest store."""

    storeId: str = "diagnostic"
    forbiddenRoots: tuple[Path, ...] = ()
    casRetries: int = _CAS_RETRIES


class DiagnosticManifestStore:
    """One isolated durable namespace of candidate diagnostic manifests."""

    def __init__(self, root: Path, policy: DiagnosticStorePolicy) -> None:
        self._root = root
        self._policy = policy
        self._require_isolated_namespace()

    # ------------------------------------------------------------------ read

    def manifest(self, candidate: CandidateIdentity) -> DiagnosticRunManifest | None:
        """Read and fully revalidate the stable manifest for one candidate."""

        return self._read_manifest(candidate)

    def newest_terminal(self, candidate: CandidateIdentity) -> DiagnosticRunResult | None:
        manifest = self._read_manifest(candidate)
        return manifest.newestTerminal if manifest is not None else None

    def live_attempt(self, candidate: CandidateIdentity) -> DiagnosticAttemptRecord | None:
        manifest = self._read_manifest(candidate)
        if manifest is None:
            return None
        live = tuple(item for item in manifest.attempts if item.state != "terminal")
        if not live:
            return None
        return live[-1]

    def next_attempt_number(self, candidate: CandidateIdentity) -> int:
        manifest = self._read_manifest(candidate)
        return (len(manifest.attempts) + 1) if manifest is not None else 1

    def has_attempts(self, candidate: CandidateIdentity) -> bool:
        manifest = self._read_manifest(candidate)
        return manifest is not None and bool(manifest.attempts)

    # ------------------------------------------------------------------ write

    def reserve(self, attempt: DiagnosticAttemptRecord) -> None:
        """Reserve the next attempt slot; refuse a live in-flight attempt."""

        def transform(current: DiagnosticRunManifest | None) -> DiagnosticRunManifest:
            _require_candidate(current, attempt.candidateIdentity)
            if current is not None and any(item.state != "terminal" for item in current.attempts):
                _raise_store(
                    "diagnostic reservation refused",
                    "diagnostic-already-in-flight",
                    "attempts",
                    "another diagnostic attempt is live for this exact candidate",
                )
            expected = (len(current.attempts) + 1) if current is not None else 1
            if attempt.attemptNumber != expected:
                _raise_store(
                    "diagnostic reservation refused",
                    "diagnostic-attempt-number-mismatch",
                    "attempts",
                    "the reservation must occupy the exact next attempt number",
                )
            attempts = (attempt,) if current is None else (*current.attempts, attempt)
            return _manifest(attempt.candidateIdentity, attempts, current)

        self._update(attempt.candidateIdentity, transform)

    def mark_running(self, attempt: DiagnosticAttemptRecord) -> DiagnosticAttemptRecord:
        """Transition the exact reserved attempt to the running state."""

        def transform(current: DiagnosticRunManifest | None) -> DiagnosticRunManifest:
            current = _require_existing_manifest(current)
            _require_candidate(current, attempt.candidateIdentity)
            updates = tuple(
                _advance_attempt_state(item, attempt, "running") for item in current.attempts
            )
            return _manifest(attempt.candidateIdentity, updates, current)

        self._update(attempt.candidateIdentity, transform)
        manifest = self._read_manifest(attempt.candidateIdentity)
        assert manifest is not None
        matched = tuple(
            item
            for item in manifest.attempts
            if item.attemptNumber == attempt.attemptNumber
            and item.diagnosticNonce == attempt.diagnosticNonce
        )
        if len(matched) != 1 or matched[0].state != "running":
            _raise_store(
                "diagnostic manifest write refused",
                "diagnostic-attempt-not-running",
                f"attempts.{attempt.attemptNumber}",
                "the reserved attempt did not transition to running",
            )
        return matched[0]

    def publish_terminal(
        self,
        attempt: DiagnosticAttemptRecord,
        draft: DiagnosticRunResultDraft,
    ) -> DiagnosticRunResult:
        """Atomically bind the chain identity and publish one terminal result."""

        def transform(current: DiagnosticRunManifest | None) -> DiagnosticRunManifest:
            current = _require_existing_manifest(current)
            _require_candidate(current, attempt.candidateIdentity)
            running = tuple(
                item
                for item in current.attempts
                if item.attemptNumber == attempt.attemptNumber
                and item.diagnosticNonce == attempt.diagnosticNonce
                and item.state == "running"
            )
            if len(running) != 1:
                _raise_store(
                    "diagnostic publication refused",
                    "diagnostic-attempt-not-running",
                    f"attempts.{attempt.attemptNumber}",
                    "only the exact running attempt may publish its terminal result",
                )
            _require_draft_binding(attempt, draft)
            predecessor = current.results[-1].resultId if current.results else ""
            result = _finalize_result(draft, predecessor=predecessor)
            attempts = tuple(
                _advance_attempt_state(item, attempt, "terminal") for item in current.attempts
            )
            return _manifest(
                attempt.candidateIdentity,
                attempts,
                current,
                extra_results=(result,),
            )

        self._update(attempt.candidateIdentity, transform)
        manifest = self._read_manifest(attempt.candidateIdentity)
        assert manifest is not None
        return manifest.results[-1]

    def abandon(self, attempt: DiagnosticAttemptRecord) -> None:
        """Drop a live slot that never started (no scenario step, no result).

        Used only when admission refused after a reservation, so an
        infrastructure refusal leaves no blocking residue.  It can never erase
        or rewrite a terminal result, never fabricates a
        not-requested-optional disposition, and only ever removes the
        newest slot (which is the only one that can still be live).
        """

        def transform(current: DiagnosticRunManifest | None) -> DiagnosticRunManifest:
            current = _require_existing_manifest(current)
            _require_candidate(current, attempt.candidateIdentity)
            matched = tuple(
                item
                for item in current.attempts
                if item.attemptNumber == attempt.attemptNumber
                and item.diagnosticNonce == attempt.diagnosticNonce
                and item.state != "terminal"
            )
            if len(matched) != 1:
                _raise_store(
                    "diagnostic abandonment refused",
                    "diagnostic-attempt-not-abandonable",
                    f"attempts.{attempt.attemptNumber}",
                    "only the exact live attempt may be abandoned before terminalization",
                )
            if current.attempts[-1].attemptNumber != attempt.attemptNumber:
                _raise_store(
                    "diagnostic abandonment refused",
                    "diagnostic-attempt-not-newest",
                    f"attempts.{attempt.attemptNumber}",
                    "only the newest live attempt slot may be abandoned",
                )
            attempts = current.attempts[:-1]
            return _manifest(attempt.candidateIdentity, attempts, current)

        self._update(attempt.candidateIdentity, transform)

    # ------------------------------------------------------------------ internals

    def _update(
        self,
        candidate: CandidateIdentity,
        transform: Callable[[DiagnosticRunManifest | None], DiagnosticRunManifest],
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
            "diagnostic manifest write refused",
            "diagnostic-manifest-cas-collision",
            path.as_posix(),
            "concurrent manifest writers did not converge on one stable state",
        )

    def _read_manifest(self, candidate: CandidateIdentity) -> DiagnosticRunManifest | None:
        path = self._manifest_path(candidate)
        payload = _read_bytes(path)
        if payload is None:
            return None
        try:
            raw = json.loads(payload.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("manifest root must be a JSON object")
            return DiagnosticRunManifest(**raw)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            _raise_store(
                "diagnostic manifest read refused",
                "diagnostic-manifest-corrupt",
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
                    "diagnostic namespace refused",
                    "diagnostic-namespace-collision",
                    root.as_posix(),
                    (
                        "the diagnostic store root overlaps a forbidden certifying "
                        "quality-report manifest root"
                    ),
                )


def _require_candidate(
    current: DiagnosticRunManifest | None,
    candidate: CandidateIdentity,
) -> None:
    if current is not None and current.candidateIdentity != candidate:
        _raise_store(
            "diagnostic manifest write refused",
            "diagnostic-manifest-candidate-mismatch",
            "candidateIdentity",
            "the manifest file belongs to a different exact candidate",
        )


def _require_draft_binding(
    attempt: DiagnosticAttemptRecord,
    draft: DiagnosticRunResultDraft,
) -> None:
    expected = (
        attempt.candidateIdentity,
        attempt.diagnosticNonce,
        attempt.registryDigest,
        attempt.certifyingPlanDigest,
        attempt.diagnosticPlanDigest,
        attempt.planVersion,
        attempt.gate,
        attempt.profileId,
    )
    observed = (
        draft.candidateIdentity,
        draft.diagnosticNonce,
        draft.registryDigest,
        draft.certifyingPlanDigest,
        draft.diagnosticPlanDigest,
        draft.planVersion,
        draft.gate,
        draft.profileId,
    )
    if observed != expected:
        _raise_store(
            "diagnostic publication refused",
            "diagnostic-draft-attempt-mismatch",
            "result",
            "the terminal draft must bind the exact attempt reservation identity",
        )


def _advance_attempt_state(
    item: DiagnosticAttemptRecord,
    attempt: DiagnosticAttemptRecord,
    state: Literal["running", "terminal"],
) -> DiagnosticAttemptRecord:
    if item.attemptNumber == attempt.attemptNumber:
        if item.diagnosticNonce != attempt.diagnosticNonce:
            _raise_store(
                "diagnostic manifest write refused",
                "diagnostic-attempt-nonce-mismatch",
                f"attempts.{item.attemptNumber}",
                "the live attempt slot binds a different diagnostic nonce",
            )
        if item.state == state or _allowed_transition(item.state, state):
            updated = item.model_copy(update={"state": state})
            payload = updated.model_dump(mode="json", exclude={"attemptDigest"})
            return updated.model_copy(update={"attemptDigest": content_digest(payload)})
        _raise_store(
            "diagnostic manifest write refused",
            "diagnostic-attempt-state-transition",
            f"attempts.{item.attemptNumber}",
            f"attempt state cannot move from {item.state!r} to {state!r}",
        )
    return item


def _allowed_transition(
    current: DiagnosticAttemptState,
    target: DiagnosticAttemptState,
) -> bool:
    return (current, target) in {("reserved", "running"), ("running", "terminal")}


def _finalize_result(
    draft: DiagnosticRunResultDraft,
    *,
    predecessor: str,
) -> DiagnosticRunResult:
    payload = {**draft.model_dump(mode="json"), "predecessorDigest": predecessor}
    result_id = content_digest(payload)
    final_payload = {**payload, "resultId": result_id}
    return DiagnosticRunResult(
        **final_payload,
        resultDigest=content_digest(final_payload),
    )


def _require_existing_manifest(
    current: DiagnosticRunManifest | None,
) -> DiagnosticRunManifest:
    if current is None:
        _raise_store(
            "diagnostic manifest write refused",
            "diagnostic-manifest-missing",
            "manifest",
            "a live attempt requires an existing stable manifest",
        )
    return current


def _manifest(
    candidate: CandidateIdentity,
    attempts: Iterable[DiagnosticAttemptRecord],
    current: DiagnosticRunManifest | None,
    *,
    extra_results: tuple[DiagnosticRunResult, ...] = (),
) -> DiagnosticRunManifest:
    results = current.results if current is not None else ()
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "candidateIdentity": candidate.model_dump(mode="json"),
        "attempts": [item.model_dump(mode="json") for item in attempts],
        "results": [item.model_dump(mode="json") for item in (*results, *extra_results)],
    }
    return DiagnosticRunManifest(**payload, manifestDigest=content_digest(payload))


def _canonical_bytes(manifest: DiagnosticRunManifest) -> bytes:
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
    "DiagnosticManifestStore",
    "DiagnosticStorePolicy",
]
