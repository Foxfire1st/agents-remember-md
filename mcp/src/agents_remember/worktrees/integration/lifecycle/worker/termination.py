"""Platform-owned worker identity, signal, and exit-proof boundary."""

from __future__ import annotations

import hashlib
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.models.lifecycles.termination import WorkerTerminationEvidence
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)


def require_linux_worker_runtime() -> None:
    """Refuse a Linux worker launch before an incompatible runtime can strand it."""

    if sys.platform != "linux":
        return
    if not callable(getattr(os, "pidfd_open", None)) or not callable(
        getattr(signal, "pidfd_send_signal", None)
    ):
        raise RuntimeError(
            "Linux lifecycle workers require native os.pidfd_open and "
            "signal.pidfd_send_signal; recreate mcp/.venv with "
            "scripts/bootstrap-mcp-venv.sh --replace"
        )


def worker_process_fingerprint(pid: int) -> str | None:
    """Identify one Linux process instance, not merely a reusable numeric PID."""

    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        end = stat.rfind(")")
        fields = stat[end + 2 :].split()
        process_group = fields[2]
        start_ticks = fields[19]
    except (IndexError, OSError):
        return None
    if not boot_id or process_group != str(pid):
        return None
    payload = f"{boot_id}\0{pid}\0{process_group}\0{start_ticks}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def signal_worker_and_prove_exit(
    request: WorkerTerminationEvidence,
    *,
    timeout_seconds: float = 5.0,
    poll_seconds: float = 0.05,
) -> WorkerTerminationEvidence:
    """Request SIGTERM and return exit proof or retained termination authority."""

    if request.state not in {"requested", "termination-required"}:
        raise RuntimeError("worker signal requires durable unproven termination intent")
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        return request.model_copy(
            update={
                "state": "termination-required",
                "detail": "platform cannot signal the exact worker through a pidfd",
                "failureEvidence": _worker_failure("UnsupportedOperation"),
            }
        )
    deadline = time.monotonic() + timeout_seconds
    while True:
        state, members = _worker_group_state(request)
        if state == "exited":
            return _exited_worker_outcome(request)
        if state == "ambiguous":
            return _termination_required(
                request,
                "worker identity changed while its numeric process group is occupied",
                evidence=_worker_failure("WorkerIdentityMismatch"),
            )
        refusal = _signal_exact_members(
            request,
            members,
            pidfd_open=pidfd_open,
            pidfd_send_signal=pidfd_send_signal,
        )
        if refusal is not None:
            return _termination_required(
                request,
                "the exact worker process group could not be signalled",
                evidence=refusal,
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_seconds)
    return _termination_required(
        request,
        "worker process group did not exit before the bounded termination timeout",
        evidence=_worker_failure("TimeoutError"),
    )


def _observed_worker_outcome(
    request: WorkerTerminationEvidence,
) -> WorkerTerminationEvidence | None:
    state, _members = _worker_group_state(request)
    if state == "owned":
        return request.model_copy(
            update={
                "state": "termination-required",
                "detail": "exact worker process group is still live",
                "failureEvidence": _worker_failure("WorkerStillRunning"),
            }
        )
    if state == "ambiguous":
        return _termination_required(
            request,
            "worker identity changed while its numeric process group is occupied",
            evidence=_worker_failure("WorkerIdentityMismatch"),
        )
    return _exited_worker_outcome(request)


def _worker_group_state(
    request: WorkerTerminationEvidence,
) -> tuple[str, list[int]]:
    observed = worker_process_fingerprint(request.pid)
    members = _process_group_members(request.pid)
    if observed == request.processFingerprint:
        return "owned", members
    if observed is None and members:
        return "owned", members
    if observed is None:
        return "exited", []
    return "ambiguous", members


def _signal_exact_members(
    request: WorkerTerminationEvidence,
    members: list[int],
    *,
    pidfd_open,
    pidfd_send_signal,
) -> dict[str, object] | None:
    for pid in members:
        try:
            pidfd = pidfd_open(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            return _worker_failure("PermissionError")
        try:
            if _process_group_id(pid) != request.pid:
                continue
            if pid == request.pid and (
                worker_process_fingerprint(pid) != request.processFingerprint
            ):
                return _worker_failure("WorkerIdentityMismatch")
            try:
                pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                return _worker_failure("PermissionError")
        finally:
            os.close(pidfd)
    return None


def _process_group_id(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        end = stat.rfind(")")
        fields = stat[end + 2 :].split()
        return int(fields[2])
    except (IndexError, OSError, ValueError):
        return None


def _termination_required(
    request: WorkerTerminationEvidence,
    detail: str,
    *,
    evidence: dict[str, object] | None = None,
) -> WorkerTerminationEvidence:
    return request.model_copy(
        update={
            "state": "termination-required",
            "detail": detail,
            "failureEvidence": evidence,
        }
    )


def _exited_worker_outcome(
    request: WorkerTerminationEvidence,
) -> WorkerTerminationEvidence:
    return request.model_copy(
        update={
            "state": "exited",
            "observedAt": _stamp(),
            "detail": "exact worker and process group exited",
            "failureEvidence": None,
        }
    )


def _worker_failure(error_type: str) -> dict[str, object]:
    return public_failure_evidence(
        stage="worker-termination",
        side="process-group",
        name="lifecycle-worker",
        error_type=error_type,
        expected={"state": "exited"},
        observed={"state": "termination-required"},
    )


def public_worker_termination_evidence(
    evidence: WorkerTerminationEvidence,
) -> dict[str, object]:
    """Bound public worker facts while the private record retains exact process authority."""

    identity = f"{evidence.pid}\0{evidence.lease}\0{evidence.processFingerprint}"
    return {
        "state": evidence.state,
        "signal": evidence.signal,
        "requestedAt": evidence.requestedAt,
        "observedAt": evidence.observedAt,
        "workerIdentitySha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "detail": (
            "exact worker exit is proven"
            if evidence.state == "exited"
            else "exact worker exit remains unproven"
        ),
        "failureEvidence": evidence.failureEvidence,
    }


def worker_termination_required_result(
    record: LifecycleOperationRecord,
) -> dict[str, object] | None:
    """Project current process authority before any historical operation handoff."""

    termination = record.workerTermination
    if termination is None:
        # A retained exact worker binding is ordinary live authority until a real
        # cancellation/termination transition records termination evidence.
        return None
    if termination.state == "exited" and record.status != "termination-required":
        return None
    observed = public_worker_termination_evidence(termination)
    surface = (
        "Exact worker exit remains unproven; use the advertised task-addressed cancel "
        "action to advance termination authority."
    )
    return {
        "state": "worker-termination-required",
        "reason": surface,
        "summary": surface,
        "expected": {"state": "exited", "workerAuthority": "retained-until-proof"},
        "observed": observed,
        "nextAction": "cancel",
    }


def worker_exit_unproven(record: LifecycleOperationRecord) -> bool:
    """Whether exact process authority remains retained pending exit proof."""

    termination = record.workerTermination
    return record.workerPid is not None or (
        termination is not None and termination.state != "exited"
    )


def bounded_worker_termination_outcome(
    evidence: WorkerTerminationEvidence,
) -> WorkerTerminationEvidence:
    """Keep exact authority while bounding a signal provider's durable detail."""

    if evidence.state == "exited":
        return evidence.model_copy(
            update={"detail": "exact worker and process group exited", "failureEvidence": None}
        )
    return evidence.model_copy(
        update={
            "detail": "exact worker exit remains unproven",
            "failureEvidence": evidence.failureEvidence
            or _worker_failure("WorkerTerminationRefused"),
        }
    )


def observe_worker_termination(
    request: WorkerTerminationEvidence,
) -> WorkerTerminationEvidence | None:
    """Project exact worker/group exit without signaling or mutating authority."""
    return _observed_worker_outcome(request)


def _process_group_members(process_group: int) -> list[int]:
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
            end = stat.rfind(")")
            fields = stat[end + 2 :].split()
            if int(fields[2]) == process_group:
                members.append(int(entry.name))
        except (IndexError, OSError, ValueError):
            continue
    return sorted(members)


def worker_termination_request(record: LifecycleOperationRecord) -> WorkerTerminationEvidence:
    """Build the exact intent callers must persist before platform signaling."""
    pid = record.workerPid
    lease = record.workerLease
    fingerprint = record.workerProcessFingerprint
    if pid is None or lease is None or fingerprint is None:
        raise RuntimeError("worker termination requires an exact pid and lease binding")
    return WorkerTerminationEvidence(
        state="requested",
        pid=pid,
        lease=lease,
        processFingerprint=fingerprint,
        requestedAt=_stamp(),
    )


def _stamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
