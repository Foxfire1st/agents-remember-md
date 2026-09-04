import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { dashboardStore } from "../data/store";
import type { EnclosureNode } from "../types/projection";
import { Hangar } from "./Hangar";

function enclosure(partial: Partial<EnclosureNode> & { enclosure: string }): EnclosureNode {
  const { enclosure: enclosurePath, ...rest } = partial;
  return {
    actions: [],
    cleanup: "pending",
    closeoutStatus: "not-started",
    codeWorktreeExists: true,
    memoryWorktreeExists: true,
    enclosure: enclosurePath,
    enclosureId: enclosurePath,
    humanReviewStatus: "pending-review",
    integrationStatus: "not-started",
    leafId: "leaf",
    lifecycleId: "lc-1",
    repoName: "agents-remember",
    taskId: "T",
    taskName: "t",
    taskRoot: "tasks/agents-remember/t",
    worktreeGroup: "g-ar",
    ...rest,
  };
}

afterEach(() => {
  cleanup();
  dashboardStore.getState().reset();
});

describe("Hangar renders worktree truth (L11)", () => {
  it("renders a row ONLY while a worktree physically exists — never from a cleanup-state proxy", () => {
    // A finalized worktree keeps its enclosure contract on disk after its directories are reaped,
    // and a reopened contract (cleanup=reopened) has no worktrees until worktree_start recreates
    // them. Visibility is the projection's stat'ed existence truth: of these four, only the two
    // with a physically existing worktree survive.
    dashboardStore.setState({
      lifecycles: {},
      enclosures: {
        live: enclosure({ enclosure: "live", cleanup: "pending" }),
        "memory-only": enclosure({
          enclosure: "memory-only",
          cleanup: "pending",
          codeWorktreeExists: false,
          memoryWorktreeExists: true,
        }),
        done: enclosure({
          enclosure: "done",
          cleanup: "completed",
          codeWorktreeExists: false,
          memoryWorktreeExists: false,
        }),
        gone: enclosure({
          enclosure: "gone",
          cleanup: "abandoned",
          codeWorktreeExists: false,
          memoryWorktreeExists: false,
        }),
      },
    });

    const { getByTestId, getAllByTestId } = render(<Hangar onSelect={vi.fn()} />);

    expect(getAllByTestId("hangar-row")).toHaveLength(2);
    expect(getByTestId("hangar").textContent).toContain("Hangar · 2 worktrees");
  });

  it("hides a reopened contract with no worktrees on disk (reset-awaiting-restart, not live work)", () => {
    // The L9 reopen defect: cleanup=reopened outflanked the old {completed, abandoned} proxy and
    // the leaf rendered as a live worktree. With existence truth it stays hidden until restart.
    dashboardStore.setState({
      lifecycles: {},
      enclosures: {
        reopened: enclosure({
          enclosure: "reopened",
          cleanup: "reopened",
          lifecycleId: "",
          codeWorktreeExists: false,
          memoryWorktreeExists: false,
        }),
      },
    });

    const { queryAllByTestId, getByText } = render(<Hangar onSelect={vi.fn()} />);

    expect(queryAllByTestId("hangar-row")).toHaveLength(0);
    expect(getByText(/no live persistent worktrees/i)).not.toBeNull();
  });

  it("shows a reopened leaf again once worktree_start recreates its worktrees", () => {
    // After restart the worktrees physically exist again; existence — not the cleanup label —
    // is what re-admits the row, so even a not-yet-rewritten contract renders.
    dashboardStore.setState({
      lifecycles: {},
      enclosures: {
        restarted: enclosure({
          enclosure: "restarted",
          cleanup: "reopened",
          codeWorktreeExists: true,
          memoryWorktreeExists: true,
        }),
      },
    });

    const { getAllByTestId } = render(<Hangar onSelect={vi.fn()} />);

    expect(getAllByTestId("hangar-row")).toHaveLength(1);
  });

  it("fully reduces to the empty state once every worktree is physically gone", () => {
    dashboardStore.setState({
      lifecycles: {},
      enclosures: {
        a: enclosure({
          enclosure: "a",
          cleanup: "completed",
          codeWorktreeExists: false,
          memoryWorktreeExists: false,
        }),
        b: enclosure({
          enclosure: "b",
          cleanup: "completed",
          codeWorktreeExists: false,
          memoryWorktreeExists: false,
        }),
      },
    });

    const { queryAllByTestId, getByText } = render(<Hangar onSelect={vi.fn()} />);

    expect(queryAllByTestId("hangar-row")).toHaveLength(0);
    expect(getByText(/no live persistent worktrees/i)).not.toBeNull();
  });

  it("shows the durable live command for a running lifecycle operation", () => {
    const currentCommand = "quality: dagger — run targeted pytest shard 7/16";
    dashboardStore.setState({
      lifecycles: {},
      enclosures: {
        running: enclosure({
          enclosure: "running",
          lifecycleOperation: {
            cancellable: true,
            currentCommand,
            elapsedSeconds: 42,
            kind: "closeout",
            legalControls: [],
            phase: "quality",
            projectionEffects: [],
            reportPath: "reports/closeout-operation.log",
            schemaVersion: "lifecycle-operation-projection/v1",
            stateMatrixVersion: "lifecycle-operation-state-matrix/v1",
            status: "running",
          },
        }),
      },
    });

    const { getByTestId } = render(<Hangar onSelect={vi.fn()} />);
    const operation = getByTestId("hangar-lifecycle-operation");

    expect(operation.textContent).toContain(currentCommand);
    expect(operation.getAttribute("title")).toBe(currentCommand);
  });
});
