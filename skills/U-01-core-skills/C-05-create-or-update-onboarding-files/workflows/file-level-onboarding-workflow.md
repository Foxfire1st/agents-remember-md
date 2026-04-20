# File-Level Onboarding Workflow

Use this workflow when creating or maintaining onboarding for one concrete source file mirrored under `onboarding/<repo>/<component>/src/...`.

Template: `../templates/file-level-onboarding-template.md`

## Goal

Create or update one onboarding markdown file mirrored under `onboarding/<repo>/...` for one concrete source file.

## Scope

1. one onboarding markdown file per source file
2. strict mirrored path under the component `src/` subtree
3. durable commentary only; planning stays in task artifacts

## Placement Rules

```text
onboarding/
  <repo>/
    <component>/
      overview.md
      src/<mirrored-source-path>.md
```

1. File name matches the source file name with `.md` appended.
2. Never group multiple source files into one onboarding file.
3. Keep the mirrored path stable so reviewers can move directly between source and onboarding.

## Metadata Rules

1. `lastUpdated`: use `mcp_time_get_current_time` in `YYYY-MM-DDThh:mm` format.
2. `lastVerifiedCommitHash` and `lastVerifiedCommitDate`: use the latest commit that touched the source file once the content has been verified.
3. For planned code not yet created, leave verification hash and date empty until the file exists and can be verified.

## Section Rules

Required top-level sections:

1. metadata table
2. `## Purpose`
3. `## Code Commentary`
4. `## Invariants And Boundaries`
5. `## Cross-Repo References`
6. `## Update History`

Subsections under `## Code Commentary`:

1. `### Logic`
2. `### Conventions`
3. `### Todos`
4. `### Docs References`

## Create Workflow

1. identify the exact source file path
2. confirm the mirrored onboarding path
3. read the source file and any adjacent onboarding or reference docs
4. gather metadata:
   - current time via MCP time tool
   - latest source-file commit via `git log --oneline -1 --format="%H %ci" -- <source-file>`
5. fill the template from `../templates/file-level-onboarding-template.md`
6. update the component overview if the file should be indexed there
7. cross-check docs references and cross-repo references before finishing

## Maintain Workflow

When code changes:

1. re-read the source file and the onboarding file
2. update any changed purpose, logic, conventions, invariants, or cross-repo references
3. update metadata after the content has been verified

When code is deleted or moved:

1. delete or move the onboarding file to match the source tree
2. update affected overview indexes and cross-links
3. check whether repo-level entity catalogs or nearby onboarding need follow-up because of the move or deletion
