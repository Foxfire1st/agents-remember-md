# Agents Remember

_"My agent keeps forgetting everything. So I made it write notes to its future self."_

Modern coding agents are better than they look on toy problems, and worse than they look on real ones.

On a small task, an `AGENTS.md` file, a few prompt rules, and a strong model can feel almost magical. That creates the illusion that the agent already “knows the codebase.” **In larger systems, that illusion breaks.** The agent does not actually know your architecture, your hidden invariants, your migration scars, or the strange rules everyone on the team has learned the hard way. It only knows what the repository makes legible.

That is why the failures are so weird. The output looks plausible. The edit is clean. **The regression is real.**

A single top-level instruction file can point the agent in the right direction, but it cannot reappear exactly when the agent needs it. **Once the agent is deep in a file, the relevant context is no longer naturally in front of it.** Recovering it becomes an explicit search problem: expensive, uncertain, and easy to skip.

It is like handing someone a city map at the train station and taking it away before they start walking. The problem is not that they never saw the map. The problem is that it is gone when they need one next turn.

That's where Agents Remember simple premise starts: important project knowledge should not have to be hunted down. If it is not local, structured, and discoverable, **then for the agent it effectively does not exist.**

So the way forward is to make that missing context visible before the agent has to guess.

![alt text](agents-remember-infographic.png)

## Why I made this repo

Imagine you work in a multi-repo product workspace. Configurator, firmware, user & device management, cloud services, etc. All of it revolving around one product. And some of the code has been growing for decades.

That is exactly the kind of environment where agents struggle. A small issue is fine. But for migrations or cross-repo changes, the important knowledge is rarely in one file. It is spread across repos, conventions, old decisions, and domain-specific quirks. Asking an agent to rediscover all of that from scratch every time either blows up the context window or produces shallow answers.

The idea came from our embedded code. Many files had large comment sections at the top: who changed what, when, and what strange behavior mattered. At first that looked excessive. But as I browsed, I realized those comments let me understand code I had never worked in before. I could read some, sure, but the commentary gave me the shape of the system much faster than code alone would have.

I wanted that same effect for developers working with agents. But without the risk of introducing "noise" into source files for experienced developers, just because it is helpful for agents. Also commentary in files can go stale without anyone noticing. So this repo keeps the extended commentary layer separate, while still making it easy to find: one markdown file per source file, mirrored by path.

That 1-to-1 mapping is the trick. If an agent is working on `src/foo/bar.ts`, it knows exactly where to look for `<onboarding-root>/my-app/src/foo/bar.md`. No secret wiki, no guessing, no giant context dump. The agent can onboard itself from the file it is touching and discover the hidden contracts around it naturally.

That is what this repository is trying to make practical: a collaborative knowledge layer that grows as work happens. Documentation stops being a second job and becomes a trail of useful context left behind by real tasks.

The onboarding files are a shared knowledge substrate. Versioned in git, readable by people, and easy for agents to retrieve. That transfer of knowledge between developers, tools, and future sessions is the heart of this project.

## Techstack

```text
Skills for for Claude Code, Cursor, VS Code etc. No software dependendencies.
Just markdown files and conventions.
```

## Quickstart

Clone this repository somewhere next to your existing code — it doesn't go inside any of them. The memory artifacts live in a management root beside this repo by default:

```text
projects/
  agents-remember/        ← this repo
    AGENTS.md
    .env.example
  ar-management/          ← local/team memory root
    onboarding/
      my-app-onboarding/
        src/
    tasks/
    docs/
    system/
      settings.md
      sources.md
      tools.md
  my-app/                 ← your existing repo
    src/
```

### Configure The Management Root

The default setup expects an `ar-management` folder beside this repo. The default is shown in `.env.example`:

```dotenv
AR_MANAGEMENT_ROOT=../ar-management
```

Relative paths resolve from the `.env` file in the agents-remember checkout. To use a different location, copy `.env.example` to `.env` and edit the value. The `.env` file is intentionally ignored because it is local machine configuration.

Scaffold the management root like this:

```text
ar-management/
├── onboarding/
│   └── my-app-onboarding/
├── tasks/
├── docs/
└── system/
    ├── settings.md
    ├── sources.md
    └── tools.md
```

Copy or rename the files in `system/*.example.md` into `ar-management/system/` as `settings.md`, `sources.md`, and `tools.md`. The onboarding root can contain newly created onboarding folders or cloned onboarding repositories that your team version-controls.

The steps are the same regardless of which tool you use:

1. Wire up the agent so it reads `AGENTS.md` from this repo at session start (tool-specific instructions below).
2. Run `C-03-repo-bootstrap` to scaffold the initial onboarding structure in `<onboarding-root>/my-app-onboarding/`. A bare `overview.md` is enough — the agent fills in depth as it works.
3. Start using the agent normally. Chat handles most tasks. The agent reads companion files alongside source files and updates them as it goes.
4. Escalate to `W-02-light-task-workflow` or `W-01-heavy-task-workflow` when the task needs a written plan or needs to survive beyond a single session.

Coverage builds from real work. The first task on any file writes the companion; every task after reads it.

### Claude Code

Add a `CLAUDE.md` at the root of each code repository:

```markdown
# my-app

@../agents-remember/AGENTS.md
```

Claude Code imports the file into context at session start. When a skill applies, the agent reads the corresponding `SKILL.md` using its normal file tools — no extra configuration needed since `agents-remember` is already accessible on disk.

### Cursor

Create `.cursor/rules/agents-remember.mdc` in your code repository:

```markdown
---
description: Agents Remember memory system conventions
alwaysApply: true
---

@../agents-remember/AGENTS.md
```

Alternatively, use Cursor's built-in GitHub import to sync rules directly from this repo. Skills are read on demand by the agent using standard file access.

### VS Code + GitHub Copilot

Open (or create) a `.code-workspace` file that includes both repositories as folders. Copilot needs the skills directories listed explicitly in `chat.agentSkillsLocations` — without this setting it won't discover them:

```json
{
  "folders": [{ "path": "agents-remember" }, { "path": "my-app" }],
  "settings": {
    "chat.agentSkillsLocations": {
      "agents-remember/skills": true,
      "agents-remember/skills/U-01-core-skills": true,
      "agents-remember/skills/W-01-heavy-task-workflow": true,
      "agents-remember/skills/W-02-light-task-workflow": true,
      "agents-remember/skills/P-00-creation": true,
      "agents-remember/skills/P-01-research": true,
      "agents-remember/skills/P-02-synthesis": true,
      "agents-remember/skills/P-03-design": true,
      "agents-remember/skills/P-04-planning": true,
      "agents-remember/skills/P-05-implementation": true,
      "agents-remember/skills/P-06-closing": true,
      "agents-remember/skills/P-99-review": true
    }
  }
}
```

You can add a `.github/copilot-instructions.md` in the code repo to layer on any repo-specific overrides.

### Windsurf

Add both repositories to your workspace. Windsurf automatically discovers `AGENTS.md` files within the workspace tree and reads skills on demand from there. You can add repo-specific additions in `.windsurf/rules/*.md` inside the code repo if needed.

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
   `<AR_MANAGEMENT_ROOT>/system/tools.md`.
```

No task folder, no phase structure. The same discipline the heavier modes enforce through artifacts is carried by chat turns.

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
- `<onboarding-root>/heavy-task-workflow/` — this workflow's self-documentation, written in its own format when available
