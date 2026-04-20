# Implementation Phase

## Purpose

This document defines how the heavy-task workflow executes the approved implementation plan.

Implementation is intentionally simple in the MVP model.

One Coder agent works through the implementation plan from start to finish, step by step, without waves and without parallel execution.

---

## Core Identity

Implementation is execution, not redesign.

The phase works from the already approved:

1. requirements
2. architecture
3. output documentation
4. implementation plan

The implementation plan remains the live execution checklist during this phase.

Output documentation defines the approved change set across real existing surfaces.

It is not a completeness filter for every touched file or package, and touching an artifact does not mean compressing it down to only the documented overlap.

---

## Inputs And Preconditions

Implementation begins only after:

1. the implementation plan has been approved
2. the approved requirements contract exists in `requirements.md`
3. the approved architecture contract exists in `architecture.md`
4. the output documentation package exists under `P-03-design/D-04-output-documentation/`

The Coder agent should read:

1. `implementation_plan.md`
2. `requirements.md`
3. `architecture.md`
4. `P-03-design/D-04-output-documentation/`
5. the relevant code

---

## Workflow

### 1. Start from the first unchecked step

Implementation starts at the first unchecked checklist step in `implementation_plan.md`.

There is no wave scheduling and no parallel batch dispatch in the MVP model.

### 2. Execute steps sequentially

For each step, the Coder agent should:

1. read the step and its dependencies
2. read the relevant code and output documentation for that step
3. identify the approved delta for the touched surfaces instead of treating the output documentation as an allow-list for the whole artifact
4. keep existing compatible material in place unless the approved change set explicitly updates it, relocates it, contradicts it, or retires it
5. remove existing material only when that removal is explicitly justified by the approved requirements, architecture, output documentation, or implementation plan
6. record an issue if it is unclear whether existing material survives, instead of deleting by assumption
7. make the required code changes you can do safely
8. run the step's verification note
9. check off the step in `implementation_plan.md` once the verification passes

### 3. Record issues in the plan

If the Coder agent finds a problem while implementing a step, it records that problem in the issues section of `implementation_plan.md`.

Issues are discussed with the developer through the orchestrator. They are not automatically turned into requirement or architecture intake.

### 4. Finish the execution pass

Implementation ends the execution pass when:

1. every checklist step is either checked off or explicitly blocked by a discussed issue
2. verification has been run for the completed steps
3. the plan accurately reflects what was completed and what remains blocked

### 5. Write `implementation_results.md`

After the sequential implementation pass, write `implementation_results.md` summarizing files changed, interfaces introduced or changed, verification results, deviations, and issue outcomes.

### 6. Present for implementation approval

Implementation is not complete just because the code changed. The phase ends only after the implementation is presented and approved.

---

## Non-Goals

Implementation does NOT:

1. silently drop code and features without the implementation plan or outputs explicitly retiring it!
2. invent new design
3. silently absorb requirement changes
4. silently absorb architecture changes
5. close the task on its own

---

## Exit Rule

Implementation ends only after:

1. the implementation has been approved
2. issue outcomes have been discussed
3. `implementation_results.md` exists

Only then may the workflow move to Closure.