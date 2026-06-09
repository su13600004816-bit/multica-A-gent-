package main

import (
	"context"
	"log/slog"
	"time"

	"github.com/multica-ai/multica/server/internal/service"
)

// stageWatchdogInterval is how often the stage orchestrator watchdog scans for
// stalled pipeline stages. It is intentionally finer than the 5-minute stall
// timeout so a stall is caught within ~1 minute of crossing the threshold.
const stageWatchdogInterval = time.Minute

// runStageWatchdog periodically asks the stage orchestrator to remind the line
// leader about any pipeline stage that completed but stalled (no next action
// within the stall timeout). See service.StageOrchestrator for the contract.
func runStageWatchdog(ctx context.Context, orch *service.StageOrchestrator) {
	if orch == nil {
		return
	}
	ticker := time.NewTicker(stageWatchdogInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if n := orch.RunWatchdogOnce(ctx); n > 0 {
				slog.Info("stage watchdog: reminders sent", "count", n)
			}
		}
	}
}
