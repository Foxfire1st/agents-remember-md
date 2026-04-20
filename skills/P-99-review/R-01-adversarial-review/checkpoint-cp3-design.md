# Checkpoint CP3 — Design Review

> **Phase:** Design
> **Trigger:** After the Design packet is assembled, before Planning begins.
> **Artifacts to review:** `P-03-design/D-01-requirement-clarification/requirement-clarification.md`, `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md`, `P-03-design/D-03-output-dry-run-planning/output-dry-run-planning.md`, `P-03-design/D-04-output-documentation/overview.md`, the in-scope projected outputs under `P-03-design/D-04-output-documentation/`, `requirements.md`, `requirement_change_candidates.md`, and `architecture_open_questions.md`.

---

## Purpose

Verify that the Design phase produced a coherent target-state packet that reflects explicit developer answers, projects the intended output set one-to-one, and is ready to support scheduling-only Planning.

---

## Review Criteria

### 1. Developer-answer integrity

- [ ] `D-01` reflects explicit developer requirement answers rather than inferred approval.
- [ ] `D-02` reflects explicit architectural direction, open tensions, and routing decisions rather than silent closure.
- [ ] Requirement and architecture staging artifacts remain consistent with those Design outputs.

### 2. Output-packet integrity

- [ ] `D-04` covers the intended target-state files one-to-one for the approved packet.
- [ ] Each projected output uses the mapped onboarding references or canon bundles identified by `D-03`.
- [ ] `D-04` includes representative projected code examples for each distinct change type, with the relevant requirement and architecture text carried alongside the IDs.
- [ ] The packet does not treat changed-in-place or moved surfaces as blanket deletion authority for compatible live content.

### 3. Traceability and promotion readiness

- [ ] Projected outputs carry the approved requirement IDs and architecture IDs coherently.
- [ ] `D-03` rollout order matches the approved packet sequence and the task's implementation constraints.
- [ ] Architecture promotion readiness is justified by the projected package, not by abstract deliberation alone.

### 4. Planning readiness

- [ ] The Design packet is concrete enough that Planning can schedule output cutover rather than invent target-state contracts.
- [ ] Any concrete code examples that Planning is expected to show can be pulled directly from `D-04` rather than invented downstream.
- [ ] Any unresolved ambiguity that would block Planning stays explicit in the staging artifacts or the design packet.

---

## Adversarial Checks

Apply all 5 checks from the Reviewer agent persona:

1. **Cross-reference tracing** — Trace 3 projected output slices back through `D-04`, `D-03`, and the relevant Design decisions.
2. **Assumption surfacing** — Identify any place where `D-01` or `D-02` appears to rely on an implied answer.
3. **Counter-scenario generation** — Ask whether a different packet order or output mapping would materially change Planning.
4. **Scope boundary probing** — Check whether the Design packet silently dropped a required surface or widened scope.
5. **Known-unknowns accounting** — Confirm unresolved blockers remain explicit instead of being postponed into Planning by assumption.

---

## Verdict Guidance

| Verdict | When to use |
| --- | --- |
| `pass` | The Design packet reflects explicit developer answers, projects the approved target-state files coherently, and is ready for scheduling-only Planning. |
| `pass-with-concerns` | The packet is broadly sound but one mapping, traceability chain, or promotion rationale needs closer scrutiny during Planning. |
| `fail` | Design relies on inferred approvals, projects the wrong output set, loses requirement or architecture traceability, or leaves Planning to invent unresolved target-state contracts or examples. |
