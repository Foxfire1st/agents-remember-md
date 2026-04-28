# Contributing to Agents Remember

Thanks for contributing.

Agents Remember is a markdown-first memory layer and workflow system for coding agents. Most contributions here improve instructions, skills, onboarding conventions, examples, and workflow clarity rather than shipping application code. The standard for changes is simple: make the system clearer, safer, and easier to apply consistently.

## What belongs here

Good contributions include:

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

- top-level agent guidance
- reusable skills
- workflow phase assets
- onboarding and examples
- supporting reference documentation

Keep those responsibilities separate. Do not move detailed phase behavior into entrypoint guidance, and do not turn examples into normative rules unless the repo is explicitly adopting them.

## Before opening a pull request

Please make sure your change is scoped and intentional.

1. Identify the exact rule, workflow step, example, or convention that is wrong or incomplete.
2. Make the smallest coherent change that fixes that problem.
3. Update nearby examples, cross references, or self-documentation in the same pull request when they would otherwise drift.
4. Call out any behavior change clearly in the pull request description.

For larger workflow changes, open a discussion or draft pull request early instead of landing a surprise rewrite.

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
