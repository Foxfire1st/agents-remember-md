"""Lifecycle-operation vocabulary without model import cycles."""

from typing import Literal

LifecycleOperationKind = Literal["closeout", "integrate", "direct-landing"]
LifecycleOperationStatus = Literal[
    "queued",
    "running",
    "input-required",
    "termination-required",
    "completed",
    "failed",
    "cancelled",
]
LifecycleOperationPhase = Literal[
    "queued",
    "preflight",
    "memory-preflight",
    "quality",
    "approval-claim",
    "recovering-after-claim",
    "code-commit",
    "memory-refresh",
    "memory-commit",
    "ledger-commit",
    "integration-replay",
    "integration-quality",
    "source-merge",
    "contract-finalization",
    "door-publication",
    "termination-required",
    "direct-preflight",
    "direct-memory-commit",
    "direct-ledger-commit",
    "direct-terminal-publication",
    "completed",
    "failed",
    "cancelled",
]
LifecycleControlAction = Literal[
    "retry",
    "recover",
    "cancel",
    "revise",
    "retire",
    "supersede",
]
