// golden_test.go httptest 假上游测试：鉴权、chat 三态（agent 透传 / benefit
// 补尾 / 流内错误帧）、模型门禁与钳制、402/限流重试/凭证重试/409 闸门、
// responses 与 anthropic 桥、models。假上游经 codearts.Client.BaseOverride 注入。
package handler

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"codearts2api/internal/codearts"
	"codearts2api/internal/config"
	"codearts2api/internal/cred"
	"codearts2api/internal/dump"
)

// ── 假上游 ──────────────────────────────────────────────────────────────────

// chatSSEUp 常量上游 SSE：content + finish + usage + [DONE]。
var chatSSEUp = strings.Join([]string{
	`data: {"id":"c1","object":"chat.completion.chunk","model":"GLM-5.2","choices":[{"index":0,"delta":{"role":"assistant","content":"你"}}]}`,
	``,
	`data: {"choices":[{"index":0,"delta":{"content":"好"}}]}`,
	``,
	`data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
	``,
	`data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}`,
	``,
	`data: [DONE]`,
	``,
}, "\n")

// captured 一次上游聊天请求的关键形状（断言用）。
type captured struct {
	method string
	path   string
	auth   string
	body   map[string]any
	count  int
}

// newEnv 建完整测试环境：auths 假凭据（短值，<24 字符）+ models-cache.json
// （新鲜 mtime 让 RefreshIfNeeded 直接短路，不打模型接口）+ httptest 假上游。
func newEnv(t *testing.T, upFn func(w http.ResponseWriter, r *http.Request, capr *captured)) (*env, *captured) {
	t.Helper()
	dir := t.TempDir()
	capr := &captured{}

	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 非 chat 请求（心跳/模型同步/福利网关）统一在这层兜
		switch {
		case strings.HasPrefix(r.URL.Path, "/snap-manager/"):
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"status":"ok"}`)
			return
		case strings.HasPrefix(r.URL.Path, "/v1/agent-center/agents/useragents"):
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"agents":[]}`)
			return
		case strings.HasPrefix(r.URL.Path, "/v1/agent-center/agents/detail"):
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"gpts":{"models":[]}}`)
			return
		case strings.HasPrefix(r.URL.Path, "/v1/benefit-gateway-config"):
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprint(w, `{"enabled":false}`)
			return
		}
		// 聊天主路径
		capr.count++
		capr.method = r.Method
		capr.path = r.URL.Path
		capr.auth = r.Header.Get("Authorization")
		body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		capr.body = nil
		_ = json.Unmarshal(body, &capr.body)
		upFn(w, r, capr)
	}))
	t.Cleanup(up.Close)

	// 凭证：auths/codearts-u-123.json（短值凭据，expires 远未来不触发刷新）
	if err := os.MkdirAll(filepath.Join(dir, "auths"), 0o700); err != nil {
		t.Fatal(err)
	}
	future := time.Now().Add(24 * time.Hour).UTC().Format("2006-01-02T15:04:05Z")
	authJSON := fmt.Sprintf(`{"access_key_id":"ak-local-1","secret_access_key":"sk-local-1","cloud_dragon_token":"tok-local-1","expiration":%q,"refresh_token":"rt-local","code_verifier":"cv-local","client_id":"codearts-agent"}`, future)
	if err := os.WriteFile(filepath.Join(dir, "auths", "codearts-u-123.json"), []byte(authJSON), 0o600); err != nil {
		t.Fatal(err)
	}

	// 模型缓存：GLM-5.2 = agent 通道，glm-5.3-flash = benefit 通道。
	// 新鲜 mtime 让 RefreshIfNeeded 短路，测试期间不产生模型同步流量。
	cache := fmt.Sprintf(`{"updated_at":%d,"models":[
		{"id":"GLM-5.2","object":"model","channel":"agent","name":"GLM 5.2"},
		{"id":"glm-5.3-flash","object":"model","channel":"benefit","name":"GLM 5.3 Flash"}]}`,
		time.Now().Unix())
	if err := os.WriteFile(filepath.Join(dir, "models-cache.json"), []byte(cache), 0o600); err != nil {
		t.Fatal(err)
	}

	creds := &cred.Provider{StateDir: dir}
	cfg := &config.Config{
		ListenHost: "127.0.0.1", Port: 0, StateDir: dir,
		BenefitAutoClaim: false, SessionGateOn: false, MaxTokensCap: 65536,
		ConnRetryMax: 0, ConnRetryBackoff: 0.001,
		UpstreamRetryMax: 3, UpstreamRetryBackoff: 0.001,
		DumpOn: false,
	}
	client := codearts.NewClient(cfg, creds, nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	client.BaseOverride = up.URL

	srv := &Server{Cfg: cfg, C: client, Ben: nil,
		Dumper: &dump.Writer{On: false}, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	return &env{t: t, srv: srv, cfg: cfg, dir: dir}, capr
}

type env struct {
	t   *testing.T
	srv *Server
	cfg *config.Config
	dir string
}

// postJSON 直连 handler 发 POST。
func (e *env) postJSON(path, body string, header http.Header) *httptest.ResponseRecorder {
	e.t.Helper()
	req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	for k, vs := range header {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	rec := httptest.NewRecorder()
	e.srv.Handler().ServeHTTP(rec, req)
	return rec
}

func (e *env) get(path string, header http.Header) *httptest.ResponseRecorder {
	e.t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	for k, vs := range header {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	rec := httptest.NewRecorder()
	e.srv.Handler().ServeHTTP(rec, req)
	return rec
}

// sseFrames 把流式响应体拆成 data: JSON 帧列表（[DONE] 以外）。
func sseFrames(t *testing.T, body string) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(body, "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		payload := strings.TrimPrefix(line, "data: ")
		if payload == "[DONE]" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(payload), &m); err != nil {
			t.Fatalf("坏 SSE 帧 %q: %v", payload, err)
		}
		out = append(out, m)
	}
	return out
}

func mustJSON(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("非 JSON: %s", raw)
	}
	return m
}

// ── 测试 ────────────────────────────────────────────────────────────────────

func TestAPIKeyGuard(t *testing.T) {
	env, capr := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, chatSSEUp)
	})
	env.cfg.APIKey = "local-key"
	rec := env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("无 Bearer = %d，要 401", rec.Code)
	}
	// 对 Bearer 放行
	rec = env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","stream":false,"messages":[{"role":"user","content":"hi"}]}`,
		http.Header{"Authorization": []string{"Bearer local-key"}})
	if rec.Code != 200 {
		t.Fatalf("对 Bearer = %d: %s", rec.Code, rec.Body.String())
	}
	if capr.count != 1 {
		t.Fatalf("上游被打 %d 次，要 1（401 请求不计）", capr.count)
	}
}

func TestChatBodyValidation(t *testing.T) {
	env, capr := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		t.Error("校验失败不应打上游")
	})
	if rec := env.postJSON("/v1/chat/completions", ``, nil); rec.Code != 400 {
		t.Fatalf("空 body = %d，要 400", rec.Code)
	}
	if rec := env.postJSON("/v1/chat/completions", `{bad`, nil); rec.Code != 400 {
		t.Fatalf("坏 JSON = %d，要 400", rec.Code)
	}
	if capr.count != 0 {
		t.Fatalf("上游被打了 %d 次", capr.count)
	}
}

func TestChatNonStream(t *testing.T) {
	// 上游即使 stream=false 也回 SSE（实际行为）：代理必须聚合成单个 JSON
	env, capr := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, chatSSEUp)
	})
	rec := env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","stream":false,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 200 {
		t.Fatalf("= %d: %s", rec.Code, rec.Body.String())
	}
	body := mustJSON(t, rec.Body.Bytes())
	if body["object"] != "chat.completion" {
		t.Fatalf("object = %v", body["object"])
	}
	ch := body["choices"].([]any)[0].(map[string]any)
	msg := ch["message"].(map[string]any)
	if msg["content"] != "你好" || ch["finish_reason"] != "stop" {
		t.Fatalf("content/finish = %v / %v", msg["content"], ch["finish_reason"])
	}
	if usage := body["usage"].(map[string]any); usage["prompt_tokens"] != float64(5) {
		t.Fatalf("usage = %v", usage)
	}
	// 非流式请求也会被强制改成 stream=true 发上游（正文丢失规避）
	if capr.body["stream"] != true {
		t.Fatalf("上游 stream = %v，要 true", capr.body["stream"])
	}
	// 上游 Authorization 是 SDK-HMAC-SHA256 签名（Bearer 客户端 key 不透传）
	if !strings.HasPrefix(capr.auth, "SDK-HMAC-SHA256 ") {
		t.Fatalf("上游 Authorization = %q，要 SDK-HMAC-SHA256 签名", capr.auth)
	}
	if capr.path != "/api/v2/chat/completions" {
		t.Fatalf("上游 path = %s", capr.path)
	}
}

func TestChatStreamBenefitFinish(t *testing.T) {
	// benefit 流：上游既无 finish_reason 也无 [DONE]，content 还是 null——
	// 代理补 content:""、proxy-finish 帧、[DONE]
	up := strings.Join([]string{
		`data: {"choices":[{"index":0,"delta":{"content":null,"reasoning_content":"想"}}]}`,
		``,
		`data: {"choices":[{"index":0,"delta":{"content":"答"}}]}`,
		``,
	}, "\n")
	env, _ := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, up)
	})
	rec := env.postJSON("/v1/chat/completions", `{"model":"glm-5.3-flash","stream":true,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 200 {
		t.Fatalf("= %d: %s", rec.Code, rec.Body.String())
	}
	raw := rec.Body.String()
	if !strings.Contains(raw, `"content":""`) {
		t.Fatalf("content:null 应改写为 \"\": %s", raw)
	}
	if !strings.Contains(raw, `"id":"proxy-finish"`) {
		t.Fatalf("benefit 流缺 proxy-finish 补块: %s", raw)
	}
	if strings.Count(raw, "data: [DONE]") != 1 {
		t.Fatalf("[DONE] 应恰好一次: %s", raw)
	}
	frames := sseFrames(t, raw)
	last := frames[len(frames)-1]
	if last["id"] != "proxy-finish" {
		t.Fatalf("补块应在最后: %v", last)
	}
}

func TestChatStreamAgentPassthrough(t *testing.T) {
	// agent 流：上游已有 finish 与 [DONE]，透传不补 proxy-finish
	env, _ := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, chatSSEUp)
	})
	rec := env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","stream":true,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 200 {
		t.Fatalf("= %d: %s", rec.Code, rec.Body.String())
	}
	raw := rec.Body.String()
	if strings.Contains(raw, "proxy-finish") {
		t.Fatalf("agent 流不应补 proxy-finish: %s", raw)
	}
	if strings.Count(raw, "data: [DONE]") != 1 {
		t.Fatalf("[DONE] 应恰好一次（透传）: %s", raw)
	}
	if !strings.Contains(raw, `"content":"你"`) {
		t.Fatalf("content 帧丢失: %s", raw)
	}
}

func TestChatStreamErrorFrame(t *testing.T) {
	// 流内上游错误帧：转 OpenAI error 帧 + [DONE] 终止，code 透传
	up := strings.Join([]string{
		`data: {"choices":[{"index":0,"delta":{"content":"前半"}}]}`,
		``,
		`data: {"error_code":"InferHub.001001005.400","error_msg":"param invalid"}`,
		``,
	}, "\n")
	env, _ := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, up)
	})
	rec := env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","stream":true,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 200 {
		t.Fatalf("= %d（流已 200，错误只能以帧收尾）", rec.Code)
	}
	raw := rec.Body.String()
	if !strings.Contains(raw, `"type":"upstream_error"`) ||
		!strings.Contains(raw, "InferHub.001001005.400") {
		t.Fatalf("错误帧形状不对: %s", raw)
	}
	if !strings.Contains(raw, "data: [DONE]") {
		t.Fatalf("错误帧后要接 [DONE]")
	}
}

func TestChatModelNotFound(t *testing.T) {
	env, _ := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		t.Error("模型 404 不应打聊天上游")
	})
	rec := env.postJSON("/v1/chat/completions", `{"model":"nope-model","messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 404 {
		t.Fatalf("= %d，要 404: %s", rec.Code, rec.Body.String())
	}
	errObj := mustJSON(t, rec.Body.Bytes())["error"].(map[string]any)
	if errObj["code"] != "model_not_found" {
		t.Fatalf("code = %v", errObj["code"])
	}
	if !strings.Contains(rec.Body.String(), "nope-model") {
		t.Fatalf("错误消息缺模型名: %s", rec.Body.String())
	}
}

func TestClampMaxTokens(t *testing.T) {
	env, capr := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, chatSSEUp)
	})
	rec := env.postJSON("/v1/chat/completions",
		`{"model":"GLM-5.2","stream":false,"max_tokens":999999,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 200 {
		t.Fatalf("= %d: %s", rec.Code, rec.Body.String())
	}
	// 66560 起整请求被上游拒，转发前必须钳到 65536
	if capr.body["max_tokens"] != float64(65536) {
		t.Fatalf("上游 max_tokens = %v，要 65536", capr.body["max_tokens"])
	}
}

func Test402CreditExhausted(t *testing.T) {
	env, capr := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprint(w, `{"error_code":"TM.00001050","error_msg":"积分不足"}`)
	})
	rec := env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","stream":false,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 402 {
		t.Fatalf("= %d，要 402", rec.Code)
	}
	errObj := mustJSON(t, rec.Body.Bytes())["error"].(map[string]any)
	if errObj["type"] != "insufficient_quota" || errObj["message"] != "积分不足" {
		t.Fatalf("error = %v", errObj)
	}
	if capr.count != 1 {
		t.Fatalf("积分耗尽不应重试，上游被打 %d 次", capr.count)
	}
}

func TestRetryOnRateLimit(t *testing.T) {
	env, capr := newEnv(t, func(w http.ResponseWriter, r *http.Request, c *captured) {
		if c.count == 1 {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadRequest)
			fmt.Fprint(w, `{"error_code":"TM.00001041","error_msg":"并发超限"}`)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, chatSSEUp)
	})
	rec := env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","stream":false,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 200 {
		t.Fatalf("限流后应重试成功，得 %d: %s", rec.Code, rec.Body.String())
	}
	if capr.count != 2 {
		t.Fatalf("上游被打 %d 次，要 2", capr.count)
	}
}

func TestAuthRetry(t *testing.T) {
	// 401（凭证失效）：强制重读凭证后重试一次
	env, capr := newEnv(t, func(w http.ResponseWriter, r *http.Request, c *captured) {
		if c.count == 1 {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusUnauthorized)
			fmt.Fprint(w, `{"error_code":"APIG.0602","error_msg":"token expired"}`)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, chatSSEUp)
	})
	rec := env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","stream":false,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 200 {
		t.Fatalf("凭证重读后应重试成功，得 %d: %s", rec.Code, rec.Body.String())
	}
	if capr.count != 2 {
		t.Fatalf("上游被打 %d 次，要 2", capr.count)
	}
}

func Test409Gate(t *testing.T) {
	env, _ := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, chatSSEUp)
	})
	env.cfg.SessionGateOn = true
	env.srv.C.Gate.Limit = 1

	var release = make(chan struct{})
	// 拦在假上游 chat 路径前：第一路挂起直到第二路收到 409
	hangCh := make(chan struct{})
	env.srv.C.HTTP.Transport = &gateTransport{base: http.DefaultTransport,
		once: &sync.Once{}, hangCh: hangCh, release: release}

	go func() {
		rec := env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","stream":false,"messages":[{"role":"user","content":"hi"}]}`, nil)
		if rec.Code != 200 {
			t.Errorf("第一路 = %d，要 200: %s", rec.Code, rec.Body.String())
		}
	}()
	// 等第一路真的到达上游（闸门已占用）
	<-hangCh
	// 第二路：闸门满员 → 409（不是 429）
	rec := env.postJSON("/v1/chat/completions", `{"model":"GLM-5.2","stream":false,"messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 409 {
		t.Fatalf("满员 = %d，要 409: %s", rec.Code, rec.Body.String())
	}
	errObj := mustJSON(t, rec.Body.Bytes())["error"].(map[string]any)
	if errObj["code"] != "local_concurrency_limit" {
		t.Fatalf("code = %v", errObj["code"])
	}
	close(release)
	time.Sleep(200 * time.Millisecond)
}

// gateTransport 拦截第一个到达上游的请求：通知测试主线程后挂起直到 release。
type gateTransport struct {
	base    http.RoundTripper
	once    *sync.Once
	hangCh  chan struct{}
	release chan struct{}
}

func (g *gateTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.Method == http.MethodPost {
		g.once.Do(func() {
			close(g.hangCh)
			<-g.release
		})
	}
	return g.base.RoundTrip(req)
}

func TestModels(t *testing.T) {
	env, _ := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		t.Errorf("models 缓存新鲜，不应打上游: %s", r.URL.Path)
	})
	rec := env.get("/v1/models", nil)
	if rec.Code != 200 {
		t.Fatalf("= %d: %s", rec.Code, rec.Body.String())
	}
	body := mustJSON(t, rec.Body.Bytes())
	data := body["data"].([]any)
	if len(data) != 2 {
		t.Fatalf("models = %d 条，要 2: %s", len(data), rec.Body.String())
	}
	// 单模型详情
	rec = env.get("/v1/models/GLM-5.2", nil)
	if rec.Code != 200 || mustJSON(t, rec.Body.Bytes())["id"] != "GLM-5.2" {
		t.Fatalf("单模型 = %d: %s", rec.Code, rec.Body.String())
	}
	// 未知模型 404
	rec = env.get("/v1/models/nope", nil)
	if rec.Code != 404 {
		t.Fatalf("未知模型 = %d，要 404", rec.Code)
	}
}

func TestResponsesNonStream(t *testing.T) {
	env, capr := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, chatSSEUp)
	})
	rec := env.postJSON("/v1/responses", `{"model":"GLM-5.2","stream":false,"input":"hi"}`, nil)
	if rec.Code != 200 {
		t.Fatalf("= %d: %s", rec.Code, rec.Body.String())
	}
	// input 字符串翻译成 chat messages
	if msgs, ok := capr.body["messages"].([]any); !ok || len(msgs) != 1 {
		t.Fatalf("input 未翻译成 messages: %v", capr.body["messages"])
	}
	body := mustJSON(t, rec.Body.Bytes())
	if body["object"] != "response" {
		t.Fatalf("object = %v，要 response", body["object"])
	}
	out, _ := body["output"].([]any)
	if len(out) == 0 {
		t.Fatalf("output 为空: %s", rec.Body.String())
	}
}

func TestAnthropicNonStream(t *testing.T) {
	env, capr := newEnv(t, func(w http.ResponseWriter, r *http.Request, _ *captured) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, chatSSEUp)
	})
	rec := env.postJSON("/v1/messages",
		`{"model":"GLM-5.2","max_tokens":64,"system":"be nice","messages":[{"role":"user","content":"hi"}]}`, nil)
	if rec.Code != 200 {
		t.Fatalf("= %d: %s", rec.Code, rec.Body.String())
	}
	// system 翻译成首条 system 消息
	msgs := capr.body["messages"].([]any)
	if first := msgs[0].(map[string]any); first["role"] != "system" {
		t.Fatalf("system 未翻译: %v", msgs)
	}
	body := mustJSON(t, rec.Body.Bytes())
	if body["type"] != "message" || body["role"] != "assistant" {
		t.Fatalf("Anthropic 形状不对: %v", body)
	}
	blocks, _ := body["content"].([]any)
	if len(blocks) == 0 {
		t.Fatalf("content 为空: %s", rec.Body.String())
	}
	if block := blocks[0].(map[string]any); block["type"] != "text" || block["text"] != "你好" {
		t.Fatalf("首个 block = %v，要 text/你好", block)
	}
	if _, ok := body["usage"].(map[string]any); !ok {
		t.Fatalf("缺 usage: %s", rec.Body.String())
	}
}
