import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NoteEntry } from "../data/notes";
import { TaskNotes } from "./TaskNotes";

// L17: TaskNotes is now the compact ENTRY SURFACE — a list + resolved references that OPEN the notes
// reader via `onOpenNotes`. The inline reading pane was retired; note CONTENT rendering (markdown /
// text fallback / binary placeholder / truncation) is now covered by NotesReaderViewer.test.tsx.
const REPO = "agents-remember";
const MASTER = "260703_agent-orchestration";

function entry(path: string, over: Partial<NoteEntry> = {}): NoteEntry {
  return { name: path.split("/").pop() ?? path, path, size: 100, language: "markdown", ...over };
}

// The notes API stub: /api/notes/list answers the seeded listing (the reader owns /api/notes/read now).
function stubNotesApi(notes: NoteEntry[], truncated = false) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const params = new URLSearchParams(url.split("?")[1] ?? "");
      if (url.startsWith("/api/notes/list")) {
        const payload = { repo: params.get("repo"), master: params.get("master"), notes, truncated };
        return { ok: true, status: 200, json: async () => payload } as unknown as Response;
      }
      return { ok: false, status: 404, json: async () => ({ status: "not-found" }) } as unknown as Response;
    }),
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("TaskNotes entry surface", () => {
  it("lists the series notes including reports/ subfolder entries", async () => {
    stubNotesApi([entry("friction-ledger.md"), entry("reports/260703-L1-worker-report.md")]);
    const view = render(<TaskNotes repo={REPO} master={MASTER} references={[]} onOpenNotes={vi.fn()} />);
    expect((await view.findByTestId("note-open-1")).textContent).toContain("friction-ledger.md");
    expect(view.getByText("Series notes")).toBeTruthy();
    expect(view.getByText("reports/260703-L1-worker-report.md")).toBeTruthy();
  });

  it("opens the notes reader on the clicked note when a list row is clicked", async () => {
    stubNotesApi([entry("friction-ledger.md"), entry("reports/260703-L1-worker-report.md")]);
    const onOpenNotes = vi.fn();
    const view = render(<TaskNotes repo={REPO} master={MASTER} references={[]} onOpenNotes={onOpenNotes} />);
    fireEvent.click(await view.findByTestId("note-open-2"));
    expect(onOpenNotes).toHaveBeenCalledWith({
      kind: "notes",
      repo: REPO,
      master: MASTER,
      path: "reports/260703-L1-worker-report.md",
    });
  });

  it("says when the server's depth cap pruned the listing", async () => {
    stubNotesApi([entry("a.md")], true);
    const view = render(<TaskNotes repo={REPO} master={MASTER} references={[]} onOpenNotes={vi.fn()} />);
    await view.findByTestId("note-open-1");
    expect(view.getByText(/beyond the list cap/)).toBeTruthy();
  });

  it("renders no notes surface when the API is unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("unreachable"))));
    const view = render(
      <TaskNotes repo={REPO} master={MASTER} references={["notes/friction-ledger.md"]} onOpenNotes={vi.fn()} />,
    );
    // The reference stays plain text; nothing to list, nothing to open.
    expect(await view.findByText("notes/friction-ledger.md")).toBeTruthy();
    expect(view.queryByTestId("note-ref-1")).toBeNull();
    expect(view.queryByText("Series notes")).toBeNull();
  });
});

describe("TaskNotes reference resolution", () => {
  it("turns a reference naming an existing notes file into a link that opens the reader on it", async () => {
    stubNotesApi([entry("friction-ledger.md")]);
    const onOpenNotes = vi.fn();
    const view = render(
      <TaskNotes
        repo={REPO}
        master={MASTER}
        references={["notes/friction-ledger.md (F-M — the finding this leaf closes)"]}
        onOpenNotes={onOpenNotes}
      />,
    );
    const link = await view.findByTestId("note-ref-1");
    expect(link.tagName.toLowerCase()).toBe("button");
    fireEvent.click(link);
    expect(onOpenNotes).toHaveBeenCalledWith({
      kind: "notes",
      repo: REPO,
      master: MASTER,
      path: "friction-ledger.md",
    });
  });

  it("keeps a non-matching reference as plain text", async () => {
    stubNotesApi([entry("friction-ledger.md")]);
    const view = render(
      <TaskNotes
        repo={REPO}
        master={MASTER}
        references={["mcp/src/agents_remember/serving/files.py (the confinement idiom to reuse)"]}
        onOpenNotes={vi.fn()}
      />,
    );
    await view.findByTestId("note-open-1"); // the listing has arrived — resolution is settled
    expect(view.queryByTestId("note-ref-1")).toBeNull();
    expect(view.getByText(/the confinement idiom to reuse/)).toBeTruthy();
  });
});
