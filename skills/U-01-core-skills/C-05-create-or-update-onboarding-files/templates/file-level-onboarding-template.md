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

<Start with a short prose summary if there is meaningful documentation context to explain, then add the citation table. Investigate and preserve useful explanation already present in this section; correct it if needed rather than deleting it. If nothing relevant exists, keep the table and record what was checked plus `No relevant documentation found.`>

| Source Path                                                    | Citations | Finding                                                                |
| -------------------------------------------------------------- | --------- | ---------------------------------------------------------------------- |
| [<path/to/doc-or-onboarding.md>](path/to/doc-or-onboarding.md) | L10-L18   | <Concise summary of the cited lines and why they matter to this file.> |

## Invariants And Boundaries

<Rules that must continue to hold, ownership boundaries, sequencing constraints, and what this file should not do.>

## Cross-Repo References

<Start with a short prose summary if there is meaningful cross-repo or external-boundary behavior to explain, then add the citation table. Investigate and preserve useful explanation already present in this section; correct it if needed rather than deleting it. If nothing relevant exists, keep the table and record what was checked plus `No meaningful cross-repo references found.`>

| Source Path                                                                    | Citations | Finding                                                                                 |
| ------------------------------------------------------------------------------ | --------- | --------------------------------------------------------------------------------------- |
| [<path/to/source-or-adjacent-repo-file>](path/to/source-or-adjacent-repo-file) | L42-L58   | <Concise summary of the interface, external repo/service involved, and why it matters.> |

## Update History

<!-- newest first; entries move here from Tasks when the parent task is completed or abandoned -->
