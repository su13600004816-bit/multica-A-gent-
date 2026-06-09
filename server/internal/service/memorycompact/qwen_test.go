package memorycompact

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestQwenClient_Summarize_OK(t *testing.T) {
	var gotAuth, gotPath string
	var gotReq qwenChatRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		gotPath = r.URL.Path
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &gotReq)
		w.Header().Set("Content-Type", "application/json")
		io.WriteString(w, `{"choices":[{"message":{"role":"assistant","content":"  归档摘要 T2  "}}]}`)
	}))
	defer srv.Close()

	q := &QwenClient{BaseURL: srv.URL, APIKey: "sk-test", Model: "qwen-plus", MaxTokens: 1500, HTTP: srv.Client()}
	out, err := q.Summarize(context.Background(), LevelT2, sampleInput().Messages)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != "归档摘要 T2" {
		t.Errorf("content = %q, want trimmed '归档摘要 T2'", out)
	}
	if gotAuth != "Bearer sk-test" {
		t.Errorf("auth header = %q", gotAuth)
	}
	if gotPath != "/chat/completions" {
		t.Errorf("path = %q, want /chat/completions", gotPath)
	}
	if gotReq.Model != "qwen-plus" || len(gotReq.Messages) != 2 {
		t.Errorf("request shape wrong: %+v", gotReq)
	}
	if !strings.Contains(gotReq.Messages[1].Content, "T2") {
		t.Errorf("user prompt should name the level, got %q", gotReq.Messages[1].Content)
	}
}

func TestQwenClient_Summarize_HTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		io.WriteString(w, `{"error":{"message":"invalid api key"}}`)
	}))
	defer srv.Close()

	q := &QwenClient{BaseURL: srv.URL, APIKey: "bad", Model: "qwen-plus", HTTP: srv.Client()}
	if _, err := q.Summarize(context.Background(), LevelT1, sampleInput().Messages); err == nil {
		t.Fatal("expected error on 401")
	}
}

func TestModelCompactor_WithQwen_FallsBackOnOutage(t *testing.T) {
	// Server always 500s => every level errors => ModelCompactor must yield a
	// complete deterministic archive, never empty, generator=deterministic.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	q := &QwenClient{BaseURL: srv.URL, APIKey: "sk", Model: "qwen-plus", HTTP: srv.Client()}
	lv, err := ModelCompactor{Client: q}.Compact(context.Background(), sampleInput())
	if err != nil {
		t.Fatalf("compact must not error on outage: %v", err)
	}
	for _, level := range AllLevels {
		if strings.TrimSpace(lv.Get(level)) == "" {
			t.Errorf("level %s empty after outage", level)
		}
	}
	if lv.Generator != "deterministic" {
		t.Errorf("generator = %q, want deterministic after full outage", lv.Generator)
	}
}

func TestDefaultCompactor_NoKeyIsDeterministic(t *testing.T) {
	t.Setenv("DASHSCOPE_API_KEY", "")
	if _, ok := DefaultCompactor().(DeterministicCompactor); !ok {
		t.Errorf("with no API key DefaultCompactor must be deterministic")
	}
}

func TestDefaultCompactor_WithKeyUsesModel(t *testing.T) {
	t.Setenv("DASHSCOPE_API_KEY", "sk-xyz")
	t.Setenv("QWEN_MODEL", "qwen-max")
	c := DefaultCompactor()
	mc, ok := c.(ModelCompactor)
	if !ok {
		t.Fatalf("with API key DefaultCompactor must be ModelCompactor, got %T", c)
	}
	if mc.Client.ID() != "qwen-max" {
		t.Errorf("model = %q, want qwen-max", mc.Client.ID())
	}
}
