"""Read the exact host-selected recovery and original predecessor evidence objects.

R21 selection is performed by the canonical host owner. This transport reader binds
that immutable decision to the frozen plan and verifies every original byte identity;
it does not discover a profile, choose a different prefix, or manufacture a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents_remember_quality.contract_json import (
    _array,
    _digest,
    _integer,
    _object,
    _safe_relative,
    _string,
)

MAX_RETAINED_FILES = 4096


@dataclass(frozen=True)
class FrozenRetainedFile:
    path: str
    sha256: str
    size: int
    generation: str


@dataclass(frozen=True)
class FrozenResume:
    first_gate: int
    execution_digest: str
    retained: tuple[dict[str, Any], ...]
    files: tuple[FrozenRetainedFile, ...]

    def reused_catalog(self, gate: int) -> dict[str, object]:
        row = self.retained[gate - 1]
        return {
            "gate": gate,
            "disposition": "reused",
            "started": False,
            "zeroStart": True,
            "certificateDigest": row["certificate"]["certificateDigest"],
            "resultManifestDigest": row["result"]["manifestDigest"],
            "originalPublication": row["publication"],
            "rails": [],
        }


def _full_digest(payload: dict[str, Any], key: str) -> None:
    if payload.get(key) != _digest({name: value for name, value in payload.items() if name != key}):
        raise ValueError(f"retained execution {key} differs from its exact bytes")


def _semantic_digest(payload: dict[str, Any], key: str) -> dict[str, Any]:
    envelope = _object(payload.get("semanticEnvelope"), f"{key}.semanticEnvelope")
    if payload.get(key) != _digest(envelope):
        raise ValueError(f"retained execution {key} differs from its original envelope")
    return envelope


def read_resume(manifest: dict[str, Any]) -> FrozenResume | None:
    raw = manifest.get("codeExecution")
    if raw is None:
        if "retainedReports" in manifest:
            raise ValueError("retained report transport requires an explicit selected execution")
        return None
    execution = _object(raw, "codeExecution")
    if set(execution) != {
        "schemaVersion",
        "diffBase",
        "run",
        "reusePlan",
        "inputChanges",
        "certificates",
        "retained",
        "executionDigest",
    }:
        raise ValueError("selected code execution fields are invalid")
    if execution.get("schemaVersion") != "code-certification-execution/v1":
        raise ValueError("selected code execution schema is unsupported")
    _full_digest(execution, "executionDigest")
    if execution.get("diffBase") != manifest.get("diffBase"):
        raise ValueError("selected execution differs from its immutable comparison base")
    admission = _read_run_admission(execution, manifest)
    reuse, first = _read_reuse_decision(execution)
    certificates = _read_certificate_prefix(execution, reuse, first)
    retained = tuple(
        _object(item, "retained") for item in _array(execution.get("retained"), "retained")
    )
    if len(retained) != first - 1:
        raise ValueError("retained result population differs from selected gate suffix")
    gates = _array(admission.get("gates"), "admission.gates")
    for gate, row in enumerate(retained, start=1):
        _validate_retained(
            row, certificates[gate - 1], _object(gates[gate - 1], "admission.gate"), admission
        )
    files = _read_files(manifest, retained, first)
    return FrozenResume(first, execution["executionDigest"], retained, files)


def _read_reuse_decision(execution):
    reuse = _object(execution.get("reusePlan"), "codeExecution.reusePlan")
    _array(execution.get("inputChanges"), "codeExecution.inputChanges")
    if reuse.get("schemaVersion") != "closeout-certificate-reuse-plan/v1":
        raise ValueError("selected reuse plan schema is unsupported")
    first = _integer(reuse.get("firstGateToRun"), "recovery.firstGateToRun")
    if first not in (1, 2, 3, 4) or reuse.get("zeroGateStarts") is not False:
        raise ValueError("selected recovery has zero code gate starts; Dagger must not launch")
    if reuse.get("invalidatedGates") != list(range(first, 6)):
        raise ValueError("selected recovery must name exactly one downstream gate suffix")
    return reuse, first


def _read_run_admission(execution, manifest):
    run = _object(execution.get("run"), "codeExecution.run")
    _full_digest(run, "runDigest")
    if run.get("schemaVersion") != "closeout-frozen-certification-run/v1" or run.get(
        "repositoryPlan"
    ) != manifest.get("profilePlan"):
        raise ValueError("selected frozen run differs from the admitted repository plan")
    admission = _semantic_digest(_object(run.get("admission"), "run.admission"), "admissionDigest")
    plan = _object(manifest.get("profilePlan"), "profilePlan")
    if (
        admission.get("candidateCodeTree"),
        admission.get("admittedProfileDigest"),
        admission.get("profileId"),
    ) != (
        plan.get("candidateIdentity"),
        plan.get("profileDigest"),
        plan.get("selectionId"),
    ):
        raise ValueError("selected admission differs from the execution plan")
    return admission


def _read_certificate_prefix(execution, reuse, first):
    certificates = _array(execution.get("certificates"), "codeExecution.certificates")
    if len(certificates) > 5:
        raise ValueError("selected recovery certificate population is invalid")
    identities = []
    for gate, raw_certificate in enumerate(certificates, start=1):
        certificate = _object(raw_certificate, "certificate")
        envelope = _semantic_digest(certificate, "certificateDigest")
        if envelope.get("gate") != gate or envelope.get("directPredecessors") != identities:
            raise ValueError(
                "selected certificates must retain their exact ordered predecessor chain"
            )
        identities.append({"gate": gate, "certificateDigest": certificate["certificateDigest"]})
    if reuse.get("reusedCertificates") != identities[: first - 1] or len(identities) < first - 1:
        raise ValueError("selected recovery names a different original certificate prefix")
    return certificates


def _validate_retained(row, certificate, admitted_gate, admission) -> None:
    if (
        set(row) != {"certificate", "result", "publication"}
        or row.get("certificate") != certificate
    ):
        raise ValueError("retained execution detached its original certificate")
    envelope = certificate["semanticEnvelope"]
    for field in ("gate", "gateSemanticDigest", "repositoryGatePlanDigest", "semanticInputs"):
        if envelope.get(field) != admitted_gate.get(field):
            raise ValueError(f"retained certificate differs from admitted gate {field}")
    if envelope.get("candidateCodeTree") != admission.get("candidateCodeTree") or envelope.get(
        "repositoryId"
    ) != admission.get("repositoryId"):
        raise ValueError("retained certificate belongs to another candidate or repository")
    result = _object(row.get("result"), "retained.result")
    _full_digest(result, "manifestDigest")
    if result.get("disposition") != "green" or result.get("manifestDigest") != envelope.get(
        "resultManifestDigest"
    ):
        raise ValueError("retained result is not the original green certified result")
    publication = _object(row.get("publication"), "retained.publication")
    _string(publication.get("generation"), "publication.generation")
    if (
        publication.get("candidateTree"),
        publication.get("profileDigest"),
        publication.get("profileSelectionId"),
    ) != (
        envelope["candidateCodeTree"]["value"],
        envelope.get("admittedProfileDigest"),
        result.get("profileId"),
    ):
        raise ValueError("retained publication differs from its original certificate authority")
    if (
        result.get("gate"),
        result.get("candidateIdentity"),
        result.get("registryDigest"),
        result.get("gatePlanDigest"),
    ) != (
        envelope.get("gate"),
        envelope.get("candidateCodeTree"),
        envelope.get("registryDigest"),
        envelope.get("gatePlanDigest"),
    ):
        raise ValueError("retained result authority differs from its original certificate")
    for rail in _array(result.get("railResults"), "retained.railResults"):
        _full_digest(_object(rail, "retained.rail"), "resultDigest")


def _read_files(
    manifest: dict[str, Any], retained: tuple[dict[str, Any], ...], first: int
) -> tuple[FrozenRetainedFile, ...]:
    declarations = _publication_bounds(manifest, first)
    maximum_bytes = sum(item[0] for item in declarations.values())
    expected: dict[str, FrozenRetainedFile] = {}
    total = 0
    for row in retained:
        publication = row["publication"]
        inventory = _object(publication.get("files"), "retained.publication.files")
        for rail in row["result"]["railResults"]:
            for key, locator in (("evidence", "reference"), ("artifacts", "evidenceRef")):
                for member in _array(rail.get(key), f"retained.rail.{key}"):
                    item = _read_member(member, locator, publication, inventory)
                    _require_member_bound(item, declarations, row["result"]["gate"])
                    path, digest, size = item.path, item.sha256, item.size
                    prior = expected.get(path)
                    if prior is not None:
                        if (prior.sha256, prior.size) != (digest, size):
                            raise ValueError("retained reports contain conflicting producer paths")
                        continue
                    expected[path] = item
                    total += size
                    if len(expected) > MAX_RETAINED_FILES or total > maximum_bytes:
                        raise ValueError("retained reports exceed the bounded transport population")
    files = _array(manifest.get("retainedReports"), "retainedReports")
    observed = [
        {"path": item.path, "sha256": item.sha256, "size": item.size, "generation": item.generation}
        for item in (expected[path] for path in sorted(expected))
    ]
    if files != observed:
        raise ValueError(
            "retained transport inventory differs from original selected result members"
        )
    return tuple(expected[path] for path in sorted(expected))


def _require_member_bound(item, declarations, gate):
    bound = declarations.get(item.path)
    if bound is None or gate not in bound[1]:
        raise ValueError("retained report lacks its exact frozen producer publication")
    if item.size > bound[0]:
        raise ValueError("retained report exceeds its frozen publication byte bound")


def _publication_bounds(manifest, first):
    declarations = {}
    seen = set()
    for raw in _array(manifest.get("publishedArtifacts"), "publishedArtifacts"):
        item = _object(raw, "publishedArtifact")
        path = _safe_relative(item.get("path"), "publishedArtifact.path", file=True)
        maximum = _integer(item.get("maxBytes"), "publishedArtifact.maxBytes")
        gates = tuple(
            _integer(gate, "publisherGate")
            for gate in _array(item.get("publisherGates"), "publisherGates")
        )
        if maximum <= 0 or maximum > 1_000_000_000:
            raise ValueError("published artifact has an invalid byte bound")
        if path in seen:
            raise ValueError("published artifact byte bounds are ambiguous")
        seen.add(path)
        if any(gate < first for gate in gates):
            declarations[path] = (maximum, gates)
    if len(declarations) > MAX_RETAINED_FILES:
        raise ValueError("retained declarations exceed their population bound")
    return declarations


def _read_member(raw_member, locator, publication, inventory):
    member = _object(raw_member, "retained.member")
    path = _safe_relative(member.get(locator), "retained.member.path", file=True)
    digest = _string(member.get("sha256"), "retained.member.sha256")
    size = _integer(member.get("size"), "retained.member.size")
    if size < 0:
        raise ValueError("retained report exceeds the per-file transport bound")
    if inventory.get(path) != {"sha256": digest, "size": size}:
        raise ValueError("retained member differs from original publication inventory")
    item = FrozenRetainedFile(path, digest, size, publication["generation"])
    return item
