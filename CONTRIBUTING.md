# Contributing to Agents Remember

Thanks for contributing.

Agents Remember is a markdown-first memory layer and workflow system for coding agents. It is also a substantial application: Python under `mcp/` (the MCP server, CLI, and workflow engine, plus its test suite) and a TypeScript dashboard under `dashboard/`, alongside the instructions, skills, and onboarding conventions. Contributions land in both halves, and most of them touch code.

The standard for changes is the same either way: make the system clearer, safer, and easier to apply consistently. Code additionally has to pass the quality gate described below — that part is not a matter of taste.

## What belongs here

Good contributions include:

- fixing defects in the MCP server, CLI, workflow engine, or dashboard
- consolidating tests and adding distinct protection for user operations, consequential failures, or regressions
- fixing unclear or conflicting workflow guidance
- improving skills so their scope, inputs, and outputs are easier to follow
- tightening onboarding conventions and examples
- correcting stale or misleading repository documentation
- improving bootstrap guidance for new repositories
- clarifying promotion gates, review checkpoints, or artifact ownership

If a change only makes the wording more clever but not more precise, it usually does not help.

## Core principles

Please preserve the ideas this repository is built around:

1. Drift check before planning.
2. Approval before implementation.
3. Onboarding updates only after approved changes.
4. Persistent memory should be path-derived, readable, and easy to maintain.
5. Workflow is optional; the memory layer is the product.

When contributing, prefer changes that reinforce those rules instead of introducing parallel exceptions.

## Repository shape

This repository is organized around a few distinct responsibilities:

- `mcp/src/agents_remember` — the MCP server, CLI, workflow engine, and quality wrapper
- `mcp/tests` — the bounded unit and integration suites
- `dashboard/` — the cockpit frontend; its build output is generated, is not committed, and is produced by the release job
- `scripts/` — synchronisers that keep generated copies in step with their sources
- `.githooks/` — the shared local gate, in a fast tier and a full tier
- top-level agent guidance
- reusable skills
- workflow phase assets
- onboarding and examples
- supporting reference documentation

Keep those responsibilities separate. Do not move detailed phase behavior into entrypoint guidance, and do not turn examples into normative rules unless the repo is explicitly adopting them. Never hand-edit a generated copy: change the source and re-run the matching script in `scripts/`.

## Before opening a pull request

Please make sure your change is scoped and intentional.

1. Identify the exact rule, workflow step, example, or convention that is wrong or incomplete.
2. Make the smallest coherent change that fixes that problem.
3. Update nearby examples, cross references, or self-documentation in the same pull request when they would otherwise drift.
4. Call out any behavior change clearly in the pull request description.

For larger workflow changes, open a discussion or draft pull request early instead of landing a surprise rewrite.

## Quality gates

### Development and delivery

Use `mcp/.venv/bin/python -m pytest` for isolated unit feedback, `-m integration` for
real boundaries, and `-m ""` for the combined population. Add `-n=0` when serial debugging
helps. Targeted `npm test -- <files>` provides dashboard feedback. Only the pinned Dagger
graph and lifecycle owners produce certifying evidence; host runs are development checks.
Use the existing shared engine, candidate snapshot, and genuine admission mechanism for
Dagger delivery. The exact comparison commit remains required for diagnostic diff reporting.

### Keep protection small

A test needs a distinct user operation, consequential failure, or actual regression to protect.
Extend or consolidate existing protection before adding overlap across model, helper, service,
and application layers. Keep a small number of real integration checks for ownership, data
integrity, recovery and external protocol boundaries. Delete obsolete assertions and unused
support together. Do not add recursive test runs, repeated collection, repository census
copies, or synthetic fixture worlds to establish test policy.

The provisional unit budget is 1,000 collected parametrized cases. The separate integration
budget is declared beside it in `pyproject.toml`. Collection checks these limits without
starting another collection or scanning source. Combined delivery observes both populations.
An increase needs an explicit protection/cost tradeoff in the change description: what
consequential behavior requires it, why existing tests cannot absorb it, and its case, code-size
and elapsed-runtime costs. Moving excess tests behind integration markers is not a reduction.

Coverage reporting, including changed-line coverage, is diagnostic. No percentage fails
delivery or automatically requires another test. CRAP applies only to production functions;
tests and verification support are excluded. The existing score threshold of 20 prompts
review rather than failing delivery. Simplify production code, add a justified behavioral
test, or briefly explain why the score is acceptable in the change description. Do not create
an exception registry or coverage baseline.

Lint (including C901/PLR0911/PLR0912/PLR0915), formatting, typing, structural rules and test
failures remain enforcing. Their rules, limits and scope are unchanged. Tool execution errors
remain errors even when the tool normally emits a diagnostic report. See
[`Python test policy`](docs/design/python-pytest-bootstrap.md) for isolation and commands and
[`Evidence ownership`](docs/design/python-evidence-system.md) for existing delivery ownership.

Set it up once per clone:

1. Install the dev environment: `pip install -e "mcp[dev]"`
2. Enable the shared hooks: run `./setup-hooks.sh` (or `git config core.hooksPath .githooks`)

### Local diagnostic tiers

`.githooks/pre-commit` and `.githooks/pre-push` are thin wrappers over
`.githooks/_gate.sh`, which takes the tier as its argument:

| Tier | Hook | Input state it reports | Runs | Cost |
| --- | --- | --- | --- | --- |
| `fast` | pre-commit | the staged content | generated-copy checks (skills, runtime, harness), ruff, `ruff format --check`, Pyright | about 20 seconds |
| `targeted` | pre-push diagnostic | Git's ref updates plus current-checkout bytes at index-known paths | the same deterministic non-test checks as `fast`; Dagger acceptance is not run | about 20 seconds |
| `full` | manual refusal | none | refuses host execution and points to the lifecycle-owned Dagger gate | immediate |

The fast tier enumerates Python paths with `git ls-files '*.py'` (the
staged/index population). The targeted pre-push tier repeats those deterministic
non-test checks against current-checkout bytes and records the pushed refs as
provenance. Neither tier runs pytest, Vitest, Playwright, or the Dagger acceptance
graph. The accepting targeted graph runs exactly once when a leaf closeout creates
its commit. Leaf integration lands that exact certified commit without rerunning it.
The accepting full graph runs exactly once per master, at the master integration
gate invoked by `worktree_integrate`. Host hooks do not replace either acceptance
boundary.

The pre-push hook forwards Git's four-field ref-update lines as provenance. It
does not stage, stash, mutate the index, run tests, or claim that the current
checkout is the pushed commit tree.

The fast tier is cheap on purpose. `--no-verify` is all-or-nothing: it disables
every check, not only the slow one. A pre-commit hook expensive enough to be
worth skipping therefore costs you ruff, the formatter and Pyright as well, which
is how this repository previously ended up with a gate that never ran.

To certify the staged content rather than the working tree, the fast tier parks
unstaged and untracked files with `git stash push --keep-index --include-untracked`
for the duration of the checks, and restores them from a trap that fires on
success, on failure, and on Ctrl-C. What follows from that:

- A scratch file you have not staged cannot fail your commit.
- A partially staged file is checked as staged, not as edited.
- Nothing is stashed when the working tree already matches the index, nor during
  a merge, rebase, cherry-pick, or revert — stashing there would move the
  conflict resolution out of the tree git is about to commit from. In those
  states the fast tier certifies the working tree instead and says so.
- If the hook is killed outright (`SIGKILL`, a crash, a closed terminal) the trap
  cannot run, and your work is left in a stash named
  `agents-remember pre-commit gate: staged-content isolation`. Recover it with
  `git reset --hard && git stash pop --index`.

### CI

GitHub validation is pull-request-only. Ordinary branch pushes do not launch a
second copy of the same workflows. `.github/workflows/quality-checks.yml` installs
the Python and dashboard development dependencies and runs the deterministic
non-test hook: generated-copy checks, Ruff, `ruff format --check`, Pyright,
dashboard code generation, lint, and typecheck. A finding fails the pull request.

Neither workflow runs Dagger acceptance or a host test suite. The pull request
validates the GitHub environment and merge surface; it does not spend the leaf or
master acceptance boundary again. If required status-check names change, update
the branch ruleset in the same change so the PR cannot merge without its current
checks.

### Integration scope

The retained suite uses disposable local application, process, socket and publication boundaries.
The obsolete vendor-account marker cohorts and their unconsumed runner were removed with their
tests. Current adapter tests use explicit protocol recordings and local peers; they do not claim
validation against an operator's signed-in vendor account. The existing profile-owned ambient
Codex end-to-end harness remains separate from ordinary pytest.

### Closeout

Worktree closeout runs the leaf change-set-scoped quality contract through Dagger
`mode=targeted` before creating a code commit, even when hooks are not configured.
Dagger `mode=full` runs exactly once per master, at the master integration gate,
invoked by the integration step itself. Leaf integration, push, pull-request
validation, tag, and publish do not rerun either acceptance. Agents Remember owns
this integrated wrapper, so removing it is a hard refusal at leaf closeout and master
integration. A consuming repository without this adapter instead receives the generic
`wrapper-unavailable` state and follows the executor policy in its own memory root.

## Writing guidelines

Write for both humans and agents.

- Prefer direct instructions over broad commentary.
- Be explicit about ownership, order, and scope.
- Use examples when they remove ambiguity.
- Keep examples minimal but realistic.
- Avoid duplicating the same rule in multiple places unless repeated visibility is part of the design.
- Do not add speculative guidance that describes how the system might work later. Document current behavior or clearly proposed behavior only when the file is meant to define it.

## Workflow-specific guidance

If you change a workflow, also update the surrounding material that explains it. A workflow change is incomplete if the main guidance changes but the related examples, onboarding, or review expectations still describe the old behavior.

If you change onboarding conventions, preserve the one-to-one path mirroring model unless the change is deliberately redesigning that contract.

If you add or revise a skill, keep its boundary narrow and explicit. A good skill says when to use it, what it reads, what it produces, and what it should not be used for.

## Pull request checklist

Before submitting, verify that:

- the change fits the repository’s memory-layer model
- related documentation and examples were updated where needed
- links, paths, and snippets still make sense
- new guidance does not conflict with existing workflow rules
- any breaking or behavior-changing workflow update is called out clearly

## Collaboration

Be precise, respectful, and willing to justify tradeoffs. The goal is not to accumulate more process. The goal is to keep the process that exists legible, trustworthy, and useful across many sessions and many repositories.
