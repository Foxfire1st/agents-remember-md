# Getting Started

This guide sets up Agents Remember in a workspace that contains one or more code repositories.

Setup is agent-driven. Once the MCP server is wired in, you ask your agent to do the work and answer one question along the way.

## The Short Version

Ask your agent to:

1. **Copy the harness package** — Use the install guide for your harness and copy
   its native starter files from this repo into the workspace. Either run the
   copied package's `render-starter` script with a single `--repo` list, for
   example `--repo my-app shared-lib`, or replace the package placeholders by
   hand.
2. **Wire the MCP server** — Register Agents Remember MCP with this harness using
   `uvx` and the copied `agents-remember-settings.json`, then **restart the
   harness once** so it loads the MCP server, skills, hooks/rules/instructions,
   and settings from the package.
3. **Onboard your project** — Invoke `c-13-install-and-onboard`. It pre-checks
   setup, runs or verifies `runtime_install()`, sets up the memory repo (it asks:
   scaffold a new one or use an existing one), bootstraps onboarding, and starts
   provider indexing.

Your only required restart in the package-based first-run path is after copying
the harness package and wiring the MCP server.

## Restart Points

Setup is agent-driven, but the harness must restart once after package and MCP
wiring so it can load new skills, settings, hooks/rules/instructions, and MCP
tools:

| Step | Why restart? | Required? |
| --- | --- | --- |
| After copying the harness package and registering MCP | The harness loads the MCP server, copied skills, hooks/rules/instructions, and settings. | Yes |

`c-13-install-and-onboard` does not install hooks or skills, so it does not add
another restart point.

## Example Workspace

```text
projects/
  AGENTS.md
  agents-remember/
  my-app/
  ar-coordination/
```

`agents-remember` is the source checkout. `ar-coordination` is the installed runtime and local coordination area. `my-app` is the repository you want agents to work on.

## Copy The Harness Package

Different tools discover instructions, hooks, MCP settings, and skills in
different native locations. Use the install page for your harness, copy its
starter package into the workspace, and render that copied package by running
its local `render-starter` script or by replacing the placeholders manually:

| Harness | Setup guide |
| --- | --- |
| Codex | [install/codex.md](install/codex.md) |
| Claude Code | [install/claude-code.md](install/claude-code.md) |
| Cursor | [install/cursor.md](install/cursor.md) |
| Antigravity | [install/antigravity.md](install/antigravity.md) |
| VS Code + GitHub Copilot | [install/vscode-copilot.md](install/vscode-copilot.md) |
| Hermes.md | [install/hermes.md](install/hermes.md) |
| Pi.dev | [install/pi.md](install/pi.md) |
| OpenClaw | [install/openclaw.md](install/openclaw.md) |

The starter packages are intentionally copy-pasteable. They carry the initial
skills, harness-native hooks/rules/instructions, and a renderer that fills the
copied settings. The renderer infers the workspace root from the copied harness
folder. Pass every repository folder the MCP server should cover after one
`--repo`, for example:

```text
python .codex/render-starter.py --repo my-app shared-lib
```

The renderer is only a convenience for the edits you would otherwise make by
hand: workspace paths, repository names, and harness-specific hook or context
commands. If you prefer manual setup, replace those placeholders yourself and
verify that no `<PATH/TO/YOUR/PROJECTS_FOLDER>`,
`<YOUR_REPOSITORY_FOLDER_NAME>`, or hook-command placeholder remains.

## Manual Wire Of The MCP Server

Your agent should usually handle MCP setup for you. If that does not work, wire it manually. The simplest path is `uvx`, which fetches and runs the server on demand — no manual virtualenv or PATH setup. Register it with your harness by pointing the command at `uvx` and an **absolute** settings path:

```json
{
  "command": "uvx",
  "args": [
    "agents-remember-mcp",
    "--config",
    "/absolute/path/to/agents-remember-settings.json"
  ]
}
```

A minimal starter `agents-remember-settings.json`:

```json
{
  "version": 1,
  "coordinationRoot": "/absolute/path/to/ar-coordination",
  "workspaceRoot": "/absolute/path/to/workspace",
  "repositories": {
    "<your-repo-name>": {
      "certificationProfile": "config/repository-certification.json"
    }
  },
  "providers": {
    "codegraphcontext-code": {},
    "grepai-memory": {}
  }
}
```

`certificationProfile` is an explicit path inside that repository, not a conventional filename.
Create the repository-owned file for its actual commands, artifacts, runtimes, selectors, and
clean-room scenarios; do not copy the Agents Remember rail inventory into another repository.
The `c-13-install-and-onboard` flow checks and compiles it before declaring code closeout ready.
See [Repository Certification Profiles](reference/repository-certification-profile.md). A
repository without this field can still be configured and onboarded, but any later operation that
would certify code fails closed before running tests.

The settings file must be absolute and must live **outside** the
`ar-coordination/` runtime folder. See the [settings.json reference](reference/settings-json.md)
for every field. After registering or changing the server, **restart the
harness** so it discovers the MCP tool list and the copied starter package.

## Run c-13 And Install The Runtime

After the restart, invoke the copied skill:

```text
c-13-install-and-onboard
```

The skill first runs or verifies:

```text
runtime_install()
```

The runtime installer reconciles package-owned runtime files into `ar-coordination`: installed `AGENTS.md` templates, skills, provider defaults, and runtime folders. It does not create memory repos, run onboarding bootstrap, overwrite live settings, or modify tasks, notes, worktrees, memory content, or temporary artifacts. Preview with `runtime_install(dry_run=true)`.

When providers are enabled in the settings, `runtime_install` also builds or pulls their Docker images. It does **not** start indexing on its own — the `c-13-install-and-onboard` skill (or the [Providers guide](guides/providers.md)) does that.

Benchmark fixtures are optional and not installed by default. Install or refresh them with `runtime_install(include_benchmarks=true)`. The benchmark package is idempotent and preserves local outputs under `ar-coordination/benchmarks/user-runs/`.

## Skills And Hooks

The package-based first-run path gets skills and hooks/rules/instructions from
the copied harness starter package. Do not run `skills_install()` for initial
setup.

`skills_install()` remains available as a maintenance/manual MCP tool for
non-package setups or later refreshes. See the [MCP tool reference](reference/mcp-tools.md)
for that capability; it is not part of the quickstart.

## Set Up Memory

The `c-13-install-and-onboard` skill asks whether to **scaffold a new memory repo** or **use an existing one** — it does not assume you always want a fresh one. If you answer "scaffold," it runs `c-00-initialize-memory-repo` for the target repository.

By default the `c-00-initialize-memory-repo` skill creates repo-local internal memory:

```text
my-app/
  ar-memory/
    onboarding/
    docs/
    system/
      settings.md
      settings.json
      sources.md
      tools.md
```

Use external memory only when you intentionally want a separate memory repo under `ar-coordination/memory-repos/ar-<repo>/`. See [Use External Memory](guides/use-external-memory.md).

**How the resolver picks a location.** With no explicit choice, the resolver prefers repo-local internal memory (`<repo>/ar-memory/`) **when it exists**, and otherwise falls back to external memory (`ar-coordination/memory-repos/ar-<repo>/`) when *that* exists. Before either exists — a brand-new repo — resolution fails until you initialize one, which is exactly what the `c-13-install-and-onboard` and `c-00-initialize-memory-repo` skills do here. For new projects the recommended default is repo-local internal memory; once `ar-memory/` exists, the resolver prefers it for that repository.

## Bootstrap Onboarding

For a new memory repo, the `c-13-install-and-onboard` skill runs `c-03-repo-bootstrap` for the target repository. A thin `overview.md` is enough to start. Larger repositories can grow route-local overviews and file-level onboarding as work touches new areas. For token-conscious bootstraps of large repos, see [Cost-aware Bootstrap](guides/cost-aware-bootstrap.md).

## Start Providers (Optional)

If you enabled `codegraphcontext-code` or `grepai-memory`, the `c-13-install-and-onboard` skill starts the watchers so the providers index your configured code and memory. You can also do this directly:

```text
provider_watchers(action="start")
provider_status()
```

Providers are optional — memory, onboarding, drift, and task workflows all work without them. See [Providers](guides/providers.md).

## Start Working

Sessions route by role through the `l-01-agent-lifecycles` skill: developer-facing free chat answers research inline and, for ordinary role-shaped work, creates or resolves the durable sprint and first leaf, compiles the canonical architect brief, then calls `dispatch_agent` once on that sprint document with role `architect`. An explicit developer-declared task-seat takeover instead targets the named role on its canonical task document. This identity-free ambient launcher hands over only after the exact brief is durable. Later plane-hosted seats use the same tool under structural child-scope authority; a plane refusal never falls back to ambient. Spawned backend seats follow their role briefs. For normal tasks the agent should:

1. resolve the repository context with `c-08-ar-coordination-context-resolver`
2. run `c-02-memory-quality-control` before planning against onboarding
3. read the relevant onboarding beside the code
4. propose changes and wait for approval
5. implement approved work
6. update onboarding through `c-05-create-or-update-onboarding-files`

Escalate to a [durable `w-02-light-task-workflow` task or master series](workflows.md) when the work needs a durable plan that survives the session.
