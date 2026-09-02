"""Fail-closed reader for one MCP-admitted repository certification plan."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _object(value: object, path: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{path} must be a JSON object")
    return value


def _array(value: object, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a JSON array")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{path} must be a nonblank unpadded string")
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    return value


def _safe_relative(value: object, path: str, *, file: bool = False) -> str:
    resolved = _string(value, path)
    if resolved == "." and not file:
        return resolved
    parts = resolved.split("/")
    if (
        resolved.startswith(("/", "\\"))
        or "\\" in resolved
        or (len(parts[0]) >= 2 and parts[0][1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError(f"{path} must be a confined repository-relative POSIX path")
    return resolved


@dataclass(frozen=True)
class FrozenRail:
    identity: str
    gate: int
    posture: str
    execution: dict[str, Any]
    runtime: dict[str, Any]


@dataclass(frozen=True)
class FrozenSelector:
    selector_id: str
    consuming_gates: tuple[int, ...]
    definition: dict[str, Any]


@dataclass(frozen=True)
class FrozenProfilePlan:
    profile_digest: str
    plan_digest: str
    candidate_kind: str
    candidate_value: str
    selection_id: str
    mode: str
    rails: tuple[FrozenRail, ...]
    selectors: tuple[FrozenSelector, ...]
    executor: dict[str, Any]
    decoder: dict[str, Any]
    published_artifacts: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class _PlanIdentity:
    profile_digest: str
    plan: dict[str, Any]
    mode: str
    candidate_kind: str
    candidate_value: str


def load_execution_manifest(raw: str) -> FrozenProfilePlan:
    """Read exact canonical nodes and reject any detached executable input."""

    try:
        manifest = _object(json.loads(raw), "manifest")
    except json.JSONDecodeError as error:
        raise ValueError("execution manifest must be valid JSON") from error
    if manifest.get("schemaVersion") != "repository-certification-admission/v1":
        raise ValueError("execution manifest schema is not supported")
    identity = _read_plan_identity(manifest)
    nodes: dict[tuple[str, str], tuple[dict[str, Any], tuple[int, ...]]] = {}
    rails = _read_plan_gates(identity, nodes)
    executor, decoder, published = _read_shared_contracts(manifest, nodes)
    selectors = _read_selectors(nodes)
    return FrozenProfilePlan(
        profile_digest=identity.profile_digest,
        plan_digest=_string(identity.plan.get("planDigest"), "manifest.profilePlan.planDigest"),
        candidate_kind=identity.candidate_kind,
        candidate_value=identity.candidate_value,
        selection_id=_string(
            identity.plan.get("selectionId"),
            "manifest.profilePlan.selectionId",
        ),
        mode=identity.mode,
        rails=tuple(rails),
        selectors=selectors,
        executor=executor,
        decoder=decoder,
        published_artifacts=published,
    )


def _read_plan_identity(manifest: dict[str, Any]) -> _PlanIdentity:
    profile = _object(manifest.get("profile"), "manifest.profile")
    profile_digest = _string(profile.get("profileDigest"), "manifest.profile.profileDigest")
    plan = _object(manifest.get("profilePlan"), "manifest.profilePlan")
    _verify_digest_field(plan, "planDigest", "manifest.profilePlan")
    if plan.get("profileDigest") != profile_digest:
        raise ValueError("execution profile plan names another admitted profile")
    if plan.get("schemaVersion") != "repository-profile-plan/v1":
        raise ValueError("execution profile plan schema is not supported")
    mode = _string(plan.get("mode"), "manifest.profilePlan.mode")
    if mode not in {"targeted", "full"}:
        raise ValueError("execution profile plan mode is invalid")
    candidate = _object(plan.get("candidateIdentity"), "manifest.profilePlan.candidateIdentity")
    candidate_kind = _string(candidate.get("kind"), "candidateIdentity.kind")
    candidate_value = _string(candidate.get("value"), "candidateIdentity.value")
    if candidate_kind == "git-tree" and re.fullmatch(r"[0-9a-f]{40,64}", candidate_value) is None:
        raise ValueError("git-tree candidate identity is invalid")
    if manifest.get("candidateTree") != candidate_value:
        raise ValueError("execution manifest candidate differs from the profile plan")
    return _PlanIdentity(profile_digest, plan, mode, candidate_kind, candidate_value)


def _read_plan_gates(
    identity: _PlanIdentity,
    nodes: dict[tuple[str, str], tuple[dict[str, Any], tuple[int, ...]]],
) -> list[FrozenRail]:
    gates = _array(identity.plan.get("gates"), "manifest.profilePlan.gates")
    observed = [gate.get("gate") if isinstance(gate, dict) else None for gate in gates]
    if observed != [1, 2, 3, 4]:
        raise ValueError("execution profile plan must contain exact ordered Gates 1-4")
    rails: list[FrozenRail] = []
    for gate_id, raw_gate in enumerate(gates, start=1):
        gate = _object(raw_gate, f"manifest.profilePlan.gates.{gate_id}")
        _verify_gate_digest(gate, gate_id)
        if gate.get("profileDigest") != identity.profile_digest:
            raise ValueError(f"Gate {gate_id} names another admitted profile")
        rails.extend(_read_gate_rails(gate, gate_id, _read_gate_nodes(gate, gate_id, nodes)))
    return rails


def _read_shared_contracts(
    manifest: dict[str, Any],
    nodes: dict[tuple[str, str], tuple[dict[str, Any], tuple[int, ...]]],
) -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], ...]]:
    executor = _object(manifest.get("executorAdapter"), "manifest.executorAdapter")
    decoder = _object(manifest.get("resultDecoder"), "manifest.resultDecoder")
    published = tuple(
        _object(item, f"manifest.publishedArtifacts.{index}")
        for index, item in enumerate(
            _array(manifest.get("publishedArtifacts"), "manifest.publishedArtifacts")
        )
    )
    _require_shared_node(nodes, "executor-adapter", executor, "adapterId")
    _require_shared_node(nodes, "result-decoder", decoder, "decoderId")
    pinned_image_digest(executor.get("imageReference"))
    expected = {
        _string(item.get("path"), "publishedArtifacts.path"): _digest(item) for item in published
    }
    observed = {
        node_id: str(node[0]["contentDigest"])
        for (kind, node_id), node in nodes.items()
        if kind == "publication-policy"
    }
    if observed != expected:
        raise ValueError("execution publication policy differs from the gate semantic closure")
    return executor, decoder, published


def _read_selectors(
    nodes: dict[tuple[str, str], tuple[dict[str, Any], tuple[int, ...]]],
) -> tuple[FrozenSelector, ...]:
    return tuple(
        FrozenSelector(
            selector_id=node_id,
            consuming_gates=gates,
            definition=_object(node["content"], f"selector.{node_id}"),
        )
        for (kind, node_id), (node, gates) in sorted(nodes.items())
        if kind == "selector"
    )


def _verify_digest_field(payload: dict[str, Any], field: str, path: str) -> None:
    observed = _string(payload.get(field), f"{path}.{field}")
    expected = _digest({key: value for key, value in payload.items() if key != field})
    if observed != expected:
        raise ValueError(f"{path} digest does not match its bytes")


def _verify_gate_digest(gate: dict[str, Any], expected_gate: int) -> None:
    if _integer(gate.get("gate"), f"gate.{expected_gate}.gate") != expected_gate:
        raise ValueError("execution gate identity is contradictory")
    observed = _string(gate.get("planDigest"), f"gate.{expected_gate}.planDigest")
    expected = _digest(
        {key: value for key, value in gate.items() if key not in {"planDigest", "profileDigest"}}
    )
    if observed != expected:
        raise ValueError(f"Gate {expected_gate} plan digest does not match its bytes")


def _read_gate_nodes(
    gate: dict[str, Any],
    gate_id: int,
    catalog: dict[tuple[str, str], tuple[dict[str, Any], tuple[int, ...]]],
) -> dict[tuple[str, str], dict[str, Any]]:
    if gate.get("applicability") == "not-applicable":
        if gate.get("semanticInputs") != []:
            raise ValueError(f"not-applicable Gate {gate_id} contains semantic inputs")
        return {}
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for index, raw_node in enumerate(_array(gate.get("semanticInputs"), "semanticInputs")):
        node = _object(raw_node, f"gate.{gate_id}.semanticInputs.{index}")
        kind = _string(node.get("inputKind"), "semanticInputs.inputKind")
        identity = _string(node.get("inputId"), "semanticInputs.inputId")
        consuming_gates = tuple(
            _integer(item, "semanticInputs.consumingGates")
            for item in _array(node.get("consumingGates"), "semanticInputs.consumingGates")
        )
        if consuming_gates != tuple(sorted(set(consuming_gates))) or gate_id not in consuming_gates:
            raise ValueError("semantic input gate closure is noncanonical or contradictory")
        canonical_json = _string(node.get("canonicalJson"), "semanticInputs.canonicalJson")
        try:
            content = _object(json.loads(canonical_json), "semanticInputs.canonicalJson")
        except json.JSONDecodeError as error:
            raise ValueError("semantic input content must be JSON") from error
        if json.dumps(content, ensure_ascii=False, separators=(",", ":"), sort_keys=True) != (
            canonical_json
        ):
            raise ValueError("semantic input content is not canonical JSON")
        digest = _string(node.get("contentDigest"), "semanticInputs.contentDigest")
        if digest != _digest(content):
            raise ValueError("semantic input digest does not match its content")
        enriched = {**node, "content": content}
        key = (kind, identity)
        previous = catalog.setdefault(key, (enriched, consuming_gates))
        if previous != (enriched, consuming_gates):
            raise ValueError("shared semantic input differs between consuming gates")
        if key in result:
            raise ValueError("gate semantic input identity is duplicated")
        result[key] = enriched
    return result


def _read_gate_rails(
    gate: dict[str, Any],
    gate_id: int,
    nodes: dict[tuple[str, str], dict[str, Any]],
) -> list[FrozenRail]:
    rail_by_key = _gate_rail_catalog(gate, gate_id)
    ordered_keys = _gate_wave_keys(gate, gate_id)
    if set(ordered_keys) != set(rail_by_key) or len(ordered_keys) != len(rail_by_key):
        raise ValueError("gate waves do not cover the exact declared rail population")
    return [_freeze_rail(key, rail_by_key[key], gate_id, nodes) for key in ordered_keys]


def _gate_rail_catalog(gate: dict[str, Any], gate_id: int) -> dict[str, dict[str, Any]]:
    rail_by_key: dict[str, dict[str, Any]] = {}
    for index, raw_rail in enumerate(_array(gate.get("rails"), f"gate.{gate_id}.rails")):
        rail = _object(raw_rail, f"gate.{gate_id}.rails.{index}")
        identity = _object(rail.get("identity"), "rail.identity")
        key = f"{_string(identity.get('railId'), 'rail.railId')}@{_string(identity.get('version'), 'rail.version')}"
        if _integer(rail.get("gate"), "rail.gate") != gate_id or key in rail_by_key:
            raise ValueError("gate rail identity is duplicated or assigned to another gate")
        rail_by_key[key] = rail
    return rail_by_key


def _gate_wave_keys(gate: dict[str, Any], gate_id: int) -> list[str]:
    ordered_keys: list[str] = []
    for wave in _array(gate.get("waves"), f"gate.{gate_id}.waves"):
        for identity in _array(wave, f"gate.{gate_id}.wave"):
            item = _object(identity, "wave.identity")
            ordered_keys.append(
                f"{_string(item.get('railId'), 'wave.railId')}@"
                f"{_string(item.get('version'), 'wave.version')}"
            )
    return ordered_keys


def _freeze_rail(
    key: str,
    rail: dict[str, Any],
    gate_id: int,
    nodes: dict[tuple[str, str], dict[str, Any]],
) -> FrozenRail:
    execution_node = nodes.get(("rail-execution", key))
    runtime_node = nodes.get(("rail-runtime", key))
    if execution_node is None or runtime_node is None:
        raise ValueError(f"rail {key} lacks executable or runtime semantic bytes")
    execution = _object(execution_node["content"], f"rail.{key}.execution")
    runtime = _object(runtime_node["content"], f"rail.{key}.runtime")
    adapter = _object(rail.get("adapter"), f"rail.{key}.adapter")
    if adapter.get("configurationDigest") != execution_node.get("contentDigest"):
        raise ValueError(f"rail {key} executable bytes differ from its compiled adapter")
    if _object(rail.get("runtimeInputs"), f"rail.{key}.runtimeInputs") != runtime:
        raise ValueError(f"rail {key} runtime bytes differ from its compiled rail")
    _safe_relative(execution.get("workingDirectory"), f"rail.{key}.workingDirectory")
    command = _array(execution.get("command"), f"rail.{key}.command")
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError(f"rail {key} command must be a nonempty string array")
    return FrozenRail(
        identity=key.split("@", 1)[0],
        gate=gate_id,
        posture=_string(rail.get("posture"), f"rail.{key}.posture"),
        execution=execution,
        runtime=runtime,
    )


def _require_shared_node(
    nodes: dict[tuple[str, str], tuple[dict[str, Any], tuple[int, ...]]],
    kind: str,
    definition: dict[str, Any],
    identity_field: str,
) -> None:
    identity = _string(definition.get(identity_field), f"{kind}.{identity_field}")
    node = nodes.get((kind, identity))
    if node is None or node[0].get("contentDigest") != _digest(definition):
        raise ValueError(f"{kind} differs from the exact gate semantic closure")


def expand_command(
    command: list[Any],
    *,
    scalar_values: dict[str, str],
    list_values: dict[str, tuple[str, ...]],
) -> list[str]:
    """Expand only declared whole-token lists and bounded scalar placeholders."""

    expanded: list[str] = []
    for raw in command:
        token = _string(raw, "command token")
        if token.startswith("{") and token.endswith("}") and token.count("{") == 1:
            name = token[1:-1]
            if name in list_values:
                expanded.extend(list_values[name])
                continue
        while "{" in token or "}" in token:
            start = token.find("{")
            end = token.find("}", start + 1)
            if start < 0 or end < 0:
                raise ValueError("command contains a malformed placeholder")
            name = token[start + 1 : end]
            if name not in scalar_values:
                raise ValueError(f"command names unknown scalar placeholder {name!r}")
            token = token[:start] + scalar_values[name] + token[end + 1 :]
        expanded.append(token)
    return expanded


def module_runtime_digest(root: Path | None = None) -> str:
    """Bind the declared outer-adapter runtime to every repository module source byte."""

    source_root = (root or Path(__file__).resolve().parent).resolve()
    sources = {
        path.relative_to(source_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source_root.glob("*.py"))
    }
    if not sources:
        raise ValueError("repository Dagger adapter contains no runtime sources")
    return _digest(sources)


def pinned_image_digest(reference: object) -> str:
    """Return the digest from one exact OCI image reference or refuse it."""

    value = _string(reference, "executorAdapter.imageReference")
    prefix, separator, digest = value.rpartition("@sha256:")
    if (
        separator != "@sha256:"
        or not prefix
        or "@" in prefix
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("executor adapter image reference must be pinned by sha256")
    return digest


__all__ = [
    "FrozenProfilePlan",
    "FrozenRail",
    "FrozenSelector",
    "expand_command",
    "load_execution_manifest",
    "module_runtime_digest",
    "pinned_image_digest",
]
