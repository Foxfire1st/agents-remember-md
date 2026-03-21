---
description: "Start a new task: gather context from onboarding, check for drift, propose a task file."
agent: agent
tools: [vscode, execute, read, agent, 'context7/*', edit, search, web, memory, todo]
---

# Start Task

You are beginning a new task. This prompt is a launcher — the full procedure lives in the **task-workflow** skill.

## Procedure

1. **Read the task-workflow skill:** Open and read `.github/skills/task-workflow/SKILL.md`.
2. **Follow Phase 1** (Starting a Task) — understand the request, gather requirements from your issue tracker and knowledge base if applicable, create the task file with the standard template.
3. **Follow Phase 2** (Planning) — read onboarding top-down, run drift detection, create onboarding for planned new files, query Context7 and Brave Search, check cross-repo awareness.
4. **Discuss with the user** — present a concise summary in chat. Wait for approval before implementing.

## Sub-skills to invoke as needed

- Your issue tracker skill — when the user provides a ticket number or link.
- Your knowledge base skill — for domain context, specs, protocol docs.
- `context7-query` — to verify planned APIs are current, check for deprecations.
- `brave-search` — for implementation patterns, migration guides, known pitfalls.
- `onboarding-drift-detection` — to catch stale onboarding before planning.
- `create_or_update-onboarding-file` — for creating onboarding MDs for planned new files.
