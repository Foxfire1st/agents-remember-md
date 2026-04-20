# Implementation Planning Workflow

## Goal

Write one scheduling-only `implementation_plan.md` from approved requirements, approved architecture, output documentation, and relevant code.

If the plan includes concrete code examples, pull them from approved D-04 representative examples rather than inventing them in Planning.

## Workflow

1. read the approved target-state and the representative examples available in D-04
2. assemble the in-scope feature requirements and architectural requirements with their plain-language summaries
3. identify the implementation surface
4. group work into phases
5. write checklist steps and substeps by dependency
6. pull the smallest set of representative D-04 examples that covers the distinct planned change types
7. handle planning issues in the plan's issues section
8. keep Planning scheduling-only
9. revise locally when possible

Output path:

`<task-folder>/P-04-planning/P-01-implementation-planning/implementation_plan.md`

## Rules

1. Approved requirements, architecture, and output documentation remain upstream inputs.
2. Every scheduled step should stay traceable to requirement and architecture IDs plus their plain-language summaries.
3. Use the smallest dependency-stable phases that leave the system in a verifiable intermediate state.
4. Concrete code examples in the plan must be copied or quoted from D-04 output docs; if D-04 lacks a needed example, return to Design instead of inventing one in Planning.