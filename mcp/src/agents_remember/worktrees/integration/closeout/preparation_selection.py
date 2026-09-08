"""Select original private preparation objects through the existing lifecycle CAS."""

from __future__ import annotations

from pathlib import Path

from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.models.lifecycles.operation import LifecycleOperationRecord
from agents_remember.models.lifecycles.preparation import (
    CloseoutPreparationIntent,
    PreparedCloseoutOutput,
    require_prepared_output_matches_intent,
)
from agents_remember.models.lifecycles.preparation_state import (
    OperationPreparationState,
    PreparationCommand,
    PreparationCommandTerminal,
    SelectedPreparation,
)
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.integration.closeout.certification.selection import (
    LoadedCertificationSelection,
    load_typed,
    require_selected_certification,
)
from agents_remember.worktrees.integration.lifecycle.certification_observation import (
    observe_certification_publication,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.modules.git import require_git
from agents_remember.worktrees.modules.quality.certification_records import certificate_store
from agents_remember.worktrees.worktree_contract import WorktreeContract


def selected_preparation_intents(
    contract: WorktreeContract, record: LifecycleOperationRecord
) -> tuple[CloseoutPreparationIntent, ...]:
    """Reopen only the selected exact objects, without discovering private outputs."""
    state = record.preparation
    if state is None:
        return ()
    objects = certificate_store(contract.worktree_group)
    intents: list[CloseoutPreparationIntent] = []
    for selected in state.legs:
        intent = load_typed(objects, selected.intent, CloseoutPreparationIntent)
        _require_intent_owner(contract, record, intent)
        if intent.leg != selected.leg or (not intent.writeEnabled and selected.commands):
            refuse("preparation-intent-selection-mismatch", intent.leg, selected.leg)
        if selected.output is not None:
            output = load_typed(objects, selected.output, PreparedCloseoutOutput)
            require_prepared_output_matches_intent(output, intent)
            if intent.writeEnabled and len(selected.commands) != 3:
                refuse("preparation-command-proof-missing", "original commit command", selected)
        intents.append(intent)
    return tuple(intents)


def select_preparation_intent(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    observed: LifecycleOperationRecord,
    reference: CertificateObjectReference,
) -> LifecycleOperationRecord:
    """Select an already stored, actually admitted intent before any private command."""
    selected = require_selected_certification(contract, observed)
    intent = load_typed(
        certificate_store(contract.worktree_group), reference, CloseoutPreparationIntent
    )
    _require_intent_owner(contract, observed, intent)
    _require_preparation_certificates(selected, intent)
    require_preparation_logical_refs(contract, observed, (intent,))
    prior = observed.preparation
    if prior is not None and any(item.intent == reference for item in prior.legs):
        return _select(store, observed, prior)
    legs = (() if prior is None else prior.legs) + (
        SelectedPreparation(leg=intent.leg, intent=reference),
    )
    state = OperationPreparationState(
        operationKey=observed.operationKey, generation=observed.generation, legs=legs
    )
    return _select(store, observed, state)


def _require_preparation_certificates(
    selected: LoadedCertificationSelection, intent: CloseoutPreparationIntent
) -> None:
    """Require the exact code prefix and, for memory legs, the selected fifth certificate."""
    certificates = tuple(item.certificate for item in selected.terminals[:4])
    if len(certificates) != 4 or any(item is None for item in certificates):
        refuse(
            "preparation-code-prefix-incomplete", "four original code certificates", certificates
        )
    prefix = tuple(item.certificate for item in selected.state.terminals[:4])
    if intent.prefixCertificates != prefix:
        refuse("preparation-code-prefix-mismatch", prefix, intent.prefixCertificates)
    gate_five = selected.state.terminals[4].certificate if len(selected.terminals) == 5 else None
    if intent.leg != "code" and (gate_five is None or gate_five != intent.gateFiveCertificate):
        refuse("preparation-memory-certificate-missing", gate_five, intent.gateFiveCertificate)


def begin_preparation_command(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    observed: LifecycleOperationRecord,
    command: PreparationCommand,
) -> LifecycleOperationRecord:
    """Record a start exactly once. Returning successfully is required before launch."""
    intents = selected_preparation_intents(contract, observed)
    selected = _last(observed)
    if not intents[-1].writeEnabled:
        refuse("preparation-command-disabled", "existing code readback only", command.kind)
    if command.terminal is not None or any(item.kind == command.kind for item in selected.commands):
        refuse("preparation-command-already-started", "named-output readback only", command.kind)
    require_preparation_logical_refs(contract, observed, intents)
    changed = selected.model_copy(update={"commands": (*selected.commands, command)})
    return _replace_last(store, observed, changed)


def observe_preparation_command(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    observed: LifecycleOperationRecord,
    command: PreparationCommand,
    terminal: PreparationCommandTerminal,
) -> LifecycleOperationRecord:
    """Retain the actual command outcome even after cancellation; grant no new start."""
    selected_preparation_intents(contract, observed)
    selected = _last(observed)
    if not selected.commands or selected.commands[-1] != command or command.terminal is not None:
        refuse("preparation-command-observation-mismatch", "exact unobserved command", command)
    finished = command.model_copy(update={"terminal": terminal})
    changed = selected.model_copy(update={"commands": (*selected.commands[:-1], finished)})
    return _replace_last(store, observed, changed)


def select_prepared_output(
    contract: WorktreeContract,
    store: LifecycleOperationStore,
    observed: LifecycleOperationRecord,
    reference: CertificateObjectReference,
) -> LifecycleOperationRecord:
    """Select a producer's physically verified output; this does not publish any task ref.

    The preparation producer must complete exact named-checkout/raw-object readback
    before this call. Here the original stored bytes and command relation are reopened.
    """
    intents = selected_preparation_intents(contract, observed)
    selected = _last(observed)
    output = load_typed(
        certificate_store(contract.worktree_group), reference, PreparedCloseoutOutput
    )
    require_prepared_output_matches_intent(output, intents[-1])
    if output.intent != selected.intent or (
        intents[-1].writeEnabled and len(selected.commands) != 3
    ):
        refuse("preparation-output-command-mismatch", selected.intent, output.intent)
    require_preparation_logical_refs(contract, observed, intents)
    return _replace_last(store, observed, selected.model_copy(update={"output": reference}))


def require_preparation_logical_refs(
    contract: WorktreeContract,
    record: LifecycleOperationRecord,
    intents: tuple[CloseoutPreparationIntent, ...] | None = None,
) -> dict[str, str]:
    """Observe unchanged protected logical roots/refs separately from private Git work."""
    selected = selected_preparation_intents(contract, record) if intents is None else intents
    facts: dict[str, str] = {}
    for intent in selected:
        _require_intent_owner(contract, record, intent)
        root = Path(intent.logicalRoot)
        if root.resolve() != root or not root.is_dir():
            refuse("preparation-logical-root-unsafe", intent.logicalRoot, root.as_posix())
        observed = {
            "repositoryIdentity": require_git(
                root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
            ),
            "logicalRef": require_git(root, ["symbolic-ref", "HEAD"]),
            "head": require_git(root, ["rev-parse", "HEAD"]),
            "ref": require_git(root, ["rev-parse", "--verify", intent.logicalRef]),
        }
        expected = {
            "repositoryIdentity": intent.repositoryIdentity,
            "logicalRef": intent.logicalRef,
            "head": intent.expectedOldCommit,
            "ref": intent.expectedOldCommit,
        }
        if observed != expected:
            refuse("preparation-logical-refs-changed", expected, observed)
        for key, value in observed.items():
            facts[f"preparation:{intent.leg}:{key}"] = value
        facts[f"preparation:{intent.leg}:intent"] = intent.intentDigest
        facts[f"preparation:{intent.leg}:logicalRoot"] = intent.logicalRoot
    return facts


def _require_intent_owner(
    contract: WorktreeContract, record: LifecycleOperationRecord, intent: CloseoutPreparationIntent
) -> None:
    selected = record.certification
    root = contract.code_worktree if intent.leg == "code" else contract.memory_worktree
    if (
        selected is None
        or record.operationKind != "closeout"
        or (intent.operationKey, intent.generation) != (record.operationKey, record.generation)
        or record.contractPath != contract.contract_path.as_posix()
        or intent.contractPath != record.contractPath
        or intent.frozenRun != selected.frozenRun
        or intent.candidateAuthorities != selected.candidateAuthorities
        or root is None
        or intent.logicalRoot != root.as_posix()
    ):
        refuse("preparation-intent-owner-mismatch", record.operationKey, intent.operationKey)


def _last(record: LifecycleOperationRecord) -> SelectedPreparation:
    if record.preparation is None:
        refuse("preparation-intent-missing", "separately selected intent", None)
    return record.preparation.legs[-1]


def _replace_last(
    store: LifecycleOperationStore,
    observed: LifecycleOperationRecord,
    selected: SelectedPreparation,
) -> LifecycleOperationRecord:
    if observed.preparation is None:
        refuse("preparation-intent-missing", "separately selected intent", None)
    state = observed.preparation.model_copy(
        update={"legs": (*observed.preparation.legs[:-1], selected)}
    )
    return _select(store, observed, state)


def _select(
    store: LifecycleOperationStore,
    observed: LifecycleOperationRecord,
    state: OperationPreparationState,
) -> LifecycleOperationRecord:
    observed = observe_certification_publication(store, observed)
    record, matched = store.update_if_current(
        observed, lambda current: current.model_copy(update={"preparation": state})
    )
    if not matched:
        refuse("preparation-selection-stale", observed.recordRevision, record.recordRevision)
    return record
