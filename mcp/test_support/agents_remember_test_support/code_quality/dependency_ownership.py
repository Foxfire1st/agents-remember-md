"""Canonical test-consumer ownership for targeted selection and retry proof."""

from __future__ import annotations

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

# Non-Python product inputs cannot participate in the import graph. Keep their
# exact pytest consumers explicit here, then verify the declaration against
# source-observed literal reads before trusting a targeted selection.
CODEX_CONFIG_PATH = Path(".codex") / "config.toml"
LAYERS_CONTRACT_PATH = Path("layers").with_suffix(".toml")
REPOSITORY_TEST_INPUT_CONSUMERS: dict[Path, frozenset[Path]] = {
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
    SAFE_FULL = "safe-full"


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
    fresh_rerun_reason: SelectionReason | None = None

    def reasons_for(self, path: Path) -> tuple[SelectionReason, ...]:
        return next((item.reasons for item in self.ownership if item.path == path), ())


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
        """Resolve affected tests or select all with an explicit incomplete-ownership reason."""

        changed_paths = tuple(sorted(set(changed), key=Path.as_posix))
        if not changed_paths:
            return TestImpact((), (), True, False)
        if refusal := self._graph_refusal(changed_paths[0]):
            return self._safe_full(refusal.source, refusal.detail)
        return self._resolved_impact(changed_paths)

    def _resolved_impact(self, changed_paths: Sequence[Path]) -> TestImpact:
        reasons: dict[Path, set[SelectionReason]] = {}
        global_sources: list[SelectionReason] = []
        for path in changed_paths:
            decision = self._resolve_one(path)
            if isinstance(decision, SelectionReason):
                if decision.kind is SelectionReasonKind.SAFE_FULL:
                    return self._safe_full(path, decision.detail)
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
            complete=True,
            global_invalidation=bool(global_sources),
        )

    def _graph_refusal(self, source: Path) -> SelectionReason | None:
        if self.parse_error is not None:
            detail = f"import-graph-invalid:{self.parse_error}"
        elif self.ambiguous_modules:
            modules = ",".join(sorted(self.ambiguous_modules))
            detail = f"ambiguous-module-identity:{modules}"
        elif self.catalog_error is not None:
            detail = f"lifecycle-catalog-invalid:{self.catalog_error}"
        else:
            return None
        return SelectionReason(SelectionReasonKind.SAFE_FULL, source, detail)

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
        if declared is not None:
            observed = self._observed_consumers(path)
            if set(observed) != set(declared):
                return SelectionReason(
                    SelectionReasonKind.SAFE_FULL,
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
        if path.suffix == ".py":
            return self._python_consumers(path)
        if _irrelevant_to_python_tests(path):
            return {}
        return SelectionReason(SelectionReasonKind.SAFE_FULL, path, "unowned-test-input")

    def _test_tree_consumers(
        self,
        path: Path,
    ) -> dict[Path, set[SelectionReason]] | SelectionReason:
        if is_test_module(path):
            if path not in self.tracked_set or not (self.project_root / path).is_file():
                return SelectionReason(SelectionReasonKind.SAFE_FULL, path, "deleted-test-module")
            return self._owned({path}, SelectionReasonKind.CHANGED_TEST, path, "self")
        declared = self.declared_consumers.get(path)
        observed = self._observed_consumers(path)
        consumers = set(observed)
        if declared is not None:
            if consumers != set(declared):
                return SelectionReason(
                    SelectionReasonKind.SAFE_FULL,
                    path,
                    "lifecycle-consumer-mismatch",
                )
            consumers.update(declared)
        if not consumers:
            return SelectionReason(SelectionReasonKind.SAFE_FULL, path, "unowned-test-support")
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
            return SelectionReason(SelectionReasonKind.SAFE_FULL, path, "unowned-python-change")
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

    def _safe_full(self, source: Path, detail: str) -> TestImpact:
        fresh_rerun_reason = SelectionReason(SelectionReasonKind.SAFE_FULL, source, detail)
        ownership = tuple(OwnedTest(path, (fresh_rerun_reason,)) for path in self.tests)
        return TestImpact(
            tests=self.tests,
            ownership=ownership,
            complete=False,
            global_invalidation=True,
            fresh_rerun_reason=fresh_rerun_reason,
        )


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
    return path.parts[:1] in {("docs",), ("notes",), ("dashboard",)} or path.suffix in {
        ".md",
        ".txt",
        ".rst",
    }
