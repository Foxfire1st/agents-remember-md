# Findings Capture Workflow

Use this workflow when durable knowledge emerges after task artifacts already exist, or when a standalone clarification needs a durable home outside chat.

## Goal

Capture a confirmed finding in the correct durable path and, when the finding is a factual current-state clarification, propagate it into onboarding immediately through `C-05-create-or-update-onboarding-files`.

## Caller Responsibilities

The caller should provide enough context to identify:

1. the finding to capture
2. whether an active task exists
3. the affected code or documentation surfaces
4. the evidence already known
5. the current phase when the finding is task-local

## Durable Capture Order

1. verify the finding
2. decide the owning durable target
3. update the task-local owner first when a task exists
4. propagate to onboarding only if the onboarding guardrail passes
5. return a capture summary

## Procedure

### 1. Establish the context

Determine:

1. whether this is inside a task or standalone
2. the current phase or task surface that owns the finding
3. which files, artifacts, or boundaries are affected
4. whether the finding is already supported by code, onboarding, docs, or explicit developer confirmation

### 2. Verify before capture

1. Read the relevant code and onboarding first.
2. If direct reads are not enough and the surfaces are dispersed, perform a broader discovery pass before writing.
3. If the finding remains unclear, ask targeted clarification questions and stop instead of guessing.

### 3. Qualify the finding

A finding is durable enough to capture when it is one or more of:

1. a developer-confirmed clarification
2. a code- or doc-verified correction
3. a newly established invariant, ownership boundary, dependency edge, or migration constraint
4. an explanation-level current-state truth that later work should not need to rediscover from chat

If the finding is still an option, debate, proposal, or unresolved ambiguity, return it as discussion context only and do not write it into durable artifacts.

### 4. Choose the owning durable target

Inside a task, route the finding to the owner that matches its responsibility:

1. `requirement_change_candidates.md` for requirement-facing scope or intent findings
2. `architecture_open_questions.md` for architecture-facing structural findings
3. the active phase artifact for planning-, implementation-, review-, or explanation-local findings
4. a shared overview or phase summary when the truth is cross-file and does not belong to one leaf artifact alone

Do not silently rewrite approved `requirements.md` or `architecture.md` from one finding. Capture the intake or clarification first, then let the normal approval flow decide promotion.

If the expected owning artifact does not exist yet, route the finding to the nearest open intake artifact instead of inventing retired project-documentation layers.

Standalone, use onboarding as the durable destination only when the finding is a factual current-state clarification. Otherwise return the capture summary and ask for explicit task context or destination.

### 5. Apply the onboarding guardrail

Only propagate a finding into onboarding when all of the following are true:

1. it is factual and current-state
2. it has been verified against code and supporting context
3. it is stable enough to survive beyond the current task
4. it is explanatory rather than speculative or planning-owned
5. it is not a future-state decision, open question, or task-only note

If the finding fails any part of this guardrail, keep it in the task-local durable surface only.

### 6. Capture the finding in durable form

When updating task-local artifacts:

1. update the owning artifact directly
2. preserve the evidence basis or confirmation source
3. keep the change local instead of cascading into approved artifacts without review
4. update any shared overview only when the truth is genuinely cross-file

When updating onboarding through `C-05-create-or-update-onboarding-files`:

1. pass the concrete file or repo scope that needs maintenance
2. update only the factual sections affected by the finding
3. preserve the separation between durable onboarding commentary and task-local notes

### 7. Return a capture summary

Report back:

1. what finding was captured
2. where it was written
3. what evidence supported it
4. whether onboarding was updated
5. what follow-up, if any, remains visible for the caller

## Prohibited Patterns

1. do not route findings through retired project-documentation layer names
2. do not use a findings ledger as the primary durable store
3. do not push unresolved discussion into onboarding
4. do not treat one finding as implicit approval of requirement or architecture changes

## Completion Criteria

This workflow is complete when:

1. the confirmed finding has been written to the correct durable owner
2. onboarding was updated only if the guardrail passed
3. no confirmed finding remains only in chat when a durable destination exists
4. the caller receives a clear capture summary
