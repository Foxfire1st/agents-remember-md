"""Stable lifecycle-operation identity derived from a worktree contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agents_remember.models.lifecycles.operation import LifecycleOperationKind
from agents_remember.worktrees.worktree_contract import (
    WorktreeContract,
    contract_publication_text,
)


def operation_state_fingerprint(contract: WorktreeContract) -> str:
    """Hash lifecycle cells that change only when a sequential operation advances."""

    payload = {
        "codeBaseCommit": contract.code_base_commit,
        "memoryBaseCommit": contract.memory_base_commit,
        "closeoutStatus": contract.closeout_status,
        "codeCommit": contract.code_commit,
        "memoryContentCommit": contract.memory_content_commit,
        "ledgerCommit": contract.ledger_commit,
        "integrationStatus": contract.integration_status,
        "integratedCodeCommit": contract.integrated_code_commit,
        "integratedMemoryContentCommit": contract.integrated_memory_content_commit,
        "integratedLedgerCommit": contract.integrated_ledger_commit,
        "cleanup": contract.cleanup,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def closeout_contract_sha256(contract: WorktreeContract) -> str:
    """Hash the exact canonical contract bytes that closeout would publish."""

    published = contract_publication_text(contract.contract_path, contract)
    return hashlib.sha256(published.encode("utf-8")).hexdigest()


def operation_key(contract_path: Path, kind: LifecycleOperationKind, fingerprint: str) -> str:
    identity = f"{contract_path.resolve().as_posix()}\0{kind}\0{fingerprint}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
