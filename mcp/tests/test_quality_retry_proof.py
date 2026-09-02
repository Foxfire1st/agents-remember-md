from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from coverage import CoverageData

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from _evidence_catalog_fixture import write_synthetic_evidence_catalog
from _quality_admission import QUALITY_TEST_ADMISSION
from agents_remember_test_support.code_quality import check, retry_coverage, retry_proof
from agents_remember_test_support.code_quality.scope import GateScope


@pytest.fixture(autouse=True)
def _isolate_outer_quality_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Focused proofs never inherit the explicit retry rollback switch."""

    monkeypatch.delenv(retry_proof.DISABLE_ENV, raising=False)


def test_changed_test_contexts_and_collection_context_are_removed(tmp_path: Path) -> None:
    source = tmp_path / "source.coverage"
    destination = tmp_path / "destination.coverage"
    measured = str(tmp_path / "module.py")
    data = CoverageData(basename=str(source))
    for context, arc in (
        ("", (1, 2)),
        ("mcp/tests/test_changed.py::test_old|run", (2, 3)),
        ("mcp/tests/test_kept.py::test_kept|run", (3, 4)),
    ):
        data.set_context(context)
        data.add_arcs({measured: [arc]})
    data.write()

    assert retry_coverage.retain_unchanged_contexts(
        source,
        destination,
        [Path("mcp/tests/test_changed.py")],
    )

    filtered = CoverageData(basename=str(destination))
    filtered.read()
    assert filtered.measured_contexts() == {retry_coverage.CACHED_CONTEXT}
    assert filtered.arcs(measured) == [(3, 4)]


def test_full_proof_becomes_exact_then_test_delta_and_source_change_invalidates(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    test_path = root / "tests" / "test_sample.py"
    source_path = root / "src" / "sample.py"
    test_path.parent.mkdir(parents=True)
    source_path.parent.mkdir(parents=True)
    test_path.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    _write_retry_contract(root)
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Retry Proof Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    inputs = _inputs(root)
    output: list[str] = []

    fresh = retry_proof.prepare(
        inputs,
        admission=QUALITY_TEST_ADMISSION,
        printer=output.append,
    )
    assert fresh is not None and fresh.mode == "fresh"
    _write_context_coverage(fresh.active_data_path, root, test_path)
    coverage_json = root / "coverage.json"
    coverage_json.write_text(json.dumps({"meta": {"branch_coverage": True}}), encoding="utf-8")
    fresh.record_pytest(0)
    fresh.finish(coverage_json, quality_passed=False)

    exact = retry_proof.prepare(
        inputs,
        admission=QUALITY_TEST_ADMISSION,
        printer=output.append,
    )
    assert exact is not None and exact.exact
    exact.prepare_artifacts(coverage_json)
    exact.finish(coverage_json, quality_passed=False)

    stale_identity = retry_proof.prepare(
        replace(inputs, selection_digest="b" * 64),
        admission=QUALITY_TEST_ADMISSION,
        printer=output.append,
    )
    assert stale_identity is not None and stale_identity.mode == "fresh"
    assert "exact-selection-identity-changed" in output[-1]

    test_path.write_text(
        test_path.read_text(encoding="utf-8") + "\ndef test_more():\n    assert True\n",
        encoding="utf-8",
    )
    delta = retry_proof.prepare(
        inputs,
        admission=QUALITY_TEST_ADMISSION,
        printer=output.append,
    )
    assert delta is not None and delta.delta
    assert delta.delta_tests == (Path("tests/test_sample.py"),)

    source_path.write_text("VALUE = 2\n", encoding="utf-8")
    invalidated = retry_proof.prepare(
        inputs,
        admission=QUALITY_TEST_ADMISSION,
        printer=output.append,
    )
    assert invalidated is not None and invalidated.mode == "fresh"


def test_retry_reruns_declared_support_and_fixture_consumers_but_not_unaffected_tests(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    tests = root / "tests"
    source = root / "src/pkg"
    tests.mkdir(parents=True)
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("", encoding="utf-8")
    (source / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "_support.py").write_text("SUPPORT = 1\n", encoding="utf-8")
    (tests / "test_sample.py").write_text(
        "from _support import SUPPORT\nfrom pkg.sample import VALUE\n"
        "def test_sample(): assert VALUE == SUPPORT\n",
        encoding="utf-8",
    )
    (tests / "test_other.py").write_text(
        "def test_other(): assert True\n",
        encoding="utf-8",
    )
    _write_retry_contract(root, fixture_consumer="tests/test_sample.py")
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Retry Proof Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    inputs = _inputs(root)
    coverage_json = root / "coverage.json"
    output: list[str] = []

    fresh = retry_proof.prepare(inputs, admission=QUALITY_TEST_ADMISSION, printer=output.append)
    assert fresh is not None
    _write_contexts(
        fresh.active_data_path,
        root,
        (tests / "test_sample.py", tests / "test_other.py"),
    )
    coverage_json.write_text(json.dumps({"meta": {"branch_coverage": True}}), encoding="utf-8")
    fresh.record_pytest(0)
    fresh.finish(coverage_json, quality_passed=False)

    (tests / "_support.py").write_text("SUPPORT = 2\n", encoding="utf-8")
    support_delta = retry_proof.prepare(
        inputs,
        admission=QUALITY_TEST_ADMISSION,
        printer=output.append,
    )
    assert support_delta is not None and support_delta.delta
    assert support_delta.delta_tests == (Path("tests/test_sample.py"),)
    assert "import-consumer" in output[-1]

    (tests / "_support.py").write_text("SUPPORT = 1\n", encoding="utf-8")
    fixture = root / "mcp/tests/fixtures/owned.json"
    fixture.write_text('{"changed":true}\n', encoding="utf-8")
    fixture_delta = retry_proof.prepare(
        inputs,
        admission=QUALITY_TEST_ADMISSION,
        printer=output.append,
    )
    assert fixture_delta is not None and fixture_delta.delta
    assert fixture_delta.delta_tests == (Path("tests/test_sample.py"),)
    assert "declared-consumer" in output[-1]

    fixture.write_text("{}\n", encoding="utf-8")
    (source / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
    source_delta = retry_proof.prepare(
        inputs,
        admission=QUALITY_TEST_ADMISSION,
        printer=output.append,
    )
    assert source_delta is not None and source_delta.delta
    assert source_delta.delta_tests == (Path("tests/test_sample.py"),)
    assert "import-consumer" in output[-1]

    (source / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tests / "conftest.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "tests/conftest.py")
    global_change = retry_proof.prepare(
        inputs,
        admission=QUALITY_TEST_ADMISSION,
        printer=output.append,
    )
    assert global_change is not None and global_change.mode == "fresh"
    assert "global-test-input" in output[-1]


def test_wrapper_retry_runs_only_changed_test_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    test_path = root / "tests" / "test_sample.py"
    source_path = root / "src" / "sample.py"
    test_path.parent.mkdir(parents=True)
    source_path.parent.mkdir(parents=True)
    test_path.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    _write_retry_contract(root)
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Retry Proof Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    monkeypatch.setenv(retry_proof.CACHE_ROOT_ENV, str(root.parent / "retry-cache"))
    coverage_json = root / "coverage.json"
    commands: list[list[str]] = []
    output: list[str] = []

    def runner(
        name: str,
        command: list[str],
        cwd: Path,
        env: Mapping[str, str],
    ) -> check.StepResult:
        commands.append(command)
        if name == "pytest":
            data_path = Path(env["COVERAGE_FILE"])
            data = CoverageData(basename=str(data_path))
            if data_path.is_file():
                data.read()
            data.set_context("tests/test_sample.py::test_sample|run")
            data.add_arcs({str(source_path): [(-1, 1), (1, -1)]})
            data.write()
            coverage_json.write_text(
                json.dumps(
                    {
                        "meta": {"branch_coverage": True},
                        "files": {"src/sample.py": {"summary": {}}},
                    }
                ),
                encoding="utf-8",
            )
        return check.StepResult(name=name, return_code=0, command=command)

    config = check.CheckConfig(
        project_root=root,
        admission=QUALITY_TEST_ADMISSION,
        scope=GateScope(
            lint_paths=[Path("src/sample.py"), Path("tests/test_sample.py")],
            type_paths=[Path("src/sample.py"), Path("tests/test_sample.py")],
            coverage_paths=[Path("src")],
            test_paths=[Path("tests")],
            size_paths=[Path("src/sample.py"), Path("tests/test_sample.py")],
            scope_roots=[Path("src"), Path("tests")],
            untracked_paths=[],
        ),
        coverage_json=coverage_json,
        threshold=30.0,
        top=20,
        diff_base="HEAD",
        selection_digest="a" * 64,
    )
    with (
        mock.patch.object(check, "run_subprocess", runner),
        mock.patch.object(check, "run_coverage_rails", return_value=1),
    ):
        first_result = check.run_quality_check(config, runner=runner, printer=output.append)
    assert first_result == 1, "\n".join(output)

    test_path.write_text(
        test_path.read_text(encoding="utf-8") + "\ndef test_more():\n    assert True\n",
        encoding="utf-8",
    )
    commands.clear()
    output.clear()
    with (
        mock.patch.object(check, "run_subprocess", runner),
        mock.patch.object(check, "run_coverage_rails", side_effect=[1, 0]),
    ):
        retry_result = check.run_quality_check(config, runner=runner, printer=output.append)
    assert retry_result == 0, "\n".join(output)

    pytest_commands = [command for command in commands if "pytest" in command]
    assert len(pytest_commands) == 2
    delta_command, fresh_rerun_command = pytest_commands
    assert "tests" in delta_command
    assert "agents_remember_test_support.testing.retry_selection" in delta_command
    assert "--ar-retry-execute-path=tests/test_sample.py" in delta_command
    assert "--cov-append" not in delta_command
    assert "--cov-context=test" in delta_command
    assert "tests" in fresh_rerun_command
    assert "--cov-append" not in fresh_rerun_command
    assert "--cov-context=test" in fresh_rerun_command


def test_exact_proof_is_not_scored_when_a_cheap_rail_breaks(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    test_path = root / "tests" / "test_sample.py"
    source_path = root / "src" / "sample.py"
    test_path.parent.mkdir(parents=True)
    source_path.parent.mkdir(parents=True)
    test_path.write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    source_path.write_text("VALUE = 1\n", encoding="utf-8")
    _write_retry_contract(root)
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Retry Proof Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "base")
    coverage_json = root / "coverage.json"
    inputs = _inputs(root)
    proof = retry_proof.prepare(
        inputs,
        admission=QUALITY_TEST_ADMISSION,
        printer=lambda line: None,
    )
    assert proof is not None
    _write_context_coverage(proof.active_data_path, root, test_path)
    coverage_json.write_text(json.dumps({"meta": {"branch_coverage": True}}), encoding="utf-8")
    proof.record_pytest(0)
    proof.finish(coverage_json, quality_passed=False)
    config = check.CheckConfig(
        project_root=root,
        admission=QUALITY_TEST_ADMISSION,
        scope=GateScope(
            lint_paths=[Path("src/sample.py"), Path("tests/test_sample.py")],
            type_paths=[Path("src/sample.py"), Path("tests/test_sample.py")],
            coverage_paths=[Path("src")],
            test_paths=[Path("tests")],
            size_paths=[Path("src/sample.py"), Path("tests/test_sample.py")],
            scope_roots=[Path("src"), Path("tests")],
            untracked_paths=[],
        ),
        coverage_json=coverage_json,
        threshold=30.0,
        top=20,
        diff_base="HEAD",
        selection_digest="a" * 64,
    )
    coverage_rails = mock.Mock(return_value=0)

    def failing_runner(
        name: str,
        command: list[str],
        cwd: Path,
        env: Mapping[str, str],
    ) -> check.StepResult:
        return check.StepResult(name=name, return_code=1 if name == "ruff" else 0, command=command)

    with (
        mock.patch.object(check, "run_subprocess", failing_runner),
        mock.patch.object(check, "run_coverage_rails", coverage_rails),
    ):
        assert (
            check.run_quality_check(config, runner=failing_runner, printer=lambda line: None) == 1
        )

    coverage_rails.assert_not_called()
    assert not coverage_json.exists()


def test_repository_snapshot_hashes_symlink_identity_without_following_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    target = tmp_path / "installed-dependencies"
    target.mkdir()
    root.mkdir()
    link = root / "node_modules"
    link.symlink_to(target, target_is_directory=True)
    _git(root, "init")
    _git(root, "add", "node_modules")
    first = retry_proof._repository_snapshot(root)  # pyright: ignore[reportPrivateUsage]

    assert first == {
        "node_modules": retry_proof.hashlib.sha256(  # pyright: ignore[reportPrivateUsage]
            b"symlink\0" + os.fsencode(os.readlink(link))
        ).hexdigest()
    }
    (target / "untracked-install-byte").write_text("ignored", encoding="utf-8")
    assert (
        retry_proof._repository_snapshot(  # pyright: ignore[reportPrivateUsage]
            root
        )
        == first
    )


def test_snapshot_and_inventory_fail_closed_with_actionable_errors(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    failed = subprocess.CompletedProcess(
        args=["git", "ls-files"], returncode=1, stdout="", stderr="inventory failed"
    )
    with (
        mock.patch.object(retry_proof.git_command, "run_git", return_value=failed),
        pytest.raises(RuntimeError, match="inventory failed"),
    ):
        retry_proof._tracked_paths(root)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError, match="must be absolute"):
        retry_proof._cache_dir(Path("relative-cache"))  # pyright: ignore[reportPrivateUsage]

    with mock.patch.object(
        retry_proof,
        "_tracked_paths",
        return_value=(Path("missing.py"),),
    ):
        assert retry_proof._repository_snapshot(  # pyright: ignore[reportPrivateUsage]
            root
        ) == {"missing.py": retry_proof.MISSING_PATH_DIGEST}

    (root / "unreadable.py").write_text("VALUE = 1\n", encoding="utf-8")
    with (
        mock.patch.object(
            retry_proof,
            "_tracked_paths",
            return_value=(Path("unreadable.py"),),
        ),
        mock.patch.object(Path, "read_bytes", side_effect=OSError("denied")),
        pytest.raises(RuntimeError, match=r"could not fingerprint tracked input unreadable\.py"),
    ):
        retry_proof._repository_snapshot(root)  # pyright: ignore[reportPrivateUsage]


def test_retry_shape_helpers_cover_direct_new_and_malformed_selections(tmp_path: Path) -> None:
    direct = Path("tests/test_direct.py")
    inputs = retry_proof.RetryInputs(
        project_root=tmp_path,
        targeted=True,
        base_revision="base",
        threshold=20.0,
        top=10,
        diff_floor=100.0,
        coverage_paths=(Path("src"),),
        test_arguments=(direct,),
        untracked_paths=(),
        cache_root=tmp_path / "retry-cache",
        lane_digest="lane-digest",
        lane_trigger="affected",
        lane_population=("accept=unit-regression",),
        selection_digest="a" * 64,
    )
    with mock.patch.object(retry_proof, "_tracked_paths", return_value=()):
        assert retry_proof._selected_test_modules(  # pyright: ignore[reportPrivateUsage]
            inputs
        ) == (direct,)

    assert retry_proof._snapshot_delta(  # pyright: ignore[reportPrivateUsage]
        [], {"tests/test_direct.py": "new"}
    ) == (direct,)
    assert not retry_proof._selection_is_compatible(  # pyright: ignore[reportPrivateUsage]
        {"selectedTests": "not-a-list"}, [direct], [direct]
    )


def test_retry_environment_identity_ignores_only_explicit_runtime_transport() -> None:
    first = {
        "AR_QUALITY_INVOCATION": "ci",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:33473",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:33473/v1/traces",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://127.0.0.1:33473/v1/logs",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://127.0.0.1:33473/v1/metrics",
        "TRACEPARENT": "00-first",
        "BAGGAGE": "global-logs-span=first",
    }
    second = {
        **first,
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:45939",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:45939/v1/traces",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://127.0.0.1:45939/v1/logs",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://127.0.0.1:45939/v1/metrics",
        "TRACEPARENT": "00-second",
        "BAGGAGE": "global-logs-span=second",
    }

    digest = retry_proof._environment_digest  # pyright: ignore[reportPrivateUsage]
    assert digest(first) == digest(second)
    second["AR_QUALITY_RETRY_CONTEXT_VARIANT"] = "changed-test-semantics"
    assert digest(first) != digest(second)


def test_retry_tool_identity_records_missing_distribution_explicitly() -> None:
    def version(name: str) -> str:
        if name == "pytest-xdist":
            raise importlib.metadata.PackageNotFoundError(name)
        return f"{name}-version"

    with mock.patch.object(retry_proof.importlib.metadata, "version", side_effect=version):
        assert retry_proof._tool_versions() == {  # pyright: ignore[reportPrivateUsage]
            "coverage": "coverage-version",
            "pytest": "pytest-version",
            "pytest-cov": "pytest-cov-version",
            "pytest-xdist": "absent",
        }


def test_prepare_disable_and_fail_closed_routes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    inputs = _inputs(root)
    output: list[str] = []

    with mock.patch.dict(os.environ, {retry_proof.DISABLE_ENV: "1"}):
        assert (
            retry_proof.prepare(
                inputs,
                admission=QUALITY_TEST_ADMISSION,
                printer=output.append,
            )
            is None
        )
    assert retry_proof.DISABLE_ENV in output[-1]

    with mock.patch.dict(os.environ, {"AR_QUALITY_INVOCATION": "ci"}):
        assert retry_proof._disabled_reason(inputs) is None  # pyright: ignore[reportPrivateUsage]

    assert "untracked" in str(
        retry_proof._disabled_reason(  # pyright: ignore[reportPrivateUsage]
            replace(inputs, untracked_paths=(Path("new.py"),))
        )
    )
    assert "no Coverage.py" in str(
        retry_proof._disabled_reason(  # pyright: ignore[reportPrivateUsage]
            replace(inputs, coverage_paths=())
        )
    )

    with mock.patch.object(retry_proof, "_fresh_plan", side_effect=RuntimeError("broken")):
        assert (
            retry_proof.prepare(
                inputs,
                admission=QUALITY_TEST_ADMISSION,
                printer=output.append,
            )
            is None
        )
    assert "unavailable (broken)" in output[-1]

    with mock.patch.object(retry_proof, "_fresh_plan", return_value=None):
        assert (
            retry_proof.prepare(
                inputs,
                admission=QUALITY_TEST_ADMISSION,
                printer=output.append,
            )
            is None
        )
    assert "selection has no concrete test modules" in output[-1]

    with (
        mock.patch.object(retry_proof, "_cache_dir", return_value=tmp_path / "cache"),
        mock.patch.object(retry_proof, "_selected_test_modules", return_value=()),
    ):
        assert retry_proof._fresh_plan(inputs) is None  # pyright: ignore[reportPrivateUsage]


def test_reuse_plan_rejects_changed_selection_and_unusable_context(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    plan = _plan(tmp_path)
    old_test = Path("tests/test_old.py")
    new_test = Path("tests/test_new.py")
    plan.snapshot = {new_test.as_posix(): "new"}
    plan.selected_tests = (new_test,)
    output: list[str] = []
    result = retry_proof._reuse_plan(  # pyright: ignore[reportPrivateUsage]
        plan,
        {
            "snapshot": {old_test.as_posix(): "old"},
            "selectedTests": [old_test.as_posix()],
        },
        inputs,
        output.append,
    )
    assert result.mode == "fresh"
    assert "selected-population-changed" in output[-1]

    plan = _plan(tmp_path)
    selected = plan.selected_tests[0]
    unaffected = Path("tests/test_unaffected.py")
    plan.selected_tests = (selected, unaffected)
    plan.snapshot = {selected.as_posix(): "new", unaffected.as_posix(): "same"}
    impact = mock.Mock(
        complete=True,
        global_invalidation=False,
        tests=(selected,),
    )
    with mock.patch.object(retry_proof, "_retry_impact", return_value=impact):
        result = retry_proof._reuse_plan(  # pyright: ignore[reportPrivateUsage]
            plan,
            {
                "snapshot": {selected.as_posix(): "old", unaffected.as_posix(): "same"},
                "selectedTests": [selected.as_posix(), unaffected.as_posix()],
            },
            inputs,
            output.append,
        )
    assert result.mode == "fresh"
    assert "unusable-context-proof" in output[-1]


def test_cache_dir_uses_only_the_explicit_dagger_owner(tmp_path: Path) -> None:
    cache = retry_proof._cache_dir(tmp_path)  # pyright: ignore[reportPrivateUsage]
    assert cache == tmp_path / retry_proof.CACHE_DIRECTORY


def test_manifest_misses_report_absence_corruption_and_digest_failure_explicitly(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)

    manifest, reason = retry_proof._load_manifest(  # pyright: ignore[reportPrivateUsage]
        plan
    )
    assert manifest is None
    assert reason == "no-prior-proof"

    plan.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    plan.manifest_path.write_text("{not-json\n", encoding="utf-8")
    manifest, reason = retry_proof._load_manifest(  # pyright: ignore[reportPrivateUsage]
        plan
    )
    assert manifest is None
    assert reason == "manifest-invalid-json"

    plan.proof_data_path.write_bytes(b"tampered")
    plan.proof_json_path.write_text("{}", encoding="utf-8")
    plan.manifest_path.write_text(
        json.dumps(
            {
                "schema": retry_proof.SCHEMA_VERSION,
                "compatibilityKey": plan.compatibility_key,
                "laneDigest": plan.lane_digest,
                "laneTrigger": plan.lane_trigger,
                "lanePopulation": list(plan.lane_population),
                "selectionDigest": plan.selection_digest,
                "snapshot": plan.snapshot,
                "selectedTests": [path.as_posix() for path in plan.selected_tests],
                "coverageDataSha256": "wrong",
                "coverageJsonSha256": retry_proof._file_digest(  # pyright: ignore[reportPrivateUsage]
                    plan.proof_json_path
                ),
            }
        ),
        encoding="utf-8",
    )
    manifest, reason = retry_proof._load_manifest(  # pyright: ignore[reportPrivateUsage]
        plan
    )
    assert manifest is None
    assert reason == "manifest-invalid:coverage-data-digest-mismatch"


def test_context_proof_and_filtering_reject_non_branch_or_contextless_data(
    tmp_path: Path,
) -> None:
    measured = str(tmp_path / "module.py")
    line_only = tmp_path / "line-only.coverage"
    data = CoverageData(basename=str(line_only))
    data.add_lines({measured: [1]})
    data.write()
    with pytest.raises(RuntimeError, match="branch coverage data is missing"):
        retry_coverage.validate_context_proof(line_only)
    with pytest.raises(RuntimeError, match="does not contain branch arcs"):
        retry_coverage.retain_unchanged_contexts(
            line_only, tmp_path / "filtered.coverage", [Path("tests/test_one.py")]
        )

    contextless = tmp_path / "contextless.coverage"
    data = CoverageData(basename=str(contextless))
    data.set_context("")
    data.add_arcs({measured: [(1, 2)]})
    data.write()
    with pytest.raises(RuntimeError, match="pytest test contexts are missing"):
        retry_coverage.validate_context_proof(contextless)


def test_publish_proof_refuses_missing_or_invalid_artifacts(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    coverage_json = tmp_path / "coverage.json"
    coverage_json.write_text("{}", encoding="utf-8")
    retry_proof._publish_proof(  # pyright: ignore[reportPrivateUsage]
        plan, coverage_json
    )
    assert not plan.manifest_path.exists()

    data = CoverageData(basename=str(plan.active_data_path))
    data.add_lines({str(tmp_path / "module.py"): [1]})
    data.write()
    retry_proof._publish_proof(  # pyright: ignore[reportPrivateUsage]
        plan, coverage_json
    )
    assert not plan.manifest_path.exists()


def test_delta_fresh_rerun_without_coverage_reports_early_failure(tmp_path: Path) -> None:
    plan = _plan(tmp_path, mode="delta")
    plan.pytest_passed = True
    coverage_json = tmp_path / "coverage.json"
    output: list[str] = []
    config = mock.Mock(project_root=tmp_path)
    runtime = check.RailRuntime(
        runner=mock.Mock(),
        printer=output.append,
        retry_plan=plan,
    )
    with (
        mock.patch.object(check, "run_coverage_rails", return_value=1),
        mock.patch.object(check, "run_pytest_only", return_value=1),
    ):
        assert (
            check.complete_coverage_rails(
                config,
                coverage_json,
                fixed_failures=0,
                runtime=runtime,
            )
            == 1
        )
    assert any("result: CRAP-Calculator SKIPPED" in line for line in output)
    assert any("result: diff-coverage SKIPPED" in line for line in output)


def test_pytest_fresh_rerun_and_exact_paths_report_their_own_results(tmp_path: Path) -> None:
    coverage_json = tmp_path / "coverage.json"
    config = mock.Mock(project_root=tmp_path, scope=mock.Mock(), targeted=True)
    plan = _plan(tmp_path, mode="fresh")
    output: list[str] = []
    with mock.patch.object(check, "quality_steps", return_value=[]):
        assert (
            check.run_pytest_only(
                config,
                coverage_json,
                plan,
                runner=mock.Mock(),
                printer=output.append,
            )
            == 1
        )
    assert "result: pytest FAIL (fresh full rerun derived no pytest rail)" in output

    pytest_step = check.Step(name="pytest", command=["pytest"])
    exact = _plan(tmp_path, mode="exact")
    cached = mock.Mock(return_value=0)
    with (
        mock.patch.object(check, "quality_steps", return_value=[pytest_step]),
        mock.patch.object(check, "report_cached_pytest", cached),
        mock.patch.object(check, "subprocess_env", return_value={}),
        mock.patch.object(check.scope_reporting, "fixed_step_scope_line", return_value="scope"),
    ):
        assert (
            check.run_fixed_checks(
                config,
                coverage_json,
                runner=mock.Mock(),
                printer=lambda line: None,
                retry_plan=exact,
            )
            == 0
        )
    cached.assert_called_once_with(coverage_json, mock.ANY)

    coverage_json.write_text("{}", encoding="utf-8")
    with mock.patch.object(
        check.scope_reporting, "coverage_result_scope_line", return_value="scope"
    ):
        assert (
            check.report_pytest_result(
                pytest_step,
                check.StepResult(name="pytest", return_code=1, command=["pytest"]),
                coverage_json,
                output.append,
            )
            == 1
        )


def test_retry_artifact_and_scope_failures_fall_back_without_stale_evidence(
    tmp_path: Path,
) -> None:
    coverage_json = tmp_path / "coverage.json"
    coverage_json.write_text("{}", encoding="utf-8")
    plan = _plan(tmp_path)
    output: list[str] = []
    with (
        mock.patch.object(check, "prepare_retry_plan", return_value=plan),
        mock.patch.object(plan, "prepare_artifacts", side_effect=RuntimeError("broken proof")),
    ):
        assert (
            check.initialized_retry_plan(
                mock.Mock(),
                coverage_json,
                tmp_path,
                runner=mock.Mock(),
                printer=output.append,
            )
            is None
        )
    assert not coverage_json.exists()
    assert "artifact preparation failed (broken proof)" in output[-1]

    with mock.patch.object(
        check.scope_reporting, "coverage_result_scope_line", return_value="scope"
    ):
        assert check.report_cached_pytest(coverage_json, output.append) == 0
    assert output[-1] == "result: pytest PASS (exact content-addressed proof reused)"

    with mock.patch.object(
        check.scope_reporting,
        "coverage_result_scope_line",
        side_effect=check.ScopeError("bad scope"),
    ):
        assert check.report_cached_pytest(coverage_json, output.append) == 1
    assert output[-1] == "result: pytest FAIL (cached Coverage.py result scope unavailable)"

    config = mock.Mock(targeted_base=None, diff_base="base")
    with mock.patch.object(
        check.diff_coverage, "resolve_base", side_effect=RuntimeError("bad base")
    ):
        assert (
            check.prepare_retry_plan(
                config,
                tmp_path,
                runner=check.run_subprocess,
                printer=output.append,
            )
            is None
        )
    assert output[-1] == "retry-proof: unavailable (bad base); running fresh"


def _inputs(root: Path) -> retry_proof.RetryInputs:
    lane_rows = tuple(
        f"file:{path.relative_to(root).as_posix()}=unit-regression"
        for path in sorted((root / "tests").glob("test_*.py"))
    )
    return retry_proof.RetryInputs(
        project_root=root,
        targeted=False,
        base_revision="HEAD",
        threshold=30.0,
        top=20,
        diff_floor=100.0,
        coverage_paths=(Path("src"),),
        test_arguments=(Path("tests"),),
        untracked_paths=(),
        cache_root=root.parent / "retry-cache",
        lane_digest="lane-digest",
        lane_trigger="release",
        lane_population=("accept=release", *lane_rows),
        selection_digest="a" * 64,
    )


def _write_retry_contract(root: Path, *, fixture_consumer: str | None = None) -> None:
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        encoding="utf-8",
    )
    support = root / "mcp/tests/_retry_catalog_anchor.py"
    support.parent.mkdir(parents=True, exist_ok=True)
    support.write_text("VALUE = 1\n", encoding="utf-8")
    artifacts = {"mcp/tests/_retry_catalog_anchor.py": ("tests/test_sample.py",)}
    if fixture_consumer is not None:
        fixture = support.parent / "fixtures/owned.json"
        fixture.parent.mkdir()
        fixture.write_text("{}\n", encoding="utf-8")
        artifacts["mcp/tests/fixtures/owned.json"] = (fixture_consumer,)
    test_files = sorted(
        path.relative_to(root).as_posix() for path in (root / "tests").glob("test_*.py")
    )
    lanes = root / "mcp/tests/test-evidence-lanes.toml"
    lanes.write_text(
        "\n".join(
            [
                'schema_version = "ar-test-evidence-lanes/v1"',
                "",
                "[files]",
                "unit-regression = [",
                *(f'  "{path}",' for path in test_files),
                "]",
                "public-contract = []",
                "integration = []",
                "architecture-fitness = []",
                "provider-conformance = []",
                "stress-durability = []",
                "migration = []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    artifacts["mcp/tests/test-evidence-lanes.toml"] = ("tests/test_sample.py",)
    write_synthetic_evidence_catalog(root, artifacts)


def _plan(tmp_path: Path, *, mode: str = "fresh") -> retry_proof.RetryPlan:
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    return retry_proof.RetryPlan(
        mode=retry_proof.RetryMode(mode),
        cache_dir=cache,
        manifest_path=cache / "manifest.json",
        proof_data_path=cache / "proof.coverage",
        proof_json_path=cache / "proof.json",
        active_data_path=cache / "active.coverage",
        retained_data_path=cache / "retained.coverage",
        snapshot={},
        selected_tests=(Path("tests/test_one.py"),),
        compatibility_key="key",
        lane_digest="lane-digest",
        lane_trigger="release",
        lane_population=("accept=release",),
        selection_digest="a" * 64,
        delta_tests=(Path("tests/test_one.py"),) if mode == "delta" else (),
    )


def _write_context_coverage(path: Path, root: Path, test_path: Path) -> None:
    _write_contexts(path, root, (test_path,))


def _write_contexts(path: Path, root: Path, test_paths: tuple[Path, ...]) -> None:
    data = CoverageData(basename=str(path))
    for index, test_path in enumerate(test_paths, start=1):
        data.set_context(f"{test_path.relative_to(root).as_posix()}::test_case|run")
        data.add_arcs({str(root / "src" / "sample.py"): [(-index, index), (index, -index)]})
    data.write()


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
