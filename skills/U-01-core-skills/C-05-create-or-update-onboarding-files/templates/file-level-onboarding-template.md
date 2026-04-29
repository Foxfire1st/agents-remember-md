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

<Start with a short prose summary if there is meaningful documentation context to explain, then add the citation table. Build this section from the `Domain Documentation` category declared in `<AR_MANAGEMENT_ROOT>/system/sources.md`. Read local mirrors if needed, but link the table row to the canonical online document reference. Investigate and preserve useful explanation already present in this section; correct it if needed rather than deleting it. If nothing relevant exists, keep the table and record what was checked plus `No relevant documentation found.`>

| Finding                                                                | Citations | Source Path                                                |
| ---------------------------------------------------------------------- | --------- | ---------------------------------------------------------- |
| <Concise summary of the cited lines and why they matter to this file.> | L10-L18   | [<doc-title-or-id>](https://example.com/canonical-doc-url) |

## Invariants And Boundaries

<Rules that must continue to hold, ownership boundaries, sequencing constraints, and what this file should not do.>

## Cross-Repo References

<Start with a short prose summary if there is meaningful cross-repo or external-boundary behavior to explain, then add the citation table. Build this section using the `Domain Documentation` category from `<AR_MANAGEMENT_ROOT>/system/sources.md` plus the necessary code and onboarding evidence, not from adjacent onboarding alone. Use workspace-relative links for code or onboarding evidence; never absolute filesystem paths. Investigate and preserve useful explanation already present in this section; correct it if needed rather than deleting it. If nothing relevant exists, keep the table and record what was checked plus `No meaningful cross-repo references found.`>

| Finding                                                                                 | Citations | Source Path                                                                     |
| --------------------------------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------- |
| <Concise summary of the interface, external repo/service involved, and why it matters.> | L42-L58   | [<source-or-adjacent-repo-file>](relative/path/to/source-or-adjacent-repo-file) |

## Update History

<!-- newest first; entries move here from Tasks when the parent task is completed or abandoned -->
