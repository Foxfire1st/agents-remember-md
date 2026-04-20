# Creation Phase

## Purpose

This document defines how the heavy-task workflow starts a task, captures raw intake, and refreshes onboarding before Research begins.

Creation is the setup phase.

It preserves the developer's ask and any explicit technical direction without prematurely normalizing or approving either one.

---

## Core Identity

Creation owns task start, not task interpretation.

The phase does five things:

1. creates the task folder structure
2. creates the orchestrator-owned root artifacts
3. records raw requirement intake
4. records raw architecture intake
5. refreshes onboarding enough for Research to begin safely

Creation does not normalize requirements, resolve requirement questions, or settle architecture.

---

## Artifacts Created Or Initialized

Creation initializes these runtime artifacts:

1. `task.md`
2. `requirement_change_candidates.md`
3. `architecture_open_questions.md`
4. `P-00-creation/progress.md`

If relevant onboarding is stale or missing, Creation also triggers onboarding refresh work before Research begins.

---

## Inputs

Creation works from:

1. the developer request
2. any linked tickets, specs, examples, or prior discussion
3. the existing codebase and onboarding landscape

If the developer already states a desired technical direction, Creation captures that as raw architecture intake. It does not silently treat it as approved architecture.

---

## Workflow

### 1. Create the task folder

Creation starts by getting an explicit developer choice for the task folder name and then establishing the task folder and the phase-local `progress.md`.

Before scaffolding, the orchestrator presents task-name options in direct discussion and waits for the developer to choose one.

The required options are:

1. a branch-based option derived from the current branch when a branch name is available
2. the normal task-intent-based suggested options that follow the workflow naming convention
3. a custom option where the developer provides the slug or the full task-folder name

If branch information is unavailable, the orchestrator should say that explicitly and still present the suggested and custom options.

After the developer chooses, the orchestrator normalizes the result to the canonical `YYMMDD_slug` pattern and creates the folder.

The orchestrator owns this setup.

### 2. Write `task.md`

Creation writes the initial `task.md`.

At this stage `task.md` is a global tracker, not the approved requirements contract.

It should contain at least:

1. objective
2. developer notes
3. execution order
4. phase progress skeleton
5. links to the runtime artifacts as they are created

### 3. Capture raw requirement intake

Creation writes the developer's raw ask into `requirement_change_candidates.md`.

This is preservation-first capture.

It should keep the initial ask intact enough that later phases can distinguish:

1. what the developer originally asked for
2. what Research later normalized
3. what Design later approved

Creation does not normalize those items.

### 4. Capture raw architecture intake

Creation writes raw architectural intake into `architecture_open_questions.md`.

This may include:

1. explicit developer-preferred technical direction
2. suspected hotspots
3. known technical risks
4. rough ownership ideas

Creation does not turn that intake into canonical architecture.

### 5. Refresh onboarding

Creation refreshes relevant onboarding before Research begins.

The purpose is not to do deep research. The purpose is to make sure Research starts from a minimally trustworthy onboarding baseline.

If onboarding is stale or missing for relevant surfaces, Creation uses the onboarding refresh path to repair it before Research.

### 6. Confirm Research readiness

Creation ends when:

1. the task folder exists
2. the root artifacts exist
3. raw requirement intake has been captured
4. raw architecture intake has been captured
5. onboarding is refreshed enough for Research to begin

At that point the workflow may enter Research.

---

## Non-Goals

Creation does not:

1. normalize requirements
2. approve requirements
3. approve architecture
4. produce input documentation
5. produce output documentation
6. write the implementation plan

---

## Exit Rule

Creation ends only when the workflow is ready for `R-01-requirements-normalization`.

If raw intake is missing or onboarding is still too stale for Research to proceed safely, Creation is not complete.
