# Design Phase

## Purpose

This document captures what the Design phase is, why it exists as its own phase, how it relates to Synthesis and Planning, and how its internal subphases work.

It is the canonical companion to `overview.md`, `requirements.md`, and `architecture.md` for everything specific to Design.

---

## Core Identity

Design is the phase where framed uncertainty is turned into an approved contract, a concrete technical direction, and a projected target-state package that can be stress-tested before Planning begins.

It is the only phase where:

1. approved requirements are promoted into `requirements.md`
2. canonical architecture IDs are first assigned
3. the chosen direction is projected into target-state output documentation
4. the projected design package is explicitly stress-tested before architecture is promoted into `architecture.md`

---

## Internal Structure of the Design Phase

Design has four ordered subphases:

1. `D-01-requirement-clarification`
2. `D-02-architecture-deliberation`
3. `D-03-output-dry-run-planning`
4. `D-04-output-documentation`

After those subphases, Design runs an explicit checkpoint review through `P-99-review/cp3-design.md` before architecture promotion is finalized.

### D-01 Requirement Clarification

`D-01-requirement-clarification` works through the requirement-facing questions with the developer before architecture is finalized.

It draws from:

1. `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`
2. `requirement_change_candidates.md`
3. existing `requirements.md`
4. Research input documentation

It writes:

1. `P-03-design/D-01-requirement-clarification/requirement-clarification.md`
2. promotion-ready requirement decisions for the orchestrator to write into `requirements.md`

The agent or orchestrator must ask the requirement-facing questions one by one, wait for the developer's answer, and record that answer.

### D-02 Architecture Deliberation

`D-02-architecture-deliberation` works through the architecture-facing questions with the developer after requirements are clarified.

It draws from:

1. approved `requirements.md`
2. `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`
3. `architecture_open_questions.md`
4. Research input documentation
5. `D-01` output

It writes:

1. `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md`
2. normalized architecture items in `architecture_open_questions.md` with assigned architecture IDs

The agent or orchestrator must ask the architecture-facing questions one by one, include tradeoffs where useful, wait for the developer's answer, and record that answer.

### D-03 Output Dry Run Planning

`D-03-output-dry-run-planning` bridges architecture deliberation into output documentation.

It carries the corresponding onboarding references for every in-scope input file into the `D-04` handoff and writes `P-03-design/D-03-output-dry-run-planning/output-dry-run-planning.md`.

### D-04 Output Documentation

`D-04-output-documentation` writes the task-local target-state projection package for the approved design direction.

It writes:

1. per-file target-state output documentation under `P-03-design/D-04-output-documentation/`
2. `P-03-design/D-04-output-documentation/overview.md`

These outputs are the explicit CP3 stress-test surface. They are not implicitly approved architecture.

---

## Why the Review Step Is Inside Design

After `D-04-output-documentation` finishes, Design explicitly initializes `P-99-review/cp3-design.md`.

That checkpoint reviews:

1. `D-01` clarification output
2. `D-02` deliberation output
3. `D-03` dry-run planning output
4. `D-04` output documentation
5. the current contract and staging state needed to judge the projected package

Only after that cycle passes can the orchestrator promote approved architecture items into `architecture.md`.

---

## Relationship to Root Artifacts

Design works through several root artifacts, but it does not collapse them together.

1. `requirements.md` is updated only after `D-01` records developer answers and the orchestrator performs requirement promotion.
2. `architecture_open_questions.md` remains the architecture staging queue.
3. `architecture.md` is updated only after `D-03`, `D-04`, `cp3-design.md`, and human review complete successfully.
4. `P-03-design/D-04-output-documentation/` is the projected target-state layer, not a substitute for the approved contracts.

---

## Correction Loops

If review feedback remains purely technical, return to `D-02-architecture-deliberation`, then re-run `D-03`, `D-04`, and `cp3-design`.

If review feedback shows the requirements were not properly understood, record requirement items in `requirement_change_candidates.md`, record architecture items in `architecture_open_questions.md`, and return to Research.