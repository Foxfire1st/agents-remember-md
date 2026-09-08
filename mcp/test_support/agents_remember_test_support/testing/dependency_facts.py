"""Observable repository import, pytest-plugin, and test-consumer facts.

This module is deliberately declaration-free. Lifecycle metadata may describe ownership, but it
cannot make itself complete; selection, retry, lifecycle validation, and causal
reporting all consume this same source-derived graph.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agents_remember_test_support.code_quality.scope import (
    ScopeError,
    git_ls_files,
    git_untracked_files,
    pytest_testpaths,
    top_level_packages,
)


def is_test_module(path: Path) -> bool:
    return path.suffix == ".py" and (
        path.name.startswith("test_") or path.name.endswith("_test.py")
    )


def within_any(path: Path, roots: Sequence[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def import_roots_for(
    tracked_python: Sequence[Path],
    test_roots: Sequence[Path],
) -> tuple[Path, ...]:
    roots = {package.parent for package in top_level_packages(list(tracked_python))}
    roots.update(test_roots)
    return tuple(sorted(roots, key=lambda path: (-len(path.parts), path.as_posix())))


def module_for_path(path: Path, import_roots: Sequence[Path]) -> str | None:
    for root in import_roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            return None
        parts = relative.with_suffix("").parts
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) or None
    return None


def dotted_ancestors(module: str) -> tuple[str, ...]:
    parts = module.split(".")
    return tuple(".".join(parts[:index]) for index in range(len(parts), 0, -1))


def resolve_relative_import(
    root_module: str,
    level: int,
    name: str | None,
    *,
    package_module: bool = False,
) -> str | None:
    parts = root_module.split(".")
    package = parts if package_module else parts[:-1]
    parents = level - 1
    if parents > len(package):
        return None
    base = package[: len(package) - parents]
    return ".".join(base if name is None else [*base, name])


def import_from_modules(
    node: ast.ImportFrom,
    root_module: str | None,
    *,
    package_module: bool = False,
) -> set[str]:
    modules: set[str] = set()
    if node.level > 0:
        if root_module is None:
            return modules
        resolved = resolve_relative_import(
            root_module,
            node.level,
            node.module,
            package_module=package_module,
        )
        if resolved is None:
            return modules
        modules.update(dotted_ancestors(resolved))
        prefix = resolved
    elif node.module:
        modules.update(dotted_ancestors(node.module))
        prefix = node.module
    else:
        return modules
    for alias in node.names:
        if alias.name != "*":
            modules.add(f"{prefix}.{alias.name}")
    return modules


def file_imports(path: Path, root_module: str | None) -> set[str]:
    """Resolve Python imports and recursive string-declared pytest plugins."""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as error:
        raise ScopeError(f"test ownership could not parse {path}: {error}") from error
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.update(dotted_ancestors(alias.name))
        elif isinstance(node, ast.ImportFrom):
            imports.update(
                import_from_modules(
                    node,
                    root_module,
                    package_module=path.name == "__init__.py",
                )
            )
    imports.update(_pytest_plugin_imports(tree))
    return imports


def _pytest_plugin_imports(tree: ast.AST) -> set[str]:
    plugins: set[str] = set()
    for node in ast.walk(tree):
        value = _pytest_plugins_value(node)
        if value is None:
            continue
        for candidate in _literal_plugin_names(value):
            plugins.update(dotted_ancestors(candidate))
    return plugins


def _literal_plugin_names(value: ast.expr) -> tuple[str, ...]:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return (value.value,)
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        names: list[str] = []
        for item in value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                raise ScopeError("pytest_plugins must contain only literal dotted module names")
            names.append(item.value)
        return tuple(names)
    raise ScopeError("pytest_plugins must be a literal string or sequence of strings")


def _pytest_plugins_value(node: ast.AST) -> ast.expr | None:
    if isinstance(node, ast.Assign):
        targets = tuple(node.targets)
        value = node.value
    elif isinstance(node, ast.AnnAssign):
        targets = (node.target,)
        value = node.value
    else:
        return None
    return value if any(_pytest_plugins_target(target) for target in targets) else None


def _pytest_plugins_target(target: ast.expr) -> bool:
    return isinstance(target, ast.Name) and target.id == "pytest_plugins"


def module_index(
    tracked: Sequence[Path],
    import_roots: Sequence[Path],
) -> tuple[dict[Path, str], frozenset[str]]:
    modules: dict[Path, str] = {}
    owners: dict[str, list[Path]] = {}
    for path in tracked:
        module = module_for_path(path, import_roots)
        if module is None:
            continue
        modules[path] = module
        owners.setdefault(module, []).append(path)
    ambiguous = frozenset(module for module, paths in owners.items() if len(paths) > 1)
    return modules, ambiguous


@dataclass(frozen=True)
class RepositoryDependencyFacts:
    """One immutable, source-derived repository dependency graph."""

    project_root: Path
    tracked: tuple[Path, ...]
    test_roots: tuple[Path, ...]
    python_paths: tuple[Path, ...]
    tests: tuple[Path, ...]
    import_roots: tuple[Path, ...]
    modules: dict[Path, str]
    ambiguous_modules: frozenset[str]
    imports: dict[Path, frozenset[str]]
    importers: dict[str, frozenset[Path]]
    string_literals: dict[Path, frozenset[str]]
    directory_readers: frozenset[Path]
    file_readers: frozenset[Path]
    parse_error: str | None

    @classmethod
    def build(cls, project_root: Path) -> RepositoryDependencyFacts:
        root = project_root.resolve()
        tracked_paths = {path for path in git_ls_files(root) if (root / path).is_file()}
        tracked_paths.update(git_untracked_files(root, [Path(".")]))
        tracked = tuple(sorted(tracked_paths, key=Path.as_posix))
        test_roots = tuple(pytest_testpaths(root))
        python_paths = tuple(
            path for path in tracked if path.suffix == ".py" and (root / path).is_file()
        )
        tests = tuple(
            path for path in python_paths if is_test_module(path) and within_any(path, test_roots)
        )
        import_roots = import_roots_for(python_paths, test_roots)
        modules, ambiguous = module_index(python_paths, import_roots)
        imports: dict[Path, frozenset[str]] = {}
        importers: dict[str, set[Path]] = {}
        string_literals: dict[Path, frozenset[str]] = {}
        directory_readers: set[Path] = set()
        file_readers: set[Path] = set()
        parse_error: str | None = None
        try:
            for path in python_paths:
                source_path = root / path
                source = source_path.read_text(encoding="utf-8")
                resolved_imports = frozenset(file_imports(source_path, modules.get(path)))
                imports[path] = resolved_imports
                for module in resolved_imports:
                    importers.setdefault(module, set()).add(path)
                tree = ast.parse(source, filename=str(source_path))
                string_literals[path] = _code_string_literals(tree)
                if any(
                    token in source for token in (".glob(", ".rglob(", ".iterdir(", "load_fixture(")
                ):
                    directory_readers.add(path)
                if any(token in source for token in (".read_text(", ".read_bytes(", "open(")):
                    file_readers.add(path)
        except (OSError, SyntaxError, UnicodeError, ScopeError) as error:
            parse_error = str(error)
            imports.clear()
            importers.clear()
            string_literals.clear()
            directory_readers.clear()
            file_readers.clear()
        return cls(
            project_root=root,
            tracked=tracked,
            test_roots=test_roots,
            python_paths=python_paths,
            tests=tests,
            import_roots=import_roots,
            modules=modules,
            ambiguous_modules=ambiguous,
            imports=imports,
            importers={key: frozenset(value) for key, value in importers.items()},
            string_literals=string_literals,
            directory_readers=frozenset(directory_readers),
            file_readers=frozenset(file_readers),
            parse_error=parse_error,
        )

    def observed_test_consumers(self, path: Path) -> frozenset[Path]:
        """Derive test consumers from imports/plugin edges and exact literal references."""

        relative = path if not path.is_absolute() else path.relative_to(self.project_root)
        if relative in self.tests:
            return frozenset({relative})
        if relative.name == "conftest.py" and within_any(relative, self.test_roots):
            return frozenset(self.tests)
        consumers = set(self.import_test_consumers(relative))
        consumers.update(self.literal_test_consumers(relative))
        return frozenset(consumers)

    def observed_source_consumers(self, path: Path) -> frozenset[Path]:
        """Derive every repository source consumer, including script-local imports."""

        relative = path if not path.is_absolute() else path.relative_to(self.project_root)
        consumers = set(self._direct_literal_referrers(relative))
        pending = [relative]
        expanded: set[Path] = set()
        while pending:
            current = pending.pop()
            if current in expanded:
                continue
            expanded.add(current)
            for importer in self._direct_source_importers(current):
                if importer not in consumers:
                    consumers.add(importer)
                    pending.append(importer)
        consumers.discard(relative)
        return frozenset(consumers)

    def _direct_source_importers(self, path: Path) -> frozenset[Path]:
        module = self.modules.get(path) or module_for_path(path, self.import_roots)
        if module is not None:
            return frozenset(self.importers.get(module, ()))
        return frozenset(
            candidate
            for candidate in self.python_paths
            if candidate.parent == path.parent and path.stem in self.imports.get(candidate, ())
        )

    def import_test_consumers(self, path: Path) -> frozenset[Path]:
        module = self.modules.get(path) or module_for_path(path, self.import_roots)
        if module is None:
            return frozenset()
        importers = self.transitive_importers(module)
        if any(candidate.name == "conftest.py" for candidate in importers):
            return frozenset(self.tests)
        tests = {candidate for candidate in importers if candidate in self.tests}
        for importer in importers - set(self.tests):
            tests.update(
                candidate
                for candidate in self._direct_literal_referrers(importer)
                if candidate in self.tests
            )
        return frozenset(tests)

    def transitive_importers(self, module: str) -> frozenset[Path]:
        reachable: set[Path] = set()
        seen_modules = {module}
        queue = [module]
        while queue:
            current = queue.pop()
            for importer in self.importers.get(current, ()):
                if importer in reachable:
                    continue
                reachable.add(importer)
                importer_module = self.modules.get(importer)
                if importer_module is not None and importer_module not in seen_modules:
                    seen_modules.add(importer_module)
                    queue.append(importer_module)
        return frozenset(reachable)

    def literal_test_consumers(self, path: Path) -> frozenset[Path]:
        module = self.modules.get(path) or module_for_path(path, self.import_roots)
        direct: set[Path] = set()
        for candidate in self.python_paths:
            literals = self.string_literals.get(candidate, frozenset())
            if (module is not None and module in literals) or _source_references_path(
                literals,
                candidate in self.directory_readers,
                candidate in self.file_readers,
                path,
            ):
                direct.add(candidate)
        tests: set[Path] = set()
        for candidate in direct:
            if candidate in self.tests:
                tests.add(candidate)
            tests.update(self.import_test_consumers(candidate))
        return frozenset(tests)

    def _direct_literal_referrers(self, path: Path) -> frozenset[Path]:
        module = self.modules.get(path) or module_for_path(path, self.import_roots)
        return frozenset(
            candidate
            for candidate in self.python_paths
            if (module is not None and module in self.string_literals.get(candidate, frozenset()))
            or _source_references_path(
                self.string_literals.get(candidate, frozenset()),
                candidate in self.directory_readers,
                candidate in self.file_readers,
                path,
            )
        )

    def dependency_chain(self, owner: Path, test: Path) -> tuple[Path, ...] | None:
        """Return one shortest real reverse-import/plugin chain from owner to a test."""

        if owner.name == "conftest.py" and within_any(owner, self.test_roots):
            return (owner, test) if test in self.tests else None
        module = self.modules.get(owner) or module_for_path(owner, self.import_roots)
        if module is None:
            return None
        queue: deque[tuple[str, tuple[Path, ...]]] = deque([(module, (owner,))])
        seen_modules = {module}
        while queue:
            current, chain = queue.popleft()
            for importer in sorted(self.importers.get(current, ()), key=Path.as_posix):
                next_chain = (*chain, importer)
                if importer == test:
                    return next_chain
                if importer.name == "conftest.py" and test in self.tests:
                    return (*next_chain, test)
                importer_module = self.modules.get(importer)
                if importer_module is not None and importer_module not in seen_modules:
                    seen_modules.add(importer_module)
                    queue.append((importer_module, next_chain))
        return None


def _source_references_path(
    literals: frozenset[str],
    directory_is_read: bool,
    file_is_read: bool,
    path: Path,
) -> bool:
    full = path.as_posix()
    test_relative = Path(*path.parts[2:]).as_posix() if path.parts[:2] == ("mcp", "tests") else full
    if full in literals or test_relative in literals:
        return True
    basename = path.name
    specific_basename = len(basename) >= 16 or any(character.isdigit() for character in basename)
    if basename in literals and specific_basename and file_is_read:
        return True
    if "fixtures" not in path.parts:
        return False
    fixture_index = path.parts.index("fixtures")
    fixture_parts = path.parts[fixture_index + 1 :]
    if len(fixture_parts) < 2 or fixture_parts[0] not in literals:
        return False
    fixture_root = fixture_parts[0]
    version_or_group = fixture_parts[1] if len(fixture_parts) > 2 else None
    if version_or_group is not None and version_or_group in literals:
        return file_is_read and (
            basename in literals or (directory_is_read and fixture_root == "claude_stream_json")
        )
    return file_is_read and basename in literals


def _code_string_literals(tree: ast.AST) -> frozenset[str]:
    class _LiteralVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.values: set[str] = set()

        def visit_Expr(self, node: ast.Expr) -> None:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return
            self.generic_visit(node)

        def visit_Constant(self, node: ast.Constant) -> None:
            if isinstance(node.value, str):
                self.values.add(node.value)

    visitor = _LiteralVisitor()
    visitor.visit(tree)
    return frozenset(visitor.values)
