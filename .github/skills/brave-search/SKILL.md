---
name: brave-search
description: "Search the web via Brave Search for implementation patterns, migration guides, known issues, and best practices. Prioritises authoritative sources. Usable standalone or invoked by the task-workflow skill during planning and research."
---

# Brave Search

Search the web for implementation patterns, architectural approaches, migration guides, known pitfalls, and best practices. This skill fills gaps that library docs (Context7) don't cover — blog posts, community knowledge, release notes, and real-world usage patterns.

---

## Prerequisites

- The Brave Search MCP server must be configured and reachable.

---

## Inputs

The user provides ONE of:
1. **A specific question** — e.g., "How to handle React 19 suspense with data fetching?"
2. **A research topic** — e.g., "MUI v7 migration from v6 breaking changes."
3. **A troubleshooting query** — e.g., "Zustand persist middleware not rehydrating on SSR."
4. **A library + version check** — e.g., "FastAPI 0.115 changelog and breaking changes."

---

## Workflow

### Step 1: Craft the search query

Transform the user's input into an effective search query:
- **Be specific:** Include library name + version when relevant.
- **Include year:** Add the current year to prefer recent results.
- **Use site filters:** For authoritative results, prefer known domains (see reference table).

```
mcp_brave-search_brave_web_search(query="<crafted query>", count=10)
```

If results are too broad or irrelevant, refine:
- Add the specific API or function name.
- Narrow by site: include `site:react.dev` or `site:typescriptlang.org`.
- Try alternative phrasing.

### Step 2: Evaluate results

For each result, assess:
- **Source authority** — is this from an authoritative source? (see reference table)
- **Recency** — prefer results from the last 12 months for evolving libraries.
- **Relevance** — does it directly address the question?
- **Quality** — is the content substantive or just surface-level?

**Prioritise results from authoritative sources.** Community posts (Stack Overflow, Reddit) are useful for troubleshooting but should be cross-referenced against official docs.

### Step 3: Fetch relevant pages (if needed)

For highly relevant results, fetch the full page content for deeper analysis if snippets are insufficient.

### Step 4: Synthesise findings

Present a structured summary:

```markdown
## Web Search: <topic>

### Query: "<search query>"

### Findings

**1. <Source title>** ([source domain](URL))
- <Key finding or pattern>
- <Relevance to our codebase>

**2. <Source title>** ([source domain](URL))
- <Key finding>

### Summary
<Concise synthesis — what's the takeaway for our implementation?>

### Actionable items
- <specific recommendation 1>
- <specific recommendation 2>
```

---

## Authoritative sources — reference table

Prioritise results from these high-quality, well-maintained sources:

| Domain           | Sources                                                                                                      |
| ---------------- | ------------------------------------------------------------------------------------------------------------ |
| React            | `react.dev` (official), `tkdodo.eu` (TanStack/React patterns), `kentcdodds.com`                              |
| TypeScript       | `typescriptlang.org`, `totaltypescript.com` (Matt Pocock), `type-challenges`                                 |
| Python           | `docs.python.org` (official), `realpython.com`, `fastapi.tiangolo.com`, `docs.pydantic.dev`                  |
| Go               | `go.dev` (official), `gobyexample.com`, `yourbasic.org/golang`                                               |
| General patterns | `martinfowler.com`, `refactoring.guru`, `web.dev`                                                            |
| Cloud / Infra    | `learn.microsoft.com`, `docs.aws.amazon.com`, `cloud.google.com/docs`                                        |

**This table is a starting point.** When encountering a domain or library not listed here, search for authoritative sources and add them to this table in the skill file for future reference.

---

## When invoked from the task-workflow skill

### During planning
- Search for implementation patterns and architectural approaches relevant to the task.
- Look for known pitfalls with specific library versions being used.
- Check for migration guides when upgrading dependencies.

### During research
- Targeted searches for specific technical problems.
- Finding real-world examples of patterns being considered.
- Checking ecosystem awareness — release notes, blog posts about major changes.

### Recording findings
- Useful patterns go into the task file under a "Research Notes" subsection or directly into the relevant implementation step.
- Do not paste large excerpts — summarise the finding and link to the source.
- If a finding contradicts Context7 results, note both and decide which to follow (prefer official docs, then Context7, then blog posts).

---

## Standalone usage

When used outside a task context:
- Present the findings directly to the user.
- If the findings suggest significant changes, recommend creating a task file.
- If used for troubleshooting, present the solution with source links.

---

## Complementary skills

- **`context7-query`** — for official library documentation. Use Context7 first for API-level questions; use Brave Search for patterns, guides, and ecosystem knowledge.

When Context7 returns insufficient results for a query, the `context7-query` skill may suggest falling back to this skill.

---

## Error Handling

- **No relevant results:** Try alternative search terms, broader or narrower queries. Report to user if truly nothing found.
- **Rate limited:** Wait and retry. Report if persistent.
- **MCP connection failure:** Report to user, suggest manual web search as fallback.
