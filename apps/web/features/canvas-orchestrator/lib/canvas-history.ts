// Pure undo/redo history for the canvas (PL-157 phase 2). A past/present/future
// stack over canvas snapshots (nodes + edges). Kept framework-free so it can be
// unit-tested and held in a single React state cell.

import type { CircuitRfEdge, CircuitRfNode } from "./rf-mapping";

export interface CanvasSnapshot {
  nodes: CircuitRfNode[];
  edges: CircuitRfEdge[];
}

export interface CanvasHistory {
  past: CanvasSnapshot[];
  present: CanvasSnapshot;
  future: CanvasSnapshot[];
}

// Cap the undo depth so a long editing session can't grow unbounded.
export const MAX_HISTORY = 50;

export function initHistory(present: CanvasSnapshot): CanvasHistory {
  return { past: [], present, future: [] };
}

// Commit a new snapshot. Pushes the current present onto `past`, clears the
// redo stack. No-ops if the snapshot is referentially identical to present
// (so a committed render doesn't create an empty undo step).
export function commit(history: CanvasHistory, next: CanvasSnapshot): CanvasHistory {
  if (next === history.present) return history;
  const past = [...history.past, history.present];
  if (past.length > MAX_HISTORY) past.shift();
  return { past, present: next, future: [] };
}

export function canUndo(history: CanvasHistory): boolean {
  return history.past.length > 0;
}

export function canRedo(history: CanvasHistory): boolean {
  return history.future.length > 0;
}

export function undo(history: CanvasHistory): CanvasHistory {
  if (!canUndo(history)) return history;
  const previous = history.past[history.past.length - 1]!;
  return {
    past: history.past.slice(0, -1),
    present: previous,
    future: [history.present, ...history.future],
  };
}

export function redo(history: CanvasHistory): CanvasHistory {
  if (!canRedo(history)) return history;
  const next = history.future[0]!;
  return {
    past: [...history.past, history.present],
    present: next,
    future: history.future.slice(1),
  };
}
