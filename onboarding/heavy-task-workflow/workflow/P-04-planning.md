# Planning Phase

## Purpose

This document defines how the heavy-task workflow turns approved requirements, approved architecture, target-state outputs, and current code reality into an implementation plan.

It is the Planning-phase companion to `overview.md`, `requirements.md`, and `architecture.md`.

Where:

1. `requirements.md` defines what must be true by the end of the task
2. `architecture.md` defines how that truth is carried technically
3. `P-03-design/D-04-output-documentation/` defines the projected target-state surfaces
4. Planning defines the safest and clearest execution order for implementing that approved target-state

Planning is intentionally downstream of approval. It does not settle contract uncertainty or architecture uncertainty. It sequences work that has already been approved.

---

## Current Status

The workflow already defines several Planning constraints canonically, and this document makes them explicit as the Planning-phase operating model.

The canonical constraints already established are:

1. Planning starts only after Design is complete
2. Design is complete only after approved architecture has been promoted into `architecture.md`
3. `implementation_plan.md` is scheduling-only
4. Planning consumes requirement IDs, architecture IDs, their plain-language summaries, and representative examples from the output documentation layer
5. CP4 must reject design or contract content that leaks into Planning

This document extends those constraints into a concrete Planning model so the workflow can operate coherently now.

Where this document extrapolates beyond what the current root companion docs say explicitly, it does so conservatively and calls the remaining gaps out in the final section.

---

## Core Identity

Planning is the phase that converts approved target-state into execution order.

The model is:

1. `requirements.md` is the approved requirements contract and is not reinterpreted here
2. `architecture.md` is the approved architecture contract and is not redesigned here
3. `P-03-design/D-04-output-documentation/` is the approved target-state implementation map that Planning schedules against
4. `P-04-planning/P-01-implementation-planning/implementation_plan.md` is the phase artifact that orders the work into dependency-ordered implementation phases and checklist steps, while only quoting representative code examples that already exist in `D-04`
5. `P-99-review/cp4-planning.md` is the review checkpoint that verifies the plan is complete, scheduling-only, and aligned with the approved target-state

Planning answers one question:

How do we implement the approved target-state safely, in order, with clear verification gates?

Planning does not answer these questions:

1. What should be true by the end of the task
2. How the system should be designed if that design is still disputed
3. Whether an unapproved architectural direction is better than the approved one
4. Whether a new contract change should be adopted without re-entering the requirement or architecture staging flows

---

## Key Principles

### Planning starts from approved contracts, not from proposals

By the time Planning begins:

1. approved requirements must already be in `requirements.md`
2. approved architecture must already be in `architecture.md`
3. promoted architecture items must already have been removed from `architecture_open_questions.md`
4. projected target-state outputs must already exist under `P-03-design/D-04-output-documentation/`

Planning must not proceed against raw requirement intake, unresolved architecture questions, or merely projected but unapproved architectural direction.

### Planning is scheduling-only

Planning orders work. It does not create new design.

That means Planning may:

1. group work into implementation phases
2. order checklist steps by dependency
3. define verification notes for each step or phase
4. note implementation risks that affect sequencing
5. pull representative approved code examples from `D-04` when concrete examples improve plan readability

That means Planning may not:

1. introduce new requirements
2. change approved requirements without re-entry
3. change approved architecture without re-entry
4. rewrite the target-state output package as a substitute for Design
5. hide design decisions inside sequencing notes
6. invent concrete code examples that are missing from `D-04`

### Finish the plan before escalating discovered issues

If Planning discovers problems while writing the plan, it should first try to complete the plan using the currently approved contracts and outputs.

That means Planning should:

1. keep the checklist steps focused on the currently approved target-state
2. collect newly discovered problems separately from the checklist in an issues section
3. append those issue entries at the bottom of `implementation_plan.md`
4. add a suggested next action explaining what discussion should likely decide next
5. present the plan together with those issues and wait for developer approval

If a coherent plan cannot be completed safely, Planning should still write the safe portion of the plan, make the blockage explicit, append the issue at the bottom, and stop for approval instead of pretending the issue was resolved locally.

### Planning may read code freely

Planning is allowed to read whatever code it needs.

The discipline in this phase comes from the artifact boundary, not from restricting code reads.

That means the planner may read code broadly enough to understand:

1. real dependency order
2. risky seams and hidden coupling
3. preparatory work that the target-state outputs alone do not make obvious
4. the concrete files or areas each step should name

Planning still may not use those reads as permission to reopen requirement or architecture decisions that belong upstream.

### Planning is shaped by four inputs

The implementation plan is shaped by four different forces:

1. requirements shape what must become true and what must be preserved
2. architecture shapes ownership, boundaries, interfaces, and technical dependency order
3. output documentation shapes the intended target-state file map and traceability surface
4. current code shape determines the safest route through the actual implementation terrain

Planning is therefore neither pure design nor a mechanical file list. It is the execution ordering layer between approved target-state and real code change.

### Traceability must survive into the plan

The output documentation layer already carries approved requirement IDs and architecture IDs.

Planning must consume that traceability rather than collapse it.

When the plan spells requirements out, it should carry the same plain-language summaries that the approved `D-04` packet already carries so readers do not need to reopen the contract files just to understand what a step or example is for.

Each implementation phase and checklist step must therefore remain explainable in terms of:

1. which approved requirement IDs it advances
2. which approved architecture IDs it instantiates
3. which output documentation files it is scheduling

Any concrete code example shown in the plan must also stay explainable in those same terms and must be pulled from `D-04` rather than authored locally in Planning.

Each implementation phase and checklist step must therefore remain explainable in the same terms.

Without that traceability, the workflow cannot reliably review whether the plan covers the approved scope.

### Code reality may reshape sequence, but not contract

The output documentation layer defines the approved target-state.

The current codebase defines the safe execution order.

Planning may therefore sequence preparatory changes earlier than the target-state outputs superficially suggest, if the real codebase demands it. But that sequencing flexibility does not authorize Planning to change the approved requirement or architecture contracts.

---

## Inputs and Preconditions

Planning begins only when the following inputs exist and are internally consistent:

1. `requirements.md`
2. `architecture.md`
3. `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md`
4. `P-03-design/D-04-output-documentation/overview.md`
5. the relevant per-file output documentation under `P-03-design/D-04-output-documentation/`
6. the corresponding current-state input documentation under `P-01-research/R-02-input-documentation/`
7. `cp3-design.md` showing the design package has passed explicit review
8. the relevant code that the plan must schedule against

Preconditions:

1. all planning-relevant requirement IDs must already be approved
2. all planning-relevant architecture IDs must already be approved and promoted
3. every in-scope output documentation file must already exist
4. no required implementation surface should exist only as chat context or implied knowledge

If those preconditions do not hold, the workflow is not ready for Planning and must return upstream.

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

Planning is not responsible for:

1. resolving requirement ambiguity
2. resolving architecture ambiguity
3. replacing output documentation
4. recording final implementation results

---

## How Requirements Shape the Plan

Requirements shape the plan by defining the success surface.

Planning uses approved requirements to determine:

1. which behavior changes are mandatory
2. which existing behaviors must be preserved during the rollout
3. which constraints limit execution order
4. which non-goals should not consume implementation capacity
5. which success checks must be satisfied before a step or phase can be considered complete

In practice, requirement influence on the plan appears as:

1. required work inclusion
2. preservation-driven sequencing
3. verification gate content
4. explicit exclusion of out-of-scope work

Example shape:

1. a requirement that behavior must remain backward-compatible may force an adapter-first phase before any destructive cleanup phase
2. a requirement that a new validation rule must exist everywhere may require a shared foundation phase before multiple feature-surface phases can run

Planning should never infer new requirements from implementation convenience.

---

## How Architecture Shapes the Plan

Architecture shape is what turns approved scope into a dependency graph.

Planning uses approved architecture to determine:

1. where ownership belongs
2. which interfaces or contracts must exist before dependent work can begin
3. which layers or modules must change first
4. which dependencies create serialization points
5. which slices can be safely parallelized because their shared boundaries are already stable

In practice, architecture influence on the plan appears as:

1. foundation-before-integration ordering
2. interface-before-callsite ordering
3. data-shape-before-consumer ordering
4. ownership transfer ordering when responsibilities move between components or layers

Architecture therefore shapes the plan more strongly than the final file list alone. A plan that ignores architectural dependency order is not implementable even if it references every target file.

---

## How Output Documentation Shapes the Plan

Output documentation gives Planning the target-state map.

Planning uses the output documentation layer to determine:

1. which files or surfaces are expected to change
2. what each changed file is supposed to become
3. which requirement IDs and architecture IDs apply to each output file
4. which files participate in the same target-state slice
5. which representative projected code examples are available for each distinct change type

The output `overview.md` is especially important because it aggregates requirement and architecture traceability into one scheduling surface.

Planning should treat the output documentation layer as the primary inventory of in-scope implementation targets.

If a file appears necessary for implementation but has no output documentation coverage, that is a gap that should normally be repaired before Planning proceeds.

---

## How Current Code Shape Shapes the Plan

Planning is not allowed to ignore the current codebase just because the target-state outputs are already documented.

Current code shape influences:

1. whether a target slice can be implemented directly or needs preparatory refactoring
2. whether a theoretical parallel split is actually safe
3. whether shared utilities or core contracts create hidden coupling
4. whether tests, fixtures, schemas, configuration, or generated artifacts create additional ordering constraints
5. whether rollback or compatibility steps are needed during implementation

This means the plan must be grounded in both:

1. the approved target-state
2. the actual dependency terrain in the current system

If the code reality materially conflicts with the approved design package, that is not a Planning invention opportunity. It is an upstream issue that may require re-entry.

---

## Planning Lifecycle

### 1. Scope Assembly

Planning begins by assembling the approved execution surface from:

1. `requirements.md`
2. `architecture.md`
3. output documentation overview and per-file docs
4. current-state input documentation

The goal is to produce one coherent implementation scope with stable IDs and file coverage.

Outputs of this step:

1. the list of approved requirement IDs in scope
2. the list of approved architecture IDs in scope
3. the set of output documentation files that must be scheduled
4. the representative example set that Planning may pull from `D-04`
5. the current-state dependencies that affect ordering

### 2. Dependency Extraction

Planning then extracts the execution dependencies implied by the approved architecture and the current codebase.

The point is to answer:

1. what must exist before downstream work can start
2. what can be implemented independently
3. where partial implementation would break behavior or leave the codebase in an incoherent state

Outputs of this step:

1. dependency notes
2. serialization points
3. candidate parallel tracks
4. high-risk transition points

### 3. Phase Formation

Planning groups the scheduled work into implementation phases.

A phase is a coherent implementation slice that:

1. has a clear objective
2. has an intelligible dependency boundary
3. can be verified before the next phase depends on it
4. does not hide unresolved design inside the phase description

The default sizing rule is:

A phase is the smallest dependency-stable chunk of work that leaves the system in a verifiable intermediate state. If work can stay inside that chunk, it becomes checklist steps. If it cannot, it becomes a new phase.

The default phase heuristic in this workflow is:

1. foundation work first
2. ownership or contract-establishing work second
3. downstream integrations next
4. leaf callsites or surface updates after their dependencies are stable
5. cleanup and hardening after the new path is working

This is a planning heuristic, not a substitute for architecture.

### 4. Step Ordering

After phases are formed, Planning writes checklist steps inside each phase and orders them by dependency.

For each phase and step, Planning states:

1. why it is placed where it is
2. which earlier phase or step it depends on
3. what must be true before the step begins
4. what must be verified before the step is considered complete

### 5. Verification Definition

Each step or phase must have an explicit verification note.

The note should be defined using the approved requirements, the intended architecture, the affected output files, and the relevant code reality.

Typical gate content may include:

1. targeted tests
2. build or typecheck confirmation
3. lint or static validation where relevant
4. contract or interface confirmation
5. manual or scenario verification for the specific behavior slice

The exact verification standard is still a workflow gap and is called out later, but the plan must still make the verification note explicit.

### 6. Plan Review

Planning ends by writing `implementation_plan.md` and then passing through `cp4-planning.md`.

CP4 verifies at minimum:

1. the plan is scheduling-only
2. every in-scope output documentation file is scheduled
3. requirement IDs and architecture IDs remain traceable through the plan
4. phase and step ordering is consistent with approved architecture and code reality
5. any concrete code examples are clearly pulled from `D-04` rather than invented in Planning
6. verification notes exist per step or phase

If CP4 finds missing coverage or smuggled design content, Planning is not complete.

---

## Implementation Plan Artifact

`implementation_plan.md` is the Planning-phase output artifact.

Its purpose is to make implementation executable without reopening design.

The artifact should contain these sections:

1. scope summary
2. feature requirements
3. architectural requirements
4. output documentation coverage summary
5. dependency notes
6. implementation steps
7. concrete code examples pulled from `D-04-output-documentation/`
8. planning risks and sequencing assumptions
9. deferred items that are explicitly out of scope for this implementation plan
10. issues section, if any
11. suggested next action and approval hold

### Minimum per-phase and per-step shape

Each phase entry should contain:

1. phase name
2. phase objective
3. dependency rationale
4. steps in dependency order

Each step entry should contain:

1. checkbox item
2. substeps when the implementation slice needs explicit checklist breakdown
3. step objective
4. dependencies
5. scheduled output documentation files or slices
6. feature requirements covered with IDs and short plain-language summaries
7. architectural requirements covered with IDs and short plain-language summaries
8. expected edits or implementation focus
9. verification note

### Concrete code example rule

If the plan includes a concrete code example section:

1. every example must be pulled from the approved `D-04` output docs
2. Planning may quote or copy representative projected code, but may not invent missing examples
3. examples should cover distinct implementation types rather than noisy copy-and-paste variants
4. each example should identify its source output doc plus the feature and architectural requirements it covers in plain language
5. if `D-04` lacks the needed example coverage, Planning should record that as an issue or return upstream rather than patch the gap locally

### Scheduling rule

Every in-scope output documentation file must appear in at least one phase or checklist step.

If one output file is implemented across multiple checklist steps, the plan must explain why that split is necessary.

### Issues section rule

If Planning surfaces a problem while writing the schedule:

1. do not absorb that problem into the checklist as if it were already approved work
2. list it separately in the issues section at the bottom of `implementation_plan.md`
3. record whether it appears to be a requirement issue, an architecture issue, a sequencing issue, or another planning issue
4. add a suggested next action for the developer to approve or redirect
5. wait for discussion before any issue is resolved, discarded, or turned into requirement or architecture intake

### Non-goal rule

`implementation_plan.md` is not the place for:

1. new architecture decisions
2. unresolved requirement debates
3. speculative future redesign
4. final implementation results

---

## Recommended Plan Construction Heuristic

To keep Planning repeatable, construct the plan in this order:

1. enumerate all approved requirement IDs in scope
2. enumerate all approved architecture IDs in scope
3. enumerate all output documentation files in scope
4. map each output file to the requirement IDs and architecture IDs it carries
5. collect the representative `D-04` examples that cover the distinct planned change types
6. read the relevant code to identify dependency anchors from the approved architecture and the current codebase
7. form implementation phases around dependency-stable slices rather than around arbitrary file batches
8. write checklist steps in dependency order inside those phases
9. attach a verification note to each step or phase
10. if the plan includes concrete code examples, quote or copy them from `D-04` with their source paths intact
11. confirm that the full output documentation set is scheduled exactly once or with justified multi-step splits

This keeps the plan anchored in approved scope rather than drifting into ad hoc implementation notes.

---

## Re-entry Rules During Planning

Planning may discover problems, but it does not resolve all of them locally.

### If Planning discovers a requirement issue

If the plan exposes a contract misunderstanding or missing requirement:

1. keep writing the plan if a coherent scheduling pass is still possible
2. record the issue in the issues section of `implementation_plan.md`
3. add a suggested next action describing whether discussion should likely resolve, discard, or turn the issue into requirement intake
4. wait for developer approval and discussion before the orchestrator routes anything into `requirement_change_candidates.md`

Planning must not silently repair requirement contract gaps inside `implementation_plan.md`.

### If Planning discovers an architecture issue

If the plan exposes a technical problem that changes approved architecture:

1. keep writing the plan if a coherent scheduling pass is still possible
2. record the issue in the issues section of `implementation_plan.md`
3. add a suggested next action describing whether discussion should likely resolve, discard, or turn the issue into architecture intake
4. wait for developer approval and discussion before the orchestrator routes anything into `architecture_open_questions.md`

Planning must not silently replace approved architecture with a new design.

### If Planning discovers only a sequencing issue

If the approved target-state is still sound but the execution order needs adjustment:

1. revise `implementation_plan.md`
2. keep the changes scheduling-only
3. rerun CP4 if the change is material

That is a local Planning correction, not an upstream restart.

---

## CP4 Review Expectations

`cp4-planning.md` reviews the plan as a plan.

It should check at least these questions:

1. does the plan cover every in-scope output documentation file
2. does each phase and step name the requirement IDs and architecture IDs it advances
3. does any phase or step smuggle in unresolved design or contract content
4. is the dependency order consistent with the approved architecture
5. do the phase boundaries follow the default sizing rule rather than arbitrary batching
6. is the dependency order realistic given the current code shape
7. does every step or phase have a verification note
8. are any necessary preparatory or cleanup phases missing
9. are any planning issues clearly separated from the checklist and paired with a suggested next action

If the answer to any of these is no, Planning should not advance to Implementation.

---

## Final Summary

Planning is the execution-order phase of the workflow.

It starts only after requirements and architecture have both been approved and promoted into their root contracts.

It consumes:

1. approved requirements to understand what must become true
2. approved architecture to understand the dependency structure
3. target-state outputs to understand what implementation surfaces exist
4. current code shape to understand the safest route through the real codebase

It produces `implementation_plan.md`, which is a scheduling-only artifact that groups work into dependency-ordered phases, preserves requirement and architecture traceability, defines checklist steps with verification notes for implementation, and keeps any planning-discovered issues separate in an issues section at the bottom of the artifact.

Planning is not allowed to invent new design. If Planning discovers a problem, it should first try to complete the scheduling pass, present the plan together with the issue and a suggested next action, and then wait for discussion and developer approval before that issue is resolved, discarded, or turned into requirement or architecture intake.

That is the Planning-phase operating model for the heavy-task workflow as currently defined.
