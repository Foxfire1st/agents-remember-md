# Synthesis Phase

## Purpose

This document captures what the Synthesis phase is, why it exists as its own phase, how it relates to Research and Design, and how its internal subphases work.

It is the canonical companion to `overview.md` and `requirements.md` for everything specific to Synthesis.

---

## Core Identity

Synthesis is a distinct phase between Research and Design.

Its role is different from both:

1. Research discovers facts, contradictions, blind spots, and unresolved questions.
2. Synthesis consolidates, classifies, deduplicates, orders, and frames those questions.
3. Design later works through those framed questions with the developer rather than letting the agent answer them on its own.

---

## Requirement Questions vs Architecture Questions

The two framing subphases work on two different kinds of question.

1. Requirement questions ask what should be true by the end of the task.
2. Architecture questions ask how that truth is carried technically.
3. Evidence questions ask what is actually the case in the current system.

This distinction is why Synthesis is split into two subphases.

---

## Internal Structure of the Synthesis Phase

Synthesis has two ordered subphases:

1. `S-01-requirement-question-framing`
2. `S-02-architecture-question-framing`

This split keeps a clean identity for Synthesis while preserving the dependency between requirement-level uncertainty and design-level uncertainty.

### S-01 Requirement Question Framing

This subphase takes the contract-affecting questions surfaced by Research and turns them into a clean, prioritized, reviewable set.

It draws from:

1. `requirements.md`
2. `requirement_change_candidates.md`
3. `P-01-research/R-02-input-documentation/overview.md`
4. the companion input documentation files

It writes one artifact:

`P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`

### S-02 Architecture Question Framing

This subphase takes the post-S-01 state and frames the architecture-facing questions that architecture deliberation will work through.

It draws from:

1. `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`
2. `architecture_open_questions.md`
3. `requirements.md`
4. `P-01-research/R-02-input-documentation/overview.md` and the companion input documentation files

It writes one artifact:

`P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`

---

## Synthesis Handoff

The two framing files together are the synthesis handoff into Design:

1. `requirement-question-framing.md` feeds `D-01-requirement-clarification`.
2. `architecture-question-framing.md` feeds `D-02-architecture-deliberation`.

There is no retired monolithic synthesis handoff artifact. Any reference to a single synthesis file outside the two framing outputs is drift.

---

## Relationship to the Global Backlog

Synthesis does not replace the always-live backlog `architecture_open_questions.md`.

The architecture backlog feeds `architecture-question-framing.md`. It does not replace it, and it continues to live across phases.

---

## Relationship to the Synthesis Checkpoint

The Synthesis checkpoint is `P-99-review/cp2-synthesis.md`.

It is review-only and does not generate new working artifacts.

`R-01-adversarial-review` reviews the two framing files plus the updated staging state, followed by human review. The disposition is recorded in `cp2-synthesis.md`.