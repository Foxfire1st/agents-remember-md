"""Public vocabulary of the read-only lifecycle status-change wait (CCR-R15).

One typed outcome Literal shared by the wait controller, the application
controller, the MCP wire response, and the focused conformance tests so the
vocabulary cannot drift between layers.  Coherent outcomes return a snapshot
plus the next meaningful cursor; every other outcome is a typed read-only
refusal that never recommends a mutating action.
"""

from __future__ import annotations

from typing import Literal

LifecycleWaitOutcome = Literal[
    "changed",
    "unchanged",
    "successor",
    "wrong-contract",
    "no-operation",
    "wrong-generation",
    "wrong-cursor",
    "journal-replaced",
    "journal-unreadable",
    "projection-incoherent",
]

OUTCOME_CHANGED: LifecycleWaitOutcome = "changed"
OUTCOME_WRONG_CONTRACT: LifecycleWaitOutcome = "wrong-contract"
OUTCOME_UNCHANGED: LifecycleWaitOutcome = "unchanged"
OUTCOME_SUCCESSOR: LifecycleWaitOutcome = "successor"
OUTCOME_NO_OPERATION: LifecycleWaitOutcome = "no-operation"
OUTCOME_WRONG_GENERATION: LifecycleWaitOutcome = "wrong-generation"
OUTCOME_WRONG_CURSOR: LifecycleWaitOutcome = "wrong-cursor"
OUTCOME_JOURNAL_REPLACED: LifecycleWaitOutcome = "journal-replaced"
OUTCOME_JOURNAL_UNREADABLE: LifecycleWaitOutcome = "journal-unreadable"
# R18 typed read-only refusal: the record advanced but its public projection is
# incoherent, so the wait returns no snapshot and no mutating recommendation.
OUTCOME_PROJECTION_INCOHERENT: LifecycleWaitOutcome = "projection-incoherent"
