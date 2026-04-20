# Implementation Plan Template

This template defines one canonical `implementation_plan.md`.

Keep phases dependency-ordered, keep issues separate from checklist steps, and pull any concrete code examples from the approved D-04 output docs rather than inventing them in Planning.

````markdown
# Implementation Plan

| Field | Value |
| --- | --- |
| task | `<task-folder-name>` |
| doc_type | `implementation-plan` |
| lastUpdated | <YYYY-MM-DDThh:mm> |

## Preconditions

- approved requirements exist
- approved architecture exists
- `P-03-design/D-04-output-documentation/` is available

## Scope Summary

## Feature Requirements

| Requirement ID | Requirement Text | Output Sources |
| --- | --- | --- |
| <REQ-ID> | <plain-language requirement summary> | `<D-04 path>` |

## Architectural Requirements

| Architecture ID | Architecture Text | Output Sources |
| --- | --- | --- |
| <ARCH-ID> | <plain-language architecture summary> | `<D-04 path>` |

## Output Documentation Coverage Summary

| Output slice | Planned step or phase |
| --- | --- |
| `<D-04 path or slice>` | <S# or Phase #> |

## Dependency Notes

1. <dependency note>

## Implementation Steps

### S1 — <title>

Objective: <what this step or phase accomplishes>

Dependency rationale: <why it must happen here>

- [ ] <step>
  - [ ] <substep>
  - [ ] <substep>
  Output documentation: `<D-04 path or slice>`
  Feature requirements:
  - `<REQ-ID>` — <plain-language requirement summary>
  Architectural requirements:
  - `<ARCH-ID>` — <plain-language architecture summary>
  Verification: <note>

## Concrete Code Examples From Output Documentation

### E1 — <title>

Source output doc: `<D-04 path>#<local example label if used>`

Distinct change covered: <type of implementation change>

Why this example is included: <why this is the representative example to carry into the plan>

Feature requirements:
- `<REQ-ID>` — <plain-language requirement summary>

Architectural requirements:
- `<ARCH-ID>` — <plain-language architecture summary>

```<language>
<code excerpt copied or quoted from the approved D-04 output doc>
```

## Planning Risks And Sequencing Assumptions

1. <risk or assumption>

## Deferred Items

1. None.

## Issues

- None.

## Suggested Next Action And Approval Hold

Suggested next action: <what should happen next>

Approval hold: <what approval or checkpoint still applies>
````

Use the smallest set of representative examples that covers each distinct implementation type. Do not repeat copy-and-paste variants.