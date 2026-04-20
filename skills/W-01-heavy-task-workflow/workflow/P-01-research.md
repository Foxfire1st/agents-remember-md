# Research Phase

## Purpose

This document captures what the Research phase is, why it exists as its own phase, how it relates to Creation and Synthesis, and how its internal subphases work.

It is the canonical companion to `overview.md`, `requirements.md`, and `architecture.md` for everything specific to Research.

---

## Core Identity

Research is the first evidence-building phase after Creation.

Its role is different from both neighboring phases:

1. Creation preserves raw intake and refreshes onboarding context.
2. Research tests that intake against onboarding and the real current system.
3. Synthesis later cleans up and frames the unresolved questions that Research exposes.

Research starts the repeatable requirement-promotion cycle and the early architecture-scoping loop.

It does two things at once:

1. turns raw requirement language into normalized requirement candidates that can be reasoned about coherently
2. freezes current-state truth in task-local input documentation so later phases stop guessing

---

## What Research Is Not

Research is not:

1. just one more onboarding refresh pass
2. a phase where requirements are approved or promoted
3. a phase where architecture direction is finalized
4. a substitute for Synthesis framing
5. a place where projected target-state output documentation is written
6. a phase that produces a separate research summary artifact outside `P-01-research/R-02-input-documentation/`

---

## Internal Structure of the Research Phase

Research has two ordered skills:

1. `R-01-requirements-normalization`
2. `R-02-input-documentation`

This order is a rule, not a preference.

### R-01 Requirements Normalization

`R-01-requirements-normalization` converts raw requirement intake into a clearer normalized requirement candidate set.

It draws from:

1. raw intake in `requirement_change_candidates.md`
2. current onboarding
3. relevant current code
4. already-approved requirements when overlap must be checked

It writes back into `requirement_change_candidates.md` and moves items from raw-intake into the normalized list.

### R-02 Input Documentation

`R-02-input-documentation` documents the relevant current-state code surfaces for the task.

It draws from:

1. normalized requirement candidates
2. onboarding context
3. the relevant current code
4. existing approved requirements when overlap matters

It writes:

1. per-file or per-surface input documentation under `P-01-research/R-02-input-documentation/`
2. `P-01-research/R-02-input-documentation/overview.md`

---

## Relationship to Root Artifacts

Research writes and updates several task-local and cross-phase artifacts, but it does not own the approved contracts.

### `requirement_change_candidates.md`

Research moves raw-intake requirement entries into normalized candidates through `R-01-requirements-normalization`.

### `architecture_open_questions.md`

Research records architectural hotspots, rough direction, and technical evidence gaps here.

It does not store requirement questions in this file.

### `requirements.md` and `architecture.md`

Research may read approved requirements for overlap context, but it does not approve or promote anything into `requirements.md` or `architecture.md`.

---

## Research Handoff to Synthesis

Research hands off through one artifact family:

1. `P-01-research/R-02-input-documentation/overview.md`
2. the companion input documentation files under `P-01-research/R-02-input-documentation/`

There is no separate `research.md`, `research-summary.md`, or checkpoint snapshot artifact for the handoff itself.

---

## Relationship to the Research Checkpoint

The Research checkpoint is `P-99-review/cp1-research.md`.

It is review-only.

It does not generate new working artifacts.

`R-01-adversarial-review` reviews the completed Research outputs together with the current state of the staging artifacts, followed by human review. The checkpoint then either approves forward movement into Synthesis or sends the workflow back for correction.