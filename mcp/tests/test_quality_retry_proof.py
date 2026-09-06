from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from coverage import CoverageData

MCP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(MCP_SRC))

from _evidence_catalog_fixture import write_synthetic_evidence_catalog
from _quality_admission import QUALITY_TEST_ADMISSION
from agents_remember_test_support.code_quality import retry_proof


@pytest.fixture(autouse=True)
def _isolate_outer_quality_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Focused proofs never inherit the explicit retry rollback switch."""

    monkeypatch.delenv(retry_proof.DISABLE_ENV, raising=False)


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
