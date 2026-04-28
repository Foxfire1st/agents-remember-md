# FAQ & Design Principles

Common questions and objections about `agents-remember.md`. If you're evaluating the system and wondering whether it has a particular failure mode baked in, start here.

---

## Design principles

Four commitments shape the system. When a proposed feature conflicts with one of these, the feature loses.

**1. Piggyback on how agents already work.**
Onboarding files live in a tree that mirrors the source tree. When the agent reads `src/foo/bar.php`, it reads `onboarding/<repo>/src/foo/bar.md` alongside it. The onboarding path is derived from the code path — no search, no retrieval, no embeddings, no index. Any agent that can follow a file path can use this system.

**2. Capture what code can't say on its own.**
Onboarding documents invariants the code assumes, conventions with social rather than syntactic enforcement, the intent behind a pattern, and cross-repo contracts that live between two repositories and are owned by neither. It doesn't duplicate what the code already says or what a type signature already reveals. That scoping rule is what keeps onboarding files small and useful.

**3. Match process weight to task risk.**
Chat mode is the default and handles simple work in a single session. Light task adds a single-page plan when work will outlive a session. Heavy task runs seven phases with adversarial review gates for migrations, cross-repo contracts, and "looks right, breaks in production" changes. You pay the process cost only when the task warrants it.

**4. Onboarding only accepts validated history.**
Nothing unapproved reaches the canonical onboarding tree. In chat mode the gate is the developer's approval turn. In light task it's approval of the plan and the implementation. In heavy task it's the promotion step at Closure after CP5 passes. Task-local artifacts — input docs, projected outputs, implementation plans — stay task-local until implementation is approved. The same discipline git applies to `main`.

---

## Questions about the memory layer

### Doesn't memory get slower and more hallucination-prone as it grows?

This is the standard failure mode of retrieval-based memory — vector stores, accumulated conversation memory, semantic search over a growing corpus. It does not apply here because there is no retrieval layer.

Navigation is path-derived. When the agent reads `src/foo/bar.php`, it knows the onboarding is at `onboarding/<repo>/src/foo/bar.md`. Load cost is O(files touched in this task), not O(repo size). Adding 10,000 more files to the repo does not change the cost of any individual read.

Hallucination from memory systems usually comes from similarity retrieval returning tangentially related content that the model then weaves into its reasoning. Path-derived onboarding cannot do that — the onboarding for `bar.php` is by construction about `bar.php`. No similarity-based false positives are possible.

In practice, hallucinations decreased after adopting this system. Invariants that the agent would previously guess at — and guess wrong on roughly 20% of edge cases — are now stated explicitly in the onboarding and read alongside the code.

The README addresses the underlying context-degradation concern with research: Du et al. (EMNLP 2025) found model accuracy drops 14–85% as input length grows even when the answer is retrievable; Liu et al. (TACL 2024) found models attend poorly to the middle of their context. Persistent onboarding attacks this at the root — the agent reads a small, curated, relevant set of files instead of rediscovering context at runtime and burning through the context window to do it.

### What stops individual onboarding files from bloating over time?

The scope rule does most of the work. Onboarding captures what code can't say on its own. Most of what you'd be tempted to write down is already visible in the code or inferable from it, so it doesn't belong. In practice, file-level onboarding stays compact — the observed pattern is that a small number of load-bearing invariants, conventions, and cross-repo edges cover most of what an agent needs to not break a file.

When onboarding does grow, it's usually a signal that the source file has accumulated too much coupling or too many invariants and should probably be split. The onboarding size becomes a useful pressure gauge for the code itself.

Heavy-task mode adds another safeguard. Task-local artifacts — research notes, projected outputs, implementation plans — stay in `tasks/` until implementation is approved. Only then does anything promote into the canonical onboarding tree. Transient task noise never permanently inflates an onboarding file.

### How does the agent know the onboarding isn't stale?

Each onboarding file carries a `lastVerifiedCommitHash` and `lastVerifiedCommitDate` in its header metadata. Before any planning against onboarding, `C-02-onboarding-drift-detection` diffs the source file against the recorded hash. Four outcomes:

- **Up to date** — no diff, proceed normally.
- **Drifted** — diff exists, onboarding is flagged for refresh through `C-05-create-or-update-onboarding-files` before the agent plans against it.
- **Missing verification** — no hash recorded, treated as actionable drift.
- **Orphaned** — source file no longer exists, flagged for deletion or relocation.

Drift detection runs as the first step of every mode — chat, light, heavy. Stale onboarding can still be used directionally if the caller explicitly accepts that trust level, but the agent never silently treats stale content as current.

This is the write-eager/read-lazy pattern. The verification hash updates when onboarding is written. Drift is detected on read, scoped per file, using git diff — the same machinery already in the repo.

### How does the system handle cross-file and cross-repo coupling?

Onboarding files include two explicit reference sections:

- **`Docs References`** — canonical external or local documentation that applies to the file, with citations.
- **`Cross-Repo References`** — interfaces, shared types, event names, and contracts that cross repo boundaries, with workspace-relative links to the other side.

When a file's behavior depends on something outside itself, the onboarding records the edge explicitly. The agent follows edges when they matter, the same way an `import` statement declares what context is needed. This is deliberate — eagerly loading a transitive closure "just in case" is how retrieval systems end up with bloated context and spurious correlations. Making the edge explicit and optional keeps the read surface bounded while preserving cross-file reasoning when it actually matters.

Repo-level context lives one level up: `onboarding/<repo>/overview.md` and an optional `entities.md` catalog describe load-bearing architecture and cross-layer entities. The `C-04-discovery` skill enforces a top-down reading order — repo overview first, then component overview, then file-level onboarding, then code — so the agent doesn't brute-force its way through unfamiliar surfaces.

### Why not just use a vector store or semantic retrieval?

Retrieval-based memory has a scaling problem this system avoids by design. Top-k similarity retrieval returns related-looking content, not necessarily content that applies to the task. As the indexed corpus grows, precision drops and the model starts incorporating tangential results into its reasoning. Liu et al. (TACL 2024) documents this: models attend poorly to the middle of their context, and retrieval-bloated context is where "the middle" grows.

Path derivation sidesteps this entirely. The onboarding that applies to `bar.php` is the one at the path derived from `bar.php`. No ranking, no similarity threshold, no top-k cutoff. This is less flexible than retrieval — it can't surface "interesting unrelated patterns" — but flexibility wasn't the goal. Determinism was.

If your use case genuinely needs semantic retrieval — searching a large unstructured knowledge base, for instance — this system is not the right tool. It's designed for code repositories where filesystem hierarchy is already meaningful.

### What happens when a file is renamed or moved?

Onboarding moves with its source file. `C-05-create-or-update-onboarding-files` owns the relocation: the mirrored onboarding file is deleted or moved to match the real source tree, and overview indexes and cross-references that pointed at the old location are updated. If a repo-level entity catalog references the moved file, it's refreshed in the same pass.

The drift detector catches the case where a move happens and the onboarding wasn't relocated — the onboarding classifies as orphaned, and maintenance is routed through the standard skill.

---

## Questions about the workflow layer

### Do I need the full seven-phase heavy-task workflow to use this?

No. Chat mode is the default. It's four steps:

1. Check onboarding drift for the files in scope (`C-02-onboarding-drift-detection`).
2. Read onboarding alongside code, propose changes in chat with concrete code examples.
3. Wait for explicit developer approval.
4. Implement, update onboarding through `C-05`, run the checks from `docs/tools.md`.

Most work happens in chat mode. Light task adds a written single-page plan when the work will outlive a session. Heavy task exists for migrations, cross-repo contracts, and work where a silent mistake would be expensive to unwind.

The three modes share one discipline — drift check, approval gate, onboarding update — and differ only in how approval happens. One system at three resolutions.

### Isn't this overengineered compared to good READMEs and docstrings?

The memory layer isn't overengineered. It's a markdown file next to a source file, which is lighter than most documentation conventions. If a critic thinks the system is heavy, they're usually looking at the workflow layer and assuming the seven-phase heavy-task flow applies to every task. It doesn't.

READMEs and docstrings cover some of this, but they're human-audience documents optimized for onboarding and API reference. File-level onboarding is an agent-audience document optimized for "what will I get wrong if I touch this file." The content that belongs in each is different. Onboarding content like "this function appears idempotent but silently retries on a specific exception class" isn't something you'd put in a docstring, but it's exactly the kind of thing an agent needs to not introduce a regression.

### Why keep onboarding in a separate repo instead of inside each code repo?

Onboarding often spans multiple code repos — cross-repo contracts, shared entity catalogs, migration direction that touches several codebases at once. Keeping onboarding in one place makes those cross-repo edges first-class and searchable from one root. It also lets you share onboarding conventions, skills, and workflow definitions across every repo that uses the system without duplicating them.

The tradeoff is a two-repo setup: the onboarding repo sits next to your code repos in the same workspace. For Claude Code, Cursor, VS Code + Copilot, and Windsurf, the wiring is a few lines of configuration. The README has setup instructions for each.

### Do I have to document the entire codebase before using this?

No. Start with a bare `overview.md` and let coverage grow as tasks touch new areas. The first task on any file writes its onboarding; every task after reads it. This is the natural path for most adopters.

For bulk coverage, `C-03-repo-bootstrap` can scaffold onboarding for an existing repo in phases. A thin orchestrator dispatches parallel specialized sub-agents to scout structure, deep-dive into components, and synthesize the repo overview. The orchestrator stays context-light because sub-agents write findings directly to disk. This can cover hundreds of files in a session on current models.

### What happens when the agent discovers something wrong mid-implementation?

In chat and light modes, the agent surfaces the discovery and waits for developer input before proceeding. In heavy mode, the route is explicit: `C-01-findings-capture` decides whether the finding changes scope (`requirement_change_candidates.md`), exposes a structural decision (`architecture_open_questions.md`), or is a verified current-state clarification that should propagate to onboarding. Approved requirement or architecture changes remain explicit developer actions — one finding cannot rewrite `requirements.md` or `architecture.md` on its own.

This matters because the failure mode "agent silently promotes a mid-implementation discovery into a requirements change" is how working code ends up not matching intent. The findings-capture skill exists specifically to route discoveries to their owning artifact instead of letting them merge invisibly into the implementation.

### How does the adversarial review work in heavy-task mode?

`P-99-review/R-01-adversarial-review` runs at defined checkpoints in the heavy-task workflow. Its job is to challenge the artifact being reviewed — not to rewrite it. Checkpoint review assesses artifacts and findings; it doesn't modify working artifacts or approvals. This keeps the critique loop honest: the reviewer can't quietly turn into the author.

The checkpoints gate transitions between phases. A failed checkpoint routes work back to the owning phase to address the findings, not around the gate.

---

## Comparison and positioning

### How is this different from BMAD / Spec Kit / GSD?

Those are task workflow systems. They produce per-project infrastructure that regenerates for each new project — a task flow, templates, checkpoints. They don't carry knowledge of your existing codebase across tasks.

`agents-remember.md` is primarily a memory layer. The workflow modes are built on top of it, but the memory layer is the product. Task workflow systems and memory systems are complementary, not competing — you can use the workflow pieces of this system without the memory layer, or the memory layer with a different workflow. The comparison table in the README breaks this down axis by axis.

### How is this different from graphify / Ix / claude-mem?

Those are memory systems, and they solve a closely related problem with different mechanics. The main differences:

- **Retrieval model.** Most memory systems use search, graph queries, or embeddings. This system uses path derivation — no retrieval layer at all.
- **Cross-repo edges.** Most memory systems focus on intra-repo context. This system treats cross-repo contracts as first-class through the `Cross-Repo References` section.
- **Promotion gate.** Most memory systems accept any content written to them. This system requires explicit developer approval before anything reaches canonical onboarding.
- **Staleness detection.** Most memory systems don't verify freshness against the source. This system anchors each onboarding file to a git commit hash and detects drift per file on read.
- **Substrate.** Most memory systems store content in an opaque backend or derived format. This system is markdown in git — human-readable, diffable, reviewable, portable.

These are deliberate tradeoffs, not improvements on every axis. If your use case needs semantic retrieval across a large unstructured corpus, this system is not the right tool.

---

## What this system is not

- It is not a general-purpose knowledge base. It's scoped to code, cross-repo contracts, and what the codebase itself can't say.
- It is not a replacement for READMEs, docstrings, or architecture documents. Those serve human readers at different moments; onboarding serves agents at read time.
- It is not a retrieval system. There is no index, no embedding model, no similarity search.
- It is not a framework. It's a convention, a file template, a small set of skills, and three workflow modes. Most of the work is discipline about what goes in onboarding and what doesn't.
