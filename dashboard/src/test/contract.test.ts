import { describe, expect, it } from 'vitest';

import {
  ACTIVE_STATES,
  ATTENTION_LANES,
  ATTENTION_SEVERITIES,
  LIFECYCLE_STATES,
  PHASES,
  PROCESS_FACT_STATES,
  PROCESS_HEALTHS,
  metricsFor,
  stateCountField,
} from '../types/projection';
import type {
  EngineProcessEdge,
  LifecycleProjection,
  SeriesSubTaskNode,
  TaskSubTaskRefNode,
  WorkspaceProjection,
} from '../types/projection';
import { asServedProjection } from './servedProjection';
import snapshot from '../fixtures/snapshot.json';

// The fixture-coverage guard. `types/projection.ts` is generated from the Pydantic schema, and the
// Python codegen tests plus `scripts/sync-projection-types.py --check` enforce that producer-to-
// mirror link. `snapshot.json` remains a hand-authored sampled payload: this file measures the
// generated contract against that independent sample so required fields and runtime vocabulary
// coverage cannot disappear from dashboard fixtures unnoticed.
//
// It has to bite in BOTH directions, and it does so at three seams:
//
//   mirror ⊇ served — the server grows a field (or a bucket, or a vocabulary member) and the
//     hand-kept mirror does not. THIS IS THE DIRECTION THAT HAPPENS. It was unguarded: the
//     fixture was consumed as `snapshot as unknown as WorkspaceProjection`, a double cast that
//     turns off assignability, excess-property checking and everything else, so a served field
//     the mirror had never heard of typechecked and passed. It is how the mirror's `State` union
//     stayed five members long while the server declared six, and how `Metrics` bucketed three
//     states out of six. `mirrorMustDeclare` below is that direction, and it names the path.
//
//   served ⊇ mirror — the mirror declares something the server never sends, so a renderer
//     branch ships permanently dead. Guarded by passing the fixture through `asServedProjection`
//     (every required mirror field must be present in the payload) plus the `@ts-expect-error`
//     pins at the bottom for the two fields that actually did this.
//
//   fixture ⊇ mirror — THE ORACLE ITSELF. Both directions above are measured against a hand-kept
//     payload, so a field the fixture does not exercise is a field neither direction can see. A
//     reviewer proved the cost: deleting `stateEnteredAt` and `gate.evidenceRefs`, and emptying
//     `expectationRows` and `landing`, produced zero new `tsc` errors and a green suite. An empty
//     array is worse than a missing field — it is blindness in BOTH directions at once, because
//     `AsJsonModule` accepts `never[]` as assignable to anything and `ServedOnlyPaths<never, …>`
//     is `never`, so a *required* field added to the mirror's `ExpectationRowNode` was not
//     reported either. `fixtureMustSample` below closes that: it walks the mirror against the
//     fixture's OWN type (`typeof snapshot` — the JSON module's exact shape, empty arrays and
//     all) and names every declared path the payload does not reach.
//
// All three are TYPE-level: free at runtime, enforced by `npm run typecheck` (`tsc -b`). The
// runtime assertions below cover what types cannot — the string VOCABULARIES, which a JSON module
// import widens to `string` (see `AsJsonModule`) and which therefore no type can check here. The
// fixture must reach every registered path and every sampled value must be legal; exhaustive
// producer vocabulary belongs to generated schema/codegen rather than to one representative payload.
//
// WHAT SCHEMA CODEGEN CLOSES. The generated contract reaches two forms of producer drift that a
// sample cannot, even a sample this file polices:
//   (1) a server field that is `T | None` and currently null is *omitted* by `exclude_none=True`,
//       so no sampled payload can reveal it — only the schema can;
//   (2) a vocabulary member the server declares and no sample happens to carry. The generated
//       schema and stale-output check own that exhaustive proof; requiring this representative
//       payload to instantiate every member would duplicate the authority and amplify fixtures;
//   (3) the difference between two field-identical models. The mirror now declares
//       `SeriesSectionNode` separately from `TaskSectionNode`, matching the server's two
//       `extra="forbid"` models — but they declare the same three fields, so TypeScript's
//       structural typing keeps them interchangeable and no walk over any payload can tell them
//       apart. It is the `TaskSubTaskRefNode` / `SeriesSubTaskNode` collapse this file pins at the
//       bottom, standing one model over; generation still emits both named model declarations.

const served = asServedProjection(snapshot);

// ── the shared vocabulary of a type-level path walk ──────────────────────────
// A leaf: recursion stops here. `undefined` is in the set because an optional mirror field is
// `T | undefined`, and `null` because the wire form of a nullable one is null.
type JsonScalar = string | number | boolean | null | undefined;

type Keys<T> = Extract<keyof T, string>;

// True for an object the mirror types with a string index signature. Such a node absorbs any key
// the server puts in it, so no walk can descend into it — see `INDEX_SIGNATURE_SITES`.
type Absorbs<T> = string extends keyof T ? true : false;

// ── direction `mirror ⊇ served` ──────────────────────────────────────────────
// Every dotted path the SERVED payload carries that the MIRROR does not declare. Recurses
// through objects and arrays; an index signature on the mirror side absorbs any key, which is
// correct — the server really can put anything there. A mirror field the payload types
// differently is not this check's business; `asServedProjection` above is.
type ServedOnlyPaths<Served, Mirror, Path extends string> = Served extends JsonScalar
  ? never
  : Served extends readonly (infer ServedItem)[]
    ? NonNullable<Mirror> extends readonly (infer MirrorItem)[]
      ? ServedOnlyPaths<ServedItem, MirrorItem, `${Path}[]`>
      : never
    : NonNullable<Mirror> extends object
      ? | UndeclaredPaths<Served, NonNullable<Mirror>, Path>
        | SharedFieldPaths<Served, NonNullable<Mirror>, Path>
      : never;

type UndeclaredKeys<Served, Mirror> = Exclude<Keys<Served>, keyof Mirror>;

type UndeclaredPaths<Served, Mirror, Path extends string> = {
  [K in UndeclaredKeys<Served, Mirror>]: `${Path}.${K}`;
}[UndeclaredKeys<Served, Mirror>];

type SharedKeys<Served, Mirror> = Extract<Keys<Served>, keyof Mirror>;

type SharedFieldPaths<Served, Mirror, Path extends string> = {
  [K in SharedKeys<Served, Mirror>]: ServedOnlyPaths<
    Served[K],
    K extends keyof Mirror ? Mirror[K] : never,
    `${Path}.${K}`
  >;
}[SharedKeys<Served, Mirror>];

/**
 * Fails `tsc -b` when the mirror does not declare something the server sends, naming the path:
 * `Type '"projection.metrics.awaitingReviewCount"' does not satisfy the constraint 'never'.`
 * With several fields missing at once TypeScript prints the deferred type rather than the list;
 * declaring the ones you know about narrows it back down to names.
 */
function mirrorMustDeclare<ServedOnly extends never>(...undeclaredPaths: ServedOnly[]): void {
  expect(undeclaredPaths).toEqual([]);
}

// ── direction `fixture ⊇ mirror`: the oracle is guarded too ──────────────────
// Every dotted path the MIRROR declares that the FIXTURE does not reach. This is the dual of
// `ServedOnlyPaths` and it reads the fixture's own JSON-module type, so it sees exactly what the
// file contains: an absent key, an absent optional node, and — the case that made the fixture
// silently useless — an EMPTY ARRAY, whose element type is `never` and whose contents therefore
// satisfy every check ever written about them.
//
// A path is reported once, at the highest level that is missing: deleting a lifecycle's `gate`
// reports `projection.lifecycles[].gate`, not its eight children; emptying `expectationRows`
// reports `projection.analytics.expectationRows[]`. Arrays are keyed by TYPE, not by element, so
// one lifecycle carrying `staleSeconds` samples it for all of them — the fixture does not have to
// be uniform, it has to be COMPLETE between them.
// The scalar test comes FIRST and the empty-array test second, in that order on purpose: an empty
// array of strings samples nothing and hides nothing (there is no structure inside a string to
// walk), while an empty array of OBJECTS hides a whole model. Reversing them would demand a
// `failedPhases` entry — noise — and say nothing more.
type MirrorOnlyPaths<Mirror, Served, Path extends string> = [NonNullable<Mirror>] extends [
  JsonScalar,
]
  ? never
  : [Served] extends [never]
    ? Path
    : [NonNullable<Mirror>] extends [readonly (infer MirrorItem)[]]
      ? [Served] extends [readonly (infer ServedItem)[]]
        ? MirrorOnlyPaths<MirrorItem, ServedItem, `${Path}[]`>
        : Path
      : Absorbs<NonNullable<Mirror>> extends true
        ? never
        : [NonNullable<Mirror>] extends [object]
          ? MirrorFieldPaths<NonNullable<Mirror>, Served, Path>
          : never;

// The keys any member of the sampled union carries, and the values only those members contribute.
// Distributive on purpose: `lifecycles` is a union of two element shapes and a field present in
// either one is sampled.
type SampledKeys<Served> = Served extends unknown ? Keys<Served> : never;
type SampledValue<Served, K extends string> = Served extends Record<K, infer Value> ? Value : never;

type MirrorFieldPaths<Mirror, Served, Path extends string> = {
  [K in Keys<Mirror>]-?: K extends SampledKeys<Served>
    ? MirrorOnlyPaths<Mirror[K], SampledValue<Served, K>, `${Path}.${K}`>
    : `${Path}.${K}`;
}[Keys<Mirror>];

type UnsampledMirrorPaths = MirrorOnlyPaths<WorkspaceProjection, typeof snapshot, 'projection'>;

// THE RESIDUE, NAMED. Every mirror path this fixture does not exercise, and why. It is meant to
// stay this short; an entry added here is a field no assertion in this file can see, so it is a
// hole being accepted rather than a formality. Both directions are checked below, so an entry
// that becomes sampled — or that names a path the mirror no longer declares — fails too.
//
// Both of these are injected by the serving app at RESPONSE time (`serving/build_info.py`,
// `serving/agent_notifier_heartbeat.py`), not by the reducer, and both are absent from the persisted
// `latest-state.json` this fixture is shaped like. `store.test.ts` exercises them by construction
// instead, including the "never ticked" (`lastTickAt: null`) reading that must not render as an
// alarm — a reading a payload that always carries a heartbeat could not express.
type KnownUnsampled =
  | 'projection.servingBuild'
  | 'projection.agentNotifierHeartbeat'
  | 'projection.supervisorHeartbeat';

/** Fails `tsc -b` naming every mirror path the fixture stopped sampling. */
function fixtureMustSample<Unsampled extends never>(...unsampledPaths: Unsampled[]): void {
  expect(unsampledPaths).toEqual([]);
}

/** Fails `tsc -b` naming an allowlist entry the fixture now samples (or the mirror dropped). */
function allowlistMustStayEarned<Stale extends never>(...spentEntries: Stale[]): void {
  expect(spentEntries).toEqual([]);
}

// ── the walls of the walk, named rather than described ───────────────────────
// Every path where the mirror types a node with a string index signature. Such a node absorbs any
// key, so `ServedOnlyPaths` cannot report anything inside it and `MirrorOnlyPaths` cannot require
// anything inside it — whatever the server puts there is unwalkable by construction. This used to
// be a prose list of four in a comment; it was actually seven, and one of the three it missed
// (`GateNode.evidenceRefs[]`, a `Record<string, unknown>[]`, so EVERY key of EVERY evidence ref)
// was added by the same change that wrote the comment. `Record<AbsorbingPaths<…>, string>` is
// exhaustive and closed, so the list cannot be short again and cannot list a site that has gone.
type AbsorbingPaths<Mirror, Path extends string> = [NonNullable<Mirror>] extends [JsonScalar]
  ? never
  : [NonNullable<Mirror>] extends [readonly (infer Item)[]]
    ? AbsorbingPaths<Item, `${Path}[]`>
    : Absorbs<NonNullable<Mirror>> extends true
      ? Path
      : [NonNullable<Mirror>] extends [object]
        ? {
            [K in Keys<NonNullable<Mirror>>]-?: AbsorbingPaths<
              NonNullable<Mirror>[K],
              `${Path}.${K}`
            >;
          }[Keys<NonNullable<Mirror>>]
        : never;

const INDEX_SIGNATURE_SITES: Record<AbsorbingPaths<WorkspaceProjection, 'projection'>, string> = {
  'projection.lifecycles[].ask': "the open question's shape is the gate kind's business",
  'projection.lifecycles[].gate.packet': 'the decision packet is control-plane payload',
  'projection.lifecycles[].gate.evidenceRefs[]': 'every key of every evidence ref is opaque',
  'projection.metrics.stalenessHistogram': 'buckets are named by the reducer, not by the mirror',
  'projection.analytics.driftSnapshots[].counts': 'drift categories come from the drift report',
  'projection.analytics.setupSummaries[].resultCounts': 'result buckets come from the setup action',
  'projection.analytics.engineProcesses[].retryArgs': "the retry call's own kwargs",
  'projection.analytics.engineProcesses[].sourceLineage.recoveries[].args':
    "the contract-addressed worktree_sync call's own kwargs",
  'projection.analytics.series[].discardedSubTasks[].proof.childJson':
    'the accepted JSON source digest is preserved as an opaque proof record',
  'projection.analytics.series[].discardedSubTasks[].proof.childMarkdown':
    'the accepted Markdown source digest is preserved as an opaque proof record',
  'projection.analytics.taskDocuments[].discardedSubTasks[].proof.childJson':
    'the task projection preserves the opaque JSON source proof',
  'projection.analytics.taskDocuments[].discardedSubTasks[].proof.childMarkdown':
    'the task projection preserves the opaque Markdown source proof',
  'projection.enclosures[].lifecycleOperation.result':
    'the lifecycle operation result is the underlying closeout or integration payload',
  'projection.enclosures[].lifecycleOperation.legalControls[]':
    'each advertised operation control is an opaque tool invocation payload',
};

// ── the string vocabularies ──────────────────────────────────────────────────
// A closed string union in the mirror is a claim that the server sends nothing else. Every one of
// them is a bare `str` in `projection.py`, so the claim is NARROWER than the server by
// construction and nothing type-level can notice: the JSON import widens the payload's literals to
// `string`, so a served `severity: "critical"` assigns to `"alarm" | "warn" | "info"` in silence.
// A runtime membership check is the only thing that bites — and it used to cover two of the six
// vocabularies (`state`, `phase`), hand-written, with the other four unguarded: `severity`, `lane`,
// `EngineProcessNode.health`, and `factState` (which alone reaches six of the eleven paths below —
// the commit refs, the boot providers and the landing arc).
//
// So the registry is derived instead of listed. `ClosedUnionPaths` finds every path in the mirror
// whose type is a literal union, and `Record<…>` over it is exhaustive AND closed: a new closed
// union anywhere in the mirror fails `tsc -b` until it is bound to a vocabulary here, and a
// vocabulary bound to a path that has opened up to `string` fails too.
type ClosedStringUnion<T> = [NonNullable<T>] extends [string]
  ? string extends NonNullable<T>
    ? false
    : true
  : false;

type ClosedUnionPaths<Mirror, Path extends string> =
  ClosedStringUnion<Mirror> extends true
    ? Path
    : [NonNullable<Mirror>] extends [JsonScalar]
      ? never
      : [NonNullable<Mirror>] extends [readonly (infer Item)[]]
        ? ClosedUnionPaths<Item, `${Path}[]`>
        : Absorbs<NonNullable<Mirror>> extends true
          ? never
          : [NonNullable<Mirror>] extends [object]
            ? {
                [K in Keys<NonNullable<Mirror>>]-?: ClosedUnionPaths<
                  NonNullable<Mirror>[K],
                  `${Path}.${K}`
                >;
              }[Keys<NonNullable<Mirror>>]
            : never;

const VOCABULARIES: Record<
  ClosedUnionPaths<WorkspaceProjection, 'projection'>,
  readonly string[]
> = {
  'projection.lifecycles[].state': LIFECYCLE_STATES,
  'projection.lifecycles[].phase': PHASES,
  'projection.analytics.attentionQueue[].severity': ATTENTION_SEVERITIES,
  'projection.analytics.attentionQueue[].lane': ATTENTION_LANES,
  'projection.analytics.engineProcesses[].health': PROCESS_HEALTHS,
  'projection.analytics.engineProcesses[].codeSource.factState': PROCESS_FACT_STATES,
  'projection.analytics.engineProcesses[].codeWorktree.factState': PROCESS_FACT_STATES,
  'projection.analytics.engineProcesses[].memorySource.factState': PROCESS_FACT_STATES,
  'projection.analytics.engineProcesses[].memoryWorktree.factState': PROCESS_FACT_STATES,
  'projection.analytics.engineProcesses[].providers[].factState': PROCESS_FACT_STATES,
  'projection.analytics.engineProcesses[].landing[].factState': PROCESS_FACT_STATES,
  'projection.analytics.engineProcesses[].sourceLineage.state': [
    'current',
    'blocked',
    'unavailable',
  ],
  'projection.analytics.engineProcesses[].sourceLineage.edges[].state': [
    'current',
    'behind',
    'diverged',
    'unavailable',
  ],
  'projection.analytics.engineProcesses[].sourceLineage.edges[].side': ['code', 'memory'],
  'projection.analytics.engineProcesses[].sourceLineage.edges[].relation': [
    'super-to-master',
    'master-to-leaf',
    'super-to-leaf',
  ],
  'projection.analytics.engineProcesses[].sourceLineage.recoveries[].tool': ['worktree_sync'],
  'projection.analytics.taskDocuments[].steps[].disposition.kind': ['intentionalSkip'],
  'projection.analytics.taskDocuments[].steps[].disposition.recordedVia': ['task_doc.skip_step'],
  'projection.analytics.taskDocuments[].steps[].substeps[].disposition.kind': ['intentionalSkip'],
  'projection.analytics.taskDocuments[].steps[].substeps[].disposition.recordedVia': [
    'task_doc.skip_step',
  ],
  'projection.analytics.taskDocuments[].executionNature': ['organizational', 'atomic'],
  'projection.analytics.taskDocuments[].executionGraphView.nodes[].kind': ['lump', 'segment'],
  'projection.analytics.taskDocuments[].executionGraphView.nodes[].frontierState': [
    'landed',
    'ready',
    'waiting',
    'in-flight',
  ],
  'projection.enclosures[].lifecycleOperation.kind': ['closeout', 'integrate', 'direct-landing'],
  'projection.enclosures[].lifecycleOperation.status': [
    'queued',
    'running',
    'input-required',
    'termination-required',
    'completed',
    'failed',
    'cancelled',
    'unreadable',
  ],
  'projection.enclosures[].lifecycleOperation.phase': [
    'queued',
    'preflight',
    'memory-preflight',
    'quality',
    'approval-claim',
    'recovering-after-claim',
    'code-commit',
    'memory-refresh',
    'memory-commit',
    'ledger-commit',
    'integration-replay',
    'integration-quality',
    'source-merge',
    'contract-finalization',
    'door-publication',
    'termination-required',
    'direct-preflight',
    'direct-memory-commit',
    'direct-ledger-commit',
    'direct-terminal-publication',
    'completed',
    'failed',
    'cancelled',
  ],
  'projection.enclosures[].lifecycleOperation.taskIntent.schema': ['task-intent/v1'],
  'projection.enclosures[].lifecycleOperation.projectionEffects[].invalidation.outcome': [
    'persisted-empty',
    'already-empty',
    'recovered-malformed',
    'would-recover-malformed',
    'would-persist-empty',
    'failed',
  ],
  'projection.enclosures[].lifecycleOperation.projectionEffects[].invalidation.diagnostic.kind': [
    'task',
    'door',
    'series',
    'projection',
  ],
  'projection.enclosures[].lifecycleOperation.projectionEffects[].invalidation.diagnostic.state': [
    'missing',
    'unreadable',
    'invalid',
  ],
  'projection.enclosures[].lifecycleOperation.projectionEffects[].rebuild.outcome': [
    'published',
    'already-current',
    'source-changed',
    'source-unreadable',
    'would-publish',
    'not-attempted',
  ],
  'projection.enclosures[].lifecycleOperation.projectionEffects[].rebuild.sourceClassification': [
    'active',
    'terminal',
  ],
  'projection.enclosures[].lifecycleOperation.projectionEffects[].rebuild.sourceProblems[].kind': [
    'task',
    'door',
    'series',
    'projection',
  ],
  'projection.enclosures[].lifecycleOperation.projectionEffects[].rebuild.sourceProblems[].state': [
    'missing',
    'unreadable',
    'invalid',
  ],
};

// Reads every value the payload carries at one of the registry's dotted paths. `[]` fans out over
// an array, so one path yields one value per element — `…engineProcesses[].health` returns the
// health of every process, not the first.
function valuesAt(root: unknown, path: string): unknown[] {
  let frontier: unknown[] = [root];
  for (const [, key] of path.matchAll(/\.([A-Za-z0-9_]+)|(\[\])/g)) {
    const next: unknown[] = [];
    for (const node of frontier) {
      if (node == null) continue;
      if (key === undefined) {
        if (Array.isArray(node)) next.push(...node);
      } else if (typeof node === 'object' && key in node) {
        next.push((node as Record<string, unknown>)[key]);
      }
    }
    frontier = next;
  }
  return frontier;
}

describe('the mirror declares everything the server sends', () => {
  it('has no served field the mirror is missing', () => {
    mirrorMustDeclare<ServedOnlyPaths<typeof snapshot, WorkspaceProjection, 'projection'>>();
  });

  it('carries exactly the metrics fields the mirror models, and no others', () => {
    // The type-level walk above, restated at runtime for the one node whose fields ARE derived
    // from a vocabulary — so it can be checked without a hand-written list on either side.
    // `metricsFor([])` produces precisely the mirror's `Metrics` keys, so this is "the served
    // rollup and the modelled rollup have the same fields" with no third copy to drift.
    // A bucket only the server has (`awaitingReviewCount`) fails here; so does a bucket only the
    // mirror has, which is what a seventh state in `LIFECYCLE_STATES` produces.
    expect(new Set(Object.keys(served.metrics))).toEqual(new Set(Object.keys(metricsFor([]))));
  });
});

describe('the fixture samples everything the mirror declares', () => {
  it('reaches every declared path except the named residue', () => {
    // Fails `tsc -b` naming the path, e.g.
    // `Type '"projection.lifecycles[].stateEnteredAt"' does not satisfy the constraint 'never'.`
    fixtureMustSample<Exclude<UnsampledMirrorPaths, KnownUnsampled>>();
  });

  it('keeps the residue list spent — no entry the fixture now samples, none the mirror dropped', () => {
    allowlistMustStayEarned<Exclude<KnownUnsampled, UnsampledMirrorPaths>>();
  });

  it('reaches every node the mirror leaves unwalkable, and says why each one is opaque', () => {
    // The registry's KEYS are exhaustive at the type level. What types cannot say is that the
    // fixture actually carries these nodes: `MirrorOnlyPaths` deliberately stops at an index
    // signature, so an absorbing node the payload omits is the one kind of unsampled path it
    // cannot report. This is that check, and it is why the reason strings are required — a wall
    // in the walk that nobody can see needs a name and a sentence, not a silent gap.
    for (const [path, reason] of Object.entries(INDEX_SIGNATURE_SITES)) {
      expect(valuesAt(snapshot, path).length, `no served value at ${path}`).toBeGreaterThan(0);
      expect(reason.length, `no reason given for ${path}`).toBeGreaterThan(0);
    }
  });
});

describe('every closed vocabulary in the mirror is checked against the payload', () => {
  it("carries only values the mirror's vocabulary declares, at every registered path", () => {
    for (const [path, vocabulary] of Object.entries(VOCABULARIES)) {
      const sampled = valuesAt(snapshot, path);
      // A path the payload never reaches makes its vocabulary check vacuous. `fixtureMustSample`
      // fails on that too, but at the type level and under a different name — say it here as well
      // so a vacuous check reads as a failure of THIS assertion rather than as a pass.
      expect(sampled.length, `no served value at ${path}`).toBeGreaterThan(0);
      for (const value of sampled) expect(vocabulary, `at ${path}`).toContain(value);
    }
  });
});

describe('projection contract fixture', () => {
  it('has the top-level projection shape', () => {
    expect(typeof served.version).toBe('number');
    expect(typeof served.generatedAt).toBe('string');
    expect(Array.isArray(served.lifecycles)).toBe(true);
    expect(Array.isArray(served.enclosures)).toBe(true);
    expect(Array.isArray(served.providers)).toBe(true);
    expect(Array.isArray(served.activeWorktreeGroups)).toBe(true);
    expect(served.metrics).toBeTypeOf('object');
    expect(served.analytics).toBeTypeOf('object');
  });

  it('lifecycles carry required fields', () => {
    // The vocabularies themselves are checked above, generically and for every closed union in
    // the mirror; what is left here is the per-row shape a JSON import cannot state.
    expect(served.lifecycles.length).toBeGreaterThan(0);
    for (const lifecycle of served.lifecycles) {
      expect(typeof lifecycle.id).toBe('string');
      expect(typeof lifecycle.fleeting).toBe('boolean');
      expect(Array.isArray(lifecycle.actions)).toBe(true);
      expect(Array.isArray(lifecycle.tokenSeries)).toBe(true);
    }
  });
});

// The bucket rollup: `Metrics` counted three states while the vocabulary declared six, so an
// `awaiting-developer` lifecycle was inside `lifecycleCount` and `totalTokens` and inside no
// bucket — the rollup could not show it at all. Every assertion reads `ACTIVE_STATES`, so it
// measures the vocabulary rather than a seventh copy of the bucket list.
describe('metrics bucket every live lifecycle state', () => {
  it('the served payload carries a bucket per live state', () => {
    const servedFields = Object.keys(served.metrics);
    for (const state of ACTIVE_STATES) {
      expect(servedFields).toContain(stateCountField(state));
    }
  });

  it('counts a lifecycle in each live state into its own bucket', () => {
    // One lifecycle per live state, so a state that shares (or misses) a bucket shows up as a
    // count of 2 (or 0) rather than passing on a fixture that happens to hold none of it.
    const lifecycles = ACTIVE_STATES.map((state): LifecycleProjection => ({
      id: `LC-${state}`,
      state,
      phase: 'build',
      fleeting: false,
      tokens: 0,
      startedAt: '2026-06-14T09:00:00+00:00',
      lastEventTs: '2026-06-14T09:00:00+00:00',
      stateEnteredAt: '2026-06-14T09:00:00+00:00',
      inferred: false,
      actions: [],
      tokenSeries: [],
    }));
    const metrics = metricsFor(lifecycles) as unknown as Record<string, number>;
    for (const state of ACTIVE_STATES) {
      expect(metrics[stateCountField(state)]).toBe(1);
    }
    expect(metrics.lifecycleCount).toBe(ACTIVE_STATES.length);
  });

  it('gives each live state a bucket of its own', () => {
    // `stateCountField` is not injective — `a-b` and `aB` both bucket into `aBCount`. A
    // collision does not announce itself: `Metrics` is keyed by field, so the later count
    // silently OVERWRITES the earlier and the rollup under-reports with no field looking wrong.
    // The server refuses the collision where it builds the map (`state_count_fields`); the
    // mirror cannot refuse at runtime (the vocabulary is frozen at build time), so it fails here.
    const buckets = ACTIVE_STATES.map(stateCountField);
    expect(new Set(buckets).size).toBe(buckets.length);
  });

  it('spells a bucket field the way the server spells it', () => {
    // Two copies of one rule are only safe while they are the same rule. The server used
    // `str.capitalize()`, which lower-cases the tail; this side upper-cases the first character
    // and leaves the tail alone. Identical on every lowercase state — which is every state today
    // — and divergent the moment one is not, so the disagreement could sit here indefinitely
    // and then produce a bucket nothing fills. Settled in favour of leaving the tail alone
    // (`projection.py::state_count_field`), because `Capitalize<>` cannot lower-case a tail at
    // the type level and because lower-casing merges states that differ only in tail case.
    const camel = (state: string): string => {
      const [head, ...rest] = state.split('-');
      return `${head}${rest.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join('')}Count`;
    };
    expect(camel('awaiting-DEVELOPER')).toBe('awaitingDEVELOPERCount');
    expect(camel('awaiting-developer')).toBe('awaitingDeveloperCount');
    expect(camel('a-b-c')).toBe('aBCCount'); // multi-hyphen: both sides already agreed
    for (const state of ACTIVE_STATES) {
      expect(stateCountField(state)).toBe(camel(state));
    }
  });
});

// Fields the mirror invented — ones no Python model declares, on models that are `extra="forbid"`
// server-side, so the server could never send them. Each one grew a renderer branch that shipped
// permanently dead. These are TYPE-level assertions: they are free at runtime but fail `tsc -b`
// (the typecheck gate) the moment a field comes back, because an UNUSED `@ts-expect-error` is
// itself a compile error.
describe('mirror does not invent fields the server cannot send', () => {
  it('keeps the master and series sub-task row models distinct', () => {
    // Two separate server models — `projection.py::TaskSubTaskRefNode` (has `linkedLifecycleId`,
    // no `createdAt`) and `::SeriesSubTaskNode` (has `createdAt`, no `linkedLifecycleId`). One TS
    // interface carrying both is what made the series index's creation sort a no-op and the
    // cross-series jump unreachable from a series.
    //
    // NB `SeriesSectionNode` is the same situation one model over. The mirror now declares it
    // separately, but it CANNOT be pinned here: it and `TaskSectionNode` have the same three
    // fields, so there is no `@ts-expect-error` to write and no payload that separates them.
    // See the header note (3) — the separate declaration is a slot, not a check.
    const masterRow: TaskSubTaskRefNode = {
      number: '1',
      name: 'slice',
      file: '01.md',
      status: 'planning',
      scope: '',
    };
    const seriesRow: SeriesSubTaskNode = {
      number: '1',
      name: 'slice',
      file: '01.md',
      status: 'planning',
      scope: '',
    };
    // @ts-expect-error a master's index row is never stamped with a creation time
    void masterRow.createdAt;
    // @ts-expect-error a series row never carries a cross-series lifecycle link
    void seriesRow.linkedLifecycleId;
    expect(masterRow.linkedLifecycleId).toBeUndefined();
    expect(seriesRow.createdAt).toBeUndefined();
  });

  it('keeps engine-process edges free of a carried refusal polarity', () => {
    // `refusedPolarity` existed on no Python model. Its renderer branch was gated on the edge
    // state `refused`, which the reducer never produces either (`_materialize_edge_state`,
    // `_seed_edge_state`, and the inline literals emit only complete/planned/blocked/skipped/
    // unknown/failed/running/stale). Flash polarity is derived from the state instead.
    const edge: EngineProcessEdge = {
      id: 'cgc-seed',
      fromNode: 'code-worktree',
      toNode: 'cgc-engine',
      kind: 'cgc-seed',
      state: 'stale',
      label: 'CGC seed',
    };
    // @ts-expect-error polarity is derived from `state`, never carried on the edge
    void edge.refusedPolarity;
    expect(edge.state).toBe('stale');
  });
});
