# AGENTS.md — Operational Principles

This file defines operational principles for AI agents working in this workspace — workflow routing, glossary usage, and source-of-truth hierarchy. These apply regardless of which task is active, which repo is being worked on, or which agent mode is in use.

Core behavioral rules (plan-in-file, approval gates, decision escalation, milestone alignment) live in **[CORE_RULES.md](CORE_RULES.md)** — read it first.

Procedural skills (discovery, documentation maintenance) live in their respective skill files — this file defines **principles**, not step-by-step procedures. Project knowledge lives in instructions, onboarding, docs, and the glossary.

---

## Memory System Awareness

This workspace uses a layered memory system. Understand the layers before acting:

| Layer                   | Location              | Purpose                                                         |
| ----------------------- | --------------------- | --------------------------------------------------------------- |
| `CORE_RULES.md`         | `CORE_RULES.md`       | Non-negotiable behavioral rules (R1–R6), harness file hierarchy |
| `AGENTS.md` (this file) | `AGENTS.md`           | Operational principles: routing, glossary, source-of-truth      |
| `*.instructions.md`     | `.github/instructions/` | Auto-attached retrieval hooks — compact, <50 lines            |
| `/onboarding/`          | `onboarding/`         | Code commentary — logic, invariants, conventions, task tracking |
| `/docs/`                | `docs/`               | Authoritative reference documentation                           |
| `/docs/glossary/`       | `docs/glossary/`      | Canonical vocabulary and cross-repo index                       |
| Task files              | `tasks/`              | Current change intent, plans, decision logs                     |
| Prompts                 | `.github/prompts/`    | Manual-invocation workflow templates                            |
| Skills                  | `.github/skills/`     | Multi-step workflows with scripts and resources                 |

---

## Discovery

**Always prefer top-down discovery over brute-force code roaming.** The full discovery procedure — reading order, cross-repo techniques, and fallback for missing onboarding — is defined in the [`discovery` skill](.github/skills/discovery/SKILL.md). Invoke it before touching code.

---

## Documentation Maintenance

Onboarding files are code commentary — they describe what the code does, not what it should do. All planning belongs in task files. The `task-workflow` skill defines when and how onboarding is updated (Phases 2.1, 3.3, 4.2–4.6). The `create_or_update-onboarding-file` skill defines the template.

---

## Planning Rules

### Task workflow routing

Before making changes, determine the appropriate workflow:

| Target                                                                                 | Routing                                                                                    |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| **Code** — multi-file, cross-component, cross-repo, risky invariants, or multi-session | `task-workflow` skill (full lifecycle with onboarding, drift detection, `<Task>` blocks)   |
| **Code** — single-file or small change that doesn't meet the above thresholds          | `light-task-workflow` skill (plan → approval → implement → close, no onboarding machinery) |
| **Non-code** — docs, skills, configs, READMEs, presentations                           | `light-task-workflow` skill — always, regardless of size                                   |

Read the relevant skill before starting. The skills define approval gates, iteration cycles, and task file structure that this section does not repeat.

### Task file discipline

The core planning rules — plan-in-file, approval gates, decision escalation, milestone alignment, mid-implementation routing — are defined in [CORE_RULES.md](CORE_RULES.md) (R1–R6). This section covers the operational details that support those rules.

- Before creating a new task file, search `tasks/` for active (non-completed) tasks that cover the same artifact or scope. If one exists, the new work is an iteration — update the existing task file instead of creating a new one.
- Plans should be externalized into task files (`tasks/`) as soon as the discussion has enough substance.
- Do not let plans live only in chat — they degrade over time.
- Each task file should include a decision log to track proposals, objections, accepted decisions, and reversals.
- Chat stays focused on concise summaries, objections, tradeoffs, and decisions.
- The task file is the source of truth for what was agreed.

---

## Source-of-Truth Hierarchy

When code, docs, onboarding, and task files disagree:

| Source         | Role                                                                 |
| -------------- | -------------------------------------------------------------------- |
| `/docs/`       | Intended reference model (authoritative system facts)                |
| Code           | Current implementation model (what actually runs)                    |
| `/onboarding/` | Code commentary — describes the code as it is, links to active tasks |
| Task files     | Change intent — what should change and why                           |

Onboarding is not a reconciliation layer. If code and docs disagree, the mismatch should be identified during task planning and resolved through a task — not absorbed into onboarding as implicit planning.

---

## Glossary Rules

- Use canonical terms from `docs/glossary/index.md` when discussing cross-repo concepts.
- If a term is unfamiliar or ambiguous, check the glossary before guessing.
- When a new cross-repo term is encountered, add it to the master glossary.
- Domain-local terms may go in scoped sub-glossaries (e.g., `docs/glossary/networking.md`).

---

## General Behavioral Rules

- Prefer editing existing files over creating new ones.
- Search for similar patterns in the codebase before implementing new features.
- When the user asks a question with multiple possible approaches, discuss before implementing.
- After user correction, treat it as a signal to review code vs. onboarding vs. reference docs.
- Do not create documentation that duplicates what is already well-documented in another layer.
- **Always use the available time tool** to get the current date when writing `lastUpdated` or any timestamp in onboarding files, task files, or change history entries. Never guess or hardcode dates.
