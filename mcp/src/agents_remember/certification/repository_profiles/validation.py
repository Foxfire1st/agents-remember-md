"""Aggregate fail-closed validation for one repository-owned Gate 1-4 profile."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from agents_remember.certification.models import RegistryValidationFinding
from agents_remember.certification.repository_profiles.environment_validation import (
    _validate_environments,
)
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    JsonExitStatusDecoderDefinition,
    RepositoryProfileValidationReport,
    RepositoryRailDefinition,
    RepositorySelectorAuthority,
)
from agents_remember.certification.repository_profiles.source_selection.validation import (
    source_placeholders,
    validate_source_applicability,
)
from agents_remember.certification.repository_profiles.validation_primitives import (
    _duplicates,
    _finding,
    _validate_gate_set,
)

_CLASS_GATE = {
    "pre-test-quality": 1,
    "ordinary-test-suite": 2,
    "post-test-quality": 3,
    "integration-test": 4,
}
_REQUIRED_SELECTIONS = {
    ("local-precommit", "targeted"),
    ("closeout", "targeted"),
    ("closeout", "full"),
}
_RESERVED_PUBLICATION_ROOTS = frozenset({".quality-report-generations", "quality-report-set.json"})
_SCALAR_PLACEHOLDERS = frozenset(
    {
        "clean-room",
        "candidate-kind",
        "candidate-value",
        "diff-base",
        "memory-cap-bytes",
        "reports",
        "selection-mode",
        "selector-configuration-digest",
        "selector-id",
        "selector-version",
    }
)
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


@dataclass(frozen=True)
class _ValidationIndex:
    rails: dict[str, RepositoryRailDefinition]
    selectors: dict[str, RepositorySelectorAuthority]
    decoders: dict[str, JsonExitStatusDecoderDefinition]
    producers: dict[str, list[RepositoryRailDefinition]]


def validate_repository_profile(
    canonical: CanonicalRepositoryCertificationProfile,
) -> RepositoryProfileValidationReport:
    """Return every independent schema/graph/config finding before any command starts."""

    profile = canonical.profile
    findings: list[RegistryValidationFinding] = []
    rails = _unique_catalog(
        profile.rails,
        identity=lambda item: item.identity.key,
        code="duplicate-rail-identity",
        path="rails",
        findings=findings,
    )
    _duplicates(
        [rail.identity.railId for rail in profile.rails],
        "duplicate-rail-id",
        "rails",
        findings,
    )
    _unique_catalog(
        profile.selections,
        identity=lambda item: item.selectionId,
        code="duplicate-profile-selection",
        path="selections",
        findings=findings,
    )
    selectors = _unique_catalog(
        profile.selectors,
        identity=lambda item: item.selectorId,
        code="duplicate-selector-authority",
        path="selectors",
        findings=findings,
    )
    executors = _unique_catalog(
        profile.executorAdapters,
        identity=lambda item: item.adapterId,
        code="duplicate-executor-adapter",
        path="executorAdapters",
        findings=findings,
    )
    decoders = _unique_catalog(
        profile.resultDecoders,
        identity=lambda item: item.decoderId,
        code="duplicate-result-decoder",
        path="resultDecoders",
        findings=findings,
    )
    _duplicates(
        [item.inputId for item in profile.generatedInputs],
        "duplicate-generated-input",
        "generatedInputs",
        findings,
    )
    for item in profile.generatedInputs:
        rail = rails.get(item.checkRail.key)
        if rail is None or rail.gate != 1:
            findings.append(
                _finding(
                    "generated-input-check-rail-invalid",
                    f"generatedInputs.{item.inputId}.checkRail",
                    "generated candidate input must name a declared Gate 1 rail",
                )
            )
    _validate_environments(profile, rails, findings)
    validate_source_applicability(profile, rails, findings)
    _validate_publication(profile, decoders, findings)
    for executor in profile.executorAdapters:
        _validate_executor(executor, findings)
    for decoder in profile.resultDecoders:
        _validate_decoder(decoder, findings)
    for selector in profile.selectors:
        _validate_selector(selector, findings)
    _validate_selection_authority(profile.selections, findings)
    for selection in profile.selections:
        _validate_selection(selection, rails, executors, decoders, findings)
    producers = _artifact_producers(profile.rails, findings)
    index = _ValidationIndex(rails, selectors, decoders, producers)
    for rail in profile.rails:
        _validate_rail(rail, index, findings)
    _validate_semantic_input_usage(profile, findings)
    _validate_rail_coverage(profile.selections, profile.rails, findings)
    _validate_selection_dependencies(profile.selections, rails, findings)
    _validate_cycles(profile.rails, rails, findings)
    return RepositoryProfileValidationReport(
        profileDigest=canonical.profileDigest,
        findings=tuple(sorted(findings, key=lambda item: (item.path, item.code, item.detail))),
    )


def _unique_catalog[Item](
    items: tuple[Item, ...],
    *,
    identity: Callable[[Item], str],
    code: str,
    path: str,
    findings: list[RegistryValidationFinding],
) -> dict[str, Item]:
    catalog: dict[str, Item] = {}
    counts = Counter(identity(item) for item in items)
    for item in items:
        key = identity(item)
        catalog.setdefault(key, item)
    for key, count in counts.items():
        if count > 1:
            findings.append(_finding(code, f"{path}.{key}", "identity must be declared once"))
    return catalog


def _validate_publication(profile, decoders, findings) -> None:
    paths = [item.path for item in profile.publishedArtifacts]
    _duplicates(paths, "duplicate-published-artifact", "publishedArtifacts", findings)
    declared = set(paths)
    for path in sorted(declared):
        parts = PurePosixPath(path).parts
        if parts[0] in _RESERVED_PUBLICATION_ROOTS:
            findings.append(
                _finding(
                    "published-artifact-reserved",
                    f"publishedArtifacts.{path}",
                    "published artifacts cannot claim a framework publication control path",
                )
            )
        prefixes = (PurePosixPath(*parts[:index]).as_posix() for index in range(1, len(parts)))
        collision = next((prefix for prefix in prefixes if prefix in declared), None)
        if collision is not None:
            findings.append(
                _finding(
                    "published-artifact-path-collision",
                    f"publishedArtifacts.{path}",
                    f"artifact path has declared file prefix {collision}",
                )
            )
    published = {item.path: item for item in profile.publishedArtifacts}
    decoder_gates: defaultdict[str, set[int]] = defaultdict(set)
    for decoder in profile.resultDecoders:
        decoder_gates[decoder.artifactPath].update(decoder.consumingGates)
        if decoder.artifactPath not in published:
            findings.append(
                _finding(
                    "decoder-artifact-undeclared",
                    f"resultDecoders.{decoder.decoderId}.artifactPath",
                    "result decoder input must be in the exact publication inventory",
                )
            )
        elif not published[decoder.artifactPath].required:
            findings.append(
                _finding(
                    "decoder-artifact-optional",
                    f"resultDecoders.{decoder.decoderId}.artifactPath",
                    "the authoritative terminal result must be a required publication artifact",
                )
            )
    for artifact_path, consuming_gates in decoder_gates.items():
        artifact = published.get(artifact_path)
        if artifact is not None and tuple(artifact.publisherGates) != tuple(
            sorted(consuming_gates)
        ):
            findings.append(
                _finding(
                    "decoder-artifact-gate-scope-mismatch",
                    f"publishedArtifacts.{artifact_path}.publisherGates",
                    "authoritative terminal publication gates must equal the exact gates "
                    "that consume its decoder",
                )
            )
    if not decoders:
        findings.append(
            _finding(
                "result-decoder-missing",
                "resultDecoders",
                "at least one typed result decoder is required",
            )
        )


def _validate_executor(executor, findings) -> None:
    if executor.executable != "dagger":
        findings.append(
            _finding(
                "executor-host-program-invalid",
                f"executorAdapters.{executor.adapterId}.executable",
                "a dagger-module adapter must resolve the Dagger CLI; repository profiles "
                "cannot select an arbitrary host program",
            )
        )
    arguments = (
        executor.sourceArgument,
        executor.repositoryBundleArgument,
        executor.memoryCapArgument,
        executor.planArgument,
        executor.reportsField,
    )
    if executor.retainedReportsArgument is not None:
        arguments += (executor.retainedReportsArgument,)
    if len(set(arguments)) != len(arguments):
        findings.append(
            _finding(
                "executor-argument-ambiguous",
                f"executorAdapters.{executor.adapterId}",
                "executor source, bundle, memory, plan, reports, and retained arguments must differ",
            )
        )
    _validate_gate_set(
        executor.consumingGates,
        f"executorAdapters.{executor.adapterId}.consumingGates",
        findings,
    )


def _validate_decoder(decoder, findings) -> None:
    path = f"resultDecoders.{decoder.decoderId}"
    if decoder.statusField == decoder.exitCodeField:
        findings.append(
            _finding(
                "decoder-field-ambiguous",
                path,
                "status and exit-code fields must be distinct",
            )
        )
    if decoder.passedValue == decoder.failedValue:
        findings.append(
            _finding(
                "decoder-status-ambiguous",
                path,
                "passed and failed status values must be distinct",
            )
        )
    reference_paths = [item.fieldPath.key for item in decoder.artifactReferences]
    _duplicates(
        reference_paths,
        "decoder-reference-field-duplicate",
        f"{path}.artifactReferences",
        findings,
    )
    declared_references = set(reference_paths)
    activation_keys: list[str] = []
    for activation in decoder.referenceActivations:
        activation_path = (
            f"{path}.referenceActivations.{activation.listFieldPath.display}."
            f"{activation.containsValue}"
        )
        activation_keys.append(f"{activation.listFieldPath.key}:{activation.containsValue}")
        activated_references = [item.key for item in activation.referenceFieldPaths]
        _duplicates(
            activated_references,
            "decoder-activation-reference-duplicate",
            f"{activation_path}.referenceFieldPaths",
            findings,
        )
        unknown = sorted(set(activated_references) - declared_references)
        if unknown:
            findings.append(
                _finding(
                    "decoder-activation-reference-unknown",
                    f"{activation_path}.referenceFieldPaths",
                    "activation names artifact-reference fields not declared by this decoder",
                )
            )
    _duplicates(
        activation_keys,
        "decoder-reference-activation-duplicate",
        f"{path}.referenceActivations",
        findings,
    )
    _validate_gate_set(
        decoder.consumingGates,
        f"{path}.consumingGates",
        findings,
    )


def _validate_semantic_input_usage(profile, findings) -> None:
    selector_usage: defaultdict[str, set[int]] = defaultdict(set)
    decoder_usage: defaultdict[str, set[int]] = defaultdict(set)
    executor_usage: defaultdict[str, set[int]] = defaultdict(set)
    for rail in profile.rails:
        execution = rail.execution
        if execution.scopeProviderId is not None:
            selector_usage[execution.scopeProviderId].add(rail.gate)
        decoder_usage[execution.resultDecoderId].add(rail.gate)
    for selection in profile.selections:
        applicable = {gate.gate for gate in selection.gates if gate.status == "applicable"}
        executor_usage[selection.executorAdapterId].update(applicable)
        decoder_usage[selection.resultDecoderId].update(applicable)
    for selector in profile.selectors:
        if not selector_usage[selector.selectorId]:
            findings.append(
                _finding(
                    "selector-unreferenced",
                    f"selectors.{selector.selectorId}",
                    "selector semantic bytes must be consumed by at least one declared rail",
                )
            )
    for executor in profile.executorAdapters:
        _validate_exact_consuming_gates(
            executor.consumingGates,
            executor_usage[executor.adapterId],
            f"executorAdapters.{executor.adapterId}.consumingGates",
            "executor",
            findings,
        )
    for decoder in profile.resultDecoders:
        _validate_exact_consuming_gates(
            decoder.consumingGates,
            decoder_usage[decoder.decoderId],
            f"resultDecoders.{decoder.decoderId}.consumingGates",
            "decoder",
            findings,
        )
    for artifact in profile.publishedArtifacts:
        _validate_gate_set(
            artifact.publisherGates,
            f"publishedArtifacts.{artifact.path}.publisherGates",
            findings,
        )


def _validate_exact_consuming_gates(
    declared,
    observed,
    path,
    subject,
    findings,
) -> None:
    if not observed:
        findings.append(
            _finding(
                f"{subject}-unreferenced",
                path.rsplit(".", 1)[0],
                f"{subject} semantic bytes must be consumed by a selection or rail",
            )
        )
        return
    if tuple(declared) != tuple(sorted(observed)):
        findings.append(
            _finding(
                f"{subject}-gate-scope-mismatch",
                path,
                f"declared gates {list(declared)} do not equal exact consuming gates "
                f"{sorted(observed)}",
            )
        )


def _validate_selection_authority(selections, findings) -> None:
    observed = {(item.purpose, item.mode) for item in selections}
    for purpose, mode in sorted(_REQUIRED_SELECTIONS - observed):
        findings.append(
            _finding(
                "required-profile-selection-missing",
                f"selections.{purpose}.{mode}",
                "local/pre-commit and targeted/full closeout selections are explicit authority",
            )
        )
    pairs = [(item.purpose, item.mode) for item in selections]
    for pair, count in Counter(pairs).items():
        if count > 1:
            findings.append(
                _finding(
                    "ambiguous-profile-selection",
                    f"selections.{pair[0]}.{pair[1]}",
                    "one purpose/mode pair resolves to multiple authorities",
                )
            )


def _validate_selection(selection, rails, executors, decoders, findings) -> None:
    path = f"selections.{selection.selectionId}"
    if selection.executorAdapterId not in executors:
        findings.append(
            _finding(
                "executor-adapter-unknown",
                f"{path}.executorAdapterId",
                "selection names no declared executor adapter",
            )
        )
    if selection.resultDecoderId not in decoders:
        findings.append(
            _finding(
                "result-decoder-unknown",
                f"{path}.resultDecoderId",
                "selection names no declared result decoder",
            )
        )
    gates = [item.gate for item in selection.gates]
    if tuple(gates) != (1, 2, 3, 4):
        findings.append(
            _finding(
                "profile-gates-incomplete",
                f"{path}.gates",
                "every selection must declare exact ordered Gates 1-4",
            )
        )
    for gate in selection.gates:
        _validate_gate_selection(selection, gate, rails, findings)


def _validate_gate_selection(selection, gate, rails, findings) -> None:
    path = f"selections.{selection.selectionId}.gates.{gate.gate}"
    keys = [identity.key for identity in gate.railIds]
    _duplicates(keys, "duplicate-selected-rail", f"{path}.railIds", findings)
    for identity in gate.railIds:
        rail = rails.get(identity.key)
        if rail is None:
            findings.append(
                _finding(
                    "selected-rail-unknown",
                    f"{path}.railIds.{identity.key}",
                    "gate selection names no declared rail",
                )
            )
        elif rail.gate != gate.gate:
            findings.append(
                _finding(
                    "selected-rail-wrong-gate",
                    f"{path}.railIds.{identity.key}",
                    f"selected rail belongs to Gate {rail.gate}",
                )
            )


def _validate_rail(
    rail: RepositoryRailDefinition,
    index: _ValidationIndex,
    findings: list[RegistryValidationFinding],
) -> None:
    path = f"rails.{rail.identity.key}"
    expected_gate = _CLASS_GATE.get(rail.railClass)
    if expected_gate != rail.gate:
        findings.append(
            _finding(
                "wrong-gate-classification",
                f"{path}.railClass",
                f"{rail.railClass} is fixed to Gate {expected_gate}",
            )
        )
    _validate_rail_fields(rail, path, findings)
    _validate_rail_runtime(rail, path, index, findings)
    _validate_rail_dependencies(rail, path, index.rails, findings)
    _validate_artifact_dependencies(rail, path, index.producers, findings)
    _validate_gate_semantics(rail, path, findings)


def _validate_rail_fields(rail, path, findings) -> None:
    fields = {
        "prerequisites": [item.key for item in rail.prerequisites],
        "requiredArtifacts": list(rail.requiredArtifacts),
        "evidenceContract": [item.evidenceId for item in rail.evidenceContract],
        "outputArtifacts": [item.artifactId for item in rail.outputArtifacts],
        "environmentContract": list(rail.execution.environmentContract),
        "inputSelectors": list(rail.execution.inputSelectors),
        "successExitCodes": list(rail.execution.successExitCodes),
        "skippedExitCodes": list(rail.execution.skippedExitCodes),
    }
    for field, values in fields.items():
        _duplicates(values, "duplicate-rail-field", f"{path}.{field}", findings)


def _validate_rail_runtime(rail, path, index, findings) -> None:
    selectors, decoders = index.selectors, index.decoders
    runtime = rail.runtimeInputs.model_dump()
    missing_runtime = sorted(name for name, value in runtime.items() if value is None)
    if missing_runtime:
        findings.append(
            _finding(
                "runtime-inputs-incomplete",
                f"{path}.runtimeInputs",
                "applicable repository rails must freeze runtime, toolchain, image, lock, "
                "environment, and secret-policy identities; missing: " + ", ".join(missing_runtime),
            )
        )
    execution = rail.execution
    if execution.resultDecoderId not in decoders:
        findings.append(
            _finding(
                "result-decoder-unknown",
                f"{path}.execution.resultDecoderId",
                "rail names no declared result decoder",
            )
        )
    if execution.scopeProviderId is not None and execution.scopeProviderId not in selectors:
        findings.append(
            _finding(
                "scope-provider-unknown",
                f"{path}.execution.scopeProviderId",
                "rail names no declared repository selector authority",
            )
        )
    selector = (
        selectors.get(execution.scopeProviderId) if execution.scopeProviderId is not None else None
    )
    list_placeholders = frozenset(selector.outputArtifacts) if selector is not None else frozenset()
    scalar_placeholders = (
        _SCALAR_PLACEHOLDERS
        | source_placeholders(rail, index.rails)
        | ({"selector-output"} if selector is not None else set())
    )
    _validate_command_placeholders(
        execution.command,
        path=f"{path}.execution.command",
        scalar_placeholders=scalar_placeholders,
        list_placeholders=list_placeholders,
        findings=findings,
    )
    if rail.gate == 2 and (execution.scopeProviderId is None or not execution.inputSelectors):
        findings.append(
            _finding(
                "test-selection-authority-missing",
                f"{path}.execution",
                "Gate-2 rails require an exact scope provider and input selectors",
            )
        )
    if any(code < 0 or code > 255 for code in execution.successExitCodes):
        findings.append(
            _finding(
                "rail-success-exit-code-invalid",
                f"{path}.execution.successExitCodes",
                "successful process exit codes must be in the portable 0-255 range",
            )
        )
    if not set(execution.skippedExitCodes) <= set(execution.successExitCodes):
        findings.append(
            _finding(
                "rail-skipped-exit-code-not-successful",
                f"{path}.execution.skippedExitCodes",
                "a skipped exit must also be an explicitly successful exit",
            )
        )


def _validate_rail_dependencies(rail, path, rails, findings) -> None:
    for identity in rail.prerequisites:
        prerequisite = rails.get(identity.key)
        if prerequisite is None:
            findings.append(
                _finding(
                    "missing-prerequisite",
                    f"{path}.prerequisites.{identity.key}",
                    "rail prerequisite is not declared",
                )
            )
        elif prerequisite.gate > rail.gate:
            findings.append(
                _finding(
                    "later-gate-prerequisite",
                    f"{path}.prerequisites.{identity.key}",
                    "a rail cannot depend on a later gate",
                )
            )


def _validate_artifact_dependencies(rail, path, producers, findings) -> None:
    for artifact in rail.requiredArtifacts:
        declarations = producers.get(artifact, ())
        if not declarations:
            findings.append(
                _finding(
                    "required-artifact-undeclared",
                    f"{path}.requiredArtifacts.{artifact}",
                    "required artifact has no declared producer",
                )
            )
        elif any(producer.gate >= rail.gate for producer in declarations):
            findings.append(
                _finding(
                    "artifact-producer-not-earlier",
                    f"{path}.requiredArtifacts.{artifact}",
                    "required artifacts must be published by an earlier gate",
                )
            )
    if rail.gate == 2 and not rail.outputArtifacts:
        findings.append(
            _finding(
                "suite-artifact-missing",
                f"{path}.outputArtifacts",
                "ordinary suite rails must publish exact result artifacts",
            )
        )
    if rail.gate == 3 and (
        not rail.requiredArtifacts
        or not any(
            producer.gate == 2
            for artifact in rail.requiredArtifacts
            for producer in producers.get(artifact, ())
        )
    ):
        findings.append(
            _finding(
                "gate-three-suite-input-missing",
                f"{path}.requiredArtifacts",
                "Gate-3 rails must consume at least one declared Gate-2 artifact",
            )
        )


def _validate_gate_semantics(rail, path, findings) -> None:
    execution = rail.execution
    if rail.gate == 4:
        if not execution.cleanRoom or execution.teardownPolicy != "always":
            findings.append(
                _finding(
                    "integration-clean-room-incomplete",
                    f"{path}.execution",
                    "Gate-4 rails require clean-room execution and always-run teardown",
                )
            )
    elif execution.cleanRoom or execution.teardownPolicy != "not-required":
        findings.append(
            _finding(
                "clean-room-wrong-gate",
                f"{path}.execution",
                "clean-room or teardown integration semantics belong only to Gate 4",
            )
        )


def _validate_selector(selector, findings) -> None:
    path = f"selectors.{selector.selectorId}"
    for field in ("inputUniverse", "externalInputs", "outputArtifacts"):
        _duplicates(
            list(getattr(selector, field)),
            "duplicate-selector-field",
            f"{path}.{field}",
            findings,
        )
    _validate_selector_command(selector, findings)


def _validate_selector_command(selector, findings) -> None:
    path = f"selectors.{selector.selectorId}.command"
    placeholders = _validate_command_placeholders(
        selector.command,
        path=path,
        scalar_placeholders=_SCALAR_PLACEHOLDERS | {"selector-output"},
        list_placeholders=frozenset(),
        findings=findings,
    )
    if "selector-output" not in placeholders:
        findings.append(
            _finding(
                "selector-result-path-unused",
                path,
                "selector command must consume its exact sandbox result path",
            )
        )
    required_identity = {
        "candidate-kind",
        "candidate-value",
        "selector-configuration-digest",
        "selector-id",
        "selector-version",
    }
    for missing in sorted(required_identity - placeholders):
        findings.append(
            _finding(
                "selector-identity-input-unused",
                path,
                f"selector command must consume {{{missing}}} to bind its exact result",
            )
        )


def _validate_command_placeholders(
    command,
    *,
    path,
    scalar_placeholders,
    list_placeholders,
    findings,
):
    observed: set[str] = set()
    allowed = set(scalar_placeholders) | set(list_placeholders)
    for index, token in enumerate(command):
        matches = tuple(_PLACEHOLDER.finditer(token))
        stripped = _PLACEHOLDER.sub("", token)
        if "{" in stripped or "}" in stripped:
            findings.append(
                _finding(
                    "command-placeholder-malformed",
                    f"{path}.{index}",
                    "command placeholder braces must form complete bounded names",
                )
            )
            continue
        for match in matches:
            name = match.group(1)
            observed.add(name)
            if name not in allowed:
                findings.append(
                    _finding(
                        "command-placeholder-unknown",
                        f"{path}.{index}",
                        f"command names undeclared placeholder {name}",
                    )
                )
            elif name in list_placeholders and token != f"{{{name}}}":
                findings.append(
                    _finding(
                        "command-list-placeholder-embedded",
                        f"{path}.{index}",
                        f"list placeholder {name} must occupy one complete command token",
                    )
                )
    return observed


def _artifact_producers(rails, findings):
    producers = defaultdict(list)
    for rail in rails:
        for artifact in rail.outputArtifacts:
            producers[artifact.artifactId].append(rail)
    for artifact, declarations in producers.items():
        if len(declarations) > 1:
            findings.append(
                _finding(
                    "artifact-producer-ambiguous",
                    f"artifacts.{artifact}",
                    "an artifact identity must have one exact producer",
                )
            )
    return producers


def _validate_rail_coverage(selections, rails, findings) -> None:
    selected = {
        identity.key
        for selection in selections
        for gate in selection.gates
        for identity in gate.railIds
    }
    for rail in rails:
        if rail.identity.key not in selected:
            findings.append(
                _finding(
                    "rail-unselected",
                    f"rails.{rail.identity.key}",
                    "every declared rail must belong to an explicit profile selection",
                )
            )


def _validate_selection_dependencies(selections, rails, findings) -> None:
    for selection in selections:
        selected = {identity.key for gate in selection.gates for identity in gate.railIds}
        for key in sorted(selected):
            rail = rails.get(key)
            if rail is None:
                continue
            for prerequisite in rail.prerequisites:
                if prerequisite.key not in selected:
                    findings.append(
                        _finding(
                            "profile-prerequisite-missing",
                            f"selections.{selection.selectionId}.rails.{key}",
                            f"selected rail requires absent {prerequisite.key}",
                        )
                    )


def _validate_cycles(rail_declarations, rails, findings) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> bool:
        if key in visiting:
            return True
        if key in visited:
            return False
        visiting.add(key)
        rail = rails.get(key)
        cyclic = bool(rail) and any(
            dependency.key in rails and visit(dependency.key) for dependency in rail.prerequisites
        )
        visiting.remove(key)
        visited.add(key)
        return cyclic

    if any(visit(rail.identity.key) for rail in rail_declarations):
        findings.append(
            _finding(
                "rail-dependency-cycle",
                "rails",
                "repository rail prerequisites must be acyclic",
            )
        )


__all__ = ["validate_repository_profile"]
