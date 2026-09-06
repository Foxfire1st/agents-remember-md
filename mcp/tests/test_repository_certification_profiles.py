"""Repository-owned Gate 1-4 profiles are explicit, portable, and fail closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles import (
    AdmittedRepositoryProfile,
    DaggerModuleExecutorAdapter,
    JsonExitStatusDecoder,
    RepositoryExecutionRequest,
    RepositorySelectionDraft,
    RepositorySelectionReason,
    RepositorySelectionResult,
    admit_repository_profile_execution,
    admit_repository_profile_plan,
    build_repository_selection_result,
    canonicalize_repository_profile,
    compile_repository_profile_plan,
    load_repository_profile,
    repository_profile_digest,
    repository_selection_result_digest,
    validate_repository_profile,
)
from agents_remember.certification.repository_profiles.models import (
    RepositoryGateSelection,
)
from agents_remember.errors import CertificationProfileError
from agents_remember.models.certification.base import RailIdentity
from agents_remember_test_support.code_quality import profile_rails, profile_selection
from repository_profile_test_support import (
    NODE_FIXTURE,
    RUST_FIXTURE,
    FixtureRepository,
    fixture_profile,
)
from source_selection_test_support import (
    ambient_selection_fixture,
    write_ambient_selection,
)

_CANDIDATE = CandidateIdentity(kind="content-digest", value="c" * 64)


def _write_profile(root: Path, profile=None) -> Path:
    selected = profile or fixture_profile()
    path = root / "config" / "certification.json"
    path.parent.mkdir(parents=True)
    path.write_text(selected.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def _finding_codes(error: CertificationProfileError) -> set[str]:
    return {str(item["code"]) for item in error.findings}


def _redigest(profile):
    provisional = profile.model_copy(update={"profileDigest": "0" * 64})
    return provisional.model_copy(update={"profileDigest": repository_profile_digest(provisional)})


@pytest.mark.parametrize("fixture", (NODE_FIXTURE, RUST_FIXTURE))
def test_two_language_distinct_repositories_compile_the_same_four_gate_protocol(
    tmp_path: Path,
    fixture,
) -> None:
    root = tmp_path / fixture.repository_id
    root.mkdir()
    profile = fixture_profile(fixture)
    _write_profile(root, profile)

    admitted = load_repository_profile(
        fixture.repository_id,
        root,
        Path("config/certification.json"),
    )
    plan = compile_repository_profile_plan(
        admitted.canonical,
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )

    assert tuple(gate.gate for gate in plan.gates) == (1, 2, 3, 4)
    assert plan.gates[1].rails[0].outputArtifacts
    assert plan.gates[2].rails[0].requiredArtifacts
    assert profile.rails[-1].execution.command == fixture.e2e_command
    assert profile.rails[-1].execution.cleanRoom is True


def test_profile_edit_changes_profile_and_plan_identity() -> None:
    original = fixture_profile()
    rail = original.rails[0]
    changed_execution = rail.execution.model_copy(
        update={"command": (*rail.execution.command, "--strict")}
    )
    changed = _redigest(
        original.model_copy(
            update={
                "rails": (
                    rail.model_copy(update={"execution": changed_execution}),
                    *original.rails[1:],
                )
            }
        )
    )
    original_plan = compile_repository_profile_plan(
        canonicalize_repository_profile(original),
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )
    changed_canonical = canonicalize_repository_profile(changed)
    changed_plan = compile_repository_profile_plan(
        changed_canonical,
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )

    assert changed.profileDigest != original.profileDigest
    assert changed_plan.gates[0].planDigest != original_plan.gates[0].planDigest
    with pytest.raises(CertificationProfileError) as raised:
        admit_repository_profile_plan(
            changed_canonical,
            original_plan,
            selection_id="closeout-targeted",
            candidate_identity=_CANDIDATE,
        )
    assert _finding_codes(raised.value) == {"repository-profile-plan-not-authorized"}


def test_later_gate_edit_retains_earlier_gate_plan_identities() -> None:
    original = fixture_profile()
    gate_four_rail = original.rails[3]
    changed_execution = gate_four_rail.execution.model_copy(
        update={"command": (*gate_four_rail.execution.command, "--strict")}
    )
    changed = _redigest(
        original.model_copy(
            update={
                "rails": (
                    *original.rails[:3],
                    gate_four_rail.model_copy(update={"execution": changed_execution}),
                )
            }
        )
    )
    original_plan = compile_repository_profile_plan(
        canonicalize_repository_profile(original),
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )
    changed_plan = compile_repository_profile_plan(
        canonicalize_repository_profile(changed),
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )

    assert changed_plan.profileDigest != original_plan.profileDigest
    assert changed_plan.planDigest != original_plan.planDigest
    assert [gate.planDigest for gate in changed_plan.gates[:3]] == [
        gate.planDigest for gate in original_plan.gates[:3]
    ]
    assert changed_plan.gates[3].planDigest != original_plan.gates[3].planDigest


def test_explicit_not_applicable_gate_remains_typed_and_visible(tmp_path: Path) -> None:
    profile = fixture_profile()
    selection = profile.selections[0]
    gate_four = RepositoryGateSelection(
        gate=4,
        status="not-applicable",
        selectionIdentity="local-targeted:gate-4",
        reason="local pre-commit does not enter the integration environment",
    )
    adjusted_selection = selection.model_copy(update={"gates": (*selection.gates[:3], gate_four)})
    adjusted = _redigest(
        profile.model_copy(update={"selections": (adjusted_selection, *profile.selections[1:])})
    )
    canonical = canonicalize_repository_profile(adjusted)

    plan = compile_repository_profile_plan(
        canonical,
        selection_id="local-targeted",
        candidate_identity=_CANDIDATE,
    )

    assert plan.gates[3].applicability == "not-applicable"
    assert plan.gates[3].rails == ()
    assert plan.gates[3].reason is not None
    execution = admit_repository_profile_execution(
        AdmittedRepositoryProfile(
            repository_id=profile.repositoryId,
            repository_root=tmp_path,
            source_path=tmp_path / "profile.json",
            source_sha256="0" * 64,
            canonical=canonical,
        ),
        purpose="local-precommit",
        mode="targeted",
        candidate_identity=_CANDIDATE,
    )
    assert NODE_FIXTURE.e2e_publication not in {
        artifact.path for artifact in execution.published_artifacts
    }
    assert "result.json" in {artifact.path for artifact in execution.published_artifacts}


@pytest.mark.parametrize("fixture", (NODE_FIXTURE, RUST_FIXTURE))
def test_distinct_fixture_adapters_and_decoders_complete_the_declared_protocol(
    tmp_path: Path,
    fixture: FixtureRepository,
) -> None:
    profile = fixture_profile(fixture)
    adapter = DaggerModuleExecutorAdapter()
    request = RepositoryExecutionRequest(
        candidate_source=tmp_path / "source",
        repository_bundle=tmp_path / "candidate.bundle",
        execution_manifest=tmp_path / "manifest.json",
        mode="full",
        export_root=tmp_path / "reports",
        memory_cap_bytes=4096,
    )

    command = adapter.command(profile.executorAdapters[0], request)

    assert command[3] == fixture.dagger_function
    assert all(not part.startswith("--mode=") for part in command)
    assert f"--execution-manifest={request.execution_manifest.as_posix()}" in command
    assert command[-3:] == ("reports", "export", f"--path={request.export_root.as_posix()}")
    report = request.export_root / "result.json"
    report.parent.mkdir()
    report.write_text(json.dumps({"status": "passed", "exit-code": 0}), encoding="utf-8")
    decoded = JsonExitStatusDecoder().decode(
        profile.resultDecoders[0],
        request.export_root,
        {"result.json"},
    )
    assert decoded.exit_code == 0
    assert decoded.artifact_path == report


def test_result_decoder_refuses_a_symlink_escape(tmp_path: Path) -> None:
    profile = fixture_profile()
    export = tmp_path / "reports"
    export.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"status": "passed", "exit-code": 0}), encoding="utf-8")
    (export / "result.json").symlink_to(outside)

    with pytest.raises(RuntimeError, match="safe authoritative result"):
        JsonExitStatusDecoder().decode(profile.resultDecoders[0], export, {"result.json"})


def test_repository_profile_selector_refuses_an_unadmitted_absolute_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AR_CERTIFICATION_SELECTOR_ROOT", raising=False)

    with pytest.raises(RuntimeError, match="sandbox scratch root"):
        profile_selection._confined_output(tmp_path, tmp_path / "outside.json")


def test_selector_result_refuses_unreasoned_output_and_targeted_full_expansion() -> None:
    result = build_repository_selection_result(
        RepositorySelectionDraft(
            selector_id="repository-test-selector",
            selector_version="2.0.0",
            configuration_digest="a" * 64,
            candidate_identity=CandidateIdentity(kind="git-tree", value="b" * 40),
            mode="targeted",
            base_revision="base",
            population="targeted",
            complete=True,
            global_invalidators=(),
            dependency_reasons=(
                RepositorySelectionReason(
                    input="src/lib.rs",
                    kind="declared-consumer",
                    effect="select",
                    outputArtifact="selected-tests",
                    outputValue="unit",
                    detail="fixture-owned-test",
                ),
            ),
            unresolved_inputs=(),
            outputs={"selected-tests": ("unit",)},
        )
    )
    payload = result.model_dump(mode="json")
    payload["dependencyReasons"] = []
    payload["selectionDigest"] = repository_selection_result_digest(payload)
    with pytest.raises(ValueError, match="exact dependency reason"):
        RepositorySelectionResult.model_validate(payload)

    payload = result.model_dump(mode="json")
    payload["population"] = "full"
    payload["selectionDigest"] = repository_selection_result_digest(payload)
    with pytest.raises(ValueError, match="cannot broaden itself to full"):
        RepositorySelectionResult.model_validate(payload)


def test_repository_profile_teardown_adapter_requires_a_passing_cleanup_checkpoint(
    tmp_path: Path,
) -> None:
    selection = write_ambient_selection(tmp_path)
    decision = ambient_selection_fixture()
    summary = tmp_path / "summary.json"
    common = {
        "mode": decision.mode,
        "candidate": {"tree": decision.sourceSelection.candidateTree},
        "diffBase": decision.sourceSelection.baseCommit,
        "selectedPaths": list(decision.selectedPaths),
    }
    summary.write_text(
        json.dumps(
            {
                **common,
                "schema": "ar-ambient-role-chat-e2e-summary/v1",
                "status": "passed",
                "runs": [
                    {"run": index, "status": "passed", "report": f"run-{index}.json"}
                    for index in (1, 2)
                ],
            }
        )
    )
    for index in (1, 2):
        (tmp_path / f"run-{index}.json").write_text(
            json.dumps({**common, "run": index, "retry": False, "checkpoints": []})
        )
    with pytest.raises(RuntimeError, match="teardown checkpoint"):
        profile_rails._verify_teardown(summary, tmp_path / "proof.json", selection)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("later-prerequisite", "later-gate-prerequisite"),
        ("undeclared-artifact", "required-artifact-undeclared"),
        ("wrong-gate", "wrong-gate-classification"),
        ("runtime", "runtime-inputs-incomplete"),
        ("duplicate-rail-id", "duplicate-rail-id"),
        ("cycle", "rail-dependency-cycle"),
    ),
)
def test_invalid_graphs_refuse_before_plan_compilation(mutation: str, expected: str) -> None:
    profile = fixture_profile()
    rails = list(profile.rails)
    if mutation == "later-prerequisite":
        rails[0] = rails[0].model_copy(
            update={"prerequisites": (RailIdentity(railId="ordinary-suite", version="1.0.0"),)}
        )
    elif mutation == "undeclared-artifact":
        rails[2] = rails[2].model_copy(update={"requiredArtifacts": ("missing",)})
    elif mutation == "wrong-gate":
        rails[0] = rails[0].model_copy(update={"railClass": "integration-test"})
    elif mutation == "runtime":
        rails[0] = rails[0].model_copy(
            update={
                "runtimeInputs": rails[0].runtimeInputs.model_copy(
                    update={"secretPolicyDigest": None}
                )
            }
        )
    elif mutation == "duplicate-rail-id":
        rails[1] = rails[1].model_copy(
            update={
                "identity": RailIdentity(
                    railId=rails[0].identity.railId,
                    version="2.0.0",
                )
            }
        )
    else:
        rails[0] = rails[0].model_copy(
            update={"prerequisites": (RailIdentity(railId="ordinary-suite", version="1.0.0"),)}
        )
        rails[1] = rails[1].model_copy(
            update={"prerequisites": (RailIdentity(railId="static-quality", version="1.0.0"),)}
        )
    invalid = _redigest(profile.model_copy(update={"rails": tuple(rails)}))
    canonical = canonicalize_repository_profile(invalid)
    report = validate_repository_profile(canonical)

    assert expected in {finding.code for finding in report.findings}
    with pytest.raises(CertificationProfileError) as raised:
        compile_repository_profile_plan(
            canonical,
            selection_id="closeout-targeted",
            candidate_identity=_CANDIDATE,
        )
    assert expected in _finding_codes(raised.value)
