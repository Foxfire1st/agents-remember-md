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

## Onboarding Usage

When working with code make sure to read the corresponding onboarding file alongside it. The code path tells the agent where to find the onboarding, and the onboarding file name matches the code file name.

For example, if you are working with `src/js/libs/device_connection/apiRequests.ts`, read `<AR_MANAGEMENT_ROOT>/onboarding/device-management/helpdesk/src/js/libs/device_connection/apiRequests.md` for the relevant commentary.
Note that onboarding repositories may be segmented by domain (e.g., `helpdesk`) to group related files.

When a task uncovers durable current-state knowledge, record that knowledge in onboarding during implementation as soon as the finding is stable enough to state accurately, or at latest in the immediate post-implementation documentation pass. Do not rely on rediscovering the same behavior in a later session.

## heavy-task-workflow

Operational references:

1. `skills/**`
2. `skills/W-01-heavy-task-workflow/workflow/**`

### Process Guard Rails

1.  Use live skills and workflow docs as the operating spec for phase order, artifact names, ownership, and checkpoint gates.
2.  Start from the live repo state and the current task. Change only what the task and workflow require.
3.  Preserve existing behavior, helper structure, and compatible guidance unless the task or workflow explicitly changes, relocates, or retires them.
4.  For `changed-in-place` and `moved` surfaces, implement the required delta. Do not collapse surviving content to the projected slice.
5.  Planning is scheduling-only. Do not introduce new design or contract content there.
6.  Implementation is sequential by default. Record issues first; do not silently promote them into requirement or architecture changes.
7.  Checkpoint reviews are review-only. They assess artifacts and findings; they do not rewrite working artifacts or approvals.

### Workflow Development Guard Rails

Workflow design reference when changing the workflow itself: `<AR_MANAGEMENT_ROOT>/onboarding/heavy-task-workflow/overview.md` when that onboarding exists and has passed drift qualification.

1. Use onboarding docs when developing or changing the workflow itself, not as the primary operational source for normal coding tasks.
2. Keep separation of concerns: `SKILL.md` owns entrypoint guidance, workflow docs own phase behavior, templates own scaffolds, and validation or check assets own verification.

## No Code Changes Before Explicit Developer Approval

When asked to find a sollution to a problem, do not change any code before you have explained your sollution in chat with code examples for all distinct changes you intend to make.
**Then wait for developer approval before touching any code!**

## Chat Based Coding Workflow

1. When planning code changes against onboarding documentation, you MUST invoke `C-02-onboarding-drift-detection` to find all drifted onboarding files. Do not plan against drifted or missing-verification onboarding until the drift report has been handed off to `C-05-create-or-update-onboarding-files` or the caller has explicitly accepted directional-only trust. Do not skip this step! It is critical to maintain onboarding integrity and prevent untracked drift.

2. Then after `C-05-create-or-update-onboarding-files` has updated the required onboarding context you can investigate and plan the changes, which then you show to the developer in chat including code examples for every distinct change you intend to make. Wait for explicit developer approval before you start changing any code.

3. After approval apply the code changes, route durable findings and onboarding refresh through `C-05-create-or-update-onboarding-files` during implementation whenever the discovery is already clear, finish any remaining onboarding pass through that skill immediately after implementation, and use the appropriate code quality checks from `<AR_MANAGEMENT_ROOT>/system/tools.md`.
