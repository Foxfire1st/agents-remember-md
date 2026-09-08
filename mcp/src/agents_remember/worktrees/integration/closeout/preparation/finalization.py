"""Publish only original prepared outputs through the current closeout mutation journal."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import cast

from agents_remember.certification.digests import content_digest
from agents_remember.certification.repository_profiles import load_repository_profile
from agents_remember.controlplane.enforcement import CLOSEOUT_GATE_KIND, evaluate_closeout_gate
from agents_remember.controlplane.store import GateStore
from agents_remember.kernel.authority import require_repo
from agents_remember.kernel.git_closeout_publication import GitCloseoutPublicationBinding
from agents_remember.kernel.git_command import (
    admit_git_closeout_publication,
    inspect_existing_git_preparation,
    inspect_git_closeout_publication,
    publish_git_closeout_ref,
    read_git_blob_bytes,
    read_git_commit_bytes,
)
from agents_remember.kernel.memory_ledger import ledger_to_text, parse_ledger_text, write_ledger
from agents_remember.kernel.primitives.gate_policy import (
    DecisionRole,
    GatePolicyRule,
    make_gate_policy,
)
from agents_remember.kernel.primitives.observer_paths import observer_logs_root
from agents_remember.kernel.primitives.runtime_config import load_config
from agents_remember.models.lifecycles.mutation_evidence import (
    CloseoutMutationLeg,
    GitMutationEvidence,
)
from agents_remember.models.lifecycles.operation import (
    CloseoutOperationInput,
    LifecycleOperationRecord,
    LifecycleOperationRecoveryCommits,
)
from agents_remember.models.lifecycles.preparation import (
    PreparedCloseoutOutput,
    require_prepared_output_matches_intent,
)
from agents_remember.models.structural.gates import GateKind
from agents_remember.observer.events import now_iso
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
)
from agents_remember.worktrees.integration.closeout.certification.observation import (
    CandidateObservationRequest,
    observe_certification_candidate,
    refuse,
)
from agents_remember.worktrees.integration.closeout.certification.selection import (
    load_typed,
    require_selected_certification,
)
from agents_remember.worktrees.integration.closeout.ledger_recovery import (
    classify_closeout_ledger_recovery,
)
from agents_remember.worktrees.integration.closeout.preparation_selection import (
    selected_preparation_intents,
)
from agents_remember.worktrees.integration.closeout.recovery_projection import (
    derive_closeout_recovery_commits,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_store import (
    LifecycleOperationStore,
)
from agents_remember.worktrees.integration.mutation_evidence import ephemeral_git_mutation_snapshot
from agents_remember.worktrees.modules.closeout import _amended_closeout_contract
from agents_remember.worktrees.modules.git import require_git
from agents_remember.worktrees.modules.guidance import status_payload
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.modules.quality.certification_records import certificate_store
from agents_remember.worktrees.queue.closeout_recovery import prove_closeout_recovery_commits
from agents_remember.worktrees.queue.closeout_staged_quality import PreparedStagedCode
from agents_remember.worktrees.route_review import require_current_route_review
from agents_remember.worktrees.worktree_contract import (
    WorktreeContract,
    load_contract,
    write_contract,
)

from .memory_output import PreparedMemoryOutputs
from .policy import observe_git_preparation_policy
from .selected import SelectedCloseoutPreparation


def _output(selected: SelectedCloseoutPreparation) -> PreparedCloseoutOutput:
    state = selected.handoff.record.preparation
    if state is None:
        refuse("finalization-preparation-missing", "selected C/M/L", None)
    leg = next((item for item in state.legs if item.leg == selected.intent.leg), None)
    if leg is None or leg.intent != selected.reference or leg.output is None:
        refuse("finalization-output-missing", selected.reference, leg)
    output = load_typed(
        certificate_store(selected.handoff.contract.worktree_group),
        leg.output,
        PreparedCloseoutOutput,
    )
    require_prepared_output_matches_intent(output, selected.intent)
    raw = read_git_commit_bytes(Path(selected.intent.logicalRoot), output.commit)
    if raw != base64.b64decode(output.rawCommitBase64, validate=True):
        refuse("finalization-output-bytes-moved", output.outputDigest, "raw commit")
    return output


def _owner(bundle: PreparedMemoryOutputs) -> LifecycleOperationRecord:
    original = bundle.handoff.record
    current = bundle.handoff.store.read()
    if (
        current is None
        or (
            current.operationKey,
            current.generation,
            current.workerPid,
            current.workerLease,
            current.workerProcessFingerprint,
            current.input,
            current.certification,
            current.preparation,
        )
        != (
            original.operationKey,
            original.generation,
            original.workerPid,
            original.workerLease,
            original.workerProcessFingerprint,
            original.input,
            original.certification,
            original.preparation,
        )
        or current.status != "running"
        or current.cancelRequested
    ):
        refuse("finalization-worker-moved", original.operationKey, current)
    actual = load_contract(bundle.handoff.contract.contract_path)
    if closeout_contract_sha256(actual) != bundle.code.intent.contractSha256:
        refuse(
            "finalization-contract-moved",
            bundle.code.intent.contractSha256,
            closeout_contract_sha256(actual),
        )
    return current


def _binding(selected: SelectedCloseoutPreparation) -> GitCloseoutPublicationBinding:
    intent, output = selected.intent, _output(selected)
    # L follows newly published M; expectedOldCommit separately retains the
    # logical pre-preparation tip and is not the ledger publication parent.
    old = intent.parentCommit if intent.leg == "ledger" else intent.expectedOldCommit
    return GitCloseoutPublicationBinding(
        Path(intent.logicalRoot),
        intent.logicalRef,
        Path(intent.repositoryIdentity),
        old,
        output.commit,
        output.tree,
        intent.operationKey,
        intent.generation,
        intent.intentDigest,
    )


def _approval(
    bundle: PreparedMemoryOutputs, record: LifecycleOperationRecord, *, claim: bool
) -> None:
    operation = record.input
    assert isinstance(operation, CloseoutOperationInput)
    if not operation.approvalNote.strip():
        refuse(
            "finalization-approval-missing",
            "original explicit approval note",
            operation.approvalNote,
        )
    contract = bundle.handoff.contract
    if not contract.lifecycle_id:
        return
    policy = make_gate_policy(
        [
            GatePolicyRule(
                cast(GateKind, item.kind),
                cast(DecisionRole | None, item.delegatedRole),
                item.requireReviewerVerdict,
            )
            for item in operation.gatePolicy
        ]
    )
    store = GateStore(observer_logs_root(contract.coordination_root))
    guard = (
        store.claim_approval(
            contract.lifecycle_id,
            kind=CLOSEOUT_GATE_KIND,
            now=now_iso(),
            policy=policy,
            operation_key=record.operationKey,
        )
        if claim
        else evaluate_closeout_gate(
            store.current(contract.lifecycle_id), policy, record.operationKey
        )
    )
    if not guard.permitted:
        refuse("finalization-approval-refused", "current original approval", guard.reason)


def _cas(
    bundle: PreparedMemoryOutputs, observed: LifecycleOperationRecord, updates: dict[str, object]
) -> LifecycleOperationRecord:
    record, matched = bundle.handoff.store.update_if_current(
        observed, lambda current: current.model_copy(update=updates)
    )
    if not matched:
        refuse("finalization-journal-moved", observed.recordRevision, record.recordRevision)
    return record


def _published_head(
    bundle: PreparedMemoryOutputs,
    record: LifecycleOperationRecord,
    selected: SelectedCloseoutPreparation,
    leg: CloseoutMutationLeg,
) -> str:
    intent = selected.intent
    root = Path(intent.logicalRoot)
    head = require_git(root, ["rev-parse", "--verify", intent.logicalRef])
    initial = (
        bundle.memory.intent.expectedOldCommit if leg == "ledger" else intent.expectedOldCommit
    )
    allowed = {initial}
    legs: tuple[tuple[CloseoutMutationLeg, SelectedCloseoutPreparation], ...] = (
        ("code", bundle.code),
        ("memory", bundle.memory),
        ("ledger", bundle.ledger),
    )
    for name, item in legs:
        if Path(item.intent.logicalRoot) != root or not item.intent.writeEnabled:
            continue
        proof = record.mutationEvidence.get(name)
        output = _output(item)
        if proof is not None and proof.state in {"mutation-intent", "commit-proven"}:
            expected_old = (
                item.intent.parentCommit if name == "ledger" else item.intent.expectedOldCommit
            )
            if (
                proof.before is None
                or proof.before.head != expected_old
                or proof.expectedOutputTree != output.tree
                or proof.repository != item.intent.logicalRoot
            ):
                refuse("finalization-mutation-binding-moved", item.intent.intentDigest, proof)
            allowed.add(output.commit)
    if head not in allowed:
        refuse("finalization-unowned-ref-movement", sorted(allowed), head)
    return head


def _physical_memory(bundle: PreparedMemoryOutputs, record: LifecycleOperationRecord) -> None:
    memory, ledger = bundle.memory, bundle.ledger
    root = Path(memory.intent.logicalRoot)
    proof = record.mutationEvidence.get("ledger")
    if ledger.intent.writeEnabled and proof is not None and proof.state == "mutation-intent":
        classified = classify_closeout_ledger_recovery(bundle.handoff.contract, record)
        if classified.state not in {
            "accepted-before",
            "prepared-unstaged",
            "prepared-staged",
            "commit-proven-pending-publication",
        }:
            refuse(
                "finalization-ledger-prestate-refused",
                "exact original ledger states",
                classified.decision_payload(),
            )
        expected = (
            classified.before_text.encode("utf-8")
            if classified.state == "accepted-before"
            else bundle.ledgerBytes
        )
        if (root / "memory.md").read_bytes() != expected:
            refuse(
                "finalization-ledger-physical-bytes-moved",
                ledger.intent.intentDigest,
                "exact bytes differ",
            )
        return
    selected = (
        ledger
        if ledger.intent.writeEnabled and proof is not None and proof.state == "commit-proven"
        else memory
    )
    if selected.intent.writeEnabled:

        def authorize(_binding: GitCloseoutPublicationBinding) -> None:
            _owner(bundle)

        inspect_git_closeout_publication(
            admit_git_closeout_publication(_binding(selected), authorize=authorize)
        )
    else:
        head = memory.intent.expectedOldCommit
        existing = memory.intent.existingMemoryProof
        tree = existing.logicalHeadTree if existing is not None else memory.intent.admittedTree
        inspect_existing_git_preparation(
            root,
            common_directory=Path(memory.intent.repositoryIdentity),
            logical_ref=memory.intent.logicalRef,
            commit=head,
            tree=tree,
        )


def _live(bundle: PreparedMemoryOutputs) -> LifecycleOperationRecord:
    record = _owner(bundle)
    contract = bundle.handoff.contract
    selected = require_selected_certification(contract, record)
    if len(selected.terminals) != 5 or any(item.certificate is None for item in selected.terminals):
        refuse(
            "finalization-certificate-prefix-incomplete",
            "five original green certificates",
            selected.state,
        )
    final_certificate = selected.terminals[-1].certificate
    assert final_certificate is not None  # The complete prefix was checked above.
    inputs = final_certificate.semanticEnvelope.gateFiveInputs
    certified_tree = (
        bundle.memory.intent.admittedTree
        if bundle.memory.intent.existingMemoryProof is None
        else bundle.memory.intent.existingMemoryProof.logicalHeadTree
    )
    if (
        inputs is None
        or inputs.memoryTree.value != certified_tree
        or bundle.memory.intent.gateFiveCertificate != selected.terminals[-1].certificateReference
    ):
        refuse("finalization-memory-tree-unbound", certified_tree, inputs)
    code_head = _published_head(bundle, record, bundle.code, "code")
    memory_head = _published_head(bundle, record, bundle.memory, "memory")
    _physical_memory(bundle, record)
    for item in (bundle.code, bundle.memory, bundle.ledger):
        _output(item)
        observe_git_preparation_policy(Path(item.intent.logicalRoot)).require_intent(item.intent)
    operation = record.input
    assert isinstance(operation, CloseoutOperationInput)
    profile_ref = require_repo(
        load_config(operation.configPath), contract.repo_name
    ).certification_profile
    profile = load_repository_profile(contract.repo_name, contract.code_worktree, profile_ref)
    if profile.canonical != selected.run.repositoryProfile:
        refuse(
            "finalization-profile-moved",
            selected.run.repositoryProfile.profileDigest,
            profile.canonical.profileDigest,
        )
    rules = selected.authorities.semanticEnvelope.worktree
    current = observe_certification_candidate(
        CandidateObservationRequest(
            contract,
            operation.effectiveInput,
            selected.run.repositoryProfile,
            PreparedStagedCode(rules.stagedTree, rules.preCommitHookRan),
            selected.run.provenance,
        )
    )
    original = selected.authorities.semanticEnvelope
    # Only the current contract's two owned refs acquire their published tips.
    published_tips = {
        (contract.contract_path.as_posix(), bundle.code.intent.logicalRef, "code"): code_head,
        (contract.contract_path.as_posix(), bundle.memory.intent.logicalRef, "memory"): memory_head,
    }
    edges = tuple(
        edge.model_copy(
            update={
                "descendantTip": published_tips.get(
                    (edge.contractPath, edge.descendantRef, edge.side), edge.descendantTip
                )
            }
        )
        for edge in original.source.edges
    )
    source = original.source.model_copy(update={"edges": edges})
    worktree = rules.model_copy(update={"headCommit": code_head})
    envelope = original.model_copy(update={"source": source, "worktree": worktree})
    candidate = selected.admission.lifecycle.semanticEnvelope.candidate.model_copy(
        update={
            "sourceAuthorityDigest": content_digest(source),
            "worktreeRuleDigest": content_digest(worktree),
        }
    )
    if current.authorities.semanticEnvelope != envelope or current.candidate != candidate:
        refuse("finalization-candidate-authority-moved", candidate, current.candidate)
    require_current_route_review(contract)
    _approval(bundle, record, claim=False)
    return _owner(bundle)


def _record_proof(
    bundle: PreparedMemoryOutputs,
    selected: SelectedCloseoutPreparation,
    leg: CloseoutMutationLeg,
    proof: GitMutationEvidence,
) -> None:
    record = _owner(bundle)
    output = _output(selected)
    actual = ephemeral_git_mutation_snapshot(Path(selected.intent.logicalRoot))
    if (actual.head, actual.headTree, actual.indexTree, actual.candidateTree) != (
        output.commit,
        output.tree,
        output.tree,
        output.tree,
    ):
        refuse("finalization-published-output-moved", output.outputDigest, actual)
    mutations = dict(record.mutationEvidence)
    mutations[leg] = proof.model_copy(
        update={"state": "commit-proven", "observed": actual, "commit": output.commit}
    )
    recovery = derive_closeout_recovery_commits(record, mutations=mutations)
    _cas(
        bundle,
        record,
        {
            "mutationEvidence": mutations,
            "recoveryCommits": recovery,
            "irreversibleBoundaryEntered": True,
        },
    )


def _materialize_ledger(bundle: PreparedMemoryOutputs, record: LifecycleOperationRecord) -> None:
    selected = bundle.ledger
    root = Path(selected.intent.logicalRoot)
    classification = classify_closeout_ledger_recovery(bundle.handoff.contract, record)
    if classification.state not in {
        "accepted-before",
        "prepared-unstaged",
        "prepared-staged",
        "commit-proven-pending-publication",
    }:
        refuse(
            "finalization-ledger-prestate-refused",
            "exact retained ledger intent",
            classification.decision_payload(),
        )
    if classification.intended_text.encode("utf-8") != bundle.ledgerBytes:
        refuse(
            "finalization-ledger-bytes-mismatch",
            selected.intent.intentDigest,
            "different deterministic ledger",
        )
    ledger = parse_ledger_text(bundle.ledgerBytes.decode("utf-8"))
    if ledger_to_text(ledger).encode("utf-8") != bundle.ledgerBytes:
        refuse(
            "finalization-ledger-not-canonical",
            selected.intent.intentDigest,
            "different serialized bytes",
        )
    if classification.state == "accepted-before":
        _live(bundle)
        write_ledger(root / "memory.md", ledger)
    if classification.state in {"accepted-before", "prepared-unstaged"}:
        _live(bundle)
        require_git(root, ["add", "--", "memory.md"])
    if (root / "memory.md").read_bytes() != bundle.ledgerBytes:
        refuse("finalization-ledger-write-moved", selected.intent.intentDigest, "physical bytes")


def _retain_existing_leg(
    bundle: PreparedMemoryOutputs,
    selected: SelectedCloseoutPreparation,
    leg: CloseoutMutationLeg,
    record: LifecycleOperationRecord,
    output: PreparedCloseoutOutput,
) -> None:
    """Retain an unchanged output under the already observed live generation."""
    root = Path(selected.intent.logicalRoot)
    ledger_proof = record.mutationEvidence.get("ledger")
    if (
        leg == "memory"
        and ledger_proof is not None
        and ledger_proof.state in {"mutation-intent", "commit-proven"}
    ):
        if (
            record.recoveryCommits is None
            or record.recoveryCommits.memoryContentCommit != output.commit
        ):
            refuse(
                "finalization-existing-memory-proof-missing", output.commit, record.recoveryCommits
            )
        return
    head = require_git(root, ["rev-parse", "HEAD"])
    tree = require_git(root, ["rev-parse", "HEAD^{tree}"])
    expected_head = selected.intent.expectedOldCommit
    if head != expected_head:
        refuse("finalization-existing-head-moved", expected_head, head)
    inspect_existing_git_preparation(
        root,
        common_directory=Path(selected.intent.repositoryIdentity),
        logical_ref=selected.intent.logicalRef,
        commit=head,
        tree=tree,
    )
    field = {"code": "codeCommit", "memory": "memoryContentCommit", "ledger": "ledgerCommit"}[leg]
    cells = (
        record.recoveryCommits.model_dump()
        if record.recoveryCommits
        else {"codeCommit": "", "memoryContentCommit": "", "ledgerCommit": ""}
    )
    cells[field] = output.commit
    _cas(
        bundle, record, {"recoveryCommits": LifecycleOperationRecoveryCommits.model_validate(cells)}
    )
    return


def _ledger_publication_started(record: LifecycleOperationRecord) -> bool:
    """Ledger publication supersedes the shared memory ref's intermediate M output."""
    proof = record.mutationEvidence.get("ledger")
    return proof is not None and proof.state in {"mutation-intent", "commit-proven"}


def _observe_proven_leg(
    bundle: PreparedMemoryOutputs,
    selected: SelectedCloseoutPreparation,
    output: PreparedCloseoutOutput,
) -> None:
    """Reobserve one previously published ref through its original authority."""

    def authorize_existing(_binding: GitCloseoutPublicationBinding) -> None:
        _live(bundle)

    capability = admit_git_closeout_publication(_binding(selected), authorize=authorize_existing)
    if inspect_git_closeout_publication(capability).state != "new":
        refuse("finalization-proven-ref-moved", output.commit, "old ref")


def _publication_intent(
    bundle: PreparedMemoryOutputs,
    selected: SelectedCloseoutPreparation,
    leg: CloseoutMutationLeg,
    record: LifecycleOperationRecord,
    output: PreparedCloseoutOutput,
) -> tuple[LifecycleOperationRecord, GitMutationEvidence]:
    """Journal the exact prestate once before publishing a prepared output."""
    root = Path(selected.intent.logicalRoot)
    proof = record.mutationEvidence.get(leg)
    if proof is None or proof.state == "pre-mutation":
        before = ephemeral_git_mutation_snapshot(root)
        binding = _binding(selected)
        if before.head != binding.expected_old_commit:
            refuse("finalization-prestate-moved", binding.expected_old_commit, before)
        proof = GitMutationEvidence(
            leg=leg,
            repository=root.as_posix(),
            state="mutation-intent",
            acceptedBefore=None if proof is None else proof.acceptedBefore,
            before=before,
            expectedOutputTree=output.tree,
        )
        mutations = dict(record.mutationEvidence)
        mutations[leg] = proof
        record = _cas(
            bundle,
            record,
            {
                "mutationEvidence": mutations,
                "phase": "recovering-after-claim",
                "currentCommand": f"publish prepared {leg}",
            },
        )
    return record, proof


def _publish_leg(
    bundle: PreparedMemoryOutputs, selected: SelectedCloseoutPreparation, leg: CloseoutMutationLeg
) -> None:
    record = _live(bundle)
    output = _output(selected)
    root = Path(selected.intent.logicalRoot)
    if not selected.intent.writeEnabled:
        _retain_existing_leg(bundle, selected, leg, record, output)
        return
    record, proof = _publication_intent(bundle, selected, leg, record, output)
    if proof.state == "commit-proven":
        if leg == "memory" and _ledger_publication_started(record):
            return
        _observe_proven_leg(bundle, selected, output)
        return
    if proof.state != "mutation-intent":
        refuse("finalization-original-mutation-unavailable", "original mutation intent", proof)
    if leg == "ledger":
        _materialize_ledger(bundle, record)
    elif require_git(root, ["rev-parse", "HEAD"]) == selected.intent.expectedOldCommit:
        actual = ephemeral_git_mutation_snapshot(root)
        if actual != proof.before:
            refuse("finalization-original-prestate-moved", proof.before, actual)

    def authorize(_binding: GitCloseoutPublicationBinding) -> None:
        _live(bundle)

    capability = admit_git_closeout_publication(_binding(selected), authorize=authorize)
    result = publish_git_closeout_ref(capability)
    if result.after.state != "new" or (
        result.command is not None and result.command.returncode != 0
    ):
        refuse("finalization-ref-command-failed", output.commit, result.after.commit)
    _record_proof(bundle, selected, leg, proof)


def _closed_payload(
    contract: WorktreeContract, record: LifecycleOperationRecord
) -> WorktreeCommandResult:
    commits = record.recoveryCommits
    if commits is None:
        refuse("finalization-recovery-tuple-missing", "C/M/L", None)
    prove_closeout_recovery_commits(contract, commits)
    return WorktreeCommandResult(
        0,
        {
            "state": "closed",
            **status_payload(contract),
            "summary": "Closeout completed; integrate the task branches into their source branches.",
            "code_commit": commits.codeCommit,
            "memory_content_commit": commits.memoryContentCommit,
            "ledger_commit": commits.ledgerCommit,
        },
    )


def finalize_prepared_closeout(bundle: PreparedMemoryOutputs) -> WorktreeCommandResult:
    """Claim once, publish exact C/M/L in order, then finalize the canonical contract."""
    record = _live(bundle)
    if not record.approvalClaimed:
        _approval(bundle, record, claim=True)
        record = _cas(
            bundle, _owner(bundle), {"approvalClaimed": True, "phase": "recovering-after-claim"}
        )
    legs: tuple[tuple[CloseoutMutationLeg, SelectedCloseoutPreparation], ...] = (
        ("code", bundle.code),
        ("memory", bundle.memory),
        ("ledger", bundle.ledger),
    )
    for leg, selected in legs:
        _publish_leg(bundle, selected, leg)
    record = _live(bundle)
    commits = record.recoveryCommits
    if commits is None:
        refuse("finalization-recovery-tuple-missing", "C/M/L", None)
    memory = prove_closeout_recovery_commits(bundle.handoff.contract, commits)
    operation = record.input
    assert isinstance(operation, CloseoutOperationInput)
    updated = _amended_closeout_contract(
        bundle.handoff.contract, operation.approvalNote, commits.codeCommit, memory, True
    )
    digest = closeout_contract_sha256(updated)
    if record.closeoutFinalizedContractSha256 not in (None, digest):
        refuse("finalization-contract-intent-moved", record.closeoutFinalizedContractSha256, digest)
    record = _cas(
        bundle,
        record,
        {"phase": "contract-finalization", "closeoutFinalizedContractSha256": digest},
    )
    _live(bundle)
    write_contract(updated.contract_path, updated)
    if closeout_contract_sha256(load_contract(updated.contract_path)) != digest:
        refuse("finalization-contract-readback-moved", digest, "changed")
    return _closed_payload(updated, record)


def resume_prepared_closeout(
    contract: WorktreeContract, record: LifecycleOperationRecord, store: LifecycleOperationStore
) -> WorktreeCommandResult | None:
    """Recover only this generation's selected publication, before original-head admission."""
    current = store.read()
    if (
        current is None
        or current.preparation is None
        or not (current.approvalClaimed or current.closeoutFinalizedContractSha256)
    ):
        return None
    if (
        (current.operationKey, current.generation, current.workerPid, current.workerLease)
        != (record.operationKey, record.generation, record.workerPid, record.workerLease)
        or current.status != "running"
        or current.cancelRequested
    ):
        refuse("finalization-worker-moved", record.operationKey, current)
    if current.closeoutFinalizedContractSha256 == closeout_contract_sha256(contract):
        return _closed_payload(contract, current)
    selected = require_selected_certification(contract, current)
    handoff = CloseoutCertificationHandoff(contract, current, store, selected)
    intents = selected_preparation_intents(contract, current)
    if tuple(item.leg for item in intents) != ("code", "memory-content", "ledger") or any(
        item.output is None for item in current.preparation.legs
    ):
        refuse("finalization-selected-output-prefix-missing", "C/M/L", current.preparation)
    wrappers = tuple(
        SelectedCloseoutPreparation(handoff, intent, leg.intent)
        for intent, leg in zip(intents, current.preparation.legs, strict=True)
    )
    ledger_output = _output(wrappers[2])
    root = Path(wrappers[2].intent.logicalRoot)
    ledger_blob = require_git(root, ["rev-parse", f"{ledger_output.tree}:memory.md"])
    bundle = PreparedMemoryOutputs(
        handoff, wrappers[0], wrappers[1], wrappers[2], read_git_blob_bytes(root, ledger_blob)
    )
    return finalize_prepared_closeout(bundle)
