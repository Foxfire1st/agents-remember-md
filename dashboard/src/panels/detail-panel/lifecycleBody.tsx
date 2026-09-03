import {
  taskDocsForLifecycle,
  taskLabel,
} from "../../data/taskIdentity";
import { parentTaskLinkForDoc } from "../../data/taskHierarchy";
import { Panel } from "../../grammar/Panel";
import { TokenGauge } from "../../grammar/TokenGauge";
import type {
  Analytics,
  EnclosureNode,
  LifecycleProjection,
  Phase,
  SeriesNode,
  TaskDocNode,
} from "../../types/projection";
import type { ChangeSetTarget } from "../changeset/ChangeSetViewer";
import { GateResponder } from "../GateResponder";
import type { TaskArtifactReaderTarget as NotesReaderTarget } from "../../data/taskArtifacts";
import { ChangeSetButton } from "./changeSetBar";
import {
  masterDocWithSeriesTokens,
  seriesAsMasterDoc,
  seriesSliceDocs,
  sliceForSlug,
} from "./model";
import type { useDetailPanelState } from "./state";
import {
  changeSetBar,
  crumb,
  label,
  lanes,
  sizing,
  spine,
  spineHead,
  step,
  stepper,
  tokensRow,
  where,
} from "./styles";
import {
  MasterOverview,
  SpineLane,
  TaskContent,
  TaskReader,
} from "./taskReader";

type DetailState = ReturnType<typeof useDetailPanelState>;

// the current as done — mc2's Request→Close mini-map.
const PHASES: Phase[] = [
  "request",
  "trust-checkpoint",
  "reframe-research",
  "decide",
  "build",
  "close",
];

interface PanelCallbacks {
  onOpenChangeSet?: (target: ChangeSetTarget) => void;
  onOpenNotes?: (target: NotesReaderTarget) => void;
}

function resolveMasterAndSlices(docs: TaskDocNode[], allDocs: TaskDocNode[]) {
  const master = docs.find((doc) => doc.kind === "master");
  const slices = master
    ? seriesSliceDocs(allDocs, master.docPath)
    : docs.filter((doc) => doc.kind !== "master");
  return { master, slices };
}

function resolveSeriesView(
  selectedSeries: SeriesNode | undefined,
  allDocs: TaskDocNode[],
  analytics: Analytics | null | undefined,
  fullTaskDoc: (doc: TaskDocNode) => TaskDocNode,
) {
  if (!selectedSeries) return { seriesDoc: undefined, seriesSlices: [] };
  const selectedSeriesTaskDoc = allDocs.find(
    (doc) => doc.docPath === selectedSeries.docPath,
  );
  const seriesDoc = selectedSeriesTaskDoc
    ? masterDocWithSeriesTokens(
        fullTaskDoc(selectedSeriesTaskDoc),
        analytics?.series ?? [],
      )
    : seriesAsMasterDoc(selectedSeries);
  const seriesSlices = seriesSliceDocs(allDocs, selectedSeries.docPath);
  return { seriesDoc, seriesSlices };
}

function resolveParentLink(
  selectedSeries: SeriesNode | undefined,
  master: TaskDocNode | undefined,
  docs: TaskDocNode[],
  allDocs: TaskDocNode[],
  analytics: Analytics | null | undefined,
) {
  if (selectedSeries || master || docs.length !== 1) return undefined;
  return parentTaskLinkForDoc(docs[0], allDocs, analytics?.series ?? []);
}

function useLifecycleDetail(
  state: DetailState,
  lifecycle: LifecycleProjection,
) {
  const {
    selectedTaskDoc,
    selectedSeries,
    allDocs,
    analytics,
    selectedEnclosure,
    openSlug,
    fullTaskDoc,
    directDocs,
  } = state;
  const currentIdx = PHASES.indexOf(lifecycle.phase);
  const docs =
    selectedTaskDoc?.lifecycleId === lifecycle.id
      ? [selectedTaskDoc]
      : taskDocsForLifecycle(lifecycle, allDocs);
  const title = taskLabel(lifecycle, directDocs, selectedEnclosure);
  const groupName = selectedEnclosure
    ? (selectedEnclosure.worktreeGroup.split("/").filter(Boolean).pop() ?? "")
    : "";
  const { master, slices } = resolveMasterAndSlices(docs, allDocs);
  const { seriesDoc, seriesSlices } = resolveSeriesView(
    selectedSeries,
    allDocs,
    analytics,
    fullTaskDoc,
  );
  const contentSlices = seriesDoc ? seriesSlices : slices;
  const openDoc = openSlug ? sliceForSlug(contentSlices, openSlug) : undefined;
  const heading = selectedSeries?.title ?? title;
  const parentLink = resolveParentLink(
    selectedSeries,
    master,
    docs,
    allDocs,
    analytics,
  );
  return {
    currentIdx,
    docs,
    master,
    slices,
    selectedSeries,
    seriesDoc,
    seriesSlices,
    contentSlices,
    openDoc,
    heading,
    parentLink,
    groupName,
  };
}

function DetailHead({
  state,
  derived,
}: {
  state: DetailState;
  derived: ReturnType<typeof useLifecycleDetail>;
}) {
  const { setOpenSlug, jump } = state;
  const { openDoc, selectedSeries, master, parentLink, heading } = derived;
  return (
    <>
      <h2>{heading}</h2>
      {openDoc ? (
        <button
          type="button"
          className={crumb}
          onClick={() => setOpenSlug(null)}
          data-testid="series-breadcrumb"
        >
          ← {selectedSeries?.title ?? (master ? master.title : "series")}
        </button>
      ) : !selectedSeries && master?.masterLifecycleId ? (
        <button
          type="button"
          className={crumb}
          onClick={() => jump(master.masterLifecycleId as string)}
          data-testid="master-parent-link"
        >
          ↑ {master.masterLifecycleId}
        </button>
      ) : parentLink ? (
        <button
          type="button"
          className={crumb}
          onClick={() => jump(parentLink.targetKey)}
          data-testid="master-parent-link"
        >
          ↑ {parentLink.title}
        </button>
      ) : null}
    </>
  );
}

function PhaseStepper({ currentIdx }: { currentIdx: number }) {
  return (
    <ol className={stepper} aria-label="phase">
      {PHASES.map((phase, i) => (
        <li
          key={phase}
          className={step({
            state: i < currentIdx ? "done" : i === currentIdx ? "current" : "todo",
          })}
        >
          {phase}
        </li>
      ))}
    </ol>
  );
}

function GateSection({ lifecycle }: { lifecycle: LifecycleProjection }) {
  return lifecycle.gate ? (
    <GateResponder
      lifecycleId={lifecycle.id}
      gateNode={lifecycle.gate}
      ask={lifecycle.ask}
      testId="gate-review"
    />
  ) : null;
}

function DetailBody({
  state,
  derived,
  onOpenChangeSet,
  onOpenNotes,
}: {
  state: DetailState;
  derived: ReturnType<typeof useLifecycleDetail>;
} & PanelCallbacks) {
  const {
    setOpenSlug,
    fullTaskDoc,
    analytics,
    taskDocumentBodyState,
    jump,
  } = state;
  const { docs, master, slices, seriesDoc, seriesSlices, openDoc } = derived;
  if (openDoc) {
    return (
      <TaskReader
        doc={fullTaskDoc(openDoc)}
        bodyState={taskDocumentBodyState}
        onOpenChangeSet={onOpenChangeSet}
        onOpenNotes={onOpenNotes}
      />
    );
  }
  if (seriesDoc) {
    return (
      <MasterOverview
        doc={seriesDoc}
        bodyState={taskDocumentBodyState}
        sliceDocs={seriesSlices}
        onOpen={setOpenSlug}
        onJump={jump}
        onOpenChangeSet={onOpenChangeSet}
        onOpenNotes={onOpenNotes}
        docPathForRef={state.docPathForRef}
      />
    );
  }
  if (master) {
    return (
      <MasterOverview
        doc={masterDocWithSeriesTokens(
          fullTaskDoc(master),
          analytics?.series ?? [],
        )}
        bodyState={taskDocumentBodyState}
        sliceDocs={slices}
        onOpen={setOpenSlug}
        onJump={jump}
        onOpenChangeSet={onOpenChangeSet}
        onOpenNotes={onOpenNotes}
        docPathForRef={state.docPathForRef}
      />
    );
  }
  return (
    <TaskContent
      docs={docs.map(fullTaskDoc)}
      bodyState={taskDocumentBodyState}
      onOpen={setOpenSlug}
      onJump={jump}
      onOpenChangeSet={onOpenChangeSet}
      onOpenNotes={onOpenNotes}
      docPathForRef={state.docPathForRef}
    />
  );
}

function WorktreeSpine({
  state,
  enclosure,
  groupName,
  taskDocumentBodyState,
  onOpenChangeSet,
}: {
  state: DetailState;
  enclosure: EnclosureNode | undefined;
  groupName: string;
  taskDocumentBodyState: DetailState["taskDocumentBodyState"];
  onOpenChangeSet?: (target: ChangeSetTarget) => void;
}) {
  const { activeWorktreeGroups, providers } = state;
  const engines = groupName
    ? Object.values(providers).filter(
        (p) => p.scope === "worktree" && p.worktreeGroup === groupName,
      )
    : [];
  if (!enclosure) return null;
  return (
    <div className={spine}>
      <div className={spineHead}>worktree · {groupName || enclosure.repoName}</div>
      {onOpenChangeSet && taskDocumentBodyState !== "loading" ? (
        <div className={changeSetBar}>
          {activeWorktreeGroups.includes(groupName) ? (
            <ChangeSetButton
              target={{ repo: enclosure.repoName, scope: groupName }}
              label="change-set"
              onOpen={onOpenChangeSet}
            />
          ) : null}
          {enclosure.taskName ? (
            <ChangeSetButton
              target={{ repo: enclosure.repoName, master: enclosure.taskName }}
              label="series"
              onOpen={onOpenChangeSet}
            />
          ) : null}
        </div>
      ) : null}
      <div className={lanes}>
        <SpineLane
          kind="code"
          title="code → CGC"
          repo={enclosure.repoName}
          engines={engines.filter((engine) => engine.role === "code")}
        />
        <SpineLane
          kind="memory"
          title="memory → GrepAI"
          repo={`ar-${enclosure.repoName}`}
          engines={engines.filter((engine) => engine.role === "memory")}
        />
      </div>
    </div>
  );
}

function TokenRow({ lifecycle }: { lifecycle: LifecycleProjection }) {
  return (
    <div className={tokensRow}>
      <span className={label}>tokens</span>
      <TokenGauge series={lifecycle.tokenSeries} />
    </div>
  );
}

export function LifecycleDetailBody({
  state,
  lifecycle,
  onOpenChangeSet,
  onOpenNotes,
}: {
  state: DetailState;
  lifecycle: LifecycleProjection;
} & PanelCallbacks) {
  const derived = useLifecycleDetail(state, lifecycle);
  return (
    <Panel
      testid="detail-panel"
      head={<DetailHead state={state} derived={derived} />}
      className={sizing}
    >
      <div className={where}>
        {lifecycle.fleeting
          ? "fleeting · no worktree"
          : `persistent worktree · ${lifecycle.repoId ?? "—"}`}
        {lifecycle.inferred ? " · inferred" : ""}
      </div>

      <PhaseStepper currentIdx={derived.currentIdx} />

      <GateSection lifecycle={lifecycle} />

      <DetailBody
        state={state}
        derived={derived}
        onOpenChangeSet={onOpenChangeSet}
        onOpenNotes={onOpenNotes}
      />

      <WorktreeSpine
        state={state}
        enclosure={state.selectedEnclosure}
        groupName={derived.groupName}
        taskDocumentBodyState={state.taskDocumentBodyState}
        onOpenChangeSet={onOpenChangeSet}
      />

      <TokenRow lifecycle={lifecycle} />
    </Panel>
  );
}
