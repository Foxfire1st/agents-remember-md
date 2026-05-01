---
name: C-00-initialize-management-root
description: "Initialize the Agents Remember management root for a fresh clone or incomplete local setup. Use when AR_MANAGEMENT_ROOT, ar-management, system/settings.md, system/sources.md, system/tools.md, onboarding, tasks, docs, or notes are missing and Codex needs to create the minimal scaffold before onboarding, drift detection, task tracking, or repo bootstrap can work."
---

# C-00 Initialize Management Root

Create the minimal local `ar-management` scaffold expected by `agents-remember-md/AGENTS.md`.

This skill is for first-run setup and repair of missing management-root infrastructure. It does not create repo onboarding or files under `onboarding/`; use `C-03-repo-bootstrap` after this scaffold exists.

## Inputs

- `agents_repo`: path to the `agents-remember-md` checkout. Default to the repo containing the active `AGENTS.md`.
- `management_root`: optional override. If omitted, resolve it from `.env`; if `.env` is absent, use `.env.example`.
- `mode`: `create-missing` by default. Use `repair` only when the user explicitly asks to fix existing scaffold files.

## Safety Rules

1. Never overwrite an existing management file without explicit user approval.
2. Create missing directories and files only.
3. Keep starter files generic; do not invent project-specific tools, docs, sources, or onboarding.
4. If the resolved management root points outside the intended workspace, state the resolved absolute path before writing.
5. If `.env` is absent, do not create it unless the user explicitly asks. The default `.env.example` path is enough for first-run scaffolding.

## Procedure

### 1. Resolve The Root

1. Read `<agents_repo>/.env` if it exists; otherwise read `<agents_repo>/.env.example`.
2. Parse `AR_MANAGEMENT_ROOT=<path>`.
3. Resolve relative paths from the file that declared the value.
4. If neither file exists or no value is declared, use `../ar-management` relative to `<agents_repo>` and state that fallback.

### 2. Inspect Existing State

Check for these paths under the resolved root:

```text
system/settings.md
system/sources.md
system/tools.md
onboarding/
tasks/
docs/
notes/
```

Report which are present and which are missing. If everything exists, stop with a clean summary.

### 3. Create Missing Directories

Create only missing directories:

```text
<AR_MANAGEMENT_ROOT>/
  system/
  onboarding/
  tasks/
  docs/
  notes/
```

`notes/` is optional scratch space, but creating it keeps the common local layout consistent.

### 4. Create Missing Starter Files

Create only files that do not already exist.

#### `system/settings.md`

```md
# Settings

This management root stores local durable context for Agents Remember.

## Path Contract

`AR_MANAGEMENT_ROOT` is resolved from `agents-remember-md/.env`. If `.env` is absent, the default comes from `agents-remember-md/.env.example`.

Relative `AR_MANAGEMENT_ROOT` values resolve from the file that declares them.

## Scaffold

| Layer | Location | Purpose |
| --- | --- | --- |
| settings | `system/settings.md` | Path contract and management-root scaffold notes |
| sources | `system/sources.md` | External and domain documentation registry |
| tools | `system/tools.md` | Repo-specific commands, checks, and local tool notes |
| onboarding | `onboarding/` | Durable repo and file-level code commentary |
| tasks | `tasks/` | Current task plans, decision logs, and implementation notes |
| docs | `docs/` | Local domain docs, mirrors, and reference material |
| notes | `notes/` | Scratch observations that are not durable onboarding yet |
```

#### `system/sources.md`

```md
# Sources

## Domain Documentation

No domain documentation configured yet.

Add project-specific docs, local mirrors, API references, and canonical source links here before creating durable onboarding that depends on external behavior.

## External References

No external references configured yet.

## Notes

- Prefer local mirrors for reading when available.
- Link onboarding `Docs References` rows to canonical source URLs when a canonical online reference exists.
- If no relevant domain documentation exists for a task, record what was checked instead of implying the search space was complete.
```

#### `system/tools.md`

```md
# Tools

## Checks

No repo-specific checks configured yet.

Add test, lint, typecheck, build, and smoke-check commands for each onboarded repo.

## Commands

No repo-specific commands configured yet.

## Runtime Notes

Record environment setup, local service assumptions, MCP notes, and command caveats here.
```

### 5. Report Result

Summarize:

- resolved `AR_MANAGEMENT_ROOT`
- directories created
- files created
- files left untouched
- next suggested skill, usually `C-03-repo-bootstrap` to create or refresh onboarding under `onboarding/`

## Common Outcomes

### Fresh Clone

Expected result: create the full scaffold and starter files, then tell the user the management root is ready for repo bootstrap.

### Partial Scaffold

Expected result: create only missing files. Preserve existing `docs/`, `tasks/`, and onboarding content.

### Existing Complete Scaffold

Expected result: make no changes and report that the management root is already initialized.
