---
name: sync-confluence-pages
description: "EXAMPLE INTEGRATION — Sync ONE Confluence page at a time to a local Markdown file via the Atlassian MCP server. Adapt for your own knowledge base export workflow."
---

# Sync Confluence Pages — Single-Page Workflow (Example Integration)

> **This is an example integration.** It demonstrates how to export knowledge base pages to local Markdown files. The concept (fetch → clean → store locally) applies to any knowledge base.

Pull ONE Confluence page through the MCP server and write a clean Markdown file to `docs/`.

**Critical rule:** Process exactly ONE page per invocation, end-to-end, before moving to the next. Never batch-fetch multiple pages.

---

## Prerequisites

- The Atlassian MCP server must be configured (in `.claude/settings.json` for Claude Code, or your tool's MCP config).
- Valid Atlassian API token in `.env`.

---

## Inputs

The user provides ONE of:
1. **A page URL** — extract the page ID from it.
2. **A page ID** — use directly.
3. **A page title / search term** — search, confirm with user, then proceed.
4. **A parent page ID + "children"** — list children, then process them **one at a time** in sequence.

---

## Workflow — 2 Steps Per Page

### Step 1: Fetch & Write Raw

1. **Resolve file path** — derive: `docs/<folder>/<Title>_<PageID>.md`

2. **Fetch the page** via your knowledge base API.

3. **Write the file** with a metadata header:

   ```markdown
   <!-- knowledge-base-sync
   pageId: <ID>
   source: <URL>
   title: "<TITLE>"
   version: <VERSION>
   lastSynced: <TODAY ISO DATE>
   -->

   # <TITLE>

   <RAW CONTENT HERE>
   ```

### Step 2: Clean Up Artifacts

Read back the file and apply any necessary cleanup for your knowledge base's export format (malformed code blocks, rendering artifacts, etc).

### Step 3: Confirm & Move On

Report to the user:
- Page title + file path written
- Version synced
- Any artifacts that needed manual judgment

Then **wait for the user** or proceed to the next page if the user asked for a sequence (still one-at-a-time).

---

## Error Handling

- **Page not found (404):** Warn user. Do not delete local file if it exists.
- **Auth failure:** Stop, tell user to check API token.
- **Conversion issues:** If an artifact is ambiguous, add `<!-- TODO: review -->` comment and move on. Don't block the whole page on one tricky section.
