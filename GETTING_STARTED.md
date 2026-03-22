# Getting Started

This guide covers the practical setup for using the AI context system. Read the [README](README.md) first for the problem space and design.

---

## 1. Orientation

Before your first task, read these three files:

1. **[CORE_RULES.md](CORE_RULES.md)** — the non-negotiable behavioral rules (R1–R6): plan-in-file, approval gates, milestone checks, decision escalation. Also documents the harness file hierarchy.
2. **[AGENTS.md](AGENTS.md)** — operational principles: workflow routing, glossary, source-of-truth hierarchy. Points to skills for procedures (discovery, documentation maintenance).
3. **Your repo's overview** — navigate to `onboarding/<repo>/overview.md` for the repo you'll be working in. This gives the AI (and you) a structural map before touching code.

If you encounter unfamiliar cross-repo terms, check the [glossary](docs/glossary/index.md).

---

## 2. Wiring the behavioral rules into your AI tool

The core behavioral rules live in **[CORE_RULES.md](CORE_RULES.md)**. The operational rules live in **[AGENTS.md](AGENTS.md)**. Together, these two files define the full collaboration model. They are tool-agnostic.

Each AI tool has a tool-specific entry point that loads these shared files. **Important:** some tools auto-load their entry file but do not follow file references into context. For those tools, the core rules must be inlined in the entry file — not just referenced. This is intentional redundancy for behavioral enforcement.

| Tool                           | Entry point                                                              | Loading behavior                                                                                                                                        |
| ------------------------------ | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code CLI                | `CLAUDE.md` in workspace root                                            | **Inlines** R1–R6 from CORE_RULES.md. Claude Code auto-loads CLAUDE.md but does not follow references, so the rules must be present in the file itself. |
| Claude Code (VS Code extension) | `CLAUDE.md` in workspace root                                           | Same as CLI — auto-loads CLAUDE.md. Skills in `.claude/skills/` are auto-discovered.                                                                     |
| JetBrains (PhpStorm, IntelliJ) | Custom instructions or system prompt in your AI plugin's settings        | Check whether your tool follows file references. If not, inline R1–R6 from CORE_RULES.md.                                                               |
| Other tools                    | Find the highest-priority prompt slot your tool offers                   | Same — inline if the tool can't follow references, reference if it can.                                                                                 |

When CORE_RULES.md is updated, any entry file that inlines the rules (currently CLAUDE.md) must be synced manually. CORE_RULES.md is always the canonical source.

The minimum that must be present in any tool's entry point:

- R1–R6 from CORE_RULES.md (inlined or referenced depending on tool behavior)
- Read AGENTS.md for operational principles
- Follow the workflow routing (light-task-workflow or task-workflow)

---

## 3. External integrations (optional)

Some skills integrate with external tools like Confluence, GitLab, or other knowledge bases. These are provided as **example integrations** in `.claude/skills/` — adapt them to your own tools:

| Example skill              | What it integrates with   | Adapt for                              |
| -------------------------- | ------------------------- | -------------------------------------- |
| `confluence-search`        | Atlassian Confluence      | Your wiki or knowledge base            |
| `sync-confluence-pages`    | Atlassian Confluence      | Syncing external docs to local copies  |
| `gitlab-ticket-retrieval`  | GitLab Issues             | Your issue tracker (Jira, Linear, etc) |

To enable the Confluence example:
1. Copy `.env.example` to `.env`
2. Set `CONFLUENCE_USERNAME` and `CONFLUENCE_API_TOKEN`
3. Configure your MCP server for Confluence (in `.claude/settings.json` for Claude Code, or your tool's MCP config)

---

## 4. How the system builds itself

You don't need to document everything upfront. The system builds incrementally through two paths.

### Path 1: Task-driven growth

Each task that touches a code file creates or updates its companion as part of the workflow. The first task on a file pays the most — researching context, writing the companion. Every task after that starts with context already in place and refines what's there. This is the default path: you do your normal work, and the documentation accumulates as a side effect.

### Path 2: Repo bootstrap (for bulk coverage)

The `repo-bootstrap` skill scaffolds onboarding for an entire repo in phases:

1. **Scout** — maps the repo structure, identifies functional areas, checks what adjacent repos already know. Produces a scout report.
2. **Area deep-dives** — one session per area to avoid context contamination. Reads code thoroughly, consults the developer for intent and domain context. Produces area reports.
3. **Synthesis** — reads all area reports (not code) and produces the repo-level `overview.md`. This is the minimum viable baseline — it gives the AI structural awareness of the whole repo.
4. **Deepen** (optional) — component overviews and file-level companion files, done incrementally as areas become relevant.

Each phase produces a standalone artifact. You can stop after any phase. Phase 3's overview is the inflection point — after that, every task on the repo starts with structural context instead of searching blind.

### Tracking your progress

Use the onboarding index (`onboarding/index.md`) to track which repos have been bootstrapped:

```markdown
| Repo            | Onboarding status                  |
| --------------- | ---------------------------------- |
| api-service     | Full — repo overview + companions  |
| web-client      | Repo overview only                 |
| mobile-app      | Placeholder — ready for bootstrap  |
```

---

## 5. Local or shared — your choice

You can maintain this repo locally, and the system works fine that way. But using a shared repo unlocks two things:

**Collaborative compounding.** When one developer's task creates a companion file or refines an invariant, every future session — by any developer, with any AI tool — benefits from that work. The system compounds across the team, not just per individual.

**Branching alongside code.** When you branch a repo for a feature, you can branch this repo alongside it. Companion files, task state, and onboarding stay aligned with the code branch. When the feature merges, the documentation merges with it — reviewed in the same PR workflow as code. A local-only setup can't track that.
