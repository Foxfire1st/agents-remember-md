## Memory System Awareness

This workspace uses a layered memory system. Understand the layers before acting:

| Layer      | Location         | Purpose                                                         |
| ---------- | ---------------- | --------------------------------------------------------------- |
| onboarding | `onboarding/`    | Code commentary — logic, invariants, conventions, task tracking |
| docs       | `docs/`          | Authoritative reference documentation                           |
| sources    | `docs/sources/`  | References to external technical documentation, mcps, etc.      |
| glossary   | `docs/glossary/` | Canonical vocabulary and cross-repo index                       |
| tasks      | `tasks/`         | Current change intent, plans, decision logs                     |

# heavy-task-workflow

Operational references:

1. `skills/**`
2. `skills/W-01-heavy-task-workflow/workflow/**`

## Process Guard Rails

1.  Use live skills and workflow docs as the operating spec for phase order, artifact names, ownership, and checkpoint gates.
2.  Start from the live repo state and the current task. Change only what the task and workflow require.
3.  Preserve existing behavior, helper structure, and compatible guidance unless the task or workflow explicitly changes, relocates, or retires them.
4.  For `changed-in-place` and `moved` surfaces, implement the required delta. Do not collapse surviving content to the projected slice.
5.  Planning is scheduling-only. Do not introduce new design or contract content there.
6.  Implementation is sequential by default. Record issues first; do not silently promote them into requirement or architecture changes.
7.  Checkpoint reviews are review-only. They assess artifacts and findings; they do not rewrite working artifacts or approvals.

## Workflow Development Guard Rails

Workflow design reference when changing the workflow itself: [onboarding/heavy-task-workflow/overview.md](onboarding/heavy-task-workflow/overview.md)

1. Use onboarding docs when developing or changing the workflow itself, not as the primary operational source for normal coding tasks.
2. Keep separation of concerns: `SKILL.md` owns entrypoint guidance, workflow docs own phase behavior, templates own scaffolds, and validation or check assets own verification.
