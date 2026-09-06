# Agents Remember Source Checkout Instructions

This repository is the source package for Agents Remember. It is not the live
coordination runtime after installation.

If this file is being read from a workspace-level pointer while you are working
on a sibling repository, use the installed runtime instructions instead:

```text
<workspace>/ar-coordination/AGENTS.md
```

When working on this repository itself, use `agents-remember` as the target
code repository for resolver, onboarding, workflow, and closeout commands.

## Start Here — Route By Role

Sessions route by role through the `l-01-agent-lifecycles` skill. A spawned
agent follows its brief. A developer-facing session is free chat: it answers
research inline and, for ordinary role-shaped work, creates or resolves a durable sprint
and first leaf, compiles `templates/architect-brief.md`, and calls
`dispatch_agent` once on that sprint document with role `architect`. The
launcher hands over after the complete brief is durably pinned; it is never a
global architect identity and never calls the internal session primitive.
An explicit developer-declared task-seat takeover instead dispatches the named
role on that role's canonical task document, as defined by the lifecycle skill.
Classify the job (bug / feature / triage / research) as a *lens* during
reframe-research — a hint, re-pickable, never a gate.

The build decision is taken at `decide`. Chat is never a build route — every
code change lives under an approved task doc:

1. **Research-only exit** — answers or assessments that change no code: no
   worktree, no task file; chat is the right medium.
2. **Durable task** — `w-02-light-task-workflow`: a task document with
   checklist, decision log, and proposed code examples; small code work takes
   the minimal `w-02-light-task-workflow` artifact, and the work escalates to a
   master + light sub-task series when it outgrows a single-page plan.

The task-collaboration doctrine in `tasks/AGENTS.md` applies inside the
architect lifecycle's reframe-research phase, in plain chat, before any task
file or format is chosen.

---

**IMPORTANT:**
Do not change code or documentation without entering the architect lifecycle and clearing its plan gate.
Do not change task plan items without approval. Think before acting.
Do not randomly commit. Use the `c-12-closeout` skill instead!

---

## Memory And Onboarding

Before relying on onboarding, task files, docs, or tools, resolve the active
Agents Remember context with `c-08-ar-coordination-context-resolver`.

For this source checkout, the normal resolver input is:

```text
code_repository_name = agents-remember
```

After the `c-08-ar-coordination-context-resolver` skill resolves the target repository and coordination root, prefer the
Agents Remember `context_packet` MCP tool when that server is configured.
Provider authority comes from the MCP settings file.

If the MCP settings configure providers, request:

```text
context_packet(repo_id="agents-remember", include_providers=true)
```

Skip this provider check when no MCP server is configured or the MCP settings
report no providers.

Then run `c-02-memory-quality-control` for the resolved context before
reasoning from onboarding or source files.

After the `c-08-ar-coordination-context-resolver` skill resolves `memory_root`, read that memory layer's repository-specific
guidance:

- `system/settings.md`
- `system/settings.json`
- `system/tools.md` for repo-specific tools, commands, and code quality checks
- `system/sources.md`
- `system/coding-guidelines.md`, when present

Do not assume this source checkout has active root-level `system/` settings.
Runtime settings examples live under `runtime/system/defaults/`, and installed
runtime settings live under the selected `ar-coordination/` or memory root.

### Memory Retrieval Strategies

- `Semantics`: Fuzzy search use GrepAI to search over onboardings. Leads to code routes & files using 1-to-1 file mapping backward.
- `Relationship`: For code-relationship questions use Code Graph Context (cgc).
- `Intent`: an anchor/location + relationships are known, but hidden contracts, invariants,
  branch-valid truths, behavioral expectations, or code intent are unknown. Use
  onboarding plus bounded source confirmation.

Use `c-04-retrieval-strategy-router` to understand the full benefit of the strategies as they allow you to complete the task faster.

## Source Layout

- `skills/` is the canonical skill source tree. Edit skills here first.
- `scripts/sync-skills.py` copies root `skills/` into the MCP package-data copy
  and all harness starter package skill folders. Run it after any skill edit.
- `agents-md-files/`, `benchmarks/`, `providers/`, and `system/` are canonical
  runtime asset source folders. Edit them at the root and run
  `python3 scripts/sync-runtime.py` to refresh MCP package data.
- `scripts/harness/` is the single source for the eight self-hosted harness
  starter packages (`.claude/`, `.codex/`, `.cursor/`, `.github-vscode/` with
  `.vscode/`, `.hermes/`, `.openclaw/`, `.pi/`, `.agents/`). Edit it and run
  `python3 scripts/sync-harness.py`. Its `README.md` records which differences
  between harnesses are genuine requirements and which files it does not manage.
- `mcp/` contains the MCP server, tool surface, and package-owned runtime
  services.
- `mcp/src/agents_remember/package_data/runtime/agents-md-files/` contains the
  generated package-owned copy of root `agents-md-files/`.
- `mcp/src/agents_remember/package_data/runtime/skills/` is the generated
  package-owned skill copy used by `runtime_install`.
- `mcp/src/agents_remember/package_data/runtime/providers/`,
  `mcp/src/agents_remember/package_data/runtime/system/`, and
  `mcp/src/agents_remember/package_data/benchmarks/` are generated package-data
  copies of the matching root runtime asset folders.
- `README.md` documents the current user-facing install and usage model.
- `roadmap/` contains future design notes, not active runtime behavior.

## Boundaries

- Keep this root `AGENTS.md` scoped to working on the source checkout.
- Keep installed coordinator instructions in `runtime/agents-md-files/`.
- Do not edit generated skill copies directly; edit root `skills/` and run
  `python3 scripts/sync-skills.py`.
- Do not edit generated runtime asset copies directly; edit the matching root
  folder and run `python3 scripts/sync-runtime.py`.
- Do not edit a generated harness starter file directly (each one says so in a
  header comment); edit `scripts/harness/` and run
  `python3 scripts/sync-harness.py`. Files a starter package owns alone, such as
  `.codex/config.toml` or `.cursor/hooks.json`, are edited in place.
- Keep user-specific behavior, project notes, and repo policy in the resolved
  memory layer, not in package-owned installed `AGENTS.md` templates.
- Do not create, close out, integrate, push, or clean up worktrees without the
  approval gates required by the selected workflow.
- Implementation approval is not commit approval. After checks or closeout
  dry-runs, stop and ask for explicit commit approval before running any real
  commit, closeout apply, integration, push, or cleanup command.
- Do not move protected branches unless the developer explicitly asks.

## Code Quality Instructions

Ordinary Python development uses `mcp/.venv/bin/python -m pytest`; it runs the isolated
unit population with four workers. Use `-m integration` for the small real-boundary suite
and `-m ""` for combined verification. Only the pinned Dagger graph and existing lifecycle
owners may produce certifying evidence; ordinary pytest does not acquire that authority.
The delivery graph runs both populations explicitly. Preserve its genuine admission, process
identity, disposable state, credential isolation, and exact candidate publication boundaries.

Follow `docs/design/python-pytest-bootstrap.md` for the current test policy and commands.
Every new case must protect a distinct user operation, consequential failure, or actual
regression. Extend, consolidate, or replace overlapping tests before adding cases. Private
branch coverage and metric improvement are not sufficient reasons for a test. Remove unused
fixtures/support with the redundant tests. Do not recursively run pytest or repeatedly scan
this repository to test the test suite.

`pyproject.toml` declares parametrized case budgets, enforced on selected collected items.
The combined run observes both complete populations. Budget growth needs an explicit change
tradeoff stating the distinct protection, case count, support size, and elapsed runtime cost;
never silently raise the limits. Keep integration small instead of moving unit bloat into it.

Coverage is diagnostic only: there is no mandatory percentage, changed-line floor, or test
obligation derived from an uncovered line. CRAP is reported only for production functions,
excluding tests and verification support. Its existing threshold of 20 is a review trigger,
not a delivery blocker. Address a finding through simpler production code, a meaningful
behavioral test, or a concise justified acceptance in the change description. There is no
score-exception registry, coverage ratchet, or baseline mechanism.

Lint, formatting, typing, structural rules, and test failures still enforce. Diagnostic-tool
errors are distinct from metric findings and remain visible failures. Each report must state
its scope and inputs. Preserve untracked files: quality reporting never stages or stashes them.

Complexity is enforced by `C901` plus `PLR0911`/`PLR0912`/`PLR0915`, reported by ruff
like every other rule. Arming them surfaced 67 offenders; those were parked behind a
shrink-only baseline in `quality/complexity-baseline.txt` for exactly one day before the
developer overruled it, and all 67 were refactored instead. The file, the module that
read it and the gate step that ran it are deleted. Clear a finding by extracting a
cohesive helper — a dispatch table for an if/elif ladder, a guard-clause prologue split
from the body, a parse step separated from a decide step. Never by `# noqa`, never by a
per-file ignore, never by widening a limit in `pyproject.toml`.

**Radon does not enforce anything.** `radon cc` and `radon mi` exit 0 whatever they
find — `radon cc mcp/src/agents_remember -s -n B --order SCORE` reports 141 blocks at
grade C or worse and still exits 0 — so no Radon invocation can fail a gate. Radon is a
*report* for refactor scouting, and it is load-bearing in one place that is not a gate:
`code_quality/crap_calculator.py` imports `radon.complexity.cc_visit` for the complexity
term of the CRAP score. Run it deliberately when you want the report; do not present a
green Radon run as evidence that anything passed.

Use the resolved memory layer's `system/tools.md` for the exact commands, and
`system/coding-guidelines.md` when present for repo-specific coding rules. Use
`system/code-quality-report-template.md` as a template for reporting code quality
results after implementation work changes source code; record Radon rows there as
`reported`, never as `passed`.
Before adding or editing any store, loop-over-a-store, queue, or append-only log, the
`system/coding-guidelines.md` "Stability, Bounded Resources, and Reclamation" section is
MUST-READ doctrine.
