# Closure Phase

## Purpose

This document defines how the heavy-task workflow closes a task after implementation approval.

Closure happens after implementation is approved, not merely after coding stops.

Its main job in the MVP model is to bring onboarding documentation up to date through `C-05-create-or-update-onboarding-files` from the final implemented state and then write the final report.

Closure is a consolidation pass, not a license to leave all durable discoveries undocumented until the very end.

---

## Core Identity

Closure is the final consistency pass.

It uses:

1. approved requirements
2. approved architecture
3. output documentation
4. actual code
5. implementation results

to update onboarding documentation through `C-05-create-or-update-onboarding-files` so the documented state matches the implemented state.

Closure does not reopen design or implementation silently. If it discovers a material mismatch, it stops and sends the workflow back to the right earlier phase.

---

## Inputs And Preconditions

Closure begins only after:

1. implementation has been approved
2. `implementation_results.md` exists
3. the approved contracts still live in `requirements.md` and `architecture.md`

Closure should read:

1. `requirements.md`
2. `architecture.md`
3. `P-03-design/D-04-output-documentation/`
4. the actual implemented code
5. `implementation_results.md`
6. the relevant onboarding documentation

---

## Workflow

### 1. Confirm implementation approval

Closure starts only after implementation approval.

### 2. Gather the final source of truth

Closure gathers the final source of truth from approved requirements, approved architecture, target-state output documentation, actual code, and implementation results.

### 3. Update onboarding documentation

Closure updates onboarding documentation for the implemented surfaces through `C-05-create-or-update-onboarding-files`.

If an onboarding document is missing for an implemented surface, Closure should create it through `C-05-create-or-update-onboarding-files` rather than leaving the surface undocumented.

Closure should also confirm that durable findings discovered during implementation were already captured through `C-05-create-or-update-onboarding-files` when practical, and then use this phase to consolidate or finish any remaining onboarding wording rather than rediscovering behavior from scratch.

### 4. Stop on mismatch instead of silently closing

If the onboarding refresh reveals a material mismatch between approved target-state and actual code, approved architecture and actual ownership, or approved requirements and actual implemented behavior, Closure must stop and send the workflow back to the appropriate earlier discussion.

### 5. Write `final_report.md`

After onboarding is updated through `C-05-create-or-update-onboarding-files`, Closure writes `final_report.md` summarizing what was learned, approved, built, and updated in onboarding.

### 6. Mark the task complete

After onboarding and `final_report.md` are complete, the orchestrator marks the task closed.

Closure has no checkpoint review for MVP.

---

## Non-Goals

Closure does not:

1. continue implementation work
2. silently rewrite approved contracts
3. ignore mismatches between docs and code
4. skip onboarding refresh just because output documentation already exists

---

## Exit Rule

Closure ends only when:

1. onboarding documentation has been updated through `C-05-create-or-update-onboarding-files` from the final implemented state
2. durable findings from the task have been preserved through `C-05-create-or-update-onboarding-files` rather than left only in chat history
3. `final_report.md` exists
4. the task has been marked complete
