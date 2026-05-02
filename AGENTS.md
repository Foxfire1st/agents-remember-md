## Memory System Awareness

This workspace uses a layered memory system. Understand the layers before acting.

Resolve `AR_MANAGEMENT_ROOT` from `.env` in this repository first. If `.env` is absent, use the default from `.env.example`: `../ar-management`, resolved relative to that `.env.example` file. The active management files then live under this scaffold:

| Layer      | Location                                  | Purpose                                                         |
| ---------- | ----------------------------------------- | --------------------------------------------------------------- |
| settings   | `<AR_MANAGEMENT_ROOT>/system/settings.md` | Derived path contract and management-root scaffold              |
| onboarding | `<AR_MANAGEMENT_ROOT>/onboarding/`        | Code commentary — logic, invariants, conventions, task tracking |
| tasks      | `<AR_MANAGEMENT_ROOT>/tasks/`             | Current change intent, plans, decision logs                     |
| docs       | `<AR_MANAGEMENT_ROOT>/docs/`              | Local domain documentation and mirrors                          |
| sources    | `<AR_MANAGEMENT_ROOT>/system/sources.md`  | References to external technical documentation, mcps, etc.      |
| tools      | `<AR_MANAGEMENT_ROOT>/system/tools.md`    | Repo-specific commands, checks, tools, and MCP notes            |

## Task Format Routing

This workspace has exactly three task/work formats. Choose deliberately before
creating or updating task artifacts.

### 1. Chat Mode

Use chat mode by default when the work is small enough to finish in the current
session and does not need a durable task file.

Chat mode is appropriate when:

- the request is a direct question, explanation, small review, or narrow fix
- the plan can be explained clearly in chat
- the work is unlikely to need multi-session continuity
- there is no need for a persistent checklist, decision log, or task-local plan

In chat mode, do not create files under `<AR_MANAGEMENT_ROOT>/tasks/` unless the
developer explicitly asks for a task artifact or the work grows beyond chat-mode
criteria.

### 2. Light Task Workflow

Use `W-02-light-task-workflow` whenever a task file is needed. This is the
standard durable-task format for planning and implementation work in this
workspace.

Light task workflow is appropriate when:

- the developer asks to "make a task", "turn this into a task", "track this",
  or create a plan/checklist for later
- the work needs a durable markdown task file under
  `<AR_MANAGEMENT_ROOT>/tasks/`
- the work may continue across sessions
- the work needs requirements, implementation steps, decision tracking, open
  questions, or approval before implementation
- the implementation plan is still compact enough to fit in one task file

When creating or updating a light task:

1. Use the `W-02-light-task-workflow` skill.
2. Search `<AR_MANAGEMENT_ROOT>/tasks/` for an existing active task covering
   the same scope before creating a duplicate.
3. Use the light-task template and status values: `planning`, `inProgress`,
   `Completed`.
4. Use checkbox-based implementation steps and substeps.
5. Include proposed code examples for each distinct implementation change when
   code changes are in scope.
6. Present the task file for developer review and stop for approval before
   implementation.

Do not create ad hoc task notes in `<AR_MANAGEMENT_ROOT>/tasks/`; task files in
that directory should follow the light-task workflow unless they are part of an
explicit heavy task folder.

### 3. Heavy Task Workflow

Use `W-01-heavy-task-workflow` only when the developer explicitly asks for the
heavy task workflow, a heavy task, or the full phased workflow.

Do not auto-escalate to heavy task based only on your own assessment. If work
looks too large or risky for a light task, say so and recommend heavy task, but
wait for explicit developer approval before invoking it.

Heavy task workflow creates a full task folder with phase artifacts such as
`task.md`, `requirements.md`, `architecture.md`,
`requirement_change_candidates.md`, `architecture_open_questions.md`, and phase
`progress.md` files. It is not the default way to make a task.


## Onboarding Documentation

Onboarding files are companion context for source files. Their main purpose is
to be read alongside the code they describe, at the moment that code is
inspected. They are not a bulk pre-read and they are not a replacement for
source.

### Hard Start-Of-Task Onboarding Gate

This gate applies to every task that touches, explains, reviews, plans around,
debugs, or changes a repository code area. Read-only analysis is not an
exception. Code explanation is not an exception. Review is not an exception.
Planning is not an exception.

Before opening, reading, summarizing, or reasoning from source file contents in
the scoped code area:

1. Resolve `AR_MANAGEMENT_ROOT`.
2. Scope the relevant repo area using discovery only: file names, directory
   listings, `rg --files`, and path searches are allowed; reading source file
   contents is not allowed yet.
3. Run `C-02-onboarding-drift-detection` once for that relevant scope.
4. If drifted, missing-verification, missing, or orphaned onboarding is found
   for the relevant pre-existing scope, stop normal task work on those files
   and route them through `C-05-create-or-update-onboarding-files`.
5. Re-run `C-02-onboarding-drift-detection` for the refreshed scope.
6. Proceed only after every pre-existing in-scope onboarding file needed for
   the task is classified as up to date. This establishes the start-of-task
   trust baseline for that scope; it does not re-trigger solely because the
   current task creates files or dirties source/onboarding within that scope.
7. Do not repeatedly drift-check each file during the same task unless the
   investigation expands materially beyond the original scope.

Do not open or inspect source file contents before this gate passes.

There is no source-only or directional bypass for establishing the initial
baseline in this workspace. If drift detection or onboarding update for
pre-existing files cannot be completed, stop and report the blocker instead of
continuing from source.

### Task-Local Working State After Gate Passes

Once the hard onboarding gate has passed for a scoped area, files created or
modified during the current task may still be opened, read, and reasoned about
within that same task even though they are now pending verification.

- Treat those files as task-local working state, not as re-verified
  onboarding.
- Do not refuse to read a file solely because the current task dirtied it or
  because the task just created its source/onboarding pair.
- Re-run the hard gate when the scope expands to additional pre-existing files
  or when a later task/session starts from that area, not every time the
  current task updates a file.
- Keep same-breath onboarding updates current so task-local drift remains
  temporary rather than ignored.

### Uncommitted Source Verification Blockers

When onboarding verification for pre-existing files is blocked because source
files have no committed state or commit hash, ask the developer whether you may
commit and push the current state. The prompt must let the developer either
provide a commit message or simply approve the commit. If the developer only
approves, inspect the files in the commit scope and write a concise commit
message based on those files. If the developer declines, report the remaining
verification blocker and continue only where the workspace rules allow.

This rule applies when establishing or refreshing verification for pre-existing
files. It does not require committing or pushing files created or modified
during the current task just to continue working.

### Paired Reading Rule

After the hard onboarding gate passes, read source and onboarding as pairs:

- When opening a relevant source file, open its verified onboarding with it.
- If the source or onboarding file was created or modified during the current
   task after the gate passed, open the current working versions together and
   mark them as pending verification in the read ledger instead of treating them
   as re-verified onboarding.
- Do not bulk-read all onboarding up front unless the source files are also
  being read then.
- Do not read source first and defer its onboarding for later.
- Do not use onboarding as a substitute for source; use both together.
- If a newly relevant file enters scope mid-task, return to the hard onboarding
  gate for that expanded scope before reading the new source file.

### Same-Breath Onboarding Updates

When changing code, update the corresponding onboarding in the same editing
pass whenever the change affects durable knowledge, including:

- purpose or responsibilities
- invariants and boundaries
- conventions
- control flow or data flow
- integration points
- known risks or TODOs
- test/check expectations

Do not postpone required onboarding updates to the end of the task. The update
should happen while the code change and its rationale are still fresh.

### Read Ledger

When working with code, include a short read ledger in chat before planning or
answering from code context. The ledger must distinguish source/onboarding
pairs actually read from files only searched, listed, drift-checked, or
inferred.

Use this shape:

```text
Read for this task:
- Source + onboarding: <source path> + <onboarding path>
- Task-local working state: <source path> + <onboarding path> - pending verification from current task edits
- Repo/component onboarding: <path>

Checked but not read:
- <path or scope> - <reason>

Not trusted / refreshed:
- <path> - <missing, drifted, missing verification, orphaned, refreshed, or user-approved bypass>
```

Only list onboarding as verified read if it was actually opened alongside the
corresponding source file after the relevant drift check passed. Files created
or modified during the current task after a passed gate may be listed as
task-local working state when they were read together, but do not present them
as verified onboarding. Do not claim onboarding informed the answer unless its
content was actually read after the relevant drift check passed, or unless you
explicitly state that it is being used only as task-local working state or as
directional context.

## No Code Changes Before Explicit Developer Approval

When asked to find a sollution to a problem, do not change any code before you have explained your solution in chat with code examples for all distinct changes you intend to make.
**Then wait for developer approval before touching any code!**

## Chat Based Coding Workflow

1. At the start of a coding workflow, scope the relevant files/component/repo area and invoke `C-02-onboarding-drift-detection` once for that scope. Do not plan against drifted, missing-verification, missing, or orphaned pre-existing onboarding until it has been refreshed through `C-05-create-or-update-onboarding-files` or the caller has explicitly accepted directional-only/source-only trust. This establishes the task-start trust baseline. Do not skip this step, and do not re-trigger it solely because the current task later creates or modifies files in that scope.

2. During investigation, read each relevant source file with its verified onboarding as a pair. If the current task has already modified or created that pair after the gate passed, read the current working versions together and treat them as pending verification rather than re-verified onboarding. Do not bulk-read onboarding as detached background context, and do not defer the onboarding read until after source interpretation. After enough paired reads, show the developer the plan in chat, including code examples for every distinct change you intend to make. Wait for explicit developer approval before you start changing any code.

3. After approval, apply code changes and update the corresponding onboarding in the same editing pass whenever the change affects durable current-state knowledge. Do not postpone required onboarding changes to the end of the task. Use the appropriate code quality checks from `<AR_MANAGEMENT_ROOT>/system/tools.md`.
