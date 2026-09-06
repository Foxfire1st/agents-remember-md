"""Repository-owned Gate 1-4 profiles are explicit, portable, and fail closed."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from agents_remember.certification.digests import content_digest
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
    JsonArtifactReferenceDefinition,
    JsonFieldPath,
    JsonReferenceActivationDefinition,
    RepositoryGateSelection,
)
from agents_remember.errors import CertificationProfileError
from agents_remember.models.certification.base import RailIdentity
from agents_remember_test_support.code_quality import profile_rails, profile_selection
from agents_remember_test_support.testing.dagger_admission import require_dagger_admission
from repository_profile_test_support import (
    AGENTS_REMEMBER_PROFILE_REFERENCE,
    NODE_FIXTURE,
    REPOSITORY_ROOT,
    RUST_FIXTURE,
    FixtureRepository,
    fixture_profile,
)
from source_selection_test_support import (
    ambient_selection_fixture,
    source_selection_fixture,
    write_ambient_selection,
)

_CANDIDATE = CandidateIdentity(kind="content-digest", value="c" * 64)
_AGENTS_REMEMBER_RAILS = {
    1: {
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
    },
    2: {"dashboard-suite", "python-suite"},
    3: {"dashboard-diff-coverage", "python-crap", "python-diff-coverage"},
    4: {
        "ambient-role-chat-e2e",
        "codex-read-only-probe",
        "dashboard-browser-e2e",
        "provider-runtime-integration",
        "teardown-process-cleanliness",
    },
}


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


def test_local_and_closeout_selections_reference_one_rail_catalog() -> None:
    canonical = canonicalize_repository_profile(fixture_profile())
    local = compile_repository_profile_plan(
        canonical,
        selection_id="local-targeted",
        candidate_identity=_CANDIDATE,
    )
    closeout = compile_repository_profile_plan(
        canonical,
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )

    assert [rail.definitionDigest for gate in local.gates for rail in gate.rails] == [
        rail.definitionDigest for gate in closeout.gates for rail in gate.rails
    ]
    assert local.planDigest != closeout.planDigest


def test_agents_remember_profile_preserves_the_complete_approved_gate_inventory() -> None:
    admitted = load_repository_profile(
        "agents-remember",
        REPOSITORY_ROOT,
        AGENTS_REMEMBER_PROFILE_REFERENCE,
    )
    plan = compile_repository_profile_plan(
        admitted.canonical,
        selection_id="closeout-full",
        candidate_identity=CandidateIdentity(kind="git-tree", value="c" * 40),
        source_selection=source_selection_fixture("c" * 40),
    )

    observed = {gate.gate: {rail.identity.railId for rail in gate.rails} for gate in plan.gates}
    assert observed == _AGENTS_REMEMBER_RAILS
    assert set().union(*observed.values()) == {
        rail.identity.railId for rail in admitted.canonical.profile.rails
    }
    assert admitted.canonical.profile.selectors[0].configurationDigest == (
        profile_selection.ownership_configuration_digest()
    )


def test_agents_remember_profile_replaces_the_legacy_result_artifact_authority() -> None:
    legacy_authority = REPOSITORY_ROOT / Path(
        "mcp/src/agents_remember/worktrees/modules/quality/result_artifacts.py"
    )
    admitted = load_repository_profile(
        "agents-remember",
        REPOSITORY_ROOT,
        AGENTS_REMEMBER_PROFILE_REFERENCE,
    )
    published = {artifact.path for artifact in admitted.canonical.profile.publishedArtifacts}
    decoder = admitted.canonical.profile.resultDecoders[0]
    decoder_paths = {item.artifactPath for item in admitted.canonical.profile.resultDecoders}
    reference_paths = {item.fieldPath.segments for item in decoder.artifactReferences}

    assert not legacy_authority.exists()
    assert decoder_paths <= published
    assert published
    assert reference_paths == {
        ("causalFailureReport",),
        ("causalFailureSummary",),
        ("ambientRoleChatEvidence", "summary"),
        ("ambientRoleChatEvidence", "runs"),
    }
    assert decoder.referenceActivations[0].listFieldPath.segments == ("completedSteps",)
    assert decoder.referenceActivations[0].containsValue == "causal-preflight"
    assert decoder.referenceActivations[0].nullPolicy == "ignore-activation"
    nested_references = {
        item.fieldPath.segments: item
        for item in decoder.artifactReferences
        if len(item.fieldPath.segments) > 1
    }
    assert nested_references[("ambientRoleChatEvidence", "summary")].nullParentPolicy == (
        "ignore-reference"
    )
    assert nested_references[("ambientRoleChatEvidence", "runs")].nullParentPolicy == (
        "ignore-reference"
    )


def test_documented_example_names_the_repository_profile_authority() -> None:
    example_path = REPOSITORY_ROOT / Path("examples/mcp/settings.example.json")

    payload = json.loads(example_path.read_text(encoding="utf-8"))

    assert (
        payload["repositories"]["agents-remember"]["certificationProfile"]
        == AGENTS_REMEMBER_PROFILE_REFERENCE.as_posix()
    )


def test_profile_reordering_has_one_canonical_digest() -> None:
    profile = fixture_profile()
    reordered = profile.model_copy(
        update={
            "selections": tuple(reversed(profile.selections)),
            "rails": tuple(reversed(profile.rails)),
            "publishedArtifacts": tuple(reversed(profile.publishedArtifacts)),
        }
    )

    assert repository_profile_digest(reordered) == profile.profileDigest
    assert canonicalize_repository_profile(reordered) == canonicalize_repository_profile(profile)


def test_decoder_reference_policy_reordering_has_one_canonical_digest() -> None:
    admitted = load_repository_profile(
        "agents-remember",
        REPOSITORY_ROOT,
        AGENTS_REMEMBER_PROFILE_REFERENCE,
    )
    profile = admitted.canonical.profile
    decoder = profile.resultDecoders[0]
    activation = decoder.referenceActivations[0]
    reordered_decoder = decoder.model_copy(
        update={
            "artifactReferences": tuple(reversed(decoder.artifactReferences)),
            "referenceActivations": (
                activation.model_copy(
                    update={"referenceFieldPaths": tuple(reversed(activation.referenceFieldPaths))}
                ),
            ),
        }
    )
    reordered = profile.model_copy(update={"resultDecoders": (reordered_decoder,)})

    assert repository_profile_digest(reordered) == profile.profileDigest
    assert canonicalize_repository_profile(reordered) == admitted.canonical


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


@pytest.mark.parametrize(
    ("semantic_input", "expected_changes"),
    (
        ("selector", (False, True, False, False)),
        ("decoder", (True, True, True, True)),
        ("executor", (True, True, True, True)),
        ("gate-four-publication", (False, False, False, True)),
    ),
)
def test_semantic_inputs_invalidate_only_their_exact_gate_closure(
    semantic_input: str,
    expected_changes: tuple[bool, bool, bool, bool],
) -> None:
    original = fixture_profile()
    if semantic_input == "selector":
        selector = original.selectors[0]
        changed = original.model_copy(
            update={"selectors": (selector.model_copy(update={"configurationDigest": "b" * 64}),)}
        )
    elif semantic_input == "decoder":
        decoder = original.resultDecoders[0]
        changed = original.model_copy(
            update={"resultDecoders": (decoder.model_copy(update={"passedValue": "accepted"}),)}
        )
    elif semantic_input == "executor":
        executor = original.executorAdapters[0]
        changed = original.model_copy(
            update={"executorAdapters": (executor.model_copy(update={"runtimeDigest": "b" * 64}),)}
        )
    else:
        gate_four = next(
            item
            for item in original.publishedArtifacts
            if item.path == NODE_FIXTURE.e2e_publication
        )
        changed = original.model_copy(
            update={
                "publishedArtifacts": tuple(
                    item.model_copy(update={"maxBytes": item.maxBytes + 1})
                    if item.path == gate_four.path
                    else item
                    for item in original.publishedArtifacts
                )
            }
        )
    changed = _redigest(changed)
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

    assert (
        tuple(
            before.planDigest != after.planDigest
            for before, after in zip(original_plan.gates, changed_plan.gates, strict=True)
        )
        == expected_changes
    )


def test_gate_plan_exposes_exact_content_addressed_semantic_closure() -> None:
    plan = compile_repository_profile_plan(
        canonicalize_repository_profile(fixture_profile()),
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )

    assert {(item.inputKind, item.inputId) for item in plan.gates[3].semanticInputs} >= {
        ("rail-execution", "clean-room-e2e@1.0.0"),
        ("rail-runtime", "clean-room-e2e@1.0.0"),
        ("executor-adapter", "certifying-dagger"),
        ("result-decoder", "terminal-result"),
        ("publication-policy", NODE_FIXTURE.e2e_publication),
    }
    assert all(
        item.contentDigest == content_digest(json.loads(item.canonicalJson))
        for item in plan.gates[3].semanticInputs
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("prerequisites", "complete earlier prefix"),
        ("not-applicable-payload", "carry only its typed reason"),
        ("applicable-reason", "requires rails and no reason"),
        ("waves", "earliest topological partition"),
        ("semantic-gate", "input not consumed by this gate"),
        ("duplicate-semantic-input", "unique and canonical"),
        ("digest", "digest does not match"),
    ),
)
def test_gate_plan_refuses_each_noncanonical_contract_branch(
    mutation: str,
    expected: str,
) -> None:
    plan = compile_repository_profile_plan(
        canonicalize_repository_profile(fixture_profile()),
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )
    gate = plan.gates[1]
    payload = gate.model_dump(mode="json")

    if mutation == "prerequisites":
        payload["gatePrerequisites"] = []
    elif mutation == "not-applicable-payload":
        payload["applicability"] = "not-applicable"
        payload["reason"] = "typed reason with forbidden applicable payload"
    elif mutation == "applicable-reason":
        payload["reason"] = "applicable gates cannot carry a not-applicable reason"
    elif mutation == "waves":
        payload["waves"] = []
    elif mutation == "semantic-gate":
        payload["semanticInputs"][0]["consumingGates"] = [1]
    elif mutation == "duplicate-semantic-input":
        payload["semanticInputs"] = [
            payload["semanticInputs"][0],
            payload["semanticInputs"][0],
        ]
    else:
        payload["planDigest"] = "0" * 64

    with pytest.raises(ValueError, match=expected):
        type(gate).model_validate(payload)


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


def test_selector_result_digest_binds_candidate_population_reasons_and_outputs() -> None:
    reason = RepositorySelectionReason(
        input="src/lib.rs",
        kind="declared-consumer",
        effect="select",
        outputArtifact="selected-tests",
        outputValue="unit",
        detail="fixture-owned-test",
    )
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
            dependency_reasons=(reason,),
            unresolved_inputs=(),
            outputs={"selected-tests": ("unit",)},
        )
    )
    payload = result.model_dump(mode="json")

    assert RepositorySelectionResult.model_validate(payload) == result
    payload["candidateIdentity"]["value"] = "c" * 40
    with pytest.raises(ValueError, match="digest does not match"):
        RepositorySelectionResult.model_validate(payload)


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


def test_profile_digest_binds_declared_external_selector_inputs() -> None:
    profile = fixture_profile()
    selector = profile.selectors[0].model_copy(
        update={"externalInputs": ("external://ci-owned-test-catalog",)}
    )
    changed = profile.model_copy(update={"selectors": (selector,)})

    assert repository_profile_digest(changed) != profile.profileDigest


@pytest.mark.parametrize("fixture", (NODE_FIXTURE, RUST_FIXTURE))
def test_non_python_selector_fixture_emits_the_canonical_generic_result(
    tmp_path: Path,
    fixture: FixtureRepository,
) -> None:
    script = fixture.source_root / "scripts/select-tests.sh"
    configuration_digest = hashlib.sha256(script.read_bytes()).hexdigest()
    output = tmp_path / "selection.json"
    completed = subprocess.run(
        (
            "sh",
            script.as_posix(),
            output.as_posix(),
            "targeted",
            "base-commit",
            "git-tree",
            "d" * 40,
            "repository-test-selector",
            "2.0.0",
            configuration_digest,
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = RepositorySelectionResult.model_validate_json(output.read_text(encoding="utf-8"))
    assert result.selectorId == "repository-test-selector"
    assert result.configurationDigest == configuration_digest
    assert result.candidateIdentity == CandidateIdentity(kind="git-tree", value="d" * 40)
    assert result.population == "targeted"
    assert result.complete
    assert result.output_values()["selected-tests"]


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


def test_framework_profile_package_contains_no_repository_specific_plan() -> None:
    source_root = Path(__file__).parents[1] / "src" / "agents_remember"
    profile_package = source_root / "certification" / "repository_profiles"
    framework_sources = (
        *profile_package.glob("*.py"),
        source_root / "worktrees" / "modules" / "quality" / "gate.py",
        source_root / "worktrees" / "modules" / "quality" / "clean_executor.py",
        source_root / "worktrees" / "modules" / "quality" / "published_manifest.py",
        source_root / "worktrees" / "integration" / "integration_quality.py",
    )
    source = "\n".join(path.read_text(encoding="utf-8").lower() for path in framework_sources)

    for forbidden in ("agents-remember", "python", "pytest", "dashboard", "codex"):
        assert forbidden not in source


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


@pytest.mark.parametrize(
    ("rail_index", "execution_update", "expected"),
    (
        (0, {"resultDecoderId": "missing-decoder"}, "result-decoder-unknown"),
        (1, {"scopeProviderId": "missing-selector"}, "scope-provider-unknown"),
        (1, {"scopeProviderId": None}, "test-selection-authority-missing"),
        (0, {"successExitCodes": (256,)}, "rail-success-exit-code-invalid"),
        (0, {"skippedExitCodes": (2,)}, "rail-skipped-exit-code-not-successful"),
    ),
)
def test_invalid_rail_runtime_contracts_refuse_before_plan_compilation(
    rail_index: int,
    execution_update: dict[str, object],
    expected: str,
) -> None:
    profile = fixture_profile()
    rails = list(profile.rails)
    rail = rails[rail_index]
    rails[rail_index] = rail.model_copy(
        update={"execution": rail.execution.model_copy(update=execution_update)}
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


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("artifact-collision", "published-artifact-path-collision"),
        ("reserved-artifact", "published-artifact-reserved"),
        ("executor-host-program", "executor-host-program-invalid"),
        ("executor-arguments", "executor-argument-ambiguous"),
        ("decoder-fields", "decoder-field-ambiguous"),
        ("decoder-publication-gates", "decoder-artifact-gate-scope-mismatch"),
        ("decoder-activation", "decoder-activation-reference-unknown"),
    ),
)
def test_profile_publication_and_adapter_ambiguity_refuse_at_admission(
    mutation: str,
    expected: str,
) -> None:
    profile = fixture_profile()
    if mutation == "artifact-collision":
        artifact = profile.publishedArtifacts[0]
        profile = profile.model_copy(
            update={
                "publishedArtifacts": (
                    artifact,
                    artifact.model_copy(update={"path": f"{artifact.path}/detail.json"}),
                )
            }
        )
    elif mutation == "reserved-artifact":
        profile = profile.model_copy(
            update={
                "publishedArtifacts": (
                    profile.publishedArtifacts[0].model_copy(
                        update={"path": ".quality-report-generations/result.json"}
                    ),
                )
            }
        )
    elif mutation == "executor-host-program":
        executor = profile.executorAdapters[0]
        profile = profile.model_copy(
            update={
                "executorAdapters": (
                    executor.model_copy(update={"executable": "./candidate-host-command"}),
                )
            }
        )
    elif mutation == "executor-arguments":
        executor = profile.executorAdapters[0]
        profile = profile.model_copy(
            update={
                "executorAdapters": (
                    executor.model_copy(
                        update={"retainedReportsArgument": executor.sourceArgument}
                    ),
                )
            }
        )
    elif mutation == "decoder-fields":
        decoder = profile.resultDecoders[0]
        profile = profile.model_copy(
            update={
                "resultDecoders": (
                    decoder.model_copy(update={"exitCodeField": decoder.statusField}),
                )
            }
        )
    elif mutation == "decoder-publication-gates":
        profile = profile.model_copy(
            update={
                "publishedArtifacts": tuple(
                    item.model_copy(update={"publisherGates": (4,)})
                    if item.path == "result.json"
                    else item
                    for item in profile.publishedArtifacts
                )
            }
        )
    else:
        decoder = profile.resultDecoders[0]
        unknown = JsonFieldPath(segments=("missingArtifactReference",))
        profile = profile.model_copy(
            update={
                "resultDecoders": (
                    decoder.model_copy(
                        update={
                            "artifactReferences": (
                                JsonArtifactReferenceDefinition(
                                    fieldPath=JsonFieldPath(segments=("artifact",)),
                                    cardinality="single",
                                ),
                            ),
                            "referenceActivations": (
                                JsonReferenceActivationDefinition(
                                    listFieldPath=JsonFieldPath(segments=("steps",)),
                                    containsValue="suite",
                                    referenceFieldPaths=(unknown,),
                                ),
                            ),
                        }
                    ),
                )
            }
        )
    canonical = canonicalize_repository_profile(_redigest(profile))

    report = validate_repository_profile(canonical)

    assert expected in {finding.code for finding in report.findings}


@pytest.mark.parametrize("drift", [None, "missing", "extra", "duplicate"])
def test_full_python_rail_uses_canonical_selector_order_and_still_refuses_scope_drift(
    tmp_path: Path, drift: str | None
) -> None:
    """Actual full selector and executable configuration agree before any pytest launch."""
    diff_base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT, text=True
    ).strip()
    selection = profile_selection.selection_result(
        REPOSITORY_ROOT, mode="full", diff_base=diff_base
    )
    scope_path = tmp_path / "scope.json"
    scope_path.write_text(selection.model_dump_json(), encoding="utf-8")
    args = argparse.Namespace(
        project_root=REPOSITORY_ROOT,
        mode="full",
        diff_base=diff_base,
        scope=scope_path,
        reports=tmp_path / "reports",
        memory_cap_bytes=0,
    )
    config = profile_rails._profile_config(args, require_dagger_admission())
    assert config.selection_digest == selection.selectionDigest
    paths = config.scope.size_paths
    assert sorted(path.as_posix() for path in paths) == list(
        selection.output_values()["size-paths"]
    )
    if drift is None:
        return
    altered = (
        paths[1:]
        if drift == "missing"
        else [*paths, Path("unselected-source.py")]
        if drift == "extra"
        else [*paths, paths[0]]
    )
    changed = replace(config, scope=replace(config.scope, size_paths=altered))
    with pytest.raises(profile_rails.quality_scope.ScopeError, match="size-paths differs"):
        profile_rails._require_exact_scope(args, changed)
