# Scaffolding, Not Learning — Design Notes

Working notes capturing a reframing of what `agents-remember.md` actually contributes to AI-assisted work, and the collaborative-layer dynamic that makes the system compound with use.

These notes sit alongside the episodic memory design. Where that document covers the mechanics of the temporal/episodic layer, this one covers the conceptual frame — what problem the system solves, what it doesn't, and why adoption by senior engineers is the load-bearing variable for the value it produces.

---

## The core reframing

"Continuous learning for AI" is a confused framing. It bundles two distinct problems that have different solutions and conflating them leads to architectures that try to do both badly.

**Pattern recognition** — the tacit expertise that comes from exposure over time, the "this looks wrong" intuition a senior engineer has without being able to articulate the rule, the grandmaster's position evaluation, the radiologist's scan reading — genuinely requires something like weight updates. It is not propositional, cannot be written down completely, and transfers only through training. This is what people are usually gesturing at when they say someone has "experience."

**Working context and institutional knowledge** — which codebase conventions apply here, who owns what, which cross-repo contracts matter, why a particular design choice was made — is propositional and transferable. It lives perfectly well in external substrates. Trying to encode it in weights is expensive, slow, and unnecessary. Trying to re-derive it every session is wasteful and error-prone.

Most of what gets called "continuous learning" is actually the second problem, and the second problem is better solved by external memory than by weight updates. Weight-level changes are slow by design, which is a feature rather than a bug — crystallized knowledge should be stable. The fact that the sky is blue does not need to be updated daily. Skills like driving a car take years to acquire and then execute at speeds working memory cannot match. This is the correct place for weight-based learning to live.

The part that changes fast — new codebase joined yesterday, new invariant discovered this morning, new cross-repo contract renegotiated last week — is the part that belongs in external scaffolding. Different substrates for different kinds of knowing. Weights for pattern recognition, working context for active reasoning, external semantic memory for stable invariants, external episodic memory for history. The system is the composition; no single substrate can do everything well.

---

## The work-onboarding analogy

Companies hiring a senior engineer are explicitly buying two things and know they are buying two things.

They are buying **pattern recognition** — the years of exposure that let this person diagnose a stack trace in seconds. Non-transferable except through training. Took a decade to build. Cannot be handed over by a wiki.

They are buying **someone who will absorb company-specific context quickly** — the codebase conventions, domain quirks, ownership boundaries, in-flight migrations, cross-repo contracts. This part is transferable, and it is exactly what onboarding programs, internal docs, standups, and pair programming with existing seniors provide. The company does not expect the hire to re-derive this. It expects them to absorb it from the scaffolding in place.

Current AI coding assistants occupy an odd position in this split. They arrive with reasonable generic pattern recognition from training, but they have no company-specific scaffolding and no way to absorb it durably. Every session is like a senior engineer with amnesia, rediscovering which repo owns the payment logic for the twentieth time this week. The model does not need more training to do that job well. It needs the scaffolding the company gives human hires, made machine-addressable.

This framing has two practical advantages. First, it gives the architecture a concrete customer need that does not require claims about AI capabilities or the future of model training. Companies already pay for the scaffolding that makes human engineers productive — onboarding programs, wikis, mentorship structures — because the alternative is worse. The same logic applies to AI assistants. Second, it sidesteps philosophically loaded arguments about whether AI systems are "really" learning. A new hire does not "really learn" the codebase on day one either. The scaffolding is solving the same problem in both cases, so the same kind of scaffolding works.

---

## What the scope rule is really saying

The onboarding-for-humans analogy also explains why the scope rule in `agents-remember.md` is the right one, in terms that don't require re-deriving it from first principles.

Good company onboarding documentation does not explain what a for-loop does, what type systems are, or what idiomatic TypeScript looks like. The hire has that. Company onboarding explains **what is specific to this company** — the invariants, the conventions, the social rather than syntactic enforcement, the cross-cutting concerns nobody wrote into a type signature. The same discipline applies to onboarding files in this system. The model has the generic knowledge. What it lacks is what is specific to this codebase, this team, these contracts.

"Capture what code can't say on its own" is a developer-facing statement of this rule. The work-onboarding frame makes it intuitive: _write the onboarding the way a good senior would explain this codebase to a new hire — the things that aren't obvious from reading the code._ That is the scope.

---

## The collaborative layer: how the system compounds

The deeper value of the architecture is not that it stores what one person knows. It is that it accumulates what the team knows, and the accumulation happens as a byproduct of normal work.

Most documentation systems fail because documentation is a second job. Writing it competes with real work for attention, and the seniors whose contributions would matter most are the ones whose time is most valuable. So they contribute least. The wiki rots, new hires read outdated pages, the gap between "what the senior knows" and "what's written down" keeps widening.

`agents-remember.md` inverts this dynamic in one structural way: the onboarding layer is where work happens, not alongside it. In normal use, the engineer is not expected to hand-author onboarding deltas line by line. The default loop is developer plus agent: the engineer reads onboarding alongside code, fixes the issue with the agent, or routes review feedback into the agent, and the agent updates the onboarding if the change revealed something durable. If a senior who does not use the system corrects a junior through PR comments, chat, or review, that correction still enters the onboarding once the junior passes it to the agent. Manual onboarding edits remain possible for quick annotations or direct corrections, but they are an available path, not the default operating mode. There is no separate documentation task — the approval gate, the drift check, and the post-implementation update are the work flow itself. Contribution is not a tax on work; it is embedded in how the work gets done.

This creates a cascade. When a senior correction gets absorbed into onboarding, the next engineer who touches that area profits. Making a better decision than they would have on their own. It does not matter whether that next engineer is human or an AI agent. The system gets smoother with use.

Most systems degrade with use or require active maintenance to stay level. A system that improves monotonically, driven by people doing their normal work, is a rare shape.

### Where the benefit concentrates

The multiplier effect favors the weakest link in the team.

The senior already has the pattern recognition. They would make the right call whether or not the onboarding was there. The junior on their first week, the mid-level engineer working in an unfamiliar subsystem, the AI agent with no persistent memory — these are the ones who benefit most from reading onboarding that captures what a senior would say if they were looking over the work.

The senior's attention is a scarce resource. Their pattern recognition expressed in onboarding form is not. One act of senior attention becomes available to every future decision on that file, by any engineer, forever, at zero additional cost.

### What actually flows through the layer

The tacit pattern recognition itself does not flow through the system. That is trained in, not written down. What flows is the **articulable residue** of pattern recognition — the statement a senior can produce when forced to explain themselves. "This is going to be fragile because X" is articulable even when the underlying recognition is not. Written into onboarding, that statement steers every future engineer and agent away from the same mistake.

This is why correction moments matter specifically. When a senior or an agent writes onboarding from scratch without being anchored to a concrete mistake, they miss things they don't think to mention — the expertise is unconscious, so the explanation is incomplete. When a senior corrects a specific mistake, the correction is answering a specific wrong thing, which pulls the unconscious knowledge into articulable form. Every correction is a concentrated transfer of exactly the knowledge that was missing.

A system that makes corrections cheap and durable is a system that mines seniors' expertise as a side effect of them doing their jobs. This is the real mechanism.

---

## Goals and intent as entity facets

A related question that comes up when extending this thinking: does the system accommodate goals, intent, or collaborative objectives? Initial framing suggested these might need a fourth substrate alongside current-state, episodic, and temporal index.

They don't. They fit as **facets of entities**.

In the code system, the entity is a file and the onboarding is singular because files don't have facets. But entities in other domains do. A person has interests, goals, relationships, skills, context-specific preferences. A project has scope, budget, timeline, risk posture. Treating each facet as its own onboarding file under a shared entity root is clean: `entities/people/alice/goals.md`, `entities/people/alice/expertise.md`, `entities/people/alice/context.md`. Each facet is path-derived, path-derived queries compose (`entities/people/alice/*` gets everything about Alice), and the update history on each facet tracks changes to that facet independently.

This matters for the collaborative case. An AI assistant does not have persistent goals of its own across sessions, but a person does, and a goals facet on the person's entity — "here's what we're working toward, here's what we've tried, here's what's currently in progress" — is exactly the coordination layer that ongoing AI assistance currently lacks. Without it, every session starts with "what's on the menu today." With it, the shared context survives across sessions as scaffolding, not as something the model has to remember internally.

The cadence of each facet is a property of the facet, not of the entity. Expertise changes slowly. Current goals change weekly. Both use the same machinery; each moves at its natural tempo.

---

## Adoption starts from one

The system has an unusual property for something that exhibits compounding effects: **it works from the first person who uses it.**

Most systems with network effects have a dead zone before value appears. Wikis need coverage before they are worth consulting. Knowledge bases need editors before they hold anything useful. Social platforms need users before they have content. This system does not have that phase. A single engineer using it on a single file produces immediate value: the engineer-agent loop leaves behind an onboarding capture of what was learned, which is useful to that same engineer next week when they have forgotten it. Two engineers produces more value because the second benefits from the first's capture. N engineers produces multiplicative value because every capture benefits every future reader. But the curve starts above zero and increases monotonically.

### Senior contribution is automatic, not conditional

The compounding dynamic does not require seniors to affirmatively adopt the system. It requires only that seniors keep doing their normal job — reviewing, approving, denying, correcting. Their contributions arrive through the submitters they correct, whether the senior thinks of themselves as "using" the system or not.

When a senior denies a PR or flags a problem in review, the submitter now holds information they did not have before: this approach was wrong, here is what needs to change. For the submitter to get the PR approved, that information has to be acted on. If the system is the place work happens, acting on it usually means routing that feedback to the agent, having the agent update the code, and having the agent update the onboarding alongside it. Manual onboarding edits are still possible, but they are not required for the correction to land. The senior's refusal to approve becomes an onboarding update executed through the submitter-agent pair. The correction lands in the system through a different pair of hands, but it lands.

This means the common failure mode for documentation systems — "our seniors don't have time to write docs" — does not apply in the same way. Seniors are contributing through the normal channels of review work. They cannot do their job without telling subordinates what to fix and how to operate. Those instructions are the exact content the scaffolding needs. The scaffolding just routes around the documentation-as-second-job problem by catching the instructions where they naturally occur.

### More participation makes it better, not necessary

More people using the system increases the rate and breadth of accumulation. An AI-using senior contributes directly rather than via submitters, which is faster and captures more. A whole team using the system produces coverage across every area someone touches. These are improvements to the rate, not prerequisites for the system to work at all.

The honest version of the adoption story: the system delivers value at one user, gets stronger with each additional user, and reaches the compounding-cascade regime when enough of the team is using it that most code changes flow through onboarding updates. There is no adoption threshold below which it fails. There is only the trivial case where nobody uses it, which is the same failure mode as any tool.

### Two absorption paths: fast and slow

Corrections enter the onboarding through two paths, and the system remains correct even when users are imperfect about which path they use.

The **fast path** is updating onboarding while the agent still has the context of what just got fixed. This is ideal. The reasoning is fresh, the agent knows exactly what changed and why, and the update cost is low because the reasoning is already loaded. In the normal workflow this means the agent updates onboarding in the same pass as the code fix, rather than a human writing the onboarding change by hand afterward. Corrections captured on the fast path preserve both the factual content (what the code now does) and the rationale (why the change was made).

The **slow path** is drift-triggered recovery. When a user fixes code by hand without updating onboarding — or when a correction arrives through a channel the agent didn't see — the onboarding and source diverge. The next task that touches the area runs `C-02-onboarding-drift-detection` as its first step, notices that the source has moved past the recorded verification hash, flags the onboarding as stale, and routes through `C-05-create-or-update-onboarding-files` to refresh it before planning. The correction that didn't land at fix-time lands at next-task.

The slow path preserves factual content — the code at the time of the refresh reveals what state things are in — but loses some of the reasoning, because the motivating "why" is only fully recoverable while someone holds it in their head. An agent reconstructing onboarding from a code diff can describe what the code does now; it can't always describe why the change was made unless the commit message or PR discussion is explicit enough, which is inconsistent in practice.

This split gives the behavioral discipline a useful gradient rather than a binary. The rule for users is not "always update onboarding at fix time or you lose the correction." It is "always update onboarding at fix time, or you lose the _reasoning_. The state will be recovered at next touch either way." Cosmetic fixes can safely take the slow path. Load-bearing architectural corrections with a subtle rationale are worth the thirty seconds of fast-path capture because the reasoning is the part that evaporates.

The drift detector is what makes the behavioral discipline soft rather than hard. Without it, missed updates would accumulate silently and divergence between onboarding and code would cause bad decisions. With it, missed updates are recoverable at the cost of some reasoning loss and the agent's time at next touch. That is an acceptable failure mode, and it is what makes the system robust to imperfect human behavior — which is the only kind available.

Two absorption paths means the system has no single behavioral point of failure. Disciplined users get richer captures; undisciplined users still get correct captures, with reduced context. Either way, the code and onboarding stay in sync over time.

### Observed in practice

The one-user case is not theoretical for this project. The author has been operating the system as a sole user within a team that does not use it, with hundreds of onboarding files accumulated, and the automatic-contribution property works through the review loop as described: colleagues reject tickets through normal channels (PR comments, review conversations), the corrections are routed to the agent, the agent updates code and onboarding in one pass. Colleagues do not need to know the system exists for their corrections to land in the scaffolding.

This is worth citing as the specific empirical ground for the structural claims above. The "adoption starts from one" property is not a prediction about what ought to happen given the architecture; it is a description of what has been happening for the duration of the project so far.

---

## One thing the architecture doesn't cover

The scaffolding a human hire relies on is partly written down and partly embodied in other humans. "The senior engineer you can ask about the migration three years ago" is part of the institutional memory system, and it's a part external docs don't fully replace.

The episodic layer being added to `agents-remember.md` covers some of this. Past tasks become a queryable record of what was done and why, which is functionally similar to "the senior who remembers the migration." The survival of episodes across entity deletion is specifically what makes this work — the senior's memory does not evaporate when the file they worked on gets refactored out of existence.

But the distinction should stay clean. The architecture covers the docs-plus-institutional-memory layers. The "person you can ask live" layer remains what it is, at least until the system has been in place long enough that its episodic record approaches the scope of what the senior would remember. That's a reasonable long-term target but not a present-day claim.

---

## The pitch this reframing supports

The architectural contribution can be summarized without making claims about AI capabilities:

> Agents operating over long horizons in complex domains need composable memory substrates matched to access patterns. Training handles pattern recognition. External scaffolding handles working context and institutional knowledge. `agents-remember.md` is a candidate design for the semantic and episodic layers of that scaffolding — the company-onboarding and team-wiki equivalents, made machine-addressable and kept fresh as a byproduct of normal work.

For public adoption, the narrower framing remains correct: a memory system for AI coding assistants, because that gives people a concrete use case they can evaluate against their current pain. The architectural frame sits underneath as the reason the specific memory system works — it gets the substrate boundaries right, it accommodates the compounding dynamic, and it generalizes beyond code without requiring claims the implementation cannot back.

Two pitches, same architecture. The narrow one for adoption, the broader one for understanding what has actually been built.
