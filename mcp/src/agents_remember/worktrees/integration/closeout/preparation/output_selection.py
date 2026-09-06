"""Retain one physically proved private or existing output through the original journal."""

from __future__ import annotations

from dataclasses import replace

from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.preparation import (
    PreparedCloseoutOutput,
    build_prepared_closeout_output,
    require_prepared_output_matches_intent,
)
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.integration.closeout.certification.selection import load_typed
from agents_remember.worktrees.integration.closeout.preparation_selection import (
    select_prepared_output,
)
from agents_remember.worktrees.modules.quality.certification_records import certificate_store

from .selected import ReobservePreparation, SelectedCloseoutPreparation


def retain_prepared_output(
    selected: SelectedCloseoutPreparation, raw_commit: bytes, *, reobserve: ReobservePreparation
) -> tuple[SelectedCloseoutPreparation, CertificateObjectReference, PreparedCloseoutOutput]:
    """Select exact already-observed Git bytes without consuming publication authority.

    The physical producer must reprove its named checkout immediately before this
    call. Live selection/configuration is checked again around immutable storage.
    """
    selected = reobserve(selected)
    output = build_prepared_closeout_output(
        raw_commit,
        selected.reference,
        disposition="created" if selected.intent.writeEnabled else "existing",
    )
    require_prepared_output_matches_intent(output, selected.intent)
    objects = certificate_store(selected.handoff.contract.worktree_group)
    objects.publish(output)
    reference = objects.reference("prepared-output", output.outputDigest)
    selected = reobserve(selected)
    state = selected.handoff.record.preparation
    assert state is not None
    leg = next((item for item in state.legs if item.leg == selected.intent.leg), None)
    if leg is None or leg.intent != selected.reference:
        refuse("prepared-output-selection-moved", selected.reference, leg)
    existing = leg.output
    if existing is not None:
        original = load_typed(objects, existing, PreparedCloseoutOutput)
        if existing != reference or original != output:
            refuse("prepared-output-moved", existing, reference)
        return selected, reference, original
    if state.legs[-1] != leg:
        refuse("prepared-output-leg-moved", selected.intent.leg, state.legs[-1].leg)
    handoff = selected.handoff
    record = select_prepared_output(handoff.contract, handoff.store, handoff.record, reference)
    return replace(selected, handoff=replace(handoff, record=record)), reference, output
