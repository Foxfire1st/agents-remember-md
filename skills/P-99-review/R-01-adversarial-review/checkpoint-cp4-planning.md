# Checkpoint CP4 — Planning Review

> **Phase:** Planning
> **Trigger:** After `P-04-planning` is complete, before Implementation begins.
> **Artifacts to review:** `P-04-planning/P-01-implementation-planning/implementation_plan.md`, `requirements.md`, `architecture.md`, `P-03-design/D-04-output-documentation/overview.md`, relevant projected output documentation files under `P-03-design/D-04-output-documentation/`, and `P-03-design/D-02-architecture-deliberation/architecture-deliberation.md` when dependency rationale needs confirmation.

---

## Purpose

Verify that Planning remains scheduling-only, covers every in-scope output slice, pulls any concrete code examples from `D-04` instead of inventing them, preserves issues instead of silently resolving them, and is ready to hand off to sequential Implementation.

---

## Review Criteria

### 1. Scheduling-only discipline

- [ ] The implementation plan schedules work and does not redefine requirements, architecture, or target-state contracts.
- [ ] Any concrete code examples included in the plan are copied or quoted from `D-04` representative examples rather than invented in Planning.
- [ ] Dependencies, ordering, and phase sequencing are explicit enough for execution.
- [ ] Planning does not smuggle unresolved design decisions back in as schedule details.

### 2. Coverage and traceability

- [ ] Every in-scope output documentation file or slice is scheduled in the plan.
- [ ] Requirement IDs and architecture IDs remain traceable through the plan, with plain-language summaries where the plan presents them.
- [ ] The plan matches the approved rollout order and packet dependencies.

### 3. Example sourcing and selection

- [ ] The chosen examples cover distinct implementation types rather than noisy copy-and-paste variants.
- [ ] Each example identifies its `D-04` source plus the covered requirement and architecture text.
- [ ] If `D-04` lacks needed example coverage, the plan leaves that as a Design gap or issue instead of patching it locally.

### 4. Issue handling

- [ ] Planning issues remain in the issues section until discussion resolves or reroutes them.
- [ ] The plan does not silently promote issues into requirement or architecture changes.
- [ ] Assumptions that could affect implementation are explicit.

### 5. Canonical planning model

- [ ] The plan uses one assembled scheduling artifact and one planning checkpoint.
- [ ] No segment index, per-segment review artifact, or obsolete batch model survives in the plan.
- [ ] The handoff is aligned to sequential `I-01-implementation` execution.

---

## Adversarial Checks

Apply all 5 checks from the Reviewer agent persona:

1. **Cross-reference tracing** — Trace 3 scheduled slices back to the approved Design packet.
2. **Assumption surfacing** — Identify any design or contract assumptions that Planning treats as settled without support.
3. **Counter-scenario generation** — Ask whether the schedule still works if one dependency or issue lands differently.
4. **Scope boundary probing** — Confirm the plan neither drops required slices nor expands implementation scope beyond the approved packet.
5. **Known-unknowns accounting** — Verify open issues and unresolved dependencies stay explicit in the plan.

---

## Verdict Guidance

| Verdict | When to use |
| --- | --- |
| `pass` | The plan is scheduling-only, fully covers the approved packet, keeps issues visible, sources any examples from `D-04`, and is ready for sequential Implementation. |
| `pass-with-concerns` | The plan is mostly sound but has a thin dependency rationale, a minor traceability or example-sourcing gap, or an issue that needs close monitoring in Implementation. |
| `fail` | Planning redefines contracts, invents examples, omits scheduled slices, hides unresolved issues, or preserves obsolete segment or batch-model assumptions. |