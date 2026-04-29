---
name: W-02-light-task-workflow
description: "In-between task workflow for work that needs a durable task file, distinct implementation examples, and an approval gate but still fits a single-page implementation plan as a rule of thumb."
---

# W-02 Light Task Workflow

This skill is the thin orchestration contract for the in-between case: work that needs a durable task file, an explicit approval gate, distinct implementation examples, and decision tracking, but still fits a single-page implementation plan as a rule of thumb.

## Companion Files

1. `workflow.md`
2. `template.md`

## When To Use

Use this workflow when:

1. the task sits between chat-mode work and the full heavy-task lifecycle
2. the work needs a plan, approval gate, and decision tracking, but the implementation plan can still fit on a single page as a rule of thumb
3. the target may be non-code or a small isolated code change, provided the lighter single-page plan remains a good fit

Treat the single-page-plan test as guidance rather than a hard routing rule. Use `W-01-heavy-task-workflow` when the task clearly needs richer phase artifacts, broader coordination, or the light-task plan starts to sprawl beyond that compact shape.

## Task Artifact

Light-task-workflow maintains one task file under `<task-root>/`, where `<task-root>` is derived from `AR_MANAGEMENT_ROOT` and documented in `<AR_MANAGEMENT_ROOT>/system/settings.md`.

Naming follows the same convention as heavy-task-workflow:

1. ticket-linked: `YYMMDD_#<number>_<short-slug>.md`
2. organic: `YYMMDD_<descriptive-slug>.md`

Use `template.md` as the canonical scaffold. Implementation steps and substeps are tracked with checkboxes, and that checklist is the live execution state during implementation. When code changes are in scope, the task file also carries proposed code examples for each distinct change type so the developer can review the intended implementation shape before approval.

## Agent Responsibilities

The agent should:

1. stay in direct discussion with the developer
2. search `<task-root>/` for an existing active task covering the same scope before creating a new one
3. create or update the task file using `template.md`
4. keep requirements, implementation steps, and decisions aligned with the latest approved intent
5. treat the task file's checkboxes as the live implementation tracker
6. include proposed code examples for each distinct implementation change when code changes are in scope
7. run `C-02-onboarding-drift-detection` before planning against onboarding files
8. stop for approval before implementation
9. after approval, treat code changes, onboarding propagation through `C-05-create-or-update-onboarding-files`, and the checks listed in `<AR_MANAGEMENT_ROOT>/system/tools.md` as one implementation cycle
10. set the task status to `Completed` once the approved implementation cycle is finished

## Context Gathering

Before planning, check:

1. `<AR_MANAGEMENT_ROOT>/docs/` for relevant local reference material when it exists
2. glossary or naming references listed in `<AR_MANAGEMENT_ROOT>/system/sources.md` when they exist
3. `<onboarding-root>/` for any repo whose behavior the artifact touches

Optional supporting tools such as Confluence search, Brave search, or Context7 may still be used when the task domain needs them, but they are not mandatory here.

## Invariants

1. Every light-task change gets a task file.
2. The task file is the living contract for requirements, checklist state, decisions, and proposed code examples.
3. When onboarding files are part of planning context, drift is checked before planning using `C-02-onboarding-drift-detection`.
4. No implementation begins before explicit developer approval.
5. Implementation steps and substeps use checkbox state rather than freeform progress prose.
6. Code-changing light tasks include code examples for each distinct implementation change.
7. After approval, onboarding is updated through `C-05-create-or-update-onboarding-files` and the listed checks in `<AR_MANAGEMENT_ROOT>/system/tools.md` are run.
8. Durable current-state findings discovered during implementation are routed through `C-05-create-or-update-onboarding-files` during that implementation cycle or, if consolidation is clearer, in the immediate closeout pass right after implementation.
9. Significant mid-implementation changes update the task file before edits continue.

## Relationship To Other Instructions

This skill extends the repository instructions and agent definitions. It does not replace them.

Read `workflow.md` for the phase behavior and `template.md` for the task-file structure.
