"""Typed task-intent slots and persisted identity states."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents_remember.errors import TaskIntentError

TASK_INTENT_SCHEMA = "task-intent/v1"


class _StrictIntentModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
    )


class ApprovedRequirementPacketRef(_StrictIntentModel):
    """One version-addressed approved packet; prose never opts itself into authority."""

    kind: Literal["approved-requirement-packet"] = "approved-requirement-packet"
    path: str = Field(min_length=1, max_length=8192)
    stableId: str = Field(min_length=1, max_length=256)
    version: str = Field(pattern=r"^v[1-9][0-9]*$")

    @field_validator("path", "stableId", "version")
    @classmethod
    def _strip_packet_identity(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("requirement packet identity fields must not be blank")
        return cleaned


class AcceptanceObligationQuestion(_StrictIntentModel):
    """A question explicitly typed as an unresolved acceptance obligation."""

    kind: Literal["acceptance-obligation"] = "acceptance-obligation"
    id: str = Field(min_length=1, max_length=256)
    question: str = Field(min_length=1, max_length=8192)

    @field_validator("id", "question")
    @classmethod
    def _strip_acceptance_obligation(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("acceptance-obligation identity and question must not be blank")
        return cleaned


class TaskIntentIdentity(_StrictIntentModel):
    """Canonical task-intent schema plus its exact projection digest."""

    schema_: Literal["task-intent/v1"] = Field(default=TASK_INTENT_SCHEMA, alias="schema")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MissingTaskIntent(_StrictIntentModel):
    """Typed legacy absence. It is deliberately not an identity and has no digest."""

    state: Literal["missing-intent"] = "missing-intent"


TaskIntentState = TaskIntentIdentity | MissingTaskIntent


def missing_task_intent() -> MissingTaskIntent:
    """Materialize typed absence only at a persisted-record decode boundary."""

    return MissingTaskIntent()


def task_intent_is_missing(value: TaskIntentState | None) -> bool:
    return value is None or isinstance(value, MissingTaskIntent)


def require_task_intent_identity(
    value: TaskIntentState | None,
    *,
    owner: str,
    next_action: str,
) -> TaskIntentIdentity:
    """Reject legacy absence at every currentness, reuse, and publication boundary."""

    if not isinstance(value, TaskIntentIdentity):
        raise TaskIntentError(
            f"{owner}-task-intent-unavailable",
            f"{owner} predates canonical task intent and cannot be current or reused",
            next_action=next_action,
        )
    return value


__all__ = [
    "TASK_INTENT_SCHEMA",
    "AcceptanceObligationQuestion",
    "ApprovedRequirementPacketRef",
    "MissingTaskIntent",
    "TaskIntentIdentity",
    "TaskIntentState",
    "missing_task_intent",
    "require_task_intent_identity",
    "task_intent_is_missing",
]
