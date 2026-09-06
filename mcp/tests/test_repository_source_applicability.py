"""Exact source admission, pre-execution non-applicability, and truthful teardown."""

from __future__ import annotations

import importlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from agents_remember.certification.digests import content_digest
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles import (
    canonicalize_repository_profile,
    compile_repository_profile_plan,
    load_repository_profile,
    repository_profile_digest,
    resolve_repository_profile_selection,
)
from agents_remember.certification.repository_profiles.models import RepositoryCertificationProfile
from agents_remember.certification.repository_profiles.source_selection import git as source_git
from agents_remember.certification.repository_profiles.source_selection.models import (
    CandidateSourceSelection,
    RailSourceSelection,
    SourcePathApplicability,
)
from agents_remember.certification.repository_profiles.source_selection.reader import (
    read_rail_source_selection,
)
from agents_remember.errors import CertificationProfileError
from agents_remember.models.certification.base import RailIdentity
from agents_remember_test_support.code_quality import profile_rails
from pydantic import ValidationError
from repository_profile_test_support import REPOSITORY_ROOT, fixture_profile
from source_selection_test_support import (
    ambient_selection_fixture,
    source_selection_fixture,
    write_ambient_selection,
)
from test_agents_remember_quality import execution_manifest, load_dagger_module


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def _repository(root: Path) -> tuple[Path, str]:
    repository = root / "code"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "source-selection@example.invalid")
    _git(repository, "config", "user.name", "Source selection test")
    (repository / "old.py").write_text("old = 1\n")
    (repository / "deleted.py").write_text("deleted = 1\n")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _profile():
    return load_repository_profile(
        "agents-remember", REPOSITORY_ROOT, Path("mcp/certification-profile-v1.json")
    )


def _compile(source, *, mode="targeted", canonical=None):
    return compile_repository_profile_plan(
        canonical or _profile().canonical,
        selection_id="closeout-" + mode,
        candidate_identity=CandidateIdentity(kind="git-tree", value="c" * 40),
        source_selection=source,
    )


def test_git_census_uses_exact_trees_including_both_rename_endpoints(tmp_path):
    repository, base = _repository(tmp_path)
    (repository / "old.py").rename(repository / "renamed.py")
    (repository / "deleted.py").unlink()
    (repository / "new.py").write_text("new = 1\n")
    _git(repository, "add", "-A")
    candidate = CandidateIdentity(kind="git-tree", value=_git(repository, "write-tree"))
    # Neither later dirty bytes nor an untracked competing source change the selected tree.
    (repository / "new.py").write_text("dirty = 2\n")
    (repository / "untracked.py").write_text("untracked = 1\n")
    observation = source_git.observe_candidate_source_selection(repository, candidate, base)
    assert observation.changedPaths == ("deleted.py", "new.py", "old.py", "renamed.py")
    assert observation.baseCommit == base
    assert observation.baseTree == _git(repository, "rev-parse", base + "^{tree}")
    assert observation.candidateTree == candidate.value
    linked = tmp_path / "linked"
    _git(repository, "worktree", "add", "--detach", str(linked), base)
    assert source_git.observe_candidate_source_selection(linked, candidate, base) == observation


@pytest.mark.parametrize("fault", ["non-git", "commit", "nested", "missing-base"])
def test_source_observer_refuses_unbound_git_authority(tmp_path, fault):
    repository, base = _repository(tmp_path)
    candidate = CandidateIdentity(kind="git-tree", value=_git(repository, "write-tree"))
    if fault == "non-git":
        candidate = CandidateIdentity(kind="content-digest", value="a" * 64)
    elif fault == "commit":
        candidate = CandidateIdentity(kind="git-tree", value=base)
    elif fault == "nested":
        repository = repository / "nested"
        repository.mkdir()
    else:
        base = "missing-exact-comparison-base"
    with pytest.raises(ValueError):
        source_git.observe_candidate_source_selection(repository, candidate, base)


@pytest.mark.parametrize("fault", ["truncated", "unsafe", "bound"])
def test_git_transport_census_refuses_incomplete_or_unsafe_observations(
    tmp_path, monkeypatch, fault
):
    repository, base = _repository(tmp_path)
    candidate = CandidateIdentity(kind="git-tree", value=_git(repository, "write-tree"))
    actual = source_git.run_git

    def transport(root, arguments):
        result = actual(root, arguments)
        if arguments[0] == "diff-tree":
            output = {"truncated": "source.py", "unsafe": "../source.py\0", "bound": "x" * 200}[
                fault
            ]
            return subprocess.CompletedProcess(
                result.args, result.returncode, output, result.stderr
            )
        return result

    monkeypatch.setattr(source_git, "run_git", transport)
    if fault == "bound":
        monkeypatch.setattr(source_git, "MAX_SOURCE_SELECTION_BYTES", 100)
    with pytest.raises(ValueError):
        source_git.observe_candidate_source_selection(repository, candidate, base)


@pytest.mark.parametrize("mode", ["targeted", "full"])
def test_selected_source_contract_requires_observation_before_plan_compilation(mode):
    with pytest.raises(CertificationProfileError) as raised:
        _compile(None, mode=mode)
    assert raised.value.findings[0]["code"] == "source-selection-missing"
    wrong_candidate = source_selection_fixture("a" * 40)
    with pytest.raises(CertificationProfileError) as raised:
        _compile(wrong_candidate, mode=mode)
    assert raised.value.findings[0]["code"] == "source-selection-invalid"


def test_generic_profile_has_no_inferred_git_source_input(monkeypatch, tmp_path):
    admitted = replace(_profile(), canonical=canonicalize_repository_profile(fixture_profile()))
    selection = resolve_repository_profile_selection(
        admitted.canonical, purpose="closeout", mode="targeted"
    )

    def forbidden(*_args):
        raise AssertionError("generic profile must not infer Git applicability")

    monkeypatch.setattr(source_git, "observe_candidate_source_selection", forbidden)
    candidate = CandidateIdentity(kind="content-digest", value="c" * 64)
    assert (
        source_git.observe_profile_source_selection(admitted, selection, candidate, "explicit-base")
        is None
    )
    plan = compile_repository_profile_plan(
        admitted.canonical, selection_id=selection.selectionId, candidate_identity=candidate
    )
    assert plan.sourceSelection is None


def test_public_profile_observer_reports_typed_failure(tmp_path):
    admitted = replace(_profile(), repository_root=tmp_path)
    selection = resolve_repository_profile_selection(
        admitted.canonical, purpose="closeout", mode="targeted"
    )
    with pytest.raises(CertificationProfileError) as raised:
        source_git.observe_profile_source_selection(
            admitted, selection, CandidateIdentity(kind="git-tree", value="c" * 40), "missing"
        )
    assert raised.value.findings[0]["code"] == "source-selection-observation-failed"


def test_targeted_nonapplicability_removes_only_declared_conditional_execution_dependency():
    source = source_selection_fixture("c" * 40, changed_paths=("unrelated.py",))
    plan = _compile(source)
    rails = {rail.identity.railId: rail for rail in plan.gates[3].rails}
    ambient = rails["ambient-role-chat-e2e"]
    teardown = rails["teardown-process-cleanliness"]
    assert ambient.applicability.status == "not-applicable"
    assert teardown.applicability.status == "applicable"
    assert ambient.identity not in teardown.prerequisites
    definition = next(
        rail for rail in _profile().canonical.profile.rails if rail.identity == teardown.identity
    )
    assert {item.key for item in teardown.prerequisites} == {
        item.key for item in definition.prerequisites
    } - {ambient.identity.key}
    node = next(
        item for item in plan.gates[3].semanticInputs if item.inputKind == "source-applicability"
    )
    decision = RailSourceSelection.model_validate_json(node.canonicalJson)
    assert decision.sourceSelection == source and decision.applicability == ambient.applicability
    full = _compile(source, mode="full")
    full_rails = {rail.identity.railId: rail for rail in full.gates[3].rails}
    assert full_rails["ambient-role-chat-e2e"].applicability.status == "applicable"
    assert ambient.identity in full_rails["teardown-process-cleanliness"].prerequisites


def test_changed_source_declaration_changes_gate_four_semantics_before_execution():
    canonical = _profile().canonical
    source = source_selection_fixture("c" * 40)
    original = _compile(source, canonical=canonical)
    rails = []
    for original_rail in canonical.profile.rails:
        rail = original_rail
        if rail.sourceApplicability is not None:
            rail = rail.model_copy(
                update={
                    "sourceApplicability": rail.sourceApplicability.model_copy(
                        update={"dependencyPrefixes": ("another/",)}
                    )
                }
            )
        rails.append(rail)
    profile = canonical.profile.model_copy(update={"rails": tuple(rails)})
    profile = profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})
    changed = _compile(source, canonical=canonicalize_repository_profile(profile))
    assert [gate.planDigest for gate in original.gates[:3]] == [
        gate.planDigest for gate in changed.gates[:3]
    ]
    assert original.gates[3].planDigest != changed.gates[3].planDigest
    assert original.planDigest != changed.planDigest


@pytest.mark.parametrize("fault", ["digest", "selected-paths", "applicability"])
def test_self_verified_decision_refuses_relabelled_applicability(fault):
    payload = ambient_selection_fixture(applicable=False).model_dump(mode="json")
    if fault == "digest":
        payload["decisionDigest"] = "f" * 64
    else:
        if fault == "selected-paths":
            payload["selectedPaths"] = ["scripts/e2e_harness/run.py"]
        else:
            payload["applicability"].update(status="applicable", population="forged", reason=None)
        payload["decisionDigest"] = content_digest(
            {key: value for key, value in payload.items() if key != "decisionDigest"}
        )
    with pytest.raises(ValueError):
        RailSourceSelection.model_validate(payload)


@pytest.mark.parametrize("prefixes", [("z/", "a/"), ("a/", "a/")], ids=["order", "duplicate"])
def test_source_declaration_refuses_noncanonical_prefix_inventory(prefixes):
    payload = ambient_selection_fixture().declaration.model_dump(mode="json")
    payload["dependencyPrefixes"] = prefixes
    with pytest.raises(ValidationError, match="prefixes must be unique and ordered"):
        SourcePathApplicability.model_validate(payload)


@pytest.mark.parametrize("paths", [("z.py", "a.py"), ("a.py", "a.py")], ids=["order", "duplicate"])
def test_source_census_refuses_noncanonical_paths_even_with_matching_digest(paths):
    with pytest.raises(ValidationError, match="census must be complete, unique and ordered"):
        source_selection_fixture("c" * 40, changed_paths=paths)


def test_source_census_rejects_changed_paths_under_the_original_digest():
    payload = source_selection_fixture("c" * 40).model_dump(mode="json")
    payload["changedPaths"] = ["unrelated.py"]
    with pytest.raises(ValidationError, match="digest differs from its exact observation"):
        CandidateSourceSelection.model_validate(payload)


def test_nonapplicability_requires_declared_reason_even_with_recomputed_digest():
    payload = ambient_selection_fixture(applicable=False).model_dump(mode="json")
    payload["applicability"]["reason"] = "executor chose not to run the declared scenario"
    payload["decisionDigest"] = content_digest(
        {key: value for key, value in payload.items() if key != "decisionDigest"}
    )
    with pytest.raises(ValidationError, match="lacks its repository-declared reason"):
        RailSourceSelection.model_validate(payload)


def _assert_profile_refusal(profile: RepositoryCertificationProfile, code: str, path: str):
    # Keep schema and digest admission real so the graph validator owns this refusal.
    profile = RepositoryCertificationProfile.model_validate(profile.model_dump(mode="json"))
    profile = profile.model_copy(update={"profileDigest": repository_profile_digest(profile)})
    canonical = canonicalize_repository_profile(profile)
    with pytest.raises(CertificationProfileError) as raised:
        _compile(source_selection_fixture("c" * 40), canonical=canonical)
    assert (code, path) in {(item["code"], item["path"]) for item in raised.value.findings}


@pytest.mark.parametrize("fault", ["missing-publication", "wrong-publisher-gate", "posthoc-skip"])
def test_source_profile_refuses_unowned_evidence_or_postexecution_reclassification(fault):
    profile = _profile().canonical.profile
    rail = next(item for item in profile.rails if item.sourceApplicability is not None)
    declaration = rail.sourceApplicability
    assert declaration is not None
    if fault == "posthoc-skip":
        changed = rail.model_copy(
            update={"execution": rail.execution.model_copy(update={"skippedExitCodes": (77,)})}
        )
        profile = profile.model_copy(
            update={"rails": tuple(changed if item == rail else item for item in profile.rails)}
        )
        code = "source-selection-posthoc-skip"
    else:
        publications = tuple(
            item.model_copy(update={"publisherGates": (1,)})
            if item.path == declaration.evidencePath
            else item
            for item in profile.publishedArtifacts
            if fault != "missing-publication" or item.path != declaration.evidencePath
        )
        profile = profile.model_copy(update={"publishedArtifacts": publications})
        code = "source-selection-publication-missing"
    _assert_profile_refusal(profile, code, f"rails.{rail.identity.key}")


@pytest.mark.parametrize("owner", ["own-source", "conditional-source"])
@pytest.mark.parametrize("fault", ["missing-input", "undeclared-input"])
def test_profile_commands_must_consume_exactly_their_declared_source_authorities(owner, fault):
    profile = _profile().canonical.profile
    rail = next(
        item
        for item in profile.rails
        if (
            item.sourceApplicability is not None
            if owner == "own-source"
            else bool(item.conditionalPrerequisites)
        )
    )
    command = tuple(
        token for token in rail.execution.command if not token.startswith("{source-selection:")
    )
    if fault == "undeclared-input":
        command = (*rail.execution.command, "{source-selection:unowned-input}")
    changed = rail.model_copy(
        update={"execution": rail.execution.model_copy(update={"command": command})}
    )
    profile = profile.model_copy(
        update={"rails": tuple(changed if item == rail else item for item in profile.rails)}
    )
    _assert_profile_refusal(
        profile, "source-selection-command-mismatch", f"rails.{rail.identity.key}"
    )


@pytest.mark.parametrize("fault", ["not-prerequisite", "missing-rail", "other-gate", "no-source"])
def test_conditional_prerequisite_requires_a_declared_same_gate_source_owner(fault):
    profile = _profile().canonical.profile
    rail = next(item for item in profile.rails if item.conditionalPrerequisites)
    if fault == "not-prerequisite":
        changed = rail.model_copy(
            update={
                "prerequisites": tuple(
                    item for item in rail.prerequisites if item not in rail.conditionalPrerequisites
                )
            }
        )
    else:
        if fault == "missing-rail":
            identity = RailIdentity(railId="missing-source-owner", version="1.0.0")
        else:
            identity = next(
                item.identity
                for item in profile.rails
                if (
                    item.gate != rail.gate
                    if fault == "other-gate"
                    else item.identity in rail.prerequisites and item.sourceApplicability is None
                )
            )
        changed = rail.model_copy(
            update={
                "conditionalPrerequisites": (identity,),
                "prerequisites": tuple(
                    sorted(set((*rail.prerequisites, identity)), key=lambda item: item.key)
                ),
            }
        )
    profile = profile.model_copy(
        update={"rails": tuple(changed if item == rail else item for item in profile.rails)}
    )
    _assert_profile_refusal(
        profile, "conditional-prerequisite-invalid", f"rails.{rail.identity.key}"
    )


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("producer-artifact", "environment-producer-invalid"),
        ("producer-publication", "environment-producer-invalid"),
        ("producer-bound", "environment-producer-invalid"),
        ("proof-publication", "environment-proof-invalid"),
        ("proof-gates", "environment-proof-invalid"),
        ("proof-bound", "environment-proof-invalid"),
        ("consumer-gate-one", "environment-consumers-invalid"),
        ("unselected-producer", "environment-producer-not-selected"),
    ],
)
def test_profile_refuses_environment_reuse_without_original_producer_and_exact_proof(fault, code):
    profile = _profile().canonical.profile
    environment = profile.environments[0]
    path = f"environments.{environment.environmentId}"
    if fault == "producer-artifact":
        environment = environment.model_copy(update={"artifactId": "unproduced-census"})
        profile = profile.model_copy(update={"environments": (environment,)})
    elif fault == "consumer-gate-one":
        environment = environment.model_copy(update={"consumingGates": (1, 2, 3, 4)})
        profile = profile.model_copy(update={"environments": (environment,)})
        path += ".consumingGates"
    elif fault == "unselected-producer":
        selection = next(
            item for item in profile.selections if item.selectionId == "closeout-targeted"
        )
        gate = selection.gates[0]
        changed_gate = gate.model_copy(
            update={
                "railIds": tuple(item for item in gate.railIds if item != environment.producerRail)
            }
        )
        changed = selection.model_copy(update={"gates": (changed_gate, *selection.gates[1:])})
        profile = profile.model_copy(
            update={
                "selections": tuple(
                    changed if item == selection else item for item in profile.selections
                )
            }
        )
        path = f"selections.{selection.selectionId}"
    else:
        artifact_path = (
            environment.manifestPath
            if fault.startswith("producer-")
            else environment.reconstructionProofPath
        )
        publications = []
        for item in profile.publishedArtifacts:
            publication = item
            if item.path == artifact_path:
                if fault.endswith("publication"):
                    continue
                publication = item.model_copy(
                    update={"publisherGates": (2,)}
                    if fault == "proof-gates"
                    else {"maxBytes": item.maxBytes - 1}
                )
            publications.append(publication)
        profile = profile.model_copy(update={"publishedArtifacts": tuple(publications)})
    _assert_profile_refusal(profile, code, path)


def test_teardown_consumes_admitted_no_start_without_fabricating_scenario_reports(tmp_path):
    selection = write_ambient_selection(tmp_path, applicable=False)
    summary, proof = tmp_path / "summary.json", tmp_path / "proof.json"
    assert profile_rails._verify_teardown(summary, proof, selection) == 0
    assert not summary.exists()
    payload = json.loads(proof.read_bytes())
    assert payload["scenarioZeroStart"] is True
    assert payload["replications"] == []
    assert payload["sourceDecisionDigest"] == read_rail_source_selection(selection).decisionDigest
    proof.unlink()
    summary.write_text('{"status":"skipped"}')
    with pytest.raises(RuntimeError, match="unexpectedly published"):
        profile_rails._verify_teardown(summary, proof, selection)
    assert not proof.exists()


@pytest.mark.parametrize("fault", ["symlink", "oversize", "digest"])
def test_frozen_selection_reader_refuses_unsafe_or_altered_evidence(tmp_path, fault):
    selection = write_ambient_selection(tmp_path)
    if fault == "symlink":
        original = tmp_path / "original.json"
        selection.rename(original)
        selection.symlink_to(original)
    elif fault == "oversize":
        with selection.open("r+b") as stream:
            stream.truncate(1_000_001)
    else:
        payload = json.loads(selection.read_bytes())
        payload["decisionDigest"] = "f" * 64
        selection.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        read_rail_source_selection(selection)


def test_frozen_selection_reader_refuses_actual_growth_after_the_size_check(tmp_path, monkeypatch):
    selection = write_ambient_selection(tmp_path)
    original = selection.read_bytes()
    open_path = type(selection).open

    def grow_before_open(path, *args, **kwargs):
        if path == selection:
            with open_path(path, "ab") as writer:
                writer.write(b" " * (1_000_001 - len(original)))
        return open_path(path, *args, **kwargs)

    monkeypatch.setattr(type(selection), "open", grow_before_open)
    with pytest.raises(ValueError, match=r"^frozen source selection exceeds its byte bound$"):
        read_rail_source_selection(selection)
    assert selection.stat().st_size == 1_000_001


@pytest.mark.parametrize("fault", ["base", "candidate", "digest"])
def test_dagger_refuses_detached_source_selection_before_any_executor_command(fault):
    load_dagger_module()
    module = importlib.import_module("agents_remember_quality.profile_plan")
    manifest = json.loads(execution_manifest(mode="targeted").value)
    if fault == "base":
        manifest["diffBase"] = "e" * 40
    else:
        source = manifest["profilePlan"]["sourceSelection"]
        source["candidateTree" if fault == "candidate" else "selectionDigest"] = "e" * (
            40 if fault == "candidate" else 64
        )
        manifest["profilePlan"]["planDigest"] = content_digest(
            {key: value for key, value in manifest["profilePlan"].items() if key != "planDigest"}
        )
    with pytest.raises(ValueError, match="source selection"):
        module.load_execution_manifest(json.dumps(manifest))
