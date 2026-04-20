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

Output documentation should be read as a target-state change map across existing repository surfaces.

It names what implementation must change and what is explicitly retired, but it does not act as a completeness filter for everything else already present in a touched artifact.

When a live file, package, or workflow surface overlaps with projected output documentation, the overlap marks the slice that must be updated. The rest of the existing surface survives by default unless the approved change set explicitly replaces it, relocates it, contradicts it, or retires it.

If the survival of existing material is unclear, implementation should surface an issue for discussion rather than infer deletion from silence.

That means the Coder agent:

1. executes steps in dependency order
2. checks off completed steps in `implementation_plan.md`
3. runs the verification note attached to each completed step
4. records issues in the plan's issues section when problems appear

Issues found during implementation are not automatically turned into requirement or architecture intake.

Discussion decides whether an issue is:

1. resolved within the approved path
2. discarded as a non-issue
3. turned into requirement intake
4. turned into architecture intake

Only after that discussion does the orchestrator update staging artifacts when needed.

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

It may also read supporting input documentation and prior phase artifacts when needed for execution clarity.

---

## Workflow

### 1. Start from the first unchecked step

Implementation starts at the first unchecked checklist step in `implementation_plan.md`.

There is no wave scheduling and no parallel batch dispatch in the MVP model.

### 2. Execute steps sequentially

The Coder agent works through the plan from top to bottom.

For each step, it should:

1. read the step and its dependencies
2. read the relevant code and output documentation for that step
3. make the required code changes
4. run the step's verification note
5. check off the step in `implementation_plan.md` once the verification passes

### 3. Record issues in the plan

If the Coder agent finds a problem while implementing a step, it records that problem in the issues section of `implementation_plan.md`.

An issue entry should make clear:

1. where the issue was found
2. which step or file it affects
3. why it matters
4. whether it appears to be a requirement issue, an architecture issue, or a local implementation issue
5. what discussion should likely decide next

If the issue does not block the current approved path, the Coder agent may continue.

If it blocks safe progress, the Coder agent should stop after recording it and wait for discussion.

### 4. Discuss issue outcomes

Issues recorded during implementation are discussed with the developer through the orchestrator.

Discussion may decide to:

1. resolve the issue and continue implementation
2. discard the issue as not requiring workflow change
3. turn the issue into requirement intake in `requirement_change_candidates.md`
4. turn the issue into architecture intake in `architecture_open_questions.md`

The issue itself does not automatically become intake just because it was discovered.

### 5. Finish the execution pass

Implementation ends the execution pass when:

1. every checklist step is either checked off or explicitly blocked by a discussed issue
2. verification has been run for the completed steps
3. the plan accurately reflects what was completed and what remains blocked

### 6. Write `implementation_results.md`

After the sequential implementation pass, write `implementation_results.md`.

It should summarize:

1. files changed
2. interfaces introduced or changed
3. verification results
4. deviations from the plan or output documentation
5. issue outcomes: resolved, discarded, or turned into intake

### 7. Present for implementation approval

Implementation is not complete just because the code changed.

The phase ends only after the implementation is presented and approved.

If approval is withheld, implementation continues from the updated plan and issue state rather than pretending the phase closed.

---

## Non-Goals

Implementation does not:

1. run parallel batches of work
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
