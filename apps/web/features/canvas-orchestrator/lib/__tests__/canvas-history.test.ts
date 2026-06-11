import { describe, expect, it } from "vitest";

import {
  canRedo,
  canUndo,
  commit,
  initHistory,
  MAX_HISTORY,
  redo,
  undo,
  type CanvasSnapshot,
} from "../canvas-history";

function snap(label: string): CanvasSnapshot {
  // Distinct object identity per label; node payload is irrelevant to history.
  return { nodes: [{ id: label } as never], edges: [] };
}

describe("canvas-history", () => {
  it("starts with no undo/redo available", () => {
    const h = initHistory(snap("a"));
    expect(canUndo(h)).toBe(false);
    expect(canRedo(h)).toBe(false);
  });

  it("commit pushes present onto past and clears redo", () => {
    let h = initHistory(snap("a"));
    h = commit(h, snap("b"));
    expect(h.present.nodes[0]!.id).toBe("b");
    expect(canUndo(h)).toBe(true);
    expect(canRedo(h)).toBe(false);
  });

  it("undo then redo restores the prior present", () => {
    let h = initHistory(snap("a"));
    const b = snap("b");
    h = commit(h, b);
    h = undo(h);
    expect(h.present.nodes[0]!.id).toBe("a");
    expect(canRedo(h)).toBe(true);
    h = redo(h);
    expect(h.present.nodes[0]!.id).toBe("b");
  });

  it("a fresh commit after undo drops the redo branch", () => {
    let h = initHistory(snap("a"));
    h = commit(h, snap("b"));
    h = undo(h);
    h = commit(h, snap("c"));
    expect(h.present.nodes[0]!.id).toBe("c");
    expect(canRedo(h)).toBe(false);
  });

  it("commit of the identical present is a no-op", () => {
    const h = initHistory(snap("a"));
    expect(commit(h, h.present)).toBe(h);
  });

  it("undo/redo on an empty branch are no-ops", () => {
    const h = initHistory(snap("a"));
    expect(undo(h)).toBe(h);
    expect(redo(h)).toBe(h);
  });

  it("caps the past stack at MAX_HISTORY", () => {
    let h = initHistory(snap("0"));
    for (let i = 1; i <= MAX_HISTORY + 10; i += 1) {
      h = commit(h, snap(String(i)));
    }
    expect(h.past.length).toBe(MAX_HISTORY);
  });
});
