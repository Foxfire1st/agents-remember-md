"""Exact route-owned Git scope for the structural affected-memory census."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents_remember.kernel.git_command import run_git
from agents_remember.models.lifecycles.memory_candidate import MemoryCandidatePairIdentity
from agents_remember.worktrees.integration.closeout.future_code_candidate import (
    FutureCodeCandidateIdentity,
    capture_future_code_candidate,
    require_current_future_code_candidate,
)
from agents_remember.worktrees.integration.closeout.memory_candidate_pair import (
    resolve_memory_candidate_pair,
)
from agents_remember.worktrees.modules.git import head_commit, is_ancestor, worktree_candidate_tree
from agents_remember.worktrees.worktree_contract import WorktreeContract


class MemoryCensusCodeInput(BaseModel):
    """Internal route-producer input; never a public caller-selected candidate.

    Non-future modes are supplied by their respective lifecycle owners. This
    module proves their Git identities; it does not issue their acceptance.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: Literal[
        "future-code",
        "current-head-memory-only",
        "direct-landing-existing-commit",
        "existing-code-recovery",
    ]
    pairIdentity: MemoryCandidatePairIdentity
    observedCodeHead: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    codeBaseCommit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    targetCodeTree: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    targetCodeCommit: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

    @model_validator(mode="after")
    def _require_route_target(self) -> MemoryCensusCodeInput:
        if (self.mode == "future-code") != (self.targetCodeCommit is None):
            raise ValueError("only future-code census input may omit its target commit")
        if self.codeBaseCommit != self.pairIdentity.codeBaseCommit:
            raise ValueError("census comparison base must belong to its canonical pair")
        return self


@dataclass(frozen=True)
class MemoryCensusPathChange:
    status: Literal["A", "M", "D", "R", "C", "T"]
    old_path: str | None
    new_path: str | None

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(path for path in (self.old_path, self.new_path) if path is not None)
        )


@dataclass(frozen=True)
class MemoryCensusScope:
    pair_identity: MemoryCandidatePairIdentity
    code_input: MemoryCensusCodeInput
    memory_candidate_tree: str
    memory_baseline_commit: str
    verified_code_commit: str | None
    working_changes: tuple[MemoryCensusPathChange, ...]
    committed_changes: tuple[MemoryCensusPathChange, ...]
    memory_changes: tuple[MemoryCensusPathChange, ...]

    def to_payload(self) -> dict[str, object]:
        """Stable JSON-ready facts bound alongside the complete census artifact."""
        return {
            "pairIdentity": self.pair_identity.model_dump(mode="json"),
            "codeInput": self.code_input.model_dump(mode="json"),
            "memoryCandidateTree": self.memory_candidate_tree,
            "memoryBaselineCommit": self.memory_baseline_commit,
            "verifiedCodeCommit": self.verified_code_commit,
            "workingChanges": [asdict(change) for change in self.working_changes],
            "committedChanges": [asdict(change) for change in self.committed_changes],
            "memoryChanges": [asdict(change) for change in self.memory_changes],
        }

    @property
    def working_paths(self) -> tuple[str, ...]:
        return _paths(self.working_changes)

    @property
    def committed_paths(self) -> tuple[str, ...]:
        return tuple(
            path for path in _paths(self.committed_changes) if path not in self.working_paths
        )

    @property
    def memory_paths(self) -> tuple[str, ...]:
        return _paths(self.memory_changes)


def capture_memory_census_scope(
    contract: WorktreeContract, *, code_input: MemoryCensusCodeInput | None = None
) -> MemoryCensusScope:
    """Capture all structurally eligible paths without adopting historical debt."""

    pair = resolve_memory_candidate_pair(contract)
    source = code_input or _preparation_input(contract, pair)
    _require_code_input(contract, pair, source)
    code_end = source.targetCodeCommit or source.observedCodeHead
    working = (
        _diff(contract.code_worktree, source.observedCodeHead, source.targetCodeTree)
        if source.mode in {"future-code", "current-head-memory-only"}
        else ()
    )
    committed = _committed_changes(contract, source.codeBaseCommit, code_end)
    memory_root = Path(pair.memoryRoot)
    baseline = _memory_baseline(contract, memory_root)
    memory_tree = _memory_tree(contract, memory_root)
    memory = _diff(memory_root, baseline, memory_tree)
    if resolve_memory_candidate_pair(contract) != pair:
        raise RuntimeError("census pair changed during scope capture")
    _require_code_input(contract, pair, source)
    if _memory_tree(contract, memory_root) != memory_tree:
        raise RuntimeError("memory candidate changed during census scope capture")
    return MemoryCensusScope(
        pair,
        source,
        memory_tree,
        baseline,
        contract.code_commit or None,
        working,
        committed,
        memory,
    )


def _preparation_input(
    contract: WorktreeContract, pair: MemoryCandidatePairIdentity
) -> MemoryCensusCodeInput:
    observed_head = head_commit(contract.code_worktree)
    head_tree = head_commit(contract.code_worktree, f"{observed_head}^{{tree}}")
    candidate_tree = _code_tree(contract)
    if head_commit(contract.code_worktree) != observed_head:
        raise RuntimeError("code HEAD moved during census preparation route selection")
    if candidate_tree == head_tree:
        # Preparation identifies unchanged code; it does not issue acceptance.
        return MemoryCensusCodeInput(
            mode="current-head-memory-only",
            pairIdentity=pair,
            observedCodeHead=observed_head,
            codeBaseCommit=pair.codeBaseCommit,
            targetCodeTree=head_tree,
            targetCodeCommit=observed_head,
        )
    candidate = capture_future_code_candidate(contract)
    if candidate.observedCodeHead != observed_head or candidate.codeCandidateTree != candidate_tree:
        raise RuntimeError("code candidate moved during census preparation route selection")
    return MemoryCensusCodeInput(
        mode="future-code",
        pairIdentity=pair,
        observedCodeHead=candidate.observedCodeHead,
        codeBaseCommit=candidate.codeBaseCommit,
        targetCodeTree=candidate.codeCandidateTree,
    )


def _require_code_input(
    contract: WorktreeContract, pair: MemoryCandidatePairIdentity, source: MemoryCensusCodeInput
) -> None:
    if source.pairIdentity != pair:
        raise RuntimeError("census route input does not bind the current canonical pair")
    repository = contract.code_worktree
    if source.mode == "future-code":
        require_current_future_code_candidate(
            contract,
            FutureCodeCandidateIdentity(
                observedCodeHead=source.observedCodeHead,
                codeBaseCommit=source.codeBaseCommit,
                codeCandidateTree=source.targetCodeTree,
            ),
        )
    else:
        if head_commit(repository, f"{source.targetCodeCommit}^{{tree}}") != source.targetCodeTree:
            raise RuntimeError("census route target commit and tree disagree")
        if source.mode == "current-head-memory-only" and (
            source.observedCodeHead != source.targetCodeCommit
            or head_commit(repository) != source.targetCodeCommit
            or _code_tree(contract) != source.targetCodeTree
            or head_commit(repository) != source.targetCodeCommit
        ):
            raise RuntimeError("current-head census requires its exact unchanged working tree")
    end = source.targetCodeCommit or source.observedCodeHead
    if not is_ancestor(repository, source.codeBaseCommit, end):
        raise RuntimeError("census route target does not descend from its recorded comparison base")


def _committed_changes(
    contract: WorktreeContract, base: str, end: str
) -> tuple[MemoryCensusPathChange, ...]:
    verified = contract.code_commit
    boundary = base
    if verified and is_ancestor(contract.code_worktree, base, verified):
        boundary = verified
    elif verified and not is_ancestor(contract.code_worktree, verified, base):
        raise RuntimeError("verified code and synced code base have conflicting histories")
    if not is_ancestor(contract.code_worktree, boundary, end):
        raise RuntimeError("code comparison boundary is outside the census target history")
    return _diff(contract.code_worktree, boundary, end)


def _memory_baseline(contract: WorktreeContract, repository: Path) -> str:
    base = contract.memory_base_commit
    verified = contract.memory_content_commit
    if verified and is_ancestor(repository, base, verified):
        base = verified
    elif verified and not is_ancestor(repository, verified, base):
        raise RuntimeError("verified memory and synced memory base have conflicting histories")
    if not is_ancestor(repository, base, head_commit(repository)):
        raise RuntimeError("verified memory baseline is outside the candidate history")
    return base


def _memory_tree(contract: WorktreeContract, repository: Path) -> str:
    return worktree_candidate_tree(
        repository, contract.worktree_group / "reports" / ".memory-census.index"
    )


def _code_tree(contract: WorktreeContract) -> str:
    return worktree_candidate_tree(
        contract.code_worktree, contract.worktree_group / "reports" / ".memory-census-code.index"
    )


def _paths(changes: tuple[MemoryCensusPathChange, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {path for change in changes for path in change.paths}, key=lambda p: p.encode("utf-8")
        )
    )


def _diff(repository: Path, before: str, after: str) -> tuple[MemoryCensusPathChange, ...]:
    result = run_git(
        repository,
        [
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--no-ext-diff",
            "--no-textconv",
            before,
            after,
            "--",
        ],
    )
    if result.returncode != 0:
        raise RuntimeError("could not read exact census tree diff")
    fields = result.stdout.split("\0")
    if fields.pop() != "":
        raise RuntimeError("census Git diff has no terminal path delimiter")
    changes: list[MemoryCensusPathChange] = []
    tokens = iter(fields)
    for status in tokens:
        if status[:1] not in {"A", "M", "D", "R", "C", "T"}:
            raise RuntimeError("unsupported census Git change")
        try:
            paths = [next(tokens)]
            if status.startswith(("R", "C")):
                paths.append(next(tokens))
        except StopIteration as exc:
            raise RuntimeError("malformed census Git change") from exc
        for path in paths:
            _require_path(repository, path)
        letter = cast(Literal["A", "M", "D", "R", "C", "T"], status[0])
        old = None if letter == "A" else paths[0]
        new = None if letter == "D" else paths[-1]
        changes.append(MemoryCensusPathChange(letter, old, new))
    return tuple(sorted(changes, key=lambda c: tuple(p.encode("utf-8") for p in c.paths)))


def _require_path(repository: Path, path: str) -> None:
    path.encode("utf-8", errors="strict")
    if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
        raise RuntimeError("census path must preserve one exact root-relative Git identity")
    if not (repository / path).resolve().is_relative_to(repository.resolve()):
        raise RuntimeError("census path escapes its repository through a symlink")


__all__ = [
    "MemoryCensusCodeInput",
    "MemoryCensusPathChange",
    "MemoryCensusScope",
    "capture_memory_census_scope",
]
