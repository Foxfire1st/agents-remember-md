---
name: c-12-closeout
description: "Close out approved Agents Remember edits by preserving approval authority, mandatory strict code quality before code commit, missing-onboarding checks, external-memory refresh, memory quality, ledger alignment, and no automatic push."
---

# c-12-closeout Closeout

Use this skill when approved Agents Remember edits in an external- or
internal-memory worktree need to be committed.

The `c-12-closeout` skill owns closeout sequencing for worktree-backed tasks.
**Closeout is worktree-only:** every change affecting the code repo runs through a
`c-09-git-worktree-manager` dual worktree (code + memory) — there is no
direct-checkout closeout path. Use the `c-09-git-worktree-manager` skill for
worktree start, attach, status, integration, lifecycle finalization, and cleanup;
use this skill for the closeout gate and code-memory-ledger commit order.

**Seat note (manager -> builder -> reviewer -> curator chain):** in that chain, the builder produces
code and a turn report only — it does not author onboarding. The dedicated curator seat
(`l-01-agent-lifecycles` `roles/curator.md`) runs the `c-05-create-or-update-onboarding-files` skill
as its own fresh pass, fed the leaf's landed change set, task doc, and notes/, BEFORE the owning
seat (the manager) runs this skill's closeout preview. Everywhere below that says "create" or
"refresh" onboarding, that authoring already happened in the curator's pass; the seat running
closeout **verifies** the curator's output against the checks in this skill, it does not author
onboarding inline to make a failing check pass. A check that still fails after the curator pass is a
closeout failure — respawn/rerun the curator, do not patch onboarding from the closeout seat. This
distinction does not apply outside that chain (e.g. a solo flat session with no separate curator
seat still runs `c-05-create-or-update-onboarding-files` itself before closing out).

**Candidate-bound route-review gate:** every code-changing leaf reaches this skill only after an
independent reviewer has written the verdict and per-major-route reports and the owner has called
`task_doc(operation="record_route_review", review={verdict, verdictRef, routes:[...]})`. The plane
stamps the exact current Git candidate tree and verifies the task-relative artifacts. Curator
dispatch and both closeout preview/apply recompute that tree and refuse when the record is absent,
blocking, stale, or points at missing evidence. Direct/solo and builder-verified tiers still require
another agent's review; loop knobs change depth, not this gate. Never supply or remember the tree
hash yourself, and never treat chat prose as a substitute for the task-bound record.

## MCP Tools

Use the worktree closeout tools against the task contract:

```text
worktree_closeout_preview(contract_path="<enclosure series-contract.md>", code_commit_message="<message>", memory_commit_message="<message>", ledger_commit_message="<message>")
worktree_closeout_apply(contract_path="<enclosure series-contract.md>", intent_note="<developer intent>", code_commit_message="<message>", memory_commit_message="<message>", ledger_commit_message="<message>")
worktree_status(repo_id="<repo-id>", contract_path="<enclosure series-contract.md>")
worktree_operation_control(contract_path="<enclosure series-contract.md>", operation_kind="closeout", action="retry|recover|cancel|revise|retire|supersede", expected_generation=<generation>, intent_note="<audit intent>")
worktree_legacy_operation(contract_path="<enclosure series-contract.md>", operation_kind="closeout", action="inspect|migrate|archive", ...)
direct_landing(contract_path="<task-root series-contract.md>", code_commit="<verified branch HEAD>", memory_commit_message="<message>", ledger_commit_message="<message>", intent_note="<authority>", candidate_tree="<gated tree>")
```

Worktree closeout records closeout state in the contract the
`c-09-git-worktree-manager` skill created or attached; the
`c-09-git-worktree-manager` skill owns later integration, lifecycle finalization,
cleanup, and task-document completion.

## Approval Authority

Closeout is always authority-gated, but the authority is contextual.

For standalone work, final super-branch landing, or any closeout where the accepted task/series
authority is unclear, agents must request the matching preview tool first, relay the proposed code,
memory, and ledger commit messages to the developer, and ask for explicit commit approval.

For subordinate work inside an accepted orchestrated series, the owning seat may apply closeout
under delegated series authority after the preview/checks are clean. Managers govern leaf commits;
the orchestrator governs manager/master edges and direct flat work when it is wearing the manager
or worker hat itself. Do not stop for the developer merely because closeout will create code,
memory, and ledger commits. The `intent_note` records the authority source, e.g. the accepted
planner/series task and the owning seat's review of the preview.

Closeout still stops for the developer when the work reaches the final completed super branch /
PR-carryover gate, when a `closeout-approval` gate has been deliberately raised, when the change is
outside the accepted scope, when checks remain red outside the task, when onboarding/memory quality
cannot be repaired inside the leaf, or when a quo-vadis decision is required.

Real closeout uses the matching apply tool with an `intent_note`. The note records the applicable
authority: either explicit developer commit approval or delegated accepted-series authority. Agents
must not treat a vague "looks good" or their own preference as authority.

Every contract-enabled code, memory, and ledger leg also requires its own explicit nonblank commit
message. Preview and apply normalize the same effective input before any claim, worker, journal, or
Git authority is acquired; a blank required cell is a typed no-effect refusal, not an immutable
half-operation. A typed not-applicable leg omits its message instead of receiving a synthesized
default.

Approval remains outside and before apply: preview, relay, and the applicable explicit or delegated
authority must be complete before `worktree_closeout_apply`. Once apply begins, it reruns its
read-only validations and — when code would commit — requires the repository's explicit
certification profile, resets the index, stages the whole task worktree, and runs the leaf
change-set-scoped acceptance contract over exactly that staged content as the first
apply-time gate, before any code, memory, ledger, contract, or applied-gate **commit**. That index
write is the one mutation that precedes the gate, and it is why the gate can see files the task
created rather than only the ones it edited: the adapter receives that staged candidate, and closeout
commits with `git add -A`, so anything not in the index was committed unread.

**Quality altitude ladder.** Leaf acceptance stays mandatory and change-set-scoped: closeout runs
the repository-prescribed acceptance implementation exactly once before creating the leaf code
commit. Leaf integration lands that certified commit without rerunning acceptance. The
repository-prescribed full check runs exactly once per master at the master integration gate;
series/master closeout does not rerun it. `memory_quality_check` is explicitly carved out: it stays
a per-leaf closeout gate. Every code-committing repository requires one explicit
`repositories.<repo-id>.certificationProfile`; missing, ambiguous, invalid, or incomplete authority
refuses as `certification-profile-invalid` before repository execution, never passes silently.

The concrete executor, permitted environment, command arguments, retry semantics, resource policy,
and evidence contract for Gates 1-4 belong to that exact repository-owned profile. Repository
memory such as `system/git-workflow.md`, `system/coding-guidelines.md`, and `system/tools.md`
explains the intended workflow but is not alternate execution authority. Do not discover a fixed
wrapper, infer an executor from this skill, substitute a familiar test runner, or add a default,
compatibility, or host fallback.

The profile declares the exact bounded artifact inventory and authoritative result decoder. The
lifecycle owns atomic enclosure publication and reclamation. Relay the published evidence path
returned by the current result; do not invent a universal repository result filename or assume
another repository's artifact set.

Any retry or proof-reuse behavior is repository policy, not seat discretion. A retry may consume
only the evidence and conditions explicitly authorized by the repository's concrete acceptance
implementation; otherwise rerun that implementation or fail closed.

Staging is **not** undone if the gate refuses. The worktree stays fully staged, nothing is
committed, and that is the intended end state rather than a gap: the checkout being staged is the
task's own worktree, created by `worktree_start` and destroyed by `lifecycle_finalize_task`, so no
one is holding a partial staging in it — and a retry does not inherit that index, because each gate
run begins with `git reset` and restages from the working tree. The reset is what makes the retry
equivalent to a first run rather than an assertion that it is. `git add -A` on its own is not
enough: git applies ignore rules only to paths it does not already track or hold staged, so a file
staged by a refused attempt stays staged even after the retry adds it to `.gitignore`, and the
commit carries it. Resetting first recomputes what gets staged on every run under the ignore rules
in force at that moment, and `--mixed` is index-only, so no file content is touched.

Two refusals guard that staging step, and because they guard it they run exactly where the gate
runs. For every code-certifying closeout, closeout refuses outright before staging anything when the code
checkout is **not** a task worktree (git reports the same `--git-dir` and `--git-common-dir`, which
is what the repository's own checkout looks like; a series/master contract records that path), and
when the code worktree has unresolved merge conflicts. A repository carrying no valid profile has
no legal code-closeout route: preview and apply refuse instead of skipping the gate. Older tasks
without code changes remain valid and do not need a profile merely to be read or recovered.

For a developer-gated closeout, the relay follows the `l-01-agent-lifecycles` orchestrator hand-off protocol: run the
preview/dry-run first, then call
`lifecycle_turn_end_notification(summary={…the preview facts + the commit ask…})` as the **last tool
call**, then deliver the preview facts and proposed messages as plain
chat output ending with the commit ask, and **STOP / end your
turn**. The notification sets the `awaiting-developer` lifecycle state, surfaces a dashboard attention item, and
returns immediately (no wait, no inbox). The developer approves on the dashboard or in the leaf's
attached chat; the **first AR tool call of your next turn** auto-resumes the lifecycle (`running`),
clears the attention item, and runs `worktree_closeout_apply` — you send no explicit `lifecycle_resume`.
Never invoke `worktree_closeout_apply` in the same turn as the relay; the preview report is what the
developer sees.

Apply returns promptly after starting or observing the task-bound closeout generation. Use
`worktree_status` against the configured contract to read the current journal projection. Do not
wait on a queue row or retain an operation id. If the journal advertises a legal next action, feed
that exact generation and action to `worktree_operation_control`: same-generation `retry` or
`recover` preserves immutable accepted input; `cancel` proves worker termination and unchanged Git
state; `revise`, `retire`, and `supersede` use their explicit evidence-aware boundaries. Proven or
ambiguous Git output always reconciles the same generation. Never recover by running Git directly,
repeating from scratch, changing journal bytes, or using a stale queue row.

## Explicit Durable Closeout Gates

`closeout-approval` is the sole human commit gate for code, memory, and ledger when one is
explicitly raised. It gates only admission of the addressed closeout generation; it never freezes
the task document, another sprint, or an already accepted operation. Apply accepts only a current
developer-attributed approval and consumes it once. Open, rejected, revision-requested, applied,
or model-approved gates refuse closeout admission. A model never self-approves a human-pinned gate.

Do not create a durable gate as an incident workaround or compatibility route. Ordinary
subordinate closeouts use recorded accepted-series authority; standalone/final work uses the
developer hand-off above. The closeout preview/apply payload's `closeout_gate` block is evidence of
an explicitly existing gate, not a second lifecycle or recovery mechanism.

## Preconditions

The `c-12-closeout` skill resolves or consumes the current
`c-08-ar-coordination-context-resolver` context. External-memory closeout
requires the code checkout/worktree and memory repo/worktree to be on the same
selected branch; internal-memory closeout commits its memory changes with the
code worktree.

Ledger compatibility is based on code-to-memory commit mappings, not branch
metadata.

Before committing code, run the package-local missing-onboarding check against
current additions:

```text
python -m agents_remember.memory_quality.integrity.check_missing_onboarding --code-repository-root "<code-root>" --onboarding-root "<resolved-onboarding-root>"
```

The check only evaluates files that are new in the current checkout or
worktree, not the whole historical repository. In the manager -> builder ->
reviewer -> curator chain, this check is expected to already pass by the time the owning seat runs
it, because the curator's memory pass created those sidecars through the
`c-05-create-or-update-onboarding-files` skill before this precondition is checked; running the
check here confirms that pass, it is not the trigger to author onboarding from the closing seat. If
it still reports missing onboarding, do not create the sidecars inline — escalate to run (or rerun)
the curator's memory pass, then rerun this check. After the code commit exists, refresh the new
sidecars' verification metadata to that commit during the normal post-code-commit memory refresh.

Changed (already-onboarded) source files have a parallel requirement: their
sidecar content must be updated to approved current state before closeout. The
closeout gate rejects any changed source file whose existing sidecar body was
not modified in the current task, because advancing verification metadata over
stale content defeats the commit-hash-based drift check. In the curator chain, changed sidecars are
updated during the curator's memory pass, not at the metadata-refresh step, and not by the builder
during implementation.

The change set also reads against the resolved memory layer's
`system/coding-guidelines.md` (when present) before the closeout preview. The repository's
acceptance implementation certifies its configured machine checks; it does not read for guideline adherence — a
task identifier in a shipped comment, a new positional boolean flag, an `object`-typed boundary
parameter, or an already-oversize file growing again all pass every rail. Read the change set's
added lines against the guideline file- and function-size budgets, the responsibility and
anti-pattern rules, the source-comment scope, the typed-boundary (DTO) rules, and the D1/D2/D3
stability doctrine; repair what falls inside the task's scope and relay everything else as named
findings at the commit-approval gate. A guideline contradiction that lands unmentioned is a
closeout failure, and in the manager -> builder -> reviewer -> curator chain this read is part of
the reviewer seat's evidence, not something to patch silently at closeout time.

The closeout worklist covers the working tree plus the leaf contract-recorded
committed range: every path changed between the last verified commit (the
contract's recorded closeout commit when present, otherwise the exact task base) and the
work branch HEAD, scoped by the recorded base so synced-in parallel work and
previously closed-out slices never re-gate. Already-onboarded artifacts —
sidecars, route overviews, entity fingerprints — gate on every transported
change regardless of who authored it, merge requests included. Committed-range
paths without onboarding are reported as `unonboarded` (count plus capped
sample) and never block; never-onboarded files are not blanket-onboarded at
closeout. Relay the `unonboarded` count and sample to the developer at the
commit-approval gate so important transported files can be onboarded
deliberately through the `c-05-create-or-update-onboarding-files` skill.

## External-Memory Order

External-memory closeout order is:

Before step 1, require the current passing task-bound route review. Any code edit after review
invalidates its candidate-tree binding and returns to the same route reviewer(s) before curator or
closeout work resumes. For an external-memory leaf, also require `curator_coherence validate`
against the exact contract. The closeout citation preflight, closeout-door evidence, and public
memory readiness all consume that same structured validator; none may parse a hand-authored
Markdown report or search historical filenames. A missing or stale authority returns to
`prepare`/`publish`, never to a compatibility fallback.

1. run `check_missing_onboarding` against current additions (in the curator chain, this confirms the
   curator's pass already covered them — it is not the cue to author onboarding here)
2. if onboarding is still missing, escalate to run/rerun the curator's memory pass through the
   `c-05-create-or-update-onboarding-files` skill before committing code (solo flat sessions with no
   separate curator seat create it directly)
3. after preview and the applicable commit authority are complete, call
   `worktree_closeout_apply`; its initial checks are read-only
4. the citation gate runs BEFORE the leaf acceptance contract and the code commit: `range_resolution` and
   `claim_reopen` over the working tree — a changed construct whose citation is current is only
   the report-only review surface, while a stale pointer, an absent or ambiguous anchor, or
   unverifiable provenance refuses in seconds. The curator clears the same
   `memory_quality_check` during the leaf, so findings here are the exception, not the rule
5. when code would commit, require and admit the exact configured repository profile, reset the
   index, stage the whole task worktree, and run the leaf change-set-scoped acceptance contract over exactly
   that staged content, before any commit; a refusal leaves the worktree staged and commits
   nothing, and the next run's reset means it starts from the working tree either way. The full
   repository check is NOT a leaf gate — it runs once per master at the master
   integration gate. Missing, invalid, incomplete, or candidate-incoherent profile authority
   refuses before repository execution; there is no no-adapter code-commit route.
6. commit code changes and capture `C2` plus its commit date
7. run the `c-02-memory-quality-control` skill's drift check against `C2` to produce the full memory update worklist
8. verify each changed source file's sidecar content was updated in this task (by the curator's pass
   in the chain above), then refresh affected onboarding `lastVerifiedCommitHash` and `lastVerifiedCommitDate` to `C2`; a changed source file with an unmodified sidecar body fails the closeout instead of receiving a metadata-only refresh
9. refresh affected repo entity catalog `git-blob-set-v1` fingerprints against `C2` when changed source paths are listed as entity evidence
10. refresh affected route overview `lastVerifiedCommitHash` / `lastVerifiedCommitDate` metadata to `C2`
11. refresh generated route indexes so `overview.index.json` matches the updated onboarding tree
12. run MCP `memory_quality_check` (the post-refresh sanity phase: drift, document shape, history order); fix reported memory findings before continuing
13. commit memory-content changes and capture `M2`
14. prepend `C2 | M2` to `memory.md`
15. commit the ledger update as `L2`
16. update the task contract closeout state

## Internal-Memory Order

Internal-memory closeout order is:

Before step 1, require the same current passing task-bound route review for every code change.

1. run the same missing-onboarding and changed-sidecar preconditions before preview
2. complete preview and the applicable explicit or delegated commit authority
3. call `worktree_closeout_apply`; its initial validations are read-only
4. when code would commit, require and admit the exact configured repository profile, reset the index, stage the whole
   task worktree, and run the leaf change-set-scoped acceptance contract over exactly
   that staged content, before any commit — a refusal leaves the worktree staged and commits
   nothing, and the next run's reset restages from the working tree regardless. Missing or invalid
   profile authority refuses; internal-memory topology does not create an optional adapter route.
5. commit the code and internal-memory changes together
6. update the task contract closeout state

Entity fingerprints must be refreshed after the code commit and before the
memory-content commit because `git-blob-set-v1` uses `HEAD:<path>` Git blobs.
Reviewing the entity prose can happen before closeout, but the final
fingerprint values must be written in the code-commit-to-memory-commit window.

Route overview metadata and generated route indexes are memory-content changes.
They must be refreshed before `memory_quality_check`, and `memory_quality_check`
must be clean before creating the memory content commit.

Push behavior is not automatic. Closeout commits code, memory, and ledger only;
it never pushes. Pushing the integration branch is part of the landing tail the
`c-09-git-worktree-manager` skill owns: call
`lifecycle_turn_end_notification(summary=…)` as the **last tool call**, then present the push intent as
your final prose, and **STOP**; push only after the developer approves and
your next turn auto-resumes. A separately raised human-pinned `push-approval` gate, when present,
must be decided by the developer; it is not a closeout recovery route.

Closeout does not mark the task `Completed`. After closeout, integration, any
PR-gated merge/pull, and memory carryover are done, use
`lifecycle_finalize_task` from the `c-09-git-worktree-manager` skill to prove the
landed parent-child branch edge, run or verify cleanup, and update the current
task plus immediate parent row.

## Sanctioned Branch-Direct Landing

`direct_landing` is not a direct-checkout closeout path. It is the policy-gated,
branch-addressed counterpart for a **leaf delivered without its own worktree enclosure**, where a
code commit already exists at the exact series branch HEAD. A series contract is necessary address
authority for this route; it is not by itself evidence that an operation is direct execution.
Ordinary master/series closeout and the later master-to-parent `worktree_integrate` edge are not
branch-direct leaf delivery and must work while `directExecutionEnabled` is false.
The tool verifies that code commit and gated candidate tree, requires explicit nonblank memory and
ledger messages for enabled legs, and validates the complete effective input before acquiring
landing authority.

Apply persists a task/contract-addressed `direct-landing` operation generation before either Git
leg. Intent and proof for memory commit, ledger conflict detection, ledger staging, ledger commit,
and terminal publication are journaled independently. After interruption, read the same generation
through `worktree_status` and execute only its advertised action through
`worktree_operation_control(operation_kind="direct-landing", ...)`. Recovery reconciles exact
code/tree/memory/ledger evidence and reuses each already produced commit once; the queue is not an
input. A transient landing lock, synthesized subject, repeat-from-scratch, or raw Git is not
recovery.

## Explicit Legacy Operation Repair

Normal lifecycle readers accept only the current schema. For an exact historical schema-1 record,
use `worktree_legacy_operation(action="inspect")`, bind the returned digest, and then choose the
single supported audited transition: `migrate` fills only proven-missing unfinished memory/ledger
message cells for the known blank-input incident and preserves live code-output evidence in one
canonical generation; `archive` accepts only proven terminal/no-live-authority evidence. Canonical
`worktree_operation_control` then recovers the migrated generation. The legacy tool is explicit and
bounded; it is never called by status, normal journal reads, cleanup, or closeout apply.

## Failure Conditions

Closeout fails without mutation when required onboarding is missing,
verification metadata is missing, external memory is not resolved, the code and
memory checkouts are on different selected branches, or no code or memory
changes exist.

For every code-committing repository, closeout also fails without any commit when its exact
configured profile is missing/invalid, its admitted executor prerequisite is unavailable, or the
leaf change-set-scoped acceptance contract exits non-zero.
It is "without any
commit" rather than "without mutation": closeout resets the index and stages the
whole task worktree before the gate so the gate can see created files, and
**leaves it staged** when the gate refuses. Nothing needs undoing — the next run
resets and restages from the working tree, so it reaches the index a first run
would have reached, and `commit_if_dirty` stages everything regardless. Fix the
reported source, test, coverage, or environment issue, rerun the repository-prescribed acceptance
and closeout preview, and only then retry apply; never bypass the failure with a
direct commit.

The next two refusals are preconditions of that staging step, so they run where the profile gate
runs. A repository with no valid configured profile has no legal code-commit path. A candidate
cannot remove, disable, relocate, or invalidate its profile/adapter and thereby turn required
acceptance into an optional result.

Where the gate runs, closeout refuses before staging anything when the code
checkout is not a task worktree. The test is git's own: in a linked worktree
`--git-dir` and `--git-common-dir` differ, and in a repository's own checkout
they are the same path. `default_series_contract` records `code_worktree` as the
repository path itself, so a series/master contract reaching
`worktree_closeout_apply` would otherwise stage in a checkout a person works in —
overwriting a partial `git add -p` selection, staging files deliberately held
back, and resolving any merge in progress to whatever is on disk. Close out the
leaf contract whose `code_worktree` is the task worktree instead.

Where the gate runs, closeout also refuses before staging anything when the code
worktree has unresolved merge conflicts (an in-progress merge, rebase,
cherry-pick, or revert with unmerged index entries). The refusal names the
conflicted paths. This is a deliberate refusal, not an incidental one:
`git add -A` over an unmerged index resolves every conflict to whatever the
working tree holds, so without this check closeout committed the `<<<<<<<`
markers. Both refusals run before the reset as well as before the add — a
`git reset` drops the unmerged entries and `MERGE_HEAD`, so running it first
would erase the very state the conflict check reads. Resolve the conflicts, stage
the resolutions, then rerun closeout.

Closeout also fails without mutation when a changed source file's existing
sidecar body was not updated in the current task, so verification metadata is
never advanced over stale onboarding content. This applies to committed-range
paths exactly as to working-tree paths — who authored the change does not
matter. Committed-range paths without existing onboarding are the one
exception: they do not fail closeout and are surfaced as `unonboarded` in the
preview and apply payloads for the commit-approval relay.

Worktree closeout also fails when the recorded code or external-memory source
branch moved since task start.

Every enabled blank/whitespace commit-message cell fails before operation authority and leaves no
claim, journal generation, worker, commit, or queue-lifecycle residue. A moved source or stale door
provenance refuses only the landing edge and returns the exact sync/republish route; it never locks
task authoring. Once a generation has been accepted, failures are classified from its journal and
live Git evidence and expose only evidence-safe task-addressed controls.

Missing onboarding is the expected hard failure when the required onboarding file was not produced —
in the manager -> builder -> reviewer -> curator chain that means the curator's memory pass did not
cover it. The next step is to run (or rerun) the curator's `c-05-create-or-update-onboarding-files`
pass for that source file, then rerun the closeout preview; a solo flat session with no separate
curator seat runs that skill itself.

## Boundaries

1. The `c-12-closeout` skill owns closeout approval and code-memory-ledger commit sequencing.
2. The `c-12-closeout` skill does not create worktrees, integrate worktrees, finalize lifecycles, or clean up worktrees.
3. The `c-12-closeout` skill does not initialize memory roots; use the `c-00-initialize-memory-repo` skill.
4. The `c-12-closeout` skill must not commit without the applicable authority after a closeout
   preview: explicit developer commit approval for standalone/final work, or recorded delegated
   series authority for subordinate accepted-series work.
5. The `c-12-closeout` skill must not create a code commit until the repository-prescribed leaf
   change-set-scoped acceptance contract passes for the current candidate. The full check belongs to the
   master integration gate only.
6. The `c-12-closeout` skill must not defer or skip `memory_quality_check`; it stays a per-leaf
   closeout gate even though full repository acceptance belongs to the master integration gate.
7. The `c-12-closeout` skill must not create a memory content commit whose affected onboarding metadata still points at pre-closeout code.
8. The `c-12-closeout` skill must not create a memory content commit before route overview metadata, generated route indexes, and `memory_quality_check` are clean for the new code commit.
9. The `c-12-closeout` skill must not push automatically.
10. The `c-12-closeout` skill must not advance `lastVerifiedCommitHash` / `lastVerifiedCommitDate` for a changed source file whose sidecar content was not updated in the current task; a metadata-only refresh that masks drift is prohibited.
11. The `c-12-closeout` skill must not close out a change set that contradicts the memory layer's `system/coding-guidelines.md` without the contradiction being repaired in scope or named at the commit-approval relay; green acceptance evidence is not evidence of guideline adherence.
12. The `c-12-closeout` skill must not acquire closeout authority until every enabled immutable
    input cell and the exact current door generation have validated.
13. The `c-12-closeout` skill must not recover from a queue row, direct Git, a reports file, a
    permanent compatibility reader, or synthesized commit-message input.
