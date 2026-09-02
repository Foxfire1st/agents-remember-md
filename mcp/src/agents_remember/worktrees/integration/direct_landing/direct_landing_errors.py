"""Typed refusals shared by direct-landing admission and recovery."""

from __future__ import annotations

from collections.abc import Mapping


class DirectLandingError(ValueError):
    """The direct landing request is malformed or violates accepted facts."""

    def __init__(
        self,
        status: str,
        detail: str,
        *,
        expected: Mapping[str, object] | None = None,
        observed: Mapping[str, object] | None = None,
        next_action: str | None = None,
    ) -> None:
        self.status = status
        self.detail = detail
        self.expected = dict(expected or {})
        self.observed = dict(observed or {})
        self.next_action = next_action
        super().__init__(f"{status}: {detail}")
