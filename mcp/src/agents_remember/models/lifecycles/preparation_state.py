"""Durable selection and at-most-once commands for private closeout work."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from agents_remember.models.certification.base import FrozenContractModel
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.preparation import PreparationLeg

_Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PreparationCommandKind = Literal["create", "materialize", "commit"]
PreparationWorker = tuple[int | None, str | None, str | None]
_COMMAND_ORDER: tuple[PreparationCommandKind, ...] = ("create", "materialize", "commit")
_LEG_ORDER: tuple[PreparationLeg, ...] = ("code", "memory-content", "ledger")


class PreparationCommandTerminal(FrozenContractModel):
    """An actual command observation; unknown never authorizes another start."""

    outcome: Literal["succeeded", "failed", "unknown"]
    observedAt: str = Field(min_length=1, max_length=128)
    returnCode: int | None = Field(default=None, strict=True)
    stdoutSha256: _Digest | None = None
    stderrSha256: _Digest | None = None
    detail: str = Field(default="", max_length=8192)

    @model_validator(mode="after")
    def _require_actual_exit(self) -> Self:
        if self.outcome == "unknown" and self.returnCode is not None:
            raise ValueError("unknown preparation outcome cannot claim a return code")
        if self.outcome == "succeeded" and self.returnCode != 0:
            raise ValueError("successful preparation command requires return code zero")
        if self.outcome == "failed" and self.returnCode in {None, 0}:
            raise ValueError("failed preparation command requires an observed nonzero exit")
        return self


class PreparationCommand(FrozenContractModel):
    """The exact worker command persisted before its first and only invocation."""

    kind: PreparationCommandKind
    cwd: str = Field(min_length=1, max_length=8192)
    argv: tuple[Annotated[str, Field(min_length=1, max_length=8192)], ...] = Field(
        min_length=1, max_length=256
    )
    workerPid: int = Field(strict=True, ge=1)
    workerLease: _Digest
    workerProcessFingerprint: _Digest
    startedAt: str = Field(min_length=1, max_length=128)
    terminal: PreparationCommandTerminal | None = None

    @field_validator("cwd")
    @classmethod
    def _require_canonical_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not path.is_absolute()
            or path.as_posix() != value
            or ".." in path.parts
            or "\x00" in value
        ):
            raise ValueError("preparation command cwd requires a canonical absolute path")
        return value

    @field_validator("argv")
    @classmethod
    def _require_bounded_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\x00" in item for item in value) or sum(map(len, value)) > 65_536:
            raise ValueError("preparation argv must be NUL-free and at most 65536 characters")
        return value


class SelectedPreparation(FrozenContractModel):
    """One exact intent and its retained command/output observations."""

    leg: PreparationLeg
    intent: CertificateObjectReference
    commands: tuple[PreparationCommand, ...] = Field(default=(), max_length=3)
    output: CertificateObjectReference | None = None

    @model_validator(mode="after")
    def _require_ordered_commands(self) -> Self:
        if self.intent.kind != "preparation-intent":
            raise ValueError("preparation selection requires an exact intent reference")
        if self.output is not None and self.output.kind != "prepared-output":
            raise ValueError("preparation output requires an exact prepared-output reference")
        if tuple(item.kind for item in self.commands) != _COMMAND_ORDER[: len(self.commands)]:
            raise ValueError("preparation commands must be a unique ordered prefix")
        if any(
            item.terminal is None or item.terminal.outcome != "succeeded"
            for item in self.commands[:-1]
        ):
            raise ValueError("unfinished or failed preparation command blocks every later command")
        if (
            self.output is not None
            and self.commands
            and (len(self.commands) != 3 or self.commands[-1].terminal is None)
        ):
            raise ValueError("created output requires an observed original commit command")
        return self


class OperationPreparationState(FrozenContractModel):
    """Unfinished private work, independent of published mutation authority."""

    schemaVersion: Literal["closeout-preparation-selection/v1"] = (
        "closeout-preparation-selection/v1"
    )
    operationKey: _Digest
    generation: int = Field(strict=True, ge=1)
    legs: tuple[SelectedPreparation, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def _require_ordered_legs(self) -> Self:
        if tuple(item.leg for item in self.legs) != _LEG_ORDER[: len(self.legs)]:
            raise ValueError(
                "private preparation must retain the ordered code/content/ledger prefix"
            )
        if any(item.output is None for item in self.legs[:-1]):
            raise ValueError("a later private leg requires the prior selected output")
        return self


def validate_preparation_owner(
    selected: OperationPreparationState | None,
    operation_kind: str,
    operation_key: str,
    generation: int,
    *,
    has_certification: bool,
) -> None:
    if selected is not None and (
        operation_kind != "closeout"
        or (selected.operationKey, selected.generation) != (operation_key, generation)
    ):
        raise ValueError("private preparation must belong to this exact closeout generation")
    if selected is not None and not has_certification:
        raise ValueError("private preparation requires the original selected certification")


def validate_preparation_transition(
    before: OperationPreparationState | None,
    after: OperationPreparationState | None,
    *,
    can_start: bool,
    worker: PreparationWorker,
    exited_worker: PreparationWorker,
) -> None:
    """Permit one durable event per CAS, preserving all prior private evidence."""
    if before == after:
        return
    if after is None:
        raise RuntimeError("private preparation selection cannot be cleared")
    if before is None:
        if not can_start or len(after.legs) != 1 or after.legs[0].commands or after.legs[0].output:
            raise RuntimeError("private preparation begins with a separately selected intent")
        return
    if (before.operationKey, before.generation) != (after.operationKey, after.generation):
        raise RuntimeError("private preparation owner is immutable")
    if len(after.legs) == len(before.legs) + 1:
        new = after.legs[-1]
        if not can_start or after.legs[:-1] != before.legs or new.commands or new.output:
            raise RuntimeError("new private leg requires a separately selected intent")
        return
    if len(after.legs) != len(before.legs) or after.legs[:-1] != before.legs[:-1]:
        raise RuntimeError("selected private preparation prefix is immutable")
    _validate_leg_transition(before.legs[-1], after.legs[-1], can_start, worker, exited_worker)


def _validate_leg_transition(
    before: SelectedPreparation,
    after: SelectedPreparation,
    can_start: bool,
    worker: PreparationWorker,
    exited_worker: PreparationWorker,
) -> None:
    if (before.leg, before.intent) != (after.leg, after.intent) or before.output is not None:
        raise RuntimeError("selected private intent and prepared output are immutable")
    if after.output is not None:
        if not can_start or after.commands != before.commands:
            raise RuntimeError("prepared output selection requires a separate current-owner CAS")
        return
    if len(after.commands) == len(before.commands) + 1:
        command = after.commands[-1]
        owner = (command.workerPid, command.workerLease, command.workerProcessFingerprint)
        if (
            not can_start
            or after.commands[:-1] != before.commands
            or command.terminal is not None
            or owner != worker
        ):
            raise RuntimeError("private command start requires its exact current worker owner")
        return
    if len(after.commands) != len(before.commands) or not before.commands:
        raise RuntimeError("private commands cannot be removed or restarted")
    original, observed = before.commands[-1], after.commands[-1]
    if (
        after.commands[:-1] != before.commands[:-1]
        or original.terminal is not None
        or observed.terminal is None
        or original.model_dump(exclude={"terminal"}) != observed.model_dump(exclude={"terminal"})
    ):
        raise RuntimeError("private command observations are append-only and immutable")
    owner = (original.workerPid, original.workerLease, original.workerProcessFingerprint)
    if not can_start and owner not in {worker, exited_worker}:
        raise RuntimeError("late private readback requires its original worker or proven exit")
