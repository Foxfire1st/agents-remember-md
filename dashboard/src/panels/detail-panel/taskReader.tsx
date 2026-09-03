// The DetailPanel task-document reader family: master overview, sub-task index, slice list, spine
// lanes, full reader, and the small section primitives (Section/Bullets/StepList/CodeExample/
// DecisionList). All presentation logic lives here; doc selection stays in model.ts.
import type { ReactNode } from "react";

import { cx } from "../../../styled-system/css";
import { Markdown } from "../../grammar/Markdown";
import { TaskRequirementLinksProvider } from "../../grammar/TaskRequirementLinks";
import { ProgressFill } from "../../grammar/ProgressFill";
import {
  type TaskDocumentBodyState,
} from "../../data/useTaskDocumentBody";
import { orderedByCreation } from "../../data/taskHierarchy";
import {
  qualifiedLeafKey,
  taskDocSelectionKey,
  taskDocumentRefForDoc,
} from "../../data/taskIdentity";
import type {
  DiscardedSubTaskNode,
  ProviderNode,
  SubTaskRow,
  TaskCodeExampleNode,
  TaskDecisionNode,
  TaskDocNode,
  TaskDocumentRef,
  TaskSectionNode,
  TaskStepNode,
} from "../../types/projection";
import type { TaskArtifactReaderTarget as NotesReaderTarget } from "../../data/taskArtifacts";
import { CloseoutQueue } from "../CloseoutQueue";
import { SprintGraphView } from "../sprint-graph/SprintGraphView";
import { DocChangeSetBar } from "./changeSetBar";
import {
  dirName,
  labelWithId,
  sliceForRef,
  sliceSlug,
  subTaskKey,
  taskStepProgress,
  type MasterDocView,
} from "./model";
import {
  SUBSTEP,
  badge,
  crossButton,
  label,
  lane,
  laneMeta,
  laneRepo,
  laneTitle,
  masterTokenValue,
  masterTokens,
  series,
  skippedDisposition,
  skippedWord,
  slice,
  sliceButton,
  sliceMeta,
  slices,
  STEP_MARK,
  STEP_TITLE,
  stepMarkBase,
  stepRow,
  stepsList,
  substeps,
  taskHead,
  taskdoc,
  taskdocBullets,
  taskdocCode,
  taskdocCodeHead,
  taskdocCodeMeta,
  taskdocDecision,
  taskdocDecisionMeta,
  taskdocDecisions,
  taskdocH,
  taskdocHead,
  taskdocSection,
  taskdocSnippet,
  taskdocStatus,
  taskdocTitle,
} from "./styles";
import type { ChangeSetTarget } from "../changeset/ChangeSetViewer";
import { TaskNotes } from "../TaskNotes";

function TaskRequirementBoundary({
  doc,
  onOpenNotes,
  children,
}: {
  doc: Pick<TaskDocNode, "repository" | "docPath">;
  onOpenNotes?: (target: NotesReaderTarget) => void;
  children: ReactNode;
}) {
  return (
    <TaskRequirementLinksProvider
      repo={doc.repository}
      master={dirName(doc.docPath)}
      document={taskDocumentRefForDoc(doc)?.path}
      onOpenArtifact={onOpenNotes}
    >
      {children}
    </TaskRequirementLinksProvider>
  );
}

export function TaskContent({
  docs,
  bodyState,
  onOpen,
  onJump,
  onOpenChangeSet,
  onOpenNotes,
  docPathForRef,
}: {
  docs: TaskDocNode[];
  bodyState: TaskDocumentBodyState | undefined;
  onOpen: (slug: string) => void;
  onJump: (id: string) => void;
  onOpenChangeSet?: (target: ChangeSetTarget) => void;
  onOpenNotes?: (target: NotesReaderTarget) => void;
  docPathForRef?: (ref: TaskDocumentRef) => string | undefined;
}) {
  if (docs.length === 0) {
    return <p className="muted">No task document bound to this task.</p>;
  }
  const master = docs.find((doc) => doc.kind === "master");
  const sliceDocs = docs.filter((doc) => doc.kind !== "master");
  if (master) {
    return (
      <MasterOverview
        doc={master}
        bodyState={bodyState}
        sliceDocs={sliceDocs}
        onOpen={onOpen}
        onJump={onJump}
        onOpenChangeSet={onOpenChangeSet}
        onOpenNotes={onOpenNotes}
        docPathForRef={docPathForRef}
      />
    );
  }
  if (sliceDocs.length === 1) {
    return (
      <TaskReader
        doc={sliceDocs[0]}
        bodyState={bodyState}
        onOpenChangeSet={onOpenChangeSet}
        onOpenNotes={onOpenNotes}
      />
    );
  }
  return <SliceList sliceDocs={sliceDocs} onOpen={onOpen} />;
}

// The master overview: identity + objective, then its ordered render plan (`sections`). A
// `subTasks` section renders the clickable index in place; `sharedDecisions` renders the decision
// table. If no section drives the index but the master carries one, it is appended.
function MasterOverviewHeader({
  doc,
  bodyState,
  onOpenChangeSet,
}: {
  doc: MasterDocView;
  bodyState: TaskDocumentBodyState | undefined;
  onOpenChangeSet?: (target: ChangeSetTarget) => void;
}) {
  return (
    <>
      <div className={taskdocHead}>
        <span className={badge}>{doc.kind}</span>
        <span className={taskdocTitle}>{doc.title}</span>
        <span className={taskdocStatus}>{doc.status}</span>
      </div>
      <TaskBodyNotice state={bodyState} />
      {bodyState !== "loading" ? (
        <DocChangeSetBar
          kind="master"
          repo={doc.repository}
          master={dirName(doc.docPath)}
          onOpen={onOpenChangeSet}
        />
      ) : null}
      <MasterTokenSummary total={doc.seriesTokenTotal} />
    </>
  );
}

export function MasterOverview({
  doc,
  bodyState,
  sliceDocs,
  onOpen,
  onJump,
  onOpenChangeSet,
  onOpenNotes,
  docPathForRef,
}: {
  doc: MasterDocView;
  bodyState: TaskDocumentBodyState | undefined;
  sliceDocs: TaskDocNode[];
  onOpen: (slug: string) => void;
  onJump: (id: string) => void;
  onOpenChangeSet?: (target: ChangeSetTarget) => void;
  onOpenNotes?: (target: NotesReaderTarget) => void;
  docPathForRef?: (ref: TaskDocumentRef) => string | undefined;
}) {
  return (
    <TaskRequirementBoundary doc={doc} onOpenNotes={onOpenNotes}>
    <div className={taskdoc}>
      <MasterOverviewHeader
        doc={doc}
        bodyState={bodyState}
        onOpenChangeSet={onOpenChangeSet}
      />
      {/* Pinned navigation: the sub-task index sits above the description, always reachable. The
          authored `subTasks` section still renders its own copy in place (MasterSection). */}
      {doc.subTasks.length > 0 ? (
        <Section title="Sub-tasks">
          <SubTaskIndex
            refs={doc.subTasks}
            sliceDocs={sliceDocs}
            onOpen={onOpen}
            onJump={onJump}
            docPathForRef={docPathForRef}
          />
        </Section>
      ) : null}
      {doc.discardedSubTasks && doc.discardedSubTasks.length > 0 ? (
        <Section title={`Discarded before start (${doc.discardedCount ?? doc.discardedSubTasks.length})`}>
          <DiscardedSubTaskHistory items={doc.discardedSubTasks} />
        </Section>
      ) : null}
      {/* Graph rendering is optional, but a legal sprint projection stays reachable without it. */}
      <SprintGraphSection doc={doc} />
      <CloseoutQueue sprintRef={taskDocumentRefForDoc(doc)} />
      {doc.objective ? (
        <Section title="Objective">
          <Markdown>{doc.objective}</Markdown>
        </Section>
      ) : null}
      {doc.sections.map((section) => (
        <MasterSection
          key={section.heading}
          section={section}
          doc={doc}
          sliceDocs={sliceDocs}
          onOpen={onOpen}
          onJump={onJump}
          docPathForRef={docPathForRef}
        />
      ))}
      {/* The series' coordination notes (design records, friction ledger, reports/) —
          browsable from the master overview too, not only from a drilled leaf reader. */}
      {bodyState !== "loading" ? (
        <TaskNotes
          repo={doc.repository}
          master={dirName(doc.docPath)}
          references={[]}
          onOpenNotes={onOpenNotes}
        />
      ) : null}
    </div>
    </TaskRequirementBoundary>
  );
}

// The wave-grid is optional. CloseoutQueue is mounted by the master surface independently because
// atomic-sequential orchestration sprints are supported without an authored execution graph.
function SprintGraphSection({ doc }: { doc: MasterDocView }) {
  if (!doc.executionGraphView) return null;
  return (
    <Section title="Execution graph">
      <SprintGraphView graphView={doc.executionGraphView} />
    </Section>
  );
}
export function MasterTokenSummary({ total }: { total: number | undefined }) {
  if (total === undefined) return null;
  return (
    <div className={masterTokens} aria-label={`${total} aggregate series tokens`}>
      <span className={label}>series tokens</span>
      <span className={masterTokenValue}>{total.toLocaleString()} tok</span>
    </div>
  );
}

export function MasterSection({
  section,
  doc,
  sliceDocs,
  onOpen,
  onJump,
  docPathForRef,
}: {
  section: TaskSectionNode;
  doc: MasterDocView;
  sliceDocs: TaskDocNode[];
  onOpen: (slug: string) => void;
  onJump: (id: string) => void;
  docPathForRef?: (ref: TaskDocumentRef) => string | undefined;
}) {
  return (
    <Section title={section.heading}>
      {section.body ? <Markdown>{section.body}</Markdown> : null}
      {section.kind === "subTasks" ? (
        <SubTaskIndex
          refs={doc.subTasks}
          sliceDocs={sliceDocs}
          onOpen={onOpen}
          onJump={onJump}
          testidPrefix="subtask-mid"
          docPathForRef={docPathForRef}
        />
      ) : null}
      {section.kind === "sharedDecisions" ? <DecisionList items={doc.decisions} /> : null}
    </Section>
  );
}

// A sprint row carrying a typed masterRef (L14-R1): opens the commanded master document directly
// — the sprint → master leg of the drill-down (L14-R2). Only a task-doc master's rows can carry
// one (`SeriesSubTaskNode` has no such field), so like the cross-series link this is structurally
// unreachable for a series rendered via `seriesAsMasterDoc`.
function MasterRefIndexRow({
  ref,
  masterRef,
  masterPath,
  position,
  label,
  meta,
  onJump,
  testidPrefix,
}: {
  ref: SubTaskRow;
  masterRef: TaskDocumentRef;
  masterPath: string;
  position: number;
  label: string;
  meta: ReactNode;
  onJump: (id: string) => void;
  testidPrefix: string;
}) {
  return (
    <li key={subTaskKey(ref, position - 1)}>
      <button
        type="button"
        className={crossButton}
        onClick={() => onJump(taskDocSelectionKey(masterPath))}
        data-testid={`${testidPrefix}-master-${position}`}
        title={`open the ${masterRef.path} master document`}
      >
        <span>⇒ {label}</span>
        {meta}
      </button>
    </li>
  );
}

// The clickable series index: one row per master `SubTaskRef`. A row whose slice has been authored
// as a task document opens its reader; an un-migrated slice shows as a static index row.
function SubTaskIndexRow({
  ref,
  position,
  sliceDocs,
  onOpen,
  onJump,
  testidPrefix,
  docPathForRef,
}: {
  ref: SubTaskRow;
  position: number;
  sliceDocs: TaskDocNode[];
  onOpen: (slug: string) => void;
  onJump: (id: string) => void;
  testidPrefix: string;
  docPathForRef?: (ref: TaskDocumentRef) => string | undefined;
}) {
  const match = sliceForRef(sliceDocs, ref);
  const { displayNumber, displayName } = subTaskDisplay(match, ref);
  const label = `${displayNumber}. ${displayName}`;
  const meta = subTaskMeta(match, ref);
  // masterRef first (L14-R2), then the older behaviors; an unprojected masterRef target falls through.
  const masterRef = "masterRef" in ref ? ref.masterRef : undefined;
  const masterPath = masterRef && docPathForRef ? docPathForRef(masterRef) : undefined;
  if (masterRef && masterPath) {
    return (
      <MasterRefIndexRow
        ref={ref}
        masterRef={masterRef}
        masterPath={masterPath}
        position={position}
        label={label}
        meta={meta}
        onJump={onJump}
        testidPrefix={testidPrefix}
      />
    );
  }
  // A row whose ref points at another master is a parallel/external series → jump lifecycles.
  const linkedLifecycleId = "linkedLifecycleId" in ref ? ref.linkedLifecycleId : undefined;
  if (linkedLifecycleId) {
    return (
      <li key={subTaskKey(ref, position - 1)}>
        <button
          type="button"
          className={crossButton}
          onClick={() => onJump(linkedLifecycleId)}
          data-testid={`${testidPrefix}-link-${position}`}
          title={`open the ${linkedLifecycleId} series`}
        >
          <span>→ {label}</span>
          {meta}
        </button>
      </li>
    );
  }
  return (
    <li key={subTaskKey(ref, position - 1)}>
      {match ? (
        <button
          type="button"
          className={sliceButton}
          onClick={() => onOpen(sliceSlug(match))}
          data-testid={`${testidPrefix}-${position}`}
        >
          <span>{label}</span>
          {meta}
        </button>
      ) : (
        <div
          className={slice}
          data-testid={`${testidPrefix}-${position}`}
          title="not authored as a task document yet"
        >
          <span>{label}</span>
          {meta}
        </div>
      )}
    </li>
  );
}

function subTaskDisplay(
  match: TaskDocNode | undefined,
  ref: SubTaskRow,
): { displayNumber: string; displayName: string } {
  return {
    displayNumber: match?.id || ref.number,
    displayName: match?.title || ref.name,
  };
}

function subTaskMeta(match: TaskDocNode | undefined, ref: SubTaskRow): ReactNode {
  const progress = match ? taskStepProgress(match) : undefined;
  return (
    <span className={sliceMeta}>
      {progress && progress.total > 0 ? `${progress.done}/${progress.total} · ` : ""}
      {ref.status}
    </span>
  );
}

export function SubTaskIndex({
  refs,
  sliceDocs,
  onOpen,
  onJump,
  testidPrefix = "subtask-open",
  docPathForRef,
}: {
  refs: SubTaskRow[];
  sliceDocs: TaskDocNode[];
  onOpen: (slug: string) => void;
  onJump: (id: string) => void;
  testidPrefix?: string;
  docPathForRef?: (ref: TaskDocumentRef) => string | undefined;
}) {
  if (refs.length === 0) {
    return <p className="muted">No sub-tasks indexed.</p>;
  }
  // Rendered in the order received, deliberately. Neither source needs a client-side sort: a
  // master's `TaskSubTaskRefNode` rows carry no `createdAt` at all (so the old
  // `orderedByCreation(refs)` here could never do anything), and a series' `SeriesSubTaskNode`
  // rows arrive already ordered by it from `snapshots.py::_series_subtask_nodes`.
  return (
    <ul className={slices}>
      {refs.map((ref, index) => (
        <SubTaskIndexRow
          key={subTaskKey(ref, index)}
          ref={ref}
          position={index + 1}
          sliceDocs={sliceDocs}
          onOpen={onOpen}
          onJump={onJump}
          testidPrefix={testidPrefix}
          docPathForRef={docPathForRef}
        />
      ))}
    </ul>
  );
}

// Fallback for a series with no master yet: the slice list, now clickable into each reader.
export function SliceList({
  sliceDocs,
  onOpen,
}: {
  sliceDocs: TaskDocNode[];
  onOpen: (slug: string) => void;
}) {
  return (
    <div className={series}>
      <div className={taskHead}>
        <span className={badge}>series</span> {sliceDocs.length} task slices
      </div>
      <ul className={slices}>
        {orderedByCreation(sliceDocs)
          .map((doc) => {
            const progress = taskStepProgress(doc);
            return (
              <li key={doc.docPath}>
                <button
                  type="button"
                  className={sliceButton}
                  onClick={() => onOpen(sliceSlug(doc))}
                  data-testid={`slice-open-${sliceSlug(doc)}`}
                >
                  <span>{doc.title}</span>
                  <span className={sliceMeta}>
                    {progress.total > 0 ? `${progress.done}/${progress.total} · ` : ""}
                    {doc.status}
                  </span>
                </button>
              </li>
            );
          })}
      </ul>
    </div>
  );
}

export function SpineLane({
  kind,
  title,
  repo,
  engines,
}: {
  kind: "code" | "memory";
  title: string;
  repo: string;
  engines: ProviderNode[];
}) {
  return (
    <div className={lane({ kind })}>
      <div className={laneTitle}>{title}</div>
      <div className={laneRepo}>{repo}</div>
      {engines.length > 0 ? (
        <div className={laneMeta}>
          {engines.map((engine) => (
            <span key={engine.id}>{engine.state}</span>
          ))}
        </div>
      ) : (
        <span className="muted">no isolated engine recorded</span>
      )}
    </div>
  );
}

function TaskReaderSections({
  doc,
  bodyState,
  onOpenNotes,
}: {
  doc: TaskDocNode;
  bodyState: TaskDocumentBodyState | undefined;
  onOpenNotes?: (target: NotesReaderTarget) => void;
}) {
  return (
    <>
      {doc.objective ? (
        <Section title="Objective">
          <Markdown>{doc.objective}</Markdown>
        </Section>
      ) : null}
      {doc.requirements.length > 0 ? (
        <Section title="Requirements">
          <Bullets items={doc.requirements} />
        </Section>
      ) : null}
      {doc.design ? (
        <Section title="Design">
          <Markdown>{doc.design}</Markdown>
        </Section>
      ) : null}
      {doc.steps.length > 0 ? (
        <Section title="Implementation steps">
          <StepList steps={doc.steps} />
        </Section>
      ) : null}
      {doc.codeExamples.length > 0 ? (
        <Section title="Proposed code">
          {doc.codeExamples.map((example) => (
            <CodeExample key={example.id} example={example} />
          ))}
        </Section>
      ) : null}
      {doc.decisions.length > 0 ? (
        <Section title="Decision log">
          <DecisionList items={doc.decisions} />
        </Section>
      ) : null}
      {doc.openQuestions.length > 0 ? (
        <Section title="Open questions">
          <Bullets items={doc.openQuestions} />
        </Section>
      ) : null}
      {doc.sections.map((section) => (
        <Section key={`${section.kind}:${section.heading}`} title={section.heading}>
          {section.body ? <Markdown>{section.body}</Markdown> : null}
        </Section>
      ))}
      {/* References moved into TaskNotes so a reference naming an existing notes/ file
          renders as an openable link into the series-notes view (plain text otherwise). */}
      {bodyState !== "loading" ? (
        <TaskNotes
          repo={doc.repository}
          master={dirName(doc.docPath)}
          references={doc.references}
          onOpenNotes={onOpenNotes}
        />
      ) : null}
    </>
  );
}

export function TaskReader({
  doc,
  bodyState,
  onOpenChangeSet,
  onOpenNotes,
}: {
  doc: TaskDocNode;
  bodyState: TaskDocumentBodyState | undefined;
  onOpenChangeSet?: (target: ChangeSetTarget) => void;
  onOpenNotes?: (target: NotesReaderTarget) => void;
}) {
  const progress = taskStepProgress(doc);
  const leafKey = qualifiedLeafKey(doc);
  return (
    <TaskRequirementBoundary doc={doc} onOpenNotes={onOpenNotes}>
    <div className={taskdoc} data-task-leaf-key={leafKey}>
      <div className={taskdocHead}>
        <span className={badge}>{doc.kind}</span>
        <span className={taskdocTitle}>{doc.title}</span>
        <span className={taskdocStatus}>{doc.status}</span>
        <ProgressFill completed={progress.done} total={progress.total} label="steps done" />
      </div>
      <TaskBodyNotice state={bodyState} />
      {bodyState !== "loading" ? (
        <DocChangeSetBar
          kind="leaf"
          repo={doc.repository}
          master={dirName(doc.docPath)}
          leaf={doc.id}
          onOpen={onOpenChangeSet}
        />
      ) : null}
      <TaskReaderSections doc={doc} bodyState={bodyState} onOpenNotes={onOpenNotes} />
    </div>
    </TaskRequirementBoundary>
  );
}

export function TaskBodyNotice({ state }: { state: TaskDocumentBodyState | undefined }) {
  if (state === "loading") {
    return (
      <p className="muted" role="status">
        Loading complete task document…
      </p>
    );
  }
  if (state === "unavailable") {
    return (
      <p className="muted">Full task document details are unavailable; showing the available summary.</p>
    );
  }
  return null;
}

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className={taskdocSection}>
      <h3 className={taskdocH}>{title}</h3>
      {children}
    </section>
  );
}

export function Bullets({ items }: { items: string[] }) {
  return (
    <ul className={taskdocBullets}>
      {items.map((item) => (
        <li key={item}>
          <Markdown inline>{item}</Markdown>
        </li>
      ))}
    </ul>
  );
}

export function DiscardedSubTaskHistory({ items }: { items: DiscardedSubTaskNode[] }) {
  return (
    <ul className={taskdocBullets}>
      {items.map((item) => (
        <li key={`${item.number}:${item.proof.fingerprint}`}>
          <strong>{item.number}. {item.name}</strong>
          {` — ${item.reason} · ${item.discardedAt} · proof ${item.proof.fingerprint}`}
        </li>
      ))}
    </ul>
  );
}

export function StepList({ steps }: { steps: TaskStepNode[] }) {
  return (
    <ol className={stepsList}>
      {steps.map((s) => (
        <li key={s.id} className={stepRow}>
          <span className={cx(stepMarkBase, STEP_MARK[s.status] ?? "")} aria-hidden="true" />
          <span className={STEP_TITLE[s.status] ?? ""}>
            {labelWithId(s.id, s.title)}
            {s.disposition ? <SkippedDisposition reason={s.disposition.reason} /> : null}
          </span>
          {s.substeps.length > 0 ? (
            <ul className={substeps}>
              {s.substeps.map((sub) => (
                <li key={sub.id} className={SUBSTEP[sub.status] ?? ""}>
                  {labelWithId(sub.id, sub.title)}
                  {sub.disposition ? <SkippedDisposition reason={sub.disposition.reason} /> : null}
                </li>
              ))}
            </ul>
          ) : null}
        </li>
      ))}
    </ol>
  );
}

export function SkippedDisposition({ reason }: { reason: string }) {
  return (
    <span className={skippedDisposition}>
      <span className={skippedWord}>SKIPPED</span> — {reason}
    </span>
  );
}

export function CodeExample({ example }: { example: TaskCodeExampleNode }) {
  return (
    <div className={taskdocCode}>
      <div className={taskdocCodeHead}>{labelWithId(example.id, example.title)}</div>
      <div className={taskdocCodeMeta}>covers: {example.distinctChange}</div>
      <div className={taskdocCodeMeta}>why: {example.why}</div>
      {example.snippet ? <pre className={taskdocSnippet}>{example.snippet}</pre> : null}
    </div>
  );
}

export function DecisionList({ items }: { items: TaskDecisionNode[] }) {
  return (
    <ul className={taskdocDecisions}>
      {items.map((item) => (
        <li key={`${item.at}:${item.decision}`}>
          <div className={taskdocDecision}>
            <Markdown inline>{item.decision}</Markdown>
          </div>
          <div className={taskdocDecisionMeta}>
            {item.at} — <Markdown inline>{item.rationale}</Markdown>
          </div>
        </li>
      ))}
    </ul>
  );
}
