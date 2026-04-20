# Input Documentation Template

Use this template for one mapped input-documentation file. Input documentation is a task-local current-state impact map for an existing file pair (code file and its onboarding file) that is in scope for the project slice.

This is not a clone of onboarding and it is not an implementation plan. Its purpose is to explain why the file matters for the task, what current behavior is load-bearing, where the requirements press on it, what is likely to change, and what may break if the task is handled naively.

```markdown
# <SourceFileName.ext>

| Field                  | Value                                                                              |
| ---------------------- | ---------------------------------------------------------------------------------- |
| repository             | <repo-name>                                                                        |
| source_path            | `<repo-relative path to existing code file>`                                       |
| onboarding_path        | `<repo-relative path to existing matching code onboarding file>`                   |
| other_onboarding_files | `<repo-relative paths to any other onboarding files that explain related context>` |

|
| task | `<task-folder-name>` |
| doc_type | `input-documentation` |
| change_expectation | `<change-in-place                                           | move | split | delete | replace | adjacent-only | unclear>` |
| lastUpdated | <YYYY-MM-DDThh:mm> |
| code_file_git_last_commit_hash | <commit hash of the code file at the time of documentation> |
| code_file_git_last_updated | <git last updated timestamp for the code file> |

## Scope In This Task

<Why this file is part of the project slice. Name the surface this file represents and how it participates in the task boundary.>

## Current Relevant Logic

<Describe only the current behavior that matters for this task. Focus on the logic, state, interfaces, or control flow that later phases must understand. Do not speculate here about potential changes. That's what likely change surface is for.>

## Invariants And Boundaries

<Document rules, authority lines, ordering constraints, ownership boundaries, or contractual truths that should not be broken accidentally.>

## Requirement Pressure

<List the normalized/approved requirements including id and later discussion outcomes from requirement elicitation as table that press on this file directly or indirectly.>

## Likely Change Surface

<Explain what is likely to change here or around this file based on your understanding of the requirements: modified behavior, moved responsibility, deleted logic, split structure, adjacent adoption, or no direct change.>

## Breakage Risk And Adjacent Impact

<Call out hidden breakage risk, impacted neighbors, or behavior that could regress even if the requirement does not mention it explicitly.>

## Evidence Basis

<List the evidence used to justify this assessment: onboarding files, code reads, docs, Confluence pages, prior task artifacts, developer discussion.>

## Change Log

- <YYYY-MM-DDThh:mm> — <what changed in this documentation and why>
```

## Important Notes

- One mapped input-documentation file corresponds to one existing code file and its onboarding file.
- Input documentation does not copy the code file or its onboarding file. It explains the current state that matters for the task.
- If the code file appears to stay unchanged but remains load-bearing, keep it in scope and say so in `Likely Change Surface`.
- Use overview files for cross-file flows and slice-level concerns that do not belong in a single file doc.
- Overview files have to list for each file level onboarding the requirements that have been mentioned in them for quick reference purposes.
