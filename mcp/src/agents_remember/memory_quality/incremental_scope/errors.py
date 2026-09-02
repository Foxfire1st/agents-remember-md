"""Typed fail-closed result for an unproved incremental memory scope."""

from __future__ import annotations

from dataclasses import dataclass

from agents_remember.errors import AgentsRememberError


@dataclass(frozen=True)
class ScopeFailure:
    code: str
    detail: str
    checker: str | None = None
    node: str | None = None
    edge_class: str | None = None
    snapshot: str | None = None
    candidate: str | None = None
    owner: str | None = None


class ScopeUnprovenError(AgentsRememberError):
    """Incremental scope could not prove one exact required authority fact."""

    status = "scope-unproven"

    def __init__(self, failure: ScopeFailure) -> None:
        self.failure = failure
        super().__init__(f"{self.status}:{failure.code}: {failure.detail}")

    def response_fields(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "reason": self.failure.code,
            "detail": self.failure.detail,
        }
        for key, value in (
            ("checker", self.failure.checker),
            ("node", self.failure.node),
            ("edgeClass", self.failure.edge_class),
            ("snapshot", self.failure.snapshot),
            ("candidateDigest", self.failure.candidate),
            ("owner", self.failure.owner),
        ):
            if value is not None:
                payload[key] = value
        return payload


__all__ = ["ScopeFailure", "ScopeUnprovenError"]
