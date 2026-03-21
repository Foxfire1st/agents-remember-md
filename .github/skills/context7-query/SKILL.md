---
name: context7-query
description: "Query Context7 for up-to-date library documentation. Verify API usage, check for deprecations, and find current patterns. Usable standalone or invoked by the task-workflow skill during planning and implementation."
---

# Context7 Query

Query Context7 to retrieve up-to-date documentation for libraries and frameworks. Use this to verify that planned or in-progress code uses current APIs, catch deprecations, and find recommended patterns. This skill ensures implementations don't rely on stale training data.

---

## Prerequisites

- The Context7 MCP server must be configured and reachable.

---

## Inputs

The user provides ONE of:
1. **A library name + question** — e.g., "Is `React.useEffect` cleanup still the right pattern in React 19?"
2. **A library name + API to check** — e.g., "Check if MUI `makeStyles` is deprecated in v7."
3. **A code snippet** — "Is this Zustand store pattern current?" followed by the code.
4. **A general topic** — e.g., "Current FastAPI dependency injection patterns."

---

## Workflow

### Step 1: Resolve the library

Map the user's request to a Context7 library ID:

```
mcp_context7_resolve-library-id(libraryName="<library>")
```

Use the reference table below to guide the mapping. If the library isn't in the table, resolve it dynamically.

#### Reference table

| Stack area           | Context7 library    | When                                                             |
| -------------------- | ------------------- | ---------------------------------------------------------------- |
| React hooks/patterns | React 19            | Any hook usage, component patterns, ref handling                 |
| UI components        | MUI v7              | Any MUI component usage, theming, sx prop patterns               |
| State management     | Zustand             | Store creation, middleware, selectors                            |
| TypeScript           | TypeScript docs     | Advanced type patterns, compiler options                         |
| Python               | Relevant Python lib | Async patterns, typing, framework APIs (FastAPI, Pydantic, etc.) |
| Go                   | Relevant Go lib     | Standard library, popular frameworks (Gin, Echo, etc.)           |
| C / Embedded         | Relevant C lib      | Compiler-specific extensions, HAL/driver APIs, standard library  |

**This table is a starting point.** When encountering a library not listed here, resolve it with Context7 and add an entry to this table in the skill file for future reference.

### Step 2: Query the docs

```
mcp_context7_query-docs(libraryId="<resolved_id>", query="<specific question or API name>")
```

Craft the query to be specific:
- **For deprecation checks:** "Is `<API name>` deprecated? What's the replacement?"
- **For pattern verification:** "Current recommended way to <do X>."
- **For API exploration:** "How to use `<API name>` with <specific parameters>."

### Step 3: Analyse the results

Evaluate the documentation response:

1. **Deprecation detected:**
   - The API is marked `@deprecated` or the docs recommend an alternative.
   - Note the replacement API and any migration path.
   - Flag this finding clearly.

2. **Pattern change detected:**
   - The recommended pattern differs from what's in the codebase.
   - Note both the current codebase pattern and the recommended one.
   - Assess whether the change is breaking or cosmetic.

3. **Confirmed current:**
   - The API/pattern is documented as the current approach.
   - No action needed — proceed with confidence.

4. **Insufficient information:**
   - Context7 didn't have relevant docs for this query.
   - Fall back to the `brave-search` skill for web-based research.

### Step 4: Report findings

Present results clearly:

```markdown
## Context7 Query: <library> — <question>

**Status:** Current | Deprecated | Pattern changed | Inconclusive

### Finding
<What the docs say>

### Impact on current code
<How this affects the planned or existing implementation>

### Recommended action
<What to do — use as-is, migrate, investigate further>

### Code example (if applicable)
<Current recommended pattern from the docs>
```

---

## When invoked from the task-workflow skill

### During planning
- Query Context7 for each library/API that the implementation steps will use.
- Add relevant code snippets to the implementation steps to ensure correct patterns during coding.
- If a deprecation or pattern change is found: note it in the task file's decision log.

### During implementation
- When using an API for the first time in a step, verify it's current.
- Check the task file's implementation steps first — Context7 findings may already be documented there.
- If a new finding surfaces during implementation, update the task file.

### Recording findings
- **Deprecation found:** Add to task file decision log + flag in the relevant implementation step.
- **Codebase contradiction:** Add to task file as a Todo or potential improvement.
- **Confirmed current:** No action needed.

---

## Standalone usage

When used outside a task context:
- Present the findings directly to the user.
- If a deprecation is found, suggest checking the codebase for affected usages.
- If a pattern change is significant, suggest creating a task file to track the migration.

---

## Error Handling

- **Library not found:** Try alternative names. e.g., "material-ui" vs "mui". Report if truly not indexed.
- **No docs for query:** The library may be indexed but the specific API isn't documented. Fall back to `brave-search` skill.
- **MCP connection failure:** Report to user, suggest trying again or falling back to web search.
