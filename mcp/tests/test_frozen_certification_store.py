"""Retained R05 authority and original-byte references in the R21 store."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Literal, cast

import pytest
from agents_remember.certification import certificate_store as store_module
from agents_remember.certification.certificate_models import (
    CertificationAdmissionManifest,
    CreationProvenance,
)
from agents_remember.certification.certificate_store import (
    CertificateObject,
    CertificateStorePolicy,
    ContentAddressedCertificateStore,
)
from agents_remember.certification.certification_lane import compile_certification_lane
from agents_remember.certification.digests import content_digest
from agents_remember.certification.frozen_run.authorities import (
    AuthorityInputSnapshot,
    CandidateAuthorityEnvelope,
    CandidateAuthorityRecords,
)
from agents_remember.certification.frozen_run.models import (
    FrozenCertificationRun,
    freeze_certification_run,
)
from agents_remember.certification.lifecycle_admission import (
    CompiledLifecycleAdmission,
    LifecycleAdmissionAuthorities,
    compile_lifecycle_admission,
)
from agents_remember.certification.lifecycle_models import (
    CertificationRecoveryRecord,
    ExactCandidateObservation,
    LifecycleAdmissionManifest,
    PriorRedDispositionManifest,
    PriorRedDispositionSemanticEnvelope,
)
from agents_remember.certification.lifecycle_recovery import compile_certification_recovery_record
from agents_remember.certification.models import CandidateIdentity
from agents_remember.certification.repository_profiles.canonical import (
    canonicalize_repository_profile,
)
from agents_remember.certification.repository_profiles.planning import (
    compile_repository_profile_plan,
)
from agents_remember.errors import CertificationContractError
from agents_remember.kernel.memory_ledger import LedgerRow, MemoryLedger, write_ledger
from agents_remember.memory_quality.gate_five_rails import gate_five_memory_rails
from agents_remember.models.certification.base import RailIdentity
from agents_remember.models.certification.corrective import (
    CorrectiveInputChange,
    RedCatalogDisposition,
)
from agents_remember.models.certification.references import (
    CertificateObjectKind,
    CertificateObjectReference,
)
from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.preparation import (
    CloseoutPreparationIntent,
    ExistingMemoryPreparationProof,
    PreparedCloseoutOutput,
    build_prepared_closeout_output,
    require_prepared_output_matches_intent,
)
from agents_remember.worktrees.integration.closeout.preparation.memory_reuse import (
    observe_existing_memory_proof,
)
from pydantic import ValidationError
from repository_profile_test_support import (
    NODE_FIXTURE,
    RUST_FIXTURE,
    FixtureRepository,
    fixture_profile,
)


def _run(
    marker: str = "original",
    tree: str = "c" * 40,
    fixture: FixtureRepository = NODE_FIXTURE,
) -> FrozenCertificationRun:
    profile = canonicalize_repository_profile(fixture_profile(fixture))
    plan = compile_repository_profile_plan(
        profile,
        selection_id="closeout-targeted",
        candidate_identity=CandidateIdentity(kind="git-tree", value=tree),
    )
    lane = compile_certification_lane(
        profile,
        plan,
        provenance=CreationProvenance(
            createdAt="2026-09-06T00:00:00+00:00",
            producer=f"fixture-{marker}",
            evidenceRef=f"fixture://creation/{marker}",
        ),
        memory_rails=gate_five_memory_rails(plan.selectionId),
    )
    return freeze_certification_run(profile, lane)


def _store(root: Path, max_objects: int = 20) -> ContentAddressedCertificateStore:
    return ContentAddressedCertificateStore(
        root,
        CertificateStorePolicy(
            scopeId="frozen-run-contract-test",
            maxObjects=max_objects,
            maxBytes=20_000_000,
            reclamationOwner="test-temporary-root",
        ),
    )


def _canonical(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _red_disposition(run: FrozenCertificationRun) -> PriorRedDispositionManifest:
    envelope = PriorRedDispositionSemanticEnvelope(
        priorAdmissionDigest="a" * 64,
        priorCatalogDigest="b" * 64,
        gate=1,
        successorCandidateCodeTree=run.repositoryPlan.candidateIdentity,
        successorCertificationPlanDigest=run.certificationPlan.planDigest,
        dispositions=(
            RedCatalogDisposition(
                rail=RailIdentity(railId="fixture-red", version="1.0.0"),
                priorStatus="fail",
                priorResultDigest="d" * 64,
                correctiveOwner="fixture-quality-owner",
                disposition="direct-repair",
                changedInputs=(
                    CorrectiveInputChange(
                        inputKind="source",
                        inputId="fixture.py",
                        beforeDigest="e" * 64,
                        afterDigest="f" * 64,
                    ),
                ),
                rationale="Storage fixture for an owner-supplied repaired input.",
            ),
        ),
    )
    return PriorRedDispositionManifest(
        semanticEnvelope=envelope,
        dispositionDigest=content_digest(envelope),
        provenance=run.provenance,
    )


def _lifecycle(run: FrozenCertificationRun, root: Path) -> CompiledLifecycleAdmission:
    candidate = ExactCandidateObservation(
        taskId="fixture-l33",
        contractPath=(root / "contract.md").as_posix(),
        lifecycleAuthorityDigest="a" * 64,
        repositoryId=run.repositoryProfile.profile.repositoryId,
        sourceBranchRef="refs/heads/fixture",
        sourceBranchTip="b" * 40,
        candidateCodeTree=run.repositoryPlan.candidateIdentity,
        topologyIdentityDigest="c" * 64,
        taskIntentIdentityDigest="d" * 64,
        normalizedCommitIntentDigest="e" * 64,
        mutationAuthorityDigest="f" * 64,
        sourceAuthorityDigest="1" * 64,
        worktreeRuleDigest="2" * 64,
        generatedInputsDigest="3" * 64,
        mutationAuthorityStatus="valid",
        sourceAuthorityStatus="valid",
        branchAuthorityStatus="valid",
        worktreeStatus="admissible",
        generatedArtifactStatus="current",
    )
    return compile_lifecycle_admission(
        LifecycleAdmissionAuthorities(
            registry=run.registry,
            certificationPlan=run.certificationPlan,
            repositoryProfile=run.repositoryProfile,
            repositoryPlan=run.repositoryPlan,
            candidate=candidate,
        ),
        provenance=run.provenance,
    )


@pytest.mark.parametrize("fixture", [NODE_FIXTURE, RUST_FIXTURE], ids=["node", "rust"])
def test_frozen_run_retains_full_aligned_authority_and_original_creation(
    tmp_path: Path, fixture: FixtureRepository
) -> None:
    run = _run(fixture=fixture)
    store = _store(tmp_path)
    path = store.publish(run)
    loaded = store.load(FrozenCertificationRun, run.runDigest)
    reference = store.reference("frozen-run", run.runDigest)

    assert loaded == run
    assert loaded.admission.provenance == loaded.provenance == run.provenance
    assert store.load_reference(reference) == run
    assert reference.contentSha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert reference.sizeBytes == path.stat().st_size
    assert json.loads(path.read_bytes())["repositoryProfile"] == run.repositoryProfile.model_dump(
        mode="json"
    )
    assert CertificateObjectReference.model_validate_json(reference.model_dump_json()) == reference


@pytest.mark.parametrize("action", ["publish", "load"])
def test_store_refuses_unregistered_model_subclasses_without_filesystem_writes(
    tmp_path: Path, action: str
) -> None:
    class UnregisteredAdmission(CertificationAdmissionManifest):
        """Same valid bytes do not register a different Python model as an authority."""

    original = _run().admission
    value = UnregisteredAdmission.model_validate(original.model_dump(mode="json"))
    assert value.model_dump(mode="json") == original.model_dump(mode="json")
    store = _store(tmp_path / "objects")

    with pytest.raises(CertificationContractError) as caught:
        if action == "publish":
            store.publish(value)
        else:
            store.load(UnregisteredAdmission, value.admissionDigest)

    assert caught.value.findings[0]["code"] == "certificate-object-model-invalid"
    assert list(tmp_path.iterdir()) == []


def test_run_identity_retains_provenance_without_changing_certificate_semantics() -> None:
    original, later = _run(), _run("later-generation")
    assert original.runDigest != later.runDigest
    assert original.admission.admissionDigest == later.admission.admissionDigest
    assert original.admission.semanticEnvelope == later.admission.semanticEnvelope
    assert original.registry == later.registry
    assert original.certificationPlan == later.certificationPlan


@pytest.mark.parametrize("field", ["certificationPlan", "repositoryPlan", "admission"])
def test_frozen_run_rejects_cross_candidate_authorities_with_a_recomputed_digest(
    field: str,
) -> None:
    original, other = _run(), _run(tree="d" * 40)
    payload = original.model_dump(mode="json", exclude={"runDigest"})
    payload[field] = other.model_dump(mode="json")[field]
    with pytest.raises(ValidationError, match=r"aligned|original admission"):
        FrozenCertificationRun.model_validate({**payload, "runDigest": content_digest(payload)})


@pytest.mark.parametrize("change", ["digest", "provenance", "unknown-field", "missing-profile"])
def test_frozen_run_rejects_malformed_or_recreated_records(change: str) -> None:
    payload = _run().model_dump(mode="json")
    if change == "digest":
        payload["runDigest"] = "0" * 64
    elif change == "provenance":
        payload["provenance"] = _run("recreated").provenance.model_dump(mode="json")
        payload["runDigest"] = content_digest(
            {k: v for k, v in payload.items() if k != "runDigest"}
        )
    elif change == "unknown-field":
        payload["latestGeneration"] = 7
    else:
        del payload["repositoryProfile"]
    with pytest.raises(ValidationError):
        FrozenCertificationRun.model_validate(payload)


@pytest.mark.parametrize("field", ["registry", "repositoryProfile"])
def test_frozen_run_rejects_cross_repository_authority(field: str) -> None:
    original, other = _run(), _run(fixture=RUST_FIXTURE)
    payload = original.model_dump(mode="json", exclude={"runDigest"})
    payload[field] = other.model_dump(mode="json")[field]
    with pytest.raises(ValidationError, match="aligned"):
        FrozenCertificationRun.model_validate({**payload, "runDigest": content_digest(payload)})


def test_existing_store_round_trips_all_new_lifecycle_object_kinds(tmp_path: Path) -> None:
    run = _run()
    lifecycle = _lifecycle(run, tmp_path)
    disposition = _red_disposition(run)
    recovery = compile_certification_recovery_record(lifecycle, (), (), provenance=run.provenance)
    store = _store(tmp_path)
    store.publish(lifecycle.lifecycle)
    store.publish(disposition)
    store.publish(recovery)

    assert (
        store.load(LifecycleAdmissionManifest, lifecycle.lifecycle.admissionDigest)
        == lifecycle.lifecycle
    )
    assert store.load(PriorRedDispositionManifest, disposition.dispositionDigest) == disposition
    assert store.load(CertificationRecoveryRecord, recovery.recoveryDigest) == recovery
    cases: tuple[tuple[CertificateObjectKind, str, CertificateObject], ...] = (
        ("lifecycle-admission", lifecycle.lifecycle.admissionDigest, lifecycle.lifecycle),
        ("prior-red-disposition", disposition.dispositionDigest, disposition),
        ("recovery", recovery.recoveryDigest, recovery),
    )
    for kind, digest, expected in cases:
        reference = store.reference(kind, digest)
        assert store.load_reference(reference) == expected


@pytest.mark.parametrize("changed_field", ["contentSha256", "sizeBytes"])
def test_reference_refuses_a_different_byte_identity(tmp_path: Path, changed_field: str) -> None:
    run = _run()
    store = _store(tmp_path)
    store.publish(run.admission)
    reference = store.reference("admission", run.admission.admissionDigest)
    payload = reference.model_dump(mode="json")
    payload[changed_field] = (
        "0" * 64 if changed_field == "contentSha256" else reference.sizeBytes + 1
    )
    with pytest.raises(CertificationContractError) as caught:
        store.load_reference(CertificateObjectReference.model_validate(payload))
    assert caught.value.findings[0]["code"] == "certificate-object-reference-mismatch"


def test_reference_rejects_rewritten_provenance_at_the_same_semantic_address(
    tmp_path: Path,
) -> None:
    original, later = _run(), _run("rewritten")
    store = _store(tmp_path)
    path = store.publish(original.admission)
    reference = store.reference("admission", original.admission.admissionDigest)
    with pytest.raises(CertificationContractError) as caught:
        store.publish(later.admission)
    assert caught.value.findings[0]["code"] == "content-address-collision"
    assert store.load_reference(reference) == original.admission

    # Emulate external evidence corruption: semantic identity alone still agrees,
    # while the selected reference must reject the changed original provenance.
    path.write_bytes(_canonical(later.admission.model_dump(mode="json")))
    with pytest.raises(CertificationContractError) as caught:
        store.load_reference(reference)
    assert caught.value.findings[0]["code"] == "certificate-object-reference-mismatch"


@pytest.mark.parametrize("distinct_provenance", [False, True])
def test_overlapping_publishers_preserve_the_first_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, distinct_provenance: bool
) -> None:
    first = _run().admission
    second = _run("contender").admission if distinct_provenance else first
    entered, release, attempted, finished = Event(), Event(), Event(), Event()
    atomic_write = store_module.atomic_write_bytes

    def pause_first_write(path: Path, payload: bytes) -> None:
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        atomic_write(path, payload)

    def contender() -> Path:
        attempted.set()
        try:
            return _store(tmp_path).publish(second)
        finally:
            finished.set()

    monkeypatch.setattr(store_module, "atomic_write_bytes", pause_first_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(_store(tmp_path).publish, first)
        try:
            assert entered.wait(5)
            other = pool.submit(contender)
            assert attempted.wait(5)
            assert not finished.wait(0.1)
        finally:
            release.set()
        path = winner.result(timeout=5)
        if distinct_provenance:
            with pytest.raises(CertificationContractError) as caught:
                other.result(timeout=5)
            assert caught.value.findings[0]["code"] == "content-address-collision"
        else:
            assert other.result(timeout=5) == path
    assert path.read_bytes() == _canonical(first.model_dump(mode="json"))
    assert _store(tmp_path).load(CertificationAdmissionManifest, first.admissionDigest) == first


@pytest.mark.parametrize("change", ["extra", "kind", "size", "digest"])
def test_object_references_are_closed_and_exact(change: str) -> None:
    payload: dict[str, object] = {
        "kind": "recovery",
        "semanticDigest": "a" * 64,
        "contentSha256": "b" * 64,
        "sizeBytes": 128,
    }
    if change == "extra":
        payload["generation"] = 2
    elif change == "kind":
        payload["kind"] = "latest"
    elif change == "size":
        payload["sizeBytes"] = True
    else:
        payload["semanticDigest"] = "latest"
    with pytest.raises(ValidationError):
        CertificateObjectReference.model_validate(payload)


@pytest.mark.parametrize("damage", ["noncanonical", "invalid-model", "missing"])
def test_reference_creation_requires_actual_validated_readback(tmp_path: Path, damage: str) -> None:
    run = _run()
    store = _store(tmp_path)
    path = store.publish(run)
    if damage == "noncanonical":
        path.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    elif damage == "invalid-model":
        payload = run.model_dump(mode="json")
        payload["runDigest"] = "0" * 64
        path.write_bytes(_canonical(payload))
    else:
        path.unlink()
    with pytest.raises(CertificationContractError):
        store.reference("frozen-run", run.runDigest)


def test_reference_requires_a_declared_object_kind(tmp_path: Path) -> None:
    with pytest.raises(CertificationContractError) as caught:
        _store(tmp_path).reference(cast(CertificateObjectKind, "latest"), "a" * 64)
    assert caught.value.findings[0]["code"] == "certificate-object-kind-invalid"


def _candidate_authorities(run: FrozenCertificationRun, root: Path) -> CandidateAuthorityRecords:
    snapshot = {
        "owner": "fixture-input-owner",
        "address": (root / "input.json").as_posix(),
        "canonicalBytes": "{}",
        "contentSha256": hashlib.sha256(b"{}").hexdigest(),
    }
    envelope = {
        "mutation": {
            "contractPath": (root / "contract.md").as_posix(),
            "taskId": "fixture-l33",
            "codeRoot": root.as_posix(),
            "codeWorkRef": "refs/heads/fixture-leaf",
            "normalizedCommitIntentDigest": "a" * 64,
        },
        "source": {
            "sourceRef": "refs/heads/fixture-master",
            "sourceTip": "b" * 40,
            "edges": [
                {
                    "side": "code",
                    "relation": "master-to-leaf",
                    "repositoryRoot": root.as_posix(),
                    "sourceRef": "refs/heads/fixture-master",
                    "sourceTip": "b" * 40,
                    "descendantRef": "refs/heads/fixture-leaf",
                    "descendantTip": "b" * 40,
                    "contractPath": (root / "contract.md").as_posix(),
                }
            ],
        },
        "worktree": {
            "codeRoot": root.as_posix(),
            "gitDirectory": (root / ".git").as_posix(),
            "commonDirectory": (root / ".git").as_posix(),
            "workRef": "refs/heads/fixture-leaf",
            "headCommit": "b" * 40,
            "stagedTree": run.repositoryPlan.candidateIdentity.value,
            "addAllTree": run.repositoryPlan.candidateIdentity.value,
            "preCommitHookRan": True,
        },
        "generated": {
            "profileDigest": run.repositoryProfile.profileDigest,
            "candidateTree": run.repositoryPlan.candidateIdentity.value,
            "declarations": snapshot,
        },
    }
    semantic = CandidateAuthorityEnvelope.model_validate(envelope)
    return CandidateAuthorityRecords(
        semanticEnvelope=semantic,
        authorityDigest=content_digest(semantic),
        inputSnapshots=(AuthorityInputSnapshot.model_validate(snapshot),),
        provenance=run.provenance,
    )


def test_candidate_authorities_retain_derivation_and_refuse_recreated_provenance(
    tmp_path: Path,
) -> None:
    run = _run()
    value = _candidate_authorities(run, tmp_path)
    store = _store(tmp_path)
    store.publish(value)
    reference = store.reference("candidate-authorities", value.authorityDigest)
    assert store.load(CandidateAuthorityRecords, value.authorityDigest) == value
    assert store.load_reference(reference) == value
    changed = value.model_copy(update={"provenance": _run("recreated").provenance})
    with pytest.raises(CertificationContractError) as caught:
        store.publish(changed)
    assert caught.value.findings[0]["code"] == "content-address-collision"
    assert store.load_reference(reference) == value


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("input-bytes", "authority input bytes do not match"),
        ("memory-root-only", "memory mutation authority requires both root and branch"),
        ("memory-ref-only", "memory mutation authority requires both root and branch"),
        ("semantic-digest", "candidate authority digest differs"),
        ("duplicate-input", "authority input snapshots must be unique and canonical"),
        ("input-order", "authority input snapshots must be unique and canonical"),
    ],
)
def test_candidate_authority_readback_refuses_corrupted_derivation_and_scope(
    tmp_path: Path, damage: str, message: str
) -> None:
    original = _candidate_authorities(_run(), tmp_path)
    store = _store(tmp_path)
    path = store.publish(original)
    reference = store.reference("candidate-authorities", original.authorityDigest)
    assert store.load_reference(reference) == original
    payload = original.model_dump(mode="json")
    if damage == "input-bytes":
        payload["inputSnapshots"][0]["canonicalBytes"] = '{"changed":true}'
    elif damage in {"memory-root-only", "memory-ref-only"}:
        mutation = payload["semanticEnvelope"]["mutation"]
        if damage == "memory-root-only":
            mutation["memoryRoot"] = (tmp_path / "memory").as_posix()
        else:
            mutation["memoryWorkRef"] = "refs/heads/fixture-memory"
        # A fresh envelope digest cannot authorize an incomplete mutation pair.
        payload["authorityDigest"] = content_digest(payload["semanticEnvelope"])
    elif damage == "semantic-digest":
        payload["semanticEnvelope"]["worktree"]["addAllTree"] = "d" * 40
    elif damage == "duplicate-input":
        payload["inputSnapshots"] *= 2
    else:
        snapshot = payload["inputSnapshots"][0]
        payload["inputSnapshots"] = [{**snapshot, "owner": "z-fixture-input-owner"}, snapshot]

    with pytest.raises(ValidationError, match=message):
        CandidateAuthorityRecords.model_validate(payload)
    corrupt_bytes = _canonical(payload)
    path.write_bytes(corrupt_bytes)
    with pytest.raises(CertificationContractError) as caught:
        store.load_reference(reference)
    assert caught.value.findings[0]["code"] == "certificate-object-invalid"
    assert path.read_bytes() == corrupt_bytes


def test_frozen_run_objects_share_the_existing_store_capacity(tmp_path: Path) -> None:
    run = _run()
    store = _store(tmp_path, max_objects=1)
    path = store.publish(run)
    with pytest.raises(CertificationContractError) as caught:
        store.publish(run.admission)
    assert caught.value.findings[0]["code"] == "certificate-store-capacity-exceeded"
    assert store.publish(run) == path
    assert store.load(FrozenCertificationRun, run.runDigest) == run


def _preparation_reference(kind: CertificateObjectKind, marker: str) -> dict[str, object]:
    """Wire-only reference input: this fixture does not assert green prefix authority."""
    return CertificateObjectReference(
        kind=kind, semanticDigest=marker * 64, contentSha256=marker * 64, sizeBytes=1
    ).model_dump(mode="json")


def _preparation_payload(root: Path) -> dict[str, object]:
    return {
        "schemaVersion": "closeout-preparation-intent/v1",
        "operationKey": "a" * 64,
        "generation": 1,
        "contractPath": (root / "contract.md").as_posix(),
        "contractSha256": "b" * 64,
        "leg": "code",
        "writeEnabled": True,
        "repositoryIdentity": (root / "logical/.git").as_posix(),
        "logicalRoot": (root / "logical").as_posix(),
        "logicalRef": "refs/heads/preparation-fixture",
        "expectedOldCommit": "a" * 40,
        "parentCommit": "a" * 40,
        "admittedTree": "b" * 40,
        "privateRoot": (root / "private").as_posix(),
        "normalizedMessage": "Préparer la sortie exacte",
        "hookPolicy": "strict-code-no-verify",
        "gitConfigSha256": "c" * 64,
        "hooksSha256": "d" * 64,
        "frozenRun": _preparation_reference("frozen-run", "e"),
        "candidateAuthorities": _preparation_reference("candidate-authorities", "f"),
        "prefixCertificates": [_preparation_reference("certificate", marker) for marker in "abcd"],
        "gateFiveCertificate": None,
        "existingMemoryProof": None,
    }


def _preparation_intent(payload: Mapping[str, object]) -> CloseoutPreparationIntent:
    return CloseoutPreparationIntent.model_validate(
        {**payload, "intentDigest": canonical_sha256(payload)}
    )


def _preparation_git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", root.as_posix(), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _real_preparation_objects(
    tmp_path: Path, *, create: bool = True, object_format: Literal["sha1", "sha256"] = "sha1"
):
    """Real porcelain output, with explicit wire-only selected-prefix placeholders.

    This exercises Git-object/store contracts, not lifecycle admission or Gate 5.
    """
    logical = tmp_path / "logical"
    logical.mkdir()
    _preparation_git(
        logical, "init", "-q", "-b", "preparation-fixture", f"--object-format={object_format}"
    )
    _preparation_git(logical, "config", "user.name", "Préparation Fixture")
    _preparation_git(logical, "config", "user.email", "preparation@example.invalid")
    _preparation_git(logical, "config", "commit.gpgsign", "false")
    (logical / "source.txt").write_text("base\n", encoding="utf-8")
    _preparation_git(logical, "add", "source.txt")
    _preparation_git(logical, "commit", "-qm", "actual base")
    parent = _preparation_git(logical, "rev-parse", "HEAD")
    physical = logical
    payload = _preparation_payload(tmp_path)
    if create:
        physical = tmp_path / "private"
        _preparation_git(logical, "worktree", "add", "--detach", physical.as_posix(), parent)
        (physical / "source.txt").write_text("prepared\n", encoding="utf-8")
        _preparation_git(physical, "add", "source.txt")
        _preparation_git(
            physical, "commit", "--no-verify", "-qm", str(payload["normalizedMessage"])
        )
    else:
        payload.update(writeEnabled=False, normalizedMessage=None, privateRoot=None)
    tree = _preparation_git(physical, "rev-parse", "HEAD^{tree}")
    payload.update(expectedOldCommit=parent, parentCommit=parent, admittedTree=tree)
    intent = _preparation_intent(payload)
    store = _store(tmp_path / "objects")
    store.publish(intent)
    reference = store.reference("preparation-intent", intent.intentDigest)
    raw = subprocess.run(
        ["git", "-C", physical.as_posix(), "cat-file", "commit", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout
    output = build_prepared_closeout_output(
        raw, reference, disposition="created" if create else "existing"
    )
    assert output.commit == _preparation_git(physical, "rev-parse", "HEAD")
    assert output.tree == tree
    assert output.parents == tuple(
        _preparation_git(physical, "show", "-s", "--format=%P", "HEAD").split()
    )
    assert output.committerDate == _preparation_git(physical, "show", "-s", "--format=%cI", "HEAD")
    assert not output.signaturePresent
    return store, intent, output, logical


@pytest.mark.parametrize("create", [True, False])
def test_preparation_store_retains_real_porcelain_output_without_publishing_task_ref(
    tmp_path: Path, create: bool
) -> None:
    store, intent, output, logical = _real_preparation_objects(tmp_path, create=create)
    before = _preparation_git(logical, "rev-parse", "HEAD")
    require_prepared_output_matches_intent(output, intent)
    path = store.publish(output)
    reference = store.reference("prepared-output", output.outputDigest)

    assert store.load(CloseoutPreparationIntent, intent.intentDigest) == intent
    assert store.load(PreparedCloseoutOutput, output.outputDigest) == output
    assert store.load_reference(reference) == output
    assert store.publish(output) == path
    assert _preparation_git(logical, "rev-parse", "HEAD") == before == intent.expectedOldCommit
    assert output.commit != before if create else output.commit == before
    assert _preparation_git(logical, "status", "--porcelain") == ""
    assert reference.contentSha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (
        not {"commit-proven", "certificationReady", "approvalClaimed"} & output.model_dump().keys()
    )


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_prepared_output_factory_retains_actual_git_format_and_verbatim_message(
    tmp_path: Path, object_format: Literal["sha1", "sha256"]
) -> None:
    """The factory observes bytes; this does not select the second fixture commit."""
    store, _intent, original, logical = _real_preparation_objects(
        tmp_path, create=False, object_format=object_format
    )
    retained = store.publish(original)
    before = retained.read_bytes()
    message = "Exact café\r\n\r\nTrailing spaces  \r\n".encode()
    subprocess.run(
        [
            "git",
            "-C",
            str(logical),
            "commit",
            "-q",
            "--allow-empty",
            "--cleanup=verbatim",
            "-F",
            "-",
        ],
        input=message,
        capture_output=True,
        check=True,
    )
    raw = subprocess.run(
        ["git", "-C", str(logical), "cat-file", "commit", "HEAD"],
        capture_output=True,
        check=True,
    ).stdout
    commit = _preparation_git(logical, "rev-parse", "HEAD")
    output = build_prepared_closeout_output(raw, original.intent, disposition="created")

    assert output.commit == commit
    assert output.tree == _preparation_git(logical, "rev-parse", "HEAD^{tree}")
    assert output.parents == (original.commit,)
    assert output.committerDate == _preparation_git(logical, "show", "-s", "--format=%cI", "HEAD")
    assert raw.partition(b"\n\n")[2] == message
    assert base64.b64decode(output.rawCommitBase64) == raw
    assert output.messageSha256 == hashlib.sha256(message).hexdigest()
    assert not output.signaturePresent
    assert retained.read_bytes() == before
    assert _preparation_git(logical, "rev-parse", "HEAD") == commit


def test_prepared_output_factory_refuses_damaged_actual_headers_without_store_writes(
    tmp_path: Path,
) -> None:
    store, _intent, original, logical = _real_preparation_objects(tmp_path)
    path = store.publish(original)
    before = path.read_bytes()
    raw = base64.b64decode(original.rawCommitBase64)
    for damaged in (
        raw.replace(b"tree ", b"tree invalid-", 1),
        raw.replace(b"\n\n", b"\n", 1),
        b"tree " + original.tree.encode() + b"\n" + raw,
        raw.replace(original.committerIdentity.encode(), b"invalid identity", 1),
        raw + b"\0",
    ):
        with pytest.raises(ValueError):
            build_prepared_closeout_output(damaged, original.intent, disposition="created")
    assert path.read_bytes() == before
    assert store.load(PreparedCloseoutOutput, original.outputDigest) == original
    assert _preparation_git(logical, "rev-parse", "HEAD") == original.parents[0]


@pytest.mark.parametrize("header_name", ["gpgsig", "gpgsig-sha256"])
def test_prepared_output_factory_retains_signature_headers_without_claiming_verification(
    tmp_path: Path, header_name: str
) -> None:
    """Untrusted signature text is retained as bytes, never accepted as a signature."""
    store, _intent, original, logical = _real_preparation_objects(tmp_path)
    retained = store.publish(original)
    before = retained.read_bytes()
    headers, _, message = base64.b64decode(original.rawCommitBase64).partition(b"\n\n")
    untrusted = headers + f"\n{header_name} unverified\n continuation".encode() + b"\n\n" + message
    expected = (
        subprocess.run(
            ["git", "-C", str(logical), "hash-object", "-t", "commit", "--stdin"],
            input=untrusted,
            capture_output=True,
            check=True,
        )
        .stdout.decode("ascii")
        .strip()
    )
    output = build_prepared_closeout_output(untrusted, original.intent, disposition="created")

    assert output.commit == expected
    assert output.signaturePresent
    assert base64.b64decode(output.rawCommitBase64) == untrusted
    assert output.rawCommitSha256 == hashlib.sha256(untrusted).hexdigest()
    assert output.messageSha256 == original.messageSha256
    assert retained.read_bytes() == before
    assert _preparation_git(logical, "rev-parse", "HEAD") == original.parents[0]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("operationKey", "A" * 64, "pattern"),
        ("generation", True, "valid integer"),
        ("expectedOldCommit", "a" * 41, "pattern"),
        ("parentCommit", "a" * 64, "one object format"),
        ("logicalRoot", "/repo/../other", "canonical absolute"),
        ("logicalRef", "refs/heads/../other", "exact local branch"),
        ("logicalRef", "refs/heads/name.lock", "exact local branch"),
        ("logicalRef", "refs/heads/name\\other", "exact local branch"),
        ("privateRoot", None, "real private path"),
        ("privateRoot", "//private", "canonical absolute"),
        ("normalizedMessage", None, "real private path"),
        ("normalizedMessage", " padded ", "unblank|unpadding|unpadded"),
        ("hookPolicy", "ordinary", "hook policy"),
        ("frozenRun", _preparation_reference("certificate", "e"), "frozen-run reference"),
        (
            "candidateAuthorities",
            _preparation_reference("certificate", "f"),
            "candidate-authorities reference",
        ),
        ("prefixCertificates", [_preparation_reference("recovery", "a")], "non-certificate"),
        ("prefixCertificates", [_preparation_reference("certificate", "a")] * 2, "unique"),
        ("gateFiveCertificate", _preparation_reference("certificate", "e"), "precede Gate-5"),
    ],
)
def test_preparation_intent_refuses_malformed_or_foreign_wire_authority(
    tmp_path: Path, field: str, value: object, error: str
) -> None:
    payload = {**_preparation_payload(tmp_path), field: value}
    with pytest.raises(ValidationError, match=error):
        _preparation_intent(payload)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("leg", ["memory-content", "ledger"])
def test_no_write_preparation_cannot_mislabel_ledger_head_as_memory_content(
    tmp_path: Path, leg: str
) -> None:
    payload = _preparation_payload(tmp_path)
    payload.update(
        leg=leg, writeEnabled=False, normalizedMessage=None, privateRoot=None, hookPolicy="ordinary"
    )
    with pytest.raises(ValidationError, match="existing memory proof"):
        _preparation_intent(payload)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("leg", ["memory-content", "ledger"])
def test_memory_preparation_retains_a_separate_gate_five_reference(
    tmp_path: Path, leg: str
) -> None:
    payload = _preparation_payload(tmp_path)
    payload.update(leg=leg, hookPolicy="ordinary")
    for reference in (None, _preparation_reference("result-manifest", "e")):
        with pytest.raises(ValidationError, match="Gate-5 certificate reference"):
            _preparation_intent({**payload, "gateFiveCertificate": reference})
    reference = _preparation_reference("certificate", "e")
    intent = _preparation_intent({**payload, "gateFiveCertificate": reference})
    assert intent.gateFiveCertificate is not None
    assert intent.gateFiveCertificate.model_dump(mode="json") == reference
    assert len(intent.prefixCertificates) == 4
    assert list(tmp_path.iterdir()) == []


def test_preparation_requires_separate_private_checkout_and_exact_digest(tmp_path: Path) -> None:
    payload = _preparation_payload(tmp_path)
    for private in (
        payload["logicalRoot"],
        str(payload["repositoryIdentity"]) + "/private",
        tmp_path.as_posix(),
    ):
        with pytest.raises(ValidationError, match="separate"):
            _preparation_intent({**payload, "privateRoot": private})
    with pytest.raises(ValidationError, match="complete record"):
        CloseoutPreparationIntent.model_validate({**payload, "intentDigest": "0" * 64})
    assert list(tmp_path.iterdir()) == []


def test_prepared_output_rejects_invented_observations_over_real_git_bytes(tmp_path: Path) -> None:
    store, intent, original, logical = _real_preparation_objects(tmp_path)
    path = store.publish(original)
    before = path.read_bytes()
    payload = original.model_dump(mode="json", exclude={"outputDigest"})
    faults = {
        "intent": _preparation_reference("certificate", "a"),
        "commit": "0" * 40,
        "tree": "1" * 40,
        "parents": [],
        "authorIdentity": "Another <another@example.invalid> 1 +0000",
        "committerDate": "2000-01-01T00:00:00+00:00",
        "messageSha256": "0" * 64,
        "rawCommitSha256": "0" * 64,
        "rawCommitBase64": str(payload["rawCommitBase64"]) + "\n",
        "signaturePresent": True,
    }
    for field, value in faults.items():
        changed = {**payload, field: value}
        with pytest.raises(ValidationError):
            PreparedCloseoutOutput.model_validate(
                {**changed, "outputDigest": canonical_sha256(changed)}
            )
    assert path.read_bytes() == before
    assert store.load(PreparedCloseoutOutput, original.outputDigest) == original
    assert _preparation_git(logical, "rev-parse", "HEAD") == intent.expectedOldCommit


def test_prepared_output_requires_the_exact_intent_parent_tree_and_write_route(
    tmp_path: Path,
) -> None:
    store, intent, output, _logical = _real_preparation_objects(tmp_path)
    for field, value in (("contentSha256", "0" * 64), ("sizeBytes", output.intent.sizeBytes + 1)):
        changed = output.model_copy(
            update={"intent": output.intent.model_copy(update={field: value})}
        )
        payload = changed.model_dump(mode="json", exclude={"outputDigest"})
        changed = PreparedCloseoutOutput.model_validate(
            {**payload, "outputDigest": canonical_sha256(payload)}
        )
        with pytest.raises(ValueError, match="exact selected intent"):
            require_prepared_output_matches_intent(changed, intent)
    for updates in (
        {"parentCommit": "0" * 40},
        {"admittedTree": "0" * 40},
        {"writeEnabled": False, "privateRoot": None, "normalizedMessage": None},
    ):
        payload = intent.model_dump(mode="json", exclude={"intentDigest"}) | updates
        other = _preparation_intent(payload)
        store.publish(other)
        changed = output.model_dump(mode="json", exclude={"outputDigest"})
        changed["intent"] = store.reference("preparation-intent", other.intentDigest).model_dump(
            mode="json"
        )
        rebound = PreparedCloseoutOutput.model_validate(
            {**changed, "outputDigest": canonical_sha256(changed)}
        )
        with pytest.raises(ValueError, match=r"admitted intent|exact intended parent"):
            require_prepared_output_matches_intent(rebound, other)


def _real_memory_reuse_objects(tmp_path: Path, *, unmapped_head: bool = False):
    """Real M/L objects and ledger bytes; selected certificate refs remain wire-only."""
    store, _intent, content, logical = _real_preparation_objects(tmp_path, create=False)
    ledger = MemoryLedger(
        repo_name="preparation-fixture",
        base_code_commit=content.commit,
        base_memory_commit=content.commit,
        last_verified_code_commit=content.commit,
        last_memory_content_commit=content.commit,
        sort_order="newest-first",
        rows=[LedgerRow(content.commit, content.commit)],
    )
    write_ledger(logical / "memory.md", ledger)
    _preparation_git(logical, "add", "memory.md")
    _preparation_git(logical, "commit", "-qm", "Retain actual content mapping")
    head = _preparation_git(logical, "rev-parse", "HEAD")
    tree = _preparation_git(logical, "rev-parse", "HEAD^{tree}")
    proof = observe_existing_memory_proof(
        logical,
        repository_name="preparation-fixture",
        code_commit=head if unmapped_head else content.commit,
        certified_tree=tree,
    )
    assert proof is not None
    assert proof.memoryContentCommit == (head if unmapped_head else content.commit)
    assert proof.memoryContentTree == (tree if unmapped_head else content.tree)
    assert proof.ledgerBlob == _preparation_git(logical, "rev-parse", "HEAD:memory.md")
    return store, proof, logical


def _memory_reuse_intent_payload(root: Path, proof: ExistingMemoryPreparationProof, leg: str):
    memory = leg == "memory-content"
    return {
        **_preparation_payload(root),
        "leg": leg,
        "writeEnabled": False,
        "expectedOldCommit": proof.logicalHeadCommit,
        "parentCommit": proof.memoryContentCommit if memory else proof.logicalHeadCommit,
        "admittedTree": proof.memoryContentTree if memory else proof.logicalHeadTree,
        "privateRoot": None,
        "normalizedMessage": None,
        "hookPolicy": "ordinary",
        "gateFiveCertificate": _preparation_reference("certificate", "e"),
        "existingMemoryProof": proof.model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    ("leg", "unmapped_head"),
    [("memory-content", False), ("ledger", False), ("memory-content", True)],
)
def test_existing_memory_preparation_retains_real_content_and_ledger_outputs(
    tmp_path: Path, leg: str, unmapped_head: bool
) -> None:
    store, proof, logical = _real_memory_reuse_objects(tmp_path, unmapped_head=unmapped_head)
    before = (logical / "memory.md").read_bytes()
    intent = _preparation_intent(_memory_reuse_intent_payload(tmp_path, proof, leg))
    store.publish(intent)
    raw = subprocess.run(
        ["git", "-C", str(logical), "cat-file", "commit", intent.parentCommit],
        capture_output=True,
        check=True,
    ).stdout
    output = build_prepared_closeout_output(
        raw, store.reference("preparation-intent", intent.intentDigest), disposition="existing"
    )
    require_prepared_output_matches_intent(output, intent)
    store.publish(output)

    assert store.load(CloseoutPreparationIntent, intent.intentDigest) == intent
    assert store.load(PreparedCloseoutOutput, output.outputDigest) == output
    assert output.commit == intent.parentCommit
    assert output.tree == intent.admittedTree
    assert proof.mappingDisposition == ("unmapped-head" if unmapped_head else "existing-mapping")
    assert (
        proof.memoryContentCommit == proof.logicalHeadCommit
        if unmapped_head
        else (proof.memoryContentCommit != proof.logicalHeadCommit)
    )
    assert _preparation_git(logical, "rev-parse", "HEAD") == proof.logicalHeadCommit
    assert (logical / "memory.md").read_bytes() == before
    assert _preparation_git(logical, "status", "--porcelain") == ""
    if leg == "memory-content" and not unmapped_head:
        ledger_raw = subprocess.run(
            ["git", "-C", str(logical), "cat-file", "commit", "HEAD"],
            capture_output=True,
            check=True,
        ).stdout
        wrong = build_prepared_closeout_output(ledger_raw, output.intent, disposition="existing")
        with pytest.raises(ValueError, match="tree differs from its admitted intent"):
            require_prepared_output_matches_intent(wrong, intent)


def test_existing_memory_proof_refuses_malformed_identity_and_unbound_content(
    tmp_path: Path,
) -> None:
    _store_value, proof, logical = _real_memory_reuse_objects(tmp_path)
    original = proof.model_dump(mode="json", exclude={"proofDigest"})
    for field, value in (
        ("repositoryIdentity", "/repo/../foreign"),
        ("logicalHeadCommit", "a" * 41),
        ("ledgerBlob", "b" * 64),
        ("nonLedgerEntriesSha256", "c" * 40),
        ("mappingDisposition", "unmapped-head"),
        ("unexpectedAuthority", True),
    ):
        payload = {**original, field: value}
        with pytest.raises(ValidationError):
            ExistingMemoryPreparationProof.model_validate(
                {**payload, "proofDigest": canonical_sha256(payload)}
            )
    with pytest.raises(ValidationError, match="complete record"):
        ExistingMemoryPreparationProof.model_validate({**original, "proofDigest": "0" * 64})
    different_code_format = {**original, "codeCommit": "a" * 64}
    assert (
        ExistingMemoryPreparationProof.model_validate(
            {**different_code_format, "proofDigest": canonical_sha256(different_code_format)}
        ).codeCommit
        == "a" * 64
    )
    assert _preparation_git(logical, "rev-parse", "HEAD") == proof.logicalHeadCommit


def test_existing_memory_intent_refuses_foreign_proof_and_wrong_leg_routes(tmp_path: Path) -> None:
    _store_value, proof, _logical = _real_memory_reuse_objects(tmp_path)
    original = _memory_reuse_intent_payload(tmp_path, proof, "memory-content")
    for changes in (
        {"repositoryIdentity": str(tmp_path / "foreign.git")},
        {"expectedOldCommit": "0" * 40},
        {"parentCommit": proof.logicalHeadCommit},
        {"admittedTree": proof.logicalHeadTree},
        {"privateRoot": str(tmp_path / "private")},
        {"normalizedMessage": "invented no-write message"},
        {"gateFiveCertificate": _preparation_reference("result-manifest", "e")},
        {"existingMemoryProof": None},
    ):
        with pytest.raises(ValidationError):
            _preparation_intent({**original, **changes})
    with pytest.raises(ValidationError, match="code preparation cannot carry"):
        _preparation_intent(
            {**_preparation_payload(tmp_path), "existingMemoryProof": proof.model_dump(mode="json")}
        )


@pytest.mark.parametrize("unmapped_head", [False, True])
def test_ledger_after_reused_memory_retains_the_original_logical_parent(
    tmp_path: Path, unmapped_head: bool
) -> None:
    _store_value, proof, logical = _real_memory_reuse_objects(tmp_path, unmapped_head=unmapped_head)
    payload = _memory_reuse_intent_payload(tmp_path, proof, "ledger")
    if unmapped_head:
        with pytest.raises(ValidationError, match="exact existing mapping"):
            _preparation_intent(payload)
    enabled = {
        **payload,
        "writeEnabled": True,
        "privateRoot": str(tmp_path / "private"),
        "normalizedMessage": "Prepare the exact ledger delta",
    }
    intent = _preparation_intent(enabled)
    assert intent.parentCommit == intent.expectedOldCommit == proof.logicalHeadCommit
    for changes in ({"parentCommit": "0" * 40}, {"leg": "memory-content"}):
        with pytest.raises(ValidationError, match="created ledger after reused memory"):
            _preparation_intent({**enabled, **changes})
    assert _preparation_git(logical, "rev-parse", "HEAD") == proof.logicalHeadCommit


def test_memory_reuse_observer_refuses_real_non_ledger_content_difference(tmp_path: Path) -> None:
    _store_value, proof, logical = _real_memory_reuse_objects(tmp_path)
    (logical / "source.txt").write_text("different actual content\n", encoding="utf-8")
    _preparation_git(logical, "add", "source.txt")
    _preparation_git(logical, "commit", "-qm", "Actual non-ledger change")
    head = _preparation_git(logical, "rev-parse", "HEAD")
    tree = _preparation_git(logical, "rev-parse", "HEAD^{tree}")
    ledger_before = (logical / "memory.md").read_bytes()
    with pytest.raises(CertificationContractError) as refused:
        observe_existing_memory_proof(
            logical,
            repository_name="preparation-fixture",
            code_commit=proof.codeCommit,
            certified_tree=tree,
        )
    assert refused.value.findings[0]["code"] == "memory-reuse-content-differs"
    assert _preparation_git(logical, "rev-parse", "HEAD") == head
    assert (logical / "memory.md").read_bytes() == ledger_before
    assert _preparation_git(logical, "status", "--porcelain") == ""


@pytest.mark.parametrize("staged", [False, True])
def test_dirty_memory_candidate_does_not_claim_existing_reuse(tmp_path: Path, staged: bool) -> None:
    _store_value, proof, logical = _real_memory_reuse_objects(tmp_path)
    source = logical / "source.txt"
    source.write_text("candidate for a genuine new M\n", encoding="utf-8")
    if staged:
        _preparation_git(logical, "add", "source.txt")
    index_before = _preparation_git(logical, "write-tree")
    assert (
        observe_existing_memory_proof(
            logical,
            repository_name="preparation-fixture",
            code_commit=proof.codeCommit,
            certified_tree=proof.logicalHeadTree,
        )
        is None
    )
    assert _preparation_git(logical, "rev-parse", "HEAD") == proof.logicalHeadCommit
    assert _preparation_git(logical, "write-tree") == index_before
    assert source.read_text(encoding="utf-8") == "candidate for a genuine new M\n"
