package memorycompact

import (
	"context"
	"fmt"
	"strings"
	"time"
)

// ArchiveRecord is one row to persist into memory_archive. Strings are used
// for ids so this package stays independent of the db row types (and the
// sqlc-generated package); the caller adapts these to db params.
type ArchiveRecord struct {
	WorkspaceID      string
	ScopeType        string
	ScopeID          string
	Level            string
	Content          string
	Generator        string
	SourceFrom       time.Time
	SourceTo         time.Time
	Count            int
	CreatedByAgentID string // "" => NULL
}

// Store persists archives and flips the scope's compaction marker. It is the
// seam onto db.Queries; the production adapter lives next to the task service.
type Store interface {
	SaveArchive(ctx context.Context, rec ArchiveRecord) error
	// MarkCompacted stamps the scope compacted and, for chat sessions, cuts
	// the provider session pointer loose. MUST be a no-op-safe idempotent
	// write for unsupported scope types.
	MarkCompacted(ctx context.Context, scopeType, scopeID string) error
}

// Archiver orchestrates compaction + persistence with fail-safe ordering.
type Archiver struct {
	Compactor Compactor
	Store     Store
}

// Archive compacts the span and persists every non-empty level, then — and
// only then — flips the compaction marker that cuts the old session loose.
//
// Fail-safe contract:
//   - The original transcript is never touched here.
//   - If compaction or any level save fails, the marker is NOT set, so the
//     daemon keeps resuming the old session as before: no 止血, but no loss.
//   - The marker is set last, so a half-written archive can never strand a
//     scope with its session cut but no summary to inject.
func (a Archiver) Archive(ctx context.Context, in Input, workspaceID, createdByAgentID string) (Levels, error) {
	levels, err := a.Compactor.Compact(ctx, in)
	if err != nil {
		// Deterministic never errors; a model compactor already falls back.
		// Reaching here means a custom compactor failed hard — bail without
		// marking, preserving the original session.
		return Levels{}, fmt.Errorf("compact %s/%s: %w", in.ScopeType, in.ScopeID, err)
	}

	for _, lv := range AllLevels {
		content := levels.Get(lv)
		if strings.TrimSpace(content) == "" {
			continue
		}
		rec := ArchiveRecord{
			WorkspaceID:      workspaceID,
			ScopeType:        in.ScopeType,
			ScopeID:          in.ScopeID,
			Level:            string(lv),
			Content:          content,
			Generator:        levels.Generator,
			SourceFrom:       levels.SourceFrom,
			SourceTo:         levels.SourceTo,
			Count:            levels.Count,
			CreatedByAgentID: createdByAgentID,
		}
		if err := a.Store.SaveArchive(ctx, rec); err != nil {
			return levels, fmt.Errorf("save archive %s for %s/%s: %w", lv, in.ScopeType, in.ScopeID, err)
		}
	}

	if err := a.Store.MarkCompacted(ctx, in.ScopeType, in.ScopeID); err != nil {
		return levels, fmt.Errorf("mark compacted %s/%s: %w", in.ScopeType, in.ScopeID, err)
	}
	return levels, nil
}
