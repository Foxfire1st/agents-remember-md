# <repo> — Onboarding Overview

> **Status:** <bootstrap status>

Use this template for `onboarding/<repo>/overview.md`.

## What This Repo Is

<Purpose, deployment model, core responsibilities, and the technologies that define the repo.>

## Architecture at a Glance

```text
<ASCII diagram showing the major components and how they interact>
```

## Code Structure

| Area   | Path                                         | Tech   | Purpose                             |
| ------ | -------------------------------------------- | ------ | ----------------------------------- |
| <area> | [<path/from/repo-root>](path/from/repo-root) | <tech> | <what lives here and why it exists> |

## Functional Areas

### <Area Name>

<Short, high-signal summary of the area and how it fits into the repo.>

## Cross-Repo References

<Start with prose that explains the important cross-repo or cross-boundary behavior in this repo. Then add the citation table to back that explanation up. Preserve and correct useful existing explanation rather than replacing it with the table. If nothing relevant exists, keep the table and record what was checked plus `No relevant cross-repo evidence found.`>

| Source Path                                                          | Citations | Finding                                                                                                 |
| -------------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------- |
| [<path/to/source-or-onboarding.md>](path/to/source-or-onboarding.md) | L10-L18   | <Concise summary of the cross-repo tie, interface, or service boundary established by the cited lines.> |

## Build & Dev

- <build command>
- <run command>
- <test command>

## Key Invariants

- <repo-wide invariant>
- <repo-wide invariant>

## Glossary Terms

| Term   | Meaning      | Notes                      |
| ------ | ------------ | -------------------------- |
| <term> | <definition> | <optional scope or nuance> |

## Docs References

<Start with prose that explains the documentation context that matters for understanding this repo. Then add the citation table to back that explanation up. Preserve and correct useful existing explanation rather than replacing it with the table. If nothing relevant exists, keep the table and record what was checked plus `No relevant documentation found.`>

| Source Path                                                    | Citations | Finding                                                                               |
| -------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------- |
| [<path/to/doc-or-onboarding.md>](path/to/doc-or-onboarding.md) | L20-L33   | <Concise summary of the cited lines and why they matter for understanding this repo.> |

## What to Explore Next

| Priority | Area / Path                                            | Why Next                           |
| -------- | ------------------------------------------------------ | ---------------------------------- |
| high     | [<component overview path>](<component overview path>) | <why this should be deepened next> |

## Needs Verification

- <Any unresolved or low-confidence findings that should not be stated as settled fact>

## Last Verified

<Verification status or commit/date note>

## Notes

- New overviews should use the canonical headings `## Cross-Repo References` and `## Docs References`.
- These sections are explanation-first surfaces backed by citation tables, not table-only replacements for narrative context.
- `Source Path` must be the clickable link to the cited file.
- `Citations` should use exact line ranges like `L10-L18` or `L10-L18; L42-L47`.
- `Finding` should be a concise summary of what the cited lines establish.
