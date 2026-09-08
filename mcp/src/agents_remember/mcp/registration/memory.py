"""Memory-root tools: drift, quality, route index, init, baseline, and carryover."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from agents_remember.application.memory_tools import (
    CarryoverCommitMessages,
    CarryoverSelection,
    CitationOperationScope,
    MemoryBranches,
)
from agents_remember.kernel.primitives.runtime_config import McpRuntimeConfig
from agents_remember.models.memory import (
    MemoryQualityCheckRequest,
    MemoryQualityPollRequest,
    MemoryQualityStartRequest,
)

from ..tools import (
    citation_fix_payload,
    drift_check_payload,
    memory_baseline_adopt_payload,
    memory_baseline_status_payload,
    memory_carryover_apply_payload,
    memory_carryover_plan_payload,
    memory_init_payload,
    memory_quality_check_payload,
    memory_quality_check_poll_payload,
    memory_quality_check_start_payload,
    route_index_refresh_payload,
)


def register_memory_tools(server: FastMCP, config: McpRuntimeConfig) -> None:
    """Register the memory tools, split by what they do to the memory root."""
    _register_memory_health_tools(server, config)
    _register_memory_baseline_tools(server, config)
    _register_memory_carryover_tools(server, config)


def _register_memory_health_tools(server: FastMCP, config: McpRuntimeConfig) -> None:
    """Read the memory root's health: drift, quality findings, route indexes.

    Each takes the same optional `contract_path` the worktree verbs take. Omitted, it means
    the configured official memory repo, exactly as before. Supplied, the tool acts on that
    leaf enclosure's memory worktree and measures it against the leaf's code worktree -- which
    is what lets a curator check its own change-set before handing it back, instead of the
    manager discovering the findings at the commit gate.
    """

    @server.tool()
    def drift_check(
        repo_id: str,
        detail_limit: int = 50,
        contract_path: str | None = None,
    ) -> dict[str, Any]:
        """Task-start gate: classify how far onboarding has drifted from the code since it was last
        verified. Read-only (writes only a temp drift report). A nonzero actionable count is
        expected after code changes, not a failure. Pass `contract_path` (a leaf enclosure
        contract) to check that leaf's memory worktree; omit it for the official memory repo."""
        return drift_check_payload(
            config, repo_id, detail_limit=detail_limit, contract_path=contract_path
        )

    @server.tool()
    def memory_quality_check(
        request: MemoryQualityCheckRequest,
    ) -> dict[str, Any]:
        """Prepare memory before certification admission using drift-integrity and style checks.
        It never changes code or memory. A full contract-scoped call atomically replaces the one
        operational checklist at `<worktree enclosure>/reports/curator-memory-quality.md` and its
        structured, report-digest-bound `.json` attestation; subset and unscoped calls write
        neither. Every contract-scoped execution also publishes the complete structural
        census to reports/memory-census.json and returns bounded memoryCensus diagnostics.
        This worklist requires no code-gate results and does not certify semantic decisions.
        ok=false means findings exist (e.g. dirty-source
        drift), not that the tool failed. `curatorActionableCount` and
        `qualityChecklistStatus` are the onboarding-repair loop gate. Once the raw checklist is
        `ready-for-closeout`, combined `checklistStatus=coherence-required` means the caller must
        prepare, publish, and validate the exact structured `curator_coherence` authority;
        `closeoutReady=true` is impossible until that same validator accepts it. Checklist and
        attestation rendering are deterministic, so a same-input rerun preserves the published
        authority; changed inputs stale it and require republishing. Final commit stamps remain
        closeout-owned. The request is exactly one discriminated mode:
        `sync` and `start` carry repository plus optional scope/check/detail fields, while
        `poll` carries repository, run id, and the same contract path for a candidate run.
        Repository-only calls are explicitly official diagnostics and cannot become candidate
        acceptance. A saturated unique start returns `capacity-reached` without launching work.
        A poll returns the identical pair-bound result; `run-not-found` means the run was evicted,
        belongs to another repository, or the server restarted, so submit a new start request
        with the original contract path for a candidate run."""
        if isinstance(request, MemoryQualityPollRequest):
            return memory_quality_check_poll_payload(config, request)
        if isinstance(request, MemoryQualityStartRequest):
            return memory_quality_check_start_payload(config, request)
        return memory_quality_check_payload(config, request)

    @server.tool()
    def citation_fix(
        repo_id: str,
        contract_path: str,
        document: str | None = None,
        expected_snapshot: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Regenerate anchored citation ranges inside one leaf memory worktree. The enclosure
        contract is mandatory and the application guard refuses the official memory repo. A
        pure move is repaired; renamed, deleted, or ambiguous anchors remain a curator worklist.
        Preview with dry_run=true. Use document for one onboarding-relative file and
        expected_snapshot to assert a previously built immutable source generation."""
        return citation_fix_payload(
            config,
            repo_id,
            contract_path=contract_path,
            operation_scope=CitationOperationScope(
                document=document,
                expected_snapshot=expected_snapshot,
            ),
            dry_run=dry_run,
        )

    @server.tool()
    def route_index_refresh(
        repo_id: str,
        dry_run: bool = False,
        contract_path: str | None = None,
    ) -> dict[str, Any]:
        """Regenerate the overview.index.json route indexes so they match the current onboarding
        tree. Apply requires a leaf enclosure contract and writes only that leaf's memory
        worktree. An unscoped call is read-only and therefore requires dry_run=true."""
        return route_index_refresh_payload(
            config, repo_id, dry_run=dry_run, contract_path=contract_path
        )


def _register_memory_baseline_tools(server: FastMCP, config: McpRuntimeConfig) -> None:
    """Stand a memory root up and give it its first ledgered baseline."""

    @server.tool()
    def memory_init(
        repo_id: str,
        dry_run: bool = False,
        initialize_git: bool = True,
    ) -> dict[str, Any]:
        """Initialize or repair a repository's memory root (scaffold system/ files, onboarding
        layout, optionally `git init`). Does not overwrite existing onboarding content. Preview
        with dry_run=true. Usually driven by the c-00-initialize-memory-repo skill."""
        return memory_init_payload(
            config,
            repo_id,
            dry_run=dry_run,
            initialize_git=initialize_git,
        )

    @server.tool()
    def memory_baseline_status(repo_id: str) -> dict[str, Any]:
        """Report drift and ledger state to decide whether an external-memory baseline can be
        adopted. Read-only."""
        return memory_baseline_status_payload(config, repo_id)

    @server.tool()
    def memory_baseline_adopt(
        repo_id: str,
        accept_drift: bool = False,
        source_branch: str | None = None,
        work_branch: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create the first ledgered memory baseline for an external memory repo. Mutating: writes
        the ledger and commits memory. Gated on clean drift unless accept_drift=true. Preview with
        dry_run=true. Usually driven by the c-10-adopt-memory-baseline skill."""
        return memory_baseline_adopt_payload(
            config,
            repo_id,
            accept_drift=accept_drift,
            branches=MemoryBranches(source_branch=source_branch, work_branch=work_branch),
            dry_run=dry_run,
        )


def _register_memory_carryover_tools(server: FastMCP, config: McpRuntimeConfig) -> None:
    """Plan and apply landed onboarding into an ordinary recovery leaf."""

    @server.tool()
    def memory_carryover_plan(
        repo_id: str,
        *,
        contract_path: str,
        source_memory: str,
        official_code_ref: str,
        source_code_ref: str,
        old_base: str,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """Plan (non-mutating) carrying richer onboarding from a source branch into the exact open
        external-memory leaf. Review it, then apply before normal leaf closeout/integration."""
        return memory_carryover_plan_payload(
            config,
            CarryoverSelection(
                repo_id=repo_id,
                contract_path=contract_path,
                source_memory=source_memory,
                official_code_ref=official_code_ref,
                source_code_ref=source_code_ref,
                old_base=old_base,
                replace_existing=replace_existing,
            ),
        )

    @server.tool()
    def memory_carryover_apply(
        repo_id: str,
        *,
        contract_path: str,
        source_memory: str,
        official_code_ref: str,
        source_code_ref: str,
        old_base: str,
        intent_note: str,
        replace_existing: bool = False,
        include_review_required: list[str] | None = None,
        memory_commit_message: str = "Carry over landed branch memory",
        ledger_commit_message: str = "Record branch memory carryover",
    ) -> dict[str, Any]:
        """Apply an approved plan inside the exact ordinary recovery leaf, committing its memory and
        ledger work branches. Mutating and approval-gated; requires intent_note. Close and integrate
        the leaf normally afterward."""
        return memory_carryover_apply_payload(
            config,
            CarryoverSelection(
                repo_id=repo_id,
                contract_path=contract_path,
                source_memory=source_memory,
                official_code_ref=official_code_ref,
                source_code_ref=source_code_ref,
                old_base=old_base,
                replace_existing=replace_existing,
            ),
            intent_note=intent_note,
            include_review_required=include_review_required,
            messages=CarryoverCommitMessages(
                memory=memory_commit_message, ledger=ledger_commit_message
            ),
        )
