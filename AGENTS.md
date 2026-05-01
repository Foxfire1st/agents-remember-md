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


## Onboarding Documentation

### Trust Gate

Before relying on any onboarding file for planning, implementation, or code review, you MUST run `C-02-onboarding-drift-detection` for the relevant scope.

Do not use onboarding as authoritative until the drift result classifies it as up to date, or until drifted/missing onboarding has been refreshed through `C-05-create-or-update-onboarding-files`.

If the drift check cannot be completed, you may use existing onboarding only as directional context, and you must state that limitation before using it.

### Usage

When working with code:

1. Resolve `AR_MANAGEMENT_ROOT`.
2. Run `C-02-onboarding-drift-detection` for the relevant source files, component, or repo.
3. If drifted, missing-verification, or orphaned onboarding is found, route it through `C-05-create-or-update-onboarding-files` before relying on it.
4. Only after the relevant onboarding is up to date, read it alongside the source file.
5. During or after implementation, update onboarding for durable current-state findings.

## No Code Changes Before Explicit Developer Approval

When asked to find a sollution to a problem, do not change any code before you have explained your sollution in chat with code examples for all distinct changes you intend to make.
**Then wait for developer approval before touching any code!**

## Chat Based Coding Workflow

1. When planning code changes against onboarding documentation, you MUST invoke `C-02-onboarding-drift-detection` to find all drifted onboarding files. Do not plan against drifted or missing-verification onboarding until the drift report has been handed off to `C-05-create-or-update-onboarding-files` or the caller has explicitly accepted directional-only trust. Do not skip this step! It is critical to maintain onboarding integrity and prevent untracked drift.

2. Then after `C-05-create-or-update-onboarding-files` has updated the required onboarding context you can investigate and plan the changes, which then you show to the developer in chat including code examples for every distinct change you intend to make. Wait for explicit developer approval before you start changing any code.

3. After approval apply the code changes, route durable findings and onboarding refresh through `C-05-create-or-update-onboarding-files` during implementation whenever the discovery is already clear, finish any remaining onboarding pass through that skill immediately after implementation, and use the appropriate code quality checks from `<AR_MANAGEMENT_ROOT>/system/tools.md`.
