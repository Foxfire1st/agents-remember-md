from __future__ import annotations

import asyncio
import json

import pytest
from agents_remember.models.core import ServingBuildPayload
from agents_remember.models.memory import (
    MemoryQualityCheckRequest,
    MemoryQualityPollRequest,
    MemoryQualityStartRequest,
    MemoryQualitySyncRequest,
)
from pydantic import TypeAdapter, ValidationError
from test_mcp_registration_wiring import RegistrationWiringTests


class RegistrationWiringTests1(RegistrationWiringTests):
    def test_citation_fix_forwards_the_guarded_leaf_scope(self) -> None:
        recorder = self.invoke(
            "citation_fix",
            "agents_remember.mcp.registration.memory.citation_fix_payload",
            {
                "repo_id": "agents-remember",
                "contract_path": "/tmp/contract.yaml",
                "document": "mcp/example.py.md",
                "expected_snapshot": "a" * 64,
                "dry_run": True,
            },
        )

        self.assertEqual(recorder.args[:2], (self.config, "agents-remember"))
        self.assertEqual(recorder.kwargs["contract_path"], "/tmp/contract.yaml")
        self.assertIs(recorder.kwargs["dry_run"], True)
        scope = recorder.kwargs["operation_scope"]
        self.assertEqual(scope.document, "mcp/example.py.md")
        self.assertEqual(scope.expected_snapshot, "a" * 64)

    @pytest.mark.integration
    @pytest.mark.usefixtures("worktree_services")
    def test_ping_takes_no_configuration(self) -> None:
        """``ping`` is a liveness check: it must not depend on resolved settings."""
        recorder = self.invoke("ping", "agents_remember.mcp.registration.core.ping_payload")

        self.assertEqual(recorder.args, ())
        self.assertEqual(recorder.kwargs, {})

    @pytest.mark.integration
    @pytest.mark.usefixtures("worktree_services")
    def test_server_info_reports_the_config_the_server_was_built_with(self) -> None:
        recorder = self.invoke(
            "server_info", "agents_remember.mcp.registration.core.server_info_payload"
        )

        self.assertEqual(recorder.args[:1], (self.config,))
        self.assertEqual(len(recorder.args), 2)
        self.assertIsInstance(recorder.args[1], ServingBuildPayload)
        self.assertIsNotNone(recorder.args[1].sourceDigest)

    def test_context_packet_defaults_to_providers_only(self) -> None:
        """The docstring's "optionally provider status, drift and freshness": providers are
        on by default, the two costlier reads (drift, and the freshness fetch) are off."""
        recorder = self.invoke(
            "context_packet",
            "agents_remember.mcp.registration.core.context_packet_payload",
            {"repo_id": "agents-remember"},
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember"))
        self.assertEqual(
            recorder.kwargs,
            {"include_providers": True, "include_drift": False, "include_freshness": False},
        )

    def test_context_packet_forwards_each_inclusion_flag(self) -> None:
        recorder = self.invoke(
            "context_packet",
            "agents_remember.mcp.registration.core.context_packet_payload",
            {
                "repo_id": "agents-remember",
                "include_providers": False,
                "include_drift": True,
                "include_freshness": True,
            },
        )

        self.assertEqual(
            recorder.kwargs,
            {"include_providers": False, "include_drift": True, "include_freshness": True},
        )

    def test_read_ar_files_forwards_the_file_requests_verbatim(self) -> None:
        files = [{"path": "mcp/src/agents_remember/mcp/server.py", "source": "full"}]

        recorder = self.invoke(
            "read_ar_files",
            "agents_remember.mcp.registration.core.read_ar_files_payload",
            {"repo_id": "agents-remember", "files": files},
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember", files))
        self.assertEqual(recorder.kwargs, {"refresh": False})

    def test_resolve_context_packs_the_locators_into_a_task_ref(self) -> None:
        """``TaskRef`` carries "the repo a task belongs to and the identifiers that locate
        its contract" -- so repo/task/leaf/parent/contract go inside it, while the
        worktree name and the topology override stay separate arguments."""
        recorder = self.invoke(
            "resolve_context",
            "agents_remember.mcp.registration.core.resolve_context_payload",
            {
                "repo_id": "agents-remember",
                "task_name": "260731-EFA",
                "parent_task": "260731-EFA-MASTER",
                "leaf_id": "260731-EFA-L2",
                "contract_path": "/tmp/contract.yaml",
                "worktree_name": "efa-l2",
                "topology": "external",
            },
        )

        config, task_ref = recorder.args
        self.assertIs(config, self.config)
        self.assertEqual(task_ref.repo_id, "agents-remember")
        self.assertEqual(task_ref.task_name, "260731-EFA")
        self.assertEqual(task_ref.parent_task, "260731-EFA-MASTER")
        self.assertEqual(task_ref.leaf_id, "260731-EFA-L2")
        self.assertEqual(task_ref.contract_path, "/tmp/contract.yaml")
        self.assertEqual(recorder.kwargs, {"worktree_name": "efa-l2", "topology": "external"})

    def test_resolve_context_leaves_unheld_locators_unset(self) -> None:
        recorder = self.invoke(
            "resolve_context",
            "agents_remember.mcp.registration.core.resolve_context_payload",
            {"repo_id": "agents-remember"},
        )

        _config, task_ref = recorder.args
        self.assertEqual(task_ref.task_name, None)
        self.assertEqual(task_ref.parent_task, None)
        self.assertEqual(task_ref.leaf_id, None)
        self.assertEqual(task_ref.contract_path, None)
        self.assertEqual(recorder.kwargs, {"worktree_name": None, "topology": None})

    def test_runtime_install_installs_provider_deps_unless_told_otherwise(self) -> None:
        """The documented defaults: a real install (not a preview) that builds provider
        images with the layer cache, and leaves benchmark fixtures out."""
        recorder = self.invoke(
            "runtime_install", "agents_remember.mcp.registration.core.runtime_install_payload"
        )

        self.assertEqual(recorder.args, (self.config,))
        self.assertEqual(
            recorder.kwargs,
            {
                "dry_run": False,
                "include_benchmarks": False,
                "install_provider_deps": True,
                "no_cache": False,
            },
        )

    def test_runtime_install_forwards_the_from_scratch_rebuild_request(self) -> None:
        recorder = self.invoke(
            "runtime_install",
            "agents_remember.mcp.registration.core.runtime_install_payload",
            {"dry_run": True, "include_benchmarks": True, "no_cache": True},
        )

        self.assertEqual(
            recorder.kwargs,
            {
                "dry_run": True,
                "include_benchmarks": True,
                "install_provider_deps": True,
                "no_cache": True,
            },
        )

    def test_skills_install_neither_overwrites_nor_archives_by_default(self) -> None:
        recorder = self.invoke(
            "skills_install", "agents_remember.mcp.registration.core.skills_install_payload"
        )

        self.assertEqual(recorder.args, (self.config,))
        self.assertEqual(
            recorder.kwargs,
            {"dry_run": False, "overwrite": False, "archive_existing": False},
        )

    def test_dispatch_agent_forwards_only_work_identity_and_brief(self) -> None:
        task_ref = {"repository": "repo-a", "path": "master/leaf-2.json"}
        recorder = self.invoke(
            "dispatch_agent",
            "agents_remember.mcp.registration.sessions.dispatch_agent_payload",
            {
                "task_document_ref": task_ref,
                "role": "reviewer",
                "brief": "Review the implementation.",
                "label": "L2 reviewer",
            },
        )

        config, request = recorder.args
        self.assertIs(config, self.config)
        self.assertEqual(request.task_document_ref.model_dump(), task_ref)
        self.assertEqual(
            [request.role, request.brief, request.label],
            ["reviewer", "Review the implementation.", "L2 reviewer"],
        )
        self.assertEqual(recorder.kwargs, {})

    def test_retire_child_forwards_structural_child_address(self) -> None:
        task_ref = {"repository": "repo-a", "path": "master/leaf-2.json"}
        recorder = self.invoke(
            "retire_child",
            "agents_remember.mcp.registration.sessions.retire_child_payload",
            {"task_document_ref": task_ref, "role": "worker"},
        )

        config, request = recorder.args
        self.assertIs(config, self.config)
        self.assertEqual(request.task_document_ref.model_dump(), task_ref)
        self.assertEqual(request.role, "worker")
        self.assertEqual(request.reason, "delegated seat retired")
        self.assertEqual(recorder.kwargs, {})

    def test_rename_child_forwards_structural_child_address(self) -> None:
        task_ref = {"repository": "repo-a", "path": "master/leaf-2.json"}
        recorder = self.invoke(
            "rename_child",
            "agents_remember.mcp.registration.sessions.rename_child_payload",
            {"task_document_ref": task_ref, "role": "curator", "label": "Memory pass"},
        )

        config, request = recorder.args
        self.assertIs(config, self.config)
        self.assertEqual(request.task_document_ref.model_dump(), task_ref)
        self.assertEqual(request.role, "curator")
        self.assertEqual(request.label, "Memory pass")
        self.assertEqual(recorder.kwargs, {})

    def test_rename_self_needs_only_display_text(self) -> None:
        recorder = self.invoke(
            "rename_self",
            "agents_remember.mcp.registration.sessions.rename_self_payload",
            {"label": "Current manager"},
        )

        self.assertEqual(recorder.args, (self.config,))
        self.assertEqual(recorder.kwargs, {"label": "Current manager"})

    def test_message_parent_needs_no_recipient_address(self) -> None:
        recorder = self.invoke(
            "message_parent",
            "agents_remember.mcp.registration.orchestration.message_parent_payload",
            {
                "ask": "Review the completed work.",
                "response": "The turn report is durable.",
                "message_kind": "turn-report",
                "artifact_path": "reports/worker.md",
            },
        )

        config, request = recorder.args
        self.assertIs(config, self.config)
        self.assertEqual(
            [request.ask, request.response, request.message_kind, request.artifact_path],
            [
                "Review the completed work.",
                "The turn report is durable.",
                "turn-report",
                "reports/worker.md",
            ],
        )
        self.assertIsNone(request.task_document_ref)
        self.assertIsNone(request.role)
        self.assertEqual(recorder.kwargs, {})

    def test_message_child_uses_document_and_role_not_an_occupant_id(self) -> None:
        task_ref = {"repository": "repo-a", "path": "master/leaf-2.json"}
        recorder = self.invoke(
            "message_child",
            "agents_remember.mcp.registration.orchestration.message_child_payload",
            {
                "task_document_ref": task_ref,
                "role": "reviewer",
                "ask": "Check the revised change.",
                "response": "Focus on replacement routing.",
            },
        )

        config, request = recorder.args
        self.assertIs(config, self.config)
        self.assertEqual(request.task_document_ref.model_dump(), task_ref)
        self.assertEqual(
            [
                request.role,
                request.ask,
                request.response,
                request.message_kind,
                request.artifact_path,
            ],
            [
                "reviewer",
                "Check the revised change.",
                "Focus on replacement routing.",
                "message",
                None,
            ],
        )
        self.assertEqual(recorder.kwargs, {})

    def test_drift_check_defaults_to_fifty_findings(self) -> None:
        recorder = self.invoke(
            "drift_check",
            "agents_remember.mcp.registration.memory.drift_check_payload",
            {"repo_id": "agents-remember"},
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember"))
        self.assertEqual(recorder.kwargs, {"detail_limit": 50, "contract_path": None})

    def test_memory_quality_check_runs_every_check_when_none_are_named(self) -> None:
        recorder = self.invoke(
            "memory_quality_check",
            "agents_remember.mcp.registration.memory.memory_quality_check_payload",
            {"request": {"mode": "sync", "repo_id": "agents-remember"}},
        )

        self.assertEqual(
            recorder.args,
            (
                self.config,
                MemoryQualitySyncRequest(mode="sync", repo_id="agents-remember"),
            ),
        )
        self.assertEqual(recorder.kwargs, {})

    def test_memory_quality_check_start_mode_starts_a_background_run(self) -> None:
        recorder = self.invoke(
            "memory_quality_check",
            "agents_remember.mcp.registration.memory.memory_quality_check_start_payload",
            {"request": {"mode": "start", "repo_id": "agents-remember"}},
        )
        self.assertEqual(
            recorder.args,
            (
                self.config,
                MemoryQualityStartRequest(mode="start", repo_id="agents-remember"),
            ),
        )
        self.assertEqual(recorder.kwargs, {})

    def test_memory_quality_check_run_id_polls_the_run(self) -> None:
        recorder = self.invoke(
            "memory_quality_check",
            "agents_remember.mcp.registration.memory.memory_quality_check_poll_payload",
            {
                "request": {
                    "mode": "poll",
                    "repo_id": "agents-remember",
                    "run_id": "abc123",
                    "contract_path": "/coord/leaf.md",
                }
            },
        )
        self.assertEqual(
            recorder.args,
            (
                self.config,
                MemoryQualityPollRequest(
                    mode="poll",
                    repo_id="agents-remember",
                    run_id="abc123",
                    contract_path="/coord/leaf.md",
                ),
            ),
        )
        self.assertEqual(recorder.kwargs, {})

    def test_memory_quality_check_forwards_a_named_subset(self) -> None:
        recorder = self.invoke(
            "memory_quality_check",
            "agents_remember.mcp.registration.memory.memory_quality_check_payload",
            {
                "request": {
                    "mode": "sync",
                    "repo_id": "agents-remember",
                    "checks": ["drift"],
                    "detail_limit": 5,
                }
            },
        )

        self.assertEqual(
            recorder.args,
            (
                self.config,
                MemoryQualitySyncRequest(
                    mode="sync",
                    repo_id="agents-remember",
                    checks=["drift"],
                    detail_limit=5,
                ),
            ),
        )
        self.assertEqual(recorder.kwargs, {})

    def test_memory_quality_poll_rejects_explicit_start_fields_before_dispatch(self) -> None:
        adapter = TypeAdapter(MemoryQualityCheckRequest)
        for field, value in (
            ("checks", []),
            ("detail_limit", 50),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                adapter.validate_python(
                    {
                        "mode": "poll",
                        "repo_id": "agents-remember",
                        "run_id": "run-1",
                        field: value,
                    }
                )
        candidate_poll = adapter.validate_python(
            {
                "mode": "poll",
                "repo_id": "agents-remember",
                "run_id": "run-1",
                "contract_path": "/coord/leaf.md",
            }
        )
        self.assertEqual(candidate_poll.contract_path, "/coord/leaf.md")

    def test_memory_quality_tool_schema_exposes_the_three_request_modes(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(self.server.list_tools())}
        schema = json.dumps(tools["memory_quality_check"].inputSchema)
        self.assertIn('"sync"', schema)
        self.assertIn('"start"', schema)
        self.assertIn('"poll"', schema)
        self.assertIn('"discriminator"', schema)

    def test_route_index_refresh_writes_unless_previewed(self) -> None:
        recorder = self.invoke(
            "route_index_refresh",
            "agents_remember.mcp.registration.memory.route_index_refresh_payload",
            {"repo_id": "agents-remember"},
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember"))
        self.assertEqual(recorder.kwargs, {"dry_run": False, "contract_path": None})

    def test_the_three_memory_health_tools_forward_a_named_enclosure(self) -> None:
        """L6-R29: `contract_path` is what points these at a leaf instead of official memory.

        Through the live FastMCP schema, so the parameter is really published and really
        coerced -- an argument the registered signature does not declare is dropped before
        the body ever sees it, and the tool would then resolve the official repo while the
        caller believed it had named its leaf.
        """
        for tool, builder, extra in (
            ("drift_check", "drift_check_payload", {"detail_limit": 50}),
            ("route_index_refresh", "route_index_refresh_payload", {"dry_run": False}),
        ):
            with self.subTest(tool=tool):
                recorder = self.invoke(
                    tool,
                    f"agents_remember.mcp.registration.memory.{builder}",
                    {"repo_id": "agents-remember", "contract_path": "/coord/leaf.md"},
                )

                self.assertEqual(recorder.kwargs, {**extra, "contract_path": "/coord/leaf.md"})

        recorder = self.invoke(
            "memory_quality_check",
            "agents_remember.mcp.registration.memory.memory_quality_check_payload",
            {
                "request": {
                    "mode": "sync",
                    "repo_id": "agents-remember",
                    "contract_path": "/coord/leaf.md",
                }
            },
        )
        self.assertEqual(
            recorder.args,
            (
                self.config,
                MemoryQualitySyncRequest(
                    mode="sync",
                    repo_id="agents-remember",
                    contract_path="/coord/leaf.md",
                ),
            ),
        )
        self.assertEqual(recorder.kwargs, {})

    def test_memory_init_initializes_git_by_default(self) -> None:
        recorder = self.invoke(
            "memory_init",
            "agents_remember.mcp.registration.memory.memory_init_payload",
            {"repo_id": "agents-remember"},
        )

        self.assertEqual(recorder.kwargs, {"dry_run": False, "initialize_git": True})

    def test_memory_baseline_status_takes_only_the_repo(self) -> None:
        recorder = self.invoke(
            "memory_baseline_status",
            "agents_remember.mcp.registration.memory.memory_baseline_status_payload",
            {"repo_id": "agents-remember"},
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember"))
        self.assertEqual(recorder.kwargs, {})

    def test_memory_baseline_adopt_groups_the_two_branches_and_gates_on_drift(self) -> None:
        recorder = self.invoke(
            "memory_baseline_adopt",
            "agents_remember.mcp.registration.memory.memory_baseline_adopt_payload",
            {
                "repo_id": "agents-remember",
                "source_branch": "main",
                "work_branch": "task/260731",
            },
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember"))
        self.assertIs(recorder.kwargs["accept_drift"], False)
        self.assertIs(recorder.kwargs["dry_run"], False)
        branches = recorder.kwargs["branches"]
        self.assertEqual(branches.source_branch, "main")
        self.assertEqual(branches.work_branch, "task/260731")

    def test_memory_carryover_plan_packs_the_selection(self) -> None:
        """Every ref the plan compares travels in one ``CarryoverSelection``; the plan is
        non-mutating so nothing else is passed."""
        recorder = self.invoke(
            "memory_carryover_plan",
            "agents_remember.mcp.registration.memory.memory_carryover_plan_payload",
            {
                "repo_id": "agents-remember",
                "contract_path": "tasks/recovery/series-contract.md",
                "source_memory": "memory/task",
                "official_code_ref": "main",
                "source_code_ref": "task/260731",
                "old_base": "abc123",
                "replace_existing": True,
            },
        )

        config, selection = recorder.args
        self.assertIs(config, self.config)
        self.assertEqual(selection.repo_id, "agents-remember")
        self.assertEqual(selection.contract_path, "tasks/recovery/series-contract.md")
        self.assertEqual(selection.source_memory, "memory/task")
        self.assertEqual(selection.official_code_ref, "main")
        self.assertEqual(selection.source_code_ref, "task/260731")
        self.assertEqual(selection.old_base, "abc123")
        self.assertIs(selection.replace_existing, True)
        self.assertEqual(recorder.kwargs, {})

    def test_memory_carryover_apply_carries_the_intent_note_and_default_messages(self) -> None:
        recorder = self.invoke(
            "memory_carryover_apply",
            "agents_remember.mcp.registration.memory.memory_carryover_apply_payload",
            {
                "repo_id": "agents-remember",
                "contract_path": "tasks/recovery/series-contract.md",
                "source_memory": "memory/task",
                "official_code_ref": "main",
                "source_code_ref": "task/260731",
                "old_base": "abc123",
                "intent_note": "code landed on main",
            },
        )

        _config, selection = recorder.args
        self.assertEqual(selection.old_base, "abc123")
        self.assertIs(selection.replace_existing, False)
        self.assertEqual(recorder.kwargs["intent_note"], "code landed on main")
        self.assertEqual(recorder.kwargs["include_review_required"], None)
        messages = recorder.kwargs["messages"]
        self.assertEqual(messages.memory, "Carry over landed branch memory")
        self.assertEqual(messages.ledger, "Record branch memory carryover")

    def test_memory_carryover_apply_forwards_caller_commit_messages(self) -> None:
        recorder = self.invoke(
            "memory_carryover_apply",
            "agents_remember.mcp.registration.memory.memory_carryover_apply_payload",
            {
                "repo_id": "agents-remember",
                "contract_path": "tasks/recovery/series-contract.md",
                "source_memory": "memory/task",
                "official_code_ref": "main",
                "source_code_ref": "task/260731",
                "old_base": "abc123",
                "intent_note": "note",
                "include_review_required": ["onboarding/a.md"],
                "memory_commit_message": "memory msg",
                "ledger_commit_message": "ledger msg",
            },
        )

        self.assertEqual(recorder.kwargs["include_review_required"], ["onboarding/a.md"])
        messages = recorder.kwargs["messages"]
        self.assertEqual((messages.memory, messages.ledger), ("memory msg", "ledger msg"))

    def test_provider_status_defaults_to_twenty_detail_rows(self) -> None:
        recorder = self.invoke(
            "provider_status",
            "agents_remember.mcp.registration.providers.provider_status_payload",
        )

        self.assertEqual(recorder.args, (self.config,))
        self.assertEqual(recorder.kwargs, {"detail_limit": 20})

    def test_provider_diagnostics_forwards_the_detail_limit(self) -> None:
        recorder = self.invoke(
            "provider_diagnostics",
            "agents_remember.mcp.registration.providers.provider_diagnostics_payload",
            {"detail_limit": 3},
        )

        self.assertEqual(recorder.kwargs, {"detail_limit": 3})

    def test_provider_watchers_runs_for_real_unless_previewed(self) -> None:
        recorder = self.invoke(
            "provider_watchers",
            "agents_remember.mcp.registration.providers.provider_watchers_payload",
            {"action": "restart"},
        )

        self.assertEqual(recorder.args, (self.config,))
        self.assertEqual(recorder.kwargs, {"action": "restart", "dry_run": False})

    def test_grepai_search_splits_query_repo_scope_and_execution_scope(self) -> None:
        recorder = self.invoke(
            "grepai_search",
            "agents_remember.mcp.registration.code_search.grepai_search_payload",
            {
                "query": "worktree contract",
                "limit": 3,
                "output_format": "toon",
                "repo_ids": ["agents-remember"],
                "all_repos": False,
                "worktree": "efa-l2",
                "dry_run": True,
                "timeout": 45,
            },
        )

        config, query = recorder.args
        self.assertIs(config, self.config)
        self.assertEqual(query.query, "worktree contract")
        self.assertEqual(query.limit, 3)
        self.assertEqual(query.output_format, "toon")
        repos = recorder.kwargs["repos"]
        self.assertEqual(repos.repo_ids, ["agents-remember"])
        self.assertIs(repos.all_repos, False)
        scope = recorder.kwargs["scope"]
        self.assertEqual(scope.worktree, "efa-l2")
        self.assertIs(scope.dry_run, True)
        self.assertEqual(scope.timeout, 45)

    def test_grepai_trace_carries_the_trace_action_and_depth(self) -> None:
        recorder = self.invoke(
            "grepai_trace",
            "agents_remember.mcp.registration.code_search.grepai_trace_payload",
            {"trace_action": "graph", "symbol": "create_server", "depth": 2},
        )

        _config, query = recorder.args
        self.assertEqual(query.trace_action, "graph")
        self.assertEqual(query.symbol, "create_server")
        self.assertEqual(query.depth, 2)
        self.assertEqual(query.output_format, "json")
        self.assertEqual(recorder.kwargs["scope"].worktree, None)

    def test_cgc_symbol_search_passes_repo_and_name_positionally(self) -> None:
        recorder = self.invoke(
            "cgc_symbol_search",
            "agents_remember.mcp.registration.code_search.cgc_symbol_search_payload",
            {"repo_id": "agents-remember", "name": "TaskRef", "worktree": "efa-l2"},
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember", "TaskRef"))
        self.assertEqual(recorder.kwargs["scope"].worktree, "efa-l2")

    def test_cgc_callers_forwards_the_disambiguating_file(self) -> None:
        recorder = self.invoke(
            "cgc_callers",
            "agents_remember.mcp.registration.code_search.cgc_callers_payload",
            {
                "repo_id": "agents-remember",
                "function": "measure",
                "file": "diff_coverage.py",
            },
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember", "measure"))
        self.assertEqual(recorder.kwargs["file"], "diff_coverage.py")

    def test_cgc_callees_takes_no_file_disambiguator(self) -> None:
        recorder = self.invoke(
            "cgc_callees",
            "agents_remember.mcp.registration.code_search.cgc_callees_payload",
            {"repo_id": "agents-remember", "function": "measure", "timeout": 30},
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember", "measure"))
        self.assertEqual(set(recorder.kwargs), {"scope"})
        self.assertEqual(recorder.kwargs["scope"].timeout, 30)

    def test_cgc_dependencies_passes_the_module(self) -> None:
        recorder = self.invoke(
            "cgc_dependencies",
            "agents_remember.mcp.registration.code_search.cgc_dependencies_payload",
            {"repo_id": "agents-remember", "module": "agents_remember.mcp.server"},
        )

        self.assertEqual(
            recorder.args, (self.config, "agents-remember", "agents_remember.mcp.server")
        )

    def test_cgc_complexity_reports_the_whole_repo_when_no_function_is_named(self) -> None:
        recorder = self.invoke(
            "cgc_complexity",
            "agents_remember.mcp.registration.code_search.cgc_complexity_payload",
            {"repo_id": "agents-remember"},
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember"))
        self.assertEqual(recorder.kwargs["function"], None)

    def test_cgc_visualize_serves_on_port_8000_by_default(self) -> None:
        recorder = self.invoke(
            "cgc_visualize",
            "agents_remember.mcp.registration.code_search.cgc_visualize_payload",
            {"repo_id": "agents-remember"},
        )

        self.assertEqual(recorder.args, (self.config, "agents-remember"))
        self.assertEqual(recorder.kwargs["port"], 8000)
        self.assertEqual(recorder.kwargs["context"], None)

    def test_worktree_start_splits_identity_bases_and_execution(self) -> None:
        """Who the task is, what it is cut from, and how the start runs -- the three
        parameter objects the application entry point takes, each carrying its own arguments."""
        recorder = self.invoke(
            "worktree_start",
            "agents_remember.mcp.registration.worktrees.worktree_start_payload",
            {
                "repo_id": "agents-remember",
                "task_name": "260731-EFA",
                "worktree_name": "efa-l2",
                "leaf_id": "260731-EFA-L2",
                "parent_task": "260731-EFA-MASTER",
                "workflow_kind": "chat-task",
                "source_branch": "main",
                "work_branch": "task/260731",
                "memory_mode": "internal",
                "memory_choice": "disabled-memory",
                "stale_base_choice": "proceed-stale",
                "dry_run": True,
                "skip_provider_setup": True,
                "retry_provider_setup": True,
            },
        )

        config, identity = recorder.args
        self.assertIs(config, self.config)
        self.assertEqual(identity.repo_id, "agents-remember")
        self.assertEqual(identity.task_name, "260731-EFA")
        self.assertEqual(identity.worktree_name, "efa-l2")
        self.assertEqual(identity.leaf_id, "260731-EFA-L2")
        self.assertEqual(identity.parent_task, "260731-EFA-MASTER")
        self.assertEqual(identity.workflow_kind, "chat-task")

        bases = recorder.kwargs["bases"]
        self.assertEqual(bases.source_branch, "main")
        self.assertEqual(bases.work_branch, "task/260731")
        self.assertEqual(bases.memory_mode, "internal")
        self.assertEqual(bases.memory_choice, "disabled-memory")
        self.assertEqual(bases.stale_base_choice, "proceed-stale")

        execution = recorder.kwargs["execution"]
        self.assertIs(execution.dry_run, True)
        self.assertIs(execution.skip_provider_setup, True)
        self.assertIs(execution.retry_provider_setup, True)
