---
name: C-01-task-folder-creation
description: "Create the task folder after explicit developer naming choice and initialize the orchestrator-owned root artifacts without normalizing or approving intake."
---

# C-01 Task Folder Creation

This entrypoint stays lean.

It should route task start work around:

1. task folder naming choice
2. task folder creation
3. `task.md` initialization
4. `requirement_change_candidates.md` initialization
5. `architecture_open_questions.md` initialization
6. `P-00-creation/progress.md` initialization

## Rules

1. Preserve raw requirement language in staging.
2. Capture raw architecture intake without approving it.
3. Before scaffolding, present task-name options in direct discussion and wait for explicit developer selection.
4. The naming options must include a current-branch-based suggestion when available, the normal task-intent-based suggestions, and a custom option.
5. Hand onboarding maintenance off to `C-02-onboarding-refresh` instead of hiding it here.