package memorycompact

import (
	"regexp"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

// QwenClient is a ModelClient backed by Alibaba DashScope's OpenAI-compatible
// chat-completions endpoint (the same path the fleet's line_bridge.py
// 记忆总管 already uses for T1..T4). It is wired into ModelCompactor; on any
// network/credential failure ModelCompactor falls back to the deterministic
// compactor per level, so a Qwen outage never blocks 止血.
type QwenClient struct {
	BaseURL   string
	APIKey    string
	Model     string
	MaxTokens int
	HTTP      *http.Client
}

// DashScope / DeepSeek OpenAI-compatible defaults, matching line_bridge.py.
const (
	defaultDashScopeBaseURL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
	defaultQwenModel        = "qwen-plus"
	defaultQwenMaxTokens    = 1500

	defaultDeepSeekBaseURL = "https://api.deepseek.com/v1"
	defaultDeepSeekModel   = "deepseek-chat"
)

// NewQwenClientFromEnv builds a QwenClient from DASHSCOPE_API_KEY /
// DASHSCOPE_BASE_URL / QWEN_MODEL. It returns nil when no API key is
// configured, which is the intended "not wired yet" state: callers pass the
// nil into ModelCompactor.Client and transparently get the deterministic
// fallback. This is how the Qwen bridge stays optional / human-gated.
func NewQwenClientFromEnv() *QwenClient {
	key := strings.TrimSpace(os.Getenv("DASHSCOPE_API_KEY"))
	if key == "" {
		return nil
	}
	base := strings.TrimSpace(os.Getenv("DASHSCOPE_BASE_URL"))
	if base == "" {
		base = defaultDashScopeBaseURL
	}
	model := strings.TrimSpace(os.Getenv("QWEN_MODEL"))
	if model == "" {
		model = defaultQwenModel
	}
	return &QwenClient{
		BaseURL:   base,
		APIKey:    key,
		Model:     model,
		MaxTokens: defaultQwenMaxTokens,
		HTTP:      &http.Client{Timeout: 90 * time.Second},
	}
}

// NewDeepSeekClientFromEnv builds an OpenAI-compatible client pointed at
// DeepSeek (DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL), reusing
// the same request/parse path as Qwen. Returns nil when no key is set, so it
// composes as a fallback. The QwenClient type is provider-agnostic — both
// DashScope and DeepSeek speak the OpenAI chat-completions dialect.
func NewDeepSeekClientFromEnv() *QwenClient {
	key := strings.TrimSpace(os.Getenv("DEEPSEEK_API_KEY"))
	if key == "" {
		return nil
	}
	base := strings.TrimSpace(os.Getenv("DEEPSEEK_BASE_URL"))
	if base == "" {
		base = defaultDeepSeekBaseURL
	}
	model := strings.TrimSpace(os.Getenv("DEEPSEEK_MODEL"))
	if model == "" {
		model = defaultDeepSeekModel
	}
	return &QwenClient{
		BaseURL:   base,
		APIKey:    key,
		Model:     model,
		MaxTokens: defaultQwenMaxTokens,
		HTTP:      &http.Client{Timeout: 90 * time.Second},
	}
}

// ID reports the model id recorded in memory_archive.generator.
func (q *QwenClient) ID() string { return q.Model }

// levelInstruction mirrors line_bridge.py's TIERS so Go-side and Python-side
// archives stay semantically aligned.
var levelInstruction = map[Level]string{
	LevelT1: "T1 简要说明书:一段话讲清这个会话/任务是什么、目标、最终结论。最多4行。",
	LevelT2: "T2 段落简要记忆:关键决策、踩的坑、结果,分几段。",
	LevelT3: "T3 完全短记忆:完整但精炼的过程记录(做了什么→怎么做→验证→结论)。",
	LevelT4: "T4 完全长记忆:全量细节、关键命令/日志/证据、完整溯源。",
}

const qwenSystemPrompt = "你是记忆总管,产出精炼准确的分梯度记忆。中文。"

type qwenChatRequest struct {
	Model     string        `json:"model"`
	Messages  []qwenMessage `json:"messages"`
	MaxTokens int           `json:"max_tokens,omitempty"`
}

type qwenMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type qwenChatResponse struct {
	Choices []struct {
		Message qwenMessage `json:"message"`
	} `json:"choices"`
	Error *struct {
		Message string `json:"message"`
	} `json:"error"`
}

// Summarize implements ModelClient by asking Qwen for one gradient.
func (q *QwenClient) Summarize(ctx context.Context, level Level, msgs []Message) (string, error) {
	instr, ok := levelInstruction[level]
	if !ok {
		return "", fmt.Errorf("memorycompact: unknown level %q", level)
	}
	src := redactSecrets(truncate(renderDigest(normalize(msgs), defaultT4Cap), 6000))  // A: 外送第三方LLM前脱敏密钥/令牌
	user := fmt.Sprintf(
		"你是记忆总管。基于以下对话记录,只产出【%s】这一梯度的内容,不要别的梯度、不要解释。\n要求:%s\n\n对话记录:\n%s",
		level, instr, src,
	)

	reqBody, err := json.Marshal(qwenChatRequest{
		Model:     q.Model,
		MaxTokens: q.MaxTokens,
		Messages: []qwenMessage{
			{Role: "system", Content: qwenSystemPrompt},
			{Role: "user", Content: user},
		},
	})
	if err != nil {
		return "", fmt.Errorf("memorycompact: marshal request: %w", err)
	}

	url := strings.TrimRight(q.BaseURL, "/") + "/chat/completions"
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(reqBody))
	if err != nil {
		return "", fmt.Errorf("memorycompact: build request: %w", err)
	}
	httpReq.Header.Set("Authorization", "Bearer "+q.APIKey)
	httpReq.Header.Set("Content-Type", "application/json")

	client := q.HTTP
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(httpReq)
	if err != nil {
		return "", fmt.Errorf("memorycompact: qwen call: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return "", fmt.Errorf("memorycompact: read response: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("memorycompact: qwen status %d: %s", resp.StatusCode, truncate(string(body), 300))
	}

	var parsed qwenChatResponse
	if err := json.Unmarshal(body, &parsed); err != nil {
		return "", fmt.Errorf("memorycompact: decode response: %w", err)
	}
	if parsed.Error != nil && parsed.Error.Message != "" {
		return "", fmt.Errorf("memorycompact: qwen error: %s", parsed.Error.Message)
	}
	if len(parsed.Choices) == 0 {
		return "", fmt.Errorf("memorycompact: qwen returned no choices")
	}
	return strings.TrimSpace(parsed.Choices[0].Message.Content), nil
}

// DefaultCompactor returns the production compactor. It prefers Qwen (the
// 记忆总管 role) when DASHSCOPE_API_KEY is set, then DeepSeek when
// DEEPSEEK_API_KEY is set, and finally falls back to the deterministic
// fail-safe. Whichever model is chosen, ModelCompactor still degrades to the
// deterministic levels per-gradient on any call failure — a model outage can
// never drop the original transcript or break the calling flow. Wiring a
// model is purely an env-config change.
func DefaultCompactor() Compactor {
	if client := NewQwenClientFromEnv(); client != nil {
		return ModelCompactor{Client: client}
	}
	if client := NewDeepSeekClientFromEnv(); client != nil {
		return ModelCompactor{Client: client}
	}
	return DeterministicCompactor{}
}

// A: 发给外部 LLM 前脱敏常见密钥/令牌/私钥,防数据出境泄露(总管 2026-06-12)。
var secretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)sk-[A-Za-z0-9_-]{16,}`),
	regexp.MustCompile(`gh[pousr]_[A-Za-z0-9]{20,}`),
	regexp.MustCompile(`AKIA[0-9A-Z]{16}`),
	regexp.MustCompile(`(?i)bearer\s+[A-Za-z0-9._-]{20,}`),
	regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----`),
	regexp.MustCompile(`(?i)(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*\S{8,}`),
	regexp.MustCompile(`eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`),
}

func redactSecrets(s string) string {
	for _, re := range secretPatterns {
		s = re.ReplaceAllString(s, "[REDACTED]")
	}
	return s
}
