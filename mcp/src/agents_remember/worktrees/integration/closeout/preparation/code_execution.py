"""Compose shared private execution with the code owner's existing-output proof."""

from __future__ import annotations

from pathlib import Path

from agents_remember.kernel.git_command import inspect_existing_git_preparation
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.preparation import PreparedCloseoutOutput
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
)

from .code_output import current_code_preparation, retain_code_output, select_code_preparation
from .private_execution import observe_private_output, prepare_private_output
from .selected import SelectedCloseoutPreparation


def observe_code_output(selected: SelectedCloseoutPreparation) -> bytes:
    """Prove the exact named physical code view; never discover another commit."""
    selected = current_code_preparation(selected)
    intent = selected.intent
    if intent.writeEnabled:
        return observe_private_output(selected, reobserve=current_code_preparation)
    raw = inspect_existing_git_preparation(
        Path(intent.logicalRoot),
        common_directory=Path(intent.repositoryIdentity),
        logical_ref=intent.logicalRef,
        commit=intent.expectedOldCommit,
        tree=intent.admittedTree,
    )
    current_code_preparation(selected)
    return raw


def prepare_code_output(
    handoff: CloseoutCertificationHandoff,
) -> tuple[SelectedCloseoutPreparation, CertificateObjectReference, PreparedCloseoutOutput]:
    """Complete only a new admitted command suffix; uncertain starts are read-only."""
    selected = select_code_preparation(handoff)
    if selected.intent.writeEnabled:
        selected, raw = prepare_private_output(selected, reobserve=current_code_preparation)
    else:
        raw = observe_code_output(selected)
    return retain_code_output(selected, raw)
