# Checkpoint CP2 — Synthesis Review

> **Phase:** Synthesis
> **Trigger:** After `P-02-synthesis` is complete, before Design questioning begins.
> **Artifacts to review:** `P-01-research/R-02-input-documentation/overview.md`, `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md`, `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`, `requirement_change_candidates.md`, `requirements.md`, and `architecture_open_questions.md`.

---

## Purpose

Verify that Synthesis framed requirement-facing and architecture-facing questions from the Research packet without collapsing into design answers or replacing the root staging artifacts.

---

## Review Criteria

### 1. Question framing quality

- [ ] `S-01` frames requirement questions instead of answering them.
- [ ] `S-02` frames architecture questions, dependencies, and decision pressure instead of assigning final direction.
- [ ] Each question is specific enough to support one-by-one developer discussion in D-01 or D-02.

### 2. Separation of concerns

- [ ] Requirement-facing and architecture-facing questions remain split into their canonical files.
- [ ] Synthesis does not redefine approved requirements or promote architecture conclusions on its own.
- [ ] The framing layer stays between Research and Design rather than acting as a hidden design pass.

### 3. Continuity with Research

- [ ] The framing files stay grounded in the Research input docs and visible evidence state.
- [ ] Relevant onboarding references, code, documentation and current-state boundaries remain visible where they constrain the framed questions.
- [ ] No major research finding disappears without an explicit reason.

### 4. Staging continuity

- [ ] `requirement_change_candidates.md` still reflects unresolved requirement pressure accurately.
- [ ] `requirements.md` only contains developer-approved requirements rather than speculative synthesis output.
- [ ] `architecture_open_questions.md` remains the staging home for unresolved architecture pressure.

---

## Adversarial Checks

Apply all 5 checks from the Reviewer agent persona:

1. **Cross-reference tracing** — Trace 3 framed questions back to the Research packet and staging artifacts.
2. **Assumption surfacing** — Identify questions that quietly assume a design direction.
3. **Counter-scenario generation** — Ask whether the same research could support a materially different framing and whether that tension stays visible.
4. **Scope boundary probing** — Confirm Synthesis does not slip into design or silently narrow the task.
5. **Known-unknowns accounting** — Verify unresolved evidence gaps stay explicit rather than being treated as settled.

---

## Verdict Guidance

| Verdict              | When to use                                                                                                                                                                                        |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pass`               | Synthesis frames the right questions, preserves continuity with Research, and leaves D-01 and D-02 ready for explicit developer answers.                                                           |
| `pass-with-concerns` | The framing is usable but one question set is thin, slightly solution-shaped, or weakly tied back to Research.                                                                                     |
| `fail`               | Synthesis answers its own questions, merges requirement and architecture framing incorrectly, drops material research continuity, or leaves Design without a clear one-question-at-a-time handoff. |
