# AI Context System

AI coding tools lose context, drift from plans in long sessions, and forget everything between sessions. When your backend defines an event type that three other codebases must implement — a TypeScript frontend, a mobile app, and a Go gateway service — no amount of code search in one of those repos will surface that contract. The AI writes confident code that breaks at integration.

This system addresses that. Structured companion files document what code search cannot reveal: cross-repo dependencies, invariants that must hold, the direction of in-progress migrations, decisions from previous sessions, and links to the technical specifications that define intended behavior. A task workflow keeps these files accurate as the code changes.

By the end of this guide, you will know how this system:

- **Prevents documentation from going stale** — every file records a git commit hash, and drift detection runs before each task. Staleness is caught before it causes damage.
- **Keeps your effort where it counts** — the AI handles file creation, routine updates, and drift detection. You contribute what it can't automate: reviewing plans, validating correctness, and adding the domain knowledge that only comes from working in the codebase.
- **Scales with complexity** — the full workflow is reserved for changes where complexity would outpace the AI's ability to stay aligned without it — multi-file refactors, cross-repo contracts, risky invariants, multi-session work. Smaller changes use a lighter workflow with less overhead but the same approval discipline.
- **Differs from existing tools** like CLAUDE.md, cursor rules, and copilot instructions — those are real and useful, and this system is built for the specific gap they leave.

---

## Problems you have probably already seen

You ask the AI to implement something. It writes code confidently. The code compiles. It even looks reasonable. But it is wrong — it ignores a pattern that every developer on the team knows about, because the pattern is not visible from the code the AI looked at.

Or: you are working through a larger change with the AI in a chat session. You agree on a plan. You start implementing part 3, and when you check back on parts 1 and 2, the AI has quietly rewritten them. Not maliciously — it just ran out of room in its context window and reconstructed what it thought was there. The longer the chat, the worse this gets.

Or: you had a good session yesterday, the AI understood the full picture, and you made real progress. Today you start a new session. The AI has no idea what happened yesterday. You spend 20 minutes re-explaining, and the re-explanation is lossy — you forget details, the AI fills in gaps with guesses, and now you are working from a slightly wrong version of yesterday's plan.

These are not unusual experiences. They are the default behavior of every LLM-based coding tool right now. They happen because of how LLMs work — and the problems get worse in proportion to the size and complexity of the task.

**Between sessions: complete amnesia.** LLMs are stateless. Every session starts with no knowledge of what happened before. If you spent an hour yesterday explaining the architecture, agreeing on a plan, and making decisions — none of that exists today. You can re-explain, but re-explanation is always lossy. You forget details, the AI fills gaps with guesses, and you end up working from a slightly different version of what was agreed.

**Within a session: gradual degradation.** The AI has a context window — the total amount of text it can hold at once. The natural assumption is that everything in the window is equally available until you run out of room. That is wrong. [Du et al., "Context Length Alone Hurts LLM Performance Despite Perfect Retrieval"](https://arxiv.org/abs/2510.05381) (EMNLP 2025) tested five open- and closed-source models on math, QA, and coding tasks and found that performance degrades 14–85% as input length increases — even when the model can perfectly retrieve the relevant information, and even when all irrelevant tokens are replaced with whitespace. The sheer length of the input is enough to hurt performance. This is not a capacity problem — it is a signal-to-noise problem. Every file the AI reads, every search result, every correction attempt stays in the window as accumulated noise. As the noise grows, each piece of relevant information gets proportionally less of the model's attention.

**The middle of the context is a blind spot.** Models attend well to the beginning and end of their context, and poorly to everything in the middle — with accuracy dropping by more than 30% for information in middle positions ([Liu et al., "Lost in the Middle," TACL 2024](https://aclanthology.org/2024.tacl-1.9/)). When you are deep into a session and the plan you agreed on earlier is sitting in the middle of the context, the AI is least able to hold onto it. This is not carelessness. It is a structural property of how the model distributes attention, and it explains why the AI quietly rewrites earlier parts of a plan: it literally cannot attend to them as effectively as to whatever is immediately in front of it.

**Longer sessions compound the problem.** Toby Ord's ["Half-Life of AI Agent Success Rates"](https://www.tobyord.com/writing/half-life) found that every agent's success rate drops after approximately 35 minutes of work — and doubling the task duration does not double the failure rate, it quadruples it. The reason is a compounding loop: degraded context causes a mistake, the mistake requires a correction, the correction requires reading more files and running more searches, and each of those adds more noise to the already-degraded context. The longer the session, the faster this loop accelerates. This is why multi-file refactors and migrations are disproportionately hard for AI: they are long tasks by nature, and long tasks trigger this compounding effect.

**Code search cannot recover what was never written down.** The AI can search your files and find function definitions, type declarations, and import chains. But no search can recover the intent behind a half-finished migration — which files have been converted, what the target pattern is, what edge cases were decided in a previous session. No search can recover invariants that exist only in the team's shared understanding. And no search can find cross-repo contracts: searching a TypeScript frontend will not reveal that the canonical definition lives in a Go service two repositories away, or that three other codebases must implement the same type.

---

## The core idea: externalize the AI's working memory

When you work on a complex change, you hold things in your head: what the code does, what must not break, what connects to other systems, what direction a migration is going. You built that understanding over months or years. The AI has none of it. Every session starts empty.

The obvious step is to write that knowledge down — version controlled, diffable, committed alongside the code. That is necessary, but it is not enough. A flat description of the project still leaves the AI to figure out the specifics by searching code. And code search cannot tell it which parts of the codebase are connected across repos, which invariants must hold, or which direction an in-progress migration is supposed to go.

This system goes further in four ways:

**Structured, not narrative.** Each companion file has typed sections — Logic, Invariants, Cross-repo References, Tasks — not free-form notes. The AI does not read prose and hope to extract what matters. Each section has a defined purpose.

**Navigable, not flat.** Each file has outward edges: cross-repo references point to the corresponding code in other repositories, docs references point to specifications, task blocks point to the active task file. The AI follows edges to adjacent context instead of searching blindly.

**Read-first.** The AI reads the companion file *alongside* reading the source code. This inverts the default behavior. Instead of only reading code and guessing at context, it starts with the structural knowledge already loaded — what the code connects to, what must not break, what direction an in-progress migration is going — and then reads the code with that context in place.

**Workflow-integrated.** Companion files are not maintained by discipline — they are maintained because the task workflow requires updating them at defined transition points. When the AI starts a substep, it reads the companion files. When it finishes a substep, it updates them. Drift detection catches everything that happens outside this workflow. The result: accurate documentation is a side effect of the process, not a separate obligation.

---

## What a companion file looks like

For a source file like `api-service/src/stores/sessionStore.ts`, the companion lives at `onboarding/api-service/.../sessionStore.md`:

```markdown
# sessionStore.ts
repository: api-service
path: src/stores/sessionStore.ts
lastUpdated: 2026-03-15
lastVerifiedCommitHash: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2
lastVerifiedCommitDate: 2026-03-14

## Code Commentary

### Logic
Zustand store holding session-level data from GET /api/v1/session/{id}/state.
commandLocks seeded from API on first load only — after that, WebSocket events
are the ongoing authority via updateLock().

### Invariants
- commandLocks: first API call seeds, WS events own all subsequent updates.
- hasPermission() and isLocked() fall back to false for unknown types.

### Cross-repo References
- EventType enum must stay in sync with Go gateway event_types and mobile-app EventCategory.
- Lock state updated by WS lockStatus events via websocket.ts.

### Docs References
- API spec: docs/api/session-management.md
- WebSocket protocol: docs/architecture/websocket-protocol.md

## Tasks
<Task>
target: updateLock() and lock state management
taskGoal: "migration"
progressStatus: "in-progress"
description: Migrate lock state from local store to shared event bus so gateway can broadcast lock changes cross-tab.
progress: Event bus wired up, updateLock() refactored. Pending: remove legacy WS handler fallback.
taskfile: tasks/260315_lock-state-migration.md (S2)
</Task>

## Update History
<!-- newest first; entries move here from Tasks when completed -->

- 2026-03-10 — Added isLocked() guard for unknown event types after silent drop bug in production. (task: tasks/260310_unknown-event-guard.md)
```

**Logic** says what the code does. **Invariants** says what must not be broken. **Cross-repo References** says what this file connects to in other repos. **Docs References** links to the specifications that define intended behavior — so the AI knows not just what the code does, but what it *should* do. **Tasks** tracks active modifications. The AI reads this before touching the code — it starts with structural context instead of guessing from syntax.

---

## What this looks like in your code

Here are three examples from a multi-repo project — the kind of thing this system is built for.

### Example 1: The plugin registration pattern

A backend service has 20+ plugins (Analytics, Notifications, Billing, Search, etc.). There is no central plugin list. No `registerPlugin()` call in main. Instead, each plugin uses a decorator that registers it at import time, and the framework collects them via package scanning:

```python
# In analytics_plugin.py — plugin registers itself via decorator
@register_plugin(
    name="analytics",
    init_order=15,
    dependencies=["database", "cache"],
)
class AnalyticsPlugin:
    async def setup(self, app):
        # ...
```

An AI that does not know this pattern will look for a plugin list in `main.py`, not find one, and either add manual import calls (wrong) or ask you where to register things (wasted time). The companion file for the plugin framework explains the pattern once, and every AI session afterwards knows.

### Example 2: Init order matters

The service has a specific initialization sequence — Database first, then Cache, then MessageQueue, then Auth, then HTTP Server. An AI adding a new plugin has no way to guess which init slot it needs. Wrong position = silent dependency on something not yet initialized.

### Example 3: Cross-repo event type sync

`EventType` in the backend defines 12 event types (`user.created`, `order.placed`, `payment.failed`, ...). These same types must appear as:
- `EventType` in the TypeScript frontend
- `EventCategory` in the mobile app
- `event_types` in the Go gateway service

Adding an event type in the backend without updating the other three repositories produces silent failures. The new event arrives, gets mapped to `unknown`, and nobody sees an error. A companion file with a Cross-repo References section makes this contract visible:

```markdown
### Cross-repo References
- EventType enum must stay in sync with:
  - TypeScript: EventType (web-client/src/types/events.ts)
  - React Native: EventCategory (mobile-app/src/constants/events.ts)
  - Go: event_types (gateway/internal/events/types.go)
```

No amount of code search in one repo will discover this. The companion file makes it explicit.

---

## Folder structure

```
ai-infinite-context/                  # This repo — clone alongside your code repos
  CORE_RULES.md                       # Non-negotiable behavioral rules (R1–R6)
  AGENTS.md                           # Operational principles: routing, glossary, source-of-truth
  onboarding/                         # Companion files mirroring source code
    index.md                          # Workspace-level map
    <repo>/                           # One folder per repo
      overview.md                     # Repo-level architecture summary
      <component>/                    # Component groupings
        overview.md                   # Component architecture
        src/<path>/<to>/<file>.md     # Per-file companion
  tasks/                              # Task plans and decision logs
  docs/                               # Reference docs (API specs, protocol definitions, wiki exports)
    glossary/                         # Canonical terms across repos
  .github/
    agents/                           # AI agent entry points (VS Code Copilot)
    skills/                           # Structured AI workflows (procedures)
    instructions/                     # Auto-attached context per file pattern
    prompts/                          # Manual-invocation templates
```

---

## What exists today and where it stops

There is a spectrum of approaches for giving AI agents project context, from simple configuration files to sophisticated agent architectures. Each solves real problems. The question is where each one stops.

| Approach                                                                                                                                                                                                                                   | What it does                                                                                                                                                                                                                                                                 | Where it stops                                                                                                                                                                                                                                                                                                  |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Context files** ([CLAUDE.md](https://claude.com/blog/using-claude-md-files), [.cursorrules](https://docs.cursor.com/context/rules), [copilot-instructions.md](https://docs.github.com/en/copilot/how-tos/configure-custom-instructions)) | Single file loaded at session start with project conventions and rules. Nested variants ([per-directory CLAUDE.md](https://claude.com/blog/using-claude-md-files), [Codebase Context Spec](https://github.com/Agentic-Insights/codebase-context-spec)) add per-area context. | Flat descriptions of the project. No cross-repo awareness, no task state, no way to encode "we are mid-migration and the direction is X."                                                                                                                                                                       |
| **RAG / semantic code search** ([Cursor @Codebase](https://docs.cursor.com/chat/context#codebase), [GitHub Copilot code search](https://docs.github.com/en/copilot))                                                                       | Embeds code chunks as vectors. Retrieves semantically similar fragments at query time.                                                                                                                                                                                       | Retrieves code fragments, not structural knowledge. No encoding of intent, contracts, or invariants. Per-query — nothing persists between retrievals. Misses relevant code that doesn't match the query embedding.                                                                                              |
| **Repository maps** ([aider RepoMap](https://aider.chat/docs/repomap.html), [Cursor indexing](https://docs.cursor.com/context/codebase-indexing))                                                                                          | Builds a structural map of symbols and relationships. Aider uses tree-sitter + PageRank to rank symbols by connectivity. Cursor indexes into a server-side vector DB for semantic search.                                                                                    | Ephemeral — rebuilt or re-queried each session. Single-repo. Maps what *is*, not what *should be*: no migration direction, no cross-repo contracts, no persistent task state.                                                                                                                                   |
| **Stateful agents** ([Letta/MemGPT](https://docs.letta.com/))                                                                                                                                                                              | Agent manages its own persistent memory across sessions. Decides what to remember and what to forget via tool calls.                                                                                                                                                         | General-purpose memory — the agent decides what matters, with no structural guarantee it captures cross-repo contracts, invariants, or migration state. Cognitive state and coordination state are separate: cross-repo awareness remains an [unsolved problem](https://github.com/letta-ai/letta/issues/3226). |

These systems share a design direction: they treat context as something to **retrieve or accumulate** — pulling information from the codebase toward the AI. This system inverts the direction. It pre-structures what the AI needs to know and places it where the workflow naturally reads it. The AI doesn't search for context; the context is already loaded when it starts working.

That retrieval-based approach breaks down when:
- The project spans multiple repositories, and correctness requires knowing what happens on the other side of an API call, WebSocket event, or message queue topic — something no single-repo index or embedding will surface.
- You are mid-migration, and the AI needs to know not just what the code does now, but which direction it should move — intent that doesn't exist in any code fragment to retrieve.
- A task spans multiple sessions, and the plan and decisions need to survive between them — not in agent memory that may silently drop what it considers unimportant.
- Multiple developers or agents are working in the same codebase, and one needs to see what the other is currently changing — coordination state that no individual agent's memory covers.

---

## What this system adds

This system extends the "context file" idea in four specific ways:

**1. Per-file companion files with cross-repo edges.**
Not one flat file per repo — a companion markdown file for each important source file with typed sections: what the code does (Logic), what must not break (Invariants), what this file connects to in other repos (Cross-repo References), which specifications define the intended behavior (Docs References), and what tasks are currently modifying it (Tasks). The cross-repo and docs reference sections are the key difference — they give the AI visibility into contracts, protocols, and specifications that no amount of code search will surface.

**2. Task files that preserve intent across sessions.**
When a complex change starts, the plan, implementation steps, and all decisions go into a task file — not the chat. If the session ends, the context window fills up, or a different developer continues the work, the task file is the source of truth for what was agreed and why. This is ["docs as code"](https://www.writethedocs.org/guide/docs-as-code/) applied to the plan itself.

**3. Skills — structured workflows the AI follows.**
Instead of hoping the AI discovers the right process, skills are explicit instructions: how to start a task, how to check for stale docs, how to bootstrap context for a new repo, how to query library documentation. The AI reads the skill and follows the steps.

**4. Drift detection.**
Every companion file records the git commit of its code-partner at which it was last verified. Before a task starts, the system compares these hashes against the current code. If someone merged a change outside this workflow, the AI detects the drift and updates the companion file before planning. This does not guarantee perfect accuracy — but it makes staleness explicit and recoverable instead of silent.

These four additions have a compounding property: the first task on a codebase pays the most — researching cross-repo contracts, documenting invariants, writing companion files where none existed. Every task after that starts with that context already in place. Without companion files, every session pays the same O(n) search cost: the AI greps, follows imports, reads files — often re-discovering relationships it found last session. Those search results also add noise to the context window, degrading the model's performance on everything else it is holding (the same mechanism Du et al. measured — longer input, worse output, even when the answer is present). With companion files, that research is replaced by a single O(1) curated read — less context consumed, higher signal-to-noise ratio, better output. The system gets cheaper with use, not more expensive.

---

## When to use which workflow

| Situation                                                                       | Workflow                                                                           |
| ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Single-file changes, small fixes, any non-code changes (docs, configs, READMEs) | Light task workflow — plan in a task file, approval gate, then implement           |
| Multi-file refactors, cross-repo features, migrations, complex bug hunts        | Full task workflow — plan, companion file updates, drift detection, approval gates |

The overhead scales with complexity. The light workflow adds a plan and an approval gate — minimal overhead, but enough to keep the AI aligned. The full workflow adds companion file maintenance and drift detection on top. Every change goes through one of these two workflows.

---

## Your role

The AI creates companion files, updates them at workflow transition points, runs drift detection, and maintains task state. You review their accuracy — the same responsibility you have for any AI-generated code. The AI automates the maintenance; you own the correctness.

But companion files are not just assets for the AI. They are assets for you. You see at a glance how a specific code file connects to other parts of the codebase — the edges, the invariants, the running tasks on that file — without having to look up and search across five different places to assemble that view. They give everyone tools to follow the breadcrumbs, explore, and get familiar with parts of the project they usually don't work in.

This is especially valuable for new contributors. When you don't know where to start looking, the onboarding folder mirrors the source code structure. Pick a repo, read the overview, follow cross-repo edges to adjacent repos. Each companion file shows what that code connects to, what must not break, and which specifications define its behavior. This is the guided tour that usually only exists in someone's head.

For experienced developers, companion files are easy to validate at a glance. When something is wrong or missing, correcting it strengthens the system for every future session — yours, another developer's, or the AI's. Adding your own perspective builds the net further. This is a system that strengthens itself with every developer working in it.

---

## Getting started

The AI loses context, drifts from plans, and forgets between sessions. This system gives it structured memory that persists — and gets better with every task. The practical setup steps are in [GETTING_STARTED.md](GETTING_STARTED.md).
