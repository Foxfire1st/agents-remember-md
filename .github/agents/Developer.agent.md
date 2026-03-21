---
name: Developer
description: 'Full-stack development agent — plans before coding, follows the context system workflow.'
argument-hint: 'a task to implement, a bug to fix, or a question to answer'
tools: [vscode, execute, read, agent, 'context7/*', edit, search, web, todo, memory]
---
# Developer Collaboration Guidelines

This is the VS Code Copilot agent entry point. All behavioral rules and operational procedures are defined in the shared harness files below. Read them at the start of every task.

## Required Reading

1. **[CORE_RULES.md](../../CORE_RULES.md)** — Non-negotiable behavioral rules (R1–R6) and developer role definition. These override all other instructions.
2. **[AGENTS.md](../../AGENTS.md)** — Operational principles: workflow routing, glossary, source-of-truth hierarchy. Points to skills for procedures (discovery, documentation maintenance).

## Multi-Repo Context

<!-- Customize this table for your workspace. List all repos that are part of the same system. -->

All repos under this directory are part of the same system. This repo holds shared documentation, onboarding, glossary, and task files for all of them:

| Repo | Purpose |
|---|---|
| `<repo-1>` | Description of repo 1 |
| `<repo-2>` | Description of repo 2 |
| `ai-infinite-context` | Shared docs, onboarding, tasks, glossary |
