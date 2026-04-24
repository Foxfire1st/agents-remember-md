---
name: W-01-heavy-task-workflow
description: "Orchestrate the full heavy-task lifecycle, maintain the root task contracts, present Creation naming options, ask D-01 and D-02 questions one by one, and route work to the phase-local skills."
---

# W-01 Heavy Task Workflow

This skill is the thin orchestration contract for the full heavy-task workflow. It stays focused on developer interaction, root-artifact ownership, routing to the phase-local skills, and checkpoint transitions rather than re-embedding detailed phase instructions.

## Workflow Files

1. `workflow/P-00-creation.md`
2. `workflow/P-01-research.md`
3. `workflow/P-02-synthesis.md`
4. `workflow/P-03-design.md`
5. `workflow/P-04-planning.md`
6. `workflow/P-05-implementation.md`
7. `workflow/P-06-closure.md`

## Supporting Templates

1. `templates/task-structure.md`
2. `templates/requirement-change-candidates-template.md`

## Orchestrator Responsibilities

The orchestrator should:

1. stay in direct discussion with the developer
2. during Creation, present task-folder naming options and wait for explicit developer selection before scaffolding
3. ask D-01 and D-02 questions one by one and wait for explicit answers
4. maintain `task.md`, `requirements.md`, `architecture.md`, `requirement_change_candidates.md`, `architecture_open_questions.md`, and each phase `progress.md`
5. route work to the appropriate phase-local skill instead of absorbing every phase locally
6. trigger `R-01-adversarial-review` at the defined checkpoints
7. route factual current-state findings through `C-01-findings-capture` and `C-05-create-or-update-onboarding-files` when allowed
8. ensure durable implementation discoveries are captured through `C-05-create-or-update-onboarding-files` during implementation when stable, or at latest in the immediate closure pass, so later sessions do not need to rediscover them

## When To Use

Use this workflow when the task spans multiple files, crosses component or repository boundaries, needs durable phase artifacts, or is likely to continue across sessions.

## Delegation Model

1. Phase workflow docs own phase behavior, phase ordering, gates, and artifact expectations.
2. Phase-local skills own the work products inside their subphase folders.
3. This entrypoint owns orchestration, cross-phase state, and developer interaction.
4. Checkpoint review stays independent through `R-01-adversarial-review`.

## Root Artifact Ownership

The orchestrator is the sole writer of the root contracts, staging artifacts, and phase progress trackers:

1. `task.md`
2. `requirements.md`
3. `architecture.md`
4. `requirement_change_candidates.md`
5. `architecture_open_questions.md`
6. `P-XX-<phase>/progress.md`

## Context Recovery

When resuming after context compression or a new session:

1. Read `task.md` to recover the active phase and current objective.
2. Read the active phase `progress.md`.
3. Load additional task artifacts only when the active phase requires them.

Before continuing, confirm that `task.md` still reflects the latest approved developer intent.

## Invariants

1. The orchestrator stays in direct discussion with the developer.
2. Requirement and architecture approvals remain explicit developer actions.
3. Root contracts stay separate from phase-owned working artifacts.
4. Planning remains scheduling-only.
5. Implementation runs through the sequential `I-01-implementation` package.
6. Durable current-state findings are documented through `C-05-create-or-update-onboarding-files` during the task rather than left only in chat history.

## Relationship To Other Instructions

This skill extends the repository instructions and agent definitions. It does not replace them.
