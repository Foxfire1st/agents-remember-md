"""Actual memory certification, private Git outputs, and interrupted publication recovery."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from agents_remember.worktrees.worktree_contract import load_contract
from test_closeout_memory_certification_reuse import _fixture
from test_worktree_support import git

pytestmark = pytest.mark.integration


def test_real_gate_five_prepares_then_publishes_memory_and_ledger(tmp_path: Path) -> None:
    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "from agents_remember_test_support.testing.global_state import begin_pytest_process",
            "from agents_remember.application.worktree_services import build_default_worktree_services",
            "from agents_remember.worktrees.services import bind_worktree_services",
            "begin_pytest_process()",
            "bind_worktree_services(build_default_worktree_services())",
            f"from test_prepared_publication_recovery import {_prepare_memory_output_scenario.__name__}",
            "_prepare_memory_output_scenario(Path(sys.argv[1]))",
        )
    )
    with subprocess.Popen(
        [sys.executable, "-B", "-c", script, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=900)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            pytest.fail(f"memory preparation worker timed out\n{stdout}\n{stderr}")
        assert process.returncode == 0, stdout + stderr


def _prepare_memory_output_scenario(root: Path) -> None:
    from agents_remember.kernel.memory_ledger import (  # noqa: PLC0415
        find_mapping,
        parse_ledger_text,
    )
    from agents_remember.memory_quality.prepared_certification import (  # noqa: PLC0415
        PreparedMemoryCertificationAdapter,
    )
    from agents_remember.worktrees.integration.closeout.preparation.code_view import (  # noqa: PLC0415
        prepare_code_view,
    )
    from agents_remember.worktrees.integration.closeout.preparation.memory_execution import (  # noqa: PLC0415
        observe_prepared_memory_candidate,
    )
    from agents_remember.worktrees.integration.closeout.preparation.memory_output import (  # noqa: PLC0415
        prepare_memory_outputs,
    )
    from agents_remember.worktrees.integration.closeout.preparation.memory_port import (  # noqa: PLC0415
        PreparedMemoryCertificationRequest,
    )

    with pytest.MonkeyPatch.context() as patch:
        fixture = _fixture(root, patch)
    # Restore the scheduler-only code-view stub before exercising the actual owner.
    handoff, view = prepare_code_view(fixture.handoff)
    assert view.disposition == "existing"
    candidate = observe_prepared_memory_candidate(handoff, view)
    assert len(handoff.selected.terminals) == 4
    request = PreparedMemoryCertificationRequest(handoff, candidate)
    adapter = PreparedMemoryCertificationAdapter()
    certified = adapter.certify(request)
    assert adapter.observe(replace(request, handoff=certified.handoff)) == certified.memoryInputs
    memory_root = handoff.contract.memory_worktree
    assert memory_root is not None
    roots = (handoff.contract.code_worktree, memory_root)
    before = tuple(git(path, "rev-parse", "HEAD") for path in roots)
    ledger_before = (memory_root / "memory.md").read_bytes()
    prepared = prepare_memory_outputs(certified)
    assert prepared.memory.intent.writeEnabled and prepared.ledger.intent.writeEnabled
    memory_private = Path(prepared.memory.intent.privateRoot or "")
    ledger_private = Path(prepared.ledger.intent.privateRoot or "")
    memory_commit = git(memory_private, "rev-parse", "HEAD")
    ledger_commit = git(ledger_private, "rev-parse", "HEAD")
    assert git(memory_private, "rev-parse", "HEAD^{tree}") == candidate.memoryTree
    assert git(memory_private, "rev-parse", "HEAD^") == before[1]
    assert git(ledger_private, "rev-parse", "HEAD^") == memory_commit
    assert git(memory_root, "diff", "--name-only", memory_commit, ledger_commit) == "memory.md"
    mapping = find_mapping(parse_ledger_text(prepared.ledgerBytes.decode()), view.codeCommit)
    assert mapping is not None and mapping.memory_commit == memory_commit
    assert (ledger_private / "memory.md").read_bytes() == prepared.ledgerBytes
    journal = handoff.store.path.read_bytes()
    repeated = prepare_memory_outputs(replace(certified, handoff=prepared.handoff))
    assert repeated == prepared
    assert handoff.store.path.read_bytes() == journal
    assert tuple(git(path, "rev-parse", "HEAD") for path in roots) == before
    assert (memory_root / "memory.md").read_bytes() == ledger_before

    _publish_and_recover_memory_outputs(
        prepared, view.codeCommit, memory_commit, ledger_commit, ledger_before
    )


def _interrupt_and_resume_memory_publication(
    prepared, memory_commit: str, ledger_commit: str, ledger_before: bytes
):
    from agents_remember.worktrees.integration.closeout.preparation import (  # noqa: PLC0415
        finalization,
    )
    from agents_remember.worktrees.integration.closeout.preparation.continuation import (  # noqa: PLC0415
        PreparedCloseoutContinuation,
    )

    handoff = prepared.handoff
    memory_root = handoff.contract.memory_worktree
    assert memory_root is not None
    publish = finalization.publish_git_closeout_ref
    prove = finalization._record_proof
    publications: list[str] = []

    def interrupt_after_publication(capability):
        result = publish(capability)
        if result.command is not None:
            assert result.command.returncode == 0 and result.after.state == "new"
            publications.append(capability.binding.prepared_commit)
            raise RuntimeError("interrupt after actual ref publication")
        return result

    def interrupt_after_memory_proof(bundle, selected, leg, proof):
        prove(bundle, selected, leg, proof)
        if leg == "memory":
            raise RuntimeError("interrupt after actual memory proof")

    def resume():
        current = handoff.store.read()
        assert current is not None
        return finalization.resume_prepared_closeout(handoff.contract, current, handoff.store)

    with mock.patch.object(finalization, "publish_git_closeout_ref", interrupt_after_publication):
        with pytest.raises(RuntimeError, match="interrupt after actual ref publication"):
            PreparedCloseoutContinuation().finalize(handoff)
        interrupted = handoff.store.read()
        assert interrupted is not None
        assert interrupted.mutationEvidence["memory"].state == "mutation-intent"
        assert git(memory_root, "rev-parse", "HEAD") == memory_commit
        assert (memory_root / "memory.md").read_bytes() == ledger_before
        with (
            mock.patch.object(finalization, "_record_proof", interrupt_after_memory_proof),
            pytest.raises(RuntimeError, match="interrupt after actual memory proof"),
        ):
            resume()
        proven_memory = handoff.store.read()
        assert proven_memory is not None
        assert proven_memory.mutationEvidence["memory"].state == "commit-proven"
        assert proven_memory.mutationEvidence["ledger"].state == "pre-mutation"
        assert publications == [memory_commit]
        # This resume must reobserve the proven M ref before first publishing L.
        with pytest.raises(RuntimeError, match="interrupt after actual ref publication"):
            resume()
        interrupted_ledger = handoff.store.read()
        assert interrupted_ledger is not None
        assert interrupted_ledger.mutationEvidence["ledger"].state == "mutation-intent"
        assert git(memory_root, "rev-parse", "HEAD") == ledger_commit
        closed = resume()
    assert publications == [memory_commit, ledger_commit]
    return closed


def _publish_and_recover_memory_outputs(
    prepared, code_commit: str, memory_commit: str, ledger_commit: str, ledger_before: bytes
) -> None:
    from agents_remember.worktrees.integration.closeout.preparation import (  # noqa: PLC0415
        finalization,
    )

    handoff = prepared.handoff
    memory_root = handoff.contract.memory_worktree
    assert memory_root is not None
    closed = _interrupt_and_resume_memory_publication(
        prepared, memory_commit, ledger_commit, ledger_before
    )
    assert closed is not None and closed.returncode == 0
    assert git(handoff.contract.code_worktree, "rev-parse", "HEAD") == code_commit
    assert git(memory_root, "rev-parse", "HEAD") == ledger_commit
    assert (memory_root / "memory.md").read_bytes() == prepared.ledgerBytes
    contract = load_contract(handoff.contract.contract_path)
    assert contract.closeout_status == "completed"
    assert (contract.code_commit, contract.memory_content_commit, contract.ledger_commit) == (
        code_commit,
        memory_commit,
        ledger_commit,
    )
    current = handoff.store.read()
    assert current is not None
    journal = handoff.store.path.read_bytes()
    reopened = finalization.resume_prepared_closeout(contract, current, handoff.store)
    assert reopened == closed
    assert handoff.store.path.read_bytes() == journal
    assert git(memory_root, "rev-parse", "HEAD") == ledger_commit
