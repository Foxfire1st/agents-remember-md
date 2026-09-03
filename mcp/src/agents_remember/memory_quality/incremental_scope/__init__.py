"""Content-addressed, fail-closed incremental memory dependency scope."""

from .affected_execution import (
    AffectedClosureExecution,
    RangeResolutionAffectedExecutor,
    RangeResolutionExecutionContext,
    execute_affected_closure,
    plan_affected_subresult_reuse,
)
from .affected_models import AffectedClosurePlan, AffectedClosureResult, AffectedUnitResult
from .affected_planning import AffectedClosureAdmission, compile_affected_closure_plan
from .candidate import (
    ContractScopeAuthority,
    observe_contract_task,
    observe_contract_task_pair,
    observe_git_tree_delta,
)
from .compiler import compile_scope_manifest
from .errors import GateFiveClosureRefusedError, ScopeFailure, ScopeUnprovenError
from .models import DependencySnapshot, ScopeCandidateIdentity, ScopeManifest
from .owners import (
    ContractDependencyAuthority,
    DependencyOwnerContext,
    observe_dependency_snapshot,
)
from .registry import checker_registry_version, checker_scope_registry
from .subresult_store import ContentAddressedSubresultStore, SubresultStorePolicy

__all__ = [
    "AffectedClosureAdmission",
    "AffectedClosureExecution",
    "AffectedClosurePlan",
    "AffectedClosureResult",
    "AffectedUnitResult",
    "ContentAddressedSubresultStore",
    "ContractDependencyAuthority",
    "ContractScopeAuthority",
    "DependencyOwnerContext",
    "DependencySnapshot",
    "GateFiveClosureRefusedError",
    "RangeResolutionAffectedExecutor",
    "RangeResolutionExecutionContext",
    "ScopeCandidateIdentity",
    "ScopeFailure",
    "ScopeManifest",
    "ScopeUnprovenError",
    "SubresultStorePolicy",
    "checker_registry_version",
    "checker_scope_registry",
    "compile_affected_closure_plan",
    "compile_scope_manifest",
    "execute_affected_closure",
    "observe_contract_task",
    "observe_contract_task_pair",
    "observe_dependency_snapshot",
    "observe_git_tree_delta",
    "plan_affected_subresult_reuse",
]
