# Architecture Development and Approval

## Purpose

This document captures how architecture is gathered, scoped, framed, deliberated, projected, stress-tested, approved, and promoted through the heavy-task workflow.

It is the canonical companion to `overview.md` and `requirements.md`. Where `overview.md` defines the artifacts, locations, phase structure, and tandem-lane model, this document defines the lifecycle that flows architectural work through those artifacts.

---

## Core Identity

The workflow treats architectural intake, normalized architectural items, projected architecture, and the approved architecture contract as distinct states.

The model is:

1. `task.md` is the global task tracker. It records objective, developer notes, phase progress, execution order, and links to phase artifacts. It is not the architecture contract.
2. `architecture_open_questions.md` is the orchestrator-owned working staging artifact for architecture development. It accepts raw architectural intake at task start, including explicit developer-directed target outcomes and proposed technical directions, and later technical review feedback. The orchestrator moves entries from its raw-intake section into its normalized section when `D-02-architecture-deliberation` turns them into canonical architecture items with architecture IDs, and removes them entirely once they are promoted into `architecture.md` or rejected.
3. `architecture.md` is the finalized and approved architecture contract written solely by the orchestrator. Items land here only after `D-02-architecture-deliberation` records the developer's chosen direction and assigns architecture IDs, `D-03-output-dry-run-planning` and `D-04-output-documentation` project that direction, `cp3-design.md` is initialized, adversarial review runs, and the human developer approves the projected result. Promotion preserves the architecture IDs assigned at deliberation. Changes to already approved architecture entries do not happen directly in `architecture.md`; they re-enter `architecture_open_questions.md` and move through the same staging cycle again.
4. `architecture-question-framing.md` is the synthesis handoff that frames the architectural decision space for Design. It never replaces `architecture_open_questions.md`.
5. `architecture-deliberation.md` records the concrete architectural directions chosen at `D-02-architecture-deliberation`. It is a decision record, not the final approved architecture contract.
6. The output documentation layer is the projection surface where the workflow stress-tests whether the chosen architectural direction can actually carry the intended implementation and intent.

This preserves the separation between initial architectural intent, normalized architecture items awaiting final disposition, projected target-state, and approved architecture contract.

The orchestrator is a workflow role in direct discussion with the developer. That role may be fulfilled by a dedicated orchestrator agent or by the developer-facing agent that accepted and initiated the workflow. It is the sole writer of `task.md`, `architecture_open_questions.md`, `architecture.md`, `requirement_change_candidates.md`, `requirements.md`, and each phase `progress.md`. Skill-owned artifacts may be written by their owning skills, but they do not directly author these orchestrator-owned runtime documents.

---

## Key Principles

### Requirements and architecture develop in tandem

Requirements and architecture are co-dependent.

Requirements answer **what should be true by the end of the task**.

Architecture answers **how that truth is carried through the system technically**.

The workflow develops them in parallel because Research needs early architectural awareness in order to size the change, pull the right onboarding, and understand likely hotspots. But it delays concrete architectural commitment until requirements are settled, because technical direction chosen against an unstable contract is usually wasted effort.

### Inputs enable early parallelism

The input documentation layer makes tandem development possible.

`P-01-research/R-02-input-documentation/` freezes current-state truth. It shows the actual seams, boundaries, behaviors, and dependencies that matter.

That lets Research and Synthesis work both lanes against the same reality:

1. the requirement lane can ask whether the contract is correct
2. the architecture lane can ask where change is likely to land and what rough direction seems plausible

Without the input layer, architectural work would either start too late or guess too much.

### Architecture becomes concrete only at deliberation

Architecture does not have a pre-deliberation normalization step equivalent to requirements.

Before `D-02-architecture-deliberation`, architecture exists as initial intake, research-scoped hotspots, and framed questions. That intake may already include an explicit developer-preferred outcome or proposed direction, but it is not yet a canonical architecture record.

The first place where the workflow records concrete architectural solutions is `D-02-architecture-deliberation`, after `D-01-requirement-clarification` has established the approved requirements contract with the developer.

That is also the point where architecture items are normalized. The orchestrator moves each resolved item out of the raw-intake section of `architecture_open_questions.md`, writes it into the normalized section with its architecture ID, and deletes the old raw-intake entry.

### IDs begin at deliberation

Requirements receive canonical requirement IDs during normalization.

Architecture does not.

Architectural intake stays as pre-approval raw intake, scoped hotspots, and framed questions until `D-02-architecture-deliberation` records the developer's chosen concrete direction and normalizes it.

That is the point where canonical architecture IDs are assigned.

The move semantics are:

1. raw-intake entries live in `architecture_open_questions.md`
2. after `D-02-architecture-deliberation`, the orchestrator moves resolved entries into the normalized section of `architecture_open_questions.md`
3. once approved after CP3, the orchestrator promotes normalized entries into `architecture.md` and removes them from `architecture_open_questions.md`
4. if an item is rejected, the orchestrator removes it from `architecture_open_questions.md` after its disposition is recorded

`architecture_open_questions.md` is therefore a working queue, not an archive. When current items have been promoted or rejected, the file should be empty.

`architecture.md` is never edited as a shortcut around this staging flow. Even changes to already approved architecture must re-enter `architecture_open_questions.md`, be normalized again at `D-02-architecture-deliberation`, and then be promoted by the orchestrator.

From that point forward, those architecture IDs must be carried through:

1. `architecture-deliberation.md`
2. `D-04-output-documentation/` per-file outputs
3. the output `overview.md` traceability tables
4. `cp3-design.md`
5. `architecture.md`
6. later planning and implementation artifacts

### Outputs enable explicit stress testing

The output documentation layer makes architectural approval possible.

`D-03-output-dry-run-planning` and `D-04-output-documentation` turn the chosen architectural direction into projected code structure, projected behavior, and projected intent.

That projection uses the task-local input documentation together with the corresponding onboarding references for every in-scope input file.

This is not cosmetic. It is the stress-test surface that shows whether the architectural direction actually supports the approved requirements in a coherent way.

### Approval is explicit and post-projection

`D-02-architecture-deliberation` does not approve architecture on its own.

Architectural approval happens only after a full post-output stress-test cycle:

1. the architecture is projected into the target-state output documentation
2. `cp3-design.md` is initialized explicitly
3. `R-01-adversarial-review` stress-tests the projected result
4. the workflow surfaces the most important hotspot projections plus intent in chat
5. the human developer reviews the projected direction and responds

Only then can approved architectural items be promoted into `architecture.md`.

That promotion is not deferred to Planning. The orchestrator performs it as part of closing the Design phase, and Planning starts only after `architecture.md` has been updated.

### Correction loops must respect the type of failure

Not every review failure means going back to Research.

The correction rule is:

1. if the feedback remains purely technical, record it in `architecture_open_questions.md` raw-intake and jump back to `D-02-architecture-deliberation`
2. if the feedback shows the requirements were not properly understood, record requirement items in `requirement_change_candidates.md`, record architectural items in `architecture_open_questions.md`, and jump back to Research

Only jump farther back than `D-02-architecture-deliberation` when the problem is no longer purely technical.

---

## Artifact Model

### Root task level

1. `task.md`
   Purpose: global task tracker written solely by the orchestrator.
   Contents: objective, developer notes, global phase tracker, execution order, links to phase artifacts, implementation status summary.

2. `architecture_open_questions.md`
   Purpose: orchestrator-owned staging artifact for architecture development and disposition.
   Contents: raw-intake concerns, developer-directed target outcomes, proposed technical directions, scoped hotspots, unresolved architectural questions, evidence gaps, normalized architecture items with architecture IDs, statuses, owners, review feedback, and re-entry items. It holds architecture work until the orchestrator promotes or rejects it.

3. `architecture.md`
   Purpose: finalized and approved architecture contract written solely by the orchestrator.
   Contents: approved architecture IDs together with their boundaries, ownership, responsibilities, interfaces, sequencing expectations, chosen direction, and accepted architectural constraints. Existing entries are changed only when the orchestrator promotes an approved staged change into this file.

4. `final_report.md`
   Purpose: closeout summary of what was learned, decided, and built. Written at Closing only.

### Phase artifacts

1. `P-01-research/R-02-input-documentation/<path/code_file_name>.md` and `overview.md`
   Current-state truth used by both lanes. These files expose the seams, behaviors, boundaries, and change surfaces that Research and Synthesis reason about.

2. `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`
   Ordered architecture question set, dependency order, evidence gaps, and briefing for `D-02-architecture-deliberation`.

3. `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md`
   Concrete architectural options, tradeoffs, chosen direction, rejected directions, rationale, and the first assignment of canonical architecture IDs. This is also where the orchestrator moves resolved items from raw-intake to normalized in `architecture_open_questions.md`.

4. `P-03-design/D-03-output-dry-run-planning/output-dry-run-planning.md`
   Dry-run plan for stress-testing the chosen architecture through projection, including the corresponding onboarding references for every in-scope input file.

5. `P-03-design/D-04-output-documentation/<path/code_file_name>.md` and `overview.md`
   Target-state projection layer used to stress-test architecture before approval. Per-file output docs carry the approved requirement IDs and the architecture IDs that apply to that file. Output generation uses task-local inputs plus the corresponding onboarding references for every mapped input file. The output `overview.md` contains traceability tables for all approved requirement IDs and all architecture IDs, each with one or more file links showing where the ID appears.

6. `P-99-review/cp3-design.md`
   Explicit review checkpoint for the projected design package. It records adversarial review findings, human review outcome, and checkpoint disposition.

The workflow also surfaces the most important projected hotspots plus intent in chat during review. Chat is a review surface, not the canonical architecture record.

---

## Architecture Lifecycle

### 1. Architectural Intake

Placement: Creation, immediately after task folder creation. Also any later point where new technical feedback or new architectural concerns appear.

Target artifact: `architecture_open_questions.md` raw-intake list.

Purpose: preserve rough architectural concerns, suspected hotspots, and likely change areas without forcing premature solution decisions.

Architectural intake may already include an explicit developer target outcome, such as moving ownership from frontend to backend. The point of intake is not to erase that intent. The point is to preserve it without prematurely converting it into an approved architecture contract.

Captured for each entry as appropriate:

1. the concern, desired target outcome, or proposed technical change
2. suspected seams or affected surfaces
3. why it matters
4. links to related requirements if known
5. current evidence if any
6. source of the concern

Rule: architectural intake is intentionally pre-approval. It may contain explicit preferred direction from the developer, but it is not yet a canonical architecture record and it is never written directly into `architecture.md`. The orchestrator owns this capture.

That includes proposed changes to architecture that is already approved. Those changes must be captured as new raw-intake entries in `architecture_open_questions.md`, not applied directly inside `architecture.md`.

### 2. Research Scoping

Placement: Research, primarily through `R-02-input-documentation`, after requirements have been normalized by `R-01-requirements-normalization`.

Purpose: use normalized requirements, onboarding, and code to understand where architectural change may be needed and what rough direction is emerging.

Research does not pick concrete architectural direction. It narrows the problem space.

Outputs:

1. input documentation and overview that expose the actual current-state seams
2. scoped hotspots and evidence links added to `architecture_open_questions.md`
3. technical evidence gaps recorded in `architecture_open_questions.md`

Rule: Research can suggest rough direction, likely blast radius, and likely affected areas, but it does not yet produce the concrete architecture contract.

### 3. Architecture Question Framing

Placement: `S-02-architecture-question-framing`, after `S-01-requirement-question-framing`.

Purpose: turn the pre-approval architectural problem space into an explicit decision set for Design.

Inputs:

1. `P-01-research/R-02-input-documentation/overview.md`
2. companion input documentation files
3. code
4. `architecture_open_questions.md`
5. `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`
6. `requirements.md` for already-approved contract context if present

Output: `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md` containing ordered architectural questions, dependencies, evidence gaps, and briefing for `D-02-architecture-deliberation`.

Rule: framing organizes the architectural decision space. It does not commit to a solution.

### 4. Architecture Deliberation

Placement: `D-02-architecture-deliberation`, after `D-01-requirement-clarification` has settled the approved requirements contract.

Purpose: work through concrete architectural direction with the developer.

Inputs:

1. `requirements.md`
2. `P-03-design/D-01-requirement-clarification/requirement-clarification.md`
3. `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`
4. `architecture_open_questions.md`
5. input documentation and overview

Outputs:

1. `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md` containing the developer-chosen direction and the architecture IDs assigned to each concrete architectural item
2. `architecture_open_questions.md` updated by the orchestrator so resolved raw-intake items are moved into a normalized section and removed from raw-intake

Rule: at `D-02`, the agent or orchestrator must ask the architecture-facing questions to the developer one by one, include explicit options and tradeoffs where helpful, wait for the developer's answer, and then record that answer. This is the first place where the workflow chooses concrete architectural solutions, and therefore the first place where canonical architecture IDs exist. It is also the point where architecture items become normalized.

### 5. Projection and Dry Run

Placement: `D-03-output-dry-run-planning` and `D-04-output-documentation`.

Purpose: turn the chosen architectural direction into projected implementation and projected intent.

Inputs for this projection come from the approved design artifacts, the task-local input documentation, and the corresponding onboarding references for every in-scope input file.

Outputs:

1. `output-dry-run-planning.md`
2. target-state per-file output documentation carrying the approved requirement IDs and the architecture IDs relevant to each file
3. target-state overview containing two traceability tables: all approved requirement IDs and all architecture IDs, each with one or more file links showing where the ID appears
4. projected hotspots and intent surfaced in chat for discussion

Rule: projection is the first proof that the chosen architecture can actually carry the intended behavior and intent. It must preserve ID traceability so that later planning can order work against stable requirement and architecture references.

### 6. Explicit Stress Testing

Placement: immediately after `D-04-output-documentation` completes.

Purpose: stress-test the projected architecture before approval.

Process:

1. initialize `cp3-design.md` explicitly
2. run `R-01-adversarial-review` against the projected output package
3. surface the most important hotspot projections plus intent in chat
4. collect human developer feedback

Rule: review is explicit. It is never implied just because `D-04-output-documentation` finished.

### 7. Approval and Promotion

Placement: after the full stress-test cycle passes.

Purpose: promote approved architectural direction into the root architecture contract and close the Design phase on an updated approved architecture record.

Source artifacts:

1. `architecture-deliberation.md`
2. `D-04-output-documentation/`
3. `cp3-design.md`
4. human review outcome

Target artifact: `architecture.md`.

Owner of every move step: the orchestrator as part of the workflow.

Disposition rules:

1. approved normalized architectural items are promoted into `architecture.md` with the same architecture IDs assigned during `D-02-architecture-deliberation`, and the orchestrator removes them from `architecture_open_questions.md`
2. rejected or superseded directions remain recorded in the design artifacts and review record, and the orchestrator removes them from `architecture_open_questions.md`
3. technical-only changes requested during review go into `architecture_open_questions.md` raw-intake and restart at `D-02-architecture-deliberation`
4. requirement-understanding failures go into both `requirement_change_candidates.md` and `architecture_open_questions.md` and restart at Research

Rule: Design is not complete at "approved in review". It becomes complete only after the approved architectural items have been written into `architecture.md` and removed from `architecture_open_questions.md`. This same rule applies when changing an already approved architecture entry: the staged replacement or update must be promoted by the orchestrator rather than edited directly in `architecture.md`. Planning must start from that updated approved contract, not from a merely projected or promotable design package.

---

## Architecture Milestones

Primary milestone states:

1. `raw-intake` — initial architectural concern or intended target outcome captured
2. `scoped` — Research has identified likely seams, hotspots, and rough direction
3. `framed` — Synthesis has compiled the actual architecture question set
4. `normalized` — `D-02-architecture-deliberation` has turned the item into a concrete architecture record with an architecture ID and the orchestrator has moved it out of raw-intake
5. `projected` — direction has been turned into target-state output documentation and intent projection
6. `adversarially-reviewed` — projected architecture has been stress-tested by adversarial review
7. `human-reviewed` — projected architecture has been reviewed by the developer
8. `approved` — projected architecture has passed the explicit review cycle
9. `promoted` — approved architecture IDs have been written into `architecture.md` and removed from `architecture_open_questions.md`
10. `implemented` — code has been brought into line with the approved architecture

Supporting states:

1. `evidence-pending` — more technical evidence is needed before safe movement
2. `rejected` — a proposed direction was considered and discarded
3. `superseded` — an older direction was replaced by a newer approved direction

Rule: architecture does not have a pre-deliberation `normalized` milestone. The first concrete architectural direction, the first canonical architecture IDs, and the first normalized architecture entries exist only after `D-02-architecture-deliberation`.

---

## Re-entry Rules

### Technical-only review feedback

If post-output review surfaces a technical issue while the requirements remain sound:

1. record the feedback in `architecture_open_questions.md` raw-intake
2. jump back to `D-02-architecture-deliberation`
3. update `D-03-output-dry-run-planning` and `D-04-output-documentation`
4. initialize `cp3-design.md` again and rerun the stress-test cycle

This is the architecture-equivalent fast loop.

### Requirement-understanding failure

If post-output review shows the requirements were not properly understood:

1. record the requirement issue in `requirement_change_candidates.md`
2. record the architectural issue in `architecture_open_questions.md`
3. jump back to Research
4. rerun Synthesis, requirement clarification, architecture deliberation, projection, and review

This is the full tandem-lane correction loop.

---

## Final Summary

The architecture lane exists from task start, and it may begin with explicit developer-directed target outcomes, but it does not become canonical immediately.

Research scopes the likely architectural problem space using normalized requirements, onboarding, and code.

Synthesis frames the actual architecture questions.

`D-02-architecture-deliberation` is the first point where the workflow records the developer's chosen concrete architectural direction and normalizes architecture items.

`D-03-output-dry-run-planning` and `D-04-output-documentation` then project that direction into target-state outputs so the workflow can stress-test both intended implementation and intended intent.

Architecture IDs begin at `D-02-architecture-deliberation`, when the orchestrator moves resolved items from raw-intake to normalized in `architecture_open_questions.md`, and are then carried through the output files, the output overview traceability tables, `cp3-design.md`, `architecture.md`, and later planning.

Approval is explicit and post-projection: projection, adversarial review, and human developer feedback together determine whether the architecture is good enough to be written into `architecture.md`.

That write happens before Planning begins, as the final action of Design, and the promoted items are removed from `architecture_open_questions.md` when they move.

If feedback remains purely technical, the workflow loops back to `D-02-architecture-deliberation`.

If feedback reveals the requirements were misunderstood, the workflow logs the issues in both lanes and jumps back to Research.

That is the canonical architecture-development model for the heavy-task workflow.