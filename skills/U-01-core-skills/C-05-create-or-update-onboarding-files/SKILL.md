---
name: create_or_update-onboarding-file
description: "Create and maintain onboarding artifacts including file-level onboarding MDs and repo-level entity catalogs. Covers template, folder organization, section conventions, and metadata. Enforces strict 1-to-1 mapping for source files and one entity catalog per repo. Use this whenever creating new onboarding files, repo entity catalogs, or auditing existing onboarding artifacts for format compliance."
---

# C-05 Create Or Update Onboarding Files

This package owns durable onboarding maintenance for two artifact types:

1. file-level onboarding markdown files for concrete source files
2. repo-level entity catalogs for load-bearing cross-layer entities

Planning stays in task artifacts. This package defines how onboarding itself is created, updated, and kept structurally consistent.

## Routing

Choose the artifact type first, then use the matching workflow and template.

| Artifact type             | Use when                                                                              | Workflow                                      | Template                                      |
| ------------------------- | ------------------------------------------------------------------------------------- | --------------------------------------------- | --------------------------------------------- |
| File-level onboarding     | You are creating or updating onboarding for one source file mirrored under onboarding | `workflows/file-level-onboarding-workflow.md` | `templates/file-level-onboarding-template.md` |
| Repo-level entity catalog | You are creating or updating `onboarding/<repo>/entities.md`                          | `workflows/repo-entity-catalog-workflow.md`   | `templates/repo-entity-catalog-template.md`   |

## Shared Placement Rules

```text
onboarding/
  index.md
  <repo>/
    overview.md
    entities.md
    <component>/
      overview.md
      src/<mirrored-source-tree>.md
```

1. File-level onboarding keeps strict 1-to-1 mirroring with source files.
2. Repo-level entity catalogs exist once per repo at `onboarding/<repo>/entities.md`.
3. Use `mcp_time_get_current_time` for onboarding timestamps; never guess.
4. Keep durable onboarding commentary separate from task-local planning and output docs.

## Quick Rules

1. Prefer updating an existing onboarding artifact over creating parallel duplicates.
2. File-level onboarding explains one concrete source file.
3. Repo-level entity catalogs document real entities and cross-layer projections, not generic glossary content.
4. If both a file-level onboarding document and a repo entity catalog need updates, handle both in the same pass when the task materially affects both.
5. This package may be invoked immediately from `C-01-findings-capture` when a verified factual current-state clarification qualifies for onboarding propagation.

## Lifecycle Rules

### When code changes

1. update the relevant onboarding sections that no longer match the code
2. refresh verification metadata after the content update

### When code is deleted or moved

1. delete or move the mirrored onboarding file to match the real source tree
2. update overview indexes and cross-references that point at the old location
3. check whether repo-level entity catalogs or related onboarding now need cleanup
