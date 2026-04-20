# Synthesis Phase

## Purpose

This document captures what the Synthesis phase is, why it exists as its own phase, how it relates to Research and Design, and how its internal subphases work.

It is the canonical companion to `overview.md` and `requirements.md` for everything specific to Phase 2 Synthesis.

---

## Core Identity

Synthesis is its own distinct phase between Research and Design.

Its role is different from both:

1. Research discovers facts, contradictions, blind spots, and unresolved questions.
2. Synthesis consolidates, classifies, deduplicates, orders, and frames those questions.
3. Design later works through those framed questions with the developer rather than letting the agent answer them on its own.

The reason Synthesis deserves its own phase is that Research produces a large amount of unresolved material at once. That material needs cleanup before Design can use it safely.

---

## Requirement Questions vs Architecture Questions

The two framing subphases work on two fundamentally different kinds of question.

1. Requirement questions ask **what should be true by the end of the task**. Scope, preserved behavior, constraints, success checks, non-goals. They shape the contract that lives in `requirements.md`.
2. Architecture questions ask **how we do it technically**. Backend vs frontend ownership, layering, module boundaries, API and contract shape, data model, sequencing, integration strategy. They shape the implementation direction once the contract is settled.
3. Evidence questions ask **what is actually the case in the current system**. They split by lane: requirement-side evidence stays attached to requirement candidates and Research input documentation, while technical evidence gaps live in `architecture_open_questions.md`.

Examples:

1. "Must preserve the current payload shape?" is a requirement question.
2. "Should validation live in the backend service or in the frontend form?" is an architecture question.
3. "Which layer currently owns translation?" is an evidence question.

This distinction is the reason Synthesis is split into two subphases. S-01 frames the **what should be true by the end of the task** questions. S-02 frames the **how we do it technically** questions in architectural form. Mixing them produces premature architectural decisions on top of unsettled contract assumptions.

---

## Why Synthesis Exists

Research is intentionally broad and evidence-heavy.

It reads:

1. onboarding
2. code
3. canonical docs
4. boundaries and seams
5. current-state slices

That breadth is useful, but it also creates noise.

By the end of Research, the workflow often has:

1. requirement questions
2. architecture questions
3. evidence gaps
4. duplicate questions phrased differently
5. questions that matter now and questions that can be deferred

If all of that is handed directly into Design, Design gets overloaded and begins doing cleanup work that should have been done earlier.

Synthesis exists to prevent that. Its job is to turn the research mess into a decision-ready map.

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

Synthesis is a true transition phase between Research and Design. It runs after the Research checkpoint (`P-99-review/cp1-research.md`) and before the Synthesis checkpoint (`P-99-review/cp2-synthesis.md`).

Its work is distinct:

1. Research gathers and exposes unresolved material.
2. Synthesis organizes and frames that material.
3. Design consumes only the framed architecture-facing subset.

---

## What Synthesis Is Not

Synthesis is not:

1. just one more research output
2. a hidden extension of architecture deliberation
3. a place where architectural decisions are made prematurely
4. the global backlog for all future unresolved items
5. a place where requirements are approved or promoted

Synthesis frames questions. It does not silently answer them and it does not promote requirements. Promotion happens only at `D-01-requirement-clarification`.

---

## Internal Structure of the Synthesis Phase

Synthesis has two ordered subphases:

1. `S-01-requirement-question-framing`
2. `S-02-architecture-question-framing`

This split keeps a clean identity for Synthesis while preserving the dependency between requirement-level uncertainty and design-level uncertainty.

### Why this split works

Requirement questions usually feed architecture questions.

If the contract-level ambiguity has not been framed properly, then the architectural questions will often be distorted, premature, or simply wrong.

So the correct order is:

1. first frame requirement questions
2. then frame architecture questions

This is treated as a rule, not a soft preference.

---

## S-01 Requirement Question Framing

### Purpose

This subphase takes the contract-affecting questions surfaced by Research ("what should be true by the end of the task") and turns them into a clean, prioritized, reviewable set.

It does not perform architecture deliberation. It does not answer **how we do it technically**. It does not promote requirements.

It frames the questions that must be addressed at `D-01-requirement-clarification` before Design can safely harden architectural direction.

### Typical question categories

1. scope ambiguity
2. preserved behavior ambiguity
3. constraints that may be real, partial, or outdated
4. success criteria that are too vague or incomplete
5. assumptions that are still implicit
6. contradictions between developer intent and discovered current-state evidence

### Inputs

S-01 draws from:

1. `requirements.md` (the approved contract, for context only)
2. `requirement_change_candidates.md` (raw-intake, normalized, evidence-pending entries)
3. `P-01-research/R-02-input-documentation/overview.md`
4. `P-01-research/R-02-input-documentation/<path/code_file_name>.md` files

There is no separate research summary artifact. The Research handoff is `overview.md` plus the companion input documentation files.

### Output

S-01 writes one artifact: `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`.

It contains:

1. a cleaned and ordered set of what-should-be-true requirement questions
2. an explicit distinction between contract-affecting questions and non-contract-affecting questions
3. the elicitation targets that `D-01-requirement-clarification` will work through
4. a narrowed base for `S-02-architecture-question-framing`

S-01 does not promote anything into `requirements.md`. Promotion happens only at `D-01-requirement-clarification`.

### Exit condition

The contract-level uncertainty is framed well enough that the workflow can ask the right architecture questions next. This is not yet full resolution; it is the point where the requirement-facing question space is cleaned up enough to drive S-02 coherently.

---

## S-02 Architecture Question Framing

### Purpose

This subphase takes the post-S-01 state and frames the architecture-facing questions ("how we do it technically") that architecture deliberation will work through.

It operates on the cleaned question space from S-01, not on raw research noise. It assumes the **what should be true by the end of the task** questions have already been framed.

### Typical question categories

1. backend vs frontend ownership
2. layering and module boundaries
3. API and contract shape
4. data model
5. abstraction and interface shape
6. sequencing dependencies
7. integration strategy
8. migration strategy where the target direction is still undecided

### Inputs

S-02 draws from:

1. `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`
2. `architecture_open_questions.md`
3. `requirements.md` for the already-approved contract context
4. `P-01-research/R-02-input-documentation/overview.md` and the companion input documentation files

### Output

S-02 writes one artifact: `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`.

It contains:

1. a deduplicated set of architectural questions
2. dependency order between those questions
3. clear separation of architectural decisions versus evidence gaps
4. the architecture briefing that `D-02-architecture-deliberation` consumes

### Exit condition

Design receives a curated, dependency-ordered set of actual architectural questions rather than a raw pile of unresolved research material.

---

## Synthesis Handoff

The two framing files together are the synthesis handoff into Design:

1. `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md` feeds `D-01-requirement-clarification`.
2. `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md` feeds `D-02-architecture-deliberation`.

There is no `synthesis.md` artifact. Any reference to `design/synthesis.md` is drift.

---

## Relationship to the Global Backlog

Synthesis does not replace the always-live backlog `architecture_open_questions.md`.

The separation is:

1. `architecture_open_questions.md` is the living cross-phase architecture backlog of unresolved architectural questions and technical evidence gaps.
2. Requirement-side uncertainty does not live there. It remains attached to `requirement_change_candidates.md` and Research input documentation until S-01 frames it.
3. The Synthesis subphases are the cleanup and framing machinery.
4. `requirement-question-framing.md` is the requirement-side handoff into D-01.
5. `architecture-question-framing.md` is the architectural-side handoff into D-02.

The architecture backlog feeds `architecture-question-framing.md`. It does not replace it, and it continues to live across phases.

---

## Relationship to the Synthesis Checkpoint

The Synthesis checkpoint is `P-99-review/cp2-synthesis.md`.

It is review-only. It does not generate any new working artifact.

`R-01-adversarial-review` reviews the two framing files plus updated states of `requirement_change_candidates.md`, `requirements.md`, and `architecture_open_questions.md`, followed by human review. The disposition is recorded in `cp2-synthesis.md`.

There is no separate "checkpoint snapshot" artifact for Synthesis beyond the framing files themselves.

---

## Relationship to Research

Research is where unresolved questions first appear.

Synthesis does not pretend that questions are born in Synthesis.

1. Research discovers and records requirement-side uncertainty through `requirement_change_candidates.md` plus `R-02-input-documentation`.
2. Research discovers and records architectural uncertainty through `architecture_open_questions.md` plus `R-02-input-documentation`.
3. Synthesis classifies, deduplicates, orders, and frames them into the two framing files.

So:

- questions emerge in Research
- questions are formalized for Design in Synthesis

---

## Relationship to Design

Design does not begin from raw research noise.

Design begins from the two framing files.

1. `D-01-requirement-clarification` consumes `requirement-question-framing.md`, presents the requirement-facing questions to the developer one by one with explicit options where helpful, records the developer's answers, promotes approved candidates from `requirement_change_candidates.md` into `requirements.md`, removes rejected candidates, and leaves evidence-pending candidates staged for later batched Research re-entry.
2. `D-02-architecture-deliberation` consumes `architecture-question-framing.md`, presents the framed architectural questions to the developer one by one with explicit options and tradeoffs where helpful, and works against the now-updated `requirements.md` using the developer's answers.

This makes Design more focused and reduces the chance that Design quietly redefines requirement-level uncertainty while trying to solve technical problems.

---

## Interaction with Requirement Elicitation

Requirement elicitation is conceptually distinct from architecture deliberation.

The split is:

1. S-01 frames the requirement-facing what-should-be-true questions that `D-01-requirement-clarification` must present to the developer and record answers for.
2. S-02 frames the architecture-facing technical how-do-we-do-it questions that `D-02-architecture-deliberation` must present to the developer, using the still-cleanest available requirement landscape.
3. Promotion into `requirements.md` happens only at `D-01-requirement-clarification`. Synthesis never promotes.

So even though some requirement-question handling lives operationally inside Synthesis, the logic remains:

- requirement-facing what-should-be-true uncertainty first
- architecture-facing technical how-do-we-do-it uncertainty second
- promotion only at D-01

---

## Corrective Re-entry Later in the Workflow

Later phases may surface new unresolved items, but they usually appear one by one rather than in the large burst produced by Research.

The workflow handles them like this:

1. record contract-affecting findings into `requirement_change_candidates.md`
2. record architectural questions into `architecture_open_questions.md`
3. decide whether the issue belongs to the requirement pipeline or the architectural pipeline
4. re-enter the relevant Synthesis logic only when the question is large enough or important enough to require reframing

Practical rule:

1. if later work exposes a contract problem, re-enter S-01 logic and then `D-01-requirement-clarification` per the Re-entry Rule
2. if later work exposes an architecture problem, re-enter S-02 logic and then `D-02-architecture-deliberation`
3. if it is a minor unresolved item, keep it in the backlog until the next opportune Research re-entry consumes it

---

## Artifact Recommendations

Synthesis-related artifacts:

1. `architecture_open_questions.md` at task root
   - living cross-phase architecture backlog
2. `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`
   - requirement-facing framed question set
3. `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`
   - architecture-facing framed question set
4. `P-02-synthesis/progress.md`
   - per-phase progress tracker maintained by the orchestrator
5. `P-99-review/cp2-synthesis.md`
   - checkpoint review record (review-only)

There is no `synthesis.md` artifact. Synthesis is not the permanent storage location for unresolved items; it is the place where those items are cleaned up and prepared for Design.

---

## Recommended Summary

Synthesis remains its own phase because it performs a distinct kind of work:

1. Research creates the unresolved mess.
2. Synthesis cleans and frames that mess into two framing files.
3. Design works the framed questions, with `D-01-requirement-clarification` first resolving what should be true by the end of the task, and `D-02-architecture-deliberation` second resolving how to do it technically.

Inside Synthesis, two ordered subphases run:

1. `S-01-requirement-question-framing` writes `requirement-question-framing.md`
2. `S-02-architecture-question-framing` writes `architecture-question-framing.md`

Synthesis does not promote requirements. It does not produce a `synthesis.md` artifact. It does not replace the `architecture_open_questions.md` backlog, which is architecture-only. Requirement-side uncertainty remains in the requirement pipeline. The Synthesis checkpoint is review-only and lives in `P-99-review/cp2-synthesis.md`.

That is the canonical model for Phase 2 Synthesis in the heavy-task workflow.
