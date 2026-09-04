"""Typed error family for Agents Remember.

Every domain error subclasses :class:`AgentsRememberError`, which itself
subclasses :class:`ValueError` so existing ``except ValueError`` handlers and
the FastMCP error surface keep working unchanged. New code should raise (and
catch) the typed members of this family rather than bare ``ValueError`` /
``RuntimeError`` so the public surface has one coherent error contract.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal


class AgentsRememberError(ValueError):
    """Base class for all Agents Remember domain errors."""


class CertificationContractError(AgentsRememberError):
    """A rail registry, plan, or result manifest failed closed validation."""

    def __init__(self, detail: str, findings: Sequence[Mapping[str, object]]) -> None:
        self._findings = tuple(_freeze_contract_finding(finding) for finding in findings)
        super().__init__(detail)

    @property
    def findings(self) -> tuple[Mapping[str, object], ...]:
        return self._findings


class CertificationProfileError(CertificationContractError):
    """One repository profile authority failed before any certification command."""

    status = "certification-profile-invalid"


class CertificationExecutorPrerequisiteError(CertificationContractError):
    """An admitted repository executor cannot start its affected gate population."""

    status = "certification-executor-prerequisite-failed"


class CloseoutReadinessContractError(CertificationContractError):
    """One closeout readiness projection contradicted its exact authorities."""

    status = "closeout-readiness-contract-failed"


class DaggerRuntimeAuthorityError(CertificationContractError):
    """The host-level shared Dagger runner/layer-store authority refused admission.

    Raised before any Dagger command starts when the declared authority is missing,
    malformed, unsupported, provisioning-capable, worktree-local, ambiguous, not
    inspectable as a live engine with the exact layer store mounted, contradicted by
    ambient or per-worktree configuration, or blocked by an active authority-transition
    barrier with live owners. The findings carry the typed defect code plus both
    authority digests and the live-owner census where relevant.
    """

    status = "dagger-runtime-authority-invalid"


def _freeze_contract_finding(finding: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze_contract_value(value) for key, value in finding.items()})


def _freeze_contract_value(value: object) -> object:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("certification finding mapping keys must be strings")
        return MappingProxyType({key: _freeze_contract_value(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_contract_value(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return value
    raise TypeError("certification finding values must be immutable JSON-like data")


class TaskIntentError(AgentsRememberError):
    """Normative task intent is unavailable, invalid, stale, or unsupported."""

    def __init__(self, status: str, detail: str, *, next_action: str = "task_doc") -> None:
        self.status = status
        self.detail = detail
        self.next_action = next_action
        super().__init__(detail)


class SeatOccupancyError(AgentsRememberError):
    """A canonical document-and-role seat has multiple claimants in one generation."""


class StructuralDispatchError(AgentsRememberError):
    """Durable dispatch evidence is ambiguous or contradicts the current seat."""


class StructuralDispatchLockError(AgentsRememberError):
    """The canonical-seat serializer could not establish cross-process exclusion."""


class StructuralRoutingError(AgentsRememberError):
    """A structural route is absent or ambiguous; routing must fail instead of guessing."""


class AuthorityError(AgentsRememberError):
    """A path or repo argument violated the MCP authority settings.

    Raised when a caller names a repo that settings do not allow, or passes a
    path that escapes the coordinator root. Centralizing this means every
    application entry point reports the same boundary violation the same way.
    """


class ConfiguredContractAuthorityError(AgentsRememberError):
    """A readable task contract contradicts configured repository authority.

    The exception identifies the bounded authority cell that failed without
    carrying repository paths or lower backend diagnostics into public results.
    """

    def __init__(self, *, side: str, name: str) -> None:
        self.side = side
        self.name = name
        super().__init__("configured contract authority does not match")


ConfiguredContractRereadReason = Literal[
    "location-invalid",
    "contract-unreadable",
    "authority-invalid",
]


class ConfiguredContractRereadError(AgentsRememberError):
    """A mutation boundary could not re-prove current configured-contract truth.

    The value contains only the finite semantic classification and bounded
    public evidence assembled by the domain authority owner. The lower
    exception remains available only through ordinary exception chaining.
    """

    def __init__(
        self,
        *,
        reason: ConfiguredContractRereadReason,
        status: str,
        detail: str,
        expected: Mapping[str, object],
        observed: Mapping[str, object],
    ) -> None:
        self.reason = reason
        self.status = status
        self.detail = detail
        self.expected = dict(expected)
        self.observed = dict(observed)
        super().__init__(detail)


class RouteIndexCensusError(AgentsRememberError):
    """A validated repository could not provide one authoritative source census."""


class FutureCodeCandidateError(AgentsRememberError):
    """A future-code candidate could not be captured or is no longer current."""

    def __init__(self, status: str, detail: str) -> None:
        self.status = status
        super().__init__(detail)


class CuratorCoherenceError(AgentsRememberError):
    """The leaf's structured curator-coherence authority is absent, stale, or invalid."""

    def __init__(
        self,
        status: str,
        detail: str,
        *,
        expected: Mapping[str, object] | None = None,
        observed: Mapping[str, object] | None = None,
        next_action: str = "curator_coherence",
    ) -> None:
        self.status = status
        self.detail = detail
        self.expected = dict(expected or {})
        self.observed = dict(observed or {})
        self.next_action = next_action
        super().__init__(detail)

    def response_fields(self) -> dict[str, object]:
        """Project the bounded refusal facts without duplicating error-family logic."""

        result: dict[str, object] = {
            "status": self.status,
            "detail": self.detail,
            "nextAction": self.next_action,
        }
        if self.expected:
            result["expected"] = self.expected
        if self.observed:
            result["observed"] = self.observed
        return result


@dataclass(frozen=True)
class MemoryCandidatePairFailure:
    """Bounded public context for one exact-pair refusal."""

    field: str
    contract_path: str
    expected: Mapping[str, object] | None = None
    observed: Mapping[str, object] | None = None
    next_action: str = "developer-decision"
    next_args: Mapping[str, object] | None = None


class MemoryCandidatePairError(AgentsRememberError):
    """An exact contract-owned code/memory pair is absent, stale, or contradictory."""

    def __init__(
        self,
        status: str,
        detail: str,
        *,
        failure: MemoryCandidatePairFailure,
    ) -> None:
        self.status = status
        self.field = failure.field
        self.detail = detail
        self.contract_path = failure.contract_path
        self.expected = dict(failure.expected or {})
        self.observed = dict(failure.observed or {})
        self.next_action = failure.next_action
        self.next_args = dict(failure.next_args or {})
        super().__init__(detail)

    def response_fields(self) -> dict[str, object]:
        """Project the one canonical public shape for an exact-pair refusal."""

        result: dict[str, object] = {
            "pairStatus": self.status,
            "pairField": self.field,
            "contractPath": self.contract_path,
            "detail": self.detail,
            "nextAction": self.next_action,
        }
        if self.expected:
            result["expected"] = self.expected
        if self.observed:
            result["observed"] = self.observed
        if self.next_args:
            result["nextArgs"] = self.next_args
        return result


class CuratorCoherencePairError(CuratorCoherenceError):
    """A curator-coherence refusal caused by the shared exact-pair validator."""

    def __init__(self, error: MemoryCandidatePairError) -> None:
        self.pair_error = error
        super().__init__(
            error.status,
            error.detail,
            expected=error.expected,
            observed=error.observed,
            next_action=error.next_action,
        )

    def response_fields(self) -> dict[str, object]:
        result = {
            **super().response_fields(),
            "pairField": self.pair_error.field,
        }
        if self.pair_error.next_args:
            result["nextArgs"] = self.pair_error.next_args
        return result


class CitationCacheError(AgentsRememberError):
    """A citation cache authority, capacity, or lifecycle lease was invalid."""


class ConversationCompositionError(AgentsRememberError):
    """The app-scoped conversation runtime composition contract was violated.

    Raised when the one immutable runtime authority is retrieved before
    installation, installed a second time, replaced by a foreign object, or
    constructed without a required authority. These are composition bugs that
    must fail at startup or request entry, never silently at first use.
    """


class TokenizerVocabularyError(AgentsRememberError):
    """The tiktoken vocabulary a token counter needs is not the one vendored here.

    Raised instead of letting tiktoken download the vocabulary it cannot find. The
    counter is built while the MCP tool surface is still importing, so a download there
    is a network round trip on the server's startup path -- the thing that made a cold
    container, an offline machine and a hermetic CI job unable to start the server.
    A build that failed to ship the file must say so, not work only where egress exists.
    """


class GrammarUnavailableError(AgentsRememberError):
    """A language the citation check declares it parses has no grammar to parse it with.

    Raised instead of falling back to occurrence matching or to a second parser. The
    check's whole value is telling a DEFINITION from a MENTION, and a run that quietly
    lost that for one language would report the same "ok" over a scope nobody stated.
    The grammars are ordinary wheels pinned in ``mcp/pyproject.toml``; nothing about the
    parse path reaches the network, so this means an incomplete install.
    """


class HarnessControlError(AgentsRememberError):
    """The hosted harness control contract or exact-session identity was violated."""


class HarnessControlClientError(HarnessControlError):
    """The serving process lost an exact-session IPC request before or after its first byte.

    ``may_have_sent`` is retry-safety evidence: callers may retry a pre-write failure, while a
    post-write failure must remain ``unknown`` until the same request id is reconciled.
    """

    def __init__(self, detail: str, *, may_have_sent: bool) -> None:
        super().__init__(detail)
        self.may_have_sent = may_have_sent


class HarnessAdapterDisconnectedError(HarnessControlError):
    """A protocol adapter disconnected before or after a prompt might have been sent."""

    def __init__(
        self,
        detail: str,
        *,
        may_have_sent: bool,
        vendor_correlation_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.may_have_sent = may_have_sent
        self.vendor_correlation_id = vendor_correlation_id


class HarnessAdapterBusyError(HarnessControlError):
    """An adapter's final write-boundary guard proved that zero operation bytes were sent.

    The common submission authority may move ``dispatching`` back to ``queued`` only for this
    typed error.  A generic exception cannot safely make that claim because vendor bytes might
    already have crossed the transport.
    """

    def __init__(self, detail: str, *, may_have_sent: bool = False) -> None:
        if may_have_sent:
            raise ValueError("HarnessAdapterBusyError must certify may_have_sent=False")
        super().__init__(detail)
        self.may_have_sent = False


class HarnessRequestConflictError(HarnessControlError):
    """A retained request id was reused with a different immutable source or payload."""


class HarnessInteractionNotPendingError(HarnessControlError):
    """An interaction response named an interaction that is not the pending one.

    The submission authority raises this (never a generic control error) when a respond
    request arrives with no matching pending interaction, with no active ordinary operation,
    or twice for the same interaction, so the serving route can answer with the honest
    ``not-pending`` conflict status instead of an unavailable claim.
    """


class HarnessBridgeEpochMismatchError(HarnessControlError):
    """A caller addressed lifecycle state from a replaced hosted runner generation."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            f"submission authority epoch changed (expected {expected!r}, actual {actual!r})"
        )
        self.expected = expected
        self.actual = actual


class CodexAppServerError(HarnessControlError):
    """The negotiated Codex app-server protocol or its configured contract was violated."""


class CodexAppServerRpcError(CodexAppServerError):
    """A correlated Codex JSON-RPC request returned an error response."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"Codex app-server {method} failed ({code}): {message}")
        self.method = method
        self.code = code


class NativeHistoryUnavailable(CodexAppServerError):
    """One native-history read is unavailable without invalidating the shared adapter."""

    def __init__(self, detail: str, *, code: str = "unavailable") -> None:
        super().__init__(detail)
        self.code = code


class NativeHistoryLimitExceeded(NativeHistoryUnavailable):
    """One native-history unit crossed its explicit bounded-materialization contract."""

    def __init__(
        self,
        detail: str,
        *,
        actual_bytes: int,
        limit_bytes: int,
    ) -> None:
        super().__init__(detail, code="materialization-limit")
        self.actual_bytes = actual_bytes
        self.limit_bytes = limit_bytes
