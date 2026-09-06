"""R10: every value a producer can emit validates at the wire boundary it crosses.

The failure this exists to make impossible is not a typo. It is a *set difference*: a
producer's vocabulary grows, the response model's hand-written copy does not, and nothing
notices until a real payload carries the new member -- at which point pydantic raises a
`ValidationError` inside an `@server.tool()` handler that has no `except` for one. Measured
before this suite existed: 165 of the 213 `series-contract.md` files on disk (77.5%) made
`context_packet` raise, across seven independent gaps.

WHAT DEFENDS WHAT
-----------------
Read this before trusting a test below, because the mechanisms are not interchangeable and
the AST scan is the weakest of the three. It reads *bare string literals*. It does not
evaluate expressions, so `"a" + "b"`, an f-string, `_MAP["x"]`, a name imported from another
module and a plain local variable all pass through it unseen. Any claim that the scan alone
keeps a vocabulary honest would be false, and was.

    vocabulary                                   defended by
    ------------------------------------------   ----------------------------------------
    the six contract cells                       pyright, at `ContractCells`' typed fields
      workflow_kind, memory_mode,                and `WorktreeContract`'s own. `Produced-
      human_review_status, closeout_status,      LiteralTests` supplies the invariant that
      integration_status, cleanup                makes that total:
                                                 NO `dataclasses.replace` call may carry
                                                 one of these keywords, and every value
                                                 written at a typed writer must be an
                                                 expression this scan can enumerate.
    phase / nextOperation / nextTool             pyright, at `LifecycleGuidance`'s TypedDict
                                                 and `next_guidance`'s typed parameters.
                                                 The scan measures the emitted set and
                                                 asserts it EQUALS the alias.
    the seven listed below                       pyright, at direct typed constructor calls
                                                 (`GitFacts(state=...)` and friends). The
                                                 scan asserts produced == declared, which
                                                 is a real measurement in the *other*
                                                 direction: a member no writer can emit.
    ContractTask.workflow_kind / .memory_mode    runtime, at `_task_vocabulary`. Deliberately
                                                 plain `str` -- it is what a caller asked
                                                 for, arriving from `worktree_start`'s MCP
                                                 signature, and there is no type to check.

`dataclasses.replace` is why the first row needs a second mechanism at all. typeshed types
it `**changes: Any`, so `replace(contract, cleanup="reclaimed-ish")` produced *zero* pyright
diagnostics against a four-member `Literal` -- one `Any` in a third-party stub voiding the
guarantee this whole module is about. `amend_contract(contract, ContractCells(...))` exists to
put those fields back in front of the checker; the no-`replace` rule below is what stops a
future edit from routing around it. `cast` still passes both, as it must: it is a programmer
overriding the checker on purpose, and the scan's readability rule is what refuses it here.

THE THREE RULES
---------------
Deliberately different in kind, so a fix that satisfies one cannot fake the others:

`GuidanceWalkTests`
    Drives every branch of the ``lifecycle_guidance`` state machine and every writable
    ``cleanup`` value, and crosses each result through ``WorktreeSummary``. Behavioural: it
    would catch a phase the machine emits from a branch nobody remembered.
`ProducedLiteralTests`
    Reads the *source* of the package: every literal written onto a contract vocabulary
    field or handed to ``next_guidance`` must validate at its wire field, every such write
    must be statically readable, and none of them may be spelled as a ``replace`` keyword.
`AdvertisedVocabularyTests`
    Holds the published input contract to the published output contract, in BOTH directions:
    the ``workflow_kind`` set ``worktree_start``'s docstring advertises must equal the alias
    (so a member no tool advertises and no producer writes cannot be added silently), a
    ``memory_mode`` the contract parser accepts must validate, and every status the session
    tools' own docstrings roster must validate in the response that reports them.

The same three rules cover seven more wire vocabularies, all of which were measured
*aligned* before they were typed (the produced set equalled the declared set exactly, in
every one). They are here because they had the identical construction -- a hand-written
`Literal` at a boundary fed by an untyped dict, over a vocabulary another module decides --
and so differed from the seven that broke `context_packet` only in not having failed yet:

    RepoSummary.state           <- kernel.git_facts.RepoState
    BranchFreshness.state       <- kernel.git_freshness.FreshnessState
    DriftCheckResponse.status   <- onboarding_drift_check.models.DriftStatus (the last copy)
    FileRead.status             <- models.read_files.FileReadStatus
    SpawnAgentSessionResponse   \\
    SessionRetireResponse        > models.terminal, produced by application.terminal_tools
    SessionRenameResponse       /

Each is measured the same way: derive the producible set from the producers' source, assert
every member of it validates at the wire boundary, and assert the scan found the writers so
a scan that silently matched nothing cannot pass. Where the scan is exact the two sets are
asserted EQUAL, which also catches the other direction -- a declared member no producer can
ever emit, i.e. a vocabulary that has outgrown its own writer.

`ContractBoundaryTests` is the other half of the same guarantee, and the one place where
tolerance is the correct answer: what the *reader* does with a contract cell it cannot
classify, and what the *writer* refuses to put on disk.
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import (
    Any,
)

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
MCP_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(MCP_SRC))
sys.path.insert(0, str(MCP_TESTS))

from agents_remember.models.worktree import WorktreeSummary
from agents_remember.worktrees.worktree_contract import (
    WorktreeContract,
)

PACKAGE_ROOT = Path(str(sys.modules["agents_remember"].__file__)).resolve().parent

# The guidance keys the packet projection actually copies onto the wire model. `summary` and
# `carryoverDoneAt` are not among them, and `WorktreeSummary` is strict, so filtering here is
# what the production projection does field by field.
GUIDANCE_WIRE_KEYS = ("phase", "nextOperation", "nextTool", "nextArgs", "nextRequiredArgs")

# Contract field -> the `WorktreeSummary` field that reports it.
CONTRACT_FIELD_TO_WIRE_FIELD = {
    "workflow_kind": "workflowKind",
    "memory_mode": "memoryMode",
    "human_review_status": "humanReviewStatus",
    "closeout_status": "closeoutStatus",
    "integration_status": "integrationStatus",
    "cleanup": "cleanup",
}
# The calls that build or amend a worktree contract. `replace(...)` is deliberately NOT here:
# it is the call this file now forbids at these fields (see `TYPED_CONTRACT_WRITERS`).
CONTRACT_CALLS = frozenset({"WorktreeContract", "ContractTask", "ContractCells"})
# ...and the subset whose fields carry the vocabulary as a type, so what reaches them has to be
# an expression this scan can read. `ContractTask` is excluded on purpose: its two vocabulary
# fields are plain `str` because they are a *request*, narrowed at runtime by
# `_task_vocabulary` and pinned by `ContractBoundaryTests`.
TYPED_CONTRACT_WRITERS = frozenset({"WorktreeContract", "ContractCells"})
# The module that DECLARES the vocabularies is the one place a contract cell is legitimately
# written from a runtime-narrowed variable -- `_vocabulary_cell`'s result and
# `_task_vocabulary`'s pair are exactly that narrowing. Everywhere else, a value that is not
# a readable expression is a value nothing checked.
VOCABULARY_DECLARING_MODULE = "worktrees/worktree_contract.py"


def worktree_summary_accepts(field: str, value: object) -> bool:
    return _accepts(WorktreeSummary, {"state": "active"}, field, value)


def _accepts(model: Any, base: dict[str, Any], field: str, value: object) -> bool:
    try:
        model.model_validate({**base, field: value})
    except Exception:
        return False
    return True


def _contract(root: Path, **overrides: Any) -> WorktreeContract:
    """A contract with only the fields the guidance machine reads, plus the overrides."""
    worktree_group = root / "worktrees" / "r" / "t"
    base = WorktreeContract(
        task_id="T",
        task_name="t",
        repo_name="r",
        workflow_kind="light-task",
        memory_mode="internal",
        coordination_root=root,
        task_root=root / "tasks",
        contract_path=root / "tasks" / "series-contract.md",
        task_artifact=root / "tasks" / "task.md",
        worktree_group=worktree_group,
        code_repo_path=root / "repo",
        code_source_branch="main",
        code_work_branch="ar/t",
        code_base_commit="abc1234",
        code_worktree=worktree_group / "code",
        leaf_id="l",
    )
    return replace(base, **overrides)


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_wire_vocabulary_exhaustiveness.py:337).


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_wire_vocabulary_exhaustiveness.py:383).


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_wire_vocabulary_exhaustiveness.py:422).


def _module_tree(relative: str) -> ast.Module:
    path = PACKAGE_ROOT / relative
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --- the other seven: producers read out of their own source ---------------------------------

TERMINAL_TOOL = "application/terminal_tools.py"
TERMINAL_SPAWN_RESULTS = "application/terminal_spawn_results.py"
# Enough of each strict model to leave only the field under test undecided.
REPO_BASE = {"id": "r", "root": "/r", "branch": "main", "head": "0" * 40, "dirty": False}
SESSION_BASE = {"ok": False, "session": "s"}


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_wire_vocabulary_exhaustiveness.py:498).


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_wire_vocabulary_exhaustiveness.py:512).


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_wire_vocabulary_exhaustiveness.py:545).


# A status as a tool docstring writes one: quoted or backticked, lower-case, hyphens allowed.
_ADVERTISED_STATUS = re.compile(r"[`']([a-z][a-z0-9]*(?:-[a-z0-9]+)*)[`']")


# 260731-EFA-L7 R10: test moved verbatim in L7 split; branch not exercised by the unchanged assertion set (mcp/tests/test_wire_vocabulary_exhaustiveness.py:605).


# --- the seven with the same construction that had not drifted yet -----------------------
#
# Each asserts set EQUALITY between what the producers' source writes and what the alias
# declares, which is stricter than the containment above: it also fails a vocabulary that
# has outgrown its writer, i.e. a member no code path can ever put on the wire.


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
