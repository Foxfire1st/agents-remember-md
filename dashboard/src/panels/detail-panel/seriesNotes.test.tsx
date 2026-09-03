import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DetailPanel } from "./DetailPanel";
import {
  seedTaskDocuments,
  stubNotes,
  taskDoc,
} from "./test-utils";

describe("DetailPanel series notes (L9)", () => {
  const leafPath = "/tasks/agents-remember/260703_agent-orchestration/09_notes-dashboard.json";

  it("lists the master's notes on a leaf reader and resolves a notes reference to a link", async () => {
    const fn = stubNotes(
      [
        { name: "friction-ledger.md", path: "friction-ledger.md", size: 10, language: "markdown" },
        {
          name: "260703-L1-worker-report.md",
          path: "reports/260703-L1-worker-report.md",
          size: 10,
          language: "markdown",
        },
      ],
      "the **ledger** body",
    );
    const doc = taskDoc({
      id: "260703-L9",
      lifecycleId: undefined,
      kind: "subTask",
      title: "Notes on the dashboard",
      repository: "agents-remember",
      docPath: leafPath,
      references: [
        "notes/friction-ledger.md (F-M — the finding this leaf closes)",
        "mcp/src/agents_remember/serving/files.py (the confinement idiom to reuse)",
      ],
    });
    seedTaskDocuments([doc]);
    const onOpenNotes = vi.fn();
    const view = render(<DetailPanel selectedId={`taskdoc:${leafPath}`} onOpenNotes={onOpenNotes} />);

    // The list is fetched for the doc's OWN series (repo + master derived from the doc node).
    await view.findByText("Series notes");
    const urls = (fn.mock.calls as unknown as string[][]).map((c) => c[0]);
    expect(urls).toContain(
      "/api/notes/list?repo=agents-remember&master=260703_agent-orchestration",
    );
    expect(view.getByText("reports/260703-L1-worker-report.md")).toBeTruthy();

    // The notes-file reference is an openable link into the L17 reader; the code-path reference stays
    // plain text. Reading itself now happens in the reader takeover, so the click opens it (no inline pane).
    fireEvent.click(await view.findByTestId("note-ref-1"));
    expect(onOpenNotes).toHaveBeenCalledWith({
      kind: "notes",
      repo: "agents-remember",
      master: "260703_agent-orchestration",
      path: "friction-ledger.md",
    });
    expect(view.queryByTestId("note-ref-2")).toBeNull();
  });

  it("shows the series notes on a master overview", async () => {
    stubNotes([{ name: "design.md", path: "design.md", size: 10, language: "markdown" }]);
    const master = taskDoc({
      lifecycleId: undefined,
      kind: "master",
      title: "Agent Orchestration",
      repository: "agents-remember",
      docPath: "/tasks/agents-remember/260703_agent-orchestration/task.json",
      objective: "Master objective.",
    });
    seedTaskDocuments([master]);
    const view = render(
      <DetailPanel selectedId="taskdoc:/tasks/agents-remember/260703_agent-orchestration/task.json" />,
    );
    await view.findByText("Series notes");
    expect((await view.findByTestId("note-open-1")).textContent).toContain("design.md");
  });
});
