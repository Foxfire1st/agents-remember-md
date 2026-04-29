# Episodic Memory Layer — Design Notes

Working notes capturing the design of the temporal/episodic layer for `agents-remember.md`. The current system covers semantic memory (what is currently true about the code). This note covers the extension into episodic memory (what happened, when, and why).

The naming follows the cognitive-science convention: **semantic memory** is timeless knowledge about how things are; **episodic memory** is the record of specific events that happened at specific times. The core artifact of the new layer is called an **episode** — a discrete, self-contained record of what happened during one task closure.

---

## Motivation

The existing onboarding layer answers "what is true about this file right now." It cannot answer three classes of question that real users ask:

1. **When did we work on X?** — entity-anchored backward search across history.
2. **What did we do on date Y?** — time-anchored query with no entity anchor.
3. **What happened to X?** — entity-anchored query where the entity may no longer exist.

The first is handled partially by per-file `Update History` sections but is incomplete because not every task that touches a file changes its onboarding materially. The second has no current answer beyond raw git log, which describes operations rather than context. The third has no answer at all — when a file is deleted, its onboarding follows it into the grave, and the "why" context is lost.

These are orientation questions. They come up when someone returns to an area after time away, onboards to a new codebase, or picks up stalled work. The episodic layer is the infrastructure that makes them answerable.

---

## Core architecture: three substrates

The system uses three storage substrates, each matched to the access pattern it serves best.

### 1. Current-state files (existing)

Markdown in git, mirroring the source tree. Path-derived: the agent reading `src/foo/bar.php` reads `<onboarding-root>/<repo>/src/foo/bar.md`. Captures invariants, conventions, cross-repo edges, and intent — what the code can't say on its own. Deleted when the source file is deleted.

Substrate choice: markdown in git is correct here because volume is bounded by the shape of the codebase, humans read these files directly, and the git diff/history machinery is load-bearing for staleness detection.

### 2. Temporal index (new, database)

Relational database keyed on (time, entity). Holds one row per episode, plus an `entity_touches` mapping that records which entities each episode covers. The timeline is a view over this data rather than a separate materialized artifact.

Substrate choice: at realistic scale — dozens of episodes per developer per day, tens to hundreds of thousands over time — filesystem storage becomes operationally unviable. Directory listing slows, globs get expensive, git struggles with the commit graph, and backup tooling treats the repo as pathological. Databases handle this volume as their baseline case, and time-range queries, entity joins, and cross-cutting queries all become single SQL statements.

### 3. Episodes (new, database column)

An **episode** is the canonical narrative record of a discrete event — what changed, why, what was considered, what was rejected. Written once at task closure. Immutable; subsequent tasks that affect the same entities produce new episodes rather than editing old ones.

Stored as a column on the episodes table, not as separate files, for the scaling reasons above. Markdown remains the interchange format: the authoring experience produces markdown, which gets inserted into the database; agents and future GUIs render the markdown back out on demand. The database is the storage layer; markdown is the format for input and display.

---

## The two access paths

The three substrates compose through two navigation directions, both ending at the same episode rows.

**Entity-anchored queries** start at a current-state file. The update history in the onboarding file carries pointers (task IDs) to episodes that affected this entity. Agent opens the onboarding, sees the pointers, resolves the ones it cares about through database lookups, reads the full narrative.

**Time-anchored queries** go directly to the database. "What happened last week" is a SELECT with a date range filter; the agent gets back a list of episodes and reads the ones relevant to the question.

**Cross-cutting queries** — "which tasks touched both auth and payments in the last quarter" — are SQL joins against the `entity_touches` table. These were effectively impossible at the file-based scale and become trivial with the database.

Routing rule: when time is the primary anchor, database first. When entity is the primary anchor, file first, then follow pointers into the database. Every query path ends at an episode row; the database is the index and the store, but markdown remains the substrate of the narrative — just held in a column rather than in files.

---

## The episode artifact

Each episode stands on its own because it's reached from multiple directions and has to make sense to an agent arriving from any of them. Minimum contents:

- Task identifier and date range
- Trigger: what prompted the task
- Outcome: what was actually changed, in narrative form
- Entities touched, with links to their onboarding (if still extant)
- Cross-repo surfaces affected
- Decisions made and alternatives considered, briefly
- Onboarding additions/modifications that resulted from the task — the durable knowledge that survived into canonical state

That last element is what makes episodes useful years later rather than just historical curiosities. It's the "here's what we learned that's now permanent" record.

Promotion rule: not every closed task earns an episode. Trivial fixes, cosmetic changes, and abandoned explorations don't need one. A task earns an episode if its outcome has durable value beyond the immediate change — if a future developer or agent would make a worse decision without knowing this happened.

---

## Why the database earns its place beyond scale

The scale argument alone justifies the database, but the database also unlocks capabilities the file-only system couldn't have:

**Survival across entity deletion.** When a file is deleted, its onboarding is deleted with it. The episode row persists. A query for "what happened to UserController" eighteen months after its refactor still returns the episode that describes the refactor, even though no current-state file for UserController exists anywhere. The file-only system cannot do this — events survive their subjects only if the substrate is decoupled from the subjects.

**Cross-cutting analysis.** Joins against `entity_touches` reveal architectural coupling: tasks that keep requiring simultaneous changes in supposedly independent areas. This is a signal about the code that the code itself doesn't carry, and it's visible only across the time dimension.

**Rich temporal filtering.** Range queries, filtered queries ("episodes involving the payment system between March and June"), and ordered queries are native database operations. Filesystems can only approximate these through directory naming conventions that break down under any nontrivial query.

---

## Consuming the data

Agents are the first renderer and the one that matters most in the near term. An agent receiving a temporal question runs the appropriate query, gets the rows, and presents the result in whatever shape fits the conversation. The agent is inherently a rendering layer — more flexible than any static file view because it tailors the presentation to what was asked.

Dedicated GUIs (IDE plugins, Obsidian integrations, web dashboards) come later, when usage patterns reveal which views earn their own tool. Building them now is premature. The rendering layer is not part of the initial build.

Common queries should be wrapped as skill-level primitives rather than leaving agents to write SQL ad hoc: "get episodes in date range," "get episodes for entity," "get cross-cutting episodes for entities A and B." This matches the existing skill architecture — operations are invoked by name, implementation is hidden.

---

## Substrate-per-concern is deliberate

The system now uses two storage substrates: markdown-in-git for onboarding, a database for the episodic layer. This is not inconsistent; it is deliberate, and the heterogeneity is a strength.

Onboarding is markdown-in-git because humans read it, volume is bounded, and git's diff/history machinery is load-bearing. Episodes are database rows because volume is unbounded, queries are the primary access pattern, and human readability is deferred to renderers. Matching substrate to access pattern is what makes both layers defensible. A system that insisted on one substrate for everything would be worse on one of the two axes.

One-sentence justification for future reference: _onboarding is markdown because humans read it; episodes are a database because agents query them; both are anchored to the same entities through shared task IDs._

---

## Generalization beyond code

The three-substrate pattern is not code-specific. It applies to any domain where these four conditions hold:

1. Entities have stable identity (something you work on can be unambiguously named).
2. Work happens in discrete events with natural closure points.
3. Context degrades over time without deliberate capture.
4. History volume is high enough that time-anchored queries matter.

Candidate domains: infrastructure-as-code, database schemas, API contracts, legal contract work, medical records, organizational policy work, creative writing with fixed structure (chapters, scenes), research documentation.

The primary porting decision for any new domain is the entity definition. For code, entities are source files. For legal work, contracts or clauses. For medical work, conditions or treatment threads. For research, hypotheses or projects. The three-substrate architecture is invariant; entity definition is what makes it usable in a new domain.

One piece does not generalize cleanly: the `lastVerifiedCommitHash` staleness mechanism depends on the substrate (code in git) having cheap cryptographic versioning and diff operations. Most other domains lack this and would need a substitute — periodic reverification, human attestation of currency, or a versioning layer added on top. This is worth being honest about: code-in-git is doing real work that other domains would have to reconstruct.

---

## Brainstorms and other ambiguous events

Events that don't obviously attach to an entity — brainstorms, exploratory discussions, rejected proposals — fit the pattern without special treatment. A brainstorm is an event like any other. If it produces an outcome, that outcome becomes the entity future events attach to. If it rejects proposals without producing anything, the episode still records the rejections as context, and future agents proposing similar ideas can find "we considered this and here's why we didn't do it."

The task model already handles this: a task may update existing onboarding, create new onboarding, delete onboarding, or conclude without changing any current state. All four outcomes are accommodated. Events are events; entities are what events act on.

---

## Implementation discipline

Three disciplines to preserve at the episodic layer that make the onboarding layer work:

**Capture only what can't be rediscovered.** The scope rule that keeps onboarding compact applies to episodes with equal force, maybe more. Most events don't earn an episode. An episode earns its place if a future task would make a worse decision without it.

**Single source of truth per artifact.** The episode row is canonical. The timeline is a view. Onboarding update-history pointers reference the task ID, not a duplicated copy of the episode content. One canonical record, multiple access paths.

**Keep domain-agnostic skills domain-agnostic.** The skills that write episodes and maintain the episodic index should talk in terms of entity paths, not file paths. For the code binding they happen to be the same thing; for future domain bindings they will diverge. This is a low-cost discipline now that keeps porting cheap later.

---

## Open questions to resolve before implementation

1. **Schema specifics**: exact column set on the episodes table, exact shape of `entity_touches`, whether the timeline needs to be a materialized table for performance or can remain a view.
2. **Supersession handling**: when a later task substantially revises onboarding a prior episode describes, does the prior episode get an annotation pointing forward? Leaning yes, at write time of the later task, to preserve immutability while giving readers a breadcrumb forward.
3. **Skill surface**: exact set of query primitives to wrap as skills. Initial candidates: episodes-in-date-range, episodes-for-entity, episodes-cross-cutting, episode-by-task-id.
4. **Authoring flow**: precise point in the closure step where the episode gets written and inserted. Should be part of the existing Closure phase for heavy-task, and an equivalent step for light-task closure.
5. **Export path**: resolver from task ID to markdown representation, for when episodes need to exist outside the database briefly (linking in PRs, sharing with tools that don't speak the schema).
