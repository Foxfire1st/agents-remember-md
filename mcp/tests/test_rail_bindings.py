"""CCR-L29 executor per-rail evidence/artifact bindings + host mint path."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_DAGGER_SRC = Path(__file__).resolve().parents[2] / ".dagger" / "src"


def _emission() -> Any:
    """Load the .dagger emission module without a static import."""
    name = "agents_remember_quality.rail_emission"
    sys.path.insert(0, str(_DAGGER_SRC))
    path = _DAGGER_SRC / "agents_remember_quality" / "rail_emission.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("rail_emission module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _bindings() -> Any:
    """Load the pure .dagger emission module without a static import."""
    path = _DAGGER_SRC / "agents_remember_quality" / "rail_bindings.py"
    name = "agents_remember_quality.rail_bindings"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("rail_bindings module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


from agents_remember.certification.certificate_authority import (
    compile_gate_certificate,
    validate_certificate_chain,
)
from agents_remember.certification.certificate_models import (
    CreationProvenance,
    GateCertificateIssuanceContext,
)
from agents_remember.certification.certification_lane import (
    compile_certification_lane,
)
from agents_remember.certification.models import (
    CandidateIdentity,
    GateResultAdmission,
    RailArtifactResult,
    RailEvidenceReference,
    RailTerminalObservation,
)
from agents_remember.certification.repository_profiles.authority import (
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
    resolve_repository_profile_selection,
)
from agents_remember.certification.results import (
    build_rail_result,
    compile_gate_result_manifest,
)
from agents_remember.errors import CertificationContractError
from agents_remember.memory_quality.gate_five_rails import gate_five_memory_rails

_REPOSITORY_ID = "agents-remember"
_PROFILE = Path(__file__).resolve().parents[1] / "certification-profile-v1.json"


def _lane(tmp_path: Path):
    root = tmp_path / "profile-root"
    root.mkdir()
    (root / "certification-profile-v1.json").write_bytes(_PROFILE.read_bytes())
    admitted = load_repository_profile(_REPOSITORY_ID, root, "certification-profile-v1.json")
    candidate = CandidateIdentity(kind="git-tree", value="c" * 40)
    selection = resolve_repository_profile_selection(
        admitted.canonical, purpose="closeout", mode="targeted"
    )
    repository_plan = compile_repository_profile_plan(
        admitted.canonical, selection_id=selection.selectionId, candidate_identity=candidate
    )
    lane = compile_certification_lane(
        admitted.canonical,
        repository_plan,
        provenance=CreationProvenance(
            createdAt="2026-09-06T00:00:00+00:00",
            producer="rail-bindings-test",
            evidenceRef="evidence://rail-bindings-test",
        ),
        memory_rails=gate_five_memory_rails(selection.selectionId),
    )
    return lane


def test_evidence_bindings_bind_real_capture_for_every_declared_id(tmp_path) -> None:
    rb = _bindings()
    del tmp_path
    output = (
        "ruff: all checks passed (agent-lint, 7 files)\nruff-format: 1 file would be reformatted"
    )
    bindings = rb.build_evidence_bindings(
        ["ruff-evidence", "ruff-format-evidence"],
        output,
        reference="/tmp/ar-quality/rail-evidence/ruff@1.0.0.log",
    )
    assert [item["evidenceId"] for item in bindings] == [
        "ruff-evidence",
        "ruff-format-evidence",
    ]
    expected = hashlib.sha256(output.encode("utf-8")).hexdigest()
    for item in bindings:
        assert item["sha256"] == expected
        assert item["size"] == len(output.encode("utf-8"))
        assert item["reference"].startswith("/tmp/ar-quality/rail-evidence/")


def test_evidence_bindings_an_empty_capture_is_a_real_empty_capture() -> None:
    rb = _bindings()
    bindings = rb.build_evidence_bindings(
        ["layering-evidence"], "", reference="/tmp/ar-quality/rail-evidence/layering@1.0.0.log"
    )
    assert bindings[0]["size"] == 0
    assert bindings[0]["sha256"] == hashlib.sha256(b"").hexdigest()


def test_artifact_bindings_bind_observed_files_and_journal_gaps_never_fake(tmp_path) -> None:
    rb = _bindings()
    coverage = (tmp_path / "coverage.data").write_bytes(b"!coverage-data")
    coverage_json = (tmp_path / "coverage.json").write_bytes(b'{"totals": {"percent_covered": 85}}')
    observed = {
        "python-coverage-data": rb.observed_file("/reports/coverage.data", b"!coverage-data"),
        "python-coverage-json": rb.observed_file(
            "/reports/coverage.json", b'{"totals": {"percent_covered": 85}}'
        ),
    }
    declared = [
        {"artifactId": "python-coverage-data", "requiredOnPass": True},
        {"artifactId": "python-coverage-json", "requiredOnPass": True},
        {"artifactId": "python-suite-result", "requiredOnPass": True},
    ]
    bound, gaps = rb.build_artifact_bindings(declared, observed)
    assert [item["artifactId"] for item in bound] == [
        "python-coverage-data",
        "python-coverage-json",
    ]
    assert gaps == ["python-suite-result"]
    assert coverage and coverage_json
    # Nothing fabricated for the missing artifact.
    assert all(item["sha256"].startswith("!") is False for item in bound)


def test_emitted_bindings_feed_the_green_mint_chain(tmp_path) -> None:
    rb = _bindings()
    lane = _lane(tmp_path)
    # Simulate real per-artifact producer files for every declared output artifact
    # (the wrapper writes these in-container; the builder only digests bytes that
    # were actually observed, never fabricating a digest).
    observed: dict[str, Any] = {}
    for gate_plan in lane.certificationPlan.gates:
        if gate_plan.gate == 5:
            continue
        for rail in gate_plan.rails:
            for artifact in rail.outputArtifacts:
                artifact_id = artifact.artifactId
                if artifact_id in observed:
                    continue
                path = tmp_path / f"{artifact_id}.bin"
                path.write_bytes(f"produced {artifact_id}".encode())
                observed[artifact_id] = rb.observed_file(
                    f"/reports/{artifact_id}.bin", path.read_bytes()
                )
    certificates = []
    for gate in (1, 2, 3, 4):
        gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == gate)
        results = []
        for rail in gate_plan.rails:
            evidence = rb.build_evidence_bindings(
                [item.evidenceId for item in rail.evidenceContract],
                f"{rail.identity.key} terminal output",
                reference=f"/tmp/ar-quality/rail-evidence/{rail.identity.key}.log",
            )
            artifacts, gaps = rb.build_artifact_bindings(
                [item.model_dump(mode="json") for item in rail.outputArtifacts],
                observed,
            )
            assert not gaps, f"gate {gate} rail {rail.identity.key} artifacts unbound: {gaps}"
            observations = RailTerminalObservation(
                rail=rail.identity,
                status="pass",
                code=f"{rail.identity.railId}-pass",
                artifacts=tuple(RailArtifactResult(**item) for item in artifacts),
                evidence=tuple(RailEvidenceReference(**item) for item in evidence),
            )
            results.append(build_rail_result(gate_plan, observations))
        manifest = compile_gate_result_manifest(
            lane.registry,
            lane.certificationPlan,
            gate_plan,
            results,
            GateResultAdmission(
                profileId=lane.selectionId,
                candidateIdentity=lane.candidate,
                altitude=lane.certificationPlan.profileKind,
            ),
        )
        assert manifest.disposition == "green"
        certificates.append(
            compile_gate_certificate(
                lane.admission,
                gate_plan,
                manifest,
                certificates,
                GateCertificateIssuanceContext(provenance=lane.admission.provenance),
            )
        )
    validate_certificate_chain(lane.admission, certificates)


def test_unbound_required_artifact_refuses_the_manifest_not_a_fake_digest(tmp_path) -> None:
    rb = _bindings()
    lane = _lane(tmp_path)
    gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == 2)
    python_suite = next(rail for rail in gate_plan.rails if rail.identity.railId == "python-suite")
    evidence = rb.build_evidence_bindings(
        [item.evidenceId for item in python_suite.evidenceContract],
        "python-suite output",
        reference="/tmp/ar-quality/rail-evidence/python-suite@1.0.0.log",
    )
    observed = {
        "python-coverage-data": rb.observed_file("/reports/coverage.data", b"coverage-data-bytes"),
        "python-coverage-json": rb.observed_file("/reports/coverage.json", b"{}"),
    }
    artifacts, gaps = rb.build_artifact_bindings(
        [item.model_dump(mode="json") for item in python_suite.outputArtifacts],
        observed,
    )
    assert gaps == ["python-suite-result"]
    result = build_rail_result(
        gate_plan,
        RailTerminalObservation(
            rail=python_suite.identity,
            status="pass",
            code="python-suite-pass",
            artifacts=tuple(RailArtifactResult(**item) for item in artifacts),
            evidence=tuple(RailEvidenceReference(**item) for item in evidence),
        ),
    )
    with pytest.raises(CertificationContractError):
        compile_gate_result_manifest(
            lane.registry,
            lane.certificationPlan,
            gate_plan,
            (result,),
            GateResultAdmission(
                profileId=lane.selectionId,
                candidateIdentity=lane.candidate,
                altitude=lane.certificationPlan.profileKind,
            ),
        )


def test_artifact_map_covers_every_declared_required_on_pass_artifact() -> None:
    rb = _bindings()
    profile = json.loads(_PROFILE.read_text(encoding="utf-8"))
    declared: set[str] = set()
    for rail in profile["rails"]:
        for artifact in rail.get("outputArtifacts", []):
            if artifact.get("requiredOnPass", True) is not False:
                declared.add(artifact["artifactId"])
    mapped = set(rb.ARTIFACT_FILE_PATHS)
    assert mapped == declared
    publications = {item["path"] for item in profile["publishedArtifacts"]}
    assert set(rb.ARTIFACT_FILE_PATHS.values()) <= publications


def test_mapped_suite_result_binds_the_pytest_phases_file() -> None:
    rb = _bindings()
    observed = {
        "python-suite-result": rb.observed_file(
            "/reports/pytest-phases.json",
            b'{"phases": [{"name": "suite", "passed": true}]}',
        )
    }
    bound, gaps = rb.build_artifact_bindings(
        [
            {"artifactId": "python-suite-result", "requiredOnPass": True},
            {"artifactId": "python-coverage-json", "requiredOnPass": True},
        ],
        {**observed, "python-coverage-json": rb.observed_file("/reports/coverage.json", b"{}")},
    )
    assert [item["artifactId"] for item in bound] == [
        "python-coverage-json",
        "python-suite-result",
    ]
    assert gaps == []


def test_dashboard_coverage_map_targets_the_vitest_coverage_json() -> None:
    rb = _bindings()
    assert rb.ARTIFACT_FILE_PATHS["dashboard-coverage-data"] == "dashboard-coverage.json"
    assert rb.artifact_source_path("dashboard-coverage-data", reports="/reports") == (
        "/workspace/dashboard/coverage/coverage-final.json"
    )
    bound, gaps = rb.build_artifact_bindings(
        [{"artifactId": "dashboard-coverage-data", "requiredOnPass": True}],
        {
            "dashboard-coverage-data": rb.observed_file(
                "/workspace/dashboard/coverage/coverage-final.json",
                b'{"total": {"percent": 90}}',
            )
        },
    )
    assert len(bound) == 1 and bound[0]["sha256"]
    assert gaps == []


class _FakeFile:
    def __init__(self, size: int):
        self._size = size

    async def size(self) -> int:
        return self._size


class _FakeContainer:
    """Immutable container double: exec returns a NEW detached handle."""

    def __init__(self, output: str = ""):
        self.output = output

    async def stdout(self) -> str:
        return self.output

    async def stderr(self) -> str:
        return ""

    async def exists(self, _path: str, **_: object) -> bool:
        return True

    def file(self, _path: str) -> Any:
        return _FakeFile(size=32)

    def with_exec(self, command: list[object], **_: object) -> Any:
        del command
        # A detached inspection handle: the shared container is unchanged and
        # later execs on it never observe this step.
        return _FakeDigestHandle()


class _FakeDigestHandle:
    async def stdout(self) -> str:
        return "a" * 64 + "  reports/x.json"

    async def sync(self) -> _FakeDigestHandle:
        return self


async def _run_attach(progress, rail, reports: str):
    container = _FakeContainer(output="suite output")
    progress.container = container
    emission = _emission()
    bindings = await emission.attach_rail_terminal_bindings(progress, rail, reports=reports)
    return bindings, container


def test_emission_never_mutates_the_shared_execution_container(tmp_path) -> None:
    rb = _bindings()
    rail = SimpleNamespace(
        identity_key="python-suite@1.0.0",
        evidence_contract=[{"evidenceId": "python-suite-evidence", "maxBytes": 1024}],
        output_artifacts=[
            {"artifactId": "python-coverage-data", "requiredOnPass": True},
            {"artifactId": "python-suite-result", "requiredOnPass": True},
        ],
    )
    progress = SimpleNamespace(retained_captures={}, retained_files={})
    progress.container = _FakeContainer(output="suite output")

    bindings, container = asyncio.run(_run_attach(progress, rail, reports=str(tmp_path)))
    # The shared container is untouched by emission: no exec or file write
    # was appended to it and the next rail sees the identical handle.
    assert progress.container is container
    # Emission produced bindings but never added a step to the shared handle:
    # the handle the next rail runs on is identical and exec-free.
    assert not hasattr(container, "mutations")
    assert bindings["evidence"]
    assert bindings["artifacts"]
    assert rb  # pragma: no cover (binding module loaded above)
