"""Independent fail-closed reader for repository-owned selector results."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RESULT_FIELDS = frozenset(
    {
        "schemaVersion",
        "selectorId",
        "selectorVersion",
        "configurationDigest",
        "candidateIdentity",
        "mode",
        "baseRevision",
        "population",
        "complete",
        "failureCode",
        "globalInvalidators",
        "dependencyReasons",
        "unresolvedInputs",
        "outputs",
        "selectionDigest",
    }
)
_REASON_FIELDS = frozenset({"input", "kind", "effect", "outputArtifact", "outputValue", "detail"})
_OUTPUT_FIELDS = frozenset({"artifactId", "values"})


class SelectionResultError(ValueError):
    """One repository selector result is not admissible."""


@dataclass(frozen=True)
class ParsedSelectionResult:
    selection_digest: str
    values: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class SelectionAdmission:
    selector_id: str
    selector_version: str
    configuration_digest: str
    candidate_kind: str
    candidate_value: str
    mode: str
    base_revision: str


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _object(value: object, path: str, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SelectionResultError(f"{path} must be a JSON object")
    if set(value) != fields:
        raise SelectionResultError(f"{path} fields differ from the canonical contract")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SelectionResultError(f"{path} must be a nonblank unpadded string")
    return value


def _id(value: object, path: str) -> str:
    result = _string(value, path)
    if _ID.fullmatch(result) is None:
        raise SelectionResultError(f"{path} must be a stable lowercase identity")
    return result


def _string_array(value: object, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SelectionResultError(f"{path} must be a JSON array")
    result = tuple(_string(item, f"{path}.{index}") for index, item in enumerate(value))
    if result != tuple(sorted(set(result))):
        raise SelectionResultError(f"{path} must be unique and canonically ordered")
    return result


def _reason(value: object, path: str) -> tuple[str, str, str, str, str, str]:
    item = _object(value, path, _REASON_FIELDS)
    source = _string(item["input"], f"{path}.input")
    kind = _id(item["kind"], f"{path}.kind")
    effect = _string(item["effect"], f"{path}.effect")
    if effect not in {"select", "global-invalidate", "irrelevant", "unresolved"}:
        raise SelectionResultError(f"{path}.effect is unsupported")
    artifact = item["outputArtifact"]
    output_value = item["outputValue"]
    if effect == "select":
        artifact_text = _id(artifact, f"{path}.outputArtifact")
        output_text = _string(output_value, f"{path}.outputValue")
    else:
        if artifact is not None or output_value is not None:
            raise SelectionResultError(f"{path} non-selection effect claims an output")
        artifact_text = ""
        output_text = ""
    detail = _string(item["detail"], f"{path}.detail")
    return source, kind, effect, artifact_text, output_text, detail


def _reasons(
    value: object,
    path: str,
    *,
    expected_effect: Literal["resolved", "unresolved"],
) -> tuple[tuple[str, str, str, str, str, str], ...]:
    if not isinstance(value, list):
        raise SelectionResultError(f"{path} must be a JSON array")
    reasons = tuple(_reason(item, f"{path}.{index}") for index, item in enumerate(value))
    if reasons != tuple(sorted(set(reasons))):
        raise SelectionResultError(f"{path} must be unique and canonically ordered")
    if expected_effect == "resolved" and any(item[2] == "unresolved" for item in reasons):
        raise SelectionResultError(f"{path} cannot contain unresolved effects")
    if expected_effect == "unresolved" and any(item[2] != "unresolved" for item in reasons):
        raise SelectionResultError(f"{path} may contain only unresolved effects")
    return reasons


def _verify_identity(
    result: dict[str, Any],
    *,
    admission: SelectionAdmission,
) -> None:
    if result["schemaVersion"] != "repository-selector-result/v2":
        raise SelectionResultError("selector result schema is unsupported")
    expected_scalars = {
        "selectorId": admission.selector_id,
        "selectorVersion": admission.selector_version,
        "configurationDigest": admission.configuration_digest,
        "mode": admission.mode,
        "baseRevision": admission.base_revision,
    }
    for field, expected in expected_scalars.items():
        if result[field] != expected:
            raise SelectionResultError(f"selector result {field} differs from admission")
    candidate = _object(
        result["candidateIdentity"],
        "selector result candidateIdentity",
        frozenset({"kind", "value"}),
    )
    if candidate != {
        "kind": admission.candidate_kind,
        "value": admission.candidate_value,
    }:
        raise SelectionResultError("selector result candidateIdentity differs from admission")


def _verify_population(result: dict[str, Any], mode: str) -> None:
    population = result["population"]
    if population not in {"empty", "targeted", "full"}:
        raise SelectionResultError("selector result population is unsupported")
    if mode == "full" and population != "full":
        raise SelectionResultError("full selection did not publish a full population")
    if mode == "targeted" and population == "full":
        raise SelectionResultError("targeted selection attempted an undeclared full expansion")


def _parse_outputs(
    value: object,
    output_artifacts: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, list):
        raise SelectionResultError("selector result outputs must be a JSON array")
    values: dict[str, tuple[str, ...]] = {}
    for index, raw_output in enumerate(value):
        output = _object(raw_output, f"selector result outputs.{index}", _OUTPUT_FIELDS)
        artifact_id = _id(output["artifactId"], f"selector result outputs.{index}.artifactId")
        if artifact_id in values:
            raise SelectionResultError("selector result output identity is duplicated")
        values[artifact_id] = _string_array(
            output["values"], f"selector result outputs.{index}.values"
        )
    if tuple(values) != tuple(sorted(output_artifacts)):
        raise SelectionResultError("selector result output population differs from the profile")
    return values


def _verify_output_reasons(
    resolved: tuple[tuple[str, str, str, str, str, str], ...],
    values: dict[str, tuple[str, ...]],
) -> None:
    reason_edges: set[tuple[str, str]] = set()
    for _source, _kind, effect, artifact, output_value, _detail in resolved:
        if effect == "select" and (artifact not in values or output_value not in values[artifact]):
            raise SelectionResultError("selector dependency reason names an absent output value")
        if effect == "select":
            reason_edges.add((artifact, output_value))
    output_edges = {
        (artifact, output_value)
        for artifact, artifact_values in values.items()
        for output_value in artifact_values
    }
    if reason_edges != output_edges:
        raise SelectionResultError(
            "every selector output value requires an exact dependency reason"
        )


def _verify_digest(result: dict[str, Any]) -> str:
    observed = result["selectionDigest"]
    if not isinstance(observed, str) or _DIGEST.fullmatch(observed) is None:
        raise SelectionResultError("selector result selectionDigest is invalid")
    expected = _digest({key: value for key, value in result.items() if key != "selectionDigest"})
    if observed != expected:
        raise SelectionResultError("selector result digest does not match its content")
    return observed


def _verify_completion(
    result: dict[str, Any],
    unresolved: tuple[tuple[str, str, str, str, str, str], ...],
) -> None:
    complete = result["complete"]
    failure_code = result["failureCode"]
    if complete is True:
        if failure_code is not None or unresolved:
            raise SelectionResultError("complete selector result carries unresolved ownership")
        return
    if complete is False:
        if failure_code != "test-selection-ownership-incomplete" or not unresolved:
            raise SelectionResultError("incomplete selector result lacks its typed ownership facts")
        details = "; ".join(f"{item[0]}:{item[5]}" for item in unresolved)
        raise SelectionResultError(f"test-selection-ownership-incomplete: {details}")
    raise SelectionResultError("selector result complete must be boolean")


def parse_selection_result(
    payload: object,
    *,
    admission: SelectionAdmission,
    output_artifacts: tuple[str, ...],
) -> ParsedSelectionResult:
    """Validate exact identity, completeness, outputs, and the canonical content digest."""

    result = _object(payload, "selector result", _RESULT_FIELDS)
    _verify_identity(
        result,
        admission=admission,
    )
    _verify_population(result, admission.mode)
    _string_array(result["globalInvalidators"], "selector result globalInvalidators")
    resolved = _reasons(
        result["dependencyReasons"],
        "selector result dependencyReasons",
        expected_effect="resolved",
    )
    unresolved = _reasons(
        result["unresolvedInputs"],
        "selector result unresolvedInputs",
        expected_effect="unresolved",
    )
    values = _parse_outputs(result["outputs"], output_artifacts)
    _verify_output_reasons(resolved, values)
    observed_digest = _verify_digest(result)
    _verify_completion(result, unresolved)
    return ParsedSelectionResult(observed_digest, values)


__all__ = [
    "ParsedSelectionResult",
    "SelectionAdmission",
    "SelectionResultError",
    "parse_selection_result",
]
