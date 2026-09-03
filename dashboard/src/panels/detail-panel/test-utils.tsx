import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { dashboardStore } from "../../data/store";
import { GALLERY } from "../../dev/fixtures";
import { metricsFor } from "../../types/projection";
import type {
  EnclosureNode,
  LifecycleProjection,
  SeriesNode,
  TaskDocNode,
  WorkspaceProjection,
} from "../../types/projection";
export function seed(name: string) {
  const projection = GALLERY.find((entry) => entry.name === name)?.projection;
  if (!projection) throw new Error(`fixture not found: ${name}`);
  dashboardStore.getState().applySnapshot(projection);
}

export function taskDoc(over: Partial<TaskDocNode> & Pick<TaskDocNode, "kind" | "docPath">): TaskDocNode {
  return {
    id: "1",
    lifecycleId: "LC-SER",
    repository: "repo-a",
    title: "doc",
    status: "inProgress",
    bodyRevision: "",
    createdAt: "2026-06-20T09:00:00+00:00",
    stepsDone: 0,
    stepsTotal: 0,
    steps: [],
    objective: "",
    requirements: [],
    codeExamples: [],
    decisions: [],
    openQuestions: [],
    references: [],
    orchestrates: [],
    executionWaves: [],
    subTasks: [],
    seats: [],
    sections: [],
    ...over,
  };
}

export function seriesNode(over: Partial<SeriesNode> & Pick<SeriesNode, "seriesId">): SeriesNode {
  return {
    repository: "repo-a",
    title: "series",
    status: "inProgress",
    objective: "",
    subTasks: [],
    discardedCount: 0,
    discardedSubTasks: [],
    doneCount: 0,
    totalCount: 0,
    seriesTokenTotal: 0,
    createdAt: "2026-06-20T09:00:00+00:00",
    sections: [],
    decisions: [],
    docPath: `/t/${over.seriesId}/task.json`,
    ...over,
  };
}

export function enclosure(over: Partial<EnclosureNode> & Pick<EnclosureNode, "enclosure" | "lifecycleId">) {
  return {
    enclosureId: over.enclosure,
    leafId: over.enclosure,
    taskRoot: "/tasks/260610_browser-dashboard",
    taskId: "260610_BROWSER-DASHBOARD",
    taskName: "260610_browser-dashboard",
    repoName: "agents-remember",
    worktreeGroup: "/worktrees/260610-browser-dashboard-s16-ar",
    humanReviewStatus: "pending-review",
    closeoutStatus: "not-started",
    integrationStatus: "not-started",
    cleanup: "pending",
    codeWorktreeExists: true,
    memoryWorktreeExists: true,
    actions: [],
    ...over,
  } satisfies EnclosureNode;
}

// A series projection: one lifecycle, a contract-paired master, and one authored slice doc.
const SERIES_LIFECYCLE: LifecycleProjection = {
  id: "LC-SER",
  state: "running",
  phase: "build",
  fleeting: false,
  tokens: 0,
  startedAt: "2026-06-20T09:00:00+00:00",
  lastEventTs: "2026-06-20T09:00:30+00:00",
  stateEnteredAt: "2026-06-20T09:00:00+00:00",
  inferred: false,
  actions: [],
  tokenSeries: [],
};

const SERIES_MASTER = seriesNode({
  seriesId: "series",
  title: "My Series",
  objective: "Series objective text",
  seriesTokenTotal: 1500,
  docPath: "/tasks/repo-a/series/task.json",
  subTasks: [
    {
      number: "1",
      name: "First slice",
      file: "01_first.md",
      status: "inProgress",
      scope: "",
      createdAt: "2026-06-20T09:00:00+00:00",
    },
    {
      number: "2",
      name: "Parallel series",
      file: "../other/task.md",
      status: "inProgress",
      scope: "",
      createdAt: "2026-06-21T09:00:00+00:00",
    },
  ],
  sections: [
    {
      kind: "freeform",
      heading: "Current State",
      body: "Status is **strong**.\n\n| Slice | Status |\n| --- | --- |\n| 01 | done |",
    },
    { kind: "subTasks", heading: "Sub-tasks", body: "" },
  ],
});

export function seedSeries(
  options: {
    lifecycleId?: string;
    enclosureTaskId?: string;
    enclosureTaskName?: string;
    sliceDoc?: Partial<TaskDocNode>;
  } = {},
) {
  const lc: LifecycleProjection = { ...SERIES_LIFECYCLE, id: options.lifecycleId ?? "LC-SER" };
  const master = SERIES_MASTER;
  const slice = taskDoc({
    lifecycleId: options.lifecycleId ?? "LC-SER",
    kind: "subTask",
    title: "First slice",
    objective: "Slice objective text",
    docPath: "/tasks/repo-a/series/01_first.json",
    stepsTotal: 1,
    steps: [{ id: "S1", title: "do the thing", status: "pending", substeps: [] }],
    ...options.sliceDoc,
  });
  const projection: WorkspaceProjection = {
    version: 2,
    generatedAt: "2026-06-20T09:01:00+00:00",
    lifecycles: [lc],
    enclosures: options.enclosureTaskId
      ? [
          enclosure({
            enclosure: "/contracts/series",
            lifecycleId: options.lifecycleId ?? "LC-SER",
            leafId: options.enclosureTaskName ?? "series",
            taskId: options.enclosureTaskId,
            taskName: options.enclosureTaskName ?? "series",
          }),
        ]
      : [],
    providers: [],
    activeWorktreeGroups: [],
    metrics: metricsFor([lc]),
    analytics: {
      driftSnapshots: [],
      stalestSidecars: [],
      setupSummaries: [],
      setupProgress: [],
      routeCoverage: [],
      toolReports: [],
      ledgers: [],
      taskDocuments: [slice],
      series: [master],
      attentionQueue: [],
      engineProcesses: [],
      agentPickups: [],
      expectationRows: [],
    },
  };
  dashboardStore.getState().applySnapshot(projection);
}

export function nestedProgressSteps(): TaskDocNode["steps"] {
  return Array.from({ length: 7 }, (_, stepIndex) => ({
    id: `S${stepIndex + 1}`,
    title: `Top level step ${stepIndex + 1}`,
    status: stepIndex < 6 ? "done" : "pending",
    substeps: Array.from({ length: 6 }, (_, substepIndex) => ({
      id: `S${stepIndex + 1}.${substepIndex + 1}`,
      title: `Nested step ${stepIndex + 1}.${substepIndex + 1}`,
      status: stepIndex < 6 || substepIndex < 4 ? "done" : "pending",
    })),
  }));
}

export function seedSeriesOrdering() {
  const master = seriesNode({
    seriesId: "series",
    title: "Ordered Series",
    subTasks: [
      {
        number: "99",
        name: "Alpha later",
        file: "alpha_later.md",
        status: "inProgress",
        scope: "",
        createdAt: "2026-06-22T09:00:00+00:00",
      },
      {
        number: "01",
        name: "Zulu earlier",
        file: "zulu_earlier.md",
        status: "planning",
        scope: "",
        createdAt: "2026-06-20T09:00:00+00:00",
      },
    ],
  });
  const projection: WorkspaceProjection = {
    version: 2,
    generatedAt: "2026-06-20T09:01:00+00:00",
    lifecycles: [],
    enclosures: [],
    providers: [],
    activeWorktreeGroups: [],
    metrics: metricsFor([]),
    analytics: {
      driftSnapshots: [],
      stalestSidecars: [],
      setupSummaries: [],
      setupProgress: [],
      routeCoverage: [],
      toolReports: [],
      ledgers: [],
      taskDocuments: [],
      series: [master],
      attentionQueue: [],
      engineProcesses: [],
      agentPickups: [],
      expectationRows: [],
    },
  };
  dashboardStore.getState().applySnapshot(projection);
}

export function seedTaskDocuments(
  docs: TaskDocNode[],
  over: Partial<Pick<WorkspaceProjection, "lifecycles" | "enclosures" | "activeWorktreeGroups">> = {},
) {
  seedProjection({
    ...over,
    analytics: {
      driftSnapshots: [],
      stalestSidecars: [],
      setupSummaries: [],
      setupProgress: [],
      routeCoverage: [],
      toolReports: [],
      ledgers: [],
      taskDocuments: docs,
      series: [],
      attentionQueue: [],
      engineProcesses: [],
      agentPickups: [],
      expectationRows: [],
    },
  });
}

export function seedProjection(over: Partial<WorkspaceProjection>) {
  const lifecycles = over.lifecycles ?? [];
  const projection: WorkspaceProjection = {
    version: 2,
    generatedAt: "2026-06-20T09:01:00+00:00",
    lifecycles,
    enclosures: [],
    providers: [],
    activeWorktreeGroups: [],
    metrics: metricsFor(lifecycles),
    analytics: {
      driftSnapshots: [],
      stalestSidecars: [],
      setupSummaries: [],
      setupProgress: [],
      routeCoverage: [],
      toolReports: [],
      ledgers: [],
      taskDocuments: [],
      series: [],
      attentionQueue: [],
      engineProcesses: [],
      agentPickups: [],
      expectationRows: [],
    },
    ...over,
  };
  dashboardStore.getState().applySnapshot(projection);
}

const PROMOTED_LIFECYCLE: LifecycleProjection = {
  id: "01KVW2FE8MQK6QCQQP0J4SEK3C",
  state: "paused",
  phase: "build",
  fleeting: false,
  enclosure: "/contracts/16",
  repoId: "agents-remember",
  tokens: 0,
  startedAt: "2026-06-24T06:00:00+00:00",
  lastEventTs: "2026-06-24T06:00:30+00:00",
  stateEnteredAt: "2026-06-24T06:00:00+00:00",
  inferred: false,
  actions: [],
  tokenSeries: [],
};

const PROMOTED_DOC = taskDoc({
  lifecycleId: "260610_BROWSER-DASHBOARD",
  kind: "subTask",
  title: "Lifecycle Finalize Task",
  docPath: "/tasks/260610_browser-dashboard/14_lifecycle-finalize-task.json",
  objective: "Close out the lifecycle finalizer.",
});

const PROMOTED_MASTER = taskDoc({
  lifecycleId: "260610_BROWSER-DASHBOARD",
  kind: "master",
  title: "Browser Dashboard Series",
  docPath: "/tasks/260610_browser-dashboard/task.json",
  objective: "Parent master content.",
});

const PROMOTED_LEAF = taskDoc({
  lifecycleId: "01KVW2FE8MQK6QCQQP0J4SEK3C",
  kind: "subTask",
  title: "Engine Room Stack Entry Height",
  status: "inProgress",
  docPath: "/tasks/260610_browser-dashboard/16_engine-room-stack-entry-height.json",
  objective: "Keep a single Engine Room enclosure entry visually bounded.",
  requirements: ["Render the selected leaf task document, not the parent task or enclosure contract."],
  stepsTotal: 1,
  steps: [{ id: "S1", title: "Fix the stack entry height", status: "inProgress", substeps: [] }],
  sections: [
    {
      kind: "freeform",
      heading: "Notes",
      body: "This is the authored leaf task document.",
    },
  ],
});

const PROMOTED_SERIES = seriesNode({
  seriesId: "260610_browser-dashboard",
  title: "Browser Dashboard Series",
  docPath: "/tasks/260610_browser-dashboard/task.json",
  subTasks: [
    {
      number: "16",
      name: "Engine Room Stack Entry Height",
      file: "16_engine-room-stack-entry-height.md",
      status: "inProgress",
      scope: "",
      createdAt: "2026-06-24T06:00:00+00:00",
    },
  ],
});

export function seedPromotedLeaf() {
  const lc = PROMOTED_LIFECYCLE;
  const doc = PROMOTED_DOC;
  const master = PROMOTED_MASTER;
  const leaf = PROMOTED_LEAF;
  const projection: WorkspaceProjection = {
    version: 2,
    generatedAt: "2026-06-24T06:01:00+00:00",
    lifecycles: [lc],
    enclosures: [
      enclosure({
        enclosure: "/contracts/16",
        lifecycleId: "01KVW2FE8MQK6QCQQP0J4SEK3C",
        leafId: "16_engine-room-stack-entry-height",
      }),
    ],
    providers: [],
    activeWorktreeGroups: [],
    metrics: metricsFor([lc]),
    analytics: {
      driftSnapshots: [],
      stalestSidecars: [],
      setupSummaries: [],
      setupProgress: [],
      routeCoverage: [],
      toolReports: [],
      ledgers: [],
      taskDocuments: [master, doc, leaf],
      series: [PROMOTED_SERIES],
      attentionQueue: [],
      engineProcesses: [],
      agentPickups: [],
      expectationRows: [],
    },
  };
  dashboardStore.getState().applySnapshot(projection);
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  dashboardStore.getState().reset();
});

// ChangeSetButton fetches its counters on mount; a tiny stub keeps the doc-reader-bar tests from
// touching a real fetch (the counts are incidental — these tests assert the buttons + click target).
export function stubCounters() {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (url: string) => {
        if (url.startsWith("/api/task-document")) {
          const params = new URLSearchParams(url.split("?", 2)[1] ?? "");
          const docPath = params.get("path") ?? "";
          const doc =
            dashboardStore.getState().analytics?.taskDocuments.find((item) => item.docPath === docPath) ??
            taskDoc({ kind: docPath.endsWith("/task.json") ? "master" : "subTask", docPath });
          return { ok: true, status: 200, json: async () => doc } as unknown as Response;
        }
        return {
          ok: true,
          status: 200,
          json: async () => ({
            counters: {
              code: { files: 0, insertions: 0, deletions: 0 },
              memory: { files: 0, insertions: 0, deletions: 0 },
            },
          }),
        } as unknown as Response;
      },
    ),
  );
}

export function stubNotes(
  notes: Array<{ name: string; path: string; size: number; language: string }>,
  body = "note body",
  requirements: Array<{
    name: string;
    path: string;
    address: string;
    size: number;
    sha256: string;
  }> = [],
) {
  const fn = vi.fn(async (url: string) => {
    if (url.startsWith("/api/task-document")) {
      const params = new URLSearchParams(url.split("?", 2)[1] ?? "");
      const docPath = params.get("path") ?? "";
      const doc =
        dashboardStore.getState().analytics?.taskDocuments.find((item) => item.docPath === docPath) ??
        taskDoc({ kind: docPath.endsWith("/task.json") ? "master" : "subTask", docPath });
      return { ok: true, status: 200, json: async () => doc } as unknown as Response;
    }
    if (url.startsWith("/api/notes/list")) {
      const payload = { repo: "agents-remember", master: "m", notes, truncated: false };
      return { ok: true, status: 200, json: async () => payload } as unknown as Response;
    }
    if (url.startsWith("/api/notes/read")) {
      const payload = {
        path: "x",
        language: "markdown",
        size: body.length,
        truncated: false,
        content: body,
      };
      return { ok: true, status: 200, json: async () => payload } as unknown as Response;
    }
    if (url.startsWith("/api/requirements/list")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          repo: "agents-remember",
          master: "m",
          document: "m/task.json",
          registered: requirements.length > 0,
          requirements,
        }),
      } as unknown as Response;
    }
    return { ok: true, status: 200, json: async () => ({}) } as unknown as Response;
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}
