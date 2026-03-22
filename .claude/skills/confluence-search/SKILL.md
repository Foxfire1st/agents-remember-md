---
name: confluence-search
description: "EXAMPLE INTEGRATION — Search Confluence for domain knowledge, specs, and reference docs. Adapt this skill for your own knowledge base (Notion, SharePoint, wiki, etc)."
---

# Confluence Search (Example Integration)

> **This is an example integration.** It demonstrates how to connect the task workflow to a knowledge base. Adapt the MCP calls and workflow to your own tool (Notion, SharePoint, Bookstack, internal wiki, etc).

Search Confluence for authoritative domain knowledge — specs, protocols, API definitions, architecture docs. When a relevant page is found, check if it's already in the local `docs/` collection and sync it if not. This skill is the **discovery** layer; the `sync-confluence-pages` skill handles the **fetch and export** layer.

---

## Prerequisites

- The Atlassian MCP server must be configured and reachable.
- Valid Atlassian API token.
- The `sync-confluence-pages` skill must be available for local sync.

---

## Inputs

The user provides ONE of:
1. **A search term or question** — e.g., "API spec", "event protocol", "authentication flow".
2. **A page URL** — extract the page ID directly.
3. **A page ID** — use directly.
4. **A domain context** — e.g., "I'm working on the notification system" — the skill derives search terms.

---

## Workflow

### Step 1: Search the knowledge base

```
confluence_search(query="<search term>", limit=10)
```

Review results and identify relevant pages. Consider:
- **Title relevance** — does the page title match the domain area?
- **Space** — is it in a known relevant space?
- **Snippet** — does the content preview suggest useful information?

If results are too broad, refine the search:
- Add domain-specific qualifiers.
- Filter by space if the relevant space is known.
- Try alternative terms from the glossary (`docs/glossary/index.md`).

### Step 2: Retrieve the page

For each relevant page:

```
confluence_get_page(page_id="<ID>")
```

Read the content and assess:
- **Is this the right page?** Does the content match what was being searched for?
- **Is it up-to-date?** Check the version/date — very old pages may be superseded.
- **Are there child pages?** Check for relevant children if the page appears to be a parent.

### Step 3: Check local collection

Check whether this page is already synced locally in `docs/`.

**Three outcomes:**
1. **Already synced and recent** — use the local copy. Note the local path for references.
2. **Already synced but stale** — re-sync using the `sync-confluence-pages` skill.
3. **Not in local collection** — proceed to Step 4.

### Step 4: Sync missing pages to local docs

When a relevant page is not in the local collection, invoke the **`sync-confluence-pages` skill** to fetch, write, clean, and register the page. This grows the local docs collection organically as tasks demand pages.

### Step 5: Compile findings

Present a summary of what was found:

```markdown
## Knowledge Base Search Results

### Query: "<search term>"

**Pages found:**
1. **<Page Title>** (Space: <KEY>, ID: <ID>)
   - Local: `docs/<path>.md` [synced / newly synced / not synced]
   - Relevance: <why this page matters>
   - Key content: <brief summary of useful information>

2. **<Page Title>** (Space: <KEY>, ID: <ID>)
   - ...

### Recommended for sync
- <Page Title> (ID: <ID>) — <reason>
```

---

## When invoked from the task-workflow skill

The findings feed into the task file:
- **Page content** → informs Objective, Problem Analysis, and Implementation Steps.
- **Local paths** → added to Sources/References section.
- **Synced pages** → noted as available in `docs/`.
- **Unsynced pages** → flagged for sync or noted as external-only references.

---

## Adapting for other knowledge bases

To adapt this skill for a different knowledge base:

1. **Replace the MCP calls** with your tool's API (Notion API, SharePoint Graph API, etc).
2. **Adjust the sync step** — adapt or remove the `sync-confluence-pages` sub-skill depending on whether you want local copies.
3. **Update the local docs path** — `docs/` can hold exports from any knowledge base, not just Confluence.
4. **Update prerequisites** — your tool's API credentials and MCP server config.

The output format (Step 5) should stay the same — it's what the task-workflow expects.

---

## Error Handling

- **No results:** Try alternative search terms. Check the glossary for canonical terminology. Report to user if nothing found.
- **Page not found (404):** The page may have been deleted or moved. Report to user.
- **Auth failure:** Stop, tell user to check API token.
- **Sync failure:** Report the sync error but still present the page content from the API response. The local sync is a convenience, not a blocker.
