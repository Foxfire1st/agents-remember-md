# Design Phase

## Purpose

This document captures what the Design phase is, why it exists as its own phase, how it relates to Synthesis and Planning, and how its internal subphases work.

It is the canonical companion to `overview.md`, `requirements.md`, and `architecture.md` for everything specific to Phase 3 Design.

---

## Core Identity

Design is the phase where framed uncertainty is turned into an approved contract, a concrete technical direction, and a projected target-state package that can be stress-tested before Planning begins.

Its role is different from both neighboring phases:

1. Synthesis frames the requirement-facing and architecture-facing questions.
2. Design works through those framed questions with the developer, records decisions, and projects the chosen direction.
3. Planning later turns the approved target-state into execution order.

Design therefore sits between question framing and implementation scheduling.

It is the only phase where:

1. approved requirements are promoted into `requirements.md`
2. canonical architecture IDs are first assigned
3. the chosen direction is projected into target-state output documentation
4. the projected design package is explicitly stress-tested before architecture is promoted into `architecture.md`

---

## Why Design Exists

Synthesis does not answer the framed questions.

Planning does not settle disputed contract or architecture uncertainty.

That gap is why Design exists.

Design creates a controlled sequence for:

1. clarifying what must be true by the end of the task
2. deciding how that truth will be carried technically
3. projecting that decision into a concrete target-state package
4. stress-testing the projected package before it becomes the approved architecture contract

Without Design, the workflow would either let the agent answer those questions prematurely or would force Planning to schedule against unresolved design work.

---

## Phase Placement

The phase flow is:

1. Creation
2. Research
3. Synthesis
4. Design
5. Planning
6. Implementation
7. Closing

Design begins only after the Synthesis checkpoint `P-99-review/cp2-synthesis.md` and ends only after the approved architectural items have been promoted into `architecture.md`.

This means Design is not finished when output documentation exists.

It is finished only after the projected package survives explicit review and the orchestrator closes the architecture promotion step.

---

## What Design Is Not

Design is not:

1. a place where the agent auto-answers requirement or architecture questions
2. a hidden extension of Synthesis framing
3. a place where architecture is implicitly approved just because projection exists
4. a substitute for Planning or implementation sequencing
5. a place where approved contracts can be bypassed or rewritten casually

Design resolves and projects. It does not implement, and it does not skip explicit review.

---

## Internal Structure of the Design Phase

Design has four ordered subphases:

1. `D-01-requirement-clarification`
2. `D-02-architecture-deliberation`
3. `D-03-output-dry-run-planning`
4. `D-04-output-documentation`

After those subphases, Design runs an explicit checkpoint review through `P-99-review/cp3-design.md` before architecture promotion is finalized.

This order is mandatory.

### Why this order matters

Requirement clarification must happen before architecture deliberation because architecture should not harden against an unstable contract.

Architecture deliberation must happen before output projection because projection needs a chosen direction and canonical architecture IDs.

Output projection must happen before architecture approval because the workflow requires an explicit stress-test surface before promotion into `architecture.md`.

---

## D-01 Requirement Clarification

### Purpose

`D-01-requirement-clarification` works through the requirement-facing questions with the developer before architecture is finalized.

This is where the workflow turns the Synthesis requirement question set into explicit developer-owned contract decisions.

### Inputs

`D-01` draws from:

1. `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`
2. `requirement_change_candidates.md`
3. existing `requirements.md`
4. Research input documentation

### Output

`D-01` writes:

1. `P-03-design/D-01-requirement-clarification/requirement-clarification.md`
2. promotion-ready requirement decisions for the orchestrator to write into `requirements.md`

### Rule

The agent or orchestrator must ask the requirement-facing questions to the developer one by one, include explicit options where helpful, wait for the developer's answer, and then record that answer.

`D-01` does not answer those questions autonomously.

Approved candidates are promoted into `requirements.md`, rejected candidates are recorded and removed from staging, and evidence-pending candidates remain in `requirement_change_candidates.md` for later batched Research re-entry.

---

## D-02 Architecture Deliberation

### Purpose

`D-02-architecture-deliberation` works through the architecture-facing questions with the developer after requirements are clarified.

This is the first point where canonical architecture IDs are assigned and where architecture items become normalized.

### Inputs

`D-02` draws from:

1. approved `requirements.md`
2. `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`
3. `architecture_open_questions.md`
4. Research input documentation
5. `D-01` requirement clarification output

### Output

`D-02` writes:

1. `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md`
2. normalized architecture items in `architecture_open_questions.md` with assigned architecture IDs

### Rule

The agent or orchestrator must ask the architecture-facing questions to the developer one by one, include explicit options and tradeoffs where helpful, wait for the developer's answer, and then record that answer.

`D-02` does not approve architecture on its own.

It chooses and normalizes direction, but approval happens only after projection and review.

---

## D-03 Output Dry Run Planning

### Purpose

`D-03-output-dry-run-planning` bridges architecture deliberation into output documentation.

It defines how the chosen design direction will be projected into a target-state documentation package before that package is written.

### Inputs

`D-03` draws from:

1. `D-02` architecture deliberation
2. `D-01` requirement clarification
3. approved `requirements.md`
4. relevant Research inputs when target-state coverage must be mapped back to current-state surfaces
5. the corresponding onboarding references for every in-scope input documentation file

### Output

`D-03` writes:

1. `P-03-design/D-03-output-dry-run-planning/output-dry-run-planning.md`

### Rule

`D-03` must carry the corresponding onboarding references for every in-scope input file into the `D-04` handoff.

The dry-run plan is not the implementation plan. It prepares the projection pass and review surface only.

---

## D-04 Output Documentation

### Purpose

`D-04-output-documentation` writes the task-local target-state projection package for the approved design direction.

Those outputs are the explicit surface used for design stress testing and later for Planning.

### Inputs

`D-04` draws from:

1. `D-03` output dry-run plan
2. `D-02` architecture deliberation
3. `D-01` requirement clarification
4. approved `requirements.md`
5. relevant input documentation from Research
6. the corresponding onboarding references for every in-scope input documentation file

### Outputs

`D-04` writes:

1. per-file target-state output documentation under `P-03-design/D-04-output-documentation/`
2. `P-03-design/D-04-output-documentation/overview.md`

### Rule

Target-state projection uses the task-local input documentation together with the corresponding onboarding references for every mapped input file.

The outputs carry approved requirement IDs and architecture IDs and become the explicit CP3 stress-test surface.

These outputs are not implicitly approved architecture.

---

## Why the Review Step Is Inside Design

Design includes an explicit checkpoint review because projection alone is not sufficient proof that the chosen architecture is safe to approve.

After `D-04-output-documentation` finishes, Design explicitly initializes `P-99-review/cp3-design.md`.

That checkpoint reviews:

1. `D-01` clarification output
2. `D-02` deliberation output
3. `D-03` dry-run planning output
4. `D-04` output documentation
5. the current contract and staging state needed to judge the projected package

The stress-test cycle has three parts:

1. adversarial review of the projected package
2. surfacing the most important projected hotspots and intent in chat
3. human developer feedback on that projected direction

Only after that cycle passes can the orchestrator promote approved architecture items into `architecture.md`.

---

## Relationship to Root Artifacts

Design works through several root artifacts, but it does not collapse them together.

### `requirements.md`

`requirements.md` is updated only after `D-01-requirement-clarification` records developer answers and the orchestrator performs requirement promotion.

### `architecture_open_questions.md`

`architecture_open_questions.md` remains the architecture staging queue.

At `D-02`, the orchestrator moves resolved items from raw-intake into the normalized section with architecture IDs. After approved promotion or rejection, those items are removed from the file.

### `architecture.md`

`architecture.md` is not updated during `D-02` just because a direction was chosen.

It is updated only after `D-03`, `D-04`, `cp3-design.md`, and human review have completed successfully.

### Output documentation

`P-03-design/D-04-output-documentation/` is the projected target-state layer.

It is a Design artifact family, not a substitute for the approved contracts in `requirements.md` or `architecture.md`.

---

## Correction Loops Inside and After Design

Design does not always fail in the same way, so the correction loop depends on failure type.

### Technical-only failure

If review feedback remains purely technical:

1. record the issue in `architecture_open_questions.md` raw-intake
2. jump back to `D-02-architecture-deliberation`
3. re-run `D-03-output-dry-run-planning`
4. re-run `D-04-output-documentation`
5. re-run the explicit `cp3-design.md` stress-test cycle

### Requirement-understanding failure

If review feedback shows the requirements were not properly understood:

1. record requirement items in `requirement_change_candidates.md`
2. record architecture items in `architecture_open_questions.md`
3. jump back to Research

### Evidence-pending requirement work

If `D-01` cannot promote a requirement candidate yet because more evidence is needed, that item remains staged in `requirement_change_candidates.md` for later batched Research re-entry.

---

## Exit Condition

Design is complete only when all of the following are true:

1. approved requirement items have been promoted into `requirements.md`
2. `D-02-architecture-deliberation` has recorded the chosen technical direction and assigned canonical architecture IDs
3. `D-03-output-dry-run-planning` and `D-04-output-documentation` have created the projected design package using the task-local inputs plus the corresponding onboarding references for every in-scope input file
4. `P-99-review/cp3-design.md` exists and the explicit design review cycle has passed
5. approved architectural items have been promoted into `architecture.md`
6. the promoted or rejected items have been removed from `architecture_open_questions.md`

Only then is the workflow ready for Planning.
