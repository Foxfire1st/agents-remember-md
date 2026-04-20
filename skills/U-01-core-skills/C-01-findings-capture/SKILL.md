---
name: C-01-findings-capture
description: "Capture confirmed findings, route them to the right durable task-local path, and propagate factual current-state clarifications into onboarding immediately when allowed."
---

# C-01 Findings Capture

Use this skill when durable knowledge emerges during developer discussion, task execution, review, or direct clarification and should not remain stranded in chat.

Companion file:

1. `findings-capture-workflow.md`

## Operating Modes

1. Inside a heavy-task-workflow task.
2. Standalone during direct clarification.

## Durable Destinations

Route the finding to the owning durable surface:

1. `requirement_change_candidates.md` when the finding changes scope, requirements, or developer intent.
2. `architecture_open_questions.md` when the finding exposes a structural decision, dependency boundary, or design uncertainty.
3. The active phase artifact when the finding is planning-, implementation-, or review-local and belongs to the current task flow.
4. Onboarding through `C-05-create-or-update-onboarding-files` when the finding is a verified factual current-state clarification that should survive outside the task.

## Rules

1. Verify relevant code and onboarding before durable capture when possible.
2. Ask targeted follow-up questions if the finding is still ambiguous.
3. Do not rewrite approved `requirements.md` or `architecture.md` directly from one finding without developer approval.
4. Do not send speculative, future-state, unresolved, or task-only notes into onboarding.
5. If a finding affects both task-local artifacts and onboarding, update the task-local owner first, then propagate to onboarding only if the factual guardrail is satisfied.
6. Use broader discovery only when direct code and onboarding checks are not enough.
7. Never leave a confirmed finding only in chat when an appropriate durable destination exists.
