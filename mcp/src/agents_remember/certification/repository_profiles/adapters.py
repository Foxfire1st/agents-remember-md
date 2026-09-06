"""Repository-neutral executor and terminal-result decoder interfaces."""

from __future__ import annotations

import json
import stat
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agents_remember.certification.repository_profiles.models import (
    DaggerModuleExecutorDefinition,
    JsonArtifactReferenceDefinition,
    JsonExitStatusDecoderDefinition,
    JsonFieldPath,
    JsonReferenceActivationDefinition,
    ProfileMode,
    require_relative_repository_file_path,
)

_MISSING = object()


@dataclass(frozen=True)
class RepositoryExecutionRequest:
    candidate_source: Path
    repository_bundle: Path
    execution_manifest: Path
    mode: ProfileMode
    export_root: Path
    memory_cap_bytes: int | None = None
    retained_reports: Path | None = None


@dataclass(frozen=True)
class DecodedExecutorResult:
    status: str
    exit_code: int
    artifact_path: Path


class RepositoryExecutorAdapter(Protocol):
    def command(
        self,
        definition: DaggerModuleExecutorDefinition,
        request: RepositoryExecutionRequest,
    ) -> tuple[str, ...]: ...


class RepositoryResultDecoder(Protocol):
    def decode(
        self,
        definition: JsonExitStatusDecoderDefinition,
        export_root: Path,
        available_artifacts: Collection[str],
    ) -> DecodedExecutorResult: ...


class DaggerModuleExecutorAdapter:
    """Build one declared Dagger function call; never substitute a host command."""

    def command(
        self,
        definition: DaggerModuleExecutorDefinition,
        request: RepositoryExecutionRequest,
    ) -> tuple[str, ...]:
        command = [
            definition.executable,
            "--progress=plain",
            "call",
            definition.functionName,
            _argument(definition.sourceArgument, request.candidate_source.as_posix()),
            _argument(
                definition.repositoryBundleArgument,
                request.repository_bundle.as_posix(),
            ),
            _argument(
                definition.planArgument,
                request.execution_manifest.as_posix(),
            ),
        ]
        if request.retained_reports is not None:
            if definition.retainedReportsArgument is None:
                raise ValueError("selected executor does not declare retained report transport")
            command.append(
                _argument(definition.retainedReportsArgument, request.retained_reports.as_posix())
            )
        if request.memory_cap_bytes is not None:
            if request.memory_cap_bytes < 0:
                raise ValueError("memory cap cannot be negative")
            command.append(_argument(definition.memoryCapArgument, str(request.memory_cap_bytes)))
        command.extend(
            (
                definition.reportsField,
                "export",
                _argument("path", request.export_root.as_posix()),
            )
        )
        return tuple(command)


class JsonExitStatusDecoder:
    """Decode only the exact declared JSON terminal result artifact."""

    def decode(
        self,
        definition: JsonExitStatusDecoderDefinition,
        export_root: Path,
        available_artifacts: Collection[str],
    ) -> DecodedExecutorResult:
        artifact = _confined_regular_artifact(export_root, definition.artifactPath)
        try:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"executor exported no valid authoritative result: {artifact}"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError("executor result must be a JSON object")
        _validate_artifact_references(definition, payload, available_artifacts)
        status = payload.get(definition.statusField)
        exit_code = payload.get(definition.exitCodeField)
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0:
            raise RuntimeError("executor result contains an invalid pipeline exit code")
        expected = definition.passedValue if exit_code == 0 else definition.failedValue
        if status != expected:
            raise RuntimeError(
                f"executor exported a contradictory result: status={status!r}, exitCode={exit_code}"
            )
        return DecodedExecutorResult(
            status=expected,
            exit_code=exit_code,
            artifact_path=artifact,
        )


def _argument(name: str, value: str) -> str:
    return f"--{name}={value}"


def _validate_artifact_references(
    definition: JsonExitStatusDecoderDefinition,
    payload: Mapping[str, object],
    available_artifacts: Collection[str],
) -> None:
    references: set[str] = set()
    presence: dict[str, bool] = {}
    for rule in definition.artifactReferences:
        value = _json_field(
            payload,
            rule.fieldPath,
            null_parent_as_missing=rule.nullParentPolicy == "ignore-reference",
        )
        presence[rule.fieldPath.key] = value is not _MISSING
        if value is _MISSING:
            continue
        references.update(_reference_values(rule, value))
    missing = sorted(references - set(available_artifacts))
    if missing:
        raise RuntimeError(f"executor result references missing published artifacts: {missing}")
    for activation in definition.referenceActivations:
        _validate_reference_activation(payload, activation, presence)


def _reference_values(
    rule: JsonArtifactReferenceDefinition,
    value: object,
) -> set[str]:
    if value is None and rule.nullPolicy == "ignore-reference":
        return set()
    if rule.cardinality == "single":
        if not isinstance(value, str):
            raise RuntimeError(f"executor result field {rule.fieldPath.display} must be a string")
        references = {value}
    else:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise RuntimeError(
                f"executor result field {rule.fieldPath.display} must be a string list"
            )
        references = {item for item in value if isinstance(item, str)}
    for reference in references:
        try:
            require_relative_repository_file_path(reference)
        except ValueError as error:
            raise RuntimeError(
                f"executor result field {rule.fieldPath.display} contains an invalid "
                "artifact reference"
            ) from error
    return references


def _validate_reference_activation(
    payload: Mapping[str, object],
    activation: JsonReferenceActivationDefinition,
    presence: Mapping[str, bool],
) -> None:
    selector = _json_field(
        payload,
        activation.listFieldPath,
        null_parent_as_missing=activation.nullPolicy == "ignore-activation",
    )
    if selector is _MISSING:
        return
    if selector is None and activation.nullPolicy == "ignore-activation":
        return
    if not isinstance(selector, list) or any(not isinstance(item, str) for item in selector):
        raise RuntimeError(
            f"executor result field {activation.listFieldPath.display} must be a string list"
        )
    active = activation.containsValue in selector
    referenced = {
        path.key: presence.get(path.key, False) for path in activation.referenceFieldPaths
    }
    if active:
        missing = sorted(
            path.display for path in activation.referenceFieldPaths if not referenced[path.key]
        )
        if missing:
            raise RuntimeError(
                f"active result selector {activation.listFieldPath.display} omitted required "
                f"artifact references: {missing}"
            )
        return
    claimed = sorted(
        path.display for path in activation.referenceFieldPaths if referenced[path.key]
    )
    if claimed:
        raise RuntimeError(
            f"inactive result selector {activation.listFieldPath.display} claimed artifact "
            f"references: {claimed}"
        )


def _json_field(
    payload: Mapping[str, object],
    path: JsonFieldPath,
    *,
    null_parent_as_missing: bool = False,
) -> object:
    current: object = payload
    traversed: list[str] = []
    for segment in path.segments:
        if current is None and null_parent_as_missing:
            return _MISSING
        if not isinstance(current, dict):
            parent = ".".join(traversed) or "result"
            raise RuntimeError(f"executor result field {parent} must be a JSON object")
        if segment not in current:
            return _MISSING
        current = current[segment]
        traversed.append(segment)
    return current


def _confined_regular_artifact(export_root: Path, relative_path: str) -> Path:
    """Resolve one declared decoder input without following an exported link."""

    artifact = export_root / relative_path
    try:
        root = export_root.resolve(strict=True)
        if not stat.S_ISDIR(export_root.lstat().st_mode):
            raise OSError("export root is not a real directory")
        current = export_root
        for part in Path(relative_path).parts[:-1]:
            current /= part
            if not stat.S_ISDIR(current.lstat().st_mode):
                raise OSError("decoder artifact parent is not a real directory")
        if not stat.S_ISREG(artifact.lstat().st_mode):
            raise OSError("decoder artifact is not a real regular file")
        resolved = artifact.resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"executor exported no safe authoritative result: {artifact}") from error
    if not resolved.is_relative_to(root):
        raise RuntimeError(f"executor result escapes its export root: {artifact}")
    return artifact


__all__ = [
    "DaggerModuleExecutorAdapter",
    "DecodedExecutorResult",
    "JsonExitStatusDecoder",
    "RepositoryExecutionRequest",
    "RepositoryExecutorAdapter",
    "RepositoryResultDecoder",
]
