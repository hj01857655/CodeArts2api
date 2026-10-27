// 签到编排：两条通道的汇报文本拼接 + done 判定。
//
// 用假上游（BaseOverride）复现真实响应形状——实测 2026-10-27：
//
//	opengw claim    → {"error_code":"4000","error_msg":"invalid channel, group not found"}
//	snap delivery   → {"code":0,"data":{"items":[...]}}
//	snap claim(已签) → {"code":40001,"message":"尚未到达权益刷新时间，暂时无法领取"}
package benefit

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"codearts2api/internal/cred"
)

// fakeUpstream 按路径返回预设响应，记录收到的请求。
type fakeUpstream struct {
	mu     sync.Mutex
	calls  []string
	routes map[string]func(w http.ResponseWriter)
}

func (f *fakeUpstream) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.calls = append(f.calls, r.Method+" "+r.URL.Path)
	f.mu.Unlock()
	key := r.Method + " " + r.URL.Path
	if fn, ok := f.routes[key]; ok {
		fn(w)
		return
	}
	http.NotFound(w, r)
}

func (f *fakeUpstream) saw(t *testing.T, want string) bool {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, c := range f.calls {
		if c == want {
			return true
		}
	}
	return false
}

// writeAuthFile 在 <stateDir>/auths/ 下写一份能被 loadAuthFiles 认的凭证。
// 三个字段缺一不可（AK/SK/cloud_dragon_token），否则该文件被跳过。
func writeAuthFile(t *testing.T, stateDir string) {
	t.Helper()
	dir := filepath.Join(stateDir, "auths")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := `{"access_key_id":"AKTEST","secret_access_key":"SKTEST",` +
		`"cloud_dragon_token":"TOKENTEST","expiration":"2099-01-01T00:00:00Z"}`
	if err := os.WriteFile(filepath.Join(dir, "codearts-test.json"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

func jsonRoute(body string) func(http.ResponseWriter) {
	return func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, body)
	}
}

func newBenefit(t *testing.T, routes map[string]func(http.ResponseWriter)) (*Benefit, *fakeUpstream) {
	t.Helper()
	f := &fakeUpstream{routes: routes}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)

	stateDir := t.TempDir()
	writeAuthFile(t, stateDir)
	b := &Benefit{
		Creds:        &cred.Provider{StateDir: stateDir},
		HTTP:         srv.Client(),
		Log:          slog.New(slog.NewTextHandler(io.Discard, nil)),
		BaseOverride: srv.URL,
	}
	return b, f
}

// deliveryItems 生成 snap delivery 响应体。
func deliveryItems(items ...map[string]any) string {
	b, _ := json.Marshal(map[string]any{
		"code": 0, "message": "ok",
		"data": map[string]any{"channel": "IDE", "items": items},
	})
	return string(b)
}

func campaign(id int, typ, title, status string, claimable bool, triggerMode string) map[string]any {
	return map[string]any{
		"campaignId": id, "type": typ, "title": title,
		"status": status, "claimable": claimable,
		"benefitAmount": 1000,
		"extra":         map[string]any{"triggerMode": triggerMode},
	}
}

// 两条通道都有汇报文本时，拼接处必须有分隔，不能粘成一个词。
// 实测响应：token 通道 4000 未开通 + 积分通道已结清。
func TestCheckinTextNotGlued(t *testing.T) {
	b, _ := newBenefit(t, map[string]func(http.ResponseWriter){
		"GET /api/v1/benefit/claim":  jsonRoute(`{"error_code":"4000","error_msg":"invalid channel, group not found"}`),
		"POST /api/v1/benefit/claim": jsonRoute(`{"error_code":"4000","error_msg":"invalid channel, group not found"}`),
		"GET /v1/ops/delivery": jsonRoute(deliveryItems(
			campaign(1, "USER_LOGIN", "每日签到领积分", "CONFIRMED", false, "MANUAL_CLAIM"),
		)),
	})

	text, done := b.CheckinOnce()
	if !done {
		t.Errorf("两通道都有结论，done 应为 true")
	}
	// 粘连的典型形态：「已跳过今日福利已确认到账」
	if strings.Contains(text, "已跳过今日") || strings.Contains(text, "跳过今日福利") {
		t.Fatalf("两条通道的汇报文本粘连了: %q", text)
	}
	if !strings.Contains(text, "福利 Token") {
		t.Errorf("token 通道的汇报丢了: %q", text)
	}
	if !strings.Contains(text, "已确认到账") {
		t.Errorf("积分通道的汇报丢了: %q", text)
	}
}

// 通道未开通是「有结论」，否则每天都会重试一次已经下线的通道。
func TestCheckinTokenChannelClosedIsDone(t *testing.T) {
	b, _ := newBenefit(t, map[string]func(http.ResponseWriter){
		"GET /api/v1/benefit/claim":  jsonRoute(`{"error_code":"4000","error_msg":"invalid channel, group not found"}`),
		"POST /api/v1/benefit/claim": jsonRoute(`{"error_code":"4000","error_msg":"invalid channel, group not found"}`),
		"GET /v1/ops/delivery":       jsonRoute(deliveryItems()),
	})
	_, done := b.CheckinOnce()
	if !done {
		t.Errorf("渠道下线应视为已有结论（否则每小时重试）")
	}
}

// 上游 5xx 不算结论——一次抽风不能把当天永久标记为已签。
func TestCheckinUpstreamErrorNotDone(t *testing.T) {
	b, _ := newBenefit(t, map[string]func(http.ResponseWriter){
		"GET /api/v1/benefit/claim":  func(w http.ResponseWriter) { w.WriteHeader(502) },
		"POST /api/v1/benefit/claim": func(w http.ResponseWriter) { w.WriteHeader(502) },
		"GET /v1/ops/delivery":       func(w http.ResponseWriter) { w.WriteHeader(502) },
	})
	_, done := b.CheckinOnce()
	if done {
		t.Errorf("上游全挂时不该标记为已签")
	}
}

// 今天已签（claim 回 40001）算结论，且不能重复领。
func TestCheckinAlreadyClaimedToday(t *testing.T) {
	b, f := newBenefit(t, map[string]func(http.ResponseWriter){
		"GET /api/v1/benefit/claim":       jsonRoute(`{"error_code":"0000","result":{"create_time":0}}`),
		"POST /api/v1/benefit/claim":      jsonRoute(`{"error_code":"0000","result":{"create_time":0}}`),
		"GET /api/v1/user/tokens/balance": jsonRoute(`{"error_code":"0000","result":{"total_balance":9941314,"total_quota":10000000}}`),
		"GET /v1/ops/delivery": jsonRoute(deliveryItems(
			campaign(1, "USER_LOGIN", "每日签到领积分", "CLAIMED", false, "MANUAL_CLAIM"),
		)),
		"POST /v1/ops/claim":   jsonRoute(`{"code":40001,"message":"尚未到达权益刷新时间，暂时无法领取","data":null}`),
		"POST /v1/ops/confirm": jsonRoute(`{"code":0,"message":"ok","data":{"id":1,"campaignId":1,"status":"CONFIRMED"}}`),
	})
	text, done := b.CheckinOnce()
	if !done {
		t.Errorf("claim 回 40001 是确定结论，done 应为 true")
	}
	if !f.saw(t, "POST /v1/ops/confirm") {
		t.Errorf("status=CLAIMED 应补发 confirm（幂等补救）")
	}
	_ = text
}

// AUTO_GRANT 的活动由服务端自动发，不该去 claim。
func TestCheckinSkipsAutoGrant(t *testing.T) {
	b, f := newBenefit(t, map[string]func(http.ResponseWriter){
		"GET /api/v1/benefit/claim":  jsonRoute(`{"error_code":"4000","error_msg":"invalid channel, group not found"}`),
		"POST /api/v1/benefit/claim": jsonRoute(`{"error_code":"4000","error_msg":"invalid channel, group not found"}`),
		"GET /v1/ops/delivery": jsonRoute(deliveryItems(
			campaign(4, "NEW_USER_REGISTER", "新用户注册送 4000 积分", "CONSUMED", true, "AUTO_GRANT"),
		)),
	})
	b.CheckinOnce()
	if f.saw(t, "POST /v1/ops/claim") {
		t.Errorf("AUTO_GRANT 不该 claim")
	}
}
