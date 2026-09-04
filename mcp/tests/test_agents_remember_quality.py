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
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles import (
    canonicalize_repository_profile,
    compile_repository_profile_plan,
    load_repository_profile,
    repository_profile_digest,
)
from agents_remember_test_support.testing.dagger_admission import (
    DAGGER_TEST_ATTESTATION_ENV,
    DaggerAdmissionError,
    dagger_admission_refusal,
)
from conftest import prepare_certifying_pytest_bootstrap
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    RUST_FIXTURE,
    FakeContainer,
    FakeDag,
    FakeFile,
    FakeSource,
    _fake_selector_result,
    fixture_execution_manifest,
)

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


GATE1_RAILS = [
    "causal-preflight",
    "dashboard-build",
    "dashboard-codegen",
    "dashboard-lint",
    "dashboard-prerequisites",
    "dashboard-typecheck",
    "dependency-prerequisites",
    "evidence-lifecycle",
    "file-size",
    "generated-harness",
    "generated-projection-types",
    "generated-runtime",
    "generated-skills",
    "layering",
    "pyright",
    "quality-config-scope",
    "radon-cc",
    "radon-mi",
    "ruff",
    "ruff-format",
    "runtime-capability",
    "test-selection-ownership",
]
TEST_RUNTIME_AUTHORITY_DIGEST = "a" * 64


def runtime_authority_manifest() -> dict[str, object]:
    return {
        "schemaVersion": "dagger-runtime-authority/v1",
        "snapshotDigest": TEST_RUNTIME_AUTHORITY_DIGEST,
        "endpoint": "container://test-dagger-engine",
        "layerStore": "/var/lib/dagger",
    }


def execution_manifest(*, mode: str, candidate: str = "c" * 40) -> FakeFile:
    admitted = load_repository_profile(
        "agents-remember",
        REPOSITORY_ROOT,
        AGENTS_REMEMBER_PROFILE_REFERENCE,
    )
    selection_id = "closeout-targeted" if mode == "targeted" else "closeout-full"
    plan = compile_repository_profile_plan(
        admitted.canonical,
        selection_id=selection_id,
        candidate_identity=CandidateIdentity(kind="git-tree", value=candidate),
    )
    profile = admitted.canonical.profile
    return FakeFile(
        json.dumps(
            {
                "schemaVersion": "repository-certification-admission/v1",
                "candidateTree": candidate,
                "profile": {"profileDigest": admitted.canonical.profileDigest},
                "profilePlan": plan.model_dump(mode="json"),
                "executorAdapter": profile.executorAdapters[0].model_dump(mode="json"),
                "resultDecoder": profile.resultDecoders[0].model_dump(mode="json"),
                "publishedArtifacts": [
                    artifact.model_dump(mode="json") for artifact in profile.publishedArtifacts
                ],
                "runtimeAuthority": runtime_authority_manifest(),
            }
        )
    )


def admitted_command_index(commands: list[list[str]], expected: list[str]) -> int:
    """Locate one exact profile command behind the bounded timeout launcher."""

    return next(
        index
        for index, command in enumerate(commands)
        if len(command) >= 5
        and command[:3] == ["timeout", "--signal=TERM", "--kill-after=10s"]
        and command[3].endswith("s")
        and command[4:] == expected
    )


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
        "portable_certification",
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
    profile = json.loads(
        (REPOSITORY_ROOT / AGENTS_REMEMBER_PROFILE_REFERENCE).read_text(encoding="utf-8")
    )
    commands = [rail["execution"]["command"] for rail in profile["rails"]]
    assert any("scripts/e2e_harness/run.py" in command for command in commands)

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
        "image_reference",
    )
    operations = fake_dag.container_value.operations

    def find_operation(kind: str, needle: str) -> tuple[int, tuple[object, ...]]:
        return next(
            (index, operation)
            for index, operation in enumerate(operations)
            if operation[0] == kind and needle in " ".join(str(part) for part in operation[1:])
        )

    runtime_build_index, runtime_build = find_operation("exec", "install-python-runtime.sh")
    runtime_link_index, runtime_link = find_operation("exec", 'ln -s "cpython-$AR_PYTHON_VERSION"')
    source_index, _ = find_operation("directory", "/workspace")
    install_index, _ = find_operation("exec", "uv sync")
    late_names = {
        "PYTHONPATH",
        "AR_QUALITY_INVOCATION",
        "AR_QUALITY_RETRY_CACHE",
        "AR_DAGGER_TEST_ATTESTATION",
        "AR_QUALITY_ATTEMPT_NONCE",
        "AR_QUALITY_PROGRESS_REPORT",
        "COVERAGE_FILE",
    }
    late_indices = [
        index
        for index, operation in enumerate(operations)
        if operation[0] == "env" and operation[1] in late_names
    ]
    retry_cache_index, _ = find_operation("cache", module.RETRY_CACHE_ROOT)

    assert len(late_indices) == len(late_names)
    assert not {("env", "AR_CODEX_PROBE_MODE"), ("env", "AR_CODEX_PROBE_REPORT")} & {
        operation[:2] for operation in operations
    }
    assert runtime_build[1:3] == ("bash", "-euc")
    assert "exec bash" in str(runtime_build[3]) and "ln -s" not in str(runtime_build[3])
    assert runtime_link[1:3] == ("bash", "-euc")
    assert "install-python-runtime.sh" not in str(runtime_link[3])
    assert runtime_build_index < runtime_link_index < source_index < install_index
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


def test_real_codex_probe_activation_belongs_only_to_the_explicit_gate_four_rail() -> None:
    admitted = load_repository_profile(
        "agents-remember",
        REPOSITORY_ROOT,
        AGENTS_REMEMBER_PROFILE_REFERENCE,
    )
    codex_rail = next(
        rail
        for rail in admitted.canonical.profile.rails
        if rail.identity.railId == "codex-read-only-probe"
    )
    assert codex_rail.gate == 4
    assert codex_rail.execution.command[:3] == (
        "env",
        "AR_CODEX_PROBE_MODE=real",
        "AR_CODEX_PROBE_REPORT={reports}/codex-probe.json",
    )

    local_plan = compile_repository_profile_plan(
        admitted.canonical,
        selection_id="local-targeted",
        candidate_identity=CandidateIdentity(kind="git-tree", value="c" * 40),
    )
    assert local_plan.gates[3].applicability == "not-applicable"
    assert local_plan.gates[3].rails == ()
    assert all(
        "AR_CODEX_PROBE" not in token
        for rail in admitted.canonical.profile.rails
        if rail.gate < 4
        for token in rail.execution.command
    )


def test_gate_four_not_applicable_result_does_not_claim_real_codex_execution() -> None:
    module = load_dagger_module()
    progress = module._QualityProgress(
        container=FakeContainer([]),
        exit_code=0,
        attempted=["python-suite", "python-crap", "python-diff-coverage"],
        completed=["python-suite", "python-crap", "python-diff-coverage"],
    )

    payload = module._quality_result_payload(
        progress,
        started_at="2026-09-02T00:00:00+00:00",
        mode="targeted",
        attempt_nonce=VALID_DAGGER_NONCE,
    )

    assert "codexMode" not in payload
    assert payload["codexProtocol"] is None
    assert payload["promptSubmitted"] is False


@pytest.mark.parametrize("step", ("codex-read-only-probe", "ambient-role-chat-e2e"))
def test_attempted_gate_four_codex_rail_preserves_real_mode_fact(step: str) -> None:
    module = load_dagger_module()
    progress = module._QualityProgress(
        container=FakeContainer([]),
        exit_code=1,
        attempted=[step],
        completed=[],
        step_exit_codes={step: 1},
    )

    payload = module._quality_result_payload(
        progress,
        started_at="2026-09-02T00:00:00+00:00",
        mode="targeted",
        attempt_nonce=VALID_DAGGER_NONCE,
    )

    assert payload["codexMode"] == "real"


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
    profile = json.loads(
        (REPOSITORY_ROOT / AGENTS_REMEMBER_PROFILE_REFERENCE).read_text(encoding="utf-8")
    )

    assert "expect=ReturnType.ANY" in source
    assert profile["resultDecoders"][0]["artifactPath"] == "clean-quality-results.json"
    assert "clean-quality-results.json" not in source
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
    ("diff_base", "memory_cap", "message"),
    [
        ("", 0, "diff_base must name"),
        ("base", -1, "memory_cap_bytes cannot be negative"),
    ],
)
def test_dagger_quality_refuses_invalid_public_inputs(
    diff_base: str, memory_cap: int, message: str
) -> None:
    module = load_dagger_module()

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            module.AgentsRememberQuality().quality(
                object(),
                object(),
                object(),
                diff_base=diff_base,
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


def test_dagger_quality_executes_the_exact_targeted_profile_plan() -> None:
    module = load_dagger_module()
    fake_dag = FakeDag([])

    with (
        patch.object(module, "dag", fake_dag),
        patch.object(module.secrets, "token_hex", return_value=VALID_DAGGER_NONCE),
    ):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                execution_manifest(mode="targeted"),
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
    selector = next(
        command
        for command in commands
        if "agents_remember_test_support.code_quality.profile_selection" in command
    )
    assert selector[selector.index("--mode") + 1] == "targeted"
    assert selector[selector.index("--diff-base") + 1] == "a" * 40
    suite = next(command for command in commands if "python-suite" in command)
    assert suite[suite.index("--memory-cap-bytes") + 1] == "1024"
    assert not any(
        "agents_remember_test_support.code_quality.check" in command for command in commands
    )
    assert admitted_command_index(commands, ["npm", "ci"]) < admitted_command_index(
        commands, ["npm", "run", "build"]
    )
    assert commands.index(suite) < commands.index(
        next(command for command in commands if "python-crap" in command)
    )
    assert commands.index(next(command for command in commands if "python-crap" in command)) < (
        commands.index(
            next(command for command in commands if "scripts/e2e_harness/run.py" in command)
        )
    )
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
    assert payload["completedSteps"][0:2] == [
        "environment",
        "selector:agents-remember-test-selection",
    ]
    assert payload["completedSteps"][-1] == "teardown-process-cleanliness"
    assert len(payload["completedSteps"]) == 34
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


@pytest.mark.parametrize("fixture", (NODE_FIXTURE, RUST_FIXTURE))
def test_portable_dagger_function_interprets_one_frozen_fixture_plan(fixture) -> None:
    module = load_dagger_module()
    fake_dag = FakeDag([])
    candidate = "d" * 40
    manifest = FakeFile(json.dumps(fixture_execution_manifest(fixture, candidate_tree=candidate)))

    with (
        patch.object(module, "dag", fake_dag),
        patch.object(module.secrets, "token_hex", return_value=VALID_DAGGER_NONCE),
    ):
        result = asyncio.run(
            module.AgentsRememberQuality().portable_certification(
                object(),
                object(),
                manifest,
                diff_base="base-commit",
            )
        )

    commands = fake_dag.container_value.commands
    assert result.exit_code == 0
    assert fake_dag.container_value.image == fixture.image_reference
    assert admitted_command_index(commands, list(fixture.gate_one_command)) >= 0
    expected_suite: list[str] = []
    for token in fixture.suite_command:
        if token == "{selected-tests}":
            expected_suite.extend(["unit"] if fixture is RUST_FIXTURE else ["test/unit.test.mjs"])
        else:
            expected_suite.append(token.replace("{reports}", "/reports"))
    assert admitted_command_index(commands, expected_suite) >= 0
    payload = json.loads(fake_dag.container_value.files["/reports/result.json"])
    assert payload["status"] == "passed"
    assert payload["exit-code"] == 0
    assert payload["profileDigest"] == json.loads(manifest.value)["profile"]["profileDigest"]
    assert {
        f"publication:{fixture.suite_publication}",
        f"publication:{fixture.coverage_publication}",
        f"publication:{fixture.e2e_publication}",
    } <= set(payload["completedSteps"])


def test_profile_selector_shape_failure_is_a_typed_terminal_step() -> None:
    module = load_dagger_module()
    manifest = fixture_execution_manifest(NODE_FIXTURE, candidate_tree="d" * 40)
    loader = sys.modules["agents_remember_quality.profile_plan"]
    plan = loader.load_execution_manifest(json.dumps(manifest))

    class IncompleteSelectorContainer(FakeContainer):
        def with_exec(self, command: list[str], **kwargs: object) -> FakeContainer:
            container = super().with_exec(command, **kwargs)
            if "scripts/select-tests.sh" in command:
                script_index = command.index("scripts/select-tests.sh")
                self.files[command[script_index + 1]] = json.dumps(
                    _fake_selector_result(
                        command,
                        {"selected-tests": []},
                        complete=False,
                    )
                )
            return container

    container = IncompleteSelectorContainer([])
    progress = module._QualityProgress(
        container=container,
        exit_code=0,
        attempted=[],
        completed=[],
        step_exit_codes={},
    )
    selector = plan.selectors[0]
    name = f"selector:{selector.selector_id}"
    expected_path = f"/tmp/ar-profile/{selector.definition['resultPath']}"

    observed_path, values = asyncio.run(
        module._run_profile_selector(
            progress,
            selector,
            scalar_values={
                "reports": "/reports",
                "selection-mode": "full",
                "candidate-kind": "git-tree",
                "candidate-value": "d" * 40,
                "diff-base": "base-commit",
                "memory-cap-bytes": "0",
                "clean-room": "/workspace",
            },
        )
    )

    assert observed_path == expected_path
    assert values == {}
    assert progress.exit_code == 65
    assert progress.attempted == [name]
    assert progress.completed == []
    assert progress.step_exit_codes == {name: 65}
    assert progress.failure_details == {
        name: (
            "test-selection-ownership-incomplete: fixture://unknown-input:fixture-ownership-missing"
        )
    }
    assert progress.selection_results[name.removeprefix("selector:")]["failureCode"] == (
        "test-selection-ownership-incomplete"
    )


def test_dagger_quality_refuses_a_rail_runtime_outside_the_admitted_adapter() -> None:
    module = load_dagger_module()
    admitted = load_repository_profile(
        "agents-remember",
        REPOSITORY_ROOT,
        AGENTS_REMEMBER_PROFILE_REFERENCE,
    )
    profile = admitted.canonical.profile
    rail = profile.rails[0]
    changed = profile.model_copy(
        update={
            "profileDigest": "0" * 64,
            "rails": (
                rail.model_copy(
                    update={
                        "runtimeInputs": rail.runtimeInputs.model_copy(
                            update={"imageDigest": "0" * 64}
                        )
                    }
                ),
                *profile.rails[1:],
            ),
        }
    )
    changed = changed.model_copy(update={"profileDigest": repository_profile_digest(changed)})
    canonical = canonicalize_repository_profile(changed)
    candidate = "c" * 40
    plan = compile_repository_profile_plan(
        canonical,
        selection_id="closeout-full",
        candidate_identity=CandidateIdentity(kind="git-tree", value=candidate),
    )
    manifest = FakeFile(
        json.dumps(
            {
                "schemaVersion": "repository-certification-admission/v1",
                "candidateTree": candidate,
                "profile": {"profileDigest": canonical.profileDigest},
                "profilePlan": plan.model_dump(mode="json"),
                "executorAdapter": profile.executorAdapters[0].model_dump(mode="json"),
                "resultDecoder": profile.resultDecoders[0].model_dump(mode="json"),
                "publishedArtifacts": [
                    artifact.model_dump(mode="json") for artifact in profile.publishedArtifacts
                ],
                "runtimeAuthority": runtime_authority_manifest(),
            }
        )
    )

    with pytest.raises(ValueError, match="runtime images outside the admitted adapter"):
        asyncio.run(
            module.AgentsRememberQuality().quality(
                object(),
                object(),
                manifest,
                diff_base="base-commit",
            )
        )


def test_dagger_quality_treats_unselected_ambient_e2e_as_an_explicit_skip() -> None:
    module = load_dagger_module()
    fake_dag = FakeDag([0, 0, *([0] * 27), module.E2E_NOT_SELECTED_EXIT_CODE, *([0] * 4)])

    with patch.object(module, "dag", fake_dag):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                execution_manifest(mode="targeted"),
                diff_base="base-commit",
            )
        )

    payload = json.loads(fake_dag.container_value.files["/reports/clean-quality-results.json"])
    assert result.exit_code == 0
    assert payload["status"] == "passed"
    assert "ambient-role-chat-e2e" in payload["attemptedSteps"]
    assert "ambient-role-chat-e2e" not in payload["completedSteps"]
    assert payload["completedSteps"][-1] == "teardown-process-cleanliness"
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
    fake_dag = FakeDag([])

    with patch.object(module, "dag", fake_dag):
        asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                execution_manifest(mode="full"),
                diff_base="base-commit",
            )
        )

    suite = next(
        command for command in fake_dag.container_value.commands if "python-suite" in command
    )
    assert suite[suite.index("--mode") + 1] == "full"
    assert suite[suite.index("--diff-base") + 1] == "base-commit"
    assert suite[suite.index("--memory-cap-bytes") + 1] == "0"
    commands = fake_dag.container_value.commands
    assert admitted_command_index(commands, ["npm", "ci"]) >= 0
    assert admitted_command_index(commands, ["npm", "run", "lint"]) >= 0
    assert admitted_command_index(commands, ["npm", "run", "typecheck"]) >= 0
    assert admitted_command_index(commands, ["npm", "run", "test:coverage"]) >= 0
    assert admitted_command_index(commands, ["npm", "run", "coverage:diff"]) >= 0
    assert (
        admitted_command_index(
            commands,
            ["npm", "run", "e2e", "--", "--fail-on-flaky-tests"],
        )
        >= 0
    )
    assert admitted_command_index(commands, ["npm", "run", "build"]) >= 0
    assert ("CI", "1") in fake_dag.container_value.environment


def test_dagger_quality_red_gate_one_still_terminalizes_every_gate_one_sibling() -> None:
    """Exhaustive within Gate 1: a red rail never suppresses its same-gate siblings."""
    module = load_dagger_module()
    fake_dag = FakeDag([0, 0, 7])

    with patch.object(module, "dag", fake_dag):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                execution_manifest(mode="full"),
                diff_base="base-commit",
            )
        )

    payload = json.loads(fake_dag.container_value.files["/reports/clean-quality-results.json"])
    assert result.exit_code == 7
    attempted = payload["attemptedSteps"]
    assert attempted[:3] == [
        "environment",
        "selector:agents-remember-test-selection",
        "causal-preflight",
    ]
    # Every applicable Gate-1 sibling reached a terminal state after causal-preflight.
    assert set(attempted[2:]) == set(GATE1_RAILS)
    assert payload["failedStep"] == "causal-preflight"
    # Fail-fast is only between gates: Gate 2 never started, so no suite command ran.
    # Gate-1 siblings keep running after the red rail, including their npm rails.
    assert not any("python-suite" in command for command in fake_dag.container_value.commands)
    assert any("npm" in command for command in fake_dag.container_value.commands)
    gates = payload["gates"]
    assert gates[0]["gate"] == 1 and gates[0]["started"] is True
    assert gates[0]["disposition"] == "red"
    assert gates[0]["laterGatesZeroStart"] is True
    assert {rail["key"] for rail in gates[0]["rails"]} == {f"{rail}@1.0.0" for rail in GATE1_RAILS}
    red_rail = next(rail for rail in gates[0]["rails"] if rail["identity"] == "causal-preflight")
    assert red_rail["status"] == "fail" and red_rail["exitCode"] == 7
    siblings = {
        rail["status"] for rail in gates[0]["rails"] if rail["identity"] != "causal-preflight"
    }
    assert siblings == {"pass"}
    for gate in gates[1:]:
        assert gate["started"] is False
        assert gate["zeroStart"] is True
        assert gate["laterGatesZeroStart"] is False
        assert gate["rails"] == []
    assert payload["runtimeAuthorityDigest"] == TEST_RUNTIME_AUTHORITY_DIGEST


@pytest.mark.parametrize(
    ("exit_codes", "expected_failed", "expected_exit", "gate_one_expected"),
    [
        (
            [9],
            "environment",
            9,
            {"started": False, "disposition": "not-run", "zeroStart": True},
        ),
        (
            [0, 7],
            "blocked:test-selection-ownership",
            125,
            {"started": True, "disposition": "red", "laterGatesZeroStart": True},
        ),
        (
            [0, 0, 7],
            "causal-preflight",
            7,
            {"started": True, "disposition": "red", "laterGatesZeroStart": True},
        ),
    ],
)
def test_dagger_quality_exports_failure_at_the_exact_completed_boundary(
    exit_codes: list[int],
    expected_failed: str,
    expected_exit: int,
    gate_one_expected: dict[str, object],
) -> None:
    """Failure facts preserve the complete within-gate catalog and later-gate zero starts."""
    module = load_dagger_module()
    fake_dag = FakeDag(exit_codes)

    with patch.object(module, "dag", fake_dag):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                execution_manifest(mode="full"),
                diff_base="base-commit",
            )
        )

    payload = json.loads(fake_dag.container_value.files["/reports/clean-quality-results.json"])
    assert result.exit_code == expected_exit
    assert payload["status"] == "failed"
    assert payload["failedStep"] == expected_failed
    gate_one = payload["gates"][0]
    for key, value in gate_one_expected.items():
        assert gate_one[key] == value
    if gate_one_expected["started"]:
        assert len(gate_one["rails"]) == 22
    for gate in payload["gates"][1:]:
        assert gate["started"] is False and gate["zeroStart"] is True
    assert payload["promptSubmitted"] is False
    if expected_exit == 9:
        assert "causalFailureReport" not in payload
        assert "causalFailureSummary" not in payload
    assert payload["runtimeAuthorityDigest"] == TEST_RUNTIME_AUTHORITY_DIGEST
