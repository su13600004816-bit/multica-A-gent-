package main

import (
	"context"
	"time"

	"github.com/multica-ai/multica/server/internal/service"
)

const lineRunnerInterval = 15 * time.Second

// runLineRunner ticks the line runner: every interval it advances all active
// line runs by one deterministic step (dispatch ready stages, gate on issue
// status, rework failed gates). It is the piece that keeps pipelined tasks
// moving instead of stalling at a stage transition.
func runLineRunner(ctx context.Context, svc *service.LineRunnerService) {
	ticker := time.NewTicker(lineRunnerInterval)
	defer ticker.Stop()

	// Advance once on startup so a run created while the process was down
	// doesn't wait a full interval.
	svc.Tick(ctx)

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			svc.Tick(ctx)
		}
	}
}
