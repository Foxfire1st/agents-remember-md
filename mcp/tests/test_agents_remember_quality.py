from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles import (
    compile_repository_profile_plan,
    load_repository_profile,
)
from agents_remember_test_support.testing.dagger_admission import (
    DAGGER_TEST_ATTESTATION_ENV,
    dagger_admission_refusal,
)
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    FakeContainer,
    FakeDag,
    FakeFile,
    FakeSource,
)
from source_selection_test_support import source_selection_fixture

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


def execution_manifest(
    *,
    mode: str,
    candidate: str = "c" * 40,
    diff_base: str = "b" * 40,
    changed_paths: tuple[str, ...] = ("scripts/e2e_harness/run.py",),
) -> FakeFile:
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
        source_selection=source_selection_fixture(
            candidate, base_commit=diff_base, changed_paths=changed_paths
        ),
    )
    profile = admitted.canonical.profile
    return FakeFile(
        json.dumps(
            {
                "schemaVersion": "repository-certification-admission/v1",
                "diffBase": diff_base,
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


def test_dagger_quality_red_gate_one_still_terminalizes_every_gate_one_sibling() -> None:
    """Exhaustive within Gate 1: a red rail never suppresses its same-gate siblings."""
    module = load_dagger_module()
    fake_dag = FakeDag([0, 0, 7])

    with patch.object(module, "dag", fake_dag):
        result = asyncio.run(
            module.AgentsRememberQuality().quality(
                FakeSource(),
                object(),
                execution_manifest(
                    mode="full", diff_base="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                ),
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
