"""Frozen suffix interpretation and real retained-byte verification at the Dagger seam."""

from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.worktrees.modules.quality.execution.retained_reports import (
    retained_report_inventory,
    snapshot_retained_reports,
)
from repository_profile_test_support import (
    FakeContainer,
    FakeDag,
    FakeFile,
    FakeSource,
    fixture_environment_census,
)
from test_agents_remember_quality import execution_manifest, load_dagger_module
from test_code_certification_execution import selected_execution


class _Directory:
    def __init__(self, root: Path):
        self.root = root

    def file(self, relative):
        return FakeFile((self.root / relative).read_text())


class _RetainedContainer(FakeContainer):
    """Engine contract double, executing the production verification shell on real files."""

    transport: _Directory | None = None
    fail_rail: str | None = None

    def with_directory(self, *args):
        if args[0] == "/tmp/ar-retained-reports":
            assert isinstance(args[1], _Directory)
            self.transport = args[1]
        return super().with_directory(*args)

    def with_exec(self, command, **kwargs):
        if len(command) > 5 and command[4] == "verify-retained-reports":
            assert self.transport is not None
            actual = [*command[:5], str(self.transport.root), *command[6:]]
            verified = subprocess.run(actual, check=False, capture_output=True, text=True)
            return FakeContainer([verified.returncode])
        result = super().with_exec(command, **kwargs)
        if self.fail_rail is not None and self.fail_rail in command:
            self.exit_codes.insert(0, 17)
        return result


def _manifest(selected, request):
    plan = selected.run.repositoryPlan
    assert plan.sourceSelection is not None
    diff_base = plan.sourceSelection.baseCommit
    manifest = json.loads(
        execution_manifest(
            mode="targeted",
            candidate=plan.candidateIdentity.value,
            diff_base=diff_base,
            changed_paths=plan.sourceSelection.changedPaths,
        ).value
    )
    # Retain the actual frozen plan, including its real base tree and source census.
    manifest["profilePlan"] = plan.model_dump(mode="json")
    manifest["codeExecution"] = selected.payload(diff_base=diff_base)
    manifest["retainedReports"] = [item.payload() for item in retained_report_inventory(selected)]
    return manifest


def _redigest_execution(manifest):
    execution = manifest["codeExecution"]
    execution["executionDigest"] = content_digest(
        {key: value for key, value in execution.items() if key != "executionDigest"}
    )


@pytest.mark.parametrize("first", (1, 2, 3, 4))
def test_frozen_suffix_starts_only_selected_gates_and_preserves_original_results(tmp_path, first):
    selected, request, _profile = selected_execution(tmp_path, first)
    module = load_dagger_module()
    directory = tmp_path / "retained"
    snapshot_retained_reports(
        selected, reports=request.worktree_group / "reports", destination=directory
    )
    fake_dag = FakeDag([])
    container = _RetainedContainer([])
    fake_dag.container_value = container
    with (
        patch.object(module, "dag", fake_dag),
        patch.object(
            module, "export_profile_reports", wraps=module.export_profile_reports
        ) as export,
    ):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                FakeFile(json.dumps(_manifest(selected, request))),
                retained_reports=_Directory(directory) if first > 1 else None,
            )
        )
    assert result.exit_code == 0
    export.assert_called_once()
    output = export.call_args.args[0]
    assert isinstance(output, _RetainedContainer)
    assert result.reports == export.call_args.kwargs["reports"]
    payload = json.loads(output.files["/reports/clean-quality-results.json"])
    assert [row["gate"] for row in payload["gates"] if row["started"]] == list(range(first, 5))
    for row, original in zip(payload["gates"][: first - 1], selected.retained, strict=True):
        assert row["disposition"] == "reused"
        assert row["zeroStart"] is True
        assert row["rails"] == []
        assert row["certificateDigest"] == original.certificate.certificateDigest
        assert row["resultManifestDigest"] == original.result.manifestDigest
        assert row["originalPublication"] == original.payload()["publication"]
    skipped_rails = {
        rail.identity.railId
        for gate in selected.run.repositoryPlan.gates
        if gate.gate < first
        for rail in gate.rails
    }
    assert not skipped_rails.intersection(payload["attemptedSteps"])
    assert not skipped_rails.intersection(payload["completedSteps"])
    if first > 1:
        reconstructed = payload["environmentReconstruction"]
        assert len(reconstructed) == 1
        assert reconstructed[0]["status"] == "verified"
        assert reconstructed[0]["command"] == ["npm", "ci"]
        assert all(not row["started"] for row in payload["gates"][: first - 1])
    else:
        assert "environmentReconstruction" not in payload
    if first >= 3:
        assert output.files["/reports/coverage.json"] == (directory / "coverage.json").read_text()
        assert (
            output.files["/workspace/dashboard/coverage/coverage-final.json"]
            == (directory / "dashboard-coverage.json").read_text()
        )


@pytest.mark.parametrize("fault", ("missing", "corrupt", "symlink", "parent-symlink"))
def test_dagger_rechecks_actual_transported_bytes_before_any_suffix_rail(tmp_path, fault):
    selected, request, _profile = selected_execution(tmp_path)
    directory = tmp_path / "retained"
    snapshot_retained_reports(
        selected, reports=request.worktree_group / "reports", destination=directory
    )
    target = directory / "coverage.json"
    if fault == "missing":
        target.unlink()
    elif fault == "corrupt":
        target.write_text("altered transport")
    elif fault == "symlink":
        target.rename(tmp_path / "outside")
        target.symlink_to(tmp_path / "outside")
    else:
        (directory / "rail-evidence").rename(tmp_path / "outside-dir")
        (directory / "rail-evidence").symlink_to(tmp_path / "outside-dir", target_is_directory=True)
    module = load_dagger_module()
    fake_dag = FakeDag([])
    container = _RetainedContainer([])
    fake_dag.container_value = container
    with (
        patch.object(module, "dag", fake_dag),
        pytest.raises(ValueError, match="transport is missing, unsafe, corrupt"),
    ):
        asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                FakeFile(json.dumps(_manifest(selected, request))),
                retained_reports=_Directory(directory),
            )
        )
    assert not any(
        "python-crap" in command or "python-diff-coverage" in command
        for command in container.commands
    )
    assert "/reports/coverage.json" not in container.files


@pytest.mark.parametrize(
    "fault", ("digest", "base", "plan", "prefix", "inventory", "first-five", "zero-start", "result")
)
def test_detached_or_contradictory_execution_inputs_refuse_before_engine_use(tmp_path, fault):
    selected, request, _profile = selected_execution(tmp_path)
    module = load_dagger_module()
    manifest = _manifest(selected, request)
    execution = manifest["codeExecution"]
    if fault == "digest":
        execution["executionDigest"] = "0" * 64
    elif fault == "base":
        manifest["diffBase"] = "another-base"
    elif fault == "plan":
        execution["run"]["repositoryPlan"]["candidateIdentity"]["value"] = "f" * 40
        execution["run"]["runDigest"] = content_digest(
            {key: value for key, value in execution["run"].items() if key != "runDigest"}
        )
    elif fault == "prefix":
        execution["retained"].pop()
    elif fault == "inventory":
        manifest["retainedReports"][0]["sha256"] = "e" * 64
    elif fault in {"first-five", "zero-start"}:
        reuse = execution["reusePlan"]
        reuse["firstGateToRun" if fault == "first-five" else "zeroGateStarts"] = (
            5 if fault == "first-five" else True
        )
    else:
        execution["retained"][0]["result"]["disposition"] = "red"
    if fault != "digest":
        _redigest_execution(manifest)
    fake_dag = FakeDag([])
    with patch.object(module, "dag", fake_dag), pytest.raises(ValueError):
        asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(), object(), FakeFile(json.dumps(manifest))
            )
        )
    assert fake_dag.container_value.commands == []


def test_failed_suffix_publishes_complete_red_gate_and_zero_start_successors(tmp_path):
    selected, request, _profile = selected_execution(tmp_path)
    directory = tmp_path / "retained"
    snapshot_retained_reports(
        selected, reports=request.worktree_group / "reports", destination=directory
    )
    module = load_dagger_module()
    fake_dag = FakeDag([])
    container = _RetainedContainer([])
    container.fail_rail = "python-crap"
    fake_dag.container_value = container
    with (
        patch.object(module, "dag", fake_dag),
        patch.object(
            module, "export_profile_reports", wraps=module.export_profile_reports
        ) as export,
    ):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                FakeFile(json.dumps(_manifest(selected, request))),
                retained_reports=_Directory(directory),
            )
        )
    export.assert_called_once()
    output = export.call_args.args[0]
    assert isinstance(output, _RetainedContainer)
    assert result.reports == export.call_args.kwargs["reports"]
    payload = json.loads(output.files["/reports/clean-quality-results.json"])
    assert result.exit_code == 17
    assert [row["disposition"] for row in payload["gates"]] == [
        "reused",
        "reused",
        "red",
        "not-run",
    ]
    assert len(payload["gates"][2]["rails"]) == 3
    assert payload["gates"][3]["zeroStart"] is True
    assert payload["gates"][3]["rails"] == []


def test_transport_presence_and_declaration_are_exact(tmp_path):
    selected, request, _profile = selected_execution(tmp_path)
    load_dagger_module()
    loader = importlib.import_module("agents_remember_quality.profile_plan")
    transport = importlib.import_module("agents_remember_quality.profile_resume")
    plan = loader.load_execution_manifest(json.dumps(_manifest(selected, request)))
    with pytest.raises(ValueError, match="presence differs"):
        asyncio.run(
            transport.seed_retained_reports(_RetainedContainer([]), plan, None, reports="/reports")
        )
    with pytest.raises(ValueError, match="does not declare"):
        asyncio.run(
            transport.seed_retained_reports(
                _RetainedContainer([]),
                replace(plan, executor={**plan.executor, "retainedReportsArgument": None}),
                _Directory(tmp_path),
                reports="/reports",
            )
        )
    with pytest.raises(ValueError, match="presence differs"):
        asyncio.run(
            transport.seed_retained_reports(
                _RetainedContainer([]),
                replace(plan, resume=None),
                _Directory(tmp_path),
                reports="/reports",
            )
        )


@pytest.mark.parametrize("fault", ("missing", "different-declaration", "omitted-entry"))
def test_environment_reconstruction_refuses_missing_or_changed_original_before_suffix(
    tmp_path, fault
):
    selected, request, _profile = selected_execution(tmp_path)
    load_dagger_module()
    plan_owner = importlib.import_module("agents_remember_quality.profile_plan")
    runtime = importlib.import_module("agents_remember_quality.environment.runtime")
    results = importlib.import_module("agents_remember_quality.profile_results")
    plan = plan_owner.load_execution_manifest(json.dumps(_manifest(selected, request)))
    original_request = next(item for rail in plan.rails for item in rail.environments)
    census = fixture_environment_census(original_request)
    container = FakeContainer([])
    progress = results.QualityProgress(container=container, exit_code=0)
    original_path = "/reports/" + original_request["definition"]["manifestPath"]
    if fault == "different-declaration":
        census["declarationDigest"] = "d" * 64
    elif fault == "omitted-entry":
        census["entries"].pop()
    census["censusDigest"] = content_digest(
        {key: value for key, value in census.items() if key != "censusDigest"}
    )
    if fault != "missing":
        container.files[original_path] = json.dumps(census)
    if fault == "omitted-entry":
        asyncio.run(runtime.reconstruct_environments(progress, plan, reports="/reports"))
        assert progress.exit_code == 66
        assert progress.environment_reconstruction[0]["status"] == "failed"
        assert progress.environment_reconstruction[0]["exitCode"] == 66
    else:
        with pytest.raises(ValueError, match=r"original.*census"):
            asyncio.run(runtime.reconstruct_environments(progress, plan, reports="/reports"))
        assert container.commands == []
        assert progress.environment_reconstruction == []
    assert progress.attempted == (
        ["environment-reconstruction:dashboard-dependencies"] if fault == "omitted-entry" else []
    )
    assert progress.completed == []


@pytest.mark.parametrize("fault", ("oversized", "wrong-gate", "undeclared"))
def test_retained_member_bound_comes_from_frozen_producer_publications(tmp_path, fault):
    selected, request, _profile = selected_execution(tmp_path)
    load_dagger_module()
    owner = importlib.import_module("agents_remember_quality.resume_contract")
    manifest = _manifest(selected, request)
    bounds = owner._publication_bounds(manifest, 3)
    maximum = bounds["coverage.data"][0]
    assert maximum >= 376_020_992
    item = owner.FrozenRetainedFile("coverage.data", "a" * 64, 376_020_992, "b" * 64)
    owner._require_member_bound(item, bounds, 2)
    if fault == "oversized":
        item = replace(item, size=maximum + 1)
    elif fault == "undeclared":
        item = replace(item, path="unadmitted-coverage.data")
    with pytest.raises(ValueError, match="frozen"):
        owner._require_member_bound(item, bounds, 1 if fault == "wrong-gate" else 2)


def _redigest_profile_plan(manifest):
    plan = manifest["profilePlan"]
    for gate in plan["gates"]:
        gate["planDigest"] = content_digest(
            {
                key: value
                for key, value in gate.items()
                if key not in {"planDigest", "profileDigest"}
            }
        )
    plan["planDigest"] = content_digest(
        {key: value for key, value in plan.items() if key != "planDigest"}
    )


@pytest.mark.parametrize("fault", ("missing", "different"))
def test_manifest_refuses_gate_candidate_drift_even_with_recomputed_digests(fault):
    load_dagger_module()
    loader = importlib.import_module("agents_remember_quality.profile_plan")
    manifest = json.loads(execution_manifest(mode="full").value)
    gate = manifest["profilePlan"]["gates"][0]
    if fault == "missing":
        gate.pop("candidateIdentity")
    else:
        gate["candidateIdentity"] = {"kind": "git-tree", "value": "d" * 40}
    _redigest_profile_plan(manifest)
    with pytest.raises(ValueError, match="candidate"):
        loader.load_execution_manifest(json.dumps(manifest))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("environmentId", "../outside"),
        ("environmentId", "nested/name"),
        ("manifestPath", "../../outside.json"),
        ("reconstructionProofPath", "/outside.json"),
        ("workingDirectory", "../outside"),
    ),
)
def test_manifest_confines_environment_paths_before_runtime_use(field, value):
    load_dagger_module()
    loader = importlib.import_module("agents_remember_quality.profile_plan")
    manifest = json.loads(execution_manifest(mode="full").value)
    for gate in manifest["profilePlan"]["gates"]:
        for node in gate["semanticInputs"]:
            if node["inputKind"] != "environment-reconstruction":
                continue
            definition = json.loads(node["canonicalJson"])
            definition[field] = value
            node["canonicalJson"] = json.dumps(
                definition, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            node["contentDigest"] = content_digest(definition)
    _redigest_profile_plan(manifest)
    with pytest.raises(ValueError, match=field):
        loader.load_execution_manifest(json.dumps(manifest))


@pytest.mark.parametrize("fault", ("future-only", "future-before-retained"))
def test_resume_manifest_refuses_duplicate_paths_before_retained_gate_filter(tmp_path, fault):
    selected, request, _profile = selected_execution(tmp_path, 3)
    load_dagger_module()
    loader = importlib.import_module("agents_remember_quality.profile_plan")
    manifest = _manifest(selected, request)
    valid = loader.load_execution_manifest(json.dumps(manifest))
    assert valid.resume is not None
    assert valid.resume.first_gate == 3
    declarations = manifest["publishedArtifacts"]
    if fault == "future-only":
        duplicate = dict(
            next(item for item in declarations if all(gate >= 3 for gate in item["publisherGates"]))
        )
    else:
        duplicate = dict(next(item for item in declarations if item["path"] == "coverage.data"))
        duplicate["publisherGates"] = [4]
    # Keep the original canonical declaration last. Shared semantic closure
    # comparison must not mask an earlier duplicate excluded by the gate filter.
    declarations.insert(0, duplicate)
    with pytest.raises(ValueError, match="published artifact byte bounds are ambiguous"):
        loader.load_execution_manifest(json.dumps(manifest))
