"""Default continuation from selected code gates to real memory and prepared publication."""

from __future__ import annotations

from agents_remember.certification.certificate_models import GateFiveSemanticInputs
from agents_remember.worktrees.integration.closeout.certification.execution import CloseoutCertificationHandoff
from agents_remember.worktrees.integration.closeout.certification.observation import refuse
from agents_remember.worktrees.modules.models import WorktreeCommandResult
from agents_remember.worktrees.services import worktree_services

from .code_view import observe_prepared_code_view, prepare_code_view
from .finalization import finalize_prepared_closeout
from .memory_execution import certify_prepared_memory, observe_prepared_memory_candidate
from .memory_output import prepare_memory_outputs
from .memory_port import PreparedMemoryCertificationRequest, PreparedMemoryCertificationResult


class PreparedCloseoutContinuation:
    """Use actual installed producers and exact selected originals; never invent a green gate."""

    def observe_memory(self, handoff: CloseoutCertificationHandoff) -> GateFiveSemanticInputs | None:
        producer = worktree_services().prepared_memory_certification
        if producer is None:
            refuse("prepared-memory-certification-unavailable", "registered production memory producer", None)
        view = observe_prepared_code_view(handoff)
        candidate = observe_prepared_memory_candidate(handoff, view)
        return producer.observe(PreparedMemoryCertificationRequest(handoff, candidate))

    def run_memory(self, handoff: CloseoutCertificationHandoff) -> WorktreeCommandResult:
        result = certify_prepared_memory(handoff)
        return finalize_prepared_closeout(prepare_memory_outputs(result))

    def finalize(self, handoff: CloseoutCertificationHandoff) -> WorktreeCommandResult:
        current, view = prepare_code_view(handoff)
        candidate = observe_prepared_memory_candidate(current, view)
        if len(current.selected.terminals) != 5:
            refuse("finalization-certificate-prefix-incomplete", "selected fifth certificate", current.selected.state)
        terminal = current.selected.terminals[-1]
        if terminal.certificate is None or terminal.certificateReference is None or terminal.certificate.semanticEnvelope.gateFiveInputs is None:
            refuse("finalization-memory-certificate-missing", "original green fifth certificate", terminal.resultReference)
        result = PreparedMemoryCertificationResult(
            current, candidate, terminal.certificate.semanticEnvelope.gateFiveInputs,
            terminal.resultReference, terminal.certificateReference,
        )
        return finalize_prepared_closeout(prepare_memory_outputs(result))
