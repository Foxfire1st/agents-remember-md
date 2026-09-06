"""Prepare exact post-certification M/L objects without publishing logical refs."""

from __future__ import annotations

import base64

from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from agents_remember.kernel.git_command import (
    read_git_blob_bytes,
    read_git_commit_bytes,
    run_git,
    run_git_with_index,
)
from agents_remember.kernel.memory_ledger import (
    find_mapping,
    ledger_to_text,
    parse_ledger_text,
    prepend_mapping,
)
from agents_remember.models.lifecycles.evidence_dependencies import canonical_sha256
from agents_remember.models.lifecycles.operation import CloseoutOperationInput
from agents_remember.models.lifecycles.preparation import (
    CloseoutPreparationIntent,
    ExistingMemoryPreparationProof,
    PreparedCloseoutOutput,
)
from agents_remember.worktrees.integration.closeout.certification.execution import (
    CloseoutCertificationHandoff,
)
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.integration.closeout.certification.selection import load_typed
from agents_remember.worktrees.integration.closeout.preparation_selection import (
    select_preparation_intent,
    selected_preparation_intents,
)
from agents_remember.worktrees.integration.lifecycle.lifecycle_operation_identity import (
    closeout_contract_sha256,
)
from agents_remember.worktrees.modules.git import require_git
from agents_remember.worktrees.modules.quality.certification_records import certificate_store

from .code_output import select_code_preparation
from .memory_execution import current_prepared_memory_result
from .memory_port import PreparedMemoryCertificationResult
from .memory_reuse import observe_existing_memory_proof
from .output_selection import retain_prepared_output
from .policy import observe_git_preparation_policy
from .private_execution import prepare_private_output
from .selected import SelectedCloseoutPreparation


@dataclass(frozen=True)
class PreparedMemoryOutputs:
    handoff: CloseoutCertificationHandoff
    code: SelectedCloseoutPreparation
    memory: SelectedCloseoutPreparation
    ledger: SelectedCloseoutPreparation
    ledgerBytes: bytes


def _intent(
    result: PreparedMemoryCertificationResult,
    *,
    leg: str,
    parent: str,
    tree: str,
    proof: ExistingMemoryPreparationProof | None,
    enabled: bool,
) -> CloseoutPreparationIntent:
    handoff = result.handoff
    contract = handoff.contract
    root = contract.memory_worktree
    if root is None or not isinstance(handoff.record.input, CloseoutOperationInput):
        refuse("prepared-memory-route", "external-memory closeout", root)
    effective = handoff.record.input.effectiveInput
    input_leg = "memory" if leg == "memory-content" else "ledger"
    if enabled and not effective.enabled(input_leg):
        refuse("prepared-memory-write-disabled", input_leg, effective)
    policy = observe_git_preparation_policy(root)
    private = (
        contract.worktree_group / "preparation" / handoff.record.operationKey
        / f"generation-{handoff.record.generation}" / leg
    )
    payload = {
        "schemaVersion": "closeout-preparation-intent/v1",
        "operationKey": handoff.record.operationKey,
        "generation": handoff.record.generation,
        "contractPath": contract.contract_path.as_posix(),
        "contractSha256": closeout_contract_sha256(contract),
        "leg": leg,
        "writeEnabled": enabled,
        "repositoryIdentity": require_git(
            root, ["rev-parse", "--path-format=absolute", "--git-common-dir"]
        ),
        "logicalRoot": root.as_posix(),
        "logicalRef": f"refs/heads/{contract.memory_work_branch}",
        "expectedOldCommit": require_git(root, ["rev-parse", "--verify", "HEAD"]),
        "parentCommit": parent,
        "admittedTree": tree,
        "privateRoot": private.as_posix() if enabled else None,
        "normalizedMessage": effective.message_for(input_leg) if enabled else None,
        "hookPolicy": "ordinary",
        "gitConfigSha256": policy.git_config_sha256,
        "hooksSha256": policy.hooks_sha256,
        "frozenRun": handoff.selected.state.frozenRun.model_dump(mode="json"),
        "candidateAuthorities": handoff.selected.state.candidateAuthorities.model_dump(mode="json"),
        "prefixCertificates": [
            item.certificateReference.model_dump(mode="json")
            for item in handoff.selected.terminals[:4]
            if item.certificateReference is not None
        ],
        "gateFiveCertificate": result.certificate.model_dump(mode="json"),
        "existingMemoryProof": None if proof is None else proof.model_dump(mode="json"),
    }
    return CloseoutPreparationIntent.model_validate(
        {**payload, "intentDigest": canonical_sha256(payload)}
    )


def _selected(
    result: PreparedMemoryCertificationResult, intent: CloseoutPreparationIntent
) -> SelectedCloseoutPreparation:
    handoff = result.handoff
    objects = certificate_store(handoff.contract.worktree_group)
    intents = selected_preparation_intents(handoff.contract, handoff.record)
    for index, existing in enumerate(intents):
        if existing.leg == intent.leg:
            if existing != intent:
                refuse("prepared-memory-intent-moved", existing.intentDigest, intent.intentDigest)
            assert handoff.record.preparation is not None
            return SelectedCloseoutPreparation(
                handoff, intent, handoff.record.preparation.legs[index].intent
            )
    objects.publish(intent)
    reference = objects.reference("preparation-intent", intent.intentDigest)
    record = select_preparation_intent(handoff.contract, handoff.store, handoff.record, reference)
    return SelectedCloseoutPreparation(replace(handoff, record=record), intent, reference)


def _output(selected: SelectedCloseoutPreparation) -> PreparedCloseoutOutput:
    assert selected.handoff.record.preparation is not None
    state = next(
        item for item in selected.handoff.record.preparation.legs
        if item.leg == selected.intent.leg
    )
    if state.output is None:
        refuse("prepared-memory-output-missing", selected.intent.leg, None)
    return load_typed(
        certificate_store(selected.handoff.contract.worktree_group),
        state.output,
        PreparedCloseoutOutput,
    )


def _prepare(
    result: PreparedMemoryCertificationResult, intent: CloseoutPreparationIntent
) -> SelectedCloseoutPreparation:
    selected = _selected(result, intent)

    def reobserve(observed: SelectedCloseoutPreparation) -> SelectedCloseoutPreparation:
        actual = current_prepared_memory_result(replace(result, handoff=observed.handoff))
        observe_git_preparation_policy(Path(intent.logicalRoot)).require_intent(intent)
        originals = selected_preparation_intents(actual.handoff.contract, actual.handoff.record)
        if intent not in originals:
            refuse("prepared-memory-selection-moved", intent.intentDigest, originals)
        if intent.existingMemoryProof is not None:
            proof = observe_existing_memory_proof(
                Path(intent.logicalRoot), repository_name=actual.handoff.contract.repo_name,
                code_commit=actual.candidate.codeView.codeCommit,
                certified_tree=actual.candidate.memoryTree,
            )
            if proof != intent.existingMemoryProof:
                refuse("prepared-memory-reuse-moved", intent.existingMemoryProof, proof)
        return replace(observed, handoff=actual.handoff)

    selected = reobserve(selected)
    state = next(
        item for item in selected.handoff.record.preparation.legs
        if item.leg == intent.leg
    )
    if state.output is not None:
        # Previously selected output is immutable. Physical reproof is still required.
        if intent.writeEnabled:
            from .private_execution import observe_private_output
            observe_private_output(selected, reobserve=reobserve)
        else:
            raw = read_git_commit_bytes(Path(intent.logicalRoot), _output(selected).commit)
            if raw != base64.b64decode(_output(selected).rawCommitBase64, validate=True):
                refuse("prepared-memory-existing-output-moved", state.output, "raw bytes")
        return reobserve(selected)
    if intent.writeEnabled:
        selected, raw = prepare_private_output(selected, reobserve=reobserve)
    else:
        commit = (
            intent.existingMemoryProof.memoryContentCommit
            if intent.leg == "memory-content" and intent.existingMemoryProof is not None
            else intent.expectedOldCommit
        )
        raw = read_git_commit_bytes(Path(intent.logicalRoot), commit)
    selected, _, _ = retain_prepared_output(selected, raw, reobserve=reobserve)
    return selected


def _ledger_tree(root: Path, group: Path, parent: str, content: bytes) -> str:
    blob = run_git(root, ["hash-object", "-w", "--stdin"], input_text=content.decode("utf-8"))
    if blob.returncode != 0:
        refuse("prepared-ledger-blob-failed", "written exact ledger blob", blob.returncode)
    with TemporaryDirectory(prefix="prepared-ledger-index-", dir=group) as directory:
        index = Path(directory) / "index"
        tree = ""
        for args in (
            ["read-tree", parent],
            ["update-index", "--add", "--cacheinfo", f"100644,{blob.stdout.strip()},memory.md"],
            ["write-tree"],
        ):
            command = run_git_with_index(root, args, index)
            if command.returncode != 0:
                refuse("prepared-ledger-tree-failed", args, command.returncode)
            tree = command.stdout.strip()
        return tree


def prepare_memory_outputs(result: PreparedMemoryCertificationResult) -> PreparedMemoryOutputs:
    """Retain genuine C/M/L outputs in order, leaving both logical branches untouched."""
    result = current_prepared_memory_result(result)
    code = select_code_preparation(result.handoff)
    root = result.handoff.contract.memory_worktree
    if root is None:
        refuse("prepared-memory-route", "external-memory closeout", None)
    head = require_git(root, ["rev-parse", "--verify", "HEAD"])
    proof = observe_existing_memory_proof(
        root, repository_name=result.handoff.contract.repo_name,
        code_commit=result.candidate.codeView.codeCommit,
        certified_tree=result.candidate.memoryTree,
    )
    memory_intent = _intent(
        result, leg="memory-content", parent=head if proof is None else proof.memoryContentCommit,
        tree=result.candidate.memoryTree if proof is None else proof.memoryContentTree,
        proof=proof, enabled=proof is None,
    )
    memory = _prepare(result, memory_intent)
    result = current_prepared_memory_result(replace(result, handoff=memory.handoff))
    memory_output = _output(memory)
    ledger_blob = require_git(root, ["rev-parse", f"{result.candidate.memoryTree}:memory.md"])
    old_bytes = read_git_blob_bytes(root, ledger_blob)
    ledger = parse_ledger_text(old_bytes.decode("utf-8"))
    if ledger.repo_name != result.handoff.contract.repo_name:
        refuse("prepared-ledger-repository", result.handoff.contract.repo_name, ledger.repo_name)
    mapping = find_mapping(ledger, result.candidate.codeView.codeCommit)
    reuse = proof is not None and mapping is not None and mapping.memory_commit == memory_output.commit
    ledger_bytes = old_bytes if reuse else ledger_to_text(
        prepend_mapping(ledger, result.candidate.codeView.codeCommit, memory_output.commit)
    ).encode("utf-8")
    parent = memory_output.commit if memory_intent.writeEnabled else head
    tree = (
        proof.logicalHeadTree if reuse and proof is not None
        else _ledger_tree(root, result.handoff.contract.worktree_group, parent, ledger_bytes)
    )
    ledger_intent = _intent(
        result, leg="ledger", parent=parent, tree=tree, proof=proof, enabled=not reuse
    )
    final = _prepare(result, ledger_intent)
    return PreparedMemoryOutputs(
        final.handoff,
        replace(code, handoff=final.handoff),
        replace(memory, handoff=final.handoff),
        final,
        ledger_bytes,
    )
