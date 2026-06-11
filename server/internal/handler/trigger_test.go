package handler

import (
	"context"
	"fmt"
	"testing"

	"github.com/jackc/pgx/v5/pgtype"
	"github.com/multica-ai/multica/server/internal/util"
	db "github.com/multica-ai/multica/server/pkg/db/generated"
)

// Helper to build a pgtype.UUID from a string.
func testUUID(s string) pgtype.UUID {
	return parseUUID(s)
}

// Helper to build a pgtype.Text.
func testText(s string) pgtype.Text {
	return pgtype.Text{String: s, Valid: true}
}

const (
	agentAssigneeID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
	otherAgentID    = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
	memberID        = "cccccccc-cccc-cccc-cccc-cccccccccccc"
	otherMemberID   = "dddddddd-dddd-dddd-dddd-dddddddddddd"
	squadAssigneeID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
	otherSquadID    = "ffffffff-ffff-ffff-ffff-ffffffffffff"
)

func issueWithAgentAssignee() db.Issue {
	return db.Issue{
		AssigneeType: testText("agent"),
		AssigneeID:   testUUID(agentAssigneeID),
	}
}

func issueWithSquadAssignee() db.Issue {
	return db.Issue{
		AssigneeType: testText("squad"),
		AssigneeID:   testUUID(squadAssigneeID),
	}
}

func issueNoAssignee() db.Issue {
	return db.Issue{}
}

// -------------------------------------------------------------------
// commentMentionsOthersButNotAssignee
// -------------------------------------------------------------------

func TestCommentMentionsOthersButNotAssignee(t *testing.T) {
	h := &Handler{} // nil handler — method doesn't use h

	issue := issueWithAgentAssignee()

	tests := []struct {
		name    string
		content string
		want    bool
	}{
		{
			name:    "no mentions → allow trigger",
			content: "just a plain comment",
			want:    false,
		},
		{
			name:    "mentions assignee → allow trigger",
			content: fmt.Sprintf("[@Agent](mention://agent/%s) please fix", agentAssigneeID),
			want:    false,
		},
		{
			name:    "mentions other agent only → suppress",
			content: fmt.Sprintf("[@Other](mention://agent/%s) what do you think?", otherAgentID),
			want:    true,
		},
		{
			name:    "mentions other member only → suppress",
			content: fmt.Sprintf("[@Bob](mention://member/%s) take a look", memberID),
			want:    true,
		},
		{
			name:    "mentions both assignee and other → allow trigger",
			content: fmt.Sprintf("[@Agent](mention://agent/%s) and [@Other](mention://agent/%s)", agentAssigneeID, otherAgentID),
			want:    false,
		},
		{
			name:    "@all mention → suppress (broadcast, not directed at agent)",
			content: "[@All](mention://all/all) heads up everyone",
			want:    true,
		},
		{
			name:    "@all with assignee mention → suppress (@all takes precedence)",
			content: fmt.Sprintf("[@All](mention://all/all) [@Agent](mention://agent/%s) fyi", agentAssigneeID),
			want:    true,
		},
		{
			name:    "issue mention only → allow trigger (cross-reference, not @person)",
			content: "[PAN-1](mention://issue/44c266e7-f6dd-4be3-9140-5ac40233f79c) is related",
			want:    false,
		},
		{
			name:    "issue mention + other agent → suppress (agent mention matters)",
			content: fmt.Sprintf("[PAN-1](mention://issue/44c266e7-f6dd-4be3-9140-5ac40233f79c) cc [@Other](mention://agent/%s)", otherAgentID),
			want:    true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := h.commentMentionsOthersButNotAssignee(tt.content, issue)
			if got != tt.want {
				t.Errorf("commentMentionsOthersButNotAssignee() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestCommentMentionsOthersButNotAssignee_NoAssignee(t *testing.T) {
	h := &Handler{}
	issue := issueNoAssignee()

	// Any mention on an unassigned issue → suppress
	content := fmt.Sprintf("[@Agent](mention://agent/%s) help", otherAgentID)
	if got := h.commentMentionsOthersButNotAssignee(content, issue); !got {
		t.Errorf("expected true for mentions on unassigned issue, got false")
	}
}

// -------------------------------------------------------------------
// isReplyToMemberThread
// -------------------------------------------------------------------

func TestIsReplyToMemberThread(t *testing.T) {
	h := &Handler{}
	issue := issueWithAgentAssignee()

	memberParent := &db.Comment{AuthorType: "member", AuthorID: testUUID(memberID), Content: "plain thread starter"}
	agentParent := &db.Comment{AuthorType: "agent", AuthorID: testUUID(agentAssigneeID), Content: "agent thread starter"}
	// Member-started thread root that @mentions the assignee agent.
	memberParentMentioningAssignee := &db.Comment{
		AuthorType: "member",
		AuthorID:   testUUID(memberID),
		Content:    fmt.Sprintf("[@Agent](mention://agent/%s) can you look at this?", agentAssigneeID),
	}
	// Member-started thread root that @mentions a non-assignee agent.
	memberParentMentioningOther := &db.Comment{
		AuthorType: "member",
		AuthorID:   testUUID(memberID),
		Content:    fmt.Sprintf("[@Other](mention://agent/%s) what do you think?", otherAgentID),
	}

	tests := []struct {
		name    string
		parent  *db.Comment
		content string
		want    bool
	}{
		{
			name:    "top-level comment (nil parent) → allow",
			parent:  nil,
			content: "a comment",
			want:    false,
		},
		{
			name:    "reply to agent thread, no mentions → allow",
			parent:  agentParent,
			content: "sounds good",
			want:    false,
		},
		{
			name:    "reply to agent thread, mention other member → allow (handled by other check)",
			parent:  agentParent,
			content: fmt.Sprintf("[@Bob](mention://member/%s) thoughts?", memberID),
			want:    false, // isReplyToMemberThread only checks member threads
		},
		{
			name:    "reply to member thread, no mentions → suppress",
			parent:  memberParent,
			content: "I agree with you",
			want:    true,
		},
		{
			name:    "reply to member thread, mention other member → suppress",
			parent:  memberParent,
			content: fmt.Sprintf("[@Alice](mention://member/%s) what about this?", otherMemberID),
			want:    true,
		},
		{
			name:    "reply to member thread, mention assignee agent → allow",
			parent:  memberParent,
			content: fmt.Sprintf("[@Agent](mention://agent/%s) can you help?", agentAssigneeID),
			want:    false,
		},
		{
			name:    "reply to member thread, mention other agent (not assignee) → suppress",
			parent:  memberParent,
			content: fmt.Sprintf("[@Other](mention://agent/%s) take a look", otherAgentID),
			want:    true,
		},
		{
			name:    "reply to member thread that @mentioned assignee, no re-mention → allow",
			parent:  memberParentMentioningAssignee,
			content: "here is more context for you",
			want:    false,
		},
		{
			name:    "reply to member thread that @mentioned other agent, no re-mention → suppress",
			parent:  memberParentMentioningOther,
			content: "here is more context",
			want:    true, // parent mentioned other agent, not assignee — still suppress on_comment
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := h.isReplyToMemberThread(context.Background(), tt.parent, tt.content, issue)
			if got != tt.want {
				t.Errorf("isReplyToMemberThread() = %v, want %v", got, tt.want)
			}
		})
	}
}

// -------------------------------------------------------------------
// shouldInheritParentMentions
// -------------------------------------------------------------------

func TestShouldInheritParentMentions(t *testing.T) {
	memberParent := &db.Comment{AuthorType: "member", AuthorID: testUUID(memberID), Content: "thread starter"}
	agentParent := &db.Comment{AuthorType: "agent", AuthorID: testUUID(agentAssigneeID), Content: "agent thread starter"}
	someMention := []util.Mention{{Type: "agent", ID: otherAgentID}}

	tests := []struct {
		name            string
		parent          *db.Comment
		replyMentions   []util.Mention
		replyAuthorType string
		want            bool
	}{
		{"nil parent → false", nil, nil, "member", false},
		{"reply has explicit mentions → false", memberParent, someMention, "member", false},
		{"agent-authored reply, member parent → false (loop guard)", memberParent, nil, "agent", false},
		{"member reply, agent parent → false (parent author guard)", agentParent, nil, "member", false},
		{"member reply, member parent, no mentions → true (intended use)", memberParent, nil, "member", true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := shouldInheritParentMentions(tt.parent, tt.replyMentions, tt.replyAuthorType)
			if got != tt.want {
				t.Errorf("shouldInheritParentMentions() = %v, want %v", got, tt.want)
			}
		})
	}
}

// Regression for the case from MUL-1535: J posts a PR completion comment
// that @mentions GPT-Boy for review; later a member posts a plain follow-up
// reply asking the assignee a question. GPT-Boy must NOT be re-triggered.
func TestShouldInheritParentMentions_AgentReviewDelegationDoesNotLeak(t *testing.T) {
	jPRCompletion := &db.Comment{
		AuthorType: "agent",
		AuthorID:   testUUID(agentAssigneeID),
		Content:    fmt.Sprintf("PR ready. [@GPT-Boy](mention://agent/%s) please review this.", otherAgentID),
	}
	if got := shouldInheritParentMentions(jPRCompletion, nil, "member"); got {
		t.Fatal("member follow-up to an agent's PR-review delegation must not inherit the @reviewer mention")
	}
}

// -------------------------------------------------------------------
// Combined trigger decision (simulates the full on_comment check)
// -------------------------------------------------------------------

func TestOnCommentTriggerDecision(t *testing.T) {
	h := &Handler{}
	issue := issueWithAgentAssignee()

	memberParent := &db.Comment{AuthorType: "member", AuthorID: testUUID(memberID), Content: "plain thread starter"}
	agentParent := &db.Comment{AuthorType: "agent", AuthorID: testUUID(agentAssigneeID), Content: "agent thread starter"}
	memberParentMentioningAssignee := &db.Comment{
		AuthorType: "member",
		AuthorID:   testUUID(memberID),
		Content:    fmt.Sprintf("[@Agent](mention://agent/%s) help me", agentAssigneeID),
	}

	// Simulates the combined check from CreateComment:
	//   !commentMentionsOthersButNotAssignee && !isReplyToMemberThread
	shouldTrigger := func(parent *db.Comment, content string) bool {
		return !h.commentMentionsOthersButNotAssignee(content, issue) &&
			!h.isReplyToMemberThread(context.Background(), parent, content, issue)
	}

	tests := []struct {
		name    string
		parent  *db.Comment
		content string
		want    bool
	}{
		{"top-level, no mention", nil, "hello agent", true},
		{"top-level, mention assignee", nil, fmt.Sprintf("[@Agent](mention://agent/%s) fix this", agentAssigneeID), true},
		{"top-level, mention other only", nil, fmt.Sprintf("[@Other](mention://agent/%s) look", otherAgentID), false},
		{"reply agent thread, no mention", agentParent, "got it", true},
		{"reply agent thread, mention other member", agentParent, fmt.Sprintf("[@Bob](mention://member/%s) ?", memberID), false},
		{"reply agent thread, mention assignee", agentParent, fmt.Sprintf("[@Agent](mention://agent/%s) yes", agentAssigneeID), true},
		{"reply member thread, no mention", memberParent, "agreed", false},
		{"reply member thread, mention other member", memberParent, fmt.Sprintf("[@Bob](mention://member/%s) ok", memberID), false},
		{"reply member thread, mention assignee", memberParent, fmt.Sprintf("[@Agent](mention://agent/%s) help", agentAssigneeID), true},
		{"reply member thread that @mentioned assignee, no re-mention", memberParentMentioningAssignee, "here is more info", true},
		{"top-level, @all broadcast", nil, "[@All](mention://all/all) heads up team", false},
		{"reply agent thread, @all broadcast", agentParent, "[@All](mention://all/all) update for everyone", false},
		{"reply member thread, @all broadcast", memberParent, "[@All](mention://all/all) fyi", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := shouldTrigger(tt.parent, tt.content)
			if got != tt.want {
				t.Errorf("shouldTrigger() = %v, want %v", got, tt.want)
			}
		})
	}
}

// -------------------------------------------------------------------
// isNoteComment — the /note opt-out prefix
// -------------------------------------------------------------------

func TestIsNoteComment(t *testing.T) {
	tests := []struct {
		name    string
		content string
		want    bool
	}{
		{"plain comment triggers", "just a plain comment", false},
		{"note prefix skips", "/note check the API expiry", true},
		{"bare note skips", "/note", true},
		{"uppercase note skips (case-insensitive)", "/NOTE shout", true},
		{"mixed case note skips", "/Note mixed", true},
		{"leading whitespace tolerated", "   /note leading space", true},
		{"note followed by newline skips", "/note\nmultiline body", true},
		{"plural notes does not match (word boundary)", "/notes are plural", false},
		{"noteworthy does not match", "/noteworthy idea", false},
		{"slash space note does not match", "/ note has a space", false},
		{"mid-sentence note does not match", "see foo/note here", false},
		{"note as second token does not match", "fyi /note", false},
		{"empty content does not match", "", false},
		{"whitespace-only content does not match", "   ", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isNoteComment(tt.content); got != tt.want {
				t.Errorf("isNoteComment(%q) = %v, want %v", tt.content, got, tt.want)
			}
		})
	}
}

// TestTriggerTasksForComment_NoteShortCircuits proves a /note comment returns
// before any of the three enqueue paths run. shouldEnqueueOnComment,
// shouldEnqueueSquadLeaderOnComment, and enqueueMentionedAgentTasks all
// dereference h.Queries, so a nil-Queries Handler would panic if the /note
// guard were missing or moved below them. The comment also @mentions an agent
// to exercise the mention path specifically.
func TestTriggerTasksForComment_NoteShortCircuits(t *testing.T) {
	h := &Handler{} // nil Queries / TaskService on purpose
	issue := issueWithAgentAssignee()
	comment := db.Comment{
		Content: fmt.Sprintf("/note cc [@Other](mention://agent/%s) just an fyi", otherAgentID),
	}

	// Must not panic — the guard short-circuits before any DB access.
	h.triggerTasksForComment(context.Background(), issue, comment, nil, "member", memberID, false)
}

// TestTriggerTasksForComment_SuppressTriggersShortCircuits proves the
// request-body-level suppress_triggers flag (CLI --no-trigger) skips ALL three
// enqueue paths even for plain content that @mentions an agent (no /note
// prefix). A nil-Queries Handler would panic in enqueueMentionedAgentTasks if
// the suppress guard were missing, so a clean return proves the gate holds.
func TestTriggerTasksForComment_SuppressTriggersShortCircuits(t *testing.T) {
	h := &Handler{} // nil Queries / TaskService on purpose
	issue := issueWithAgentAssignee()
	comment := db.Comment{
		Content: fmt.Sprintf("cc [@Other](mention://agent/%s) status sync", otherAgentID),
	}

	// suppress=true must short-circuit before any DB access — no panic.
	h.triggerTasksForComment(context.Background(), issue, comment, nil, "member", memberID, true)
}

// TestTriggerTasksForComment_SuppressFalseReachesEnqueue is the negative
// control: the same plain @agent comment WITHOUT suppression must reach the
// mention enqueue path, which dereferences the nil Queries and panics. This
// proves suppression is what stops the wake in the test above, not some other
// short-circuit.
func TestTriggerTasksForComment_SuppressFalseReachesEnqueue(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("expected panic: plain @agent comment with suppress=false must reach the enqueue path")
		}
	}()

	h := &Handler{} // nil Queries / TaskService on purpose
	issue := issueWithAgentAssignee()
	comment := db.Comment{
		Content: fmt.Sprintf("cc [@Other](mention://agent/%s) please run", otherAgentID),
	}

	h.triggerTasksForComment(context.Background(), issue, comment, nil, "member", memberID, false)
}

// -------------------------------------------------------------------
// suppress_triggers gates the squad-leader wake paths (DB-free)
// -------------------------------------------------------------------
//
// These two tests are the Postgres-free equivalents of the assertions in
// squad_comment_trigger_test.go's TestCreateComment_SuppressTriggersBlocksSquadLeader.
// That handler test exercises the real EnqueueTaskForSquadLeader against a
// live queue, so it SKIPS when no database is reachable — which means a
// reviewer without Postgres never actually runs the squad-leader assertion.
// The tests below close that gap: they drive the same chokepoint
// (triggerTasksForComment) with a nil-Queries Handler so they run and assert
// in any environment, no database required.
//
// Mechanism (same idiom as TestTriggerTasksForComment_SuppressFalseReachesEnqueue):
// with h.Queries nil, the first code path that touches the DB panics. The
// suppress=true case must return at the chokepoint BEFORE that path (no
// panic); the suppress=false control must reach it (panic). The panic is thus
// a positive proof that, without suppression, the squad-leader wake path is
// reached — and that suppress_triggers is exactly what prevents it.

// TestTriggerTasksForComment_SuppressGatesSquadLeaderOnComment is the DB-free
// equivalent of the "plain member comment" scenario. On a squad-assigned
// issue with a plain member comment:
//
//   - shouldEnqueueOnComment returns false WITHOUT a DB call (issue is not
//     agent-assigned), so the && short-circuits before any panic.
//   - shouldEnqueueSquadLeaderOnComment is the FIRST path to dereference
//     h.Queries (GetSquadInWorkspace) — the on-comment squad-leader wake.
//
// suppress=true must short-circuit before it (no panic); suppress=false must
// reach it (panic), proving suppression alone gates the squad-leader wake.
func TestTriggerTasksForComment_SuppressGatesSquadLeaderOnComment(t *testing.T) {
	issue := issueWithSquadAssignee()
	comment := db.Comment{Content: "what is the latest on this?"}

	t.Run("suppress=true short-circuits before the squad-leader path", func(t *testing.T) {
		h := &Handler{} // nil Queries on purpose
		// Must NOT panic: the suppress guard returns before
		// shouldEnqueueSquadLeaderOnComment touches the nil Queries.
		h.triggerTasksForComment(context.Background(), issue, comment, nil, "member", memberID, true)
	})

	t.Run("suppress=false reaches the squad-leader path", func(t *testing.T) {
		defer func() {
			if recover() == nil {
				t.Fatal("expected panic: a plain member comment on a squad-assigned issue with suppress=false must reach the squad-leader wake (shouldEnqueueSquadLeaderOnComment → GetSquadInWorkspace)")
			}
		}()
		h := &Handler{} // nil Queries on purpose
		h.triggerTasksForComment(context.Background(), issue, comment, nil, "member", memberID, false)
	})
}

// TestTriggerTasksForComment_SuppressGatesSquadMentionWake is the DB-free
// equivalent of the "mention://squad" scenario. The comment @mentions a
// squad, which routes to that squad's leader via enqueueMentionedAgentTasks.
// The issue itself is left UNASSIGNED so the earlier on-comment paths
// (shouldEnqueueOnComment, shouldEnqueueSquadLeaderOnComment) both return
// false without a DB call, isolating the @squad mention branch as the first —
// and only — path that dereferences h.Queries (GetSquadInWorkspace).
//
// suppress=true must short-circuit before it (no panic); suppress=false must
// reach it (panic), proving suppression gates the @squad mention wake too.
func TestTriggerTasksForComment_SuppressGatesSquadMentionWake(t *testing.T) {
	issue := issueNoAssignee()
	comment := db.Comment{
		Content: fmt.Sprintf("handing to [@Squad](mention://squad/%s)", otherSquadID),
	}

	t.Run("suppress=true short-circuits before the @squad mention path", func(t *testing.T) {
		h := &Handler{} // nil Queries on purpose
		// Must NOT panic: the suppress guard returns before
		// enqueueMentionedAgentTasks resolves the @squad mention.
		h.triggerTasksForComment(context.Background(), issue, comment, nil, "member", memberID, true)
	})

	t.Run("suppress=false reaches the @squad mention path", func(t *testing.T) {
		defer func() {
			if recover() == nil {
				t.Fatal("expected panic: a @squad-mention comment with suppress=false must reach the squad-leader wake (enqueueMentionedAgentTasks → GetSquadInWorkspace)")
			}
		}()
		h := &Handler{} // nil Queries on purpose
		h.triggerTasksForComment(context.Background(), issue, comment, nil, "member", memberID, false)
	})
}
