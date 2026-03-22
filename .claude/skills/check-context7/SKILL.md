---
name: check-context7
description: "Get documentation for a framework, library, or language via Context7 MCP. Resolves the library ID and queries docs."
---

# Check Context7

Use the Context7 MCP tools to retrieve up-to-date documentation for a framework, library, or language.

## Workflow

1. Use `mcp__context7__resolve-library-id` to find the library ID for the requested framework/library.
2. Use `mcp__context7__query-docs` with the resolved library ID to retrieve relevant documentation.
3. Present the findings to the user.
