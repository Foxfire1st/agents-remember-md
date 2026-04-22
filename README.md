# Agents Remember

## What this is

Most AI coding systems give you a workflow. This one gives you a **persistent memory layer** for your codebase, and three ways to interact with it.

The memory layer is a shadow documentation tree that mirrors your source tree one-to-one. For `src/Backend/UserController.php` there's an `onboarding/src/Backend/UserController.md`. No search, no retrieval, no embedding — the doc path is derived from the code path. An agent reading a source file opens its companion file alongside. The companion captures what code can't say on its own: invariants the code assumes, conventions with social rather than syntactic enforcement, the intent behind a pattern, and cross-repo contracts that live between two repositories and are owned by neither.

The memory layer is the product. Everything else in this repo is a way to interact with it.

## The three modes

Most tasks don't need a framework. They need an agent that already knows the codebase. That's what the memory layer provides, and that's why the default mode is just **chat**.

| Mode               | When                                                                                                      | What the agent does                                                                                                                                                                             |
| ------------------ | --------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Chat** (default) | Simple tasks that fit in one session                                                                      | Reads onboarding alongside code, proposes changes with code examples in chat, implements on approval, updates onboarding                                                                        |
| **Light task**     | Medium tasks, or tasks likely to outlive one session                                                      | Writes a single-page plan to a task file, gets approval, implements, updates onboarding                                                                                                         |
| **Heavy task**     | Migrations, cross-repo contracts, changes where "looks right, breaks in production" would be catastrophic | Seven phases with review gates and adversarial checkpoints, projected code+intent before touching real code, task-local docs that promote into onboarding only after implementation is approved |

All three modes share the same three-part discipline:

1. **Drift check before planning.** Before the agent plans against an onboarding file, it verifies the file isn't stale against the source. The `C-02-onboarding-drift-detection` skill runs this check and classifies trust.
2. **Approval before implementation.** The agent proposes changes. The developer approves. No implicit approval, no "I'll just make this small edit."
3. **Onboarding update after approved changes.** Onboarding reflects approved code, not speculation. The update happens after the developer approves the change, not before.

The modes differ in _how approval happens_ — a chat turn, a task file review, a phase-gate checkpoint — not in what the discipline is. One system at three resolutions.

In chat mode, the whole loop is small enough to state in full. It lives in `AGENTS.md` and reads:

```markdown
1. When planning code changes against onboarding documentation, invoke
   `C-02-onboarding-drift-detection` to find drifted onboardings for the
   files in question. Do not plan against drifted or missing-verification
   onboarding until the drift report has been handed off to
   `C-05-create-or-update-onboarding-files` or the caller has explicitly
   accepted directional-only trust.

2. Once planned, show the changes to the developer in chat including
   code examples for every distinct change you intend to make. Wait for
   explicit developer approval before changing any code.

3. After approval, apply the code changes, update the onboarding
   documentation, and use the appropriate code quality checks from
   `docs/tools.md`.
```

No task folder, no phase structure. The same discipline the heavier modes enforce through artifacts is carried by chat turns.

## Why the memory layer changes things

An AI coding session without persistent memory starts every task from scratch. It re-reads files it read last session, re-discovers cross-repo contracts it found before, re-infers invariants that nobody wrote down. All of that rediscovery consumes context window — and context-window degradation is measurable and severe. Du et al. (EMNLP 2025) showed model accuracy drops 14–85% as input length grows even when the answer is perfectly retrievable. Liu et al. (TACL 2024) showed models attend poorly to the middle of their context, with more than 30% accuracy loss for information placed mid-window. Ord's _Half-Life of AI Agent Success Rates_ found that doubling task duration quadruples failure rate, because each mistake forces correction work that adds more noise.

Persistent memory attacks this at the root. The agent doesn't rediscover — it reads a small, relevant, curated set of companion files and starts with context already loaded. Cross-repo contracts, invariants, and migration direction are visible at read time instead of reconstructed at runtime. The first task on an area pays for the companion file. Every task after that benefits from it.

The same properties that make companion files useful to agents make them useful to developers. When returning to old code months later, reading the captured intent reconstructs context faster than re-reading the code. New engineers read the companion next to the file and see invariants, conventions, and cross-repo edges in one place instead of hunting through wikis and Slack archives.

## What makes the memory layer honest

Memory systems fail in two ways. They go stale (the code moves, the docs don't). They get polluted with speculation (an agent writes what it _planned_ to build, not what exists). This system addresses both:

**Staleness.** Each companion file records the git commit of its source file at last verification. Before any planning work, a diff against that hash tells the agent whether the file has changed. Stale companions are flagged and refreshed before the agent plans against them. This is `C-02-onboarding-drift-detection`, and it runs as the first step of every mode.

**Pollution.** The approval gate is global: no unapproved work goes into onboarding. In chat mode, the gate is the developer's approval turn. In light task, it's approval of the plan and of the implementation. In heavy task, it's the promotion step at Closure after CP5 passes. Task-local artifacts — input documentation, projected outputs, implementation plans — stay task-local until implementation is approved. Only then does anything reach the canonical onboarding tree.

Both guarantees hold across all three modes. The memory layer only accepts validated history, the same discipline git applies to `main`.

## Repository bootstrapping

Companion files don't need to exist before you can use the system. A repo with no onboarding can start with a bare `overview.md` and be scaffolded by using the `C-03-repo-bootstrap` skill. From there it can grow organically as tasks touch new areas. The first task on a file pays the cost of writing its companion; every task after that benefits.

For bulk coverage the `C-03-repo-bootstrap` skill can do more. After `overview.md` you can scaffold an entire repo in phases. Start with the hotspots and then go into detail where needed. You can bootstrap hundreds of files in a session, which is nowadays practical on current models using sub-Agents and parallelism.

## What's in this repo

- `skills/W-01-heavy-task-workflow/` — the seven-phase workflow for high-stakes tasks
- `skills/W-02-light-task-workflow/` — the single-page-plan workflow for medium tasks
- `skills/U-01-core-skills/` — supporting skills used by all modes:
  - `C-02-onboarding-drift-detection` — staleness detection (used by every mode)
  - `C-03-repo-bootstrap` — scaffold onboarding for an existing repo
  - `C-04-discovery` — top-down reading order for unfamiliar code
  - `C-05-create-or-update-onboarding-files` — the onboarding file template and maintenance
- `skills/P-99-review/` — the adversarial review package used by heavy task
- `AGENTS.md` — operational principles, including the chat-mode loop
- `onboarding/heavy-task-workflow/` — this workflow's self-documentation, written in its own format

## Comparison

The fundamental choice isn't "task workflow or memory system" — it's whether you want infrastructure that compounds across tasks or infrastructure that regenerates per project.

|                                           | Task workflow systems (BMAD, GSD, Spec Kit) | Memory systems (graphify, Ix, claude-mem) | This system                                 |
| ----------------------------------------- | ------------------------------------------- | ----------------------------------------- | ------------------------------------------- |
| **Persistent knowledge of existing code** | No                                          | Yes                                       | Yes                                         |
| **Workflow is optional**                  | No — framework is the product               | N/A                                       | Yes — default mode is chat                  |
| **Retrieval model**                       | N/A                                         | Search / graph query / embeddings         | Path-derived — doc path mirrors code path   |
| **Cross-repo edges first-class**          | No                                          | Usually no                                | Yes                                         |
| **Promotion gate for new knowledge**      | N/A                                         | No                                        | Yes — task-local → canonical after approval |
| **Staleness detection**                   | None                                        | Varies                                    | Git-anchored, per-file                      |
| **Substrate**                             | Files, various formats                      | Opaque backend or derived                 | Markdown in git, human-readable             |
| **Infrastructure compounds across tasks** | No — regenerated per project                | Yes                                       | Yes                                         |

## Getting started

The smallest possible adoption path:

1. Add `onboarding/` as a folder next to your code.
2. Write a bare `onboarding/overview.md` for your repo.
3. Put `AGENTS.md` in the root so agents read it at session start.
4. Start using an agent normally. Chat mode handles the first tasks. Onboarding grows as the agent writes companion files for the areas it touches.
5. Escalate to `W-02-light-task-workflow` or `W-01-heavy-task-workflow` when a task's complexity crosses the threshold.

The system rewards incremental adoption. You don't need to map your entire codebase before the first task — you need the conventions in place so the first task builds the first companion file, and every task after benefits.
