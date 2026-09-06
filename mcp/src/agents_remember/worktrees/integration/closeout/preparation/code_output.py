"""Prepare and reprove one real code output under its original lifecycle owner."""

from __future__ import annotations

from dataclasses import replace

from agents_remember.certification.certificate_authority import validate_certificate_chain
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.operation import CloseoutOperationInput
from agents_remember.models.lifecycles.preparation import (
    CloseoutPreparationIntent,
    PreparedCloseoutOutput,
)
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
    current_certification_handoff,
)
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.integration.closeout.preparation_selection import (
    select_preparation_intent,
    selected_preparation_intents,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.modules.git import require_git
from agents_remember.worktrees.modules.quality.certification_records import certificate_store

from .output_selection import retain_prepared_output
from .policy import observe_git_preparation_policy
from .selected import SelectedCloseoutPreparation


def current_code_preparation(selected: SelectedCloseoutPreparation) -> SelectedCloseoutPreparation:
    """Reopen the selected original and current code prefix before any physical use."""
    handoff = current_certification_handoff(
        selected.handoff.contract, selected.handoff.record, selected.handoff.store
    )
    intents = selected_preparation_intents(handoff.contract, handoff.record)
    state = handoff.record.preparation
    if (
        not intents
        or state is None
        or (intents[0] != selected.intent or state.legs[0].intent != selected.reference)
    ):
        refuse("prepared-code-selection-moved", selected.reference, state)
    actual = _new_code_intent(handoff)
    if actual != selected.intent:
        refuse("prepared-code-intent-moved", selected.intent.intentDigest, actual.intentDigest)
    return replace(selected, handoff=handoff)


def select_code_preparation(handoff: CloseoutCertificationHandoff) -> SelectedCloseoutPreparation:
    """Select a closed intent before creating any checkout or invoking a commit."""
    handoff = current_certification_handoff(handoff.contract, handoff.record, handoff.store)
    state = handoff.record.preparation
    if state is not None:
        intent = selected_preparation_intents(handoff.contract, handoff.record)[0]
        return current_code_preparation(
            SelectedCloseoutPreparation(handoff, intent, state.legs[0].intent)
        )
    intent = _new_code_intent(handoff)
    objects = certificate_store(handoff.contract.worktree_group)
    objects.publish(intent)
    reference = objects.reference("preparation-intent", intent.intentDigest)
    record = select_preparation_intent(handoff.contract, handoff.store, handoff.record, reference)
    return current_code_preparation(
        SelectedCloseoutPreparation(replace(handoff, record=record), intent, reference)
    )


def _code_prefix(handoff: CloseoutCertificationHandoff) -> tuple[CertificateObjectReference, ...]:
    terminals = handoff.selected.terminals[:4]
    certificates = tuple(item.certificate for item in terminals if item.certificate is not None)
    references = tuple(
        item.certificateReference for item in terminals if item.certificateReference is not None
    )
    if len(certificates) != 4 or len(references) != 4:
        refuse(
            "prepared-code-prefix-incomplete", "four original green code gates", len(certificates)
        )
    validate_certificate_chain(handoff.selected.run.admission, certificates)
    return references


def _new_code_intent(handoff: CloseoutCertificationHandoff) -> CloseoutPreparationIntent:
    if not isinstance(handoff.record.input, CloseoutOperationInput):
        refuse("prepared-code-input-kind", "closeout", handoff.record.operationKind)
    contract = handoff.contract
    root = contract.code_worktree
    effective = handoff.record.input.effectiveInput
    enabled = effective.enabled("code")
    prefix = _code_prefix(handoff)
    policy = observe_git_preparation_policy(root)
    head = require_git(root, ["rev-parse", "--verify", "HEAD"])
    private = (
        contract.worktree_group
        / "preparation"
        / handoff.record.operationKey
        / f"generation-{handoff.record.generation}"
        / "code"
    )
    payload = {
        "schemaVersion": "closeout-preparation-intent/v1",
        "operationKey": handoff.record.operationKey,
        "generation": handoff.record.generation,
        "contractPath": contract.contract_path.as_posix(),
        "contractSha256": closeout_contract_sha256(contract),
        "leg": "code",
        "writeEnabled": enabled,
        "repositoryIdentity": require_git(
            root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        ),
        "logicalRoot": root.as_posix(),
        "logicalRef": f"refs/heads/{contract.code_work_branch}",
        "expectedOldCommit": head,
        "parentCommit": head,
        "admittedTree": handoff.selected.run.repositoryPlan.candidateIdentity.value,
        "privateRoot": private.as_posix() if enabled else None,
        "normalizedMessage": effective.message_for("code") if enabled else None,
        "hookPolicy": "strict-code-no-verify",
        "gitConfigSha256": policy.git_config_sha256,
        "hooksSha256": policy.hooks_sha256,
        "frozenRun": handoff.selected.state.frozenRun.model_dump(mode="json"),
        "candidateAuthorities": handoff.selected.state.candidateAuthorities.model_dump(mode="json"),
        "prefixCertificates": [item.model_dump(mode="json") for item in prefix],
        "gateFiveCertificate": None,
        "existingMemoryProof": None,
    }
    return CloseoutPreparationIntent.model_validate(
        {**payload, "intentDigest": canonical_sha256(payload)}
    )


def retain_code_output(
    selected: SelectedCloseoutPreparation, raw_commit: bytes
) -> tuple[SelectedCloseoutPreparation, CertificateObjectReference, PreparedCloseoutOutput]:
    """Retain an already-proved code output through the shared original-byte owner."""
    return retain_prepared_output(selected, raw_commit, reobserve=current_code_preparation)
