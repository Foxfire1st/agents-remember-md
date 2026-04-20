# Task Folder Structure

This template defines the canonical runtime layout for heavy-task-workflow task folders.

Read it during task creation and use it as the authoritative placement guide for root artifacts, phase folders, and checkpoint outputs.

## Canonical Runtime Layout

```text
tasks/YYMMDD_slug/
├── task.md
├── requirement_change_candidates.md
├── requirements.md
├── architecture_open_questions.md
├── architecture.md
├── final_report.md
├── P-00-creation/
│   └── progress.md
├── P-01-research/
│   ├── progress.md
│   ├── R-01-requirements-normalization/
│   └── R-02-input-documentation/
├── P-02-synthesis/
│   ├── progress.md
│   ├── S-01-requirement-question-framing/
│   └── S-02-architecture-question-framing/
├── P-03-design/
│   ├── progress.md
│   ├── D-01-requirement-clarification/
│   ├── D-02-architecture-deliberation/
│   ├── D-03-output-dry-run-planning/
│   └── D-04-output-documentation/
├── P-04-planning/
│   ├── progress.md
│   └── P-01-implementation-planning/
├── P-05-implementation/
│   ├── progress.md
│   └── I-01-implementation/
├── P-06-closing/
│   └── progress.md
└── P-99-review/
    ├── cp1-research.md
    ├── cp2-synthesis.md
    ├── cp3-design.md
    ├── cp4-planning.md
    └── cp5-implementation.md
```

## Naming Conventions

All task folders use a `YYMMDD_slug` format.

Examples:

1. `260318_#1588_alarm-sim-command`
2. `260318_viewmodel-adoption`

Use a short English kebab-case slug that reflects the task intent.

During task creation, the developer should be presented with naming options before the folder is created.

The required options are:

1. a branch-based option derived from the current branch when one is available
2. the normal suggested options derived from the task request under the same naming convention
3. a custom option where the developer supplies the slug or full folder name

Normalization rules:

1. keep the date prefix in `YYMMDD_` form
2. convert the slug to English kebab-case
3. preserve useful ticket identifiers from the branch or request when they improve traceability
4. if the developer provides a full folder name that already matches the canonical pattern, use it as chosen

## Root Artifact Boundaries

1. `task.md` is the global tracker, not the finalized requirements contract.
2. `requirements.md` is the approved requirement contract.
3. `architecture.md` is the approved architecture contract.
4. `requirement_change_candidates.md` and `architecture_open_questions.md` are staging artifacts.
5. `final_report.md` is written during Closing, not used as a working artifact earlier.

## Phase Artifact Boundaries

1. Input docs and output docs are separate truth layers.
2. Phase-owned working artifacts live under their owning `P-XX-<phase>/X-YY-<subphase>/` path.
3. Planning is scheduling-only and lives under `P-04-planning/P-01-implementation-planning/`.
4. Implementation executes sequentially under `P-05-implementation/I-01-implementation/`.
5. Review artifacts live only in `P-99-review/`.

## Placement Rules

1. If the artifact defines approved task-level truth, it belongs at the root.
2. If the artifact stages unresolved requirement or architecture pressure, it belongs in the root staging files.
3. If the artifact is phase-local work product, it belongs in that phase folder.
4. If the artifact is checkpoint review output, it belongs directly under `P-99-review/`.
5. Do not recreate retired `research/`, `design/`, `implementation/`, or side-review directory families.

## Recovery Guidance

For fast context recovery:

1. Read `task.md`.
2. Read the active phase `progress.md`.
3. Load additional phase artifacts on demand.

Keep the file focused on placement and boundary rules; detailed per-phase behavior belongs in the workflow files and phase-local skills.
