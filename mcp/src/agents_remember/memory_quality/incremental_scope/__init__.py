"""Content-addressed, fail-closed incremental memory dependency scope."""

from .candidate import (
    ContractScopeAuthority,
    observe_contract_task,
    observe_contract_task_pair,
    observe_git_tree_delta,
)
from .compiler import compile_scope_manifest
from .errors import ScopeFailure, ScopeUnprovenError
from .models import DependencySnapshot, ScopeCandidateIdentity, ScopeManifest
from .owners import (
    ContractDependencyAuthority,
    DependencyOwnerContext,
    observe_dependency_snapshot,
)
from .registry import checker_registry_version, checker_scope_registry

__all__ = [
    "ContractDependencyAuthority",
    "ContractScopeAuthority",
    "DependencyOwnerContext",
    "DependencySnapshot",
    "ScopeCandidateIdentity",
    "ScopeFailure",
    "ScopeManifest",
    "ScopeUnprovenError",
    "checker_registry_version",
    "checker_scope_registry",
    "compile_scope_manifest",
    "observe_contract_task",
    "observe_contract_task_pair",
    "observe_dependency_snapshot",
    "observe_git_tree_delta",
]
