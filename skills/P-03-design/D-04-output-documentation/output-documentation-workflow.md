# Output Documentation Workflow

## Goal

Use `P-03-design/D-03-output-dry-run-planning/output-dry-run-planning.md` plus approved Design artifacts to write per-file output docs and one root `overview.md` under `P-03-design/D-04-output-documentation/`.

Each file-level output doc should carry the plain-language feature and architectural requirements it serves plus the representative projected code examples that downstream Planning is allowed to quote.

## Procedure

1. read the active D-03 packet and approved Design artifacts
2. read the mapped input docs and corresponding onboarding references named by D-03
3. write one task-local output doc per target file or future target file in the approved slice
4. record the file's feature requirements and architectural requirements with IDs plus short plain-language summaries pulled from approved artifacts
5. add representative projected code examples for each distinct change type in that file or slice, preferring the most complex useful example instead of copy-paste variants
6. keep removed surfaces visible rather than silently dropping them
7. after file-level docs exist, synthesize one root `overview.md` that aggregates file coverage, requirement or architecture summaries, and a representative example index
8. stop and hand off to the phase-level CP3 checkpoint rather than running a package-local step review

## Rules

1. `D-04` writes task-local projected outputs only.
2. Representative code examples belong in `D-04`. Planning may only quote or copy them; it may not invent missing examples.
3. Example coverage should represent distinct change types rather than every repeated callsite or copy-and-paste edit.
4. Sequencing comes from `D-03`, not from a package-local `dry-run-plan.md` sidecar.
5. Review happens through `P-99-review/cp3-design.md`, not per-step review files.