"""Application entry points for worktree-backed MCP tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents_remember.application.completion_cleanup import auto_complete_seats
from agents_remember.application.task_docs.task_ref import TaskRef
from agents_remember.errors import (
    CuratorCoherenceError,
    MemoryCandidatePairError,
    TaskIntentError,
)
from agents_remember.kernel.authority import require_repo, require_within_coordination
from agents_remember.kernel.primitives.runtime_config import (
    DEFAULT_PROVIDER_SETUP_SECONDS,
    McpRuntimeConfig,
    RepositoryScope,
    reload_provider_authority,
)
from agents_remember.models.closeout.input import CloseoutCorrectedCall, EffectiveCloseoutInput
from agents_remember.models.declared_caller import DeclaredCaller
from agents_remember.models.lifecycles.operation import (
    GatePolicyRuleSnapshot,
    IntegrateOperationInput,
    IntegrateStrategy,
    LifecycleOperationKind,
    LifecycleOperationProjection,
)
from agents_remember.models.lifecycles.responses import TerminalState
from agents_remember.models.worktree import MemorySyncChoice, SyncResolutionAction
from agents_remember.observer.ambient import AmbientLifecycle, ambient
from agents_remember.observer.save_gate import coerce_save_decision
from agents_remember.observer.ulid import new_ulid
from agents_remember.providers.lifecycle.log_capture import summarize_command_logs
from agents_remember.providers.settings import write_lifecycle_settings
from agents_remember.worktrees import git_worktree_manager
from agents_remember.worktrees.closeout_input import (
    CloseoutInputError,
    capture_closeout_candidate,
    corrected_closeout_arguments,
    normalize_closeout_input,
    raw_closeout_messages,
    resolve_closeout_plan,
)
from agents_remember.worktrees.integration.closeout.memory_candidate_pairing import (
    memory_candidate_pair_payload,
    resolve_closeout_memory_pair,
)
from agents_remember.worktrees.integration.closeout.operation_admission import (
    CloseoutOperationAdmission,
    prevalidate_closeout_operation_admission,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_controls import (
    LifecycleControlCommand,
    LifecycleControlError,
    control_operation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocationError,
    require_matching_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_read_decision import (
    lifecycle_journal_read_decision,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_request import (
    LifecycleControlRequestError,
    LifecycleControlRequestShape,
    validate_lifecycle_control_request,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationReadError,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operations import (
    start_or_observe_closeout_operation,
    start_or_observe_operation,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)
from agents_remember.worktrees.integration.lifecycle.observation.projection import (
    current_operation_projections,
)
from agents_remember.worktrees.sync_transaction_state import observe_sync_operation
from agents_remember.worktrees.worktree_contract import (
    ContractError,
    WorktreeContract,
    load_contract,
)

from .lifecycle.configured_contract_admission import (
    ConfiguredContractAccepted,
    ConfiguredContractRefused,
    TerminalConfiguredContractAccepted,
    admit_configured_contract,
    admit_configured_terminal_contract,
    execute_configured_contract_operation,
    project_configured_contract_refusal,
)
from .lifecycle.lifecycle_control_authority import (
    LifecycleCallerError,
    completed_disposition_authorized,
    require_completed_disposition_authority,
    resolve_lifecycle_caller,
)
from .lifecycle.lifecycle_operation_location import (
    LifecycleOperationPublicAddress,
    configured_lifecycle_operation_location,
    location_decision_payload,
    unreadable_operation_refusal,
    unreadable_status_operations,
)
from .worktree_tool_requests import (
    DEFAULT_START_EXECUTION,
    DEFAULT_TASK_BASES,
    NO_TASK_DOCS,
    PREVIEW_ONLY,
    CloseoutApproval,
    CloseoutCommitMessages,
    FinalizeTaskDocs,
    OperationControlRequest,
    StartExecution,
    TaskBases,
    TaskIdentity,
)


def worktree_start_tool(
    config: McpRuntimeConfig,
    identity: TaskIdentity,
    *,
    bases: TaskBases = DEFAULT_TASK_BASES,
    execution: StartExecution = DEFAULT_START_EXECUTION,
) -> dict[str, Any]:
    repo = require_repo(config, identity.repo_id)
    amb = ambient()
    if amb is not None and amb.current is not None and not amb.current.fleeting:
        return {
            "ok": False,
            "state": "lifecycle-switch-required",
            "summary": (
                "worktree_start refuses to repoint the active persistent lifecycle; switch "
                "away from it to a fresh fleeting lifecycle, then retry worktree_start"
            ),
            "nextOperation": "switch_task_lifecycle",
            "nextTool": "switch_lifecycle",
            "nextArgs": {},
            "nextStep": {
                "summary": "Switch to a fresh fleeting lifecycle, then retry worktree_start.",
                "nextOperation": "switch_task_lifecycle",
                "nextTool": "switch_lifecycle",
                "nextArgs": {},
            },
        }
    # worktree_start promotes the active lifecycle to persistent (design §1.3); with
    # no active lifecycle, mint a fresh anchor so the contract always carries one.
    lifecycle_id = amb.current.id if amb is not None and amb.current is not None else new_ulid()
    # Containment R1 (260707-HFX-L1): the on-disk authority file — not the boot
    # snapshot — decides whether provider setup may launch. An empty (or
    # unreadable: fail-closed) live providers map skips setup outright; the
    # worktree itself is still created. Launch runs on the LIVE providers map.
    authority = None if execution.skip_provider_setup else reload_provider_authority(config)
    settings_path = (
        write_lifecycle_settings(authority.apply(config))
        if authority is not None and authority.providers and authority.error is None
        else None
    )
    provider_setup_config = (
        None
        if settings_path is None
        else git_worktree_manager.WorktreeProviderSetupConfig(
            coordination_root=config.coordination_root,
            settings_path=settings_path,
            seed_source_coordination_root=config.coordination_root,
            # The temp settings file must outlive this entry point's call: the
            # background setup thread reads it and owns the unlink (GitHub #53).
            unlink_settings_after_setup=True,
        )
    )
    args = _worktree_namespace(
        config,
        repo,
        task_name=identity.task_name,
        leaf_id=identity.leaf_id,
        parent_task=identity.parent_task,
        worktree_name=identity.worktree_name,
        lifecycle_id=lifecycle_id,
        workflow_kind=identity.workflow_kind,
        source_branch=bases.source_branch,
        work_branch=bases.work_branch,
        memory_mode=bases.memory_mode,
        memory_choice=bases.memory_choice,
        stale_base_choice=bases.stale_base_choice,
        custom_instruction=None,
        skip_provider_setup=execution.skip_provider_setup,
        retry_provider_setup=execution.retry_provider_setup,
        provider_setup_config=provider_setup_config,
        # Setup-flow bound: the documented timeoutCaps.providerSetupSeconds cap
        # (matching runtime_install), not the docker-control default — the seed
        # export of a large graph is legitimate setup work (GitHub #53/#58).
        provider_timeout=config.timeout_caps.get(
            "providerSetupSeconds", DEFAULT_PROVIDER_SETUP_SECONDS
        ),
        dry_run=execution.dry_run,
    )
    result: dict[str, Any] | None = None
    try:
        result = _worktree_result("worktree_start", git_worktree_manager.start_result(args))
        if authority is not None and (
            authority.error is not None or (config.providers and not authority.providers)
        ):
            # Surface the veto so a stale-snapshot session sees WHY setup was
            # skipped instead of silently diverging from its boot config.
            veto: dict[str, Any] = {
                "source": str(authority.source_path),
                "bootSnapshotProviders": sorted(config.providers),
            }
            if authority.error is not None:
                veto["error"] = authority.error
            result["providersAuthority"] = veto
        _attribute_start(amb, result, identity.repo_id)
        return result
    finally:
        if settings_path is not None and not _settings_owned_by_background(result):
            settings_path.unlink(missing_ok=True)


def _settings_owned_by_background(result: dict[str, Any] | None) -> bool:
    """True when a launched background setup took over the temp settings file."""
    if result is None:
        return False
    providers = result.get("providers")
    return isinstance(providers, dict) and providers.get("state") == "starting"


def summarized_worktree_start_tool(
    config: McpRuntimeConfig,
    identity: TaskIdentity,
    *,
    bases: TaskBases = DEFAULT_TASK_BASES,
    execution: StartExecution = DEFAULT_START_EXECUTION,
) -> dict[str, Any]:
    """Run worktree start and apply command-log reporting policy before transport."""
    return summarize_command_logs(
        worktree_start_tool(config, identity, bases=bases, execution=execution)
    )


def _attribute_start(amb: AmbientLifecycle | None, result: dict[str, Any], repo_id: str) -> None:
    """Promote the active lifecycle into the freshly started worktree (design §1.3).

    Only a clean ``started`` result attributes: the contract now anchors the
    lifecycle. An active lifecycle is promoted in place; with none active the
    minted contract id is adopted as a fresh persistent lifecycle. Re-attaching to
    an existing contract is ``worktree_attach``'s job, not start's.
    """
    if amb is None or result.get("state") != "started":
        return
    enclosure = result.get("enclosure_path") or result.get("contract_path")
    if not isinstance(enclosure, str):
        return
    if amb.current is not None:
        amb.promote(enclosure=enclosure, repo_id=repo_id, scope=repo_id)
        return
    lifecycle_id = result.get("lifecycle_id")
    if isinstance(lifecycle_id, str) and lifecycle_id:
        amb.attach(lifecycle_id, enclosure=enclosure, repo_id=repo_id)


def _attribute_attach(
    amb: AmbientLifecycle | None, result: dict[str, Any], repo_id: str, on_unsaved: str | None
) -> None:
    """Resume the contract's lifecycle on ``worktree_attach`` (design §1.3 table).

    ``attach`` adopts when none is active, no-ops on the same id, auto-pauses a
    persistent current, and routes an unsaved fleeting through the save gate --
    raising ``SaveGateRequired`` when ``on_unsaved`` was not supplied, so unsaved
    work is never dropped silently (the read-only attach already returned).
    """
    if amb is None or result.get("state") != "attached":
        return
    lifecycle_id = result.get("lifecycle_id")
    enclosure = result.get("enclosure_path") or result.get("contract_path")
    if not isinstance(lifecycle_id, str) or not lifecycle_id or not isinstance(enclosure, str):
        return
    decision = coerce_save_decision(on_unsaved) if on_unsaved else None
    amb.attach(lifecycle_id, enclosure=enclosure, repo_id=repo_id, on_unsaved=decision)


def worktree_attach_tool(
    config: McpRuntimeConfig,
    task: TaskRef,
    *,
    on_unsaved: str | None = None,
) -> dict[str, Any]:
    args = _task_ref_namespace(config, task)
    result = _worktree_result("worktree_attach", git_worktree_manager.attach_result(args))
    _attribute_attach(ambient(), result, task.repo_id, on_unsaved)
    return result


def worktree_status_tool(
    config: McpRuntimeConfig,
    task: TaskRef,
    *,
    caller: DeclaredCaller | None = None,
) -> dict[str, Any]:
    args = _task_ref_namespace(config, task)
    result = _worktree_result("worktree_status", git_worktree_manager.status_result(args))
    contract_path = result.get("contract_path")
    if not isinstance(contract_path, str) or not contract_path:
        return result
    requested_path = Path(contract_path)
    sync_operation = None
    try:
        _, sync_location = configured_lifecycle_operation_location(config, requested_path)
        sync_operation = observe_sync_operation(
            sync_location.worktree_group,
            contract_path=requested_path,
        )
    except LifecycleOperationLocationError:
        pass
    if sync_operation is not None:
        result["syncOperation"] = sync_operation.model_dump(mode="json", exclude_none=True)
    return _project_contract_status(config, result, requested_path, caller)


def _project_contract_status(
    config: McpRuntimeConfig,
    result: dict[str, Any],
    path: Path,
    caller: DeclaredCaller | None,
) -> dict[str, Any]:
    terminal = admit_configured_terminal_contract(config, path.as_posix())
    if isinstance(terminal, TerminalConfiguredContractAccepted):
        _project_terminal_contract_status(result, terminal)
        _replace_operation_status(result, [])
        return result
    if isinstance(terminal, ConfiguredContractRefused) and terminal.status.startswith(
        "terminal-archive-"
    ):
        result.update(project_configured_contract_refusal(terminal, operation="worktree_status"))
        _replace_operation_status(result, [])
        return result
    try:
        resolved_caller = resolve_lifecycle_caller(config, caller)
    except LifecycleCallerError as exc:
        return {
            "ok": False,
            "operation": "worktree_status",
            "state": "refused",
            "status": exc.status,
            "detail": exc.detail,
        }
    read_failure = result.get("contractReadFailure")
    if isinstance(read_failure, dict):
        operations = unreadable_status_operations(config, result, path, read_failure)
    else:
        operations = _readable_status_operations(config, result, path, resolved_caller)
    _replace_operation_status(result, operations)
    return result


def _readable_status_operations(
    config: McpRuntimeConfig,
    result: dict[str, Any],
    path: Path,
    resolved_caller: DeclaredCaller | None,
) -> list[LifecycleOperationProjection]:
    try:
        contract = load_contract(path)
        location = require_matching_lifecycle_operation_location(contract)
        return current_operation_projections(
            path,
            allow_completed_disposition=completed_disposition_authorized(
                contract,
                resolved_caller,
            ),
            caller=resolved_caller,
            contract=contract,
            location=location,
        )
    except LifecycleOperationLocationError as exc:
        result.update(location_decision_payload(exc))
        return []
    except (ContractError, OSError, UnicodeError, ValueError) as exc:
        detail = "the canonical worktree contract is unreadable"
        result.update(
            {
                "ok": False,
                "state": "worktree-contract-unreadable",
                "status": "worktree-contract-unreadable",
                "summary": detail,
                "detail": detail,
            }
        )
        return unreadable_status_operations(
            config,
            result,
            path,
            public_failure_evidence(
                stage="contract-read",
                side="contract",
                name=path.name,
                error_type=type(exc).__name__,
                observed={"state": "missing" if not path.exists() else "unreadable"},
            ),
        )


def _replace_operation_status(
    result: dict[str, Any], operations: list[LifecycleOperationProjection]
) -> None:
    result.pop("contractReadFailure", None)
    result.pop("lifecycleOperation", None)
    result["lifecycleOperations"] = [
        operation.model_dump(mode="json", exclude_none=True) for operation in operations
    ]


def _project_terminal_contract_status(
    result: dict[str, Any],
    accepted: TerminalConfiguredContractAccepted,
) -> None:
    authority = accepted.authority
    archive = authority.archive
    completed = authority.state == "cleanup-completed"
    status = "terminal-cleanup-completed" if completed else "terminal-archive-ready"
    result.update(
        {
            "ok": True,
            "state": status,
            "status": status,
            "summary": (
                "Terminal cleanup is complete and the external enclosure archive remains proven."
                if completed
                else "Terminal archive proof is durable; resume the accepted cleanup operation."
            ),
            "terminalArchive": {
                "state": "terminal-archive-proven",
                "cleanupOperation": archive.cleanupOperation,
                "cleanupArguments": archive.cleanupArguments.model_dump(mode="json"),
                "cleanupRequestId": archive.cleanupRequestId,
                "archivePath": accepted.locator.terminalArchivePath,
                "archiveSha256": accepted.locator.terminalArchiveSha256,
                "receiptPath": accepted.locator.terminalReceiptPath,
                "contractState": authority.state,
            },
        }
    )
    if completed:
        result.pop("nextAction", None)
        return
    result.update(
        {
            "nextAction": archive.cleanupOperation,
            "nextTool": archive.cleanupOperation,
            "nextArgs": _terminal_cleanup_next_args(
                accepted.contract_path,
                archive.cleanupArguments.model_dump(mode="json"),
            ),
        }
    )


def _terminal_cleanup_next_args(
    contract_path: Path,
    accepted_arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "contract_path": contract_path.as_posix(),
        "dry_run": False,
        **accepted_arguments,
    }


def _task_ref_namespace(
    config: McpRuntimeConfig, task: TaskRef
) -> git_worktree_manager.WorktreeArgs:
    """Resolve a caller's task reference into the domain's worktree arguments."""
    return _worktree_namespace(
        config,
        require_repo(config, task.repo_id),
        task_name=task.task_name,
        leaf_id=task.leaf_id,
        parent_task=task.parent_task,
        contract_path=require_within_coordination(config, task.contract_path, "contract_path")
        if task.contract_path
        else None,
    )


def worktree_sync_tool(
    config: McpRuntimeConfig,
    *,
    contract_path: str,
    memory_sync_choice: MemorySyncChoice | None = None,
    resolution_action: SyncResolutionAction | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    configured = admit_configured_contract(config, contract_path)
    if isinstance(configured, ConfiguredContractRefused):
        return project_configured_contract_refusal(configured, operation="worktree_sync")
    args = git_worktree_manager.WorktreeArgs(
        contract_path=configured.contract_path,
        memory_sync_choice=memory_sync_choice,
        resolution_action=resolution_action,
        dry_run=dry_run,
    )
    return _worktree_result("worktree_sync", git_worktree_manager.sync_result(args))


def worktree_closeout_preview_tool(
    config: McpRuntimeConfig,
    contract_path: str,
    messages: CloseoutCommitMessages,
) -> dict[str, Any]:
    return _worktree_closeout(
        config,
        operation="worktree_closeout_preview",
        contract_path=contract_path,
        messages=messages,
        approval=PREVIEW_ONLY,
    )


def worktree_closeout_apply_tool(
    config: McpRuntimeConfig,
    contract_path: str,
    messages: CloseoutCommitMessages,
    approval: CloseoutApproval,
) -> dict[str, Any]:
    if approval.dry_run:
        return _worktree_closeout(
            config,
            operation="worktree_closeout_apply",
            contract_path=contract_path,
            messages=messages,
            approval=approval,
        )
    return _start_closeout_operation(config, contract_path, messages, approval)


def _start_closeout_operation(
    config: McpRuntimeConfig,
    contract_path: str,
    messages: CloseoutCommitMessages,
    approval: CloseoutApproval,
) -> dict[str, Any]:
    configured = admit_configured_contract(
        config,
        contract_path,
        require_candidate_identity=False,
    )
    address = LifecycleOperationPublicAddress("worktree_closeout_apply", "closeout")
    if isinstance(configured, ConfiguredContractRefused):
        return project_configured_contract_refusal(
            configured,
            operation=address.operation,
            address=address,
        )
    confined = configured.contract_path
    corrected_arguments = corrected_closeout_arguments(
        confined.as_posix(), intent_note="<developer intent>"
    )
    admission = CloseoutOperationAdmission(
        config_path=config.config_path.as_posix(),
        contract_path=confined,
        messages=raw_closeout_messages(
            code=messages.code,
            memory=messages.memory,
            ledger=messages.ledger,
        ),
        approval_note=approval.intent_note,
        gate_policy=_gate_policy_snapshot(config),
        corrected_call=CloseoutCorrectedCall(
            tool="worktree_closeout_apply",
            arguments=corrected_arguments,
        ),
    )
    try:
        # Input intent is the outermost public boundary.  Reuse the canonical
        # admission normalizer here so blank enabled-leg messages refuse before
        # candidate authority or another lifecycle can influence the result.
        # The lease-owned start repeats this check against current state.
        prevalidate_closeout_operation_admission(configured.contract, admission)
    except (CloseoutInputError, TaskIntentError) as error:
        return _start_operation_refusal(config, confined, address, error)
    try:
        pair_identity = resolve_closeout_memory_pair(configured.contract)
    except MemoryCandidatePairError as error:
        return _memory_candidate_pair_refusal(address.operation, error)
    try:
        execution = execute_configured_contract_operation(
            configured,
            lambda: start_or_observe_closeout_operation(
                admission,
                configured.contract,
            ),
        )
    except (
        CloseoutInputError,
        LifecycleControlError,
        LifecycleOperationReadError,
        TaskIntentError,
    ) as error:
        return _start_operation_refusal(
            config,
            confined,
            address,
            error,
        )
    if isinstance(execution, ConfiguredContractRefused):
        return project_configured_contract_refusal(
            execution,
            operation=address.operation,
            address=address,
        )
    return {
        **_operation_acknowledgement("worktree_closeout_apply", execution),
        **memory_candidate_pair_payload(pair_identity),
    }


def worktree_integrate_tool(
    config: McpRuntimeConfig,
    *,
    contract_path: str,
    strategy: IntegrateStrategy = "ff-only",
    ledger_commit_message: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Start or observe the exact contract-addressed integration operation.

    This task-addressed boundary does not make scheduling decisions or claim a
    closeout door. The operation worker revalidates its exact journal, contract,
    and protected-ref authority immediately before moving source history.
    """

    configured = admit_configured_contract(config, contract_path)
    address = LifecycleOperationPublicAddress("worktree_integrate", "integrate")
    if isinstance(configured, ConfiguredContractRefused):
        return project_configured_contract_refusal(
            configured,
            operation=address.operation,
            address=address,
        )
    confined_contract = configured.contract_path
    if not dry_run:
        try:
            execution = execute_configured_contract_operation(
                configured,
                lambda: start_or_observe_operation(
                    IntegrateOperationInput(
                        configPath=config.config_path.as_posix(),
                        contractPath=confined_contract.as_posix(),
                        strategy=strategy,
                        ledgerCommitMessage=ledger_commit_message,
                        gatePolicy=_gate_policy_snapshot(config),
                        autoCompleteSeats=config.retirement.auto_land_on_integration,
                    ),
                    configured.contract,
                ),
            )
        except (
            LifecycleControlError,
            LifecycleOperationReadError,
        ) as error:
            return _start_operation_refusal(
                config,
                confined_contract,
                address,
                error,
            )
        if isinstance(execution, ConfiguredContractRefused):
            return project_configured_contract_refusal(
                execution,
                operation=address.operation,
                address=address,
            )
        return _operation_acknowledgement("worktree_integrate", execution)
    args = git_worktree_manager.WorktreeArgs(
        contract_path=confined_contract,
        strategy=strategy,
        approved=not dry_run,
        ledger_commit_message=ledger_commit_message,
        dry_run=dry_run,
        # The configured policy MUST reach the seam guard (mirror of the closeout
        # path below): the dataclass default is all-human, which would refuse the
        # exact delegated approval the master-handover channel produces.
        gate_policy=config.orchestration.gate_policy,
    )
    result = _worktree_result(
        "worktree_integrate",
        git_worktree_manager.integrate_result(args, configured.contract),
    )
    if result["ok"] and not dry_run and config.retirement.auto_land_on_integration:
        result.update(
            auto_complete_seats(
                config,
                confined_contract,
                reason="auto-close: leaf integrated into master",
                edge="leaf-integration",
            )
        )
    return result


def worktree_operation_control_tool(
    config: McpRuntimeConfig,
    request: OperationControlRequest,
) -> dict[str, Any]:
    invalid = _operation_control_request_refusal(request)
    if invalid is not None:
        return invalid
    configured = admit_configured_contract(config, request.contract_path)
    if isinstance(configured, ConfiguredContractRefused):
        return _configured_control_refusal(config, request, configured)
    caller_result = _resolve_control_caller(config, configured.contract, request)
    if isinstance(caller_result, dict):
        return caller_result
    caller, disposition_authorized = caller_result
    return _execute_operation_control(
        config,
        request,
        configured,
        caller,
        disposition_authorized=disposition_authorized,
    )


def _operation_control_request_refusal(
    request: OperationControlRequest,
) -> dict[str, Any] | None:
    try:
        validate_lifecycle_control_request(
            LifecycleControlRequestShape(
                action=request.action,
                expected_generation=request.expected_generation,
                intent_note=request.intent_note,
                commit_messages={
                    "code_commit_message": request.code_commit_message,
                    "memory_commit_message": request.memory_commit_message,
                    "ledger_commit_message": request.ledger_commit_message,
                },
                has_grade=request.grade is not None,
                has_admission=request.admission is not None,
            )
        )
    except LifecycleControlRequestError as error:
        return {
            "ok": False,
            "operation": "worktree_operation_control",
            "state": "refused",
            "status": error.status,
            "detail": error.detail,
            "expected": error.expected,
            "observed": error.observed,
            "nextAction": "correct-request",
        }
    return None


def _configured_control_refusal(
    config: McpRuntimeConfig,
    request: OperationControlRequest,
    configured: ConfiguredContractRefused,
) -> dict[str, Any]:
    if configured.reason in {"authority-invalid", "contract-unreadable"}:
        try:
            resolve_lifecycle_caller(config, request.caller)
        except LifecycleCallerError as exc:
            return {
                "ok": False,
                "operation": "worktree_operation_control",
                "state": "refused",
                "status": exc.status,
                "detail": exc.detail,
            }
    return project_configured_contract_refusal(
        configured,
        operation="worktree_operation_control",
        address=LifecycleOperationPublicAddress(
            "worktree_operation_control",
            request.operation_kind,
            request.expected_generation,
        ),
    )


def _resolve_control_caller(
    config: McpRuntimeConfig,
    contract: WorktreeContract,
    request: OperationControlRequest,
) -> tuple[DeclaredCaller | None, bool] | dict[str, Any]:
    try:
        caller = resolve_lifecycle_caller(config, request.caller)
        if request.action in {"retire", "supersede"}:
            require_completed_disposition_authority(contract, caller)
        return caller, completed_disposition_authorized(contract, caller)
    except LifecycleCallerError as exc:
        return {
            "ok": False,
            "operation": "worktree_operation_control",
            "state": "refused",
            "status": exc.status,
            "detail": exc.detail,
            "expected": {},
            "observed": {},
            "nextAction": "developer-decision",
            "developerDecisionRequired": True,
            "decisionSurface": exc.detail,
        }


def _execute_operation_control(
    config: McpRuntimeConfig,
    request: OperationControlRequest,
    configured: ConfiguredContractAccepted,
    caller: DeclaredCaller | None,
    *,
    disposition_authorized: bool,
) -> dict[str, Any]:
    confined = configured.contract_path
    revision_messages = (
        raw_closeout_messages(
            code=request.code_commit_message,
            memory=request.memory_commit_message,
            ledger=request.ledger_commit_message,
        )
        if request.action == "revise"
        else None
    )
    contract = configured.contract
    try:
        execution = execute_configured_contract_operation(
            configured,
            lambda: control_operation(
                LifecycleControlCommand(
                    admitted_contract=contract,
                    admitted_location=configured.location,
                    configured_authority=config.config_path.as_posix(),
                    kind=request.operation_kind,
                    action=request.action,
                    expected_generation=request.expected_generation,
                    intent_note=request.intent_note,
                    dry_run=request.dry_run,
                    revision_messages=revision_messages,
                    revision_gate_policy=(
                        _gate_policy_snapshot(config) if request.action == "revise" else None
                    ),
                    supersede_grade=request.grade,
                    supersede_admission=request.admission,
                    allow_completed_disposition=disposition_authorized,
                    caller=caller,
                )
            ),
        )
    except LifecycleControlError as exc:
        return {
            "ok": False,
            "operation": "worktree_operation_control",
            "state": "refused",
            "status": exc.status,
            "detail": exc.detail,
            **exc.response_fields(
                contract_path=confined.as_posix(),
                kind=request.operation_kind,
                generation=request.expected_generation,
                caller=caller,
            ),
        }
    except LifecycleOperationReadError as exc:
        return _journal_read_refusal(
            "worktree_operation_control",
            request.operation_kind,
            exc,
        )
    if isinstance(execution, ConfiguredContractRefused):
        return project_configured_contract_refusal(
            execution,
            operation="worktree_operation_control",
            address=LifecycleOperationPublicAddress(
                "worktree_operation_control",
                request.operation_kind,
                request.expected_generation,
            ),
        )
    return _operation_acknowledgement("worktree_operation_control", execution)


def _journal_read_refusal(
    operation: str,
    kind: LifecycleOperationKind,
    error: LifecycleOperationReadError,
) -> dict[str, Any]:
    decision = lifecycle_journal_read_decision(kind, error)
    payload = decision.payload()
    return {
        "ok": False,
        "operation": operation,
        "state": "refused",
        "status": decision.status,
        "detail": decision.detail,
        **{key: value for key, value in payload.items() if key != "state"},
    }


def _start_operation_refusal(
    config: McpRuntimeConfig,
    contract_path: Path,
    address: LifecycleOperationPublicAddress,
    error: Exception,
) -> dict[str, Any]:
    """Translate one start/admission failure without duplicating route classifiers."""

    if isinstance(error, CloseoutInputError):
        return _closeout_input_refusal(address.operation, error)
    if isinstance(error, TaskIntentError):
        return {
            "ok": False,
            "operation": address.operation,
            "state": "refused",
            "status": error.status,
            "detail": error.detail,
            "nextAction": error.next_action,
        }
    if isinstance(error, LifecycleControlError):
        return {
            "ok": False,
            "operation": address.operation,
            "state": "refused",
            "status": error.status,
            "detail": error.detail,
            **error.response_fields(
                contract_path=contract_path.as_posix(),
                kind=address.kind,
                generation=address.generation or 0,
            ),
        }
    if isinstance(error, LifecycleOperationReadError):
        return _journal_read_refusal(address.operation, address.kind, error)
    return unreadable_operation_refusal(config, contract_path, address, error)


def _gate_policy_snapshot(config: McpRuntimeConfig) -> list[GatePolicyRuleSnapshot]:
    return [
        GatePolicyRuleSnapshot(
            kind=rule.kind,
            delegatedRole=rule.delegated_role,
            requireReviewerVerdict=rule.require_reviewer_verdict,
        )
        for rule in config.orchestration.gate_policy.rules
    ]


def _operation_acknowledgement(
    operation: str, projection: LifecycleOperationProjection
) -> dict[str, Any]:
    payload = projection.model_dump(mode="json", exclude_none=True)
    return {
        "ok": True,
        "operation": operation,
        "state": payload["status"],
        "summary": (
            f"{payload['kind']} is {payload['status']}; poll worktree_status for task-bound "
            "progress. No job or process identifier is required."
        ),
        "pollTool": "worktree_status",
        "lifecycleOperation": payload,
    }


def worktree_cleanup_tool(
    config: McpRuntimeConfig,
    *,
    contract_path: str,
    dry_run: bool = False,
    teardown_providers: bool = True,
) -> dict[str, Any]:
    configured = admit_configured_terminal_contract(config, contract_path)
    if isinstance(configured, ConfiguredContractRefused):
        return project_configured_contract_refusal(configured, operation="worktree_cleanup")
    args = git_worktree_manager.WorktreeArgs(
        contract_path=configured.contract_path,
        approved=not dry_run,
        dry_run=dry_run,
        teardown_providers=teardown_providers,
    )
    return _worktree_result("worktree_cleanup", git_worktree_manager.cleanup_result(args))


def worktree_abandon_tool(
    config: McpRuntimeConfig,
    *,
    contract_path: str,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    configured = admit_configured_terminal_contract(config, contract_path)
    if isinstance(configured, ConfiguredContractRefused):
        return project_configured_contract_refusal(configured, operation="worktree_abandon")
    args = git_worktree_manager.WorktreeArgs(
        contract_path=configured.contract_path,
        approved=not dry_run,
        dry_run=dry_run,
        force=force,
    )
    result = _worktree_result("worktree_abandon", git_worktree_manager.abandon_result(args))
    # End the ambient lifecycle when it anchors the abandoned worktree — the owner-written
    # lifecycle.ended (L11). A lifecycle whose owner is gone (e.g. the server restarted) is
    # terminalized by the reader instead: the reducer projects `abandoned` from the
    # contract's cleanup field, honoring the store's single-writer invariant.
    if not dry_run and result.get("state") == "abandoned":
        end_ambient_lifecycle_if_anchored(
            str(result.get("lifecycle_id") or ""), outcome="abandoned"
        )
    return result


def end_ambient_lifecycle_if_anchored(lifecycle_id: str, *, outcome: TerminalState) -> None:
    """End the process-owned ambient when one task transition retires its anchor."""
    amb = ambient()
    if not lifecycle_id or amb is None:
        return
    current = amb.current
    if current is not None and current.id == lifecycle_id:
        amb.end(outcome)


def lifecycle_finalize_task_tool(
    config: McpRuntimeConfig,
    contract_path: str,
    *,
    docs: FinalizeTaskDocs = NO_TASK_DOCS,
    dry_run: bool = False,
    teardown_providers: bool = True,
) -> dict[str, Any]:
    confined_contract = require_within_coordination(config, contract_path, "contract_path")
    args = git_worktree_manager.FinalizeArgs(
        contract_path=confined_contract,
        task_doc_path=require_within_coordination(config, docs.task_doc_path, "task_doc_path")
        if docs.task_doc_path
        else None,
        master_doc_path=require_within_coordination(config, docs.master_doc_path, "master_doc_path")
        if docs.master_doc_path
        else None,
        subtask_number=docs.subtask_number,
        dry_run=dry_run,
        teardown_providers=teardown_providers,
    )
    result = _worktree_result("lifecycle_finalize_task", git_worktree_manager.finalize_result(args))
    if result["ok"] and not dry_run and config.retirement.auto_land_on_finalize:
        result.update(
            auto_complete_seats(
                config,
                confined_contract,
                reason="auto-close: master finalized into super",
                edge="master-finalization",
            )
        )
    return result


def _worktree_namespace(
    config: McpRuntimeConfig,
    repo: RepositoryScope,
    **kwargs: Any,
) -> git_worktree_manager.WorktreeArgs:
    values: dict[str, Any] = {
        "code_repository_name": repo.repo_id,
        "workspace_root": config.workspace_root,
        "coordination_root": config.coordination_root,
        "code_repository_root": repo.path,
        "topology": None,
        "contract_path": None,
        "task_name": None,
    }
    values.update(kwargs)
    return git_worktree_manager.WorktreeArgs(**values)


def _worktree_result(
    operation: str, result: git_worktree_manager.WorktreeCommandResult
) -> dict[str, Any]:
    return {**result.payload, "ok": result.returncode == 0, "operation": operation}


def _worktree_closeout(
    config: McpRuntimeConfig,
    *,
    operation: str,
    contract_path: str,
    messages: CloseoutCommitMessages,
    approval: CloseoutApproval,
) -> dict[str, Any]:
    configured = admit_configured_contract(
        config,
        contract_path,
        require_candidate_identity=False,
    )
    address = LifecycleOperationPublicAddress(operation, "closeout")
    if isinstance(configured, ConfiguredContractRefused):
        return project_configured_contract_refusal(
            configured,
            operation=operation,
            address=address,
        )
    confined_contract = configured.contract_path
    corrected_arguments = corrected_closeout_arguments(confined_contract.as_posix())
    if operation == "worktree_closeout_apply":
        corrected_arguments.update(intent_note="<developer intent>", dry_run=True)
    try:
        effective_input = _normalize_worktree_closeout(
            configured.contract,
            messages,
            tool_name=operation,
            corrected_arguments=corrected_arguments,
        )
    except CloseoutInputError as exc:
        return _closeout_input_refusal(operation, exc)
    args = git_worktree_manager.WorktreeArgs(
        contract_path=confined_contract,
        closeout_input=effective_input,
        approval_note=approval.intent_note,
        approved=not approval.dry_run,
        dry_run=approval.dry_run,
        gate_policy=config.orchestration.gate_policy,
    )
    try:
        result = git_worktree_manager.closeout_result(args, configured.contract)
    except CuratorCoherenceError as error:
        return {
            "ok": False,
            "operation": operation,
            "state": "refused",
            "contractPath": confined_contract.as_posix(),
            **error.response_fields(),
        }
    return _worktree_result(operation, result)


def _memory_candidate_pair_refusal(
    operation: str,
    error: MemoryCandidatePairError,
) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": operation,
        "state": "refused",
        "status": error.status,
        **error.response_fields(),
    }


def _normalize_worktree_closeout(
    contract: WorktreeContract,
    messages: CloseoutCommitMessages,
    *,
    tool_name: str,
    corrected_arguments: dict[str, object],
) -> EffectiveCloseoutInput:
    plan = resolve_closeout_plan(
        contract,
        route="worktree",
        candidate=capture_closeout_candidate(contract),
    )
    return normalize_closeout_input(
        contract,
        raw_closeout_messages(code=messages.code, memory=messages.memory, ledger=messages.ledger),
        route="worktree",
        corrected_call=CloseoutCorrectedCall(
            tool=tool_name,
            arguments=corrected_arguments,
        ),
        resolved_plan=plan,
    )


def _closeout_input_refusal(operation: str, error: CloseoutInputError) -> dict[str, Any]:
    return {
        "ok": False,
        "operation": operation,
        "state": "refused",
        "status": error.status,
        "detail": "closeout input is invalid; use the corrected call",
        **error.response_fields(),
    }
