"""One pinned Ubuntu quality pipeline shared by local worktrees and GitHub."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import dagger
from dagger import Doc, ReturnType, dag, field, function, object_type

from agents_remember_quality import profile_results
from agents_remember_quality.profile_plan import (
    FrozenProfilePlan,
    FrozenSelector,
    expand_command,
    load_execution_manifest,
    module_runtime_digest,
    pinned_image_digest,
)
from agents_remember_quality.quality_command import (
    ExpectedCommand,
    causal_evidence_steps,
)
from agents_remember_quality.retry_evidence_route import (
    RetryEvidenceContext,
    run_exact_retry_evidence,
    run_retry_matrix_evidence,
)

AMBIENT_CODEX_PROTOCOL = profile_results.AMBIENT_CODEX_PROTOCOL
BASELINE_CODEX_PROTOCOL = profile_results.BASELINE_CODEX_PROTOCOL
_QualityProgress = profile_results.QualityProgress
_profile_result_payload = profile_results.profile_result_payload
_quality_result_payload = profile_results.quality_result_payload

PLAYWRIGHT_IMAGE = (
    "mcr.microsoft.com/playwright:v1.60.0-noble@"
    "sha256:83192064c7510f7ee73dd63dc5f22a5e01a92c81a2e6a9c715d9e3fe55471fd9"
)
CODEX_VERSION = "0.151.0"
RETRY_CACHE_ROOT = "/var/cache/agents-remember-quality-retry"
MAX_SELECTOR_RESULT_BYTES = 8 * 1024 * 1024
PYTHON_BUILD_CACHE_ROOT = "/var/cache/agents-remember-python"
RUNTIME_INSTALLER_ROOT = "/opt/agents-remember-runtime-installer"
RUNTIME_CONTRACT = f"{RUNTIME_INSTALLER_ROOT}/python-runtime-contract.env"
RUNTIME_CHECKER = f"{RUNTIME_INSTALLER_ROOT}/check-python-runtime.py"
RUNTIME_INSTALLER = f"{RUNTIME_INSTALLER_ROOT}/install-python-runtime.sh"
RUNTIME_ROOT = "/opt/agents-remember-python"
RUNTIME_CURRENT = f"{RUNTIME_ROOT}/current"
RUNTIME_PYTHON = f"{RUNTIME_CURRENT}/bin/python3.13"
RUNTIME_UV = f"{RUNTIME_CURRENT}/bin/uv"
RUNTIME_PROOF = "/opt/agents-remember-python-runtime.json"
VENV_ROOT = "/opt/ar-venv"
VENV_PYTHON = f"{VENV_ROOT}/bin/python"
VENV_PROOF = "/opt/agents-remember-venv-runtime.json"
E2E_NOT_SELECTED_EXIT_CODE = 78


def _canonical_python_base(
    source: dagger.Directory,
    *,
    image_reference: str = PLAYWRIGHT_IMAGE,
) -> dagger.Container:
    """Provision one source-built Python recipe before candidate-specific source."""

    return (
        dag.container()
        .from_(image_reference)
        .with_mounted_cache("/root/.cache/pip", dag.cache_volume("ar-quality-pip-v1"))
        .with_mounted_cache(
            PYTHON_BUILD_CACHE_ROOT,
            dag.cache_volume("ar-python-source-build-v1"),
        )
        .with_file(
            RUNTIME_CONTRACT,
            source.file("scripts/python-runtime-contract.env"),
        )
        .with_file(
            RUNTIME_CHECKER,
            source.file("scripts/check-python-runtime.py"),
        )
        .with_file(
            RUNTIME_INSTALLER,
            source.file("scripts/install-python-runtime.sh"),
        )
        .with_env_variable("DEBIAN_FRONTEND", "noninteractive")
        .with_exec(["apt-get", "update"])
        .with_exec(
            [
                "bash",
                "-c",
                f". {RUNTIME_CONTRACT}; apt-get install -y --no-install-recommends "
                "$AR_PYTHON_APT_BUILD_DEPS tmux",
            ]
        )
        .with_exec(["rm", "-rf", "/var/lib/apt/lists"])
        .with_exec(
            [
                "bash",
                "-c",
                f". {RUNTIME_CONTRACT}; "
                f"bash {RUNTIME_INSTALLER} "
                f'--prefix "{RUNTIME_ROOT}/cpython-$AR_PYTHON_VERSION" '
                f"--cache-root {PYTHON_BUILD_CACHE_ROOT} "
                f"--tooling-root {PYTHON_BUILD_CACHE_ROOT}/tooling; "
                f"mkdir -p {RUNTIME_ROOT}; cd {RUNTIME_ROOT}; "
                'ln -s "cpython-$AR_PYTHON_VERSION" current',
            ]
        )
        .with_exec(
            [
                "bash",
                "-c",
                f". {RUNTIME_CONTRACT}; {RUNTIME_PYTHON} {RUNTIME_CHECKER} "
                '--expected-version "$AR_PYTHON_VERSION" '
                f"--expected-base-prefix {RUNTIME_CURRENT} "
                "--require-linux-pidfd "
                '--source-url "$AR_PYTHON_SOURCE_URL" '
                '--source-sha256 "$AR_PYTHON_SOURCE_SHA256" '
                '--builder-commit "$AR_PYTHON_BUILD_COMMIT" '
                f"> {RUNTIME_PROOF}",
            ]
        )
        .with_exec(
            [
                "bash",
                "-c",
                f". {RUNTIME_CONTRACT}; {RUNTIME_PYTHON} -m pip install "
                '--disable-pip-version-check "uv==$AR_UV_VERSION"',
            ]
        )
    )


def _candidate_base(
    source: dagger.Directory,
    repository_bundle: dagger.File,
    *,
    image_reference: str = PLAYWRIGHT_IMAGE,
) -> dagger.Container:
    """Build the deterministic candidate environment shared by every evidence attempt."""

    return (
        _canonical_python_base(source, image_reference=image_reference)
        .with_mounted_cache("/root/.npm", dag.cache_volume("ar-quality-npm-v1"))
        .with_env_variable("HOME", "/tmp/ar-home")
        .with_env_variable("PIP_CACHE_DIR", "/root/.cache/pip")
        .with_env_variable("TMPDIR", "/tmp/ar-quality")
        .with_env_variable("TMP", "/tmp/ar-quality")
        .with_env_variable("TEMP", "/tmp/ar-quality")
        .with_exec(["mkdir", "-p", "/tmp/ar-home", "/tmp/ar-quality"])
        .with_exec(["npm", "install", "--global", f"@openai/codex@{CODEX_VERSION}"])
        .with_exec(["codex", "--version"])
        .with_exec([RUNTIME_PYTHON, "-m", "venv", VENV_ROOT])
        .with_directory("/workspace", source)
        .with_file("/tmp/ar-candidate.bundle", repository_bundle)
        .with_workdir("/workspace")
        .with_exec(["rm", "-rf", ".git"])
        .with_exec(["git", "init"])
        .with_exec(["git", "fetch", "--no-tags", "/tmp/ar-candidate.bundle", "HEAD"])
        .with_exec(["git", "reset", "--mixed", "FETCH_HEAD"])
        .with_exec(["git", "add", "--all"])
        .with_exec(["git", "rev-parse", "--verify", "FETCH_HEAD^{commit}"])
        .with_exec(
            [
                "git",
                "config",
                "--local",
                "user.email",
                "clean-room@agents-remember.invalid",
            ]
        )
        .with_exec(
            [
                "git",
                "config",
                "--local",
                "user.name",
                "Agents Remember clean room",
            ]
        )
        .with_exec(
            [
                "bash",
                "-c",
                f"UV_PROJECT_ENVIRONMENT={VENV_ROOT} {RUNTIME_UV} sync "
                f"--project /workspace/mcp --python {VENV_PYTHON} "
                "--no-managed-python --frozen --all-extras",
            ]
        )
        .with_exec(
            [
                "bash",
                "-c",
                f". {RUNTIME_CONTRACT}; {VENV_PYTHON} {RUNTIME_CHECKER} "
                '--expected-version "$AR_PYTHON_VERSION" '
                f"--expected-base-prefix {RUNTIME_CURRENT} "
                "--require-linux-pidfd "
                '--source-url "$AR_PYTHON_SOURCE_URL" '
                '--source-sha256 "$AR_PYTHON_SOURCE_SHA256" '
                '--builder-commit "$AR_PYTHON_BUILD_COMMIT" '
                f"> {VENV_PROOF}",
            ]
        )
        .with_exec([RUNTIME_UV, "pip", "check", "--python", VENV_PYTHON])
    )


def _bind_candidate_attempt(
    container: dagger.Container,
    *,
    attempt_nonce: str,
    reports: str,
) -> dagger.Container:
    """Bind non-deterministic evidence inputs after reusable candidate setup."""

    return (
        container.with_mounted_cache(
            RETRY_CACHE_ROOT,
            dag.cache_volume("ar-quality-retry-v3"),
            sharing=dagger.CacheSharingMode.LOCKED,
        )
        .with_env_variable(
            "PYTHONPATH",
            "/workspace/mcp/test_support:/workspace/mcp/src",
        )
        .with_env_variable("AR_QUALITY_INVOCATION", "ci")
        .with_env_variable("CI", "1")
        .with_env_variable("AR_QUALITY_RETRY_CACHE", RETRY_CACHE_ROOT)
        .with_env_variable("AR_DAGGER_TEST_ATTESTATION", attempt_nonce)
        .with_env_variable("AR_QUALITY_ATTEMPT_NONCE", attempt_nonce)
        .with_env_variable("AR_CERTIFICATION_SELECTOR_ROOT", "/tmp/ar-profile")
        .with_env_variable("AR_QUALITY_PROGRESS_REPORT", f"{reports}/quality-progress.json")
        .with_env_variable("COVERAGE_FILE", f"{reports}/coverage.data")
        .with_exec(["mkdir", "-p", reports, "/tmp/ar-profile"])
        .with_exec(["cp", RUNTIME_PROOF, f"{reports}/python-runtime.json"])
        .with_exec(["cp", VENV_PROOF, f"{reports}/python-venv-runtime.json"])
        .with_exec(
            [
                "sh",
                "-c",
                "umask 077; printf '%s' \"$AR_DAGGER_TEST_ATTESTATION\" "
                "> /tmp/ar-quality/dagger-test-attestation",
            ]
        )
    )


def _candidate_container(
    source: dagger.Directory,
    repository_bundle: dagger.File,
    *,
    attempt_nonce: str,
    reports: str,
    image_reference: str = PLAYWRIGHT_IMAGE,
) -> dagger.Container:
    """Build one reusable candidate base, then bind an exact evidence attempt."""

    return _bind_candidate_attempt(
        _candidate_base(
            source,
            repository_bundle,
            image_reference=image_reference,
        ),
        attempt_nonce=attempt_nonce,
        reports=reports,
    )


async def _run_expected_commands(
    container: dagger.Container,
    commands: tuple[ExpectedCommand, ...],
    step_codes: dict[str, int],
) -> tuple[dagger.Container, bool]:
    """Run a non-accepting route whose deliberate failures are part of its proof."""

    route_ok = all(code == 0 for code in step_codes.values())
    for step in commands:
        if not route_ok:
            break
        container = await container.with_exec(
            list(step.command),
            expect=ReturnType.ANY,
        ).sync()
        code = await container.exit_code()
        step_codes[step.name] = code
        route_ok = code == step.expected_exit
    return container, route_ok


@dataclass(frozen=True)
class _ProfileRunInputs:
    plan: FrozenProfilePlan
    mode: str
    diff_base: str
    reports: str
    memory_cap_bytes: int


async def _admitted_profile_plan(
    execution_manifest: dagger.File,
    *,
    function_name: str,
) -> FrozenProfilePlan:
    plan = load_execution_manifest(await execution_manifest.contents())
    if plan.executor.get("runtimeDigest") != module_runtime_digest():
        raise ValueError("execution manifest names another Dagger adapter runtime")
    if plan.executor.get("functionName") != function_name:
        raise ValueError("execution manifest names another Dagger adapter function")
    return plan


def _require_adapter_runtime(plan: FrozenProfilePlan) -> str:
    """Require every rail to consume the admitted single-image adapter runtime."""

    image_reference = str(plan.executor["imageReference"])
    image_digest = pinned_image_digest(image_reference)
    mismatched = sorted(
        rail.identity for rail in plan.rails if rail.runtime.get("imageDigest") != image_digest
    )
    if mismatched:
        raise ValueError(
            "repository profile rails name runtime images outside the admitted adapter: "
            + ", ".join(mismatched)
        )
    return image_reference


def _portable_candidate_container(
    source: dagger.Directory,
    repository_bundle: dagger.File,
    *,
    image_reference: str,
    reports: str,
) -> dagger.Container:
    """Reconstruct one exact fixture repository in its profile-owned clean image."""

    return (
        dag.container()
        .from_(image_reference)
        .with_env_variable("CI", "1")
        .with_env_variable("AR_CERTIFICATION_SELECTOR_ROOT", "/tmp/ar-profile")
        .with_directory("/workspace", source)
        .with_file("/tmp/repository-candidate.bundle", repository_bundle)
        .with_workdir("/workspace")
        .with_exec(["mkdir", "-p", reports, "/tmp/ar-profile"])
        .with_exec(["rm", "-rf", ".git"])
        .with_exec(["git", "init"])
        .with_exec(
            [
                "git",
                "fetch",
                "--no-tags",
                "/tmp/repository-candidate.bundle",
                "HEAD",
            ]
        )
        .with_exec(["git", "reset", "--mixed", "FETCH_HEAD"])
        .with_exec(["git", "add", "--all"])
    )


async def _run_profile_acceptance(
    container: dagger.Container,
    inputs: _ProfileRunInputs,
) -> _QualityProgress:
    """Execute only commands recovered from the exact admitted semantic closure."""

    environment_exit = await container.exit_code()
    progress = _QualityProgress(
        container=container,
        exit_code=environment_exit,
        attempted=["environment"],
        completed=["environment"] if environment_exit == 0 else [],
        step_exit_codes={"environment": environment_exit},
    )
    scalar_values = {
        "reports": inputs.reports,
        "selection-mode": inputs.mode,
        "diff-base": inputs.diff_base,
        "memory-cap-bytes": str(inputs.memory_cap_bytes),
        "clean-room": "/workspace",
    }
    selector_results: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {}
    for selector in inputs.plan.selectors:
        if progress.exit_code != 0:
            break
        result_path, values = await _run_profile_selector(
            progress,
            selector,
            scalar_values=scalar_values,
        )
        if progress.exit_code == 0:
            selector_results[selector.selector_id] = (result_path, values)
    for rail in inputs.plan.rails:
        if progress.exit_code != 0:
            break
        execution = rail.execution
        if execution.get("adapterKind") != "container-command":
            raise ValueError(
                f"rail {rail.identity} names unsupported sandbox adapter "
                f"{execution.get('adapterKind')!r}"
            )
        provider = execution.get("scopeProviderId")
        rail_scalars = dict(scalar_values)
        rail_lists: dict[str, tuple[str, ...]] = {}
        if provider is not None:
            if not isinstance(provider, str) or provider not in selector_results:
                raise ValueError(f"rail {rail.identity} lacks its admitted selector result")
            selector_path, rail_lists = selector_results[provider]
            rail_scalars["selector-output"] = selector_path
        command = expand_command(
            execution.get("command", []),
            scalar_values=rail_scalars,
            list_values=rail_lists,
        )
        await _run_profile_rail(progress, rail.identity, execution, command)
    return progress


async def _run_profile_selector(
    progress: _QualityProgress,
    selector: FrozenSelector,
    *,
    scalar_values: dict[str, str],
) -> tuple[str, dict[str, tuple[str, ...]]]:
    definition = selector.definition
    result_path = _selector_result_path(str(definition["resultPath"]))
    command = expand_command(
        definition.get("command", []),
        scalar_values={**scalar_values, "selector-output": result_path},
        list_values={},
    )
    name = f"selector:{selector.selector_id}"
    progress.attempted.append(name)
    progress.container = (
        await progress.container.with_workdir(
            _workspace_path(str(definition["workingDirectory"]), directory=True)
        )
        .with_exec(command, expect=ReturnType.ANY)
        .sync()
    )
    code = await progress.container.exit_code()
    progress.step_exit_codes[name] = code
    progress.exit_code = code
    if code != 0:
        return result_path, {}
    exists = await progress.container.exists(
        result_path,
        expected_type=dagger.ExistsType.REGULAR_TYPE,
        do_not_follow_symlinks=True,
    )
    size = await progress.container.file(result_path).size() if exists else None
    if size is None or size > MAX_SELECTOR_RESULT_BYTES:
        return _selector_result_failure(
            progress,
            name,
            result_path,
            "selector result is missing, unsafe, or exceeds the framework maximum",
        )
    try:
        payload = json.loads(await progress.container.file(result_path).contents())
    except (json.JSONDecodeError, TypeError):
        return _selector_result_failure(
            progress,
            name,
            result_path,
            "selector result is not valid JSON",
        )
    if (
        not isinstance(payload, dict)
        or payload.get("schemaVersion") != definition.get("schemaVersion")
        or payload.get("complete") is not True
    ):
        return _selector_result_failure(
            progress,
            name,
            result_path,
            "selector result has the wrong schema or is incomplete",
        )
    values: dict[str, tuple[str, ...]] = {}
    outputs = definition.get("outputArtifacts")
    if not isinstance(outputs, list):
        raise ValueError(f"selector {selector.selector_id} output contract is invalid")
    for output in outputs:
        if not isinstance(output, str):
            raise ValueError(f"selector {selector.selector_id} output identity is invalid")
        raw = payload.get(output)
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            return _selector_result_failure(
                progress,
                name,
                result_path,
                f"selector result omitted string-list {output}",
            )
        values[output] = tuple(raw)
    progress.completed.append(name)
    return result_path, values


def _selector_result_failure(
    progress: _QualityProgress,
    name: str,
    result_path: str,
    detail: str,
) -> tuple[str, dict[str, tuple[str, ...]]]:
    progress.exit_code = 65
    progress.step_exit_codes[name] = progress.exit_code
    progress.failure_details[name] = detail
    return result_path, {}


async def _run_profile_rail(
    progress: _QualityProgress,
    name: str,
    execution: dict[str, object],
    command: list[str],
) -> None:
    success_codes = execution.get("successExitCodes")
    skipped_codes = execution.get("skippedExitCodes")
    if (
        not isinstance(success_codes, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in success_codes)
        or not isinstance(skipped_codes, list)
        or any(isinstance(item, bool) or not isinstance(item, int) for item in skipped_codes)
    ):
        raise ValueError(f"rail {name} exit contract is invalid")
    environment = execution.get("environmentContract")
    if not isinstance(environment, list) or any(
        not isinstance(item, str) or not item for item in environment
    ):
        raise ValueError(f"rail {name} environment contract is invalid")
    missing_environment = [
        item for item in environment if await progress.container.env_variable(item) is None
    ]
    if missing_environment:
        progress.attempted.append(name)
        progress.exit_code = 125
        progress.step_exit_codes[name] = progress.exit_code
        progress.failure_details[name] = (
            "executor prerequisite missing declared environment: " + ", ".join(missing_environment)
        )
        return
    timeout_seconds = execution.get("timeoutSeconds")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds <= 0
    ):
        raise ValueError(f"rail {name} timeout contract is invalid")
    progress.attempted.append(name)
    bounded_command = [
        "timeout",
        "--signal=TERM",
        "--kill-after=10s",
        f"{timeout_seconds}s",
        *command,
    ]
    progress.container = (
        await progress.container.with_workdir(
            _workspace_path(str(execution["workingDirectory"]), directory=True)
        )
        .with_exec(bounded_command, expect=ReturnType.ANY)
        .sync()
    )
    code = await progress.container.exit_code()
    progress.step_exit_codes[name] = code
    if code in skipped_codes:
        progress.skipped.append(name)
        progress.exit_code = 0
    elif code in success_codes:
        progress.completed.append(name)
        progress.exit_code = 0
    else:
        progress.exit_code = code or 1


def _workspace_path(relative: str, *, directory: bool = False) -> str:
    if relative == "." and directory:
        return "/workspace"
    if (
        not relative
        or relative.startswith(("/", "\\"))
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValueError("profile execution path is not confined to the candidate")
    return f"/workspace/{relative}"


def _selector_result_path(relative: str) -> str:
    if (
        not relative
        or relative.startswith(("/", "\\"))
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValueError("profile selector result path is not confined")
    return f"/tmp/ar-profile/{relative}"


async def _verify_required_profile_publications(
    progress: _QualityProgress,
    plan: FrozenProfilePlan,
    *,
    reports: str,
) -> None:
    """Consume the admitted publication policies before terminal publication."""

    if progress.exit_code != 0:
        return
    terminal_path = str(plan.decoder["artifactPath"])
    for publication in plan.published_artifacts:
        if publication.get("required") is not True:
            continue
        relative = str(publication["path"])
        if relative == terminal_path:
            continue
        name = f"publication:{relative}"
        progress.attempted.append(name)
        path = f"{reports}/{relative}"
        exists = await progress.container.exists(
            path,
            expected_type=dagger.ExistsType.REGULAR_TYPE,
            do_not_follow_symlinks=True,
        )
        maximum = publication.get("maxBytes")
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError(f"publication {relative} has an invalid size contract")
        size = await progress.container.file(path).size() if exists else None
        if size is None or size > maximum:
            progress.exit_code = 66
            progress.step_exit_codes[name] = progress.exit_code
            progress.failure_details[name] = (
                "required publication is missing, unsafe, or exceeds its admitted maximum"
            )
            return
        progress.completed.append(name)
        progress.step_exit_codes[name] = 0


@object_type
class QualityResult:
    """Exportable evidence plus the canonical pipeline exit code."""

    reports: dagger.Directory = field()
    exit_code: int = field()


@object_type
class AgentsRememberQuality:
    """Run Agents Remember quality in a pristine Ubuntu userland."""

    @function
    async def quality(
        self,
        source: Annotated[
            dagger.Directory,
            Doc("Exact candidate source tree to install and test in clean Ubuntu."),
        ],
        repository_bundle: Annotated[
            dagger.File,
            Doc("Git bundle containing the candidate commit and its ancestry."),
        ],
        execution_manifest: Annotated[
            dagger.File,
            Doc("Exact MCP-admitted profile, semantic closure, and candidate plan."),
        ],
        diff_base: Annotated[
            str,
            Doc(
                "Required Git commit used for changed-line coverage: the leaf base in "
                "targeted mode or the super-integration base in full mode."
            ),
        ],
        memory_cap_bytes: Annotated[
            int,
            Doc("Optional container memory cap in bytes; zero leaves memory host-managed."),
        ] = 0,
    ) -> QualityResult:
        """Run only the exact admitted repository profile inside clean Ubuntu."""
        started_at = datetime.now(UTC).isoformat()
        if not diff_base.strip():
            raise ValueError("diff_base must name the explicit acceptance comparison commit")
        if memory_cap_bytes < 0:
            raise ValueError("memory_cap_bytes cannot be negative")
        plan = await _admitted_profile_plan(
            execution_manifest,
            function_name="quality",
        )
        mode = plan.mode
        if plan.candidate_kind != "git-tree":
            raise ValueError("certifying Dagger execution requires a git-tree candidate")
        reports = "/reports"
        attempt_nonce = secrets.token_hex(16)
        container = _candidate_container(
            source,
            repository_bundle,
            attempt_nonce=attempt_nonce,
            reports=reports,
            image_reference=_require_adapter_runtime(plan),
        )
        container = await container.with_exec(
            [
                "sh",
                "-c",
                f'test "$(git write-tree)" = "{plan.candidate_value}"',
            ],
            expect=ReturnType.ANY,
        ).sync()
        progress = await _run_profile_acceptance(
            container,
            _ProfileRunInputs(
                plan=plan,
                mode=mode,
                diff_base=diff_base,
                reports=reports,
                memory_cap_bytes=memory_cap_bytes,
            ),
        )
        await _verify_required_profile_publications(progress, plan, reports=reports)
        result = _quality_result_payload(
            progress,
            started_at=started_at,
            mode=mode,
            attempt_nonce=attempt_nonce,
        )
        result[plan.decoder["statusField"]] = (
            plan.decoder["passedValue"] if progress.exit_code == 0 else plan.decoder["failedValue"]
        )
        result[plan.decoder["exitCodeField"]] = progress.exit_code
        result["profileDigest"] = plan.profile_digest
        result["profilePlanDigest"] = plan.plan_digest
        container = progress.container.with_new_file(
            f"{reports}/{plan.decoder['artifactPath']}",
            contents=json.dumps(result, indent=2, sort_keys=True) + "\n",
        )
        return QualityResult(
            reports=container.directory(reports),
            exit_code=progress.exit_code,
        )

    @function
    async def portable_certification(
        self,
        source: Annotated[
            dagger.Directory,
            Doc("Exact portable fixture source tree to execute in its admitted image."),
        ],
        repository_bundle: Annotated[
            dagger.File,
            Doc("Git bundle containing the exact portable fixture candidate."),
        ],
        execution_manifest: Annotated[
            dagger.File,
            Doc("Exact MCP-admitted portable profile and semantic closure."),
        ],
        diff_base: Annotated[
            str,
            Doc("Explicit comparison identity made available to declared fixture commands."),
        ],
        memory_cap_bytes: Annotated[
            int,
            Doc("Optional profile command memory input; zero leaves it image-managed."),
        ] = 0,
    ) -> QualityResult:
        """Execute a non-Agents-Remember profile through the same frozen-plan interpreter."""

        started_at = datetime.now(UTC).isoformat()
        if not diff_base.strip():
            raise ValueError("diff_base must name the portable comparison identity")
        if memory_cap_bytes < 0:
            raise ValueError("memory_cap_bytes cannot be negative")
        plan = await _admitted_profile_plan(
            execution_manifest,
            function_name="portable-certification",
        )
        mode = plan.mode
        if plan.candidate_kind != "git-tree":
            raise ValueError("portable certification requires a git-tree candidate")
        reports = "/reports"
        attempt_nonce = secrets.token_hex(16)
        container = _portable_candidate_container(
            source,
            repository_bundle,
            image_reference=_require_adapter_runtime(plan),
            reports=reports,
        )
        container = await container.with_exec(
            [
                "sh",
                "-c",
                f'test "$(git write-tree)" = "{plan.candidate_value}"',
            ],
            expect=ReturnType.ANY,
        ).sync()
        progress = await _run_profile_acceptance(
            container,
            _ProfileRunInputs(
                plan=plan,
                mode=mode,
                diff_base=diff_base,
                reports=reports,
                memory_cap_bytes=memory_cap_bytes,
            ),
        )
        await _verify_required_profile_publications(progress, plan, reports=reports)
        result = _profile_result_payload(
            progress,
            started_at=started_at,
            mode=mode,
            attempt_nonce=attempt_nonce,
        )
        result[plan.decoder["statusField"]] = (
            plan.decoder["passedValue"] if progress.exit_code == 0 else plan.decoder["failedValue"]
        )
        result[plan.decoder["exitCodeField"]] = progress.exit_code
        result["profileDigest"] = plan.profile_digest
        result["profilePlanDigest"] = plan.plan_digest
        container = progress.container.with_new_file(
            f"{reports}/{plan.decoder['artifactPath']}",
            contents=json.dumps(result, indent=2, sort_keys=True) + "\n",
        )
        return QualityResult(
            reports=container.directory(reports),
            exit_code=progress.exit_code,
        )

    @function
    async def retry_evidence(
        self,
        source: Annotated[
            dagger.Directory,
            Doc("Exact candidate source tree for non-accepting retry-route evidence."),
        ],
        repository_bundle: Annotated[
            dagger.File,
            Doc("Git bundle containing the candidate commit and its ancestry."),
        ],
        diff_base: Annotated[
            str,
            Doc("Explicit comparison commit used by both retry attempts."),
        ],
        mode: Annotated[
            str,
            Doc("Either 'targeted' or 'full'; both attempts use the same population."),
        ] = "targeted",
    ) -> QualityResult:
        """Prove fresh publication then exact reuse across two real Dagger containers."""

        if mode not in {"targeted", "full"}:
            raise ValueError(f"unknown retry evidence mode: {mode}")
        if not diff_base.strip():
            raise ValueError("diff_base must name the explicit retry comparison commit")
        context = RetryEvidenceContext(
            source,
            repository_bundle,
            diff_base,
            RETRY_CACHE_ROOT,
            _candidate_container,
        )
        outcome = await run_exact_retry_evidence(
            context,
            mode=mode,
        )
        return QualityResult(
            reports=outcome.container.directory("/reports"),
            exit_code=outcome.exit_code,
        )

    @function
    async def retry_matrix_evidence(
        self,
        source: Annotated[
            dagger.Directory,
            Doc("Exact candidate source tree for non-accepting retry-matrix evidence."),
        ],
        repository_bundle: Annotated[
            dagger.File,
            Doc("Git bundle containing the candidate commit and its ancestry."),
        ],
        diff_base: Annotated[
            str,
            Doc("Explicit comparison commit used by every retry-matrix attempt."),
        ],
    ) -> QualityResult:
        """Prove mutation, lane, context, and filtering decisions on the real wrapper."""

        if not diff_base.strip():
            raise ValueError("diff_base must name the explicit retry comparison commit")
        context = RetryEvidenceContext(
            source,
            repository_bundle,
            diff_base,
            RETRY_CACHE_ROOT,
            _candidate_container,
        )
        outcome = await run_retry_matrix_evidence(context)
        return QualityResult(
            reports=outcome.container.directory("/reports"),
            exit_code=outcome.exit_code,
        )

    @function
    async def causal_evidence(
        self,
        source: Annotated[
            dagger.Directory,
            Doc("Exact candidate source tree for non-accepting causal-route evidence."),
        ],
        repository_bundle: Annotated[
            dagger.File,
            Doc("Git bundle containing the candidate commit and its ancestry."),
        ],
    ) -> QualityResult:
        """Prove exact-node suppression and independent execution on real pytest."""

        reports = "/reports"
        attempt_nonce = secrets.token_hex(16)
        container = (
            _candidate_container(
                source,
                repository_bundle,
                attempt_nonce=attempt_nonce,
                reports=reports,
            )
            .with_env_variable("AR_QUALITY_ATTEMPT_NONCE", attempt_nonce)
            .with_env_variable("AR_CAUSAL_EVIDENCE_FORCE_DEPENDENT_FAILURE", "1")
        )
        container = await container.sync()
        step_codes: dict[str, int] = {"environment": await container.exit_code()}
        container, route_ok = await _run_expected_commands(
            container,
            causal_evidence_steps(reports),
            step_codes,
        )
        payload = {
            "schemaVersion": "ar-causal-route-results/v1",
            "status": "passed" if route_ok else "failed",
            "acceptanceEligible": False,
            "stepExitCodes": step_codes,
        }
        container = container.with_new_file(
            f"{reports}/causal-route-results.json",
            contents=json.dumps(payload, indent=2, sort_keys=True) + "\n",
        )
        return QualityResult(
            reports=container.directory(reports),
            exit_code=0 if route_ok else 1,
        )

    @function
    async def cadence_evidence(
        self,
        source: Annotated[
            dagger.Directory,
            Doc("Exact candidate source tree for non-accepting cadence evidence."),
        ],
        repository_bundle: Annotated[
            dagger.File,
            Doc("Git bundle containing the candidate commit and its ancestry."),
        ],
        trigger: Annotated[
            str,
            Doc("One of 'scheduled', 'provider-bump', or 'migration-window'."),
        ],
    ) -> QualityResult:
        """Run an explicit evidence cadence without minting quality acceptance."""

        allowed = {"scheduled", "provider-bump", "migration-window"}
        if trigger not in allowed:
            raise ValueError(f"unknown cadence evidence trigger: {trigger}")
        started_at = datetime.now(UTC).isoformat()
        reports = "/reports"
        attempt_nonce = secrets.token_hex(16)
        container = await _candidate_container(
            source,
            repository_bundle,
            attempt_nonce=attempt_nonce,
            reports=reports,
        ).sync()
        exit_code = await container.exit_code()
        completed = ["environment"]
        if exit_code == 0:
            container = await container.with_exec(
                [
                    "/opt/ar-venv/bin/python",
                    "-m",
                    "agents_remember_test_support.testing.cadence_runner",
                    "--project-root",
                    "/workspace",
                    "--trigger",
                    trigger,
                    "--json-output",
                    f"{reports}/cadence-evidence.json",
                    "--pytest-report-log",
                    f"{reports}/pytest-events.jsonl",
                    "--pytest-phase-report",
                    f"{reports}/pytest-phases.json",
                ],
                expect=ReturnType.ANY,
            ).sync()
            exit_code = await container.exit_code()
            completed.append("cadence-evidence")
        result = {
            "status": "passed" if exit_code == 0 else "failed",
            "startedAt": started_at,
            "finishedAt": datetime.now(UTC).isoformat(),
            "trigger": trigger,
            "acceptanceEligible": False,
            "certifying": False,
            "credentialsMounted": False,
            "containerSocketMounted": False,
            "completedSteps": completed,
            "exitCode": exit_code,
        }
        container = container.with_new_file(
            f"{reports}/cadence-route-results.json",
            contents=json.dumps(result, indent=2, sort_keys=True) + "\n",
        )
        return QualityResult(reports=container.directory(reports), exit_code=exit_code)

    @function
    async def route_measurement_evidence(
        self,
        source: Annotated[
            dagger.Directory,
            Doc("Exact candidate source tree for non-accepting representative measurements."),
        ],
        repository_bundle: Annotated[
            dagger.File,
            Doc("Git bundle containing the exact candidate commit and ancestry."),
        ],
        repetitions: Annotated[
            int,
            Doc("Repeated cold/warm pairs for every pure, integration, and durability route."),
        ] = 3,
    ) -> QualityResult:
        """Compare representative cohorts under serial and repository-default xdist."""

        if repetitions < 2:
            raise ValueError("repetitions must be at least 2 for medians and ranges")
        reports = "/reports"
        attempt_nonce = secrets.token_hex(16)
        container = await _candidate_container(
            source,
            repository_bundle,
            attempt_nonce=attempt_nonce,
            reports=reports,
        ).sync()
        exit_code = await container.exit_code()
        if exit_code == 0:
            container = await container.with_exec(
                [
                    "/opt/ar-venv/bin/python",
                    "-m",
                    "agents_remember_test_support.testing.route_measurement",
                    "--project-root",
                    "/workspace",
                    "--output",
                    f"{reports}/representative-route-measurement.json",
                    "--repetitions",
                    str(repetitions),
                ],
                expect=ReturnType.ANY,
            ).sync()
            exit_code = await container.exit_code()
        return QualityResult(reports=container.directory(reports), exit_code=exit_code)
