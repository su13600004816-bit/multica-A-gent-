package service

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/multica-ai/multica/server/internal/events"
	"github.com/multica-ai/multica/server/internal/issueposition"
	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
	"github.com/multica-ai/multica/server/pkg/protocol"
)

// defaultLineMaxRework bounds how many times a gate can bounce its upstream
// work node back for rework before the run is marked failed.
const defaultLineMaxRework = 3

// LineRunnerService deterministically advances production lines. Unlike the
// LLM squad leader (which stalls when a stage transition is missed), the runner
// is a backend goroutine: each tick it polls every active run, dispatches the
// next ready stage as a multica issue, gates advancement on issue terminal
// status, and reworks failed gates up to a bounded number of rounds.
type LineRunnerService struct {
	Queries   *db.Queries
	TxStarter TxStarter
	Bus       *events.Bus
	TaskSvc   *TaskService
}

func NewLineRunnerService(q *db.Queries, tx TxStarter, bus *events.Bus, taskSvc *TaskService) *LineRunnerService {
	return &LineRunnerService{Queries: q, TxStarter: tx, Bus: bus, TaskSvc: taskSvc}
}

// Tick advances every active line run by one step. All transitions are
// idempotent and persisted per run, so repeated calls are safe.
func (s *LineRunnerService) Tick(ctx context.Context) {
	runs, err := s.Queries.ListActiveLineRuns(ctx)
	if err != nil {
		slog.Warn("line runner: list active runs failed", "error", err)
		return
	}
	for _, run := range runs {
		if err := s.advanceRun(ctx, run); err != nil {
			slog.Warn("line runner: advance run failed",
				"run_id", util.UUIDToString(run.ID), "error", err)
		}
	}
}

func (s *LineRunnerService) advanceRun(ctx context.Context, run db.LineRun) error {
	graph, err := parseLineGraph(run.Graph)
	if err != nil {
		return err
	}
	state := map[string]*LineNodeState{}
	if len(run.NodeState) > 0 {
		if err := json.Unmarshal(run.NodeState, &state); err != nil {
			return fmt.Errorf("parse node_state: %w", err)
		}
	}

	nodeByID := make(map[string]LineNode, len(graph.Nodes))
	for _, n := range graph.Nodes {
		nodeByID[n.ID] = n
		if state[n.ID] == nil {
			state[n.ID] = &LineNodeState{Status: "pending"}
		}
	}
	preds := make(map[string][]string)
	for _, e := range graph.Edges {
		preds[e.To] = append(preds[e.To], e.From)
	}

	// 1. Poll dispatched nodes for terminal issue status.
	for id, st := range state {
		if st.Status != "dispatched" || st.IssueID == "" {
			continue
		}
		iid, perr := util.ParseUUID(st.IssueID)
		if perr != nil {
			continue
		}
		issue, gerr := s.Queries.GetIssue(ctx, iid)
		if gerr != nil {
			slog.Warn("line runner: get issue failed", "issue_id", st.IssueID, "error", gerr)
			continue
		}
		switch issue.Status {
		case "done":
			st.Status = "done"
		case "cancelled", "wont_do", "archived":
			node := nodeByID[id]
			max := node.MaxRework
			if max <= 0 {
				max = defaultLineMaxRework
			}
			if node.Kind == "gate" && node.GateFor != "" && st.ReworkRound < max {
				// Gate rejected the work: bounce the upstream work node back
				// for another round, and re-run this gate after it.
				if tgt := state[node.GateFor]; tgt != nil {
					tgt.Status = "pending"
					tgt.ReworkRound++
					tgt.IssueID = ""
				}
				st.Status = "pending"
				st.ReworkRound++
				st.IssueID = ""
			} else {
				st.Status = "failed"
			}
		}
	}

	// 2. Cascade: a done/dispatched node whose predecessor is no longer done
	//    (an upstream rework just reopened it) must itself be redone.
	for moved := true; moved; {
		moved = false
		for id, st := range state {
			if st.Status != "done" && st.Status != "dispatched" {
				continue
			}
			for _, p := range preds[id] {
				if ps := state[p]; ps == nil || ps.Status != "done" {
					st.Status = "pending"
					st.IssueID = ""
					moved = true
					break
				}
			}
		}
	}

	// 3. Dispatch ready pending nodes (all predecessors done).
	for _, n := range graph.Nodes {
		st := state[n.ID]
		if st.Status != "pending" {
			continue
		}
		ready := true
		for _, p := range preds[n.ID] {
			if ps := state[p]; ps == nil || ps.Status != "done" {
				ready = false
				break
			}
		}
		if !ready {
			continue
		}
		issue, derr := s.dispatchNode(ctx, run, n, st.ReworkRound)
		if derr != nil {
			// Transient (e.g. assignee runtime offline): leave pending, retry
			// next tick rather than failing the whole run.
			slog.Warn("line runner: dispatch node failed",
				"run_id", util.UUIDToString(run.ID), "node", n.ID, "error", derr)
			continue
		}
		st.IssueID = util.UUIDToString(issue.ID)
		st.IssueNumber = issue.Number
		st.Status = "dispatched"
	}

	// 4. Compute run status.
	runStatus := "running"
	allDone, anyFailed := true, false
	for _, n := range graph.Nodes {
		switch state[n.ID].Status {
		case "failed":
			anyFailed = true
		case "done":
		default:
			allDone = false
		}
	}
	switch {
	case anyFailed:
		runStatus = "failed"
	case allDone:
		runStatus = "passed"
	}

	// 5. Persist.
	stateJSON, merr := json.Marshal(state)
	if merr != nil {
		return fmt.Errorf("marshal node_state: %w", merr)
	}
	if _, err := s.Queries.UpdateLineRunState(ctx, db.UpdateLineRunStateParams{
		ID:        run.ID,
		NodeState: stateJSON,
		Status:    runStatus,
		Error:     pgtype.Text{},
	}); err != nil {
		return fmt.Errorf("persist line run: %w", err)
	}
	return nil
}

// dispatchNode creates an issue for the node and enqueues an agent task, the
// same path autopilot uses. The issue's origin is the line so created work is
// traceable back to the pipeline.
func (s *LineRunnerService) dispatchNode(ctx context.Context, run db.LineRun, node LineNode, round int) (db.Issue, error) {
	agentID, perr := util.ParseUUID(node.AssigneeID)
	if perr != nil {
		return db.Issue{}, fmt.Errorf("node %q: invalid assignee_id: %w", node.ID, perr)
	}

	tx, err := s.TxStarter.Begin(ctx)
	if err != nil {
		return db.Issue{}, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)
	qtx := s.Queries.WithTx(tx)

	title := node.Title
	if title == "" {
		title = node.ID
	}
	if round > 0 {
		title = fmt.Sprintf("%s（返工轮%d）", title, round)
	}

	number, err := qtx.IncrementIssueCounter(ctx, run.WorkspaceID)
	if err != nil {
		return db.Issue{}, fmt.Errorf("increment issue counter: %w", err)
	}
	pos, err := issueposition.NextTopPosition(ctx, tx, run.WorkspaceID, "todo")
	if err != nil {
		return db.Issue{}, fmt.Errorf("next position: %w", err)
	}
	issue, err := qtx.CreateIssueWithOrigin(ctx, db.CreateIssueWithOriginParams{
		WorkspaceID:   run.WorkspaceID,
		Title:         title,
		Description:   pgtype.Text{String: buildLineNodeDescription(node), Valid: true},
		Status:        "todo",
		Priority:      "none",
		AssigneeType:  pgtype.Text{String: "agent", Valid: true},
		AssigneeID:    agentID,
		CreatorType:   "agent",
		CreatorID:     agentID,
		ParentIssueID: pgtype.UUID{},
		Position:      pos,
		StartDate:     pgtype.Date{},
		DueDate:       pgtype.Date{},
		Number:        number,
		ProjectID:     pgtype.UUID{},
		OriginType:    pgtype.Text{String: "line", Valid: true},
		OriginID:      run.LineID,
	})
	if err != nil {
		return db.Issue{}, fmt.Errorf("create issue: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return db.Issue{}, fmt.Errorf("commit: %w", err)
	}

	prefix := s.TaskSvc.getIssuePrefix(run.WorkspaceID)
	s.Bus.Publish(events.Event{
		Type:        protocol.EventIssueCreated,
		WorkspaceID: util.UUIDToString(run.WorkspaceID),
		ActorType:   "agent",
		ActorID:     util.UUIDToString(agentID),
		Payload:     map[string]any{"issue": issueToMap(issue, prefix)},
	})

	if _, err := s.TaskSvc.EnqueueTaskForIssue(ctx, issue); err != nil {
		return db.Issue{}, fmt.Errorf("enqueue task: %w", err)
	}

	slog.Info("line runner: dispatched node",
		"run_id", util.UUIDToString(run.ID),
		"line_id", util.UUIDToString(run.LineID),
		"node", node.ID,
		"issue_number", issue.Number,
		"round", round,
	)
	return issue, nil
}

func buildLineNodeDescription(node LineNode) string {
	out := node.Instruction
	if node.Role != "" {
		out = fmt.Sprintf("[%s] %s", node.Role, out)
	}
	if len(node.OwnedPaths) > 0 {
		out += "\n\n负责路径(owned_paths):\n"
		for _, p := range node.OwnedPaths {
			out += "- " + p + "\n"
		}
	}
	return out
}
