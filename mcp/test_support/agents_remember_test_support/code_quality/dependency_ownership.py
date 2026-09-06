"""Canonical test-consumer ownership for targeted selection and retry proof."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agents_remember_test_support.code_quality.scope import top_level_packages
from agents_remember_test_support.testing.dependency_facts import (
    RepositoryDependencyFacts,
    is_test_module,
    module_for_path,
    within_any,
)
from agents_remember_test_support.testing.evidence_lifecycle import (
    EvidenceLifecycleError,
    load_evidence_inventory,
)

GLOBAL_TEST_INPUTS = frozenset(
    {
        Path("pyproject.toml"),
        Path("mcp/tests/evidence-lifecycle.toml"),
    }
)
OWNERSHIP_AUTHORITY_VERSION = "2.0.0"
IRRELEVANT_ROOTS = frozenset({"dashboard", "docs", "notes"})
IRRELEVANT_SUFFIXES = frozenset({".md", ".rst", ".txt"})

# Repository inputs whose pytest population differs from their broader source-consumer
# lifecycle keep exact test consumers here. Non-Python inputs cannot participate in the
# import graph, so verify each declaration against source-observed literal reads before
# trusting a targeted selection.
SETTINGS_EXAMPLE_PATH = Path("examples") / "mcp" / "settings.example.json"
CERTIFICATION_PROFILE_PATH = Path("mcp") / "certification-profile-v1.json"
CODEX_CONFIG_PATH = Path(".codex") / "config.toml"
LAYERS_CONTRACT_PATH = Path("layers").with_suffix(".toml")
AMBIENT_ROLE_RUNNER_PATH = Path("scripts") / "e2e_harness" / "run.py"
# These repository-owned patterns are the exact ordinary dashboard-suite population in
# dashboard/vitest.config.ts. Keeping them in the selector authority digest makes the population
# contract explicit without teaching the repository-neutral profile framework about Vitest.
DASHBOARD_TEST_PATTERNS: tuple[tuple[Path, tuple[str, ...]], ...] = (
    (Path("dashboard/src"), (".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx")),
    (Path("dashboard/scripts"), (".test.mjs",)),
)
REPOSITORY_TEST_INPUT_CONSUMERS: dict[Path, frozenset[Path]] = {
    AMBIENT_ROLE_RUNNER_PATH: frozenset(
        {
            # The shared profile container fixture models the runner's output. Its
            # importers therefore participate in the exact pytest consumer closure.
            Path("mcp/tests/test_agents_remember_quality.py"),
            Path("mcp/tests/test_clean_quality_executor.py"),
            Path("mcp/tests/test_gate_certificate_authority.py"),
            Path("mcp/tests/test_l5_quality_and_recovery_edges.py"),
            Path("mcp/tests/test_python_test_evidence_firewall.py"),
            Path("mcp/tests/test_quality_gate_public_contract.py"),
            Path("mcp/tests/test_quality_report_publication_security.py"),
            Path("mcp/tests/test_rail_evidence_publication.py"),
            Path("mcp/tests/test_repository_certification_profiles.py"),
            Path("mcp/tests/test_repository_profile_authority.py"),
            Path("mcp/tests/test_repository_profile_branch_coverage.py"),
            Path("mcp/tests/test_repository_quality_branch_coverage.py"),
            Path("mcp/tests/test_worktree_closeout_gate_scope.py"),
            Path("mcp/tests/test_worktree_closeout_quality_gate.py"),
            Path("mcp/tests/test_worktree_integrate_quality_gate.py"),
            Path("mcp/tests/test_worktree_quality_gate_runner.py"),
        }
    ),
    SETTINGS_EXAMPLE_PATH: frozenset(
        {
            Path("mcp/tests/test_repository_certification_profiles.py"),
        }
    ),
    CERTIFICATION_PROFILE_PATH: frozenset(
        {
            Path("mcp/tests/test_agents_remember_quality.py"),
            Path("mcp/tests/test_atomic_series_activation.py"),
            Path("mcp/tests/test_atomic_series_landing_l3.py"),
            Path("mcp/tests/test_author_execution_graph.py"),
            Path("mcp/tests/test_certification_lane_bridge.py"),
            Path("mcp/tests/test_clean_quality_executor.py"),
            Path("mcp/tests/test_clean_room.py"),
            Path("mcp/tests/test_cleanup_carryover.py"),
            Path("mcp/tests/test_cleanup_carryover_cache_1.py"),
            Path("mcp/tests/test_cleanup_carryover_cache_2.py"),
            Path("mcp/tests/test_cleanup_carryover_terminal.py"),
            Path("mcp/tests/test_closeout_claim_l3.py"),
            Path("mcp/tests/test_closeout_candidate_publication_guard.py"),
            Path("mcp/tests/test_closeout_execution_input_guards.py"),
            Path("mcp/tests/test_closeout_generation_boundary.py"),
            Path("mcp/tests/test_closeout_input_boundary.py"),
            Path("mcp/tests/test_closeout_input_model_invariants.py"),
            Path("mcp/tests/test_closeout_ledger_evidence_order.py"),
            Path("mcp/tests/test_closeout_mutation_evidence_boundary.py"),
            Path("mcp/tests/test_closeout_queue.py"),
            Path("mcp/tests/test_closeout_queue_lifecycle.py"),
            Path("mcp/tests/test_closeout_queue_projection.py"),
            Path("mcp/tests/test_closeout_recovery_projection_invariants.py"),
            Path("mcp/tests/test_code_quality_check.py"),
            Path("mcp/tests/test_code_quality_check_scope.py"),
            Path("mcp/tests/test_compact_content.py"),
            Path("mcp/tests/test_config.py"),
            Path("mcp/tests/test_configured_contract_admission_l2.py"),
            Path("mcp/tests/test_context_packet.py"),
            Path("mcp/tests/test_controlplane_gates_seam.py"),
            Path("mcp/tests/test_curator_coherence.py"),
            Path("mcp/tests/test_curator_coherence_edges.py"),
            Path("mcp/tests/test_direct_landing.py"),
            Path("mcp/tests/test_direct_landing_input_boundary.py"),
            Path("mcp/tests/test_direct_landing_operation_recovery.py"),
            Path("mcp/tests/test_dispatch_agent_ambient_reviewer.py"),
            Path("mcp/tests/test_dispatch_expectation_rows.py"),
            Path("mcp/tests/test_durable_store_contract.py"),
            Path("mcp/tests/test_evidence_dependencies.py"),
            Path("mcp/tests/test_file_size_detector.py"),
            Path("mcp/tests/test_gate_certificate_authority.py"),
            Path("mcp/tests/test_gate_certification_evidence.py"),
            Path("mcp/tests/test_gate_certification_records.py"),
            Path("mcp/tests/test_gate_replay_window.py"),
            Path("mcp/tests/test_integration_organizational_decisions_l2.py"),
            Path("mcp/tests/test_integration_apply_recovery_edges_l2.py"),
            Path("mcp/tests/test_integration_authority_lowest_writers.py"),
            Path("mcp/tests/test_integration_branch_authority.py"),
            Path("mcp/tests/test_integration_branch_authority_bootstrap_edges.py"),
            Path("mcp/tests/test_integration_branch_authority_edges.py"),
            Path("mcp/tests/test_integration_branch_authority_series_drift.py"),
            Path("mcp/tests/test_integration_ref_cas_classification_l2.py"),
            Path("mcp/tests/test_integration_ref_controls_l2.py"),
            Path("mcp/tests/test_integration_ref_transaction.py"),
            Path("mcp/tests/test_l4_authority_branch_coverage.py"),
            Path("mcp/tests/test_l4_integration_authority_gap_coverage.py"),
            Path("mcp/tests/test_l4_start_authority_gap_coverage.py"),
            Path("mcp/tests/test_l4_terminal_and_series_gap_coverage.py"),
            Path("mcp/tests/test_l5_quality_and_recovery_edges.py"),
            Path("mcp/tests/test_legacy_operation_bridge.py"),
            Path("mcp/tests/test_legacy_operation_entry_totality_l2.py"),
            Path("mcp/tests/test_lifecycle_control_configured_contract_toctou_l2.py"),
            Path("mcp/tests/test_lifecycle_enclosure_adoption_l2.py"),
            Path("mcp/tests/test_lifecycle_enclosure_model_invariants.py"),
            Path("mcp/tests/test_lifecycle_enclosure_publication_l2.py"),
            Path("mcp/tests/test_lifecycle_enclosure_successor.py"),
            Path("mcp/tests/test_lifecycle_finalize.py"),
            Path("mcp/tests/test_lifecycle_journal_read_totality_l2.py"),
            Path("mcp/tests/test_lifecycle_operation_controls_l2.py"),
            Path("mcp/tests/test_lifecycle_operation_dispositions_l2.py"),
            Path("mcp/tests/test_lifecycle_operation_dry_run_l2.py"),
            Path("mcp/tests/test_lifecycle_operation_store_invariants.py"),
            Path("mcp/tests/test_lifecycle_operation_request_validation_l2.py"),
            Path("mcp/tests/test_lifecycle_operation_worker_entrypoint.py"),
            Path("mcp/tests/test_lifecycle_operations.py"),
            Path("mcp/tests/test_lifecycle_reconciliation_concurrency_l2.py"),
            Path("mcp/tests/test_lifecycle_resume_invariant_owner_l2.py"),
            Path("mcp/tests/test_lifecycle_worker_release_guards.py"),
            Path("mcp/tests/test_mcp_registration_wiring.py"),
            Path("mcp/tests/test_mcp_registration_wiring_tests_1.py"),
            Path("mcp/tests/test_mcp_registration_wiring_tests_2.py"),
            Path("mcp/tests/test_mcp_stdio_transport.py"),
            Path("mcp/tests/test_memory_candidate_pair.py"),
            Path("mcp/tests/test_memory_citation_fix_operations.py"),
            Path("mcp/tests/test_memory_citation_migration_guard.py"),
            Path("mcp/tests/test_memory_quality.py"),
            Path("mcp/tests/test_memory_tool_enclosure_scope.py"),
            Path("mcp/tests/test_onboarding_drift.py"),
            Path("mcp/tests/test_orchestration_comms.py"),
            Path("mcp/tests/test_pending_door_live_classification_l2.py"),
            Path("mcp/tests/test_provider_async.py"),
            Path("mcp/tests/test_provider_current_state.py"),
            Path("mcp/tests/test_public_surface_conformance.py"),
            Path("mcp/tests/test_python_test_evidence_firewall.py"),
            Path("mcp/tests/test_quality_gate_public_contract.py"),
            Path("mcp/tests/test_rail_bindings.py"),
            Path("mcp/tests/test_rail_evidence_publication.py"),
            Path("mcp/tests/test_quality_report_publication_security.py"),
            Path("mcp/tests/test_quality_scope_reporting.py"),
            Path("mcp/tests/test_read_ar_files.py"),
            Path("mcp/tests/test_register_scaffold.py"),
            Path("mcp/tests/test_repository_certification_profiles.py"),
            Path("mcp/tests/test_repository_profile_authority.py"),
            Path("mcp/tests/test_repository_profile_branch_coverage.py"),
            Path("mcp/tests/test_repository_quality_branch_coverage.py"),
            Path("mcp/tests/test_sequential_default_mode.py"),
            Path("mcp/tests/test_serving_app_routes.py"),
            Path("mcp/tests/test_spawn_agent_session.py"),
            Path("mcp/tests/test_spawn_agent_session_knobs_1.py"),
            Path("mcp/tests/test_spawn_agent_session_resolution.py"),
            Path("mcp/tests/test_spawn_agent_session_settings.py"),
            Path("mcp/tests/test_structural_agent_tools.py"),
            Path("mcp/tests/test_task_doc_structural_publication_l2.py"),
            Path("mcp/tests/test_task_doc_wire_shape.py"),
            Path("mcp/tests/test_task_execution_topology.py"),
            Path("mcp/tests/test_task_execution_topology_l15.py"),
            Path("mcp/tests/test_task_execution_topology_segments.py"),
            Path("mcp/tests/test_task_reopen.py"),
            Path("mcp/tests/test_task_reopen_authority.py"),
            Path("mcp/tests/test_task_reopen_guards.py"),
            Path("mcp/tests/test_task_sprint_linkage.py"),
            Path("mcp/tests/test_terminal_leaf_assignment.py"),
            Path("mcp/tests/test_terminal_enclosure_archive_boundary_l2.py"),
            Path("mcp/tests/test_terminal_enclosure_cleanup_l5.py"),
            Path("mcp/tests/test_terminal_opener.py"),
            Path("mcp/tests/test_terminal_ws.py"),
            Path("mcp/tests/test_terminal_ws_misc.py"),
            Path("mcp/tests/test_terminal_ws_websocket_1.py"),
            Path("mcp/tests/test_terminal_ws_websocket_2.py"),
            Path("mcp/tests/test_tool_response_conformance.py"),
            Path("mcp/tests/test_tools.py"),
            Path("mcp/tests/test_topology_publication_authority.py"),
            Path("mcp/tests/test_worktree_abandon.py"),
            Path("mcp/tests/test_worktree_and_observer_helpers.py"),
            Path("mcp/tests/test_worktree_closeout_gate_scope.py"),
            Path("mcp/tests/test_worktree_closeout_quality_gate.py"),
            Path("mcp/tests/test_worktree_closeout_recovery.py"),
            Path("mcp/tests/test_worktree_edge_paths.py"),
            Path("mcp/tests/test_worktree_integrate_quality_gate.py"),
            Path("mcp/tests/test_worktree_organizational_start.py"),
            Path("mcp/tests/test_worktree_quality_gate_runner.py"),
            Path("mcp/tests/test_worktree_support.py"),
            Path("mcp/tests/test_worktree_support_benchmark.py"),
            Path("mcp/tests/test_worktree_support_tests_1.py"),
            Path("mcp/tests/test_worktree_support_tests_2.py"),
            Path("mcp/tests/test_worktree_support_tests_3.py"),
        }
    ),
    CODEX_CONFIG_PATH: frozenset(
        {
            Path("mcp/tests/test_public_surface_conformance.py"),
            Path("mcp/tests/test_starter_renderers.py"),
        }
    ),
    LAYERS_CONTRACT_PATH: frozenset(
        {
            Path("mcp/tests/test_application_boundary.py"),
            Path("mcp/tests/test_l6_diff_coverage_code_quality.py"),
            Path("mcp/tests/test_layering.py"),
            Path("mcp/tests/test_leaf_structural_coverage.py"),
            Path("mcp/tests/test_structural_limits.py"),
        }
    ),
}


class SelectionReasonKind(StrEnum):
    """Stable reason vocabulary shared by reports and retry decisions."""

    CHANGED_TEST = "changed-test"
    IMPORT_CONSUMER = "import-consumer"
    LITERAL_CONSUMER = "literal-consumer"
    DECLARED_CONSUMER = "declared-consumer"
    NAME_HEURISTIC = "name-heuristic"
    TEXT_HEURISTIC = "text-heuristic"
    PYTEST_GLOBAL = "pytest-global"
    IRRELEVANT = "irrelevant"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, order=True)
class SelectionReason:
    kind: SelectionReasonKind
    source: Path
    detail: str

    def render(self) -> str:
        return f"{self.kind.value}:{self.source.as_posix()}:{self.detail}"


@dataclass(frozen=True)
class OwnedTest:
    path: Path
    reasons: tuple[SelectionReason, ...]


@dataclass(frozen=True)
class TestImpact:
    """Affected test population and whether partial reuse is sound."""

    tests: tuple[Path, ...]
    ownership: tuple[OwnedTest, ...]
    complete: bool
    global_invalidation: bool
    input_decisions: tuple[SelectionReason, ...] = ()
    unresolved_inputs: tuple[SelectionReason, ...] = ()
    global_invalidators: tuple[SelectionReason, ...] = ()

    def reasons_for(self, path: Path) -> tuple[SelectionReason, ...]:
        return next((item.reasons for item in self.ownership if item.path == path), ())

    @property
    def reasons(self) -> tuple[SelectionReason, ...]:
        owned = {reason for item in self.ownership for reason in item.reasons}
        return tuple(sorted(owned | set(self.input_decisions) | set(self.unresolved_inputs)))


def name_match_tests(test_roots: Sequence[Path], module: str, tracked: Sequence[Path]) -> set[Path]:
    """Test files matching a suffix of a dotted module identity."""

    parts = module.split(".")
    candidates = {f"test_{'_'.join(parts[-tail:])}.py" for tail in range(1, len(parts) + 1)}
    return {path for path in tracked if path.name in candidates and within_any(path, test_roots)}


class DependencyOwnershipGraph:
    """One immutable consumer graph for production, support, plugins, and fixtures."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.facts = RepositoryDependencyFacts.build(self.project_root)
        self.tracked = self.facts.tracked
        self.tracked_set = frozenset(self.tracked)
        self.test_roots = self.facts.test_roots
        self.python_paths = self.facts.python_paths
        self.tests = self.facts.tests
        self.import_roots = self.facts.import_roots
        self.modules = self.facts.modules
        self.ambiguous_modules = self.facts.ambiguous_modules
        self.importers = {
            module: set(importers) for module, importers in self.facts.importers.items()
        }
        self.parse_error = self.facts.parse_error
        self.declared_consumers, self.catalog_error = _declared_consumers(self.project_root)

    def resolve(self, changed: Sequence[Path]) -> TestImpact:
        """Resolve exact consumers and retain every unresolved input without broadening."""

        changed_paths = tuple(sorted(set(changed), key=Path.as_posix))
        if not changed_paths:
            return TestImpact((), (), True, False)
        if refusal_detail := self._graph_refusal_detail():
            unresolved = tuple(
                SelectionReason(SelectionReasonKind.UNRESOLVED, path, refusal_detail)
                for path in changed_paths
            )
            return TestImpact(
                tests=(),
                ownership=(),
                complete=False,
                global_invalidation=False,
                unresolved_inputs=unresolved,
            )
        return self._resolved_impact(changed_paths)

    def _resolved_impact(self, changed_paths: Sequence[Path]) -> TestImpact:
        reasons: dict[Path, set[SelectionReason]] = {}
        global_sources: list[SelectionReason] = []
        input_decisions: list[SelectionReason] = []
        unresolved: list[SelectionReason] = []
        for path in changed_paths:
            decision = self._resolve_one(path)
            if isinstance(decision, SelectionReason):
                if decision.kind is SelectionReasonKind.UNRESOLVED:
                    unresolved.append(decision)
                elif decision.kind is SelectionReasonKind.IRRELEVANT:
                    input_decisions.append(decision)
                else:
                    global_sources.append(decision)
                continue
            for test, owned_reasons in decision.items():
                reasons.setdefault(test, set()).update(owned_reasons)
        if global_sources:
            for test in self.tests:
                reasons.setdefault(test, set()).update(global_sources)
        ownership = tuple(
            OwnedTest(path, tuple(sorted(owned_reasons)))
            for path, owned_reasons in sorted(reasons.items(), key=lambda item: item[0].as_posix())
        )
        return TestImpact(
            tests=tuple(item.path for item in ownership),
            ownership=ownership,
            complete=not unresolved,
            global_invalidation=bool(global_sources),
            input_decisions=tuple(sorted(input_decisions)),
            unresolved_inputs=tuple(sorted(unresolved)),
            global_invalidators=tuple(sorted(global_sources)),
        )

    def _graph_refusal_detail(self) -> str | None:
        if self.parse_error is not None:
            detail = f"import-graph-invalid:{self.parse_error}"
        elif self.ambiguous_modules:
            modules = ",".join(sorted(self.ambiguous_modules))
            detail = f"ambiguous-module-identity:{modules}"
        elif self.catalog_error is not None:
            detail = f"lifecycle-catalog-invalid:{self.catalog_error}"
        else:
            return None
        return detail

    def _resolve_one(
        self,
        path: Path,
    ) -> dict[Path, set[SelectionReason]] | SelectionReason:
        global_reason = self._global_reason(path)
        if global_reason is not None:
            return global_reason
        if within_any(path, self.test_roots):
            return self._test_tree_consumers(path)
        return self._repository_consumers(path)

    def _global_reason(self, path: Path) -> SelectionReason | None:
        if path in GLOBAL_TEST_INPUTS:
            return SelectionReason(SelectionReasonKind.PYTEST_GLOBAL, path, "global-test-config")
        if path.name == "conftest.py" and within_any(path, self.test_roots):
            return SelectionReason(SelectionReasonKind.PYTEST_GLOBAL, path, "conftest-plugin-root")
        return None

    def _repository_consumers(
        self,
        path: Path,
    ) -> dict[Path, set[SelectionReason]] | SelectionReason:
        repository_declared = REPOSITORY_TEST_INPUT_CONSUMERS.get(path)
        declared = repository_declared or self.declared_consumers.get(path)
        observed = self._observed_consumers(path)
        if declared is not None:
            if set(observed) != set(declared):
                return SelectionReason(
                    SelectionReasonKind.UNRESOLVED,
                    path,
                    "lifecycle-consumer-mismatch",
                )
            owned = self._owned(
                declared,
                SelectionReasonKind.DECLARED_CONSUMER,
                path,
                "verified-repository-input" if repository_declared else "verified-catalog",
            )
            _merge_owned(owned, observed)
            return owned
        if observed:
            return observed
        if path.suffix == ".py":
            return self._python_consumers(path)
        if _irrelevant_to_python_tests(path):
            return SelectionReason(
                SelectionReasonKind.IRRELEVANT,
                path,
                "explicitly-irrelevant-to-python-suite",
            )
        return SelectionReason(SelectionReasonKind.UNRESOLVED, path, "unowned-test-input")

    def _test_tree_consumers(
        self,
        path: Path,
    ) -> dict[Path, set[SelectionReason]] | SelectionReason:
        if is_test_module(path):
            if path not in self.tracked_set or not (self.project_root / path).is_file():
                return SelectionReason(
                    SelectionReasonKind.IRRELEVANT,
                    path,
                    "deleted-test-removed-from-population",
                )
            return self._owned({path}, SelectionReasonKind.CHANGED_TEST, path, "self")
        declared = self.declared_consumers.get(path)
        observed = self._observed_consumers(path)
        consumers = set(observed)
        if declared is not None:
            if consumers != set(declared):
                return SelectionReason(
                    SelectionReasonKind.UNRESOLVED,
                    path,
                    "lifecycle-consumer-mismatch",
                )
            consumers.update(declared)
        if not consumers:
            return SelectionReason(SelectionReasonKind.UNRESOLVED, path, "unowned-test-support")
        owned = observed
        if declared is not None:
            _merge_owned(
                owned,
                self._owned(
                    declared,
                    SelectionReasonKind.DECLARED_CONSUMER,
                    path,
                    "verified-catalog",
                ),
            )
        return owned

    def _python_consumers(
        self,
        path: Path,
    ) -> dict[Path, set[SelectionReason]] | SelectionReason:
        module = self.modules.get(path) or module_for_path(path, self.import_roots)
        owned: dict[Path, set[SelectionReason]] = {}
        _merge_owned(owned, self._observed_consumers(path))
        if module is not None:
            _merge_owned(
                owned,
                self._owned(
                    self._importing_tests(module),
                    SelectionReasonKind.IMPORT_CONSUMER,
                    path,
                    module,
                ),
            )
            _merge_owned(
                owned,
                self._owned(
                    name_match_tests(self.test_roots, module, self.tests),
                    SelectionReasonKind.NAME_HEURISTIC,
                    path,
                    module,
                ),
            )
        if not owned:
            return SelectionReason(SelectionReasonKind.UNRESOLVED, path, "unowned-python-change")
        return owned

    def _importing_tests(self, module: str) -> set[Path]:
        importers = self.facts.transitive_importers(module)
        if any(path.name == "conftest.py" for path in importers):
            return set(self.tests)
        return {path for path in importers if path in self.tests}

    def _observed_consumers(self, path: Path) -> dict[Path, set[SelectionReason]]:
        owned = self._owned(
            self.facts.import_test_consumers(path),
            SelectionReasonKind.IMPORT_CONSUMER,
            path,
            self.modules.get(path, "source-derived"),
        )
        _merge_owned(
            owned,
            self._owned(
                self.facts.literal_test_consumers(path),
                SelectionReasonKind.LITERAL_CONSUMER,
                path,
                "exact-source-reference",
            ),
        )
        return owned

    @staticmethod
    def _owned(
        consumers: Iterable[Path],
        kind: SelectionReasonKind,
        source: Path,
        detail: str,
    ) -> dict[Path, set[SelectionReason]]:
        reason = SelectionReason(kind, source, detail)
        return {path: {reason} for path in consumers}


def transitive_importers(
    module: str,
    modules: dict[Path, str],
    importers: dict[str, set[Path]],
) -> set[Path]:
    """Cycle-safe reverse import closure."""

    reachable: set[Path] = set()
    seen_modules = {module}
    queue = [module]
    while queue:
        current = queue.pop()
        for importer in importers.get(current, ()):
            if importer in reachable:
                continue
            reachable.add(importer)
            importer_module = modules.get(importer)
            if importer_module is not None and importer_module not in seen_modules:
                seen_modules.add(importer_module)
                queue.append(importer_module)
    return reachable


def reverse_import_closure(
    changed: set[Path],
    changed_modules: Sequence[str],
    modules: dict[Path, str],
    importers: dict[str, set[Path]],
) -> set[Path]:
    closure = set(changed)
    for module in changed_modules:
        closure.update(transitive_importers(module, modules, importers))
    return closure


def coverage_root_modules(tracked: Sequence[Path], import_roots: Sequence[Path]) -> tuple[str, ...]:
    root_modules = {
        module
        for package in top_level_packages(list(tracked))
        if (module := module_for_path(package, import_roots)) is not None
    }
    return tuple(sorted(root_modules))


def _declared_consumers(project_root: Path) -> tuple[dict[Path, set[Path]], str | None]:
    try:
        inventory = load_evidence_inventory(project_root)
    except EvidenceLifecycleError as error:
        return {}, str(error)
    declared: dict[Path, set[Path]] = {}
    for artifact in inventory.artifacts:
        declared.setdefault(Path(artifact.path), set()).update(
            Path(path) for path in artifact.consumers
        )
    return declared, None


def _merge_owned(
    target: dict[Path, set[SelectionReason]],
    source: dict[Path, set[SelectionReason]],
) -> None:
    for path, reasons in source.items():
        target.setdefault(path, set()).update(reasons)


def _irrelevant_to_python_tests(path: Path) -> bool:
    return bool(path.parts and path.parts[0] in IRRELEVANT_ROOTS) or (
        path.suffix in IRRELEVANT_SUFFIXES
    )


def ownership_configuration_digest() -> str:
    """Digest the versioned repository-owned classifications that selection consumes."""

    payload = {
        "schemaVersion": "agents-remember-test-selection-authority/v2",
        "version": OWNERSHIP_AUTHORITY_VERSION,
        "globalInputs": sorted(path.as_posix() for path in GLOBAL_TEST_INPUTS),
        "declaredConsumers": {
            path.as_posix(): sorted(consumer.as_posix() for consumer in consumers)
            for path, consumers in sorted(
                REPOSITORY_TEST_INPUT_CONSUMERS.items(),
                key=lambda item: item[0].as_posix(),
            )
        },
        "irrelevantRoots": sorted(IRRELEVANT_ROOTS),
        "irrelevantSuffixes": sorted(IRRELEVANT_SUFFIXES),
        "profileTestPatterns": {
            root.as_posix(): list(suffixes) for root, suffixes in DASHBOARD_TEST_PATTERNS
        },
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
