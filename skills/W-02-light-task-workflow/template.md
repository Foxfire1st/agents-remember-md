# Light Task Template

Use this template for any task file created by `W-02-light-task-workflow`.

Implementation sections use checkbox-based steps and checkbox substeps. The checklist is the live execution state during implementation and review.

```markdown
# Task: <Title>

**Status:** planning
**Repo:** <primary repo>
**Type:** <Docs | Skill | Config | Other>
**Created:** <YYYY-MM-DD>

---

## Objective

<What is changing and why. Keep this brief and concrete.>

---

## Requirements

- <requirement>
- <requirement>

---

## Implementation Steps

### S1 — <title>

- [ ] <step outcome>
  - [ ] <substep>
  - [ ] <substep>
- [ ] <verification or review-ready outcome>

### S2 — <title>

- [ ] <step outcome>
  - [ ] <substep>
  - [ ] <substep>
- [ ] <verification or review-ready outcome>

---

## Proposed Code Examples

### E1 — <title>

Distinct change covered: <what kind of implementation change this example represents>

Why this example is included: <why this is the representative example the developer should review>

```<language>
<example snippet>
```

### E2 — <title or "Not needed for this task">

Distinct change covered: <second distinct change type, or explain why no further code examples are needed>

Why this example is included: <reason>

```<language>
<example snippet or short note>
```

---

## Decision Log

| Date | Decision | Rationale |
| --- | --- | --- |
| <YYYY-MM-DD> | <what was decided> | <why> |

---

## Open Questions

- None.

---

## References

- <related file, ticket, or discussion>
```

## Usage Rules

1. Keep the section structure even for small tasks.
2. Use configured management-root paths such as `<task-root>/`, `<onboarding-root>/`, and `<AR_MANAGEMENT_ROOT>/docs/`.
3. When code changes are in scope, include proposed code examples for each distinct implementation change.
4. For documentation-only or other non-code tasks, keep the section and state that no code examples are needed.
5. Mark substeps complete before their parent verification item.
6. Add or reorder checklist items when scope changes, then get approval again if the change is significant.
7. Use the light-task status values: `planning`, `inProgress`, `Completed`.