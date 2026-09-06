"""Execute one selected private code, memory-content or ledger command suffix."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from agents_remember.kernel.git_command import (
    admit_private_git_preparation,
    inspect_git_preparation,
    preparation_command,
    run_git_preparation,
)
from agents_remember.kernel.git_preparation import (
    PreparationAction,
    PrivateGitPreparationBinding,
    PrivateGitPreparationCapability,
)
from agents_remember.models.lifecycles.preparation import CloseoutPreparationIntent
from agents_remember.models.lifecycles.preparation_state import (
    PreparationCommand,
    PreparationCommandTerminal,
    SelectedPreparation,
)
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.integration.closeout.preparation_selection import (
    begin_preparation_command,
    observe_preparation_command,
)

from .policy import observe_git_preparation_policy
from .selected import ReobservePreparation, SelectedCloseoutPreparation


def _selected_leg(selected: SelectedCloseoutPreparation) -> SelectedPreparation:
    state = selected.handoff.record.preparation
    leg = (
        None
        if state is None
        else next((item for item in state.legs if item.leg == selected.intent.leg), None)
    )
    if leg is None or leg.intent != selected.reference:
        refuse("private-preparation-selection-moved", selected.reference, leg)
    return leg


def private_git_binding(intent: CloseoutPreparationIntent) -> PrivateGitPreparationBinding:
    """Convert one exact write-enabled intent through the single preparation binding owner."""
    if not intent.writeEnabled or intent.privateRoot is None or intent.normalizedMessage is None:
        refuse("private-preparation-write-disabled", "write-enabled private intent", intent.leg)
    return PrivateGitPreparationBinding(
        logical_root=Path(intent.logicalRoot),
        logical_ref=intent.logicalRef,
        expected_logical_commit=intent.expectedOldCommit,
        common_directory=Path(intent.repositoryIdentity),
        private_root=Path(intent.privateRoot),
        parent_commit=intent.parentCommit,
        admitted_tree=intent.admittedTree,
        message=intent.normalizedMessage,
        hook_policy=intent.hookPolicy,
        operation_key=intent.operationKey,
        generation=intent.generation,
        intent_digest=intent.intentDigest,
    )


def _capability(
    selected: SelectedCloseoutPreparation, reobserve: ReobservePreparation
) -> PrivateGitPreparationCapability:
    binding = private_git_binding(selected.intent)

    def authorize(actual: PrivateGitPreparationBinding) -> None:
        current = reobserve(selected)
        if actual != private_git_binding(current.intent):
            refuse("private-preparation-binding-moved", binding.intent_digest, actual.intent_digest)
        if actual.private_root.exists():
            policy = observe_git_preparation_policy(actual.private_root)
            if policy.git_config_sha256 != current.intent.gitConfigSha256:
                refuse(
                    "private-preparation-config-mismatch",
                    current.intent.gitConfigSha256,
                    policy.git_config_sha256,
                )
            # Before materialization, repository-relative hooks may not exist yet.
            # The actual commit and every output read must prove the full policy.
            leg = _selected_leg(current)
            if any(item.kind == "commit" for item in leg.commands) or leg.output is not None:
                policy.require_intent(current.intent)

    return admit_private_git_preparation(binding, authorize=authorize)


def _terminal_record(
    selected: SelectedCloseoutPreparation,
    command: PreparationCommand,
    terminal: PreparationCommandTerminal,
) -> SelectedCloseoutPreparation:
    handoff = selected.handoff
    current = handoff.store.read()
    if current is None or (current.operationKey, current.generation) != (
        handoff.record.operationKey,
        handoff.record.generation,
    ):
        refuse("private-preparation-terminal-owner-moved", handoff.record.operationKey, current)
    updated = replace(selected, handoff=replace(handoff, record=current))
    leg = _selected_leg(updated)
    assert current.preparation is not None
    if current.preparation.legs[-1] != leg:
        refuse("private-preparation-terminal-leg-moved", selected.intent.leg, leg.leg)
    record = observe_preparation_command(
        handoff.contract, handoff.store, current, command, terminal
    )
    return replace(selected, handoff=replace(handoff, record=record))


def _run_once(
    selected: SelectedCloseoutPreparation,
    action: PreparationAction,
    reobserve: ReobservePreparation,
) -> SelectedCloseoutPreparation:
    selected = reobserve(selected)
    leg = _selected_leg(selected)
    assert selected.handoff.record.preparation is not None
    if selected.handoff.record.preparation.legs[-1] != leg:
        refuse("private-preparation-command-leg-moved", selected.intent.leg, leg.leg)
    capability = _capability(selected, reobserve)
    plan = preparation_command(capability.binding, action)
    handoff = selected.handoff
    owner = handoff.record
    if (
        owner.workerPid is None
        or owner.workerLease is None
        or owner.workerProcessFingerprint is None
    ):
        refuse(
            "private-preparation-worker-missing", "actual live worker identity", owner.operationKey
        )
    command = PreparationCommand(
        kind=action,
        cwd=plan.cwd.as_posix(),
        argv=plan.argv,
        workerPid=owner.workerPid,
        workerLease=owner.workerLease,
        workerProcessFingerprint=owner.workerProcessFingerprint,
        startedAt=datetime.now(UTC).isoformat(),
    )
    record = begin_preparation_command(handoff.contract, handoff.store, owner, command)
    selected = replace(selected, handoff=replace(handoff, record=record))
    # Rebinding reopens the selected command before Git; the earlier inspection
    # cannot itself authorize a start after a concurrent cancellation.
    try:
        result = run_git_preparation(_capability(selected, reobserve), action)
    except BaseException as error:
        _terminal_record(
            selected,
            command,
            PreparationCommandTerminal(
                outcome="unknown",
                observedAt=datetime.now(UTC).isoformat(),
                detail=f"Original command did not return a proved terminal: {type(error).__name__}",
            ),
        )
        raise
    selected = _terminal_record(
        selected,
        command,
        PreparationCommandTerminal(
            outcome="succeeded" if result.returncode == 0 else "failed",
            observedAt=datetime.now(UTC).isoformat(),
            returnCode=result.returncode,
            stdoutSha256=hashlib.sha256(
                result.stdout.encode("utf-8", "surrogateescape")
            ).hexdigest(),
            stderrSha256=hashlib.sha256(
                result.stderr.encode("utf-8", "surrogateescape")
            ).hexdigest(),
        ),
    )
    if result.returncode != 0:
        refuse("private-preparation-command-failed", "named-output recovery without rerun", action)
    return reobserve(selected)


def observe_private_output(
    selected: SelectedCloseoutPreparation, *, reobserve: ReobservePreparation
) -> bytes:
    """Prove only the selected original private output; existing legs belong to their owners."""
    selected = reobserve(selected)
    _selected_leg(selected)
    observed = inspect_git_preparation(_capability(selected, reobserve))
    if observed.state != "committed" or observed.raw_commit is None:
        refuse(
            "private-preparation-output-unfinished", "one exact committed output", observed.state
        )
    binding = private_git_binding(selected.intent)
    observe_git_preparation_policy(binding.private_root).require_intent(selected.intent)
    reobserve(selected)
    return observed.raw_commit


def prepare_private_output(
    selected: SelectedCloseoutPreparation, *, reobserve: ReobservePreparation
) -> tuple[SelectedCloseoutPreparation, bytes]:
    """Complete only an unstarted command suffix; uncertain original starts never repeat."""
    selected = reobserve(selected)
    binding = private_git_binding(selected.intent)
    if binding.private_root.parent.resolve() != binding.private_root.parent:
        refuse(
            "private-preparation-namespace-unsafe",
            "canonical selected namespace",
            binding.private_root,
        )
    # The owning workflow selected this exact intent before any namespace creation.
    leg = _selected_leg(selected)
    binding.private_root.parent.mkdir(parents=True, exist_ok=True)
    if leg.output is None:
        for action in ("create", "materialize", "commit"):
            prior = next(
                (item for item in _selected_leg(selected).commands if item.kind == action), None
            )
            if prior is not None:
                if prior.terminal is not None and prior.terminal.outcome == "succeeded":
                    continue
                if action == "commit" and prior.terminal is not None:
                    # A failed/unknown original commit may have produced its real
                    # object. Inspect that exact output; never repeat Git.
                    break
                refuse(
                    "private-preparation-command-unresolved",
                    "original named-output recovery",
                    action,
                )
            selected = _run_once(selected, action, reobserve)
    raw = observe_private_output(selected, reobserve=reobserve)
    return reobserve(selected), raw
