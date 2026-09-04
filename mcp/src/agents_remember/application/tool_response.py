"""Complete one tool result at the application boundary.

The MCP adapter supplies the tool name and raw use-case result.  This service
selects the wire model, attaches lifecycle-wide state, finalizes the token
count, and records the completed call.  Domain observation therefore remains
below the application boundary; the adapter receives a protocol-ready mapping.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agents_remember.application.next_step import next_step_for
from agents_remember.kernel.agentic_settings import DEFAULT_AGENT_NOTIFIER_STALE_CUTOFF_SECONDS
from agents_remember.models.base import NextStep, ResponseEnvelope
from agents_remember.models.tools.tool_response import finalize_tool_response
from agents_remember.observer.ambient import AmbientLifecycle, ambient
from agents_remember.serving.agent_notifier_heartbeat import agent_notifier_staleness_banner

_RESPONSE_PATH_FIELDS = ("contractPath", "enclosurePath")
_ARGUMENT_PATH_FIELDS = (
    "contract_path",
    "enclosure_path",
    "contractPath",
    "enclosurePath",
)


def bound_next_step(response: ResponseEnvelope, step: NextStep | None) -> NextStep | None:
    """Omit guidance whose task address contradicts the response's exact address."""

    if step is None or step.nextArgs is None:
        return step
    response_paths = {
        value
        for field in _RESPONSE_PATH_FIELDS
        if isinstance((value := getattr(response, field, None)), str) and value
    }
    if not response_paths:
        return step
    if len(response_paths) != 1:
        return None
    expected = next(iter(response_paths))
    observed = {
        str(step.nextArgs[field]) for field in _ARGUMENT_PATH_FIELDS if field in step.nextArgs
    }
    if observed and observed != {expected}:
        return None
    return step


def _agent_notifier_banner(amb: AmbientLifecycle) -> str | None:
    """Return the stale-agent-notifier banner without blocking a tool response."""
    try:
        return agent_notifier_staleness_banner(
            amb.root,
            now=datetime.now(UTC),
            stale_cutoff_seconds=DEFAULT_AGENT_NOTIFIER_STALE_CUTOFF_SECONDS,
        )
    except Exception:
        return None


def _attach_lifecycle_tail(
    response: ResponseEnvelope, amb: AmbientLifecycle, tool_name: str
) -> None:
    if (
        amb.current is not None
        and amb.current.state == "awaiting-developer"
        and tool_name != "lifecycle_turn_end_notification"
    ):
        amb.resume_from_await()
    # A refusal/recovery producer may supply an explicit nextStep alongside its top-level
    # recovery keys. Preserve that one authority instead of overwriting it with ambient phase
    # guidance derived from a contract the operation intentionally refused or just rewrote.
    step = response.nextStep or next_step_for(amb, tool_name)
    response.nextStep = bound_next_step(response, step)
    banner = _agent_notifier_banner(amb)
    response.agentNotifierBanner = banner
    response.supervisorBanner = banner


def complete_tool_response(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate, enrich, count, and observe one application result."""
    amb = ambient()
    finalized = finalize_tool_response(
        tool_name,
        payload,
        enrich=(
            (lambda response: _attach_lifecycle_tail(response, amb, tool_name))
            if amb is not None
            else None
        ),
    )
    if amb is not None:
        amb.emit_tool(tool_name, finalized)
    return finalized
