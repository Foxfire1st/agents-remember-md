# Light Task Workflow

## Goal

Run the in-between task lifecycle: plan in a task file, stop for approval, implement against a live checklist, and close only after developer confirmation.

The routing heuristic is simple: if the implementation plan still fits on a single page, light-task-workflow is probably the right tool. That is a rule of thumb, not a hard boundary.

Light-task-workflow still follows the same shared discipline documented in `README.md`:

1. drift check before planning when onboarding is part of the planning context
2. approval before implementation
3. onboarding update through `C-05-create-or-update-onboarding-files` after approved changes, with durable findings routed through that skill during implementation when they become clear enough

## Phase 1 — Create Or Update The Task File

### 1. Ensure the local task area exists

All light-task artifacts live under `<task-root>/`, where `<task-root>` is derived from `AR_MANAGEMENT_ROOT` and documented in `<AR_MANAGEMENT_ROOT>/system/settings.md`.

If `<task-root>/` does not exist yet, create it before writing the task file.

### 2. Reuse an existing active task when appropriate

Before creating a new file:

1. search `<task-root>/` for an active task already covering the same scope
2. update that task instead of creating a duplicate when the scope matches

### 3. Name the task file

Use the same naming convention as heavy-task-workflow:

| Origin        | Naming convention                  | Example                       |
| ------------- | ---------------------------------- | ----------------------------- |
| Ticket-linked | `YYMMDD_#<number>_<short-slug>.md` | `260319_#42_update-readme.md` |
| Organic       | `YYMMDD_<descriptive-slug>.md`     | `260319_readme-rewrite.md`    |

### 4. Run drift detection before planning against onboarding

If the task plan relies on onboarding files:

1. invoke `C-02-onboarding-drift-detection` before planning against those files
2. do not plan against drifted or missing-verification pre-existing onboarding until the drift report has been handed off to `C-05-create-or-update-onboarding-files` or the developer has explicitly accepted directional-only trust
3. treat files created or modified during the current task as task-local working state after that initial gate passes; they remain pending verification, but they do not by themselves re-block planning for the same task

### 5. Gather context before writing the plan

Before planning:

1. check `<AR_MANAGEMENT_ROOT>/docs/` for local reference material when it exists
2. check glossary or naming references listed in `<AR_MANAGEMENT_ROOT>/system/sources.md` when they exist
3. check `<onboarding-root>/` for any repo whose behavior or terminology the artifact touches
4. use supporting search or docs tools only when the task domain needs them

### 6. Write the task file

Use `template.md` as the canonical scaffold.

The file must include:

1. objective
2. requirements
3. implementation steps with checkbox steps and checkbox substeps
4. proposed code examples for each distinct implementation change when code changes are in scope
5. decision log
6. open questions
7. references

Status values should align with the repository rules:

1. `planning`
2. `inProgress`
3. `Completed`

### 7. Present the plan and stop for approval

Present a concise summary in chat:

1. objective in one or two sentences
2. the key implementation steps
3. the distinct implementation examples when code changes are in scope
4. any open questions or decisions needed

Then explicitly ask the developer to review the task file.

Do not implement before approval.

Developer outcomes:

1. approve: set status to `inProgress` and continue to Phase 2
2. request changes: update the task file and re-present
3. reject: record the rejection reason in the decision log and stop

## Phase 2 — Implement Against The Live Checklist

### 1. Start from the first unchecked work item

The task file is the live execution checklist.

Implementation starts at the first unchecked checkbox under the approved implementation steps.

### 2. Work step by step

For each implementation section:

1. read the step objective and its checkbox items
2. read the relevant files or materials
3. perform the approved work
4. route durable current-state findings for that implemented slice through `C-05-create-or-update-onboarding-files` as soon as the finding is stable enough to state accurately
5. use the checks listed in `<AR_MANAGEMENT_ROOT>/system/tools.md` for that implemented slice when those checks are available
6. finish any remaining onboarding cleanup for that implemented slice through `C-05-create-or-update-onboarding-files` before considering it done
7. mark a substep complete only after its code or artifact change, its onboarding capture or update through `C-05-create-or-update-onboarding-files`, and its relevant listed checks are done
8. mark the parent step checkbox complete only after its substeps and verification are complete
9. record any meaningful judgment call in the decision log

If `<AR_MANAGEMENT_ROOT>/system/tools.md` is still blank, there may be no repo-specific checks listed yet; the file exists so the developer can fill in that checklist over time.

### 3. Milestone alignment

After each step:

1. re-read the task file
2. confirm the changed work still matches the approved plan
3. if the work drifted materially, stop and update the plan before continuing

### 4. Finish Phase 2

When the approved plan has been fully implemented:

1. confirm the checklist reflects completed code changes, onboarding updates, and listed checks
2. set the task status to `Completed`
3. present a concise completion summary in chat covering what changed, what onboarding was updated, and which listed checks were run

## Phase 3 — Close

Close does not own implementation work. Code changes, onboarding updates, and listed checks all belong to Phase 2 and should already be finished before this phase begins.

Close may still consolidate or polish onboarding language through `C-05-create-or-update-onboarding-files` if needed, but it must not depend on rediscovering durable findings that should have been captured during Phase 2.

### 1. Prepare the completion handoff

When all planned work is complete:

1. present what was done, any deviations, and any deferred items
2. verify that the Phase 2 completion summary still reflects the final state accurately
3. confirm that durable findings discovered during implementation were routed through `C-05-create-or-update-onboarding-files` rather than left implicit in chat history

### 2. Cross-reference check

Before final closure:

1. verify any referenced workflow or skill paths still resolve
2. check whether newly introduced terms belong in the glossary or naming references listed in `<AR_MANAGEMENT_ROOT>/system/sources.md`
3. update any repo-level descriptions that would now be misleading

## Three-touch iteration cycle

When the developer changes scope or requests further changes during implementation, use this cycle.

### Touch 1 — Update the plan before edits

Update the task file first:

| What changed     | Update                                                                           |
| ---------------- | -------------------------------------------------------------------------------- |
| New requirement  | Append it to Requirements with a short annotation noting when it was added       |
| New work slice   | Add a new `S#` section or new checkbox items under an existing section           |
| Changed approach | Rewrite the affected step text and record the reason in the decision log         |
| Deferred work    | Mark it as deferred in the relevant step or note it in a dedicated deferred line |

If the change is significant, get renewed approval before editing files.

### Touch 2 — Implement and present

Do the work for the current slice, update onboarding for that same slice through `C-05-create-or-update-onboarding-files`, run the listed checks for that same slice when available, then update the same checklist:

1. check off completed substeps
2. check off completed parent steps when they are truly done
3. present the result to the developer for review

### Touch 3 — Record the review outcome

Based on developer feedback:

1. approved: keep the completed checkbox state, add any notable decision entry, and continue
2. changes requested: return to Touch 1 and update the plan before editing again
3. rejected: record the rejection in the decision log and revert or defer as appropriate

## Multi-session continuity

If the session ends mid-task:

1. re-read the task file first when resuming
2. continue from the first unchecked checkbox
3. keep step text detailed enough that a fresh agent can recover context quickly

## What This Workflow Does Not Cover

These concerns usually point toward heavy-task-workflow when the single-page light-task plan is no longer a good fit:

1. multi-phase root contracts such as `requirements.md`, `architecture.md`, `requirement_change_candidates.md`, and `architecture_open_questions.md`
2. mandatory checkpoint review packages and adversarial gates
3. multi-phase target-state projection artifacts such as the heavy-task output-documentation layer and implementation-planning package
4. broad cross-repo or high-risk code changes
5. work that no longer fits a compact single-page implementation plan

## Relationship To Heavy Task Workflow

```
Developer request
       │
       ▼
     Clearly needs the full heavy-task workflow? ──yes──▶ heavy-task-workflow
       │
       no
       │
       ▼
     In-between task with a still-compact implementation plan?
       │
       yes
       │
       ▼
  light-task-workflow
       │
      ├─ task file under `<task-root>/`
       ├─ approval gate before implementation
       └─ live checkbox checklist during execution
```
