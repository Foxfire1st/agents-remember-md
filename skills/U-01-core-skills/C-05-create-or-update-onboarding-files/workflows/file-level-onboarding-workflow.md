# File-Level Onboarding Workflow

Use this workflow when creating or maintaining onboarding for one concrete source file mirrored under `<onboarding-root>/<repo>/<component>/src/...`.

Template: `../templates/file-level-onboarding-template.md`

## Goal

Create or update one onboarding markdown file mirrored under `<onboarding-root>/<repo>/...` for one concrete source file.

## Scope

1. one onboarding markdown file per source file
2. strict mirrored path under the component `src/` subtree
3. durable commentary only; planning stays in task artifacts

## Source Discovery Rules

1. Start by reading `<AR_MANAGEMENT_ROOT>/system/sources.md` and use its `Domain Documentation` category for the file being documented.
2. Use the `Domain Documentation` sources from `<AR_MANAGEMENT_ROOT>/system/sources.md` when building `### Docs References` and any load-bearing explanatory prose that depends on technical or behavioral documentation.
3. Treat adjacent onboarding as supporting input, not the whole discovery plan and not a substitute for the `Domain Documentation` pass.
4. If `Domain Documentation` includes both local and live variants, use the local material first for direct access and line citations, but write the onboarding link to the canonical online reference rather than the local mirror path.
5. If relevant material cannot be found in the `Domain Documentation` sources, record what was checked and that no relevant evidence was found.

## Placement Rules

```text
<onboarding-root>/
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

Citation requirements for reference sections:

1. `### Docs References` must include a concise prose summary when there is meaningful domain context to explain, followed by a markdown table with columns `Finding`, `Citations`, and `Source Path`.
2. `## Cross-Repo References` must include a concise prose summary when there is meaningful system-boundary behavior to explain, followed by a markdown table with columns `Finding`, `Citations`, and `Source Path`.
3. In `### Docs References`, `Source Path` must link to the canonical online document URL. Read local mirrors if needed, but do not link to them.
4. In `## Cross-Repo References`, `Source Path` must use a workspace-relative markdown link to the cited code or onboarding file. Do not use absolute filesystem paths.
5. `Citations` must list exact line ranges, for example `L10-L18` or `L10-L18; L42-L47`.
6. `Finding` must be a concise summary of what those cited lines establish.
7. Do not rely on uncited prose alone in either section. Investigate and preserve useful explanation, then support it with the citation table. If nothing relevant exists, keep the table and note what was checked and that no relevant evidence was found.

## Create Workflow

1. identify the exact source file path
2. confirm the mirrored onboarding path
3. read `<AR_MANAGEMENT_ROOT>/system/sources.md`, then read the source file and the relevant materials from its `Domain Documentation` category, capturing the exact citation ranges needed for `Docs References` and `Cross-Repo References`
4. gather metadata:
   - current time via MCP time tool
   - latest source-file commit via `git log --oneline -1 --format="%H %ci" -- <source-file>`
5. fill the template from `../templates/file-level-onboarding-template.md`
6. update the component overview if the file should be indexed there
7. cross-check docs references and cross-repo references before finishing: preserve any load-bearing explanation, ensure the cited material reflects the `Domain Documentation` sources from `<AR_MANAGEMENT_ROOT>/system/sources.md`, ensure docs rows link to the canonical online reference, ensure code/onboarding rows use workspace-relative links, and ensure every table row has exact line ranges plus a concise finding summary

## Maintain Workflow

When code changes:

1. re-read the source file and the onboarding file
2. re-read `<AR_MANAGEMENT_ROOT>/system/sources.md` when the domain-documentation discovery path may have changed, then update any changed purpose, logic, conventions, invariants, docs references, or cross-repo references, including correcting existing explanation and refreshing citation line ranges when the source moved or changed
3. update metadata after the content has been verified

When code is deleted or moved:

1. delete or move the onboarding file to match the source tree
2. update affected overview indexes and cross-links
3. check whether repo-level entity catalogs or nearby onboarding need follow-up because of the move or deletion
