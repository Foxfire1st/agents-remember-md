from __future__ import annotations

from pathlib import Path

from agents_remember.certification.repository_profiles.authority import load_repository_profile
from agents_remember.certification.repository_profiles.source_selection.models import selected_paths
from agents_remember_test_support.testing.dependency_facts import RepositoryDependencyFacts

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = Path("scripts/e2e_harness")


def _direct_repository_dependencies(facts: RepositoryDependencyFacts) -> set[Path]:
    by_module = {module: path for path, module in facts.modules.items()}
    dependencies: set[Path] = set()
    for harness in facts.python_paths:
        if harness.parent != HARNESS_ROOT:
            continue
        imported = facts.imports[harness]
        resolved = {module: by_module[module] for module in imported if module in by_module}
        dependencies.update(
            path
            for module, path in resolved.items()
            if not any(other != module and other.startswith(f"{module}.") for other in resolved)
        )
    return dependencies


def test_targeted_selection_covers_every_direct_repository_dependency() -> None:
    admitted = load_repository_profile(
        "agents-remember", REPOSITORY_ROOT, Path("mcp/certification-profile-v1.json")
    )
    declaration = next(
        rail.sourceApplicability
        for rail in admitted.canonical.profile.rails
        if rail.identity.railId == "ambient-role-chat-e2e"
    )
    assert declaration is not None
    facts = RepositoryDependencyFacts.build(REPOSITORY_ROOT)
    dependencies = _direct_repository_dependencies(facts)
    unselected = sorted(
        path.as_posix()
        for path in dependencies
        if not selected_paths(declaration, (path.as_posix(),))
    )

    assert unselected == [], (
        "ambient-role E2E directly imports repository inputs outside its targeted selector: "
        f"{unselected}"
    )
