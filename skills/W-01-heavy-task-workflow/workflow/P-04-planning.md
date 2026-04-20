# Planning Phase

## Purpose

This document defines how the heavy-task workflow turns approved requirements, approved architecture, target-state outputs, and current code reality into an implementation plan.

It is the Planning-phase companion to `overview.md`, `requirements.md`, and `architecture.md`.

Planning is intentionally downstream of approval. It does not settle contract uncertainty or architecture uncertainty. It sequences work that has already been approved.

---

## Core Identity

Planning is the phase that converts approved target-state into execution order.

The model is:

1. `requirements.md` is the approved requirements contract and is not reinterpreted here
2. `architecture.md` is the approved architecture contract and is not redesigned here
3. `P-03-design/D-04-output-documentation/` is the approved target-state implementation map that Planning schedules against
4. `P-04-planning/P-01-implementation-planning/implementation_plan.md` is the phase artifact that orders the work into dependency-ordered implementation phases and checklist steps
5. `P-99-review/cp4-planning.md` is the review checkpoint that verifies the plan is complete, scheduling-only, and aligned with the approved target-state

Planning answers one question: how do we implement the approved target-state safely, in order, with clear verification gates?

---

## Key Principles

### Planning starts from approved contracts, not from proposals

By the time Planning begins:

1. approved requirements must already be in `requirements.md`
2. approved architecture must already be in `architecture.md`
3. projected target-state outputs must already exist under `P-03-design/D-04-output-documentation/`

### Planning is scheduling-only

Planning may:

1. group work into implementation phases
2. order checklist steps by dependency
3. define verification notes for each step or phase
4. note implementation risks that affect sequencing

Planning may not:

1. introduce new requirements
2. change approved architecture without re-entry
3. rewrite the target-state output package as a substitute for Design
4. hide design decisions inside sequencing notes

### Traceability must survive into the plan

Each implementation phase and checklist step must remain explainable in terms of:

1. which approved requirement IDs it advances
2. which approved architecture IDs it instantiates
3. which output documentation files it is scheduling

---

## Inputs and Preconditions

Planning begins only when the following inputs exist and are internally consistent:

1. `requirements.md`
2. `architecture.md`
3. `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md`
4. `P-03-design/D-04-output-documentation/overview.md`
5. the relevant per-file output documentation under `P-03-design/D-04-output-documentation/`
6. the corresponding current-state input documentation under `P-01-research/R-02-input-documentation/`
7. `cp3-design.md`
8. the relevant code that the plan must schedule against

---

## Planning Responsibilities

Planning is responsible for producing a plan that is complete enough to drive implementation without reopening design.

That responsibility includes:

1. identifying the executable work slices implied by the output documentation layer
2. determining dependency order across those slices
3. grouping slices into implementation phases that are readable and coherent
4. defining checklist steps inside those phases
5. defining a verification note for each step or phase
6. making explicit where current code shape increases risk or forces sequence constraints
7. ensuring every in-scope output documentation file appears somewhere in the plan

---

## Relationship To CP4

`P-99-review/cp4-planning.md` reviews the plan as a plan.

It rejects design or contract content that leaks into Planning and checks that every in-scope output documentation file is scheduled.