// adminCheckin：手动签到不能因为 Token 通道下线就整段失败。
//
// 实测 2026-10-27 本账号：POST /api/v1/benefit/claim 稳定回
// {"error_code":"4000","error_msg":"invalid channel, group not found"}。
// 旧实现直接调 Claim()，一遇 4000 就 500 返回，积分通道完全不会被尝试——
// 而积分通道恰恰是唯一还有收益的那条。
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
	"testing"

	"codearts2api/internal/benefit"
	"codearts2api/internal/config"
	"codearts2api/internal/cred"
)

// newCheckinServer 造一个只服务签到接口的最小 Server，Ben 指向假上游。
func newCheckinServer(t *testing.T, routes map[string]string) *Server {
	t.Helper()
	dir := t.TempDir()

	authDir := filepath.Join(dir, "auths")
	if err := os.MkdirAll(authDir, 0o755); err != nil {
		t.Fatal(err)
	}
	body := `{"access_key_id":"AKTEST","secret_access_key":"SKTEST",` +
		`"cloud_dragon_token":"TOKENTEST","expiration":"2099-01-01T00:00:00Z"}`
	if err := os.WriteFile(filepath.Join(authDir, "codearts-test.json"), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}

	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if resp, ok := routes[r.Method+" "+r.URL.Path]; ok {
			fmt.Fprint(w, resp)
			return
		}
		http.NotFound(w, r)
	}))
	t.Cleanup(up.Close)

	ben := &benefit.Benefit{
		Creds:        &cred.Provider{StateDir: dir},
		HTTP:         up.Client(),
		Log:          slog.New(slog.NewTextHandler(io.Discard, nil)),
		BaseOverride: up.URL,
	}
	return &Server{
		Cfg:    &config.Config{StateDir: dir},
		Ben:    ben,
		Dumper: nil,
		Log:    slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

// Token 通道回 4000（渠道下线）时，签到仍须成功，且必须去领积分。
func TestAdminCheckinTokenChannelClosedStillClaimsCredit(t *testing.T) {
	srv := newCheckinServer(t, map[string]string{
		// Token 通道：状态查不到、领取被拒（渠道下线）
		"GET /api/v1/benefit/claim":       `{"error_code":"4000","error_msg":"invalid channel, group not found"}`,
		"POST /api/v1/benefit/claim":      `{"error_code":"4000","error_msg":"invalid channel, group not found"}`,
		"GET /api/v1/user/tokens/balance": `{"error_code":"4000","error_msg":"invalid channel, group not found"}`,
		// 积分通道：今日已签，回读为 CONFIRMED
		"GET /v1/ops/delivery": `{"code":0,"message":"ok","data":{"channel":"IDE","items":[
			{"campaignId":1,"type":"USER_LOGIN","title":"每日签到领积分",
			 "status":"CONFIRMED","claimable":false,"benefitAmount":1000,
			 "extra":{"triggerMode":"MANUAL_CLAIM"}}]}}`,
		"GET /v1/ops/credit/balance": `{"code":0,"data":{"total":5000,"remain":4200,"used":800}}`,
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/admin/api/checkin", nil)
	srv.Handler().ServeHTTP(rec, req)

	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应非 JSON: %v\n%s", err, rec.Body.String())
	}
	if ok, _ := out["ok"].(bool); !ok {
		t.Fatalf("Token 通道下线不该让签到整体失败: HTTP %d %s", rec.Code, rec.Body.String())
	}
	msg, _ := out["message"].(string)
	if msg == "" {
		t.Errorf("message 为空，用户看不到任何结论")
	}
}
