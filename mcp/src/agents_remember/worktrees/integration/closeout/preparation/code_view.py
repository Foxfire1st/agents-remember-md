"""Reprove selected real code bytes while retaining the canonical logical memory pair."""

from __future__ import annotations

from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.preparation import (
    PreparedCloseoutOutput,
    build_prepared_closeout_output,
    require_prepared_output_matches_intent,
)
from agents_remember.models.lifecycles.prepared_memory import PreparedCodeExecutionView
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
)
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.integration.closeout.certification.selection import load_typed
from agents_remember.worktrees.integration.closeout.memory_candidate_pair import (
    resolve_memory_candidate_pair,
)
from agents_remember.worktrees.integration.closeout.preparation_selection import (
    selected_preparation_intents,
)
from agents_remember.worktrees.modules.quality.certification_records import certificate_store

from .code_execution import observe_code_output, prepare_code_output
from .code_output import current_code_preparation
from .selected import SelectedCloseoutPreparation


def observe_prepared_code_view(handoff: CloseoutCertificationHandoff) -> PreparedCodeExecutionView:
    """Read only this generation's named output; no creation, adoption or ledger fiction."""
    state = handoff.record.preparation
    if state is None or state.legs[0].output is None:
        refuse("prepared-code-view-unavailable", "journal-selected code output", state)
    intent = selected_preparation_intents(handoff.contract, handoff.record)[0]
    selected = current_code_preparation(
        SelectedCloseoutPreparation(handoff, intent, state.legs[0].intent)
    )
    objects = certificate_store(selected.handoff.contract.worktree_group)
    output = load_typed(objects, state.legs[0].output, PreparedCloseoutOutput)
    require_prepared_output_matches_intent(output, intent)
    raw = observe_code_output(selected)
    actual = build_prepared_closeout_output(raw, selected.reference, disposition=output.disposition)
    if actual != output:
        refuse("prepared-code-view-output-moved", output.outputDigest, actual.outputDigest)
    pair = resolve_memory_candidate_pair(selected.handoff.contract)
    physical_root = intent.logicalRoot if output.disposition == "existing" else intent.privateRoot
    payload = {
        "schemaVersion": "prepared-code-execution-view/v1",
        "operationKey": selected.handoff.record.operationKey,
        "generation": selected.handoff.record.generation,
        "logicalPair": pair.model_dump(mode="json"),
        "preparationIntent": selected.reference.model_dump(mode="json"),
        "preparedOutput": state.legs[0].output.model_dump(mode="json"),
        "physicalCodeRoot": physical_root,
        "repositoryIdentity": intent.repositoryIdentity,
        "codeCommit": output.commit,
        "codeTree": output.tree,
        "disposition": output.disposition,
    }
    current_code_preparation(selected)
    return PreparedCodeExecutionView.model_validate(
        {**payload, "viewDigest": canonical_sha256(payload)}
    )


def prepare_code_view(
    handoff: CloseoutCertificationHandoff,
) -> tuple[CloseoutCertificationHandoff, PreparedCodeExecutionView]:
    """Finish allowed private commands and return a newly proved physical execution view."""
    selected, _, _ = prepare_code_output(handoff)
    return selected.handoff, observe_prepared_code_view(selected.handoff)
