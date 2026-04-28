# Multi-Repo Task Worktree Design Spec

## Status

Draft design note. This describes a target workflow model, not an implemented system.

## Goal

Support concurrent agent-driven tasks without local-state collisions by making task isolation explicit across both code repositories and their corresponding onboarding repositories.

The design assumes that onboarding content will eventually live in dedicated onboarding repositories instead of inside `docs_and_tasks`.

## Core Model

The unit of isolation is not a single repository and not a single Git worktree.
The unit of isolation is a **task context**.

A task context contains one repo-specific worktree per repository touched by the task.

Typical case:

- one code repository worktree
- one onboarding repository worktree

Important Git constraint:

- a Git worktree belongs to exactly one repository
- one worktree cannot span a code repository and an onboarding repository

Therefore:

- `1 task != 1 git worktree`
- `1 task = 1 task context = N repo-specific worktrees`

## Intended Repository Layout

Steady-state repository layout:

```text
Projects/
  <code_repo>/
  onboarding/
    <onboarding_repo_xyz>/
```

Task-isolated working layout:

```text
Projects/
  _worktrees/
    <task-id>/
      code/
        <code_repo>/
      onboarding/
        <onboarding_repo_xyz>/
```

The `/code` and `/onboarding` split is intentional. It tells the broader story that the task may cross repository boundaries even when the primary implementation happens in one code repository.

## Design Principles

1. A code-changing task is assumed to have onboarding impact by default.
2. Code and onboarding history remain in separate repositories with separate commits.
3. Task-local dirty state must not leak between unrelated chats or tasks.
4. The task must still feel like one coordinated unit even though it spans multiple repositories.
5. Cleanup happens only after both code and onboarding sides have been wrapped up or explicitly deferred.

## Task Context Contract

Each task context should resolve a repo-worktree map instead of a single path.

Example:

```json
{
  "taskId": "<task-id>",
  "repos": {
    "<code-repo>": {
      "kind": "code",
      "branch": "<task-branch>",
      "worktreePath": "Projects/_worktrees/<task-id>/code/<code-repo>"
    },
    "<onboarding-repo>": {
      "kind": "onboarding",
      "branch": "<task-branch>",
      "worktreePath": "Projects/_worktrees/<task-id>/onboarding/<onboarding-repo>"
    }
  }
}
```

This map is the task-scoped execution context.

## Default Workflow

### 1. Resolve Task Scope

For any code-changing task:

- identify the primary code repository
- identify the corresponding onboarding repository
- allocate one worktree per touched repository

Default assumption:

- if code changes in one repo, the matching onboarding repo is in scope unless explicitly deferred by the developer

### 2. Prepare Worktrees

For the task id:

- create or reuse the code repo worktree under `Projects/_worktrees/<task-id>/code/<code_repo>`
- create or reuse the onboarding repo worktree under `Projects/_worktrees/<task-id>/onboarding/<onboarding_repo_xyz>`

Reuse is allowed only when the existing worktree is clearly compatible with the same task context.

### 3. Implement Code

Perform code investigation, planning, approval, and implementation inside the resolved code repo worktree.

Code-side validation and commits happen only in that worktree.

### 4. Refresh Onboarding From Actual Code State

After code implementation is approved and committed:

- run drift detection against the changed code files
- update onboarding in the onboarding repo worktree
- write the commit hashes of the newly changed code files into the relevant onboarding files as their verification anchors

This is the crucial benefit of separating the onboarding repo worktree: onboarding is updated against actual committed code state, not speculative local edits.

### 5. Commit Onboarding

Commit onboarding changes in the onboarding repo worktree as their own repository-local commit set.

### 6. Wrap Up

The wrap-up phase should:

- merge or publish the code repo branch according to its repo workflow
- merge or publish the onboarding repo branch according to its repo workflow
- archive any task-local artifacts if required
- remove the task worktrees unless the developer asked to keep them

## Why This Model Is Useful

### Isolation

Different chats can work on different tasks without sharing dirty state.

### Clean History

Code commits remain code-only. Onboarding commits remain onboarding-only.

### Accurate Onboarding Verification

Onboarding can point at the actual commit hashes of already-changed code instead of trying to document speculative or partially-applied code state.

### Cross-Repo Awareness Without Cross-Repo Pollution

The task still spans both repositories, but each repository remains independently manageable.

## Non-Goals

This design does not attempt to:

- force one physical Git worktree to cover multiple repositories
- collapse code and onboarding into one commit stream
- require onboarding publication at exactly the same moment as code publication

## Integration Direction

This design can be integrated into the current workflow system by evolving worktree preparation from:

- one resolved worktree path

to:

- one resolved repo-worktree map per task context

The most natural place for that evolution is the worktree preparation workflow, so downstream planning, implementation, onboarding refresh, and cleanup all operate against the same task context object.

## Open Questions

1. Should onboarding repos always mirror code repo names one-to-one, or can one onboarding repo cover multiple code repos?
2. Should onboarding repo creation be mandatory for every code repo before this workflow is enabled?
3. Should wrap-up require both code and onboarding repos to be published together, or can onboarding publication remain intentionally deferred?
4. What exact branch naming convention should tie code and onboarding repos together for the same task id?
5. Should task-local metadata record the repo-worktree map in one shared file so multiple agents can reuse the same task context safely?
