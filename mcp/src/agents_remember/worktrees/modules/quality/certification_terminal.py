"""Typed terminal publication outcomes and exact executor-catalog decoding."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from agents_remember.certification.certificate_models import GateCertificate
from agents_remember.certification.models import (
    CompiledRail,
    GatePlan,
    GateResultManifest,
    RailArtifactResult,
    RailEvidenceReference,
    RailResult,
    RailStatus,
    RailTerminalObservation,
)
from agents_remember.certification.results import build_rail_result
from agents_remember.errors import CertificationContractError
from agents_remember.models.certification.base import RailIdentity
from agents_remember.models.certification.references import CertificateObjectReference
from agents_remember.worktrees.modules.quality.published_manifest import PublishedQualityManifest


@dataclass(frozen=True)
class RecordedGateTerminal:
    """Exact persisted result and optional certificate, with their original report bytes."""

    result: GateResultManifest
    resultReference: CertificateObjectReference
    publication: PublishedQualityManifest
    certificate: GateCertificate | None = None
    certificateReference: CertificateObjectReference | None = None


@dataclass(frozen=True)
class RecordedCertificationGeneration:
    """Typed operation-selection inputs; JSON rendering is presentation only."""

    terminals: tuple[RecordedGateTerminal, ...]
    records: tuple[dict[str, object], ...]

    def as_payload(self) -> dict[str, object]:
        published = [item for item in self.records if item["kind"] == "certificate"]
        return {
            "published": published,
            "certificates": [item["certificate"] for item in published],
            "terminal": [item for item in self.records if item["kind"] == "terminal"],
            "refused": [item for item in self.records if item["kind"] == "refused"],
        }


@dataclass
class GateRecordPublication:
    """Bounded in-flight result accumulation for one exact published generation."""

    publication: PublishedQualityManifest
    certificates: list[GateCertificate]
    terminals: list[RecordedGateTerminal]


class TerminalEvidenceMissing(RuntimeError):
    """A terminal gate result lacks the executor's per-rail evidence payload."""


def refused_record(
    gate: int, code: str, *, detail: str = "", disposition: object = "green"
) -> dict[str, object]:
    return {
        "kind": "refused",
        "gate": gate,
        "disposition": disposition,
        "certificate": None,
        "manifest": None,
        "refusalCode": code,
        "refusalDetail": detail,
    }


def catalog_gates(payload: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    """Require an exact bounded gate population before any object publication."""
    catalog = payload.get("gates")
    if not isinstance(catalog, list) or not 1 <= len(catalog) <= 5:
        raise CertificationContractError(
            "published generation omits its complete bounded gate catalog",
            ({"code": "missing-run-evidence", "path": "gates"},),
        )
    gates: list[dict[str, object]] = []
    seen: set[int] = set()
    for entry in catalog:
        if not isinstance(entry, dict):
            raise CertificationContractError(
                "gate catalog contains a non-object entry",
                ({"code": "terminal-catalog-invalid", "path": "gates"},),
            )
        gate = entry.get("gate")
        if isinstance(gate, bool) or not isinstance(gate, int):
            raise CertificationContractError(
                "catalog lacks an exact gate",
                ({"code": "terminal-catalog-invalid", "path": "gates"},),
            )
        if gate in seen:
            raise CertificationContractError(
                "duplicate terminal gate", ({"code": "terminal-catalog-invalid", "path": "gates"},)
            )
        seen.add(gate)
        gates.append(entry)
    return tuple(gates)


def terminal_results(
    gate_plan: GatePlan,
    rail_outcomes: object,
) -> tuple[RailResult, ...]:
    """Map a terminal gate catalog into typed rail results (payload-bound).

    A green pipeline publishes pass outcomes; a contradictory catalog rail
    (fail/blocked under a green disposition, or an unknown status) is
    refused below the manifest boundary rather than invented away.  Only an
    outcome the payload records is mapped -- never a synthesized one.
    """
    if not isinstance(rail_outcomes, list) or len(rail_outcomes) != len(gate_plan.rails):
        raise TerminalEvidenceMissing("terminal rail catalog does not equal the complete gate plan")
    catalog: dict[str, Mapping[str, object]] = {}
    for outcome in rail_outcomes:
        if not isinstance(outcome, dict):
            raise TerminalEvidenceMissing("terminal rail catalog contains a non-object outcome")
        key = outcome.get("key")
        if not isinstance(key, str) or key in catalog:
            raise TerminalEvidenceMissing(
                "terminal rail catalog has missing or duplicate identities"
            )
        catalog[key] = outcome
    if set(catalog) != {rail.identity.key for rail in gate_plan.rails}:
        raise TerminalEvidenceMissing("terminal rail catalog does not equal the complete gate plan")
    statuses: dict[str, RailStatus] = {
        "pass": "pass",
        "fail": "fail",
        "blocked": "blocked",
        "not-applicable": "not-applicable",
    }
    observations: list[RailTerminalObservation] = []
    planned: dict[str, CompiledRail] = {rail.identity.key: rail for rail in gate_plan.rails}
    for rail in gate_plan.rails:
        outcome = catalog[rail.identity.key]
        evidence = _payload_list(outcome, "evidence")
        artifacts = _payload_list(outcome, "artifacts")
        raw_status = outcome.get("status")
        raw = raw_status if isinstance(raw_status, str) else ""
        status = statuses.get(raw)
        if status is None:
            raise TerminalEvidenceMissing(
                f"gate {gate_plan.gate} catalog rail {rail.identity.key} is not a "
                f"certifiable terminal outcome: {raw_status!r}"
            )
        observations.append(
            RailTerminalObservation(
                rail=rail.identity,
                status=status,
                code=_terminal_code(rail, outcome),
                blockedBy=_terminal_blocked_by(outcome, planned),
                artifacts=tuple(RailArtifactResult.model_validate(item) for item in artifacts),
                evidence=tuple(RailEvidenceReference.model_validate(item) for item in evidence),
            )
        )
    return tuple(build_rail_result(gate_plan, observation) for observation in observations)


def _terminal_code(rail: CompiledRail, outcome: Mapping[str, object]) -> str:
    """One deterministic terminal code from the payload or the rail identity."""
    failure_code = outcome.get("failureCode")
    if isinstance(failure_code, str) and failure_code:
        return failure_code
    status = outcome.get("status")
    return f"{rail.identity.railId}-{status}"


def _terminal_blocked_by(
    outcome: Mapping[str, object],
    planned: Mapping[str, CompiledRail],
) -> tuple[RailIdentity, ...]:
    """Resolve payload blockedBy keys only to planned same-gate rails."""
    if outcome.get("status") != "blocked":
        return ()
    keys = outcome.get("blockedBy")
    if not isinstance(keys, list):
        return ()
    return tuple(planned[key].identity for key in keys if isinstance(key, str) and key in planned)


def _payload_list(mapping: Mapping[str, object], name: str) -> list[object]:
    value = mapping.get(name)
    return list(value) if isinstance(value, list) else []
