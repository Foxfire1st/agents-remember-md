# Repo Entity Catalog Workflow

Use this workflow when creating or maintaining a repo-level entity catalog at `onboarding/<repo>/entities.md`.

Template: `../templates/repo-entity-catalog-template.md`

## Goal

Create or update one repo-level entity catalog documenting load-bearing real entities and their cross-layer projections.

## Scope

1. exactly one entity catalog per repo
2. focus on load-bearing real entities that recur across layers and cause review, migration, or naming confusion
3. do not use the file as a glossary or an exhaustive ontology of every noun in the repo

## Placement Rules

```text
onboarding/
  <repo>/
    overview.md
    entities.md
```

1. The file lives directly under the repo onboarding folder.
2. It complements `overview.md`; it does not replace it.

## Metadata Rules

1. `lastUpdated`: use `mcp_time_get_current_time` in `YYYY-MM-DDThh:mm` format.
2. `status`: use `draft` while the structure is still evolving and `active` once it is stable enough for routine reuse.

## Entity Entry Rules

1. Use canonical entity names as section headings.
2. Prefer the real entity over a drifted UI or storage label.
3. Record current naming drift instead of creating duplicate entries for synonyms.
4. State the current canonical source of truth when it is knowable.
5. Use layer comparisons to show how the same entity appears across systems.

## Recommended Entry Criteria

Add an entity when at least one is true:

1. the same real thing appears under multiple names across layers
2. the entity is central to a current migration or refactor
3. developers regularly confuse it with a nearby entity
4. the entity is important in cross-repo tracing

Avoid entries that are:

1. pure vocabulary with no stable entity behind it
2. very local implementation details with no cross-layer relevance
3. duplicates of an existing entry with only wording differences

## Create Workflow

1. confirm the repo does not already have an entity catalog
2. gather current time via MCP time tool
3. read the repo overview and adjacent onboarding or docs needed to identify the first load-bearing entities
4. fill the template from `../templates/repo-entity-catalog-template.md`
5. seed the catalog with the most confusion-prone entities first
6. add a lightweight pointer from the repo overview when it improves discoverability

## Maintain Workflow

1. re-read the current catalog and the adjacent source or onboarding docs for the entity being updated
2. prefer updating an existing entry over creating a near-duplicate
3. update `lastUpdated` whenever the entity set or any existing entity meaningfully changes
4. keep the file selective; expand only when the extra entries materially improve understanding

## Review Heuristics

Before finalizing changes, check:

1. are the entities real and stable rather than just terms?
2. does each entry separate the entity from commonly confused neighbors?
3. is naming drift documented without becoming the new canonical label?
4. do the layer representations help a reviewer trace the same entity across systems?
