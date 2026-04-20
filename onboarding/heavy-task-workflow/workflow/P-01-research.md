# Research Phase

## Purpose

This document captures what the Research phase is, why it exists as its own phase, how it relates to Creation and Synthesis, and how its internal subphases work.

It is the canonical companion to `overview.md`, `requirements.md`, and `architecture.md` for everything specific to Phase 1 Research.

---

## Core Identity

Research is the first evidence-building phase after Creation.

Its role is different from both neighboring phases:

1. Creation preserves raw intake and refreshes onboarding context.
2. Research tests that intake against onboarding and the real current system.
3. Synthesis later cleans up and frames the unresolved questions that Research exposes.

Research starts the repeatable requirement-promotion cycle and the early architecture-scoping loop.

It does two things at once:

1. it turns raw requirement language into normalized requirement candidates that can be reasoned about coherently
2. it freezes current-state truth in task-local input documentation so later phases stop guessing

Research therefore creates the shared evidence base used by both the requirement lane and the architecture lane.

---

## Why Research Exists

Creation is intentionally preservation-heavy.

After Creation, the workflow has:

1. raw requirement intake
2. refreshed onboarding context
3. an initialized task structure
4. rough architectural intake if any exists

What it does not have yet is verified understanding of the current system.

That is why Research exists.

Research compares the developer ask against onboarding and code, exposes contradictions, maps the relevant current-state surfaces, and makes scope, risk, and likely change volume explicit before Synthesis and Design begin working from that material.

Without Research, the workflow would harden questions from incomplete evidence and would repeatedly rediscover the same current-state facts later.

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

Research begins only after onboarding is complete enough for investigation to start.

It ends before Synthesis and is followed by the explicit Research checkpoint `P-99-review/cp1-research.md`.

Its work is distinct:

1. Creation preserves and prepares
2. Research investigates and documents
3. Synthesis classifies and frames

---

## What Research Is Not

Research is not:

1. just one more onboarding refresh pass
2. a phase where requirements are approved or promoted
3. a phase where architecture direction is finalized
4. a substitute for Synthesis framing
5. a place where projected target-state output documentation is written
6. a phase that produces a separate research summary artifact outside `P-01-research/R-02-input-documentation/`

Research discovers and documents. It does not approve, promote, or project target-state.

---

## Internal Structure of the Research Phase

Research has two ordered skills:

1. `R-01-requirements-normalization`
2. `R-02-input-documentation`

This order is a rule, not a preference.

### Why this order matters

Input documentation needs a stable normalized requirement view before it can annotate current-state surfaces cleanly.

If Research skips normalization and jumps straight into current-state documentation, it tends to document against unstable or overlapping requirement language, which later makes Synthesis noisier and Design less reliable.

So Research first stabilizes the requirement-side vocabulary, then maps current-state truth against that vocabulary.

---

## R-01 Requirements Normalization

### Purpose

`R-01-requirements-normalization` converts raw requirement intake into a clearer normalized requirement candidate set.

It is the first operational step in the repeatable requirement-promotion pipeline.

### Inputs

`R-01` draws from:

1. raw intake in `requirement_change_candidates.md`
2. current onboarding
3. relevant current code
4. already-approved requirements when overlap must be checked

### Output

`R-01` writes back into `requirement_change_candidates.md`:

1. normalized requirement candidates
2. clearer requirement language for downstream research and design

The orchestrator moves items from raw-intake into the normalized list as part of this step.

### Exit condition

The requirement side of the task is clean enough that `R-02-input-documentation` can investigate the current system against stable normalized candidates instead of raw phrasing noise.

---

## R-02 Input Documentation

### Purpose

`R-02-input-documentation` documents the relevant current-state code surfaces for the task.

It turns normalized requirements plus onboarding and code reads into the present-state evidence package that later phases use to reason about scope, risk, likely blast radius, and likely technical direction.

### Inputs

`R-02` draws from:

1. normalized requirement candidates
2. onboarding context
3. the relevant current code
4. existing approved requirements when overlap matters

### Outputs

`R-02` writes:

1. per-file or per-surface input documentation under `P-01-research/R-02-input-documentation/`
2. `P-01-research/R-02-input-documentation/overview.md`

### Exit condition

The workflow has a durable task-local current-state evidence layer that later phases can cite directly instead of re-deriving current behavior from memory or chat.

---

## How Research Works the Two Lanes

Research is where the tandem-lane model first becomes concrete.

Requirement-side work and architecture-side work both depend on Research, but they do not produce the same outputs.

The split is:

1. requirement-side evidence stays attached to normalized requirement candidates and Research input documentation
2. technical evidence gaps and architectural hotspots are recorded into `architecture_open_questions.md`

Research therefore does not force everything into one artifact.

It uses:

1. `requirement_change_candidates.md` for contract-side staging
2. `P-01-research/R-02-input-documentation/` for current-state evidence
3. `architecture_open_questions.md` for architecture-side unresolved technical work

This is what allows Synthesis later to frame requirement questions and architecture questions separately.

---

## Relationship to Root Artifacts

Research writes and updates several task-local and cross-phase artifacts, but it does not own the approved contracts.

### `requirement_change_candidates.md`

Research moves raw-intake requirement entries into normalized candidates through `R-01-requirements-normalization`.

Evidence-pending candidates may remain staged there for later batched Research re-entry.

### `architecture_open_questions.md`

Research records architectural hotspots, rough direction, and technical evidence gaps here.

It does not store requirement questions in this file.

### `requirements.md`

Research may read already-approved requirements for overlap context, but it does not approve or promote anything into `requirements.md`.

### `architecture.md`

Research does not write the approved architecture contract and does not assign canonical architecture IDs.

### `task.md` and `progress.md`

The orchestrator records phase progress and checkpoint state, but the Research handoff itself lives in `P-01-research/R-02-input-documentation/` rather than in a separate summary artifact.

---

## Research Handoff to Synthesis

Research hands off through one artifact family:

1. `P-01-research/R-02-input-documentation/overview.md`
2. the companion input documentation files under `P-01-research/R-02-input-documentation/`

There is no separate `research.md`, `research-summary.md`, or checkpoint snapshot artifact for the handoff itself.

Synthesis consumes those files directly.

That means Research must leave them in a state that Synthesis can use without reopening broad discovery.

---

## Relationship to the Research Checkpoint

The Research checkpoint is `P-99-review/cp1-research.md`.

It is review-only.

It does not generate new working artifacts.

`R-01-adversarial-review` reviews the completed Research outputs together with the current state of the staging artifacts, followed by human review. The checkpoint then either approves forward movement into Synthesis or sends the workflow back for correction.

---

## Re-entry into Research Later in the Workflow

Research is not only a first-pass phase. The workflow can re-enter it later when the failure is evidence or contract understanding rather than purely technical design adjustment.

Typical re-entry cases include:

1. evidence-pending requirement candidates accumulated in `requirement_change_candidates.md`
2. a later discovery that current-state understanding in the input layer was wrong or incomplete
3. review feedback showing that requirements were misunderstood rather than merely designed badly

Research is not the default correction loop for purely technical design feedback.

If the issue is still purely technical, the workflow normally routes back to `D-02-architecture-deliberation` rather than all the way back to Research.

---

## Exit Condition

Research is ready to hand off when all of the following are true:

1. raw requirement intake relevant to this pass has been normalized
2. the relevant current-state surfaces have been documented in `P-01-research/R-02-input-documentation/`
3. normalized requirement IDs appear in the relevant input documentation files and the Research overview
4. architectural hotspots and technical evidence gaps discovered during Research have been recorded in `architecture_open_questions.md`
5. the resulting evidence layer is coherent enough for Synthesis to frame questions without reopening broad discovery immediately

At that point, the workflow is ready for `P-99-review/cp1-research.md` and, if approved, for Synthesis.
