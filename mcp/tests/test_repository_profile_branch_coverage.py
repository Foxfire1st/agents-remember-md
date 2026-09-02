"""Focused negative-path proofs for repository certification profiles."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles import (
    DaggerModuleExecutorAdapter,
    RepositoryExecutionRequest,
    admit_repository_profile_plan,
    canonicalize_repository_profile,
    compile_repository_profile_plan,
    repository_profile_digest,
    resolve_repository_profile_path,
    resolve_repository_profile_selection,
    validate_repository_profile,
)
from agents_remember.certification.repository_profiles import authority as authority_mod
from agents_remember.certification.repository_profiles.adapters import (
    _confined_regular_artifact,
    _reference_values,
    _validate_reference_activation,
)
from agents_remember.certification.repository_profiles.authority import (
    _read_profile_bytes,
    _validation_finding,
    load_repository_profile,
)
from agents_remember.certification.repository_profiles.models import (
    CanonicalRepositoryCertificationProfile,
    JsonArtifactReferenceDefinition,
    JsonFieldPath,
    JsonReferenceActivationDefinition,
    RepositoryGateSelection,
    RepositoryProfilePlan,
    RepositorySemanticInputNode,
    _require_safe_relative_path,
    repository_gate_plan_digest,
    require_relative_repository_file_path,
)
from agents_remember.certification.repository_profiles.planning import (
    _compile_semantic_inputs,
    _selector_consuming_gates,
)
from agents_remember.errors import CertificationProfileError
from repository_profile_test_support import fixture_profile

_CANDIDATE = CandidateIdentity(kind="git-tree", value="c" * 40)


def _unchecked(profile) -> CanonicalRepositoryCertificationProfile:
    return CanonicalRepositoryCertificationProfile.model_construct(
        profile=profile,
        profileDigest=profile.profileDigest,
    )


def _codes(profile) -> set[str]:
    return {finding.code for finding in validate_repository_profile(_unchecked(profile)).findings}


def _redigest(profile):
    provisional = profile.model_copy(update={"profileDigest": "0" * 64})
    return provisional.model_copy(update={"profileDigest": repository_profile_digest(provisional)})


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("duplicate-selector", "duplicate-selector-authority"),
        ("optional-decoder-artifact", "decoder-artifact-optional"),
        ("missing-decoder", "result-decoder-missing"),
        ("ambiguous-decoder-status", "decoder-status-ambiguous"),
        ("unreferenced-executor", "executor-unreferenced"),
        ("executor-gate-scope", "executor-gate-scope-mismatch"),
        ("noncanonical-gate-set", "semantic-input-gates-not-canonical"),
    ),
)
def test_profile_catalog_validation_reports_each_fail_closed_branch(
    case: str,
    expected: str,
) -> None:
    profile = fixture_profile()

    if case == "duplicate-selector":
        profile = profile.model_copy(update={"selectors": profile.selectors * 2})
    elif case == "optional-decoder-artifact":
        profile = profile.model_copy(
            update={
                "publishedArtifacts": tuple(
                    item.model_copy(update={"required": False})
                    if item.path == profile.resultDecoders[0].artifactPath
                    else item
                    for item in profile.publishedArtifacts
                )
            }
        )
    elif case == "missing-decoder":
        profile = profile.model_copy(update={"resultDecoders": ()})
    elif case == "ambiguous-decoder-status":
        decoder = profile.resultDecoders[0]
        profile = profile.model_copy(
            update={
                "resultDecoders": (decoder.model_copy(update={"failedValue": decoder.passedValue}),)
            }
        )
    elif case == "unreferenced-executor":
        executor = profile.executorAdapters[0]
        profile = profile.model_copy(
            update={
                "executorAdapters": (
                    executor,
                    executor.model_copy(update={"adapterId": "unused-dagger"}),
                )
            }
        )
    elif case == "executor-gate-scope":
        executor = profile.executorAdapters[0]
        profile = profile.model_copy(
            update={"executorAdapters": (executor.model_copy(update={"consumingGates": (1,)}),)}
        )
    else:
        executor = profile.executorAdapters[0]
        profile = profile.model_copy(
            update={"executorAdapters": (executor.model_copy(update={"consumingGates": (2, 1)}),)}
        )

    assert expected in _codes(profile)


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("missing-selection", "required-profile-selection-missing"),
        ("ambiguous-selection", "ambiguous-profile-selection"),
        ("unknown-executor", "executor-adapter-unknown"),
        ("unknown-decoder", "result-decoder-unknown"),
        ("reordered-gates", "profile-gates-incomplete"),
        ("wrong-gate-rail", "selected-rail-wrong-gate"),
    ),
)
def test_profile_selection_validation_reports_each_fail_closed_branch(
    case: str,
    expected: str,
) -> None:
    profile = fixture_profile()
    rails = list(profile.rails)
    selections = list(profile.selections)

    if case == "missing-selection":
        profile = profile.model_copy(update={"selections": tuple(selections[1:])})
    elif case == "ambiguous-selection":
        duplicate = selections[0].model_copy(update={"selectionId": "local-targeted-copy"})
        profile = profile.model_copy(update={"selections": (*selections, duplicate)})
    elif case == "unknown-executor":
        selections[0] = selections[0].model_copy(update={"executorAdapterId": "missing"})
        profile = profile.model_copy(update={"selections": tuple(selections)})
    elif case == "unknown-decoder":
        selections[0] = selections[0].model_copy(update={"resultDecoderId": "missing"})
        profile = profile.model_copy(update={"selections": tuple(selections)})
    elif case == "reordered-gates":
        selections[0] = selections[0].model_copy(
            update={"gates": tuple(reversed(selections[0].gates))}
        )
        profile = profile.model_copy(update={"selections": tuple(selections)})
    else:
        gate = selections[0].gates[0].model_copy(update={"railIds": (rails[1].identity,)})
        selections[0] = selections[0].model_copy(update={"gates": (gate, *selections[0].gates[1:])})
        profile = profile.model_copy(update={"selections": tuple(selections)})

    assert expected in _codes(profile)


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("late-artifact-producer", "artifact-producer-not-earlier"),
        ("missing-suite-artifact", "suite-artifact-missing"),
        ("incomplete-clean-room", "integration-clean-room-incomplete"),
        ("clean-room-wrong-gate", "clean-room-wrong-gate"),
        ("unused-selector-output", "selector-result-path-unused"),
        ("malformed-placeholder", "command-placeholder-malformed"),
        ("embedded-list-placeholder", "command-list-placeholder-embedded"),
        ("ambiguous-artifact-producer", "artifact-producer-ambiguous"),
        ("missing-selection-prerequisite", "profile-prerequisite-missing"),
    ),
)
def test_profile_rail_validation_reports_each_fail_closed_branch(
    case: str,
    expected: str,
) -> None:
    profile = fixture_profile()
    rails = list(profile.rails)
    selections = list(profile.selections)

    if case in {"late-artifact-producer", "ambiguous-artifact-producer"}:
        artifact = rails[1].outputArtifacts[0]
        rails[2] = rails[2].model_copy(
            update={
                "requiredArtifacts": (artifact.artifactId,),
                "outputArtifacts": (artifact,),
            }
        )
        profile = profile.model_copy(update={"rails": tuple(rails)})
    elif case == "missing-suite-artifact":
        rails[1] = rails[1].model_copy(update={"outputArtifacts": ()})
        profile = profile.model_copy(update={"rails": tuple(rails)})
    elif case == "incomplete-clean-room":
        execution = rails[3].execution.model_copy(update={"cleanRoom": False})
        rails[3] = rails[3].model_copy(update={"execution": execution})
        profile = profile.model_copy(update={"rails": tuple(rails)})
    elif case == "clean-room-wrong-gate":
        execution = rails[0].execution.model_copy(
            update={"cleanRoom": True, "teardownPolicy": "always"}
        )
        rails[0] = rails[0].model_copy(update={"execution": execution})
        profile = profile.model_copy(update={"rails": tuple(rails)})
    elif case == "unused-selector-output":
        selector = profile.selectors[0].model_copy(update={"command": ("selector",)})
        profile = profile.model_copy(update={"selectors": (selector,)})
    elif case == "malformed-placeholder":
        execution = rails[0].execution.model_copy(update={"command": ("runner", "{reports")})
        rails[0] = rails[0].model_copy(update={"execution": execution})
        profile = profile.model_copy(update={"rails": tuple(rails)})
    elif case == "embedded-list-placeholder":
        execution = rails[1].execution.model_copy(
            update={"command": ("runner", "--tests={selected-tests}")}
        )
        rails[1] = rails[1].model_copy(update={"execution": execution})
        profile = profile.model_copy(update={"rails": tuple(rails)})
    else:
        first = selections[0]
        gate_one = first.gates[0].model_copy(
            update={
                "status": "not-applicable",
                "railIds": (),
                "population": None,
                "reason": "Gate 1 intentionally omitted for this invalid-profile proof",
            }
        )
        selections[0] = first.model_copy(update={"gates": (gate_one, *first.gates[1:])})
        profile = profile.model_copy(update={"selections": tuple(selections)})

    assert expected in _codes(profile)


def test_path_and_gate_selection_models_refuse_invalid_payloads() -> None:
    # Direct validators keep these low-level fail-closed paths independently falsifiable.
    with pytest.raises(ValueError, match="canonical repository-relative"):
        _require_safe_relative_path("\\unsafe")
    with pytest.raises(ValueError, match="must name"):
        require_relative_repository_file_path(".")
    with pytest.raises(ValueError, match="applicable gate selection"):
        RepositoryGateSelection(
            gate=1,
            status="applicable",
            selectionIdentity="invalid-applicable",
        )
    with pytest.raises(ValueError, match="not-applicable gate selection"):
        RepositoryGateSelection(
            gate=4,
            status="not-applicable",
            selectionIdentity="invalid-not-applicable",
        )


def test_canonical_profile_and_semantic_inputs_refuse_each_invalid_identity() -> None:
    profile = fixture_profile()
    with pytest.raises(ValueError, match="canonical repository profile digest"):
        CanonicalRepositoryCertificationProfile(profile=profile, profileDigest="f" * 64)

    base = {
        "inputKind": "rail-execution",
        "inputId": "input",
        "consumingGates": (1,),
        "contentDigest": "0" * 64,
        "canonicalJson": "{}",
    }
    cases = (
        ({"consumingGates": (2, 1)}, "unique and ordered"),
        ({"canonicalJson": "{]"}, "canonical JSON"),
        ({"canonicalJson": "[]"}, "JSON object"),
        ({"canonicalJson": "{ }"}, "not canonical JSON"),
        ({"contentDigest": "f" * 64}, "digest does not match"),
    )
    for update, expected in cases:
        with pytest.raises(ValueError, match=expected):
            RepositorySemanticInputNode(**{**base, **update})


def test_profile_plan_refuses_order_identity_and_digest_drift() -> None:
    plan = compile_repository_profile_plan(
        canonicalize_repository_profile(fixture_profile()),
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )
    reordered = plan.model_dump(mode="json")
    reordered["gates"] = list(reversed(reordered["gates"]))
    with pytest.raises(ValueError, match="exact ordered Gates 1-4"):
        RepositoryProfilePlan.model_validate(reordered)

    identity = plan.model_dump(mode="json")
    identity["gates"][0]["selectionId"] = "other-selection"
    identity["gates"][0]["planDigest"] = repository_gate_plan_digest(identity["gates"][0])
    with pytest.raises(ValueError, match="identity does not match"):
        RepositoryProfilePlan.model_validate(identity)

    digest = plan.model_dump(mode="json")
    digest["planDigest"] = "0" * 64
    with pytest.raises(ValueError, match="digest does not match"):
        RepositoryProfilePlan.model_validate(digest)


def test_planning_selection_and_semantic_input_failures_are_typed() -> None:
    canonical = canonicalize_repository_profile(fixture_profile())
    local = next(
        selection
        for selection in canonical.profile.selections
        if selection.purpose == "local-precommit" and selection.mode == "targeted"
    )
    with pytest.raises(CertificationProfileError, match="selection authority failed"):
        resolve_repository_profile_selection(
            _unchecked(
                canonical.profile.model_copy(
                    update={
                        "selections": tuple(
                            selection
                            for selection in canonical.profile.selections
                            if selection != local
                        )
                    }
                )
            ),
            purpose="local-precommit",
            mode="targeted",
        )
    ambiguous = local.model_copy(update={"selectionId": "local-targeted-copy"})
    with pytest.raises(CertificationProfileError, match="selection authority failed"):
        resolve_repository_profile_selection(
            _unchecked(
                canonical.profile.model_copy(
                    update={"selections": (*canonical.profile.selections, ambiguous)}
                )
            ),
            purpose="local-precommit",
            mode="targeted",
        )
    with pytest.raises(CertificationProfileError, match="selection failed"):
        compile_repository_profile_plan(
            canonical,
            selection_id="missing-selection",
            candidate_identity=_CANDIDATE,
        )

    plan = compile_repository_profile_plan(
        canonical,
        selection_id="closeout-targeted",
        candidate_identity=_CANDIDATE,
    )
    assert (
        admit_repository_profile_plan(
            canonical,
            plan,
            selection_id="closeout-targeted",
            candidate_identity=_CANDIDATE,
        )
        == plan
    )

    selection = canonical.profile.selections[0]
    gate_two = selection.gates[1]
    catalog = {rail.identity.key: rail for rail in canonical.profile.rails}
    selected = tuple(catalog[identity.key] for identity in gate_two.railIds)
    executor = canonical.profile.executorAdapters[0].model_copy(update={"consumingGates": (1,)})
    no_gate_two_executor = _unchecked(
        canonical.profile.model_copy(update={"executorAdapters": (executor,)})
    )
    nodes = _compile_semantic_inputs(
        no_gate_two_executor,
        selection,
        gate_two,
        selected,
        _selector_consuming_gates(no_gate_two_executor),
    )
    assert all(node.inputKind != "executor-adapter" for node in nodes)
    with pytest.raises(ValueError, match="duplicate identities"):
        _compile_semantic_inputs(
            canonical,
            selection,
            gate_two,
            (selected[0], selected[0]),
            _selector_consuming_gates(canonical),
        )


def test_executor_adapter_and_decoder_confinement_refuse_uncovered_edges(
    tmp_path: Path,
) -> None:
    profile = fixture_profile()
    request = RepositoryExecutionRequest(
        candidate_source=tmp_path / "source",
        repository_bundle=tmp_path / "candidate.bundle",
        execution_manifest=tmp_path / "manifest.json",
        mode="targeted",
        diff_base="base",
        export_root=tmp_path / "reports",
        memory_cap_bytes=-1,
    )
    with pytest.raises(ValueError, match="memory cap"):
        DaggerModuleExecutorAdapter().command(profile.executorAdapters[0], request)

    single = JsonArtifactReferenceDefinition(
        fieldPath=JsonFieldPath(segments=("artifact",)),
        cardinality="single",
    )
    with pytest.raises(RuntimeError, match="must be a string"):
        _reference_values(single, 1)

    activation = JsonReferenceActivationDefinition(
        listFieldPath=JsonFieldPath(segments=("steps",)),
        containsValue="suite",
        referenceFieldPaths=(single.fieldPath,),
    )
    _validate_reference_activation({"steps": []}, activation, {single.fieldPath.key: False})

    root_file = tmp_path / "root-file"
    root_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(RuntimeError, match="safe authoritative"):
        _confined_regular_artifact(root_file, "result.json")

    export = tmp_path / "export"
    export.mkdir()
    (export / "nested").write_text("not a directory", encoding="utf-8")
    with pytest.raises(RuntimeError, match="safe authoritative"):
        _confined_regular_artifact(export, "nested/result.json")

    regular = export / "result.json"
    regular.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    resolved_root = export.resolve()
    with (
        mock.patch.object(Path, "resolve", autospec=True, side_effect=(resolved_root, outside)),
        pytest.raises(RuntimeError, match="escapes its export root"),
    ):
        _confined_regular_artifact(export, "result.json")


def test_profile_authority_refuses_schema_semantics_and_filesystem_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "fixture-node"
    root.mkdir()
    source = root / "profile.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(CertificationProfileError) as schema:
        load_repository_profile("fixture-node", root, "profile.json")
    assert {item["code"] for item in schema.value.findings} == {"profile-schema-invalid"}

    invalid = fixture_profile()
    executor = invalid.executorAdapters[0].model_copy(update={"executable": "host"})
    invalid = _redigest(invalid.model_copy(update={"executorAdapters": (executor,)}))
    source.write_text(json.dumps(invalid.model_dump(mode="json")), encoding="utf-8")
    with pytest.raises(CertificationProfileError, match="profile is invalid"):
        load_repository_profile("fixture-node", root, "profile.json")

    with pytest.raises(CertificationProfileError) as unavailable:
        resolve_repository_profile_path(tmp_path / "missing-root", "profile.json")
    assert {item["code"] for item in unavailable.value.findings} == {"profile-root-unavailable"}

    directory_root = tmp_path / "directory-root"
    directory_root.mkdir()
    (directory_root / "profile.json").mkdir()
    with pytest.raises(CertificationProfileError) as directory:
        resolve_repository_profile_path(directory_root, "profile.json")
    assert {item["code"] for item in directory.value.findings} == {"profile-path-escape"}

    with (
        mock.patch.object(Path, "lstat", autospec=True, side_effect=OSError("inspect")),
        pytest.raises(CertificationProfileError) as inspection,
    ):
        resolve_repository_profile_path(root, "profile.json")
    assert {item["code"] for item in inspection.value.findings} == {"profile-path-unavailable"}

    budget = tmp_path / "budget.json"
    budget.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(authority_mod, "MAX_PROFILE_BYTES", 1)
    with pytest.raises(CertificationProfileError) as declared_size:
        _read_profile_bytes(budget)
    assert {item["code"] for item in declared_size.value.findings} == {"profile-size-exceeded"}
    with (
        mock.patch.object(Path, "stat", autospec=True, return_value=SimpleNamespace(st_size=0)),
        pytest.raises(CertificationProfileError) as actual_size,
    ):
        _read_profile_bytes(budget)
    assert {item["code"] for item in actual_size.value.findings} == {"profile-size-exceeded"}

    finding = _validation_finding({"loc": [], "msg": ""})
    assert finding.path == "profile"
    assert finding.detail == "profile field is invalid"
