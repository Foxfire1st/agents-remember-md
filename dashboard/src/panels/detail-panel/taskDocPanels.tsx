import { parentTaskLinkForDoc } from "../../data/taskHierarchy";
import { Panel } from "../../grammar/Panel";
import type { TaskDocNode } from "../../types/projection";
import { EmptyStateBackdrop } from "../EmptyStateBackdrop";
import type { ChangeSetTarget } from "../changeset/ChangeSetViewer";
import type { TaskArtifactReaderTarget as NotesReaderTarget } from "../../data/taskArtifacts";
import {
  masterDocWithSeriesTokens,
  seriesAsMasterDoc,
  seriesSliceDocs,
  sliceForSlug,
} from "./model";
import type { useDetailPanelState } from "./state";
import { crumb, sizing, where } from "./styles";
import { MasterOverview, TaskReader } from "./taskReader";

type DetailState = ReturnType<typeof useDetailPanelState>;

interface PanelCallbacks {
  onOpenChangeSet?: (target: ChangeSetTarget) => void;
  onOpenNotes?: (target: NotesReaderTarget) => void;
}

function TaskDocBody({
  state,
  selectedTaskDoc,
  openDoc,
  sliceDocs,
  onOpenChangeSet,
  onOpenNotes,
}: {
  state: DetailState;
  selectedTaskDoc: TaskDocNode;
  openDoc: TaskDocNode | undefined;
  sliceDocs: TaskDocNode[];
} & PanelCallbacks) {
  const { fullTaskDoc, analytics, taskDocumentBodyState, jump, setOpenSlug, docPathForRef } =
    state;
  const seriesList = analytics?.series ?? [];
  if (selectedTaskDoc.kind === "master" && openDoc) {
    return (
      <TaskReader
        doc={fullTaskDoc(openDoc)}
        bodyState={taskDocumentBodyState}
        onOpenChangeSet={onOpenChangeSet}
        onOpenNotes={onOpenNotes}
      />
    );
  }
  if (selectedTaskDoc.kind === "master") {
    return (
      <MasterOverview
        doc={masterDocWithSeriesTokens(fullTaskDoc(selectedTaskDoc), seriesList)}
        bodyState={taskDocumentBodyState}
        sliceDocs={sliceDocs}
        onOpen={setOpenSlug}
        onJump={jump}
        onOpenChangeSet={onOpenChangeSet}
        onOpenNotes={onOpenNotes}
        docPathForRef={docPathForRef}
      />
    );
  }
  return (
    <TaskReader
      doc={fullTaskDoc(selectedTaskDoc)}
      bodyState={taskDocumentBodyState}
      onOpenChangeSet={onOpenChangeSet}
      onOpenNotes={onOpenNotes}
    />
  );
}

export function TaskDocumentPanel({
  state,
  onOpenChangeSet,
  onOpenNotes,
}: {
  state: DetailState;
} & PanelCallbacks) {
  const {
    selectedTaskDoc,
    allDocs,
    analytics,
    openSlug,
    setOpenSlug,
    jump,
  } = state;
  if (!selectedTaskDoc) return null;
  const seriesList = analytics?.series ?? [];
  const sliceDocs = seriesSliceDocs(allDocs, selectedTaskDoc.docPath);
  const openDoc = openSlug ? sliceForSlug(sliceDocs, openSlug) : undefined;
  const parentLink = parentTaskLinkForDoc(
    selectedTaskDoc,
    allDocs,
    seriesList,
  );
  const head = (
    <>
      <h2>{selectedTaskDoc.title}</h2>
      {openDoc ? (
        <button
          type="button"
          className={crumb}
          onClick={() => setOpenSlug(null)}
          data-testid="series-breadcrumb"
        >
          ← {selectedTaskDoc.title}
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

  return (
    <Panel testid="detail-panel" head={head} className={sizing}>
      <div className={where}>task document · {selectedTaskDoc.repository}</div>
      <TaskDocBody
        state={state}
        selectedTaskDoc={selectedTaskDoc}
        openDoc={openDoc}
        sliceDocs={sliceDocs}
        onOpenChangeSet={onOpenChangeSet}
        onOpenNotes={onOpenNotes}
      />
    </Panel>
  );
}

export function EmptyDetailPanel() {
  return (
    <Panel testid="detail-panel" title="Detail" className={sizing} fill>
      <EmptyStateBackdrop src="/assets/sc2-battlecruiser-boomerang.mp4" opacity={0.18}>
        Select a task to inspect its phase, gate, and tokens.
      </EmptyStateBackdrop>
    </Panel>
  );
}

export function SeriesDetailPanel({
  state,
  onOpenChangeSet,
  onOpenNotes,
}: {
  state: DetailState;
} & PanelCallbacks) {
  const {
    selectedSeries,
    allDocs,
    analytics,
    openSlug,
    setOpenSlug,
    fullTaskDoc,
    taskDocumentBodyState,
  } = state;
  if (!selectedSeries) return null;
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
  const openDoc = openSlug ? sliceForSlug(seriesSlices, openSlug) : undefined;
  const head = (
    <>
      <h2>{selectedSeries.title}</h2>
      {openDoc ? (
        <button
          type="button"
          className={crumb}
          onClick={() => setOpenSlug(null)}
          data-testid="series-breadcrumb"
        >
          ← {selectedSeries.title}
        </button>
      ) : null}
    </>
  );

  return (
    <Panel testid="detail-panel" head={head} className={sizing}>
      <div className={where}>series master · {selectedSeries.repository}</div>
      {openDoc ? (
        <TaskReader
          doc={fullTaskDoc(openDoc)}
          bodyState={taskDocumentBodyState}
          onOpenChangeSet={onOpenChangeSet}
          onOpenNotes={onOpenNotes}
        />
      ) : (
        <MasterOverview
          doc={seriesDoc}
          bodyState={taskDocumentBodyState}
          sliceDocs={seriesSlices}
          onOpen={setOpenSlug}
          onJump={state.jump}
          onOpenChangeSet={onOpenChangeSet}
          onOpenNotes={onOpenNotes}
          docPathForRef={state.docPathForRef}
        />
      )}
    </Panel>
  );
}
