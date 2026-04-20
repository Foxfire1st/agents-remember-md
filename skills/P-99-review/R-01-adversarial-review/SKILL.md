---
name: R-01-adversarial-review
description: "Run an adversarial checkpoint review against the canonical cumulative task artifact chain and write one review-only artifact under P-99-review."
---

# R-01 Adversarial Review

This skill keeps checkpoint review independent from the producing skill. The reviewer grades completed phase outputs against the canonical runtime artifact chain and writes one review-only artifact under `P-99-review/`.

The package stays review-only:

1. It does not rewrite the working artifact being judged.
2. It does not auto-fix failed checkpoints.
3. It does not create parallel working documents outside the checkpoint artifact itself.

## Companion Files

1. `checkpoint-cp1-research.md`
2. `checkpoint-cp2-synthesis.md`
3. `checkpoint-cp3-design.md`
4. `checkpoint-cp4-planning.md`
5. `checkpoint-cp5-implementation.md`
6. `review-template.md`

## Checkpoint Artifacts

Write one checkpoint artifact at a time:

1. `<task-folder>/P-99-review/cp1-research.md`
2. `<task-folder>/P-99-review/cp2-synthesis.md`
3. `<task-folder>/P-99-review/cp3-design.md`
4. `<task-folder>/P-99-review/cp4-planning.md`
5. `<task-folder>/P-99-review/cp5-implementation.md` (optional)

## Review Model

1. The reviewer stays independent from the producing skill and only judges completed outputs plus the root contracts and staging artifacts needed for continuity.
2. Each checkpoint review should run with a fresh context window containing the reviewer persona, one checkpoint criteria file, the review template, and the cumulative task artifacts for that checkpoint.
3. The orchestrator should present failed or concern-bearing verdicts to the developer and wait for explicit direction before proceeding.
4. Re-review replaces the previous artifact at the same checkpoint path after the producing skill addresses the findings.

## Checkpoints

| ID    | When                                                                             | Criteria file                                                        | Output                              |
| ----- | -------------------------------------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------- |
| `CP1` | After Research, before Synthesis                                                 | [checkpoint-cp1-research.md](checkpoint-cp1-research.md)             | `P-99-review/cp1-research.md`       |
| `CP2` | After Synthesis, before Design questioning                                       | [checkpoint-cp2-synthesis.md](checkpoint-cp2-synthesis.md)           | `P-99-review/cp2-synthesis.md`      |
| `CP3` | After Design packet assembly, before Planning                                    | [checkpoint-cp3-design.md](checkpoint-cp3-design.md)                 | `P-99-review/cp3-design.md`         |
| `CP4` | After implementation planning, before Implementation                             | [checkpoint-cp4-planning.md](checkpoint-cp4-planning.md)             | `P-99-review/cp4-planning.md`       |
| `CP5` | After Implementation, before Closing when the workflow invokes the optional gate | [checkpoint-cp5-implementation.md](checkpoint-cp5-implementation.md) | `P-99-review/cp5-implementation.md` |

## Cumulative Artifact Expectations

The checkpoint criteria file defines the minimum review set. In addition to those required artifacts, the reviewer may read earlier checkpoint outputs when continuity or repeated findings need confirmation.

Canonical expectations by checkpoint:

1. `CP1`: `task.md`, `P-01-research/R-02-input-documentation/**`, and root staging artifacts created or updated by Research.
2. `CP2`: CP1 inputs plus `P-02-synthesis/S-01-requirement-question-framing/requirement-question-framing.md` and `P-02-synthesis/S-02-architecture-question-framing/architecture-question-framing.md`.
3. `CP3`: CP2 inputs plus the full Design packet under `P-03-design/`.
4. `CP4`: approved root contracts, `P-03-design/D-04-output-documentation/**`, and `P-04-planning/P-01-implementation-planning/implementation_plan.md`.
5. `CP5`: CP4 inputs plus `P-05-implementation/I-01-implementation/implementation_results.md`, implementation progress, and relevant verification evidence.

## Dispatch Guidance

When dispatching the reviewer:

1. Pass exactly one checkpoint criteria file.
2. Pass [review-template.md](review-template.md).
3. Pass the cumulative artifact set for that checkpoint.
4. Instruct the reviewer to write only the checkpoint artifact under `P-99-review/`.
5. Do not pass phase workflow docs or orchestration instructions unless the checkpoint criteria explicitly requires them.

## Verdict Handling

| Verdict              | Orchestrator action                                                                                                   |
| -------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `pass`               | Record the pass in progress tracking and continue to the developer-facing gate.                                       |
| `pass-with-concerns` | Present the checkpoint plus concerns to the developer and await direction.                                            |
| `fail`               | Stop forward movement, present the findings, and wait for explicit developer direction on fix, override, or rollback. |

## Integration Notes

1. This package preserves checkpoint review as a separate layer between producing skills and developer approval.
2. Planning has exactly one checkpoint review artifact: `cp4-planning.md`.
3. Review artifacts live directly under `P-99-review/`; the package does not use a separate legacy side-review directory.
4. The package must stay aligned with the canonical phase workflow files and the delegated package set rather than older monolithic design or planning artifacts.
