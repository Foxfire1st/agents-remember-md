# Requirements Gathering and Management

## Purpose

This document captures how requirements are gathered, normalized, questioned, and promoted through the heavy-task workflow.

It is the canonical companion to `overview.md`. Where `overview.md` defines the artifacts, locations, and phase structure, this document defines the lifecycle that flows requirements through those artifacts.

---

## Core Identity

The workflow treats raw intake, staged candidates, and the approved contract as distinct artifacts.

The model is:

1. `task.md` is the global task tracker. It records objective, developer notes, phase progress, execution order, and links to phase artifacts. It is not the requirements contract.
2. `requirement_change_candidates.md` is the orchestrator-owned staging artifact. It accepts raw intake at any phase and holds raw-intake entries, normalized records, and evidence-pending candidates until the orchestrator promotes them into `requirements.md` or removes them after rejection.
3. `requirements.md` is the finalized and approved requirements contract. It is written solely by the orchestrator. Items land here only after `D-01-requirement-clarification` approves them and the orchestrator performs the move. Changes to already approved requirements do not happen directly in `requirements.md`; they re-enter `requirement_change_candidates.md` and move through the same staging cycle as a new requirement would.
4. `architecture_open_questions.md` is the orchestrator-owned architecture staging artifact for developer-directed target outcomes, proposed technical directions, unresolved architectural questions, technical evidence gaps, and later normalized architecture items. It does not hold requirement questions. Concrete contract-change proposals belong in `requirement_change_candidates.md`, not here. Canonical architecture IDs are not assigned in this queue until `D-02-architecture-deliberation`, where the orchestrator moves resolved items from raw-intake to normalized.
5. The synthesis subphase artifacts (`requirement-question-framing.md`, `architecture-question-framing.md`) are phase artifacts that feed Design. They never replace the open-questions backlog. `architecture-question-framing.md` still frames questions rather than assigning architecture IDs.

This preserves the architectural separation between global tracking, staged candidates, the approved contract, current-state truth, target-state truth, and the open-questions backlog.

---

## Key Principles

### Separation of global tracking, staging, and the approved contract

`task.md` tracks the task globally across phases. It does not hold approved requirements.

`requirement_change_candidates.md` is the staging artifact for all requirement work. Raw intake, normalization, evidence pending, and disposition all happen here.

`requirements.md` holds only finalized and approved requirements. It is updated only through orchestrator-owned promotion at `D-01-requirement-clarification`.

The orchestrator is a workflow role in direct discussion with the developer. That role may be fulfilled by a dedicated orchestrator agent or by the developer-facing agent that accepted and initiated the workflow. It is the sole writer of `task.md`, `requirement_change_candidates.md`, `requirements.md`, `architecture_open_questions.md`, `architecture.md`, and each phase `progress.md`. Skill-owned artifacts may be written by their owning skills, but they do not directly author these orchestrator-owned runtime documents.

That rule applies to changes as well as new items. If an already approved requirement needs to change, the proposed change must re-enter `requirement_change_candidates.md` and move through raw-intake, normalization, questioning, and clarification again before the orchestrator updates `requirements.md`.

This separation means there is one durable place to compare:

1. what the developer originally asked for (raw intake in `requirement_change_candidates.md`)
2. what the workflow later concluded that request means (normalized entries in `requirement_change_candidates.md`)
3. what was finally approved (entries promoted into `requirements.md`)

The move semantics are destructive and orchestrator-owned:

1. raw-intake entries live in `requirement_change_candidates.md`
2. the orchestrator moves them into the normalized list during `R-01-requirements-normalization`
3. the orchestrator promotes approved items into `requirements.md` at `D-01-requirement-clarification` and removes them from staging
4. the orchestrator removes rejected items from staging after their disposition is recorded

`requirements.md` is therefore never edited as a shortcut around the staging flow.

### Separation of requirement changes and architectural open questions

The workflow uses two different cross-phase queues:

1. `requirement_change_candidates.md` for proposed contract changes
2. `architecture_open_questions.md` for architecture items still in staging: raw intake, unresolved architectural questions, technical evidence gaps, and normalized architecture entries awaiting final disposition

These are not collapsed into one artifact.

#### Requirement questions vs architecture questions

The distinction is:

1. Requirement questions ask **what should be true by the end of the task**. Scope, preserved behavior, constraints, success checks, non-goals. They shape the contract.
2. Architecture questions ask **how we do it technically**. Backend vs frontend ownership, layering, module boundaries, API and contract shape, data model, sequencing, integration strategy. They shape the implementation direction once the contract is settled.
3. Evidence questions ask **what is actually the case in the current system**. Requirement-side evidence stays attached to requirement candidates and Research input documentation. Technical evidence gaps live in `architecture_open_questions.md`.

Examples:

1. "Must preserve the current payload shape?" is a requirement question (constraint, preserved behavior).
2. "Should validation live in the backend service or in the frontend form?" is an architecture question (ownership, layering).
3. "Which layer currently owns translation?" is a technical evidence question.
4. "Do we actually still emit this event in production?" is a requirement-side evidence question if it changes expected behavior.

All three matter, but they do not belong in the same queue and they are not resolved by the same step. Requirement questions stay in the requirement pipeline: they remain attached to `requirement_change_candidates.md`, are framed in `S-01-requirement-question-framing`, and are resolved at `D-01-requirement-clarification`. Architecture questions stay in the architectural pipeline: they live in `architecture_open_questions.md`, are framed in `S-02-architecture-question-framing`, and are resolved at `D-02-architecture-deliberation`. Evidence questions resolve through Research re-entry, but requirement-side evidence attaches to the requirement pipeline while technical evidence attaches to `architecture_open_questions.md`.

### Synthesis as a pair of framing artifacts

Synthesis produces two framing artifacts:

1. `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`
2. `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`

Together they are the synthesis handoff into Design.

`requirement-question-framing.md` belongs to the requirement pipeline and is built from requirement candidates plus Research input documentation.

`architecture-question-framing.md` belongs to the architectural pipeline and is built from `architecture_open_questions.md` plus Research input documentation.

Only the architecture framing file relates to `architecture_open_questions.md`. Requirement-side uncertainty does not route through that backlog.

### Separation of input and output documentation

Input and output documentation are not treated as if one simply merges into the other.

1. `P-01-research/R-02-input-documentation/` freezes current-state truth.
2. `P-03-design/D-04-output-documentation/` freezes target-state projection.

If a later discovery shows the current-state understanding was wrong, the input layer is repaired first.

If the current-state understanding was right but the intended target changed, the output layer is repaired.

Each layer maps relevant code 1-to-1 on a per file basis, plus one scope-level `overview.md`.

The ID rule across those layers is:

1. input documentation tracks normalized requirement IDs
2. output documentation tracks approved requirement IDs and architecture IDs
3. the output `overview.md` aggregates those IDs into traceability tables with one or more file links showing where each ID appears

That traceability is not just for evidence. Planning later depends on it.

---

## Artifact Model

### Root task level

1. `task.md`
   Purpose: global task tracker written solely by the orchestrator.
   Contents: objective, developer notes, global phase tracker, execution order, links to phase artifacts, implementation status summary.

2. `requirement_change_candidates.md`
   Purpose: orchestrator-owned staging artifact for all requirement work.
   Contents: raw-intake list, normalized list, evidence-pending entries, evidence links, status. The orchestrator moves items through this file and removes them when they are promoted into `requirements.md` or recorded as rejected in `requirement-clarification.md`.

3. `requirements.md`
   Purpose: finalized and approved requirements contract written solely by the orchestrator.
   Contents: only items promoted from `requirement_change_candidates.md` after approval at `D-01-requirement-clarification`, preserving their requirement IDs. Changes to already approved requirement entries must also arrive here by orchestrator-owned promotion from staging rather than by direct edit.

4. `architecture.md`
   Purpose: finalized and approved architecture contract written solely by the orchestrator.
   Contents: only architecture items promoted after post-output review approval, preserving the architecture IDs assigned at `D-02-architecture-deliberation`.

5. `architecture_open_questions.md`
   Purpose: orchestrator-owned staging artifact for architecture promotion and rejection.
   Contents: raw-intake architecture entries, developer-directed target outcomes, proposed technical directions, architectural questions about how to do it, technical evidence gaps, normalized architecture entries with architecture IDs, statuses, and owners. The orchestrator moves items through this file and removes them when they are promoted into `architecture.md` or rejected. It does not hold requirement questions or concrete contract-change proposals, and it is not the final architecture contract.

6. `final_report.md`
   Purpose: closeout summary of what was learned, decided, and built. Written at Closing only.

### Phase artifacts

1. `P-XX-<phase>/progress.md`
   Per-phase progress tracker written and maintained solely by the orchestrator.

2. `P-01-research/R-02-input-documentation/<path/code_file_name>.md` and `overview.md`
   Current-state input documentation with requirement annotations and normalized requirement IDs. Files map 1-to-1 with relevant code. The `overview.md` covers scope-level context and the normalized requirement ID view. This is the Research handoff into Synthesis and later phases. There is no separate research summary artifact.

3. `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`
   Cleaned, ordered what-should-be-true requirement questions and contract-affecting tensions.

4. `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`
   Architectural questions, dependency order, decision gaps, evidence gaps, briefing for Design. It does not yet assign canonical architecture IDs.

5. `P-03-design/D-01-requirement-clarification/requirement-clarification.md`
   Resolved clarifications, promotion-ready updates, rejection rationales, and pending-evidence items queued for later Research re-entry.

6. `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md`
   Architectural options, tradeoffs, chosen direction, rejected directions, rationale, and the first assignment of architecture IDs to concrete architectural items. This is also where the orchestrator moves resolved architecture items from raw-intake to normalized in `architecture_open_questions.md`.

7. `P-03-design/D-03-output-dry-run-planning/output-dry-run-planning.md`
   Dry-run plan for validating intended outputs and target-state expectations, including the corresponding onboarding references for every in-scope input file.

8. `P-03-design/D-04-output-documentation/<path/code_file_name>.md` and `overview.md`
   Target-state output documentation. Files map 1-to-1 with the input documentation files. Per-file outputs carry approved requirement IDs and architecture IDs. Output generation uses the task-local input layer plus the corresponding onboarding references for every mapped input file. The output `overview.md` contains traceability tables for all approved requirement IDs and all architecture IDs, each with one or more file links.

9. `P-04-planning/P-01-implementation-planning/implementation_plan.md`
   Scheduling-only plan: dependency-ordered implementation phases and checklist steps keyed to output documentation files, requirement IDs, and architecture IDs, plus verification notes per step or phase. It consumes the requirement and architecture ID traceability from the output documentation layer and the relevant code shape. If Planning or Implementation discovers problems while using the plan, those problems are recorded in the plan's issues section and discussed before they are resolved, discarded, or turned into requirement or architecture intake. Substantive design or contract content does not belong here.

10. `P-05-implementation/I-01-implementation/implementation_results.md`
    Per-phase and per-step summary of files changed, interfaces introduced, deviations, verification results, deferred follow-ups, and issue dispositions.

11. `P-99-review/cp<N>-<phase>.md`
    Checkpoint review artifacts (cp1-research, cp2-synthesis, cp3-design, cp4-planning, cp5-implementation). Review-only. No new working artifacts are generated at a checkpoint.

---

## Requirement Lifecycle

### 1. Raw Intake

Placement: Creation, immediately after task folder creation. Also any later phase where new requirement material appears.

Target artifact: `requirement_change_candidates.md` raw-intake list.

Purpose: preserve what the developer asked for before any code analysis or agent interpretation starts shaping it.

Captured for each entry as appropriate:

1. the developer's initial ask
2. what they believe is broken or missing
3. expected outcome
4. success signal if known
5. explicit non-goals
6. known constraints
7. linked tickets, specs, docs, or examples

Rule: raw intake is evidence-light and preservation-heavy. It is never written directly into `requirements.md`.

That includes changes to requirements that were already approved earlier. Those changes must be captured as new raw-intake entries in `requirement_change_candidates.md`, not applied directly in `requirements.md`.

### 2. Onboarding Refresh and Drift Correction

Placement: Creation, after raw intake is recorded.

Purpose: detect onboarding drift and refresh relevant onboarding before Research begins. Skills `C-02-onboarding-drift-detection` and `C-05-create-or-update-onboarding-files` are used here.

Rule: onboarding refresh sharpens the requirement picture, but it does not overwrite the developer's original ask. Raw intake stays preserved in `requirement_change_candidates.md`.

Creation ends once onboarding is complete enough for Research to start.

### 3. Normalization

Placement: Research, as the first skill `R-01-requirements-normalization`. Not Creation.

Purpose: translate raw intake into stable normalized requirement records.

Source artifacts:

1. raw-intake list in `requirement_change_candidates.md`
2. onboarding
3. code at the relevant seams
4. canonical docs, tickets, prior developer discussion as needed

Output: the orchestrator adds entries to the normalized list in `requirement_change_candidates.md` and removes them from the raw-intake list once they have been normalized.

Normalization is contract-shaping, not deep area research. It draws only on what is needed to produce a stable record.

Suggested record fields:

1. requirement ID
2. scope group or functional slice
3. requirement type
4. source basis
5. affected surfaces if known
6. confidence
7. verification intent
8. status
9. promotion link into `requirements.md` once approved

Requirement type values:

1. `change`
2. `preservation`
3. `constraint`
4. `success-check`
5. `non-goal`
6. `assumption`

Note: open questions are not a candidate type. Requirement-side uncertainty stays attached to requirement candidates via status and Research evidence until it is framed in `requirement-question-framing.md`. Technical open questions live in `architecture_open_questions.md`.

Source basis values:

1. developer statement
2. ticket
3. onboarding
4. code
5. canonical doc

Status values:

1. `raw` — captured as raw intake, not yet normalized
2. `normalized` — turned into a stable record but not yet pressure-tested
3. `questioned` — pressure-tested by Research, disposition still being worked
4. `evidence-pending` — explicitly waiting for additional evidence before disposition
5. `approved` — promoted into `requirements.md` at `D-01-requirement-clarification`
6. `rejected` — disposition recorded in `requirement-clarification.md`, then removed from this file

### 4. Research Questioning

Placement: Research, as the second skill `R-02-input-documentation`.

Purpose: pressure-test normalized requirements against the real current-state slice.

Output artifacts:

1. `P-01-research/R-02-input-documentation/<path/code_file_name>.md` files annotated with requirement IDs
2. `P-01-research/R-02-input-documentation/overview.md` with scope-level normalized requirement ID references
3. updated statuses in `requirement_change_candidates.md` (e.g. `questioned`, `evidence-pending`)
4. new entries in `requirement_change_candidates.md` for newly discovered concrete candidates, added to the raw-intake list and re-normalized at the next R-01 pass
5. new entries in `architecture_open_questions.md` for unresolved architectural questions and technical evidence gaps

Rule: input documentation does not silently mutate requirements. It produces evidence supporting or contradicting candidates, surfaces blind spots, and feeds the staging artifact and the architecture backlog.

Research has no separate summary artifact. The handoff into Synthesis is `P-01-research/R-02-input-documentation/overview.md` plus the companion input documentation files.

### 5. Synthesis Phase

Placement: Phase 2, after the Research checkpoint.

Purpose: turn research findings into requirement-facing and architecture-facing question sets that Design can resolve.

Synthesis consists of two ordered subphases:

1. `S-01-requirement-question-framing`
2. `S-02-architecture-question-framing`

#### S-01 requirement-question-framing

Frames the requirement-facing what-should-be-true questions surfaced by Research.

It works through questions about:

1. scope ambiguity
2. preserved behavior ambiguity
3. constraints that may be real, partial, or outdated
4. success criteria that are too vague or incomplete
5. assumptions that are still implicit
6. contradictions between developer intent and discovered current-state evidence

Output: `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md` containing a cleaned and ordered set of what-should-be-true requirement questions, an explicit distinction between contract-affecting and non-contract-affecting items, and the narrowed base for `S-02-architecture-question-framing`.

#### S-02 architecture-question-framing

Frames the architectural questions ("how we do it technically") that architecture deliberation consumes.

It works through questions about:

1. backend vs frontend ownership
2. layering and module boundaries
3. API and contract shape
4. data model
5. abstractions and interfaces
6. sequencing and integration strategy

It operates on the cleaned question space from S-01, not on raw research noise.

Output: `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md` containing deduplicated architectural questions, dependency order, clear separation of architectural decisions versus evidence gaps, and the briefing that `D-02-architecture-deliberation` consumes.

Architecture IDs are not assigned here. They begin only once `D-02-architecture-deliberation` records the developer's chosen concrete direction and the orchestrator moves the resolved items into the normalized section of `architecture_open_questions.md`.

#### Synthesis handoff

The two framing files are the synthesis handoff into Design. `requirement-question-framing.md` frames what should be true by the end of the task. `architecture-question-framing.md` frames the architectural how-do-we-do-it questions. There is no `synthesis.md` artifact.

`architecture_open_questions.md` is not replaced by `architecture-question-framing.md`. It remains the living architecture backlog across phases. Requirement-side uncertainty remains in the requirement pipeline and is not stored in `architecture_open_questions.md`.

### 6. Checkpoints

Placement: between phases, starting with the Research checkpoint.

Purpose: review completed phase outputs through `R-01-adversarial-review` followed by human review, then approve forward movement or send the work back.

Output: `P-99-review/cp<N>-<phase>.md` for each checkpoint:

1. `cp1-research.md`
2. `cp2-synthesis.md`
3. `cp3-design.md`
4. `cp4-planning.md`
5. `cp5-implementation.md`

Rules:

1. Checkpoints are review-only. They do not generate new working artifacts.
2. CP4 specifically verifies that `implementation_plan.md` adds nothing beyond dependency ordering, checklist step definition, and verification, and that every output documentation file is scheduled.
3. Closing has no checkpoint review for MVP.

### 7. Design Step D-01 Requirement Clarification

Placement: first step of Design, after the Synthesis checkpoint and before `D-02-architecture-deliberation`.

Purpose: work through requirement-level uncertainty with the developer at the start of Design and promote approved clarifications into `requirements.md` before architecture deliberation begins.

This is conceptually distinct from architecture deliberation.

At `D-01`, the agent or orchestrator must ask the **what should be true by the end of the task** questions to the developer one by one, include explicit options where helpful, wait for the developer's answer, and then record that answer:

1. scope
2. preserved behavior
3. constraints
4. success checks
5. non-goals
6. assumptions that affect the contract

`D-02-architecture-deliberation` then works through the **architectural how we do it** questions with the developer:

1. how the approved contract is satisfied
2. backend vs frontend ownership
3. layering and module boundaries
4. API and contract shape
5. data model
6. sequencing and integration strategy

Output: `P-03-design/D-01-requirement-clarification/requirement-clarification.md` capturing developer-answered clarifications, promotion-ready updates, rejection rationales, and pending-evidence items.

Disposition rules at D-01:

1. Approved candidates are promoted by the orchestrator from `requirement_change_candidates.md` into `requirements.md`.
2. Rejected candidates are recorded in `requirement-clarification.md` and then removed by the orchestrator from `requirement_change_candidates.md`.
3. Evidence-pending candidates remain in `requirement_change_candidates.md` for a later batched Research re-entry.

If new contradictions appear and cannot be resolved inside `D-01-requirement-clarification`, the workflow re-enters Research per the Re-entry Rule.

### 8. Promotion into the Approved Contract

Placement: at `D-01-requirement-clarification` only.

Purpose: move approved requirement conclusions into `requirements.md`.

Source: `requirement_change_candidates.md`.

Target: `requirements.md`.

Rule: only items explicitly approved through the developer's answers at `D-01-requirement-clarification` are promoted, and the orchestrator performs the move. This applies both to new requirements and to changes against already approved requirements. The staging artifact remains the audit trail of how the contract evolved.

### 9. Planning, Implementation, Closing

Placement: Phases 4, 5, and 6 after Design.

`P-04-planning/P-01-implementation-planning` reads `requirements.md`, `architecture.md`, `P-03-design/D-04-output-documentation/`, `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md`, and the relevant code, then produces the scheduling-only `implementation_plan.md`. By the time Planning begins, `architecture.md` must already contain the approved architectural items promoted at the end of Design, and those promoted items must already have been removed from `architecture_open_questions.md`. The plan relies on the requirement and architecture IDs carried by the output documentation layer and turns them into dependency-ordered implementation phases and checklist steps. If Planning surfaces a problem while writing that plan, it should still try to complete the plan, record the problem in the plan's issues section, present a suggested next action, and wait for discussion before any issue is resolved, discarded, or turned into requirement or architecture intake. Substantive design or contract content does not belong here and must be rejected at CP4 if it appears.

`P-05-implementation/I-01-implementation` uses one Coder agent working sequentially from the first unchecked step to the last. The Coder checks off completed steps in `implementation_plan.md`, runs the step or phase verification notes, records issues in the plan's issues section, and writes `implementation_results.md`. Discussion then resolves, discards, or turns those issues into requirement or architecture intake. They do not automatically become staging artifacts just because they were discovered during implementation.

`P-06-closing` starts only after implementation is approved. It updates onboarding documentation from the approved requirements, approved architecture, output documentation, and actual code, then writes `final_report.md`. Closing has no checkpoint review for MVP.

---

## Re-entry Rule

Raw requirement intake can happen at any phase.

Promotion cannot happen at any phase. Promotion happens only at `D-01-requirement-clarification`.

Raw-intake entries and evidence-pending candidates can accumulate in `requirement_change_candidates.md` until the developer chooses an opportune time to re-enter Research.

If a phase ends and `requirement_change_candidates.md` still contains raw-intake entries or evidence-pending candidates that should be revisited, the developer can choose to jump back to the start of Research. That restart re-runs the same promotion cycle:

1. normalize raw intake (`R-01-requirements-normalization`)
2. re-run input documentation against the normalized and evidence-pending set (`R-02-input-documentation`)
3. re-run Synthesis (S-01 then S-02)
4. re-run `D-01-requirement-clarification`
5. update downstream artifacts before resuming later phases

This makes requirement promotion repeatable and batch-friendly even when a new requirement appears in the middle of Design, Planning, or Implementation.

---

## Corrective Loops

The workflow supports re-entry without collapsing artifact roles.

### If current-state understanding changes

1. repair `P-01-research/R-02-input-documentation/`
2. add or update entries in `requirement_change_candidates.md` if the changed understanding affects requirement interpretation
3. add entries to `architecture_open_questions.md` only for architectural implications and technical evidence gaps
4. re-run Synthesis framing artifacts if the issue is material

### If target-state intent changes

1. repair `P-03-design/D-04-output-documentation/`
2. add candidates to `requirement_change_candidates.md` if the change affects the approved contract
3. add entries to `architecture_open_questions.md` if the change introduces architectural uncertainty without changing the contract
4. re-enter `D-01-requirement-clarification` or `D-02-architecture-deliberation` depending on whether the issue is requirement-level or architecture-level

### If design exposes a requirement-level gap

1. stop `D-02-architecture-deliberation`
2. return to `D-01-requirement-clarification`
3. re-enter Research per the Re-entry Rule if the gap reflects missing or contradictory evidence rather than a local clarification issue
4. resume Design only after the requirement-level gap is resolved and promoted as needed

### If scope changes (e.g., a new overlooked branch is discovered)

1. expand `P-01-research/R-02-input-documentation/` first
2. revisit normalized candidates against the larger surface
3. update architecture open questions in `architecture_open_questions.md` and requirement candidates in `requirement_change_candidates.md` before continuing downstream

---

## Phase Placement Summary

### Creation

1. create the task folder and `task.md`
2. record initial raw requirements in `requirement_change_candidates.md`
3. detect onboarding drift and refresh onboarding

### Research

1. run `R-01-requirements-normalization` against raw intake in `requirement_change_candidates.md`
2. run `R-02-input-documentation` to pressure-test normalized candidates against current-state code
3. annotate input docs with requirement IDs
4. emit newly discovered concrete contract candidates into `requirement_change_candidates.md` and newly discovered architectural questions into `architecture_open_questions.md`
5. write `cp1-research.md` at the checkpoint

### Synthesis

1. run `S-01-requirement-question-framing` and write `requirement-question-framing.md`
2. run `S-02-architecture-question-framing` and write `architecture-question-framing.md`
3. write `cp2-synthesis.md` at the checkpoint

### Design

1. run `D-01-requirement-clarification`, write `requirement-clarification.md`, promote approved items into `requirements.md`, remove rejected items from `requirement_change_candidates.md`, leave evidence-pending items staged
2. run `D-02-architecture-deliberation` and write `architecture-deliberation.md`
3. run `D-03-output-dry-run-planning` and write `output-dry-run-planning.md`
4. run `D-04-output-documentation` and write the per-file output documentation plus `overview.md`
5. write `cp3-design.md` at the checkpoint

### Planning

1. run `P-01-implementation-planning` and write the scheduling-only `implementation_plan.md` with dependency-ordered phases and checklist steps
2. write `cp4-planning.md` at the checkpoint

### Implementation

1. run `I-01-implementation` through one sequential Coder agent, executing edits, checking off completed steps, reporting issues in the plan's issues section, running verification notes, and writing `implementation_results.md`
2. discuss implementation issues and either resolve them, discard them, or turn them into requirement or architecture intake
3. write `cp5-implementation.md` at the checkpoint

### Closing

1. update onboarding documentation from output documentation, code, architecture, and requirements after implementation approval
2. write `final_report.md`
2. no checkpoint review for MVP

---

## Artifact Responsibilities at a Glance

| Artifact                                                                             | Role                                                                                                      | Lifetime                        |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- | ------------------------------- |
| `task.md`                                                                            | Global task tracker                                                                                       | Living task artifact            |
| `requirement_change_candidates.md`                                                   | Staging for raw-intake, normalized, and evidence-pending candidates                                       | Living cross-phase queue        |
| `requirements.md`                                                                    | Finalized and approved requirements contract                                                              | Living contract artifact        |
| `architecture.md`                                                                    | Finalized and approved architecture contract keyed by architecture ID                                     | Living contract artifact        |
| `architecture_open_questions.md`                                                     | Living cross-phase architecture backlog of unresolved architectural questions and technical evidence gaps | Living cross-phase queue        |
| `P-XX-<phase>/progress.md`                                                           | Per-phase progress tracker                                                                                | Phase-local artifact            |
| `P-01-research/R-02-input-documentation/<path>.md` and `overview.md`                 | Current-state evidence and normalized requirement ID map                                                  | Living task-local documentation |
| `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`   | Requirement-facing question framing                                                                       | Phase artifact                  |
| `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md` | Architecture-facing question framing                                                                      | Phase artifact                  |
| `P-03-design/D-01-requirement-clarification/requirement-clarification.md`            | Requirement clarification and promotion record                                                            | Phase artifact                  |
| `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md`            | Architecture deliberation record and architecture ID assignment point                                     | Phase artifact                  |
| `P-03-design/D-03-output-dry-run-planning/output-dry-run-planning.md`                | Output dry-run plan                                                                                       | Phase artifact                  |
| `P-03-design/D-04-output-documentation/<path>.md` and `overview.md`                  | Target-state dry run plus requirement/architecture ID traceability                                        | Living task-local documentation |
| `P-04-planning/P-01-implementation-planning/implementation_plan.md`                  | Scheduling-only implementation plan with checklist state and issues section                               | Phase artifact                  |
| `P-05-implementation/I-01-implementation/implementation_results.md`                  | Sequential implementation results and issue dispositions                                                  | Phase artifact                  |
| `P-99-review/cp<N>-<phase>.md`                                                       | Checkpoint review record                                                                                  | Phase artifact                  |
| `final_report.md`                                                                    | Closeout summary                                                                                          | Closing artifact                |

---

## Final Summary

The workflow adopts a three-layer requirement model:

1. Raw intake is recorded in `requirement_change_candidates.md` during Creation and at any later phase where new material appears.
2. Normalization and questioning happen in Research (`R-01-requirements-normalization` then `R-02-input-documentation`). Contract-affecting findings feed back into `requirement_change_candidates.md`. Architectural uncertainty feeds into `architecture_open_questions.md`.
3. The approved contract lives in `requirements.md` and is updated only through explicit promotion at `D-01-requirement-clarification`.

Input documentation tracks normalized requirement IDs. Output documentation later tracks both approved requirement IDs and architecture IDs, with the output `overview.md` aggregating those IDs into traceability tables that Planning can consume. Requirement items move through orchestrator-owned transitions in `requirement_change_candidates.md`: raw-intake to normalized at `R-01-requirements-normalization`, then promoted into `requirements.md` or removed after rejection. Changes to already approved requirements use that same path. Architecture items themselves move through orchestrator-owned transitions in `architecture_open_questions.md`: raw-intake to normalized at `D-02-architecture-deliberation`, then promoted into `architecture.md` or removed after rejection.

The requirement pipeline and the architectural pipeline remain separate. Requirement-side uncertainty stays in `requirement_change_candidates.md`, is framed in `requirement-question-framing.md`, and is resolved in `D-01-requirement-clarification`. Architectural uncertainty stays in `architecture_open_questions.md`, is framed in `architecture-question-framing.md`, and is resolved in `D-02-architecture-deliberation`. Checkpoints review completed phase outputs in `P-99-review/cp<N>-<phase>.md` rather than generating new working artifacts. Design begins with `D-01-requirement-clarification` before `D-02-architecture-deliberation`. Input and output documentation remain distinct truth layers under `P-01-research/R-02-input-documentation/` and `P-03-design/D-04-output-documentation/`.

That is the canonical requirement-development model for the heavy-task workflow.
