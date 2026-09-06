"""Public configured-contract consumers share one closed semantic admission."""

from __future__ import annotations

import ast
import inspect
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal
from unittest import mock

import pytest
from agents_remember.application import worktree_tools
from agents_remember.application.lifecycle import direct_landing as direct_app
from agents_remember.application.lifecycle.configured_contract_admission import (
    ConfiguredContractAccepted,
    ConfiguredContractRefused,
    admit_configured_contract,
)
from agents_remember.application.worktree_tools import (
    CloseoutApproval,
    CloseoutCommitMessages,
    OperationControlRequest,
)
from agents_remember.errors import ConfiguredContractAuthorityError
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.worktrees import direct_landing as direct_domain
from agents_remember.worktrees.direct_landing import DirectLandingRequest
from agents_remember.worktrees.integration import configured_contract_authority as authority_mod
from agents_remember.worktrees.integration.lifecycle import lifecycle_operations as operations_mod
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_location import (
    LifecycleOperationLocationError,
    lifecycle_operation_locator_path,
    resolve_lifecycle_operation_location,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_public_evidence import (
    public_failure_evidence,
)
from agents_remember.worktrees.modules import closeout as closeout_mod
from agents_remember.worktrees.modules import integrate as integrate_mod
from closeout_input_test_support import ensure_fixture_waiting_door
from test_direct_landing import _series_fixture
from test_lifecycle_operations import _contract

Consumer = Literal[
    "sync",
    "closeout-preview",
    "closeout-apply",
    "integrate-preview",
    "integrate-apply",
    "control",
    "cleanup",
    "abandon",
    "direct",
]
RefusalKind = Literal["address", "location", "unreadable", "authority"]


def _byte_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _fixture(tmp_path: Path):
    contract, _ = ensure_fixture_waiting_door(_contract(tmp_path))
    config = replace(
        load_config(tmp_path / "settings.json"),
        direct_execution_enabled=True,
    )
    location = resolve_lifecycle_operation_location(
        contract.coordination_root,
        contract.contract_path,
    )
    return contract, config, location


def _refusal(contract, location, kind: RefusalKind) -> ConfiguredContractRefused:
    if kind == "address":
        return ConfiguredContractRefused(
            reason="address-invalid",
            status="configured-contract-address-invalid",
            detail="configured contract address is invalid",
            expected={"contractAddress": "confined under coordinationRoot"},
            observed={"state": "invalid"},
        )
    if kind == "location":
        return ConfiguredContractRefused(
            reason="location-invalid",
            status="operation-location-mismatch",
            detail="configured locator and enclosure manifest disagree",
            expected={"state": "addressable"},
            observed={"state": "mismatch"},
        )
    if kind == "authority":
        return ConfiguredContractRefused(
            reason="authority-invalid",
            status="configured-contract-authority-invalid",
            detail="configured repository authority does not match",
            expected={
                "contractPath": contract.contract_path.as_posix(),
                "repositoryAuthority": "configured",
            },
            observed=public_failure_evidence(
                stage="contract-authority",
                side="code",
                name="repository",
                error_type="ConfiguredContractAuthorityError",
                observed={"state": "mismatch"},
            ),
            contract_path=contract.contract_path,
            location=location,
        )
    return ConfiguredContractRefused(
        reason="contract-unreadable",
        status="configured-contract-unreadable",
        detail="configured contract is unreadable",
        expected={
            "contractPath": contract.contract_path.as_posix(),
            "route": "locator -> root manifest -> root journal",
        },
        observed=public_failure_evidence(
            stage="contract-read",
            side="contract",
            name=contract.contract_path.name,
            error_type="PermissionError",
            observed={"state": "unreadable"},
        ),
        contract_path=contract.contract_path,
        location=location,
    )


def _call_consumer(consumer: Consumer, config, contract) -> dict[str, Any]:
    path = contract.contract_path.as_posix()
    result: dict[str, Any]
    if consumer == "sync":
        result = worktree_tools.worktree_sync_tool(config, contract_path=path, dry_run=True)
    elif consumer == "closeout-preview":
        result = worktree_tools.worktree_closeout_preview_tool(
            config, path, CloseoutCommitMessages(code="preview")
        )
    elif consumer == "closeout-apply":
        result = worktree_tools.worktree_closeout_apply_tool(
            config,
            path,
            CloseoutCommitMessages(code="apply"),
            CloseoutApproval(intent_note="approve"),
        )
    elif consumer == "integrate-preview":
        result = worktree_tools.worktree_integrate_tool(config, contract_path=path, dry_run=True)
    elif consumer == "integrate-apply":
        result = worktree_tools.worktree_integrate_tool(config, contract_path=path)
    elif consumer == "control":
        result = worktree_tools.worktree_operation_control_tool(
            config,
            OperationControlRequest(
                contract_path=path,
                operation_kind="closeout",
                action="cancel",
                expected_generation=1,
                intent_note="stop exact generation",
            ),
        )
    elif consumer == "cleanup":
        result = worktree_tools.worktree_cleanup_tool(config, contract_path=path, dry_run=True)
    elif consumer == "abandon":
        result = worktree_tools.worktree_abandon_tool(config, contract_path=path, dry_run=True)
    else:
        assert consumer == "direct"
        result = direct_app.direct_landing_tool(
            config,
            DirectLandingRequest(
                contract_path=path,
                code_commit="1" * 40,
                memory_commit_message="direct memory",
                ledger_commit_message="direct ledger",
                intent_note="approve direct landing",
            ),
        )
    return result


@pytest.mark.parametrize(
    "consumer",
    [
        "sync",
        "closeout-preview",
        "closeout-apply",
        "integrate-preview",
        "integrate-apply",
        "control",
        "cleanup",
        "abandon",
        "direct",
    ],
)
@pytest.mark.parametrize("kind", ["address", "location", "unreadable", "authority"])
def test_every_public_consumer_exhaustively_refuses_each_semantic_category(
    tmp_path: Path,
    consumer: Consumer,
    kind: RefusalKind,
) -> None:
    contract, config, location = _fixture(tmp_path)
    refusal = _refusal(contract, location, kind)
    before = _byte_tree(tmp_path)
    worktree_admission = mock.patch.object(
        worktree_tools,
        "admit_configured_contract",
        return_value=refusal,
    )
    terminal_admission = mock.patch.object(
        worktree_tools,
        "admit_configured_terminal_contract",
        return_value=refusal,
    )
    direct_admission = mock.patch.object(
        direct_app,
        "admit_configured_contract",
        return_value=refusal,
    )
    direct_mutation = mock.patch.object(
        direct_app,
        "direct_landing",
        side_effect=AssertionError("direct landing mutated after refusal"),
    )
    with (
        worktree_admission,
        terminal_admission,
        direct_admission,
        mock.patch.object(
            worktree_tools.git_worktree_manager,
            "sync_result",
            side_effect=AssertionError("sync mutated after refusal"),
        ),
        mock.patch.object(
            worktree_tools.git_worktree_manager,
            "closeout_result",
            side_effect=AssertionError("closeout mutated after refusal"),
        ),
        mock.patch.object(
            worktree_tools,
            "start_or_observe_closeout_operation",
            side_effect=AssertionError("closeout journal mutated after refusal"),
        ),
        mock.patch.object(
            worktree_tools.git_worktree_manager,
            "integrate_result",
            side_effect=AssertionError("integration preview mutated after refusal"),
        ),
        mock.patch.object(
            worktree_tools,
            "start_or_observe_operation",
            side_effect=AssertionError("integration journal mutated after refusal"),
        ),
        mock.patch.object(
            worktree_tools,
            "control_operation",
            side_effect=AssertionError("control mutated after refusal"),
        ),
        mock.patch.object(
            worktree_tools.git_worktree_manager,
            "cleanup_result",
            side_effect=AssertionError("cleanup mutated after refusal"),
        ),
        mock.patch.object(
            worktree_tools.git_worktree_manager,
            "abandon_result",
            side_effect=AssertionError("abandon mutated after refusal"),
        ),
        direct_mutation,
    ):
        result = _call_consumer(consumer, config, contract)

    assert result["ok"] is False
    assert result["nextAction"] == "developer-decision"
    assert result["developerDecisionRequired"] is True
    assert "nextTool" not in result
    assert "nextArgs" not in result
    assert _byte_tree(tmp_path) == before


def test_real_admission_classifies_expected_failures_and_leaves_unexpected_faults_loud(
    tmp_path: Path,
) -> None:
    contract, config, _location = _fixture(tmp_path)
    before = _byte_tree(tmp_path)
    accepted = admit_configured_contract(config, contract.contract_path)
    assert isinstance(accepted, ConfiguredContractAccepted)

    outside = admit_configured_contract(config, tmp_path / "outside-contract.md")
    assert isinstance(outside, ConfiguredContractRefused)
    assert outside.reason == "address-invalid"

    with mock.patch(
        "agents_remember.application.lifecycle.configured_contract_admission."
        "require_configured_contract_repositories",
        side_effect=ConfiguredContractAuthorityError(side="code", name="repository"),
    ):
        authority = admit_configured_contract(config, contract.contract_path)
    assert isinstance(authority, ConfiguredContractRefused)
    assert authority.reason == "authority-invalid"

    with mock.patch(
        "agents_remember.application.lifecycle.configured_contract_admission.load_contract",
        side_effect=PermissionError("PRIVATE_CONTRACT_READ_DETAIL"),
    ):
        unreadable = admit_configured_contract(config, contract.contract_path)
    assert isinstance(unreadable, ConfiguredContractRefused)
    assert unreadable.reason == "contract-unreadable"
    assert "PRIVATE_CONTRACT_READ_DETAIL" not in repr(unreadable)

    with (
        mock.patch(
            "agents_remember.application.lifecycle.configured_contract_admission.load_contract",
            side_effect=RuntimeError("programmer fault sentinel"),
        ),
        pytest.raises(RuntimeError, match="programmer fault sentinel"),
    ):
        admit_configured_contract(config, contract.contract_path)
    assert _byte_tree(tmp_path) == before

    locator_path = lifecycle_operation_locator_path(
        contract.coordination_root,
        contract.contract_path,
    )
    locator_path.unlink()
    location = admit_configured_contract(config, contract.contract_path)
    assert isinstance(location, ConfiguredContractRefused)
    assert location.reason == "location-invalid"


def test_candidate_repository_identity_is_strict_on_both_external_leaf_sides() -> None:
    contract = mock.Mock(
        code_worktree=Path("/candidate-code"),
        memory_mode="external",
        kind="leaf",
        memory_worktree=Path("/candidate-memory"),
    )
    configured = mock.Mock()
    code_identity = Path("/code-repository")
    memory_identity = Path("/memory-repository")
    configured_authority = mock.patch.object(
        authority_mod,
        "_require_configured_repository_authority",
        return_value=(configured, code_identity),
    )
    memory_authority = mock.patch.object(
        authority_mod,
        "_require_external_memory_repository_authority",
        return_value=memory_identity,
    )

    with (
        configured_authority,
        memory_authority,
        mock.patch.object(
            authority_mod,
            "repository_identity",
            return_value=Path("/foreign-code-repository"),
        ),
        pytest.raises(ConfiguredContractAuthorityError) as code,
    ):
        authority_mod.require_configured_contract_repositories(contract, "/config.json")
    assert (code.value.side, code.value.name) == ("code", "candidate")

    with (
        mock.patch.object(
            authority_mod,
            "_require_configured_repository_authority",
            return_value=(configured, code_identity),
        ),
        mock.patch.object(
            authority_mod,
            "_require_external_memory_repository_authority",
            return_value=memory_identity,
        ),
        mock.patch.object(
            authority_mod,
            "repository_identity",
            side_effect=[code_identity, Path("/foreign-memory-repository")],
        ),
        pytest.raises(ConfiguredContractAuthorityError) as memory,
    ):
        authority_mod.require_configured_contract_repositories(contract, "/config.json")
    assert (memory.value.side, memory.value.name) == ("memory", "candidate")


@pytest.mark.parametrize(
    ("operation", "kind"),
    [
        ("worktree_closeout_apply", "closeout"),
        ("worktree_integrate", "integrate"),
        ("worktree_operation_control", "closeout"),
        ("direct_landing", "direct-landing"),
    ],
)
def test_mutation_time_configured_authority_change_uses_the_same_public_projector(
    tmp_path: Path,
    operation: str,
    kind: Literal["closeout", "integrate", "direct-landing"],
) -> None:
    contract, config, _location = _fixture(tmp_path)
    if operation == "direct_landing":
        direct_fixture = _series_fixture(tmp_path / "direct")
        contract = direct_fixture["contract"]
        config = direct_fixture["config"]
    error = ConfiguredContractAuthorityError(side="code", name="repository")
    with mock.patch.object(
        authority_mod,
        "require_configured_contract_repositories",
        side_effect=error,
    ):
        if operation == "worktree_closeout_apply":
            result = worktree_tools.worktree_closeout_apply_tool(
                config,
                contract.contract_path.as_posix(),
                CloseoutCommitMessages(code="close"),
                CloseoutApproval(intent_note="approve"),
            )
        elif operation == "worktree_integrate":
            result = worktree_tools.worktree_integrate_tool(
                config,
                contract_path=contract.contract_path.as_posix(),
            )
        elif operation == "worktree_operation_control":
            result = worktree_tools.worktree_operation_control_tool(
                config,
                OperationControlRequest(
                    contract_path=contract.contract_path.as_posix(),
                    operation_kind="closeout",
                    action="cancel",
                    expected_generation=1,
                    intent_note="stop exact generation",
                ),
            )
        else:
            result = direct_app.direct_landing_tool(
                config,
                DirectLandingRequest(
                    contract_path=contract.contract_path.as_posix(),
                    code_commit="1" * 40,
                    memory_commit_message="direct memory",
                    ledger_commit_message="direct ledger",
                    intent_note="approve",
                ),
            )

    assert result["status"] == f"{kind}-contract-invalid"
    assert result["nextAction"] == "developer-decision"
    observed = result["observed"]
    assert isinstance(observed, dict)
    observed_facts = observed["observed"]
    assert isinstance(observed_facts, dict)
    assert observed_facts["state"] == "mismatch"


@pytest.mark.parametrize(
    ("operation", "kind"),
    [
        ("worktree_closeout_apply", "closeout"),
        ("worktree_integrate", "integrate"),
        ("worktree_operation_control", "closeout"),
        ("direct_landing", "direct-landing"),
    ],
)
@pytest.mark.parametrize("seam", ["contract-read", "location"])
def test_real_post_admission_lower_failures_share_the_public_projector(
    tmp_path: Path,
    operation: str,
    kind: Literal["closeout", "integrate", "direct-landing"],
    seam: str,
) -> None:
    contract, config, _location = _fixture(tmp_path)
    if operation == "direct_landing":
        direct_fixture = _series_fixture(tmp_path / "direct")
        contract = direct_fixture["contract"]
        config = direct_fixture["config"]
    before = _byte_tree(tmp_path)
    if seam == "contract-read":
        cut = mock.patch.object(
            authority_mod,
            "load_contract",
            side_effect=PermissionError("PRIVATE_REREAD_SENTINEL"),
        )
    else:
        cut = mock.patch.object(
            authority_mod,
            "require_matching_lifecycle_operation_location",
            side_effect=LifecycleOperationLocationError(
                "operation-location-mismatch",
                "configured locator and enclosure manifest disagree",
                expected={"state": "addressable"},
                observed={"state": "mismatch"},
            ),
        )
    with (
        cut,
        mock.patch.object(operations_mod, "launch_detached_worker") as launch,
        mock.patch.object(direct_app, "_direct_recovery_action") as direct_recovery,
    ):
        if operation == "worktree_closeout_apply":
            result = worktree_tools.worktree_closeout_apply_tool(
                config,
                contract.contract_path.as_posix(),
                CloseoutCommitMessages(code="close"),
                CloseoutApproval(intent_note="approve"),
            )
        elif operation == "worktree_integrate":
            result = worktree_tools.worktree_integrate_tool(
                config,
                contract_path=contract.contract_path.as_posix(),
            )
        elif operation == "worktree_operation_control":
            result = worktree_tools.worktree_operation_control_tool(
                config,
                OperationControlRequest(
                    contract_path=contract.contract_path.as_posix(),
                    operation_kind="closeout",
                    action="cancel",
                    expected_generation=1,
                    intent_note="stop exact generation",
                ),
            )
        else:
            result = direct_app.direct_landing_tool(
                config,
                DirectLandingRequest(
                    contract_path=contract.contract_path.as_posix(),
                    code_commit="1" * 40,
                    memory_commit_message="direct memory",
                    ledger_commit_message="direct ledger",
                    intent_note="approve",
                ),
            )

    assert result["ok"] is False
    assert result["nextAction"] == "developer-decision"
    assert result["developerDecisionRequired"] is True
    expected = result["expected"]
    assert isinstance(expected, dict)
    assert expected.get("operationKind", kind) == kind
    assert "PRIVATE_REREAD_SENTINEL" not in repr(result)
    after = _byte_tree(tmp_path)
    added = set(after) - set(before)
    lifecycle_locks = {
        path
        for path in added
        if "/controlplane/lifecycle-enclosures/locks/" in path and path.endswith(".lock")
    }
    integration_locks = {
        path
        for path in added
        if "/controlplane/integration-authority/" in path and path.endswith(".lock")
    }
    assert lifecycle_locks and len(lifecycle_locks) == 1
    assert integration_locks == (added - lifecycle_locks)
    assert integration_locks == set()
    for lock in added:
        assert after.pop(lock) == b""
    assert after == before
    launch.assert_not_called()
    direct_recovery.assert_not_called()


def test_unexpected_post_admission_fault_remains_loud(tmp_path: Path) -> None:
    contract, config, _location = _fixture(tmp_path)
    with (
        mock.patch.object(
            authority_mod,
            "load_contract",
            side_effect=RuntimeError("UNEXPECTED_PROGRAMMER_FAULT"),
        ),
        pytest.raises(RuntimeError, match="UNEXPECTED_PROGRAMMER_FAULT"),
    ):
        worktree_tools.worktree_integrate_tool(
            config,
            contract_path=contract.contract_path.as_posix(),
        )


@pytest.mark.parametrize("operation", ["closeout-preview", "integrate-preview"])
def test_preview_consumes_the_admitted_contract_without_a_second_domain_read(
    tmp_path: Path,
    operation: Consumer,
) -> None:
    contract, config, _location = _fixture(tmp_path)
    reads: list[str] = []

    def admitted_read(_path):
        reads.append("admission")
        return contract

    preview_boundary = RuntimeError("preview domain boundary reached")
    with (
        mock.patch(
            "agents_remember.application.lifecycle.configured_contract_admission.load_contract",
            side_effect=admitted_read,
        ),
        mock.patch.object(
            authority_mod,
            "load_contract",
            side_effect=AssertionError("preview reconstructed admitted contract"),
        ),
        mock.patch.object(
            closeout_mod,
            "load_contract",
            side_effect=AssertionError("closeout preview reconstructed its contract"),
        ),
        mock.patch.object(
            integrate_mod,
            "load_contract",
            side_effect=AssertionError("integrate preview reconstructed its contract"),
        ),
        mock.patch.object(
            closeout_mod,
            "_effective_closeout_input",
            side_effect=preview_boundary,
        ),
        mock.patch.object(
            closeout_mod,
            "_closeout_contract",
            wraps=closeout_mod._closeout_contract,
        ) as closeout_contract_boundary,
        mock.patch.object(
            integrate_mod,
            "require_ordinary_worktree",
            side_effect=preview_boundary,
        ),
        mock.patch.object(
            worktree_tools.git_worktree_manager,
            "closeout_result",
            wraps=closeout_mod.closeout_result,
        ) as closeout_boundary,
        mock.patch.object(
            worktree_tools.git_worktree_manager,
            "integrate_result",
            wraps=integrate_mod.integrate_result,
        ) as integrate_boundary,
        pytest.raises(RuntimeError, match="preview domain boundary reached"),
    ):
        _call_consumer(operation, config, contract)

    assert reads == ["admission"]
    selected = closeout_boundary if operation == "closeout-preview" else integrate_boundary
    unselected = integrate_boundary if operation == "closeout-preview" else closeout_boundary
    selected.assert_called_once()
    assert selected.call_args.args[1] is contract
    unselected.assert_not_called()
    if operation == "closeout-preview":
        closeout_contract_boundary.assert_called_once()
        assert closeout_contract_boundary.call_args.args[1] is contract
    else:
        closeout_contract_boundary.assert_not_called()


@pytest.mark.parametrize(
    "operation",
    ["closeout-apply", "integrate-apply", "control", "direct"],
)
def test_apply_routes_reuse_admission_then_reread_once_at_mutation_authority(
    tmp_path: Path,
    operation: Consumer,
) -> None:
    contract, config, _location = _fixture(tmp_path)
    if operation == "direct":
        direct_fixture = _series_fixture(tmp_path / "direct")
        contract = direct_fixture["contract"]
        config = direct_fixture["config"]
    reads: list[str] = []

    def admitted_read(_path):
        reads.append("admission")
        return contract

    def authority_read(_path):
        reads.append("authority")
        raise PermissionError("PRIVATE_AUTHORITY_READ_SENTINEL")

    with (
        mock.patch(
            "agents_remember.application.lifecycle.configured_contract_admission.load_contract",
            side_effect=admitted_read,
        ),
        mock.patch.object(authority_mod, "load_contract", side_effect=authority_read),
        mock.patch.object(direct_app, "_direct_recovery_action") as direct_recovery,
    ):
        result = _call_consumer(operation, config, contract)

    assert reads == ["admission", "authority"]
    assert result["nextAction"] == "developer-decision"
    assert "PRIVATE_AUTHORITY_READ_SENTINEL" not in repr(result)
    direct_recovery.assert_not_called()


def test_public_consumers_do_not_enumerate_configured_reread_lower_families() -> None:
    forbidden = {
        "ContractError",
        "LifecycleOperationLocationError",
        "ConfiguredContractAuthorityError",
        "UnicodeError",
        "OSError",
        "ValueError",
    }
    consumers = (
        worktree_tools._start_closeout_operation,
        worktree_tools.worktree_integrate_tool,
        worktree_tools.worktree_operation_control_tool,
        direct_app.direct_landing_tool,
    )
    for consumer in consumers:
        tree = ast.parse(inspect.getsource(consumer))
        identifiers = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, (ast.Name, ast.Attribute))
        }
        assert identifiers.isdisjoint(forbidden)
        for handler in (node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)):
            assert handler.type is not None
            assert not any(
                (isinstance(node, ast.Name) and node.id == "Exception")
                or (isinstance(node, ast.Attribute) and node.attr == "Exception")
                for node in ast.walk(handler.type)
            )


def test_domain_entry_signatures_require_the_admitted_contract_without_defaults() -> None:
    required = (
        (operations_mod.start_or_observe_operation, "admitted_contract"),
        (operations_mod.start_or_observe_closeout_operation, "admitted_contract"),
        (direct_domain.direct_landing, "admitted_contract"),
        (closeout_mod.closeout_result, "current_contract"),
        (integrate_mod.integrate_result, "current_contract"),
    )
    for owner, parameter_name in required:
        parameter = inspect.signature(owner).parameters[parameter_name]
        assert parameter.default is inspect.Parameter.empty
