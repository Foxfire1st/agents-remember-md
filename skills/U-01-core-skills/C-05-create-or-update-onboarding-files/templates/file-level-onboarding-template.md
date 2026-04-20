# <SourceFileName.ext>

| Field                  | Value                                 |
| ---------------------- | ------------------------------------- |
| repository             | <repo-name>                           |
| path                   | `<repo-relative path to source file>` |
| doc_type               | `file-level-onboarding`               |
| lastUpdated            | <YYYY-MM-DDThh:mm>                    |
| lastVerifiedCommitHash | `<full 40-char SHA>`                  |
| lastVerifiedCommitDate | <YYYY-MM-DDThh:mm>                    |

## Purpose

<What this source file is responsible for and why it matters.>

## Code Commentary

### Logic

<What the code does. Key functions or methods, data flow, notable branching, and the parts that are easiest to misunderstand.>

### Conventions

<Patterns, naming conventions, or local style choices specific to this file or area.>

### Todos

<Known issues or technical debt that are not tied to one active task file.>

### Docs References

<Reference docs, specs, or adjacent onboarding that explain the domain implemented by this file.>

## Invariants And Boundaries

<Rules that must continue to hold, ownership boundaries, sequencing constraints, and what this file should not do.>

## Cross-Repo References

<Interfaces to or from other repos, external services, or cross-layer contracts. State `None.` when there are no meaningful cross-repo references.>

## Tasks

> **Task** — `<what part of this file is affected>`
>
> - **Goal:** <migration|deprecation|bugfix|feature>
> - **Status:** <planned|in-progress|readyForReview|needsFixes|completed>
> - **Description:** <what will change and why>
> - **Progress:** <current state, blockers>
> - **Taskfile:** tasks/<task-file>.md (<task-id>)

## Update History

<!-- newest first; entries move here from Tasks when the parent task is completed or abandoned -->
