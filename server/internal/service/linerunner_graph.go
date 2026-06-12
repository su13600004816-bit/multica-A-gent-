package service

import (
	"encoding/json"
	"fmt"
	"sort"
)

// LineNode is a single stage in a production line. Each node maps to one
// multica issue when the line runner dispatches it.
type LineNode struct {
	ID           string   `json:"id"`
	Title        string   `json:"title"`
	Kind         string   `json:"kind"`          // "work" (default) | "gate"
	ExecutorType string   `json:"executor_type"` // claude | codex (advisory; the assignee agent picks the runtime)
	Role         string   `json:"role"`          // dev | audit (advisory)
	AssigneeType string   `json:"assignee_type"` // currently only "agent"
	AssigneeID   string   `json:"assignee_id"`   // agent uuid
	Instruction  string   `json:"instruction"`
	OwnedPaths   []string `json:"owned_paths,omitempty"`
	GateFor      string   `json:"gate_for,omitempty"` // for kind=gate: node id this gate audits
	MaxRework    int      `json:"max_rework,omitempty"`
}

// LineEdge is a dependency: To may not start until From has completed.
type LineEdge struct {
	From string `json:"from"`
	To   string `json:"to"`
}

// LineGraph is the persisted pipeline definition.
type LineGraph struct {
	Nodes []LineNode `json:"nodes"`
	Edges []LineEdge `json:"edges"`
}

// LineNodeState is the per-node runtime state tracked in line_run.node_state.
type LineNodeState struct {
	IssueID     string `json:"issue_id,omitempty"`
	IssueNumber int32  `json:"issue_number,omitempty"`
	Status      string `json:"status"` // pending | dispatched | done | failed
	ReworkRound int    `json:"rework_round"`
}

func parseLineGraph(raw []byte) (LineGraph, error) {
	var g LineGraph
	if len(raw) == 0 {
		return g, nil
	}
	if err := json.Unmarshal(raw, &g); err != nil {
		return g, fmt.Errorf("parse line graph: %w", err)
	}
	return g, nil
}

// ValidateLineGraph rejects malformed graphs before a line is stored or run:
// empty, missing/dup ids, dangling edges/gate refs, non-agent assignees, cycles.
func ValidateLineGraph(g LineGraph) error {
	if len(g.Nodes) == 0 {
		return fmt.Errorf("line has no nodes")
	}
	ids := make(map[string]bool, len(g.Nodes))
	for _, n := range g.Nodes {
		if n.ID == "" {
			return fmt.Errorf("node with empty id")
		}
		if ids[n.ID] {
			return fmt.Errorf("duplicate node id %q", n.ID)
		}
		ids[n.ID] = true
		if n.AssigneeType != "" && n.AssigneeType != "agent" {
			return fmt.Errorf("node %q: only assignee_type=agent is supported", n.ID)
		}
		if n.AssigneeID == "" {
			return fmt.Errorf("node %q: missing assignee_id", n.ID)
		}
	}
	for _, n := range g.Nodes {
		if n.Kind == "gate" && n.GateFor != "" && !ids[n.GateFor] {
			return fmt.Errorf("gate node %q references unknown gate_for %q", n.ID, n.GateFor)
		}
	}
	for _, e := range g.Edges {
		if !ids[e.From] || !ids[e.To] {
			return fmt.Errorf("edge references unknown node: %s -> %s", e.From, e.To)
		}
	}
	if _, err := CompileToWaves(g); err != nil {
		return err
	}
	return nil
}

// CompileToWaves performs a Kahn topological sort, grouping nodes that can run
// concurrently into the same wave. Returns an error if the graph has a cycle.
// Order within and across waves is deterministic (sorted by id).
func CompileToWaves(g LineGraph) ([][]LineNode, error) {
	nodeByID := make(map[string]LineNode, len(g.Nodes))
	indeg := make(map[string]int, len(g.Nodes))
	for _, n := range g.Nodes {
		nodeByID[n.ID] = n
		indeg[n.ID] = 0
	}
	adj := make(map[string][]string)
	for _, e := range g.Edges {
		adj[e.From] = append(adj[e.From], e.To)
		indeg[e.To]++
	}
	var frontier []string
	for id, d := range indeg {
		if d == 0 {
			frontier = append(frontier, id)
		}
	}
	sort.Strings(frontier)
	var waves [][]LineNode
	seen := 0
	for len(frontier) > 0 {
		var wave []LineNode
		var next []string
		for _, id := range frontier {
			wave = append(wave, nodeByID[id])
			seen++
			for _, m := range adj[id] {
				indeg[m]--
				if indeg[m] == 0 {
					next = append(next, m)
				}
			}
		}
		sort.Strings(next)
		waves = append(waves, wave)
		frontier = next
	}
	if seen != len(g.Nodes) {
		return nil, fmt.Errorf("line graph has a cycle")
	}
	return waves, nil
}
