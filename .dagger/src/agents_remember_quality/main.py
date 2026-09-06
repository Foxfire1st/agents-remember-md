"""One pinned Ubuntu quality pipeline shared by local worktrees and GitHub."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Annotated

import dagger
from dagger import Doc, ReturnType, dag, field, function, object_type

from agents_remember_quality import profile_results
from agents_remember_quality.engine_helpers import (
    _admitted_profile_plan,
    _require_adapter_runtime,
)
from agents_remember_quality.profile_execution import _ProfileRunInputs, _run_profile_acceptance
from agents_remember_quality.profile_publication import (
    export_profile_reports,
    prepare_profile_reports,
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
                "-euc",
                f". {RUNTIME_CONTRACT}; "
                f"exec bash {RUNTIME_INSTALLER} "
                f'--prefix "{RUNTIME_ROOT}/cpython-$AR_PYTHON_VERSION" '
                f"--cache-root {PYTHON_BUILD_CACHE_ROOT} "
                f"--tooling-root {PYTHON_BUILD_CACHE_ROOT}/tooling",
            ]
        )
        .with_exec(
            [
                "bash",
                "-euc",
                f". {RUNTIME_CONTRACT}; mkdir -p {RUNTIME_ROOT}; "
                f"cd {RUNTIME_ROOT}; "
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
        memory_cap_bytes: Annotated[
            int,
            Doc("Optional container memory cap in bytes; zero leaves memory host-managed."),
        ] = 0,
        *,
        retained_reports: Annotated[
            dagger.Directory | None,
            Doc("Exact bounded predecessor report snapshot selected by the frozen recovery."),
        ] = None,
    ) -> QualityResult:
        """Run only the exact admitted repository profile inside clean Ubuntu."""
        started_at = datetime.now(UTC).isoformat()
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
                diff_base=plan.diff_base,
                reports=reports,
                memory_cap_bytes=memory_cap_bytes,
                retained_reports=retained_reports,
            ),
        )
        output = await prepare_profile_reports(progress, plan, reports=reports)
        result = _quality_result_payload(
            progress,
            started_at=started_at,
            mode=mode,
            attempt_nonce=attempt_nonce,
            gates=tuple(progress.gate_catalog),
        )
        exported = export_profile_reports(output, progress, plan, result, reports=reports)
        return QualityResult(
            reports=exported,
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
        memory_cap_bytes: Annotated[
            int,
            Doc("Optional profile command memory input; zero leaves it image-managed."),
        ] = 0,
        *,
        retained_reports: Annotated[
            dagger.Directory | None,
            Doc("Exact bounded predecessor report snapshot selected by the frozen recovery."),
        ] = None,
    ) -> QualityResult:
        """Execute a non-Agents-Remember profile through the same frozen-plan interpreter."""

        started_at = datetime.now(UTC).isoformat()
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
                diff_base=plan.diff_base,
                reports=reports,
                memory_cap_bytes=memory_cap_bytes,
                retained_reports=retained_reports,
            ),
        )
        output = await prepare_profile_reports(progress, plan, reports=reports)
        result = _profile_result_payload(
            progress,
            started_at=started_at,
            mode=mode,
            attempt_nonce=attempt_nonce,
            gates=tuple(progress.gate_catalog),
        )
        exported = export_profile_reports(output, progress, plan, result, reports=reports)
        return QualityResult(
            reports=exported,
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
