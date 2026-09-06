"""Retained R05 authority and original-byte references in the R21 store."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import cast

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
