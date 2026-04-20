# Checkpoint CP5 — Implementation Review

> **Phase:** Implementation
> **Trigger:** After `P-05-implementation` is complete, before Closing when the optional implementation checkpoint is invoked.
> **Artifacts to review:** `P-05-implementation/I-01-implementation/implementation_results.md`, `P-04-planning/P-01-implementation-planning/implementation_plan.md`, `requirements.md`, `architecture.md`, `P-03-design/D-04-output-documentation/overview.md`, relevant code diff and verification evidence, and `P-05-implementation/progress.md` when needed for continuity.

---

## Purpose

Verify that implementation honestly reflects sequential plan execution, recorded deviations, verification evidence, and the approved target-state contracts before the task moves into Closing.

---

## Review Criteria

### 1. Plan adherence

- [ ] The implementation followed the sequential plan rather than an obsolete batch or segment model.
- [ ] Every plan step is either completed, explicitly deferred, or intentionally dropped with justification.
- [ ] Deviations from the implementation plan are visible and explained.

### 2. Contract fidelity

- [ ] The code and `implementation_results.md` align with the approved requirements, architecture, and `D-04` output documentation.
- [ ] The implementation does not silently mutate the approved target-state outputs.
- [ ] Any design or contract drift is surfaced explicitly rather than normalized in place.

### 3. Verification and issue handling

- [ ] Verification evidence is recorded honestly for the changed surfaces.
- [ ] Known issues, deferred work, and failure cases are tracked with clear disposition.
- [ ] `progress.md` matches the real implementation state closely enough for Closing handoff.

### 4. Scope and continuity

- [ ] Code changes trace back to approved requirements or explicit issue handling.
- [ ] No major approved scope item disappeared without explanation.
- [ ] The implementation handoff is usable for developer review and Closing without reconstructing the work from scratch.

---

## Adversarial Checks

Apply all 5 checks from the Reviewer agent persona:

1. **Cross-reference tracing** — Trace 3 implemented changes back to the plan and approved contracts.
2. **Assumption surfacing** — Identify runtime or verification assumptions that remain unproven.
3. **Counter-scenario generation** — Ask where the sequential implementation could still fail under realistic edge conditions.
4. **Scope boundary probing** — Check for out-of-scope changes or silently missing approved work.
5. **Known-unknowns accounting** — Confirm unresolved issues and deferred items are explicitly handed forward.

---

## Verdict Guidance

| Verdict | When to use |
| --- | --- |
| `pass` | Implementation followed the approved sequential plan closely, recorded deviations honestly, and has adequate verification for Closing. |
| `pass-with-concerns` | Implementation is substantially aligned but has thin verification, a narrow deviation, or a follow-up item that the developer should review closely. |
| `fail` | Implementation hides deviations, breaks approved contracts, lacks critical verification evidence, or leaves material scope unresolved without disposition. |
