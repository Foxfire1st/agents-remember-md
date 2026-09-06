"""Prove genuine existing memory content separately from the current ledger HEAD."""

from __future__ import annotations

import hashlib
from pathlib import Path

from agents_remember.kernel.git_command import (
    inspect_existing_git_preparation,
    read_git_blob_bytes,
    read_git_tree_bytes,
    run_git,
)
from agents_remember.kernel.memory_ledger import find_mapping, parse_ledger_text
from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.preparation import ExistingMemoryPreparationProof
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.modules.git import require_git


def _memory_entries(root: Path, tree: str) -> tuple[str | None, str]:
    rows = read_git_tree_bytes(root, tree)
    if rows and not rows.endswith(b"\0"):
        refuse("memory-reuse-tree-malformed", "complete NUL-delimited tree", tree)
    ledger_blob: str | None = None
    other_entries: list[bytes] = []
    for row in rows.split(b"\0")[:-1]:
        metadata, separator, path = row.partition(b"\t")
        if not separator:
            refuse("memory-reuse-tree-malformed", "Git tree entry", tree)
        if path == b"memory.md":
            mode, kind, blob = metadata.split(b" ")
            if ledger_blob is not None or kind != b"blob" or mode != b"100644":
                refuse("memory-reuse-ledger-kind", "one ordinary memory.md blob", tree)
            ledger_blob = blob.decode("ascii")
        else:
            other_entries.append(row + b"\0")
    return ledger_blob, hashlib.sha256(b"".join(other_entries)).hexdigest()


def observe_existing_memory_proof(
    root: Path, *, repository_name: str, code_commit: str, certified_tree: str
) -> ExistingMemoryPreparationProof | None:
    """A dirty candidate needs a new M; a clean one requires exact real reuse evidence.

    An existing mapping selects its genuine historical M and proves that only
    ledger bytes differ from current HEAD. When no mapping exists, the exact
    current HEAD can be content for a future first mapping; no earlier edge is
    invented. Caller-owned Gate-5/operation authority is required to use either.
    """
    status = run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if status.returncode != 0:
        refuse("memory-reuse-status-unavailable", "actual logical memory status", status.returncode)
    if status.stdout:
        return None
    common = Path(require_git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]))
    logical_ref = require_git(root, ["symbolic-ref", "HEAD"])
    head = require_git(root, ["rev-parse", "--verify", "HEAD"])
    head_tree = require_git(root, ["rev-parse", "--verify", f"{head}^{{tree}}"])
    if head_tree != certified_tree:
        refuse("memory-reuse-certified-tree-moved", certified_tree, head_tree)
    inspect_existing_git_preparation(
        root, common_directory=common, logical_ref=logical_ref, commit=head, tree=head_tree
    )
    ledger_blob, non_ledger_digest = _memory_entries(root, head_tree)
    if ledger_blob is None:
        refuse("memory-reuse-ledger-missing", "current root memory.md", head_tree)
    ledger = parse_ledger_text(read_git_blob_bytes(root, ledger_blob).decode("utf-8"))
    if ledger.repo_name != repository_name:
        refuse("memory-reuse-ledger-repository", repository_name, ledger.repo_name)
    mapping = find_mapping(ledger, code_commit)
    content_commit = head if mapping is None else mapping.memory_commit
    content_tree = require_git(root, ["rev-parse", "--verify", f"{content_commit}^{{tree}}"])
    ancestry = run_git(root, ["merge-base", "--is-ancestor", content_commit, head])
    if ancestry.returncode != 0:
        refuse("memory-reuse-content-not-reachable", head, content_commit)
    _, content_digest = _memory_entries(root, content_tree)
    if content_digest != non_ledger_digest:
        refuse("memory-reuse-content-differs", non_ledger_digest, content_digest)
    payload = {
        "schemaVersion": "existing-memory-preparation-proof/v1",
        "repositoryIdentity": common.as_posix(),
        "logicalHeadCommit": head,
        "logicalHeadTree": head_tree,
        "ledgerBlob": ledger_blob,
        "codeCommit": code_commit,
        "memoryContentCommit": content_commit,
        "memoryContentTree": content_tree,
        "nonLedgerEntriesSha256": non_ledger_digest,
        "mappingDisposition": "unmapped-head" if mapping is None else "existing-mapping",
    }
    inspect_existing_git_preparation(
        root, common_directory=common, logical_ref=logical_ref, commit=head, tree=head_tree
    )
    return ExistingMemoryPreparationProof.model_validate(
        {**payload, "proofDigest": canonical_sha256(payload)}
    )
