---
name: I-01-implementation
description: "Execute the approved implementation plan sequentially from first unchecked step to completion, recording issues without silently reopening design."
---

# I-01 Implementation

Read:

1. `implementation_plan.md`
2. `requirements.md`
3. `architecture.md`
4. `P-03-design/D-04-output-documentation/`
5. relevant code

Write:

`<task-folder>/P-05-implementation/I-01-implementation/implementation_results.md`

## Workflow

1. start from the first unchecked step in the approved plan
2. execute the plan sequentially to completion
3. verify each step before moving to the next one
4. record implementation issues in the plan's issues section
5. stop for discussion when a problem would change approved requirements or architecture
6. summarize completed work and verification in `implementation_results.md`

## Rules

1. One sequential execution pass replaces multi-batch orchestration and concurrent dispatch.
2. Issues require discussion before they become requirement or architecture intake.
3. Implementation stays downstream of approved Design artifacts.
