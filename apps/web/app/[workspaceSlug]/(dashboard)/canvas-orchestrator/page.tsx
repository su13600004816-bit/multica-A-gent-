"use client";

import { CanvasOrchestrator } from "@/features/canvas-orchestrator/components";
import { ErrorBoundary } from "@multica/ui/components/common/error-boundary";

export default function CanvasOrchestratorRoute() {
  // SidebarInset is `relative overflow-hidden`; fill it so the canvas owns the
  // whole content viewport.
  return (
    <div className="absolute inset-0">
      <ErrorBoundary>
        <CanvasOrchestrator />
      </ErrorBoundary>
    </div>
  );
}
