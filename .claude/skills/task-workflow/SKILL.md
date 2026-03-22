---
name: task-workflow
description: "End-to-end task lifecycle: creation, planning, implementation with paired onboarding updates, review checkpoints, and structured closing with commit workflow. Orchestrates sub-skills for issue retrieval, knowledge base search, Context7 queries, Brave Search, and onboarding drift detection."
---

# Task Workflow

This skill codifies how task files interact with the rest of the documentation ecosystem end-to-end. The non-negotiable behavioral rules (R1–R6) are defined in [CORE_RULES.md](../../../CORE_RULES.md) — read them before starting. This skill builds on those rules with workflow-specific mechanics.

**Core principle:** Onboarding is a **read-first, update-at-transitions** resource — not a write-once-at-close artifact. Onboarding files are touched at defined transition points in the task lifecycle (not on every individual file edit). This keeps documentation accurate while avoiding the overhead of per-edit pairing, and preserves context against LLM compression events: if the context window is compressed mid-session, the agent can re-read onboarding MDs updated at the last transition to recover state.

**Tracking model:** The `<Task>` block's scope in each onboarding file is the **atomic tracking unit**. The task file's substep is the **batch boundary** — all files in a substep transition together.

---

## Sub-skills

This skill orchestrates several standalone skills. Each can also be used independently outside of a task context.

| Skill | Purpose | Invoked during |
|---|---|---|
| `discovery` | Top-down discovery order and cross-repo relationship techniques | 2.1 (context gathering), 2.2 (cross-repo awareness) |
| `issue-tracker` (example: `gitlab-ticket-retrieval`) | Retrieve issue context (details, comments, linked issues) | 1.3 (requirements gathering) |
| `knowledge-base` (example: `confluence-search`) | Search/retrieve knowledge base pages, sync to local docs | 1.3 (requirements), 2.2 (research) |
| `context7-query` | Query library documentation via Context7 | 2.2 (research), 3.2 (implementation) |
| `brave-search` | Web research via Brave Search MCP | 2.2 (research), 3.2 (implementation) |
| `onboarding-drift-detection` | Git-diff-based onboarding staleness detection | 2.1 (before plan), 4.3 (post-commit hash stamp) |
| `create_or_update-onboarding-file` | Onboarding MD template and maintenance | 2.4 (set `planned`), 3.4 (verify + `readyForReview`) |
| `repo-bootstrap` | Bootstrap onboarding for under-documented repos | 2.1 (when target area has no onboarding) |
| `light-task-workflow` | Task lifecycle for non-code artifacts (docs, skills, configs) | 1.1 (when target is non-code) |

> **Note:** The `issue-tracker` and `knowledge-base` entries above are placeholders. This repo ships with example integrations for GitLab and Confluence — adapt them to your own tools (Jira, Linear, Notion, etc).

---

## Phase 1 — Creating the task file and gathering requirements

### 1.1 When to create a task file

**Two triggers:**

1. **Developer-initiated:** The developer explicitly asks to create a task file, or provides a ticket number/link.
2. **Agent-initiated:** The agent notices that a request is growing complex enough to warrant structured tracking, and **proactively asks** the developer if a task file should be created. The agent should not silently decide — it must surface the observation and let the developer confirm.

**Thresholds that signal task-file territory** (for the agent's judgement):
- **Size:** More than ~3 files touched, or changes span multiple components/layers.
- **Risk:** Changes affect invariants, cross-repo contracts, or public APIs.
- **Cross-file scope:** Logic changes in one file require coordinated changes elsewhere.
- **Duration:** Expected to take more than one session.
- **Ambiguity:** Multiple implementation approaches exist and a decision log is needed.

If none of these apply, use `light-task-workflow` instead — every change gets a task file.

**Non-code artifacts or small code changes below threshold → use `light-task-workflow` instead.**
If the target is a non-code artifact — documentation, skills, configs, READMEs, presentations — use the `light-task-workflow` skill, not this one. Also use `light-task-workflow` for small code changes that don't meet the complexity thresholds above but still need a plan and approval gate. Light-task-workflow provides the same planning discipline and approval gate without the onboarding machinery. Non-code artifacts always go through light-task-workflow; small code changes go through it when they need planning but don't warrant the full lifecycle.

### 1.2 Naming conventions

All task files live in `tasks/` and are date-prefixed with `YYMMDD` format. This ensures chronological sorting and makes future year-based folder organisation trivial (move all `26*` files into a `2026/` folder).

| Origin | Naming convention | Example |
|---|---|---|
| Issue tracker ticket | `YYMMDD_#<number>_<short-slug>.md` | `260318_#1588_alarm-sim-command.md` |
| Organic / ad-hoc | `YYMMDD_<descriptive-slug>.md` | `260318_task-workflow-skill.md` |

For ticket-based tasks, `<short-slug>` is an **English kebab-case summary** derived from the ticket content — not a transliteration of the ticket title if it's in another language.

### 1.3 Requirements gathering

Before writing the task file content, gather requirements from available external sources.

#### From your issue tracker

If you have an issue tracker integration (e.g., the `gitlab-ticket-retrieval` example skill), invoke it with the ticket number or URL. The skill retrieves the issue, comments, linked issues, and knowledge base references. Its structured summary feeds into the task file's Objective and Problem Analysis.

#### From your knowledge base

If you have a knowledge base integration (e.g., the `confluence-search` example skill), search for domain context related to the task area:
- Domain terms and specs relevant to the code being changed.
- Protocol definitions, API contracts, architecture docs.
- Any links found in the issue tracker (from the previous step).

#### Include a References section

When external sources inform the task, the task file should include a "Sources" or "References" subsection:
- For knowledge base pages: note whether they're in the local `docs/` collection or only live externally.
- For ticket requirements: summarise, don't copy — the ticket is the source of truth for stakeholder intent.

### 1.4 Create the task file

Use the `YYMMDD` naming convention. The task file uses this template structure:

```markdown
# Task: <Title>

**Status:** Planning
**Repo:** <primary repo>
**Type:** <Feature | Bugfix | Refactor | Investigation>
**Created:** <YYYY-MM-DD>
**Ticket:** <#number or N/A>

---

## Objective

<What and why — 2-3 sentences.>

---

## Problem Analysis

<Current state. What's wrong. Root causes. Source-specific findings.>

---

## Sources / References

- Issue tracker: <link or ticket number>
- Knowledge base: <pages consulted, with local path if synced>
- Other: <any other authoritative sources>

---

## Current State (baseline)

<Tables/inventories of what exists before changes. Relevant code, configuration, data.>

---

## Implementation Steps

### Step 1 — <title>

<Phased plan with substeps. Include code snippets from Context7 where relevant.>

---

## Files Affected

<Explicit list of source files that will be created, modified, or deleted.>

---

## Constraints

<Non-functional requirements, deployment considerations, backward compatibility.>

---

## Alternatives Considered

<What else was evaluated and why not.>

---

## Open Questions

<Unresolved items with status tracking.>

---

## Decision Log

| # | Date | Decision | Rationale |
|---|---|---|---|
| 1 | <date> | <what was decided> | <why> |

---

## Accepted Decisions

1. <Quick-reference list of what's agreed.>

---

## Out of Scope

<Explicit boundaries — what this task will NOT do.>

---

## Onboarding / Documentation Updates Needed

<Which onboarding files need creation or updating. Planned new files.>
```

### 1.5 Discuss with the developer

Present a concise summary in chat. The detailed plan lives in the task file. Wait for approval before implementing.

---

## Phase 2 — Planning

### 2.1 Drift detection and context gathering

This happens **after the decision to create a task file including the info from phase 1 but before writing the plan meaning before heading to Phase 2**.

1. **Invoke the `onboarding-drift-detection` skill** with the files likely in scope.
   - If any onboarding files have drifted from their `lastVerifiedCommitHash`, update them now.
   - This catches changes made outside the task workflow — hotfixes, other developers' commits, refactors.
   - The agent now has accurate onboarding to plan against.

2. **Invoke the `discovery` skill** — follow the top-down reading order to gather context from the now-accurate docs. Additionally:
   - Note all active `<Task>` blocks — check for collisions with planned work.
   - Note all `readyForReview` / `needsFixes` tasks — flag these for developer attention.
   - Follow Docs References to relevant reference documentation for domain understanding.

### 2.2 Research (feeds into the plan)

These steps happen **after 2.1 and before writing the implementation plan**. Their findings shape which files are affected, which patterns to use, and what constraints apply.

#### Cross-repo awareness

1. Check if the code area communicates with other repos (API calls, WS events, message queues).
2. If changes affect a cross-repo interface: document in the task file which repos are affected and whether they need coordinated changes.
3. Check the relevant onboarding Cross-repo References section.
4. **Actively discover ties** using the techniques in the [`discovery` skill](../discovery/SKILL.md) — don't rely only on existing docs.

#### Knowledge base / domain research

If you have a knowledge base integration, search for domain knowledge beyond what was linked from a ticket (1.3). Search for specs, protocols, and domain definitions that govern the area being changed.

**When to search:**
- The task touches a **protocol or command set** — e.g., changing how events are published → search for the event protocol spec.
- The task involves **external service behavior** — e.g., modifying device provisioning → search for the provisioning flow docs.
- The task changes a **cross-system interface** — e.g., modifying WebSocket message handling → search for the protocol definition or event catalog.
- An **unfamiliar domain term** appears in the code or onboarding → search the knowledge base before guessing.
- The task affects **business rules or workflows** — e.g., changing how alerts are escalated → search for the escalation workflow definition.

#### Query library documentation

**Invoke the `context7-query` skill** for each library/framework the implementation will use.

- Verify the planned approach uses current APIs.
- Check for deprecated patterns.
- Add relevant code snippets to the task file's Implementation Steps to ensure correct patterns during implementation.

#### Research via web search

**Invoke the `brave-search` skill** when:
- The implementation involves patterns not well-covered by library docs alone.
- You need ecosystem awareness: blog posts, migration guides, release notes.
- The task involves a library version newer than the agent's training data.

Record findings in the task file under a "Research Notes" subsection or directly in the relevant implementation step.

### 2.3 Write the plan and get approval

Using the findings from 2.1 (onboarding context) and 2.2 (research), write the implementation plan in the task file:
- Implementation Steps with substeps.
- Files Affected list.
- Constraints, alternatives, open questions.
- Present to the developer for approval. Wait for sign-off before proceeding.

### 2.4 Set `planned` on all affected onboarding files

This happens **after the developer approves the plan**. Only now do you know which files will be affected.

1. **For new source files** — create their onboarding MDs immediately (before the code exists) using the `create_or_update-onboarding-file` skill:
   - Use intent-oriented language ("Will handle…", "Responsible for…").
   - Include a `<Task>` block with `progressStatus: "planned"`.

2. **For existing source files** — update their onboarding MDs:
   - Add a `<Task>` block with `progressStatus: "planned"` if one doesn't exist.
   - This creates a breadcrumb trail: anyone reading onboarding sees what's about to change.

3. **Batch operation:** All files listed in the task file's "Files Affected" section get their `<Task>` blocks set in one pass.

---

## Phase 3 — Implementation

Each substep in the task file's Implementation Steps defines a batch of files. Onboarding is touched at substep boundaries, not on every individual file edit.

### 3.1 Substep starts — batch-set `in-progress`

When beginning work on a substep:

1. **Identify the files** in this substep from the task file's Implementation Steps.
2. **Read their onboarding MDs** — refresh context on logic, invariants, cross-repo ties, and the `<Task>` scope.
3. **Batch-set `progressStatus: "in-progress"`** on all files in the substep.
4. Update the `progress` field with the substep reference and current intent.
5. Update `lastUpdated` (use the available time tool).

Then proceed with the actual code changes across those files.

### 3.2 Library verification during implementation

When using a specific component, hook, or API for the first time in a substep:
- Check the task file's Implementation Steps for code snippets gathered during planning.
- If the substep uses something not previously checked, **invoke the `context7-query` skill** to verify it's not deprecated.

### 3.3 Substep implemented — verify and batch-set `readyForReview`

When the agent considers a substep done:

1. **For each file in the substep**, re-read the onboarding MD's `<Task>` block.
2. **Verify scope fulfilment:** Does the `<Task>` description match what was actually implemented?
   - Check: `target` — was the specified part of the file changed?
   - Check: `description` — does the change match what was planned?
   - Cross-reference the task file's substep to confirm alignment.
3. **Three outcomes per file:**
   - **Scope fulfilled:** Batch-set `progressStatus: "readyForReview"`.
   - **Scope partially fulfilled:** Complete the remaining work, then set `readyForReview`.
   - **Scope conflict** (implementation diverged from plan): Ask the developer how to proceed — update the plan, adjust the scope, or revert.
4. **Update Code Commentary** in each file's onboarding MD to reflect the new code state.
5. **Update `lastUpdated`** on all affected MDs.
6. **Update the task file** with substep completion status.

**Why verify at this point:** The `<Task>` scope is the contract between planning and implementation. Checking it here — not at task close — catches drift early. If the implementation diverged, it's cheaper to fix now than after all substeps are done.

### 3.4 User reviews — batch-set `completed`

`readyForReview` is the agent's equivalent of "checking the checkbox." It signals: the scope defined in this file's `<Task>` block is implemented and the agent considers it done.

**What happens during review:**
- The developer reviews files at their own pace — some may be approved while others get feedback.
- Multiple files can be in `readyForReview` simultaneously.

**Developer review outcomes:**
- **Approved:** Batch-set `progressStatus: "completed"` on all approved files. Only the developer can trigger this transition.
- **Changes needed:** Set `progressStatus: "needsFixes"`. Document the feedback in the task file's decision log. After fixes, return to `readyForReview`.

**Stale review detection:** If files are stuck in `readyForReview` for an extended period (visible when reading onboarding at the start of a new session), nudge the developer for feedback.

---

## Phase 4 — Closing a Task

### 4.1 Summary and confirmation

Before entering the mechanical closing steps, present the developer with a summary:

1. **Compile a completion summary:**
   - List all files changed, with a one-line description of what changed in each.
   - Note any deviations from the original plan (scope changes, dropped items, added items).
   - List any follow-up work or deferred items.
   - Reference the task file's decision log for key decisions made during implementation.

2. **Present to the developer** for final review. This is the last chance to catch omissions before the commit.

3. **Developer confirms** → proceed to closing steps below.

### 4.2 Final onboarding sync

1. Verify all Code Commentary reflects the final code state.
2. Confirm all `<Task>` blocks are at `progressStatus: "completed"` (set during 3.4 reviews).
3. Update `lastUpdated` on all affected onboarding MDs.

### 4.3 Move completed tasks to Update History

For each `<Task>` block with `progressStatus: "completed"`:
1. Remove the `<Task>` block from `## Tasks`.
2. Add an entry to `## Update History`:
   ```
   - YYYY-MM-DD — <what changed and why> (task: <task-file>.md)
   ```
3. Newest entries first.

### 4.4 Commit workflow

1. **Check for pending work:** Confirm no `needsFixes` or `in-progress` items remain. If any remain, resolve them first.

2. **Ask the developer:** "Should I create a commit?"
   - If yes: suggest a commit message following the project's commit format.
   - Developer approves the message or provides their own.
   - Agent commits with the agreed message.

3. **Post-commit onboarding update:** Invoke the `onboarding-drift-detection` skill in **hash-stamp mode** — it updates `lastVerifiedCommitHash` and `lastVerifiedCommitDate` on all affected onboarding files to the new commit hash. This is a metadata-only pass since content was maintained during implementation.

### 4.5 Task file finalisation

1. Set the task file status to "Completed" with the current date.
2. Add a final decision log entry summarising the outcome.
3. Note any follow-up work or deferred items.
4. The developer moves the task file to `tasks/archive/` (not deleted).

### 4.6 Cross-reference and cleanup check

1. Verify component overview files reflect the changes.
2. Check if glossary terms need updating.
3. Verify instructions files are still accurate.
4. **If source files or folders were deleted:**
   - Delete the mirrored onboarding files or folders.
   - Update any instructions files that referenced the deleted area.
   - Check the glossary for terms that may now be orphaned.
5. **If source files were renamed or moved:** rename or merge the corresponding onboarding files.

---

## Invocation from Prompts

The `start-task.prompt.md` and `close-task.prompt.md` prompts serve as lightweight entry points:

- **start-task** → reads this skill and follows Phase 1 + Phase 2.
- **close-task** → reads this skill and follows Phase 4.

Phase 3 (implementation) runs as part of normal work — it doesn't need a separate prompt trigger.

---

## Relationship to CORE_RULES.md and AGENTS.md

This skill **extends and details** the rules in [CORE_RULES.md](../../../CORE_RULES.md) and [AGENTS.md](../../../AGENTS.md); it does not replace them. Specifically:

| Rule source | This skill provides |
|---|---|
| CORE_RULES.md R2 (plan in file) | Phase 1 — naming, template, requirements gathering |
| CORE_RULES.md R3 (approval gate) | 1.5, 2.3 — approval checkpoints within phases |
| `discovery` skill | 2.1 — top-down reading order; 2.2 — cross-repo discovery techniques |
| `create_or_update-onboarding-file` skill | 3.3 — verify and update onboarding at substep completion |
| AGENTS.md "source-of-truth hierarchy" | Task file = change intent; onboarding = code commentary; docs = reference |
