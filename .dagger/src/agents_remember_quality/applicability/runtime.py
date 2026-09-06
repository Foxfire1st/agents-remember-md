"""Retain exact planned no-start evidence without starting a scenario command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agents_remember_quality.contract_json import _digest, _object, _safe_relative
from agents_remember_quality.rail_bindings import build_evidence_bindings

if TYPE_CHECKING:
    from agents_remember_quality.profile_plan import FrozenProfilePlan, FrozenRail
    from agents_remember_quality.profile_results import QualityProgress


def validate_source_decisions(plan: FrozenProfilePlan, source: Any) -> None:
    decisions = [rail for rail in plan.rails if rail.source_selection is not None]
    if not decisions:
        if source is not None:
            raise ValueError("source selection exists without a selected source-applicability rail")
        return
    census = _object(source, "profilePlan.sourceSelection")
    if census.get("schemaVersion") != "candidate-source-selection/v1" or (
        census.get("selectionDigest")
        != _digest({key: value for key, value in census.items() if key != "selectionDigest"})
    ):
        raise ValueError("source selection is malformed or has a contradictory digest")
    if census.get("candidateTree") != plan.candidate_value or (
        census.get("baseCommit") != plan.diff_base
    ):
        raise ValueError("source selection differs from the exact candidate or comparison base")
    for rail in decisions:
        _validate_decision(plan, rail, census)


def _validate_decision(plan: FrozenProfilePlan, rail: FrozenRail, census: dict) -> None:
    decision = _object(rail.source_selection, "rail.sourceSelection")
    if decision.get("schemaVersion") != "rail-source-selection/v1" or (
        decision.get("decisionDigest")
        != _digest({key: value for key, value in decision.items() if key != "decisionDigest"})
    ):
        raise ValueError("rail source selection is malformed or has a contradictory digest")
    if decision.get("sourceSelection") != census or decision.get("mode") != plan.mode:
        raise ValueError("rail source selection differs from the frozen plan")
    if decision.get("applicability") != rail.applicability:
        raise ValueError("rail source decision differs from its compiled applicability")
    declaration = _object(decision.get("declaration"), "sourceSelection.declaration")
    relative = _safe_relative(declaration.get("evidencePath"), "sourceSelection.evidencePath")
    policy = next((item for item in plan.published_artifacts if item["path"] == relative), None)
    if (
        policy is None
        or rail.gate not in policy["publisherGates"]
        or (len(decision_bytes(decision)) > policy["maxBytes"])
    ):
        raise ValueError("source selection evidence is outside its declared publication bounds")


def decision_bytes(decision: dict) -> bytes:
    return (
        json.dumps(decision, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def publish_source_decisions(
    progress: QualityProgress, plan: FrozenProfilePlan, *, reports: str
) -> dict[str, str]:
    scalars: dict[str, str] = {}
    for rail in plan.rails:
        decision = rail.source_selection
        if decision is None:
            continue
        declaration = decision["declaration"]
        relative = declaration["evidencePath"]
        path = reports.rstrip("/") + "/" + relative
        raw = decision_bytes(decision)
        progress.container = progress.container.with_new_file(path, raw.decode())
        progress.retained_captures[relative] = raw
        scalars["source-selection:" + declaration["selectorId"]] = path
    return scalars


def not_applicable_outcome(rail: FrozenRail) -> dict[str, object]:
    if rail.source_selection is None:
        raise ValueError("a non-applicable executable rail lacks its frozen source decision")
    raw = decision_bytes(rail.source_selection)
    if any(len(raw) > item["maxBytes"] for item in rail.evidence_contract):
        raise ValueError("source selection evidence exceeds its rail evidence contract")
    evidence = build_evidence_bindings(
        tuple(item["evidenceId"] for item in rail.evidence_contract),
        raw,
        reference=rail.source_selection["declaration"]["evidencePath"],
    )
    return {
        "status": "not-applicable",
        "started": False,
        "zeroStart": True,
        "exitCode": None,
        "evidence": evidence,
        "artifacts": [],
    }
