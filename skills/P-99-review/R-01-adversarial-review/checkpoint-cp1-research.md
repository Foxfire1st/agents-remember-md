# Checkpoint CP1 — Research Review

> **Phase:** Research
> **Trigger:** After `P-01-research` is complete, before Synthesis begins.
> **Artifacts to review:** `task.md`, `P-01-research/R-02-input-documentation/overview.md`, the in-scope input-doc packet under `P-01-research/R-02-input-documentation/`, `requirement_change_candidates.md`, and `architecture_open_questions.md` when Research surfaced architecture pressure.

---

## Purpose

Verify that Research produced a coherent present-state input-documentation layer and routed newly surfaced requirement or architecture pressure into the correct canonical staging artifacts.

---

## Review Criteria

### 1. Present-state discipline

- [ ] Research stays current-state only and does not drift into target-state design.
- [ ] Input documentation describes the live seams, responsibilities, and evidence gaps in present tense.
- [ ] Any canon or onboarding gaps remain explicit instead of being treated as silently resolved.

### 2. Input-documentation quality

- [ ] The packet under `P-01-research/R-02-input-documentation/` is complete enough for Synthesis to frame questions without reopening basic discovery.
- [ ] In-scope input docs carry normalized requirement IDs when requirement pressure was found.
- [ ] Onboarding references, linked docs, or explicit canon-gap notes are preserved for the reviewed surfaces.
- [ ] Evidence is concrete enough to support downstream work without guessing about the current state.

### 3. Staging-artifact routing

- [ ] Requirement-facing findings were routed into `requirement_change_candidates.md`.
- [ ] Architecture-facing pressure was routed into `architecture_open_questions.md` instead of being promoted prematurely.
- [ ] `task.md` still reflects the approved task scope and continuity context.

### 4. Scope and continuity

- [ ] The research packet covers the in-scope surfaces identified by `task.md` or explicitly documents exclusions.
- [ ] Terminology is consistent across `task.md`, the input docs, and staging artifacts.
- [ ] The packet preserves enough continuity that Synthesis can ask sharp questions one by one rather than rediscovering the current state.

---

## Adversarial Checks

Apply all 5 checks from the Reviewer agent persona:

1. **Cross-reference tracing** — Pick 3 current-state claims from the input docs and verify the cited evidence.
2. **Assumption surfacing** — Identify present-state assumptions that are not yet evidenced.
3. **Counter-scenario generation** — Ask whether an adjacent live surface could invalidate the packet's framing.
4. **Scope boundary probing** — Check whether any in-scope seam was silently dropped from `R-02-input-documentation`.
5. **Known-unknowns accounting** — Confirm missing evidence and canon gaps are explicit with downstream impact.

---

## Verdict Guidance

| Verdict | When to use |
| --- | --- |
| `pass` | Research produced a present-state-only packet, routed staging pressure correctly, and left Synthesis with a reliable current-state foundation. |
| `pass-with-concerns` | The packet is usable but has thin evidence, weak onboarding linkage, or a small staging inconsistency that should stay visible in Synthesis. |
| `fail` | Research projects target-state behavior, omits required input docs or staging updates, hides canon gaps, or leaves the current-state packet too weak for Synthesis. |
