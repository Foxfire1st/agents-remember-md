---
applyTo: "**"
---

# Workspace Defaults

## Multi-Repo Workspace

<!-- Customize this table for your workspace repos -->

This workspace contains multiple repositories:

| Repo | Purpose | Status |
|---|---|---|
| `<repo-1>` | Description | Primary — actively developed |
| `<repo-2>` | Description | Actively developed |
| `ai-infinite-context` | Documentation, onboarding, task tracking, context system | Meta — always active |

## Context System

This workspace uses a layered context system defined in `AGENTS.md`. Follow its rules for discovery, documentation maintenance, and planning.

Key paths:
- Onboarding: `onboarding/`
- Reference docs: `docs/`
- Glossary: `docs/glossary/index.md`
- Task files: `tasks/`

## Cross-Repo Awareness

Many features span multiple repos. Cross-repo ties must be **actively discovered**, not just read from existing docs.

Before making changes:
- Check onboarding and glossary for already-documented cross-repo ties.
- **Actively search the code** for API calls, WebSocket events, message queue topics, HTTP requests, and environment variables that reference other services.
- **Check reference docs** in `docs/` for interface contracts and data flow definitions.
- **Trace into adjacent repos**: when you find an outbound call, search the receiving repo for the handler — and vice versa.
- Check whether adjacent repos need coordinated changes.
- Use the glossary for canonical term usage across repos.
- Document newly discovered cross-repo ties in the relevant onboarding files.
