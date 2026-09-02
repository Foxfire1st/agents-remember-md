from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from agents_remember_test_support.testing.dagger_admission import (
    DAGGER_TEST_ATTESTATION_ENV,
    DaggerAdmissionError,
    dagger_admission_refusal,
)
from conftest import prepare_certifying_pytest_bootstrap

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DAGGER_MANIFEST = REPOSITORY_ROOT / "dagger.json"
DAGGER_SOURCE_ROOT = REPOSITORY_ROOT / ".dagger/src"
DAGGER_MODULE = DAGGER_SOURCE_ROOT / "agents_remember_quality/main.py"
DAGGER_MODULE_ID = "agents_remember_quality.main"
E2E_HARNESS_ROOT = REPOSITORY_ROOT / "scripts/e2e_harness"
VALID_DAGGER_NONCE = "0123456789abcdef0123456789abcdef"
E2E_HARNESS_PYTHON = (
    "scripts/e2e_harness/codex_driver.py",
    "scripts/e2e_harness/dispatch_sentinels.py",
    "scripts/e2e_harness/fixture.py",
    "scripts/e2e_harness/reporting.py",
    "scripts/e2e_harness/responses_server.py",
    "scripts/e2e_harness/responses_sse.py",
    "scripts/e2e_harness/run.py",
    "scripts/e2e_harness/scenario.py",
    "scripts/e2e_harness/scenario_control.py",
    "scripts/e2e_harness/scenario_evidence.py",
    "scripts/e2e_harness/scenario_runtime.py",
    "scripts/e2e_harness/selection.py",
)


def load_dagger_module() -> ModuleType:
    module_root = str(DAGGER_SOURCE_ROOT)
    if module_root not in sys.path:
        sys.path.insert(0, module_root)
    sys.modules.pop(DAGGER_MODULE_ID, None)
    sys.modules.pop("agents_remember_quality", None)
    return importlib.import_module(DAGGER_MODULE_ID)


def load_e2e_module(filename: str, module_id: str) -> ModuleType:
    harness_root = str(E2E_HARNESS_ROOT)
    if harness_root not in sys.path:
        sys.path.insert(0, harness_root)
    spec = importlib.util.spec_from_file_location(module_id, E2E_HARNESS_ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_id] = module
    spec.loader.exec_module(module)
    return module


class FakeContainer:
    def __init__(self, exit_codes: list[int]) -> None:
        self.exit_codes = exit_codes
        self.commands: list[list[str]] = []
        self.files: dict[str, str] = {}
        self.environment: list[tuple[object, ...]] = []
        self.operations: list[tuple[object, ...]] = []

    def from_(self, image: str) -> FakeContainer:
        self.operations.append(("from", image))
        return self

    def with_mounted_cache(self, *args: object, **kwargs: object) -> FakeContainer:
        self.operations.append(("cache", *args, kwargs))
        return self

    def with_env_variable(self, *args: object) -> FakeContainer:
        self.environment.append(args)
        self.operations.append(("env", *args))
        return self

    def with_exec(self, command: list[str], **_kwargs: object) -> FakeContainer:
        self.commands.append(command)
        self.operations.append(("exec", *command))
        return self

    def with_directory(self, *args: object) -> FakeContainer:
        self.operations.append(("directory", *args))
        return self

    def with_file(self, *args: object) -> FakeContainer:
        self.operations.append(("file", *args))
        return self

    def with_workdir(self, *args: object) -> FakeContainer:
        self.operations.append(("workdir", *args))
        return self

    def with_new_file(self, path: str, *, contents: str) -> FakeContainer:
        self.files[path] = contents
        return self

    def directory(self, path: str) -> str:
        return path

    async def sync(self) -> FakeContainer:
        return self

    async def exit_code(self) -> int:
        return self.exit_codes.pop(0)


class FakeDag:
    def __init__(self, exit_codes: list[int]) -> None:
        self.container_value = FakeContainer(exit_codes)

    def container(self) -> FakeContainer:
        return self.container_value

    def cache_volume(self, name: str) -> str:
        return name


class FakeSource:
    def file(self, path: str) -> str:
        return path


def test_agents_remember_quality_module_is_pinned_and_parseable() -> None:
    manifest = json.loads(DAGGER_MANIFEST.read_text(encoding="utf-8"))
    tree = ast.parse(DAGGER_MODULE.read_text(encoding="utf-8"), filename=DAGGER_MODULE.as_posix())

    assert manifest["engineVersion"] == "v0.21.8"
    quality_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AgentsRememberQuality"
    )
    public_functions = {
        node.name for node in quality_class.body if isinstance(node, ast.AsyncFunctionDef)
    }
    assert public_functions == {
        "quality",
        "cadence_evidence",
        "causal_evidence",
        "retry_evidence",
        "retry_matrix_evidence",
        "route_measurement_evidence",
    }
    assert DAGGER_MODULE_ID.endswith(".main")
    assert DAGGER_MODULE.parent.parent == DAGGER_SOURCE_ROOT
    assert manifest["disableDefaultFunctionCaching"] is True


def test_ambient_role_chat_harness_is_wired_and_parseable() -> None:
    dagger_source = DAGGER_MODULE.read_text(encoding="utf-8")
    assert "scripts/e2e_harness/run.py" in dagger_source

    for relative in E2E_HARNESS_PYTHON:
        path = REPOSITORY_ROOT / relative
        compile(path.read_text(encoding="utf-8"), path.as_posix(), "exec")


def test_ambient_role_chat_runner_refuses_before_declaring_a_host_test_process() -> None:
    runner = load_e2e_module("run.py", "test_ambient_role_chat_e2e_run")

    with (
        patch.object(
            runner,
            "require_dagger_admission",
            side_effect=DaggerAdmissionError("Dagger admission missing"),
        ),
        patch.object(runner, "declare_test_process") as declare,
        pytest.raises(DaggerAdmissionError, match="admission missing"),
    ):
        runner._admit_execution()

    declare.assert_not_called()


def test_ambient_role_chat_candidate_identity_excludes_observer_scratch(
    tmp_path: Path,
) -> None:
    runner = load_e2e_module("run.py", "test_ambient_role_chat_candidate_identity")
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", repository.as_posix(), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init")
    git("config", "user.email", "candidate@example.invalid")
    git("config", "user.name", "Candidate Identity")
    candidate_file = repository / "candidate.txt"
    candidate_file.write_text("base\n", encoding="utf-8")
    git("add", "candidate.txt")
    git("commit", "-m", "base")
    candidate_file.write_text("staged candidate\n", encoding="utf-8")
    git("add", "candidate.txt")

    expected_tree = git("write-tree")
    identity = runner._candidate_identity(repository)
    tree_paths = git("ls-tree", "-r", "--name-only", identity["tree"]).splitlines()

    assert identity == {"commit": git("rev-parse", "HEAD"), "tree": expected_tree}
    assert tree_paths == ["candidate.txt"]
    assert all("candidate-index" not in path for path in tree_paths)
    assert list(repository.glob(".arspawn-e2e-candidate-index*")) == []


def test_ambient_role_chat_tmux_commands_use_only_the_fixture_socket_root(
    tmp_path: Path,
) -> None:
    runtime = load_e2e_module("scenario_runtime.py", "test_ambient_role_chat_scenario_runtime")
    fixture = SimpleNamespace(root=tmp_path, codex_home=tmp_path / "codex-home")

    with (
        patch.dict(os.environ, {"TMUX": "inherited-parent-server"}, clear=False),
        patch.object(
            runtime.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0),
        ) as run,
    ):
        runtime.prepare_tmux_server(fixture)
        expected = (tmp_path / "tmux-runtime").as_posix()
        assert os.environ["TMUX_TMPDIR"] == expected
        assert "TMUX" not in os.environ
        assert len(run.call_args_list) == 4
        for invocation in run.call_args_list:
            command_env = invocation.kwargs["env"]
            assert command_env["TMUX_TMPDIR"] == expected
            assert "TMUX" not in command_env
        assert run.call_args_list[2].args[0] == [
            "tmux",
            "set-option",
            "-s",
            "exit-empty",
            "off",
        ]


def test_ambient_role_chat_candidate_mcp_inherits_fixture_tmux_namespace(
    tmp_path: Path,
) -> None:
    fixture = load_e2e_module("fixture.py", "test_ambient_role_chat_fixture")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    fixture._write_codex_config(
        codex_home,
        authority_path=tmp_path / "mcp-authority.json",
        responses_base_url="http://127.0.0.1:1/v1",
    )

    config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    candidate = config["mcp_servers"]["agents_remember_candidate"]
    assert candidate["env_vars"] == [
        "AR_HOSTED_SESSION_ID",
        "AR_SPAWN_ROLE",
        "TMUX_TMPDIR",
    ]


@pytest.mark.parametrize(("ok", "expected"), [(True, True), (False, False)])
def test_ambient_role_chat_discovery_decodes_current_codex_tool_result_envelope(
    ok: bool,
    expected: bool,
) -> None:
    evidence = load_e2e_module(
        "scenario_evidence.py",
        "test_ambient_role_chat_scenario_evidence",
    )
    result = f'Wall time: 4.1996 seconds\nOutput:\n{{"ok":{str(ok).lower()}}}'

    assert evidence._tool_result_ok(result) is expected
    assert evidence._tool_result_ok('prefix\nOutput:\n{"ok":true}') is False


def test_candidate_setup_precedes_every_attempt_specific_cache_input() -> None:
    module = load_dagger_module()
    fake_dag = FakeDag([])

    with patch.object(module, "dag", fake_dag):
        module._candidate_container(
            FakeSource(),
            object(),
            attempt_nonce=VALID_DAGGER_NONCE,
            reports="/reports/scenario",
        )

    assert tuple(inspect.signature(module._candidate_base).parameters) == (
        "source",
        "repository_bundle",
    )
    operations = fake_dag.container_value.operations
    runtime_build_index = next(
        index
        for index, operation in enumerate(operations)
        if operation[0] == "exec"
        and "install-python-runtime.sh" in " ".join(str(part) for part in operation[1:])
    )
    source_index = next(
        index
        for index, operation in enumerate(operations)
        if operation[:2] == ("directory", "/workspace")
    )
    install_index = next(
        index
        for index, operation in enumerate(operations)
        if operation[0] == "exec" and "uv sync" in " ".join(str(part) for part in operation[1:])
    )
    late_names = {
        "PYTHONPATH",
        "AR_QUALITY_INVOCATION",
        "AR_QUALITY_RETRY_CACHE",
        "AR_DAGGER_TEST_ATTESTATION",
        "AR_QUALITY_ATTEMPT_NONCE",
        "AR_CODEX_PROBE_MODE",
        "AR_CODEX_PROBE_REPORT",
        "AR_QUALITY_PROGRESS_REPORT",
        "COVERAGE_FILE",
    }
    late_indices = [
        index
        for index, operation in enumerate(operations)
        if operation[0] == "env" and operation[1] in late_names
    ]
    retry_cache_index = next(
        index
        for index, operation in enumerate(operations)
        if operation[0] == "cache" and operation[1] == module.RETRY_CACHE_ROOT
    )

    assert len(late_indices) == len(late_names)
    assert runtime_build_index < source_index < install_index
    assert all(index > install_index for index in late_indices)
    assert retry_cache_index > install_index
    assert all(VALID_DAGGER_NONCE not in operation for operation in operations[:install_index])
    assert all("/reports/scenario" not in operation for operation in operations[:install_index])
    assert ("exec", "apt-get", "update") in operations[:install_index]
    assert (
        "exec",
        "npm",
        "install",
        "--global",
        f"@openai/codex@{module.CODEX_VERSION}",
    ) in operations[:install_index]
    assert (
        "exec",
        module.RUNTIME_PYTHON,
        "-m",
        "venv",
        module.VENV_ROOT,
    ) in operations[:install_index]


def test_python_suite_refuses_missing_or_mismatched_dagger_attestation(tmp_path: Path) -> None:
    attestation = tmp_path / "dagger-test-attestation"
    assert "absent or invalid" in (dagger_admission_refusal({}, attestation) or "")
    assert "unavailable" in (
        dagger_admission_refusal(
            {DAGGER_TEST_ATTESTATION_ENV: VALID_DAGGER_NONCE},
            attestation,
        )
        or ""
    )
    attestation.write_text("f" * 32, encoding="utf-8")
    assert "do not match" in (
        dagger_admission_refusal(
            {DAGGER_TEST_ATTESTATION_ENV: VALID_DAGGER_NONCE},
            attestation,
        )
        or ""
    )
    with (
        patch(
            "conftest._prepare_certifying_pytest_bootstrap",
            side_effect=DaggerAdmissionError(
                "Agents Remember tests are Dagger-only; refusing host execution"
            ),
        ),
        pytest.raises(pytest.UsageError, match="refusing host execution"),
    ):
        prepare_certifying_pytest_bootstrap()


def test_python_suite_accepts_matching_dagger_attestation(tmp_path: Path) -> None:
    attestation = tmp_path / "dagger-test-attestation"
    attestation.write_text(VALID_DAGGER_NONCE, encoding="utf-8")
    assert (
        dagger_admission_refusal(
            {DAGGER_TEST_ATTESTATION_ENV: VALID_DAGGER_NONCE},
            attestation,
        )
        is None
    )


def test_agents_remember_quality_exports_failures_as_the_only_authoritative_result() -> None:
    source = DAGGER_MODULE.read_text(encoding="utf-8")

    assert "expect=ReturnType.ANY" in source
    assert "clean-quality-results.json" in source
    assert "async def verify" not in source


@pytest.mark.parametrize(
    ("pytest_results", "must_run", "expected"),
    [
        (("result: pytest PASS",), True, True),
        (("result: pytest SKIPPED (an earlier quality rail failed)",), False, True),
        ((), False, False),
        (("result: pytest FAIL",), False, False),
    ],
)
def test_retry_matrix_distinguishes_pytest_execution_from_explicit_skip(
    pytest_results: tuple[str, ...],
    must_run: bool,
    expected: bool,
) -> None:
    module_root = str(DAGGER_SOURCE_ROOT)
    if module_root not in sys.path:
        sys.path.insert(0, module_root)
    route = importlib.import_module("agents_remember_quality.retry_evidence_route")

    assert route._pytest_observation_matches(pytest_results, must_run=must_run) is expected


@pytest.mark.parametrize(
    ("mode", "diff_base", "memory_cap", "message"),
    [
        ("quick", "base", 0, "unknown quality mode"),
        ("full", "", 0, "diff_base must name"),
        ("full", "base", -1, "memory_cap_bytes cannot be negative"),
    ],
)
def test_dagger_quality_refuses_invalid_public_inputs(
    mode: str, diff_base: str, memory_cap: int, message: str
) -> None:
    module = load_dagger_module()

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            module.AgentsRememberQuality().quality(
                object(),
                object(),
                diff_base=diff_base,
                mode=mode,
                memory_cap_bytes=memory_cap,
            )
        )


def test_dagger_route_measurement_refuses_single_observation_distributions() -> None:
    module = load_dagger_module()

    with pytest.raises(ValueError, match="at least 2"):
        asyncio.run(
            module.AgentsRememberQuality().route_measurement_evidence(
                object(),
                object(),
                repetitions=1,
            )
        )


def test_dagger_quality_builds_the_real_probe_and_targeted_wrapper_graph() -> None:
    module = load_dagger_module()
    fake_dag = FakeDag([0, 0, 0, 0])

    with (
        patch.object(module, "dag", fake_dag),
        patch.object(module.secrets, "token_hex", return_value=VALID_DAGGER_NONCE),
    ):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                mode="targeted",
                diff_base="a" * 40,
                memory_cap_bytes=1024,
            )
        )

    commands = fake_dag.container_value.commands
    assert result.exit_code == 0
    assert result.reports == "/reports"
    assert any(command[-1] == "mcp/tests/test_codex_clean_room_probe.py" for command in commands)
    assert ["git", "fetch", "--no-tags", "/tmp/ar-candidate.bundle", "HEAD"] in commands
    assert ["git", "add", "--all"] in commands
    wrapper = next(
        command
        for command in commands
        if "agents_remember_test_support.code_quality.check" in command
    )
    assert "--targeted" in wrapper
    assert wrapper[wrapper.index("--pytest-phase-report") + 1] == "/reports/pytest-phases.json"
    assert wrapper[wrapper.index("--causal-failure-report") + 1] == (
        "/reports/causal-failures.json"
    )
    assert wrapper[-4:] == ["--diff-base", "a" * 40, "--memory-cap-bytes", "1024"]
    payload = json.loads(fake_dag.container_value.files["/reports/clean-quality-results.json"])
    assert payload["status"] == "passed"
    assert payload["startedAt"] <= payload["finishedAt"]
    assert payload["attemptNonce"] == VALID_DAGGER_NONCE
    assert payload["causalFailureReport"] == "causal-failures.json"
    assert (
        "AR_DAGGER_TEST_ATTESTATION",
        VALID_DAGGER_NONCE,
    ) in fake_dag.container_value.environment
    assert (
        "AR_QUALITY_ATTEMPT_NONCE",
        VALID_DAGGER_NONCE,
    ) in fake_dag.container_value.environment
    assert any(
        command[:2] == ["sh", "-c"] and "/tmp/ar-quality/dagger-test-attestation" in command[-1]
        for command in commands
    )
    assert payload["completedSteps"] == [
        "environment",
        "codex-read-only-probe",
        "ambient-role-chat-e2e",
        "quality-wrapper",
    ]
    assert payload["attemptedSteps"] == payload["completedSteps"]
    assert payload["skippedSteps"] == []
    assert payload["failedStep"] is None
    assert payload["promptSubmitted"] is True
    assert payload["codexProtocol"] == module.AMBIENT_CODEX_PROTOCOL
    assert payload["ambientRoleChatEvidence"] == {
        "status": "passed",
        "summary": "ambient-role-chat-e2e/summary.json",
        "runs": [
            "ambient-role-chat-e2e/run-1.json",
            "ambient-role-chat-e2e/run-2.json",
        ],
    }


def test_dagger_quality_treats_unselected_ambient_e2e_as_an_explicit_skip() -> None:
    module = load_dagger_module()
    fake_dag = FakeDag([0, 0, module.E2E_NOT_SELECTED_EXIT_CODE, 0])

    with patch.object(module, "dag", fake_dag):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(), object(), mode="targeted", diff_base="base-commit"
            )
        )

    payload = json.loads(fake_dag.container_value.files["/reports/clean-quality-results.json"])
    assert result.exit_code == 0
    assert payload["status"] == "passed"
    assert payload["attemptedSteps"] == [
        "environment",
        "codex-read-only-probe",
        "ambient-role-chat-e2e",
        "quality-wrapper",
    ]
    assert payload["completedSteps"] == [
        "environment",
        "codex-read-only-probe",
        "quality-wrapper",
    ]
    assert payload["skippedSteps"] == ["ambient-role-chat-e2e"]
    assert payload["failedStep"] is None
    assert payload["stepExitCodes"]["ambient-role-chat-e2e"] == module.E2E_NOT_SELECTED_EXIT_CODE
    assert payload["promptSubmitted"] is False
    assert payload["codexProtocol"] == module.BASELINE_CODEX_PROTOCOL
    assert payload["ambientRoleChatEvidence"] == {
        "status": "skipped",
        "summary": "ambient-role-chat-e2e/summary.json",
    }


def test_dagger_cadence_evidence_is_a_separate_non_accepting_graph() -> None:
    module = load_dagger_module()
    fake_dag = FakeDag([0, 0])

    with (
        patch.object(module, "dag", fake_dag),
        patch.object(module.secrets, "token_hex", return_value=VALID_DAGGER_NONCE),
    ):
        result = asyncio.run(
            module.AgentsRememberQuality().cadence_evidence(
                FakeSource(),
                object(),
                trigger="scheduled",
            )
        )

    command = next(
        item
        for item in fake_dag.container_value.commands
        if "agents_remember_test_support.testing.cadence_runner" in item
    )
    assert command[command.index("--trigger") + 1] == "scheduled"
    assert not any(
        "agents_remember_test_support.code_quality.check" in item
        for item in fake_dag.container_value.commands
    )
    payload = json.loads(fake_dag.container_value.files["/reports/cadence-route-results.json"])
    assert result.exit_code == 0
    assert payload["acceptanceEligible"] is False
    assert payload["certifying"] is False
    assert payload["completedSteps"] == ["environment", "cadence-evidence"]


def test_dagger_cadence_evidence_refuses_unknown_trigger() -> None:
    module = load_dagger_module()

    with pytest.raises(ValueError, match="unknown cadence evidence trigger"):
        asyncio.run(
            module.AgentsRememberQuality().cadence_evidence(
                object(),
                object(),
                trigger="full",
            )
        )


def test_dagger_quality_full_uses_explicit_diff_base_without_targeted_flags() -> None:
    module = load_dagger_module()
    fake_dag = FakeDag([0] * 11)

    with patch.object(module, "dag", fake_dag):
        asyncio.run(
            module.AgentsRememberQuality().quality(FakeSource(), object(), diff_base="base-commit")
        )

    wrapper = next(
        command
        for command in fake_dag.container_value.commands
        if "agents_remember_test_support.code_quality.check" in command
    )
    assert "--targeted" not in wrapper
    assert wrapper[-2:] == ["--diff-base", "base-commit"]
    assert "--memory-cap-bytes" not in wrapper
    commands = fake_dag.container_value.commands
    assert ["npm", "ci"] in commands
    assert ["npm", "run", "lint"] in commands
    assert ["npm", "run", "typecheck"] in commands
    assert ["npm", "run", "test:coverage"] in commands
    assert ["npm", "run", "coverage:diff"] in commands
    assert ["npm", "run", "e2e", "--", "--fail-on-flaky-tests"] in commands
    assert ["npm", "run", "build"] in commands
    assert ("CI", "1") in fake_dag.container_value.environment


def test_dagger_quality_stops_at_the_first_failed_dashboard_rail() -> None:
    module = load_dagger_module()
    fake_dag = FakeDag([0, 0, 0, 0, 0, 7])

    with patch.object(module, "dag", fake_dag):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(), object(), mode="full", diff_base="base-commit"
            )
        )

    payload = json.loads(fake_dag.container_value.files["/reports/clean-quality-results.json"])
    assert result.exit_code == 7
    assert payload["completedSteps"][-2:] == ["quality-wrapper", "dashboard-install"]
    assert payload["attemptedSteps"][-2:] == ["dashboard-install", "dashboard-lint"]
    assert payload["failedStep"] == "dashboard-lint"
    assert ["npm", "run", "typecheck"] not in fake_dag.container_value.commands


@pytest.mark.parametrize(
    ("exit_codes", "attempted", "completed", "failed", "prompt_submitted"),
    [
        ([9], ["environment"], [], "environment", False),
        (
            [0, 7],
            ["environment", "codex-read-only-probe"],
            ["environment"],
            "codex-read-only-probe",
            False,
        ),
        (
            [0, 0, 7],
            ["environment", "codex-read-only-probe", "ambient-role-chat-e2e"],
            ["environment", "codex-read-only-probe"],
            "ambient-role-chat-e2e",
            None,
        ),
        (
            [0, 0, 0, 7],
            [
                "environment",
                "codex-read-only-probe",
                "ambient-role-chat-e2e",
                "quality-wrapper",
            ],
            ["environment", "codex-read-only-probe", "ambient-role-chat-e2e"],
            "quality-wrapper",
            True,
        ),
    ],
)
def test_dagger_quality_exports_failure_at_the_exact_completed_boundary(
    exit_codes: list[int],
    attempted: list[str],
    completed: list[str],
    failed: str,
    prompt_submitted: bool | None,
) -> None:
    module = load_dagger_module()
    fake_dag = FakeDag(exit_codes)

    with patch.object(module, "dag", fake_dag):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(FakeSource(), object(), diff_base="base-commit")
        )

    payload = json.loads(fake_dag.container_value.files["/reports/clean-quality-results.json"])
    assert result.exit_code != 0
    assert payload["status"] == "failed"
    assert payload["attemptedSteps"] == attempted
    assert payload["completedSteps"] == completed
    assert payload["failedStep"] == failed
    assert payload["promptSubmitted"] is prompt_submitted
    assert "causalFailureReport" not in payload
    assert "causalFailureSummary" not in payload
