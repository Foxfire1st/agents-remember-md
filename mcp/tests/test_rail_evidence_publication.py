"""Real producer output and exact capture bytes across the report export boundary."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from agents_remember.certification.certificate_models import CreationProvenance
from agents_remember.certification.certification_lane import compile_certification_lane
from agents_remember.certification.models import (
    RailArtifactResult,
    RailEvidenceReference,
    RailTerminalObservation,
)
from agents_remember.certification.results import build_rail_result
from agents_remember.memory_quality.gate_five_rails import gate_five_memory_rails
from agents_remember.worktrees.modules.quality import clean_executor
from agents_remember.worktrees.modules.quality.certification_evidence import verify_result_evidence
from agents_remember.worktrees.modules.quality.published_manifest import (
    load_published_quality_manifest,
)
from agents_remember_test_support.code_quality import profile_rails
from agents_remember_test_support.testing import pytest_phase_reporter
from repository_profile_test_support import (
    agents_remember_profile_execution,
    write_source_selection_artifacts,
)
from source_selection_test_support import write_ambient_selection

_ROOT = Path(__file__).resolve().parents[2]


def _modules():
    source = str(_ROOT / ".dagger/src")
    if source not in sys.path:
        sys.path.insert(0, source)
    return (
        importlib.import_module("agents_remember_quality.rail_emission"),
        importlib.import_module("agents_remember_quality.profile_publication"),
        importlib.import_module("agents_remember_quality.profile_results"),
    )


@dataclass(frozen=True)
class _File:
    payload: bytes

    async def size(self):
        return len(self.payload)


@dataclass(frozen=True)
class _Container:
    """Immutable SDK contract double; file bytes and command stdout stay separate."""

    files: dict[str, bytes] = field(default_factory=dict)
    out: str = ""
    err: str | OSError = ""
    commands: tuple[tuple[str, ...], ...] = ()

    async def stdout(self):
        return self.out

    async def stderr(self):
        if isinstance(self.err, OSError):
            raise self.err
        return self.err

    async def exists(self, path, **kwargs):
        assert kwargs["do_not_follow_symlinks"] is True
        return path in self.files

    def file(self, path):
        return _File(self.files[path])

    def with_file(self, path, source):
        return replace(self, files={**self.files, path: source.payload})

    def with_new_file(self, path, *, contents):
        return replace(self, files={**self.files, path: contents.encode()})

    def with_exec(self, command, **_kwargs):
        history = (*self.commands, tuple(command))
        if command[0] == "sha256sum":
            result = subprocess.run(
                ["sha256sum"], input=self.files[command[1]], capture_output=True, check=True
            )
            return replace(self, out=result.stdout.decode(), err="", commands=history)
        assert command[:2] == ["sh", "-c"]
        assert command[3] == "publish-rail-evidence"
        result = subprocess.run(
            ["base64", "--decode"], input=self.files[command[5]], capture_output=True, check=True
        )
        return replace(
            self, files={**self.files, command[6]: result.stdout}, out="", err="", commands=history
        )

    async def sync(self):
        return self

    def directory(self, root):
        return {
            name.removeprefix(root + "/"): payload
            for name, payload in self.files.items()
            if name.startswith(root + "/")
        }


def _rail(*, maximum=1024, artifact=None):
    return SimpleNamespace(
        identity="teardown-process-cleanliness",
        identity_key="teardown-process-cleanliness@1.0.0",
        execution={"successExitCodes": [0]},
        evidence_contract=(
            {"evidenceId": "teardown-process-cleanliness-evidence", "maxBytes": maximum},
        ),
        environments=(),
        output_artifacts=(
            () if artifact is None else ({"artifactId": artifact, "requiredOnPass": True},)
        ),
    )


def _plan():
    execution = agents_remember_profile_execution(candidate_tree="a" * 40)
    return SimpleNamespace(
        published_artifacts=(),
        decoder=execution.decoder.model_dump(mode="json"),
        profile_digest=execution.admitted.canonical.profileDigest,
        plan_digest=execution.plan.planDigest,
        runtime_authority_digest=os.environ.get("AR_DAGGER_RUNTIME_AUTHORITY_DIGEST") or "c" * 64,
    )


def _export(progress):
    _emission, publication, results = _modules()
    plan = _plan()
    output = asyncio.run(publication.prepare_profile_reports(progress, plan, reports="/reports"))
    payload = results.profile_result_payload(
        progress, started_at="2026-01-01T00:00:00Z", mode="targeted", attempt_nonce="0" * 32
    )
    return publication.export_profile_reports(output, progress, plan, payload, reports="/reports")


def test_capture_export_preserves_unicode_tail_and_the_next_rail_container():
    emission, _publication, results = _modules()
    source = _Container(out="x€end", err="")
    progress = results.QualityProgress(container=source, exit_code=0)
    binding = asyncio.run(
        emission.attach_rail_terminal_bindings(progress, _rail(maximum=5), reports="/reports")
    )
    assert progress.container is source
    assert source.commands == () and source.files == {}
    exported = _export(progress)
    evidence = binding["evidence"][0]
    retained = exported[evidence["reference"]]
    assert retained == "x€end".encode()[-5:]
    with pytest.raises(UnicodeDecodeError):
        retained.decode()
    assert evidence["size"] == len(retained)
    assert evidence["sha256"] == hashlib.sha256(retained).hexdigest()
    assert progress.container is source
    assert asyncio.run(progress.container.stdout()) == "x€end"


@pytest.mark.parametrize("out,err", [("", ""), ("first", "second")])
def test_observed_empty_and_both_output_streams_have_actual_retained_files(out, err):
    emission, _publication, results = _modules()
    source = _Container(out=out, err=err)
    progress = results.QualityProgress(container=source, exit_code=0)
    binding = asyncio.run(
        emission.attach_rail_terminal_bindings(progress, _rail(), reports="/reports")
    )
    evidence = binding["evidence"][0]
    assert (
        _export(progress)[evidence["reference"]]
        == "\n".join(part for part in (out, err) if part).encode()
    )
    assert "unavailableEvidenceStreams" not in binding


def test_unavailable_stream_is_partial_diagnostic_capture_without_certifying_evidence():
    emission, _publication, results = _modules()
    progress = results.QualityProgress(
        container=_Container(out="available stdout", err=OSError("capture unavailable")),
        exit_code=1,
    )
    binding = asyncio.run(
        emission.attach_rail_terminal_bindings(progress, _rail(), reports="/reports")
    )
    assert binding["evidence"] == []
    assert binding["unavailableEvidenceStreams"] == ["stderr"]
    assert (
        _export(progress)["rail-evidence/teardown-process-cleanliness@1.0.0.log"]
        == b"available stdout"
    )


@pytest.mark.parametrize(
    "status", ["pass-missing", "pass-unavailable", "failed", "skipped", "not-executed"]
)
def test_terminal_binding_preserves_actual_failure_and_refuses_missing_proof(status):
    emission, _publication, results = _modules()
    rail = _rail(artifact="teardown-proof")
    source = _Container(
        out="actual rail output", err=OSError("unavailable") if status == "pass-unavailable" else ""
    )
    code = 1 if status in {"failed", "not-executed"} else 0
    progress = results.QualityProgress(
        container=source,
        exit_code=code,
        completed=[rail.identity] if status.startswith("pass-") else [],
        skipped=[rail.identity] if status == "skipped" else [],
        step_exit_codes={rail.identity: code},
    )
    outcome = asyncio.run(
        emission.terminal_rail_outcome(
            progress, rail, reports="/reports", executed=status != "not-executed"
        )
    )
    assert progress.container is source
    if status.startswith("pass-"):
        assert outcome["status"] == "fail" and outcome["exitCode"] == 66
        assert rail.identity not in progress.completed
    elif status == "skipped":
        assert outcome["status"] == "skipped" and outcome["artifacts"] == []
    else:
        assert outcome["status"] == "fail" and outcome["exitCode"] == 1
    if status == "not-executed":
        assert progress.retained_captures == {} and "evidence" not in outcome


def _teardown_inputs(root: Path) -> tuple[Path, Path]:
    """Serialize checkpoint fixtures with the real owners, without running the scenario."""
    write_ambient_selection(root)
    runner = _ROOT / "scripts/e2e_harness/run.py"
    completed = subprocess.run(
        [sys.executable, "-B", "-", str(runner), str(root)],
        input="""
import argparse
import runpy
import sys
from pathlib import Path

harness = runpy.run_path(sys.argv[1])
root = Path(sys.argv[2])
reports = []
for index in range(1, harness["RUN_COUNT"] + 1):
    recorder = harness["CheckpointRecorder"](scenario="checkpoint-serialization-fixture")
    recorder.check(
        harness["C10"],
        actual={"residualSessions": [], "cleanup": {"status": "clean"}},
        passed=True,
    )
    report = recorder.report(run=index, status="passed", fixture=True, retry=False,
        mode="targeted", diffBase="b" * 40, selectedPaths=["scripts/e2e_harness/run.py"], candidate={"tree": "a" * 40})
    harness["write_json"](root / f"run-{index}.json", report)
    reports.append(report)
context = harness["_RunContext"](
    reports=root,
    args=argparse.Namespace(mode="targeted", diff_base="b" * 40),
    repository=Path(sys.argv[1]).parents[2],
    selected=("scripts/e2e_harness/run.py",),
    candidate={"tree": "a" * 40, "fixture": "checkpoint serialization only; no scenario execution"},
    command="checkpoint-serialization-fixture",
    invocation="checkpoint-serialization-fixture",
)
harness["_write_run_summary"](context, passed=True, changed=(), run_reports=reports)
""",
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(runner.parent), str(_ROOT / "mcp/src"), str(_ROOT / "mcp/test_support")]
            ),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return root / "summary.json", root / "run-1.json"


def test_real_teardown_producer_bytes_reach_the_emitted_binding_and_export(tmp_path, capsys):
    summary, detail = _teardown_inputs(tmp_path)
    proof = tmp_path / "teardown-proof.json"
    assert profile_rails._verify_teardown(summary, proof, tmp_path / "source-selection.json") == 0
    actual_output = capsys.readouterr().out
    actual_proof = proof.read_bytes()
    parsed = json.loads(actual_proof)
    assert parsed["summarySha256"] == hashlib.sha256(summary.read_bytes()).hexdigest()
    assert parsed["replications"][0]["sha256"] == hashlib.sha256(detail.read_bytes()).hexdigest()
    emission, _publication, results = _modules()
    container = _Container(files={"/reports/teardown-proof.json": actual_proof}, out=actual_output)
    progress = results.QualityProgress(container=container, exit_code=0)
    bindings = asyncio.run(
        emission.attach_rail_terminal_bindings(
            progress, _rail(artifact="teardown-proof"), reports="/reports"
        )
    )
    artifact = bindings["artifacts"][0]
    exported = _export(progress)
    assert exported[artifact["evidenceRef"]] == actual_proof
    assert artifact["sha256"] == hashlib.sha256(actual_proof).hexdigest()
    assert exported[bindings["evidence"][0]["reference"]] == actual_output.encode()
    assert progress.container is container
    assert asyncio.run(container.stdout()) == actual_output
    execution = agents_remember_profile_execution(candidate_tree="a" * 40)
    lane = compile_certification_lane(
        execution.admitted.canonical,
        execution.plan,
        provenance=CreationProvenance(
            createdAt="2026-01-01T00:00:00Z",
            producer="producer-test",
            evidenceRef="fixture://teardown",
        ),
        memory_rails=gate_five_memory_rails(execution.selection.selectionId),
    )
    gate_plan = next(item for item in lane.certificationPlan.gates if item.gate == 4)
    producer = next(
        rail for rail in gate_plan.rails if rail.identity.railId == "teardown-process-cleanliness"
    )
    result = build_rail_result(
        gate_plan,
        RailTerminalObservation(
            rail=producer.identity,
            status="pass",
            code="teardown-process-cleanliness-pass",
            artifacts=tuple(RailArtifactResult(**item) for item in bindings["artifacts"]),
            evidence=tuple(RailEvidenceReference(**item) for item in bindings["evidence"]),
        ),
    )
    exported_root = tmp_path / "exported"
    for relative, contents in exported.items():
        target = exported_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
    write_source_selection_artifacts(exported_root, execution.plan)
    retained_reports = tmp_path / "retained"
    clean_executor._publish_reports(
        exported_root, retained_reports, candidate_tree="a" * 40, profile_execution=execution
    )
    publication = load_published_quality_manifest(retained_reports)
    verify_result_evidence(retained_reports, publication, (result,))
    assert (
        clean_executor.published_report_path_from_manifest(
            retained_reports, publication, artifact["evidenceRef"]
        ).read_bytes()
        == actual_proof
    )


@pytest.mark.parametrize("failure", ["missing", "failed"])
def test_teardown_refusal_never_writes_a_passing_proof(tmp_path, failure):
    summary, detail = _teardown_inputs(tmp_path)
    if failure == "missing":
        summary.unlink()
    else:
        report = json.loads(detail.read_bytes())
        report["checkpoints"][0]["status"] = "failed"
        detail.write_text(json.dumps(report))
    proof = tmp_path / "proof.json"
    with pytest.raises(RuntimeError):
        profile_rails._verify_teardown(summary, proof, tmp_path / "source-selection.json")
    assert not proof.exists()


def test_teardown_refuses_obsolete_checkpoint_identity(tmp_path):
    summary, detail = _teardown_inputs(tmp_path)
    report = json.loads(detail.read_bytes())
    checkpoint = report["checkpoints"][0]
    assert checkpoint["status"] == "passed" and checkpoint["id"] != "C10"
    checkpoint["id"] = "C10"
    detail.write_text(json.dumps(report))
    proof = tmp_path / "proof.json"
    with pytest.raises(RuntimeError, match="lacks a passing teardown checkpoint"):
        profile_rails._verify_teardown(summary, proof, tmp_path / "source-selection.json")
    assert not proof.exists()


@dataclass
class _NodeReport:
    nodeid: str
    outcome: str
    when: str


def test_provider_reporter_persists_actual_terminal_node_facts(tmp_path, monkeypatch):
    target = tmp_path / "provider-integration-result.json"
    session = cast(
        pytest.Session,
        SimpleNamespace(
            config=SimpleNamespace(getoption=lambda name: target),
            items=[SimpleNamespace(nodeid="provider-case")],
        ),
    )
    monkeypatch.setattr(pytest_phase_reporter, "_STATE", pytest_phase_reporter._PhaseState())
    pytest_phase_reporter.pytest_sessionstart(session)
    pytest_phase_reporter.pytest_collection_finish(session)
    pytest_phase_reporter.pytest_runtest_logreport(
        _NodeReport(nodeid="provider-case", outcome="failed", when="call")
    )
    pytest_phase_reporter.pytest_sessionfinish(session, 1)
    payload = json.loads(target.read_bytes())
    assert payload["pytestExitCode"] == 1
    assert payload["nodes"] == [{"nodeId": "provider-case", "outcome": "failed"}]


def test_provider_command_loads_its_option_owner_and_writes_a_real_pytest_report(tmp_path):
    profile = json.loads((_ROOT / "mcp/certification-profile-v1.json").read_text())
    rail = next(
        rail
        for rail in profile["rails"]
        if rail["identity"]["railId"] == "provider-runtime-integration"
    )
    command = rail["execution"]["command"]
    assert command[command.index("-p") + 1] == pytest_phase_reporter.__name__
    probe = tmp_path / "test_provider_command_probe.py"
    probe.write_text("def test_provider_command_probe():\n    assert True\n")
    arguments = [
        str(probe) if value == "mcp/tests/test_provider_workflow_integration.py" else value
        for value in command[1:]
    ]
    completed = subprocess.run(
        [sys.executable, *(value.replace("{reports}", str(tmp_path)) for value in arguments)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [str(_ROOT / "mcp/src"), str(_ROOT / "mcp/test_support")]
            ),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads((tmp_path / "provider-integration-result.json").read_bytes())
    assert payload["pytestExitCode"] == 0
    assert payload["nodes"] == [
        {
            "nodeId": "test_provider_command_probe.py::test_provider_command_probe",
            "outcome": "passed",
        }
    ]


def test_gate_four_profile_commands_admit_each_real_producer_publication():
    profile = json.loads((_ROOT / "mcp/certification-profile-v1.json").read_text())
    rails = {rail["identity"]["railId"]: rail for rail in profile["rails"]}
    publications = {item["path"]: item for item in profile["publishedArtifacts"]}
    command = rails["dashboard-browser-e2e"]["execution"]["command"]
    assert "PLAYWRIGHT_JSON_OUTPUT_FILE={reports}/dashboard-e2e-result.json" in command
    assert "--reporter=line,json" in command
    assert (
        "--ar-pytest-phase-report={reports}/provider-integration-result.json"
        in rails["provider-runtime-integration"]["execution"]["command"]
    )
    command = rails["teardown-process-cleanliness"]["execution"]["command"]
    assert command[command.index("--proof") + 1] == "{reports}/teardown-proof.json"
    for filename in (
        "dashboard-e2e-result.json",
        "provider-integration-result.json",
        "teardown-proof.json",
    ):
        assert publications[filename]["publisherGates"] == [4]
        assert publications[filename]["maxBytes"] > 0
