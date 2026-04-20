# Output Documentation Template

Use this template for one mapped D-04 output-documentation file.

````md
# <TargetFileName.ext>

| Field | Value |
| --- | --- |
| repository | <repo-name> |
| target_path | `<repo-relative target path>` |
| task | `<task-folder-name>` |
| doc_type | `output-documentation` |
| change_status | `<changed-in-place|new-file|moved|removed|reused-unchanged|no-code-changes>` |
| source_reference | `<source path or N/A>` |
| lastUpdated | <YYYY-MM-DDThh:mm> |

## Target Role
## Change Status
## Feature Requirements
| Requirement ID | Requirement Text | File Impact |
| --- | --- | --- |

## Architectural Requirements
| Architecture ID | Architecture Text | File Impact |
| --- | --- | --- |

## Projected Structure Or Content
## Invariants And Preserved Truths
## Migration And Dependency Notes

## Representative Code Examples

### E1 — <title>

Why this changes:
<short explanation>

Distinct change covered: <change type>
Source span or surface: <function/class/flow>

Feature requirements:
- <REQ-ID> — <plain-language requirement summary>

Architectural requirements:
- <ARCH-ID> — <plain-language architecture summary>

```<language>
<projected code excerpt>
```

## Planning And Progress Hooks
## Change Log
````

## Rules

1. One mapped output doc corresponds to one target file or future target file.
2. Removed files stay visible in the mapped output slice.
3. File-level docs must not rely on `dry-run-plan.md`, `symbol-ledger.md`, or per-step review artifacts.
4. Feature and architectural requirement sections should carry IDs plus short plain-language summaries, not IDs alone.
5. Representative code examples must be projected target-state snippets that Planning can quote verbatim or near-verbatim; they are not current-state snapshots.
6. Cover each distinct implementation type once and avoid noisy copy-paste variants; prefer the most complex representative sample when several edits share the same pattern.