# MCP Tool Reference

The Agents Remember MCP server exposes 63 public tools. Tools **apply by default** — pass
`dry_run=true` to preview first (for the read-only `cgc_*`/`grepai_*` query tools,
`dry_run=true` returns the planned provider command without running it). The two
`codex_benchmark_*` tools are the exception: they default to `dry_run=true`
because a real run clones repos and executes Codex agents. Repository-scoped tools
take a `repo_id` that must be an allowed repo in the MCP settings.

This page is a map of the surface; behavior detail lives in the linked skill and
reference pages.

**Skills vs tools.** Many skills wrap one or more of these tools and add the
procedure, gates, and ordering around them: the `c-00-initialize-memory-repo` skill drives the `memory_init` MCP tool, the `c-02-memory-quality-control` skill
drives the `drift_check` / `memory_quality_check` MCP tools, the `c-09-git-worktree-manager` skill drives the `worktree_*`
MCP tools, and the `c-12-closeout` skill drives the `*_closeout_*` MCP tools. When the docs say "run a core skill,"
the agent runs the skill, which calls the matching tool with the right
preconditions. Call the raw tool directly only when you do not need the skill's
surrounding procedure. See the [Skills reference](skills.md).

## Server & context

| Tool | Purpose | Key args |
| --- | --- | --- |
| `ping` | Liveness check; returns server name/version/transport. | — |
| `server_info` | Report resolved roots, allowed repos/providers, and the tool list. | — |
| `resolve_context` | Resolve a repository's coordination/memory context (topology, roots, settings, storage, pathRules). | `repo_id`, optional `task_name` / `contract_path` / `worktree_name` / `topology` |
| `context_packet` | Bundle repo state, paths, memory, worktree, and provider status into one packet. | `repo_id`, `include_providers=true`, `include_drift=false` |

## Structural role control

| Tool | Purpose | Key args |
| --- | --- | --- |
| `dispatch_agent` | Create or replace one canonical role seat and durably pin its exact initial brief. Plane-hosted callers use injected seat identity plus direct-child scope; identity-free developer launchers use target-document resolution plus role-altitude validation. | `task_document_ref`, `role`, `brief`, optional `label` |
| `message_parent` | Send one durable whole-message submission to the caller's structurally current parent. | `ask`, `response`, optional agent-visible `message_kind` |
| `message_child` | Send one durable whole-message submission to an authorized structurally current child. | `task_document_ref`, `role`, `ask`, `response`, optional agent-visible `message_kind` |
| `retire_child` | Retire an authorized child seat without exposing its occupant address. | `task_document_ref`, `role`, `reason` |
| `rename_child` | Rename an authorized child chat by document and role. | `task_document_ref`, `role`, `label` |
| `rename_self` | Rename the caller's ambient structural chat. | `label` |
| `lifecycle_gate` | Raise a gate on the caller's ambient task document. | `kind`, optional `ask`/`packet`/`evidence_refs`, `wait` |
| `gate_decide` | Decide exactly one open gate matching an authorized child document and kind; zero or multiple matches fail closed. | `task_document_ref`, `kind`, `decision`, optional `note`/`evidence_refs` |
| `gate_list` | List document-projected gate state in the caller's authorized structural scope. | — |

Runtime session, lifecycle, inbox-row, adapter, vendor, and gate identifiers are private
control-plane correlations. The plane may use exact addressing internally for the initial brief,
delivery, recovery, diagnostics, and trusted dashboard administration, but those operations are
not registered as agent MCP tools and must not appear in role briefs or handoffs.

`dispatch_agent` is the only public spawn verb and its two caller kinds are disjoint. Presence of
plane-injected hosted identity selects structural child authorization; absence selects the ambient
launcher, which has no parent seat and must address a canonical task document at the target role's
altitude. The request never supplies caller identity or selects a mode. Both modes share settings,
internal creation/readiness, exact brief pinning, rollback, and canonical seat publication. A
plane authorization failure remains a refusal and never retries as ambient. `dispatched` and
`dispatch-queued` both mean the brief is durable; callers do not poll readiness or send it again.

## Install & scaffolding

| Tool | Purpose | Key args |
| --- | --- | --- |
| `runtime_install` | Reconcile package-owned runtime (AGENTS.md templates, skills, provider defaults, runtime folders) into the coordinator; optionally install provider deps and benchmark fixtures. **Preserves** user data (`memory-repos/`, `providers/data/`, `providers/runners/`) and **replaces** the managed scaffold (`skills/`, provider compose/shape). With `install_provider_deps=true` it builds provider images but **skips** any image whose tag already exists; pass `no_cache=true` to force a from-scratch rebuild (bypasses that skip and adds `--no-cache`). | `dry_run=false`, `include_benchmarks=false`, `install_provider_deps=true`, `no_cache=false` |
| `skills_install` | Maintenance/manual install tool for non-package setups: copy the packaged (already flat) skills into the harness skill root — one folder per skill `name`; no layout option. The package-based first-run path gets skills from the copied harness starter package instead. | `dry_run=false`, `overwrite=false`, `archive_existing=false` |

## Memory & onboarding

| Tool | Purpose | Key args |
| --- | --- | --- |
| `memory_init` | Initialize (or repair) a repository's memory root. | `repo_id`, `dry_run=false`, `initialize_git=true` |
| `drift_check` | Task-start onboarding drift classification; writes a temp drift report. | `repo_id`, `detail_limit=50`, `contract_path=None` |
| `memory_quality_check` | Closeout memory-quality gate (drift integrity + style checks). A full contract-scoped call also atomically replaces the curator worklist at `<worktree enclosure>/reports/curator-memory-quality.md`, combining quality findings, current-addition onboarding coverage, route-index preview, drift candidates, and report-only evidence. | `request={"mode":"sync", "repo_id":..., "checks":..., "detail_limit":50, "contract_path":...}`; use `mode:"start"` for async admission and poll only with `mode:"poll"`, `repo_id`, `run_id` |
| `citation_fix` | Regenerate anchored citation ranges only inside a contract-selected leaf memory worktree; official memory, ambiguous anchors, renames, and deletions refuse. | `repo_id`, `contract_path`, `document=None`, `expected_snapshot=None`, `dry_run=false` |
| `route_index_refresh` | Regenerate `overview.index.json` route indexes to match the onboarding tree. **Writes** into the memory root it resolves. | `repo_id`, `dry_run=false`, `contract_path=None` |

`memory_quality_check.request` is exactly one shape. `sync` and `start` accept repository,
scope, checks, and detail fields; `poll` accepts only `mode`, `repo_id`, and `run_id`. Do not resend
start fields while polling, even when their values equal defaults: the discriminated boundary
rejects contradictory presence before it reads the run registry. A unique start at capacity returns
`capacity-reached` without a `runId`; poll or wait for active work, then submit a new start request.

The three rows above support the same optional `contract_path` as the `worktree_*` verbs: a leaf
enclosure contract path. `memory_quality_check` carries it inside its discriminated `request`;
the other two tools take it directly. Omitted, they resolve the configured **official** memory repo
(unchanged). Supplied, they act on that leaf's **memory worktree** and measure it against the leaf's code
worktree — how a curator checks its own change-set before handing it back, and the only correct way
to run `route_index_refresh` from inside a leaf, since without it that tool writes indexes into the
official repo and leaves it dirty. A contract naming another repo, or one whose memory worktree is
gone, is refused; nothing falls back to the official repo. The response carries `onboardingRoot`, so
which tree was acted on is always visible.

Only a full leaf-scoped sync/start `memory_quality_check` request writes the curator checklist. It reports
`curatorActionableCount` and `checklistStatus`; the curator reruns it until the actionable count is
zero. Subset checks and official-repository checks do not create that artifact. The checklist is
outside both Git worktrees, replaces its predecessor instead of accumulating timestamped files,
and `worktree_cleanup`/`worktree_abandon` remove its reserved `reports/` directory with the
enclosure. Dirty-source and real-commit residuals remain listed separately so the pre-commit loop
does not fabricate closeout metadata.

The `reports/` directory is disposable evidence, not the live operation journal. Lifecycle reads
have two strict routes. A live locator reaches the exact root-local
`.lifecycle/enclosure-manifest.json` and canonical journal/history. A terminal locator reaches only
the exact external archive/receipt plus surviving configured-contract truth; it never falls through
to the old root. Terminal cleanup archives and reads back the canonical evidence, publishes the
external receipt, and only then may remove the enclosure root and its reports. A task document,
task/worktree scan, naming inference, caller-supplied root, or reports path is never
operation-location authority.

The repository-profile quality gate has the same enclosure-owned lifetime for its evidence. Every
completed leaf-closeout or leaf/master-integration gate atomically replaces
`<worktree enclosure>/reports/test-results.md` with the run status, command, timestamps, scope,
exit code, profile/plan/adapter identities, memory-cap facts when applicable, and the complete
adapter output.
A failed gate writes the report before refusing and names its path in the error. An interrupted
run cannot replace the previous completed result, and cleanup/abandon removes the file with the
same `reports/` directory. The gate payload returns that stable path as `reportPath`.

Executed closeout and integration gates expose their typed result under `code_quality_gate` or
`quality_gate`. `reportPath` names the developer-facing `reports/test-results.md` summary.
`publishedResultPath` names the manifest-verified immutable generation's profile-declared decoder
artifact for both fresh success and recovery; `resultArtifact` carries its repository-relative
identity. The immutable machine result never replaces or renames the human summary, and there is no
universal result filename fallback.

Citation ranges are not repaired by hand. MCP `citation_fix(repo_id=<id>,
contract_path=<enclosure contract>, dry_run=true|false)` regenerates every range that can be
regenerated from its anchor — the whole tree in one call after a package move — and prints a work
order for the rest: the anchor, the range, the file, and every location in the code tree that does
hold that anchor. It repairs pure MOVES only, where a symbol kept its name and changed file; a
rename, a deletion and an ambiguous match are refused rather than guessed at. `contract_path` is
**required**: there is no argument list that names the official memory repo, and a contract that
points at it is refused. The checkout-only CLI remains a development adapter to the same guarded
application operation, not an agent-facing route around MCP.
| `read_ar_files` | Batch read up to 5 repo-relative source paths in a managed repo, each paired with its file-level onboarding, plus the repository + governing route overviews (auto-attached, session-deduped); emits a facts-only `read.packet`. **The research-phase read** — use it instead of a native read up to the build decision; a native read remains the edit precondition during build. | `repo_id`, `files:[{path, source:"full"\|{startLine,endLine}, onboarding?}]`, `refresh=false` |

## Memory baseline & carryover

| Tool | Purpose | Key args |
| --- | --- | --- |
| `memory_baseline_status` | Report drift + ledger state for adopting an external-memory baseline. | `repo_id` |
| `memory_baseline_adopt` | Create the first ledgered memory baseline (gated on clean/accepted drift). | `repo_id`, accept-drift / dry-run options |
| `memory_carryover_plan` | Plan carrying richer onboarding from a source branch into official memory. | repo/branch scope |
| `memory_carryover_apply` | Apply an approved carryover plan once the code has landed officially. | plan + intent |

## Worktree lifecycle & closeout

| Tool | Purpose | Key args |
| --- | --- | --- |
| `worktree_start` | Create/load a task contract and code (+ external-memory) worktrees. | `repo_id`, `task_name`, `worktree_name`, `workflow_kind="light-task"`, `dry_run=false`, `memory_choice` |
| `worktree_enclosure_adopt` | Explicitly adopt one exact readable pre-locator enclosure. Validates the configured contract/root pair and writes an audited locator receipt; normal readers never invoke it. Dry-run by default. | `contract_path`, `expected_worktree_group`, nonblank `rationale`, `dry_run=true`, `approved=false`, optional `expected_publication_request_id` |
| `worktree_attach` | Re-attach to an existing task contract without mutating Git. | `repo_id`, `task_name` / `contract_path` |
| `worktree_status` | Report strict task-addressed lifecycle status without queue input. A live locator resolves to the root manifest/journal and its executable controls; a terminal locator resolves to the exact external archive/receipt plus surviving contract truth, distinguishes archive-ready from cleanup-completed, and returns the archived `cleanupArguments` with exact retry `nextArgs`. | `repo_id`, `task_name` / `contract_path` |
| `worktree_closeout_preview` | Non-mutating preview of a worktree-backed closeout. | `contract_path`, code/memory/ledger commit messages |
| `worktree_closeout_apply` | Validate every enabled explicit nonblank input, claim the exact first-ready door generation through a short CAS, then start or observe its durable task-bound closeout and return promptly. Poll `worktree_status`; no operation ID is exposed. Agents Remember source commits run the leaf change-set-scoped quality contract (`--targeted`) before Git commit — the full wrapper runs once per master at the master integration gate. | `contract_path`, `intent_note`, explicit message for each enabled commit leg |
| `worktree_integrate` | Start or observe durable task-bound landing (`ff-only` or `replay`) and return promptly. Poll `worktree_status`; retries with conflicting input refuse. | `contract_path`, `strategy`, `dry_run=false` |
| `worktree_operation_control` | Execute one currently advertised task-addressed control for an exact closeout/integrate/direct-landing generation. Retry/recover preserve accepted input; cancellation proves worker exit and Git safety; revise/retire/supersede are evidence-aware. | `contract_path`, `operation_kind`, `action`, `expected_generation`, nonblank `intent_note`, action-specific inputs, `dry_run=false` |
| `worktree_legacy_operation` | Explicitly inspect, migrate, or archive one exact schema-1 operation. Migration is limited to the proven blank-message incident; normal readers remain current-schema-only. | `contract_path`, `operation_kind`, `action`, inspect-bound `expected_digest`, action-specific messages/reason, `dry_run=false` |
| `direct_landing` | Policy-gated branch-addressed memory/ledger landing for an already verified series code commit. Persists a durable direct-landing generation before Git mutation and recovers it through `worktree_operation_control`. | `contract_path`, `code_commit`, explicit enabled-leg messages, `intent_note`, optional gated `candidate_tree`, `dry_run=false` |
| `worktree_cleanup` | After integration, archive/read back canonical lifecycle evidence, publish an external terminal receipt, then remove worktrees, merged task branches, reports, and the enclosure root. Archive-ready is not cleanup-completed; retry this exact public call with its archived `teardown_providers` value until surviving contract truth records completion. This is non-terminal for task documents. | `contract_path`, `dry_run=false`, `teardown_providers=true` |
| `worktree_abandon` | Abandon an unintegrated generation through exact contract/journal/Git authority and preserve terminal archive proof before deletion. Archive-ready but incomplete abandon retries this exact public call with its archived `force` value until surviving contract truth records abandonment. | `contract_path`, `dry_run=false`, `force=false` |
| `lifecycle_finalize_task` | Prove the landed edge, resolve the exact contract-bound leaf, refuse before cleanup unless every parent/nested step is done, then complete that leaf and, when it declares an existing immediate parent, automatically derive and reconcile that exact row. Standalone/no-parent tasks remain supported; the parent document's own task status and higher ancestors are not completed. | `contract_path`; optional `task_doc_path`, `master_doc_path`, and `subtask_number` are independent identity assertions; `dry_run=false` |

Receipt-file existence alone does not select the terminal route. While the locator is still live,
an identical accepted `worktree_cleanup` or `worktree_abandon` retry reuses the exact published
archive/receipt bytes and completes terminal-locator publication. Only after the locator becomes
`terminal-archived` does `worktree_status` use the terminal route to distinguish archive-ready from
surviving contract completion; the same accepted disposition with its original public arguments
finishes an incomplete destructive tail. This is not a `worktree_operation_control` action. A
conflicting terminal request must expose the archive-accepted verb as its public retry action; it
never invents a fallback reader or second cleanup route. The archive request identity includes
typed `cleanupArguments`: `{teardown_providers: <bool>}` for `worktree_cleanup` or
`{force: <bool>}` for `worktree_abandon`. Status and conflict payloads return that object and the
exact `nextArgs`. A retry with a different value refuses; omitted/default arguments are not
reconstructed as recovery intent.

For a same-address reopened or abandoned successor, reservation first proves the exact terminal
archive and restartable predecessor contract. Under that reservation, the stable contract file is
atomically changed only when its bytes equal the accepted predecessor tombstone; already accepted
successor bytes are idempotent, and every other byte state refuses. This is a strict successor
publication transaction, not a generic overwrite or compatibility reader.

## Closeout scheduling projection

| Tool | Purpose | Key args |
| --- | --- | --- |
| `closeout_door` | Publish or inspect one contract-owned closeout generation. Declare/update-provenance bind complete current evidence; defer/resume/withdraw change only the exact generation's scheduling disposition. Claim is intentionally absent. | request with `action`, `contract_path`, action-specific `candidate_task_document_ref`, `expected_generation_id`, `grade`, `admission`, optional declared `caller` |
| `closeout_queue` | Inspect or rebuild one sprint's disposable waiting-only projection. It is either exact-current `valid-built` or non-admitting `invalid-empty`; rebuild derives solely from current task plus waiting-door facts. | request with `action:"status"|"rebuild"`, `sprint_task_document_ref`, optional declared `caller` |

The queue has no declare, select, claim, defer, withdraw, lifecycle, commit, certification,
recovery, replan, drain, or stale-row transition. Claim transfers the current generation from its
door into the enclosure-root operation journal. Queue invalidation or absence cannot destroy or
strand accepted operation evidence.

## Task documents

| Tool | Purpose | Key args |
| --- | --- | --- |
| `task_doc` | Author JSON-primary task documents and deterministic markdown. Every intrinsically valid mutation publishes during every queue/operation phase and returns per-sprint `projectionEffects`; an incomplete effect carries its exact rebuild `nextAction`. `skip_step` requires a nonblank reason; `discard-unstarted` removes only centrally proven never-started planning work without completion fiction; `Completed` refuses unresolved units. | `repo_id`, task/contract target, `operation`, edit payload, `dry_run=false` |
| `task_reopen` | Reopen a fully landed leaf under the same leaf id so new work can be declared explicitly. | `contract_path`, `dry_run=false` |

See [c-09-git-worktree-manager Worktrees And Closeout](worktrees-c09.md) for the lifecycle and gates.

## Providers

| Tool | Purpose | Key args |
| --- | --- | --- |
| `provider_status` | Compact provider readiness summary. | `detail_limit=20` |
| `provider_diagnostics` | Raw provider-native diagnostic detail. | `detail_limit=20` |
| `provider_watchers` | Start/stop/report provider watchers. | `action`, `dry_run=false` |

## Semantic memory search (grepai)

| Tool | Purpose | Key args |
| --- | --- | --- |
| `grepai_search` | Semantic search over memory/onboarding. | `query`, `repo_ids=None`, `all_repos=true`, `limit=10`, `output_format="json"`, `dry_run=false` |
| `grepai_trace` | Trace relationships in the semantic graph. | `trace_action`, `symbol`, scope + `output_format`, `dry_run=false` |

## Code-relationship search (CodeGraphContext)

| Tool | Purpose | Key args |
| --- | --- | --- |
| `cgc_symbol_search` | Find a symbol in the code graph. | `repo_id`, `name`, `dry_run=false` |
| `cgc_callers` | Who calls a function. | `repo_id`, `function`, `file=None`, `dry_run=false` |
| `cgc_callees` | What a function calls. | `repo_id`, `function`, `dry_run=false` |
| `cgc_dependencies` | A module's dependencies. | `repo_id`, `module`, `dry_run=false` |
| `cgc_complexity` | Complexity metrics from the code graph. | `repo_id`, `dry_run=false` |
| `cgc_visualize` | Produce a graph visualization. | `repo_id`, `dry_run=false` |

## Benchmarks

| Tool | Purpose | Key args |
| --- | --- | --- |
| `codex_benchmark_prepare` | Prepare resettable benchmark case workspaces. | case/scope options |
| `codex_benchmark_run` | Run a Codex benchmark case. | case + optional sandbox |

---

The authoritative source for this surface is `mcp/src/agents_remember/mcp/registration/`
(one module per tool family, each holding that family's `@server.tool()` registrations;
`create_server` only walks `TOOL_REGISTRARS`); response shapes are enforced by the models in
`mcp/src/agents_remember/models/` via `tool_registry.PUBLIC_TOOL_RESPONSE_MODELS`.
