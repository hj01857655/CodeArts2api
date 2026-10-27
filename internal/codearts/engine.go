// Package codearts 对应 Python server.py 的引擎层：SDK-HMAC 签名发送、
// 三类重试（连接级/限流/凭证）、并发闸门接入、模型双通道同步、SSE 聚合。
// 协议形状在 handler 包，方言翻译在 adapters 包。
package codearts

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"codearts2api/internal/benefit"
	"codearts2api/internal/config"
	"codearts2api/internal/cred"
	"codearts2api/internal/session"
	"codearts2api/internal/signer"
)

// UpstreamError _send_upstream 最终失败时的 (status, body) 形状，
// handler 直接把它写给客户端（CORS 由 handler 统一加）。
type UpstreamError struct {
	Status int
	Body   map[string]any
}

// Client 引擎核心。
type Client struct {
	Cfg     *config.Config
	Creds   *cred.Provider
	Benefit *benefit.Benefit
	Log     *slog.Logger
	Gate    *session.Gate
	// HeartbeatOn 关掉后仍发 user-session-id，只是不 busy/idle
	HeartbeatOn bool
	// BaseOverride 测试注入口：非空时官方 base（snap-access / opengw 两个域）
	// 都换成假上游地址，生产路径绝不设置。
	BaseOverride string

	// HTTP 只管连接级超时（dial 10s / TLS 10s / 响应头 600s，对应 Python
	// timeout=(10,600) 的连接段）；总时长不设——流式可以跑很久。
	HTTP *http.Client

	mu                 sync.Mutex
	supported          []string
	details            map[string]map[string]any
	agentModels        map[string]bool
	benefitChannelDown bool

	refreshInProgress atomic.Bool
	lastForcedSync    atomic.Int64 // unix 秒；强制同步节流 20s
}

// NewClient 装配引擎。Gate 由调用方建（限流数可配）。
func NewClient(cfg *config.Config, creds *cred.Provider, ben *benefit.Benefit, log *slog.Logger) *Client {
	transport := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 10 * time.Second}).DialContext,
		TLSHandshakeTimeout:   10 * time.Second,
		ResponseHeaderTimeout: 600 * time.Second,
		MaxIdleConns:          16,
		MaxIdleConnsPerHost:   16,
	}
	c := &Client{
		Cfg:         cfg,
		Creds:       creds,
		Benefit:     ben,
		Log:         log,
		Gate:        session.NewGate(session.SessionLimit()),
		HeartbeatOn: sessionHeartbeatOn(),
		HTTP:        &http.Client{Transport: transport},
		supported:   append([]string{}, config.FallbackAgentModels...),
		agentModels: map[string]bool{},
		details:     map[string]map[string]any{},
	}
	for _, m := range config.BenefitModels {
		c.supported = append(c.supported, m)
	}
	// 兜底通道归属：FallbackAgentModels 是 agent，BenefitModels 是 benefit
	for _, m := range config.FallbackAgentModels {
		c.agentModels[m] = true
	}
	return c
}

func sessionHeartbeatOn() bool {
	switch os.Getenv("CODEARTS_SESSION_HEARTBEAT") {
	case "0", "false", "False", "no", "":
		return false
	}
	return true
}

// ── 凭证 ─────────────────────────────────────────────────────────────────────

// currentCreds 便捷取凭证（nil 时给空凭证，上游会拒绝并给出明确错误）。
func (c *Client) currentCreds() *cred.Cred {
	if cc := c.Creds.Get(false); cc != nil {
		return cc
	}
	return cred.Empty()
}

// AccountID 并发闸门身份（跨 AK 轮换稳定；空串时所有请求挤同一个闸门口）。
func (c *Client) AccountID() string {
	return strings.TrimSpace(c.currentCreds().AccountID)
}

// ── 通用签名请求（对应 _signed_request）──────────────────────────────────────

// isConnRetryable 连接级失败可以重发；读超时不行——请求已进入推理阶段，
// 重发会把同一次生成再算一遍。可重发：TLS 被中断、连接被重置、连不上。
func isConnRetryable(err error) bool {
	var opErr *net.OpError
	if errors.As(err, &opErr) {
		switch opErr.Op {
		case "dial":
			return true
		case "read", "write":
			msg := opErr.Err.Error()
			return strings.Contains(msg, "reset") || strings.Contains(msg, "broken pipe") ||
				strings.Contains(msg, "EOF")
		}
		return false
	}
	msg := err.Error()
	if strings.Contains(msg, "timeout awaiting response headers") {
		return false // ResponseHeaderTimeout：读超时
	}
	return strings.Contains(msg, "EOF") || strings.Contains(msg, "connection reset") ||
		strings.Contains(msg, "broken pipe")
}

// SignedRequest 带签名的通用请求，连接级失败时换新连接重发。
// x-sdk-date 参与签名，每次重发都重新签。
func (c *Client) SignedRequest(method, rawURL string, body []byte, extra map[string]string, timeout time.Duration) (*http.Response, error) {
	method = strings.ToUpper(method)
	rawURL = c.upstreamURL(rawURL)
	for attempt := 0; ; attempt++ {
		cc := c.currentCreds()
		headers := signer.SignHeaders(cc.AK, cc.SK, cc.Token, method, rawURL, string(body), extra)
		req, err := http.NewRequest(method, rawURL, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		for k, v := range headers {
			req.Header.Set(k, v)
		}
		if timeout > 0 {
			ctx, cancel := context.WithTimeout(context.Background(), timeout)
			defer cancel()
			req = req.WithContext(ctx)
		}
		resp, err := c.HTTP.Do(req)
		if err != nil {
			if attempt < c.Cfg.ConnRetryMax && isConnRetryable(err) {
				delay := time.Duration(c.Cfg.ConnRetryBackoff*float64(attempt+1)) * time.Second
				c.Log.Warn(fmt.Sprintf("[重试] %s %s 连接级失败（%v），%.1fs 后第 %d/%d 次重发",
					method, hostOf(rawURL), err, delay.Seconds(), attempt+1, c.Cfg.ConnRetryMax))
				time.Sleep(delay)
				continue
			}
			return nil, err
		}
		return resp, nil
	}
}

// upstreamURL 测试注入口：BaseOverride 非空时把官方 base（snap-access 与
// opengw 两个域）换成假上游地址；生产路径 BaseOverride 恒为空。
func (c *Client) upstreamURL(rawURL string) string {
	base := c.BaseOverride
	if base == "" {
		base = c.Cfg.UpstreamBase
	}
	if base == "" {
		return rawURL
	}
	rawURL = strings.Replace(rawURL, config.BaseURL, base, 1)
	return strings.Replace(rawURL, config.OpengwBase, base, 1)
}

func hostOf(rawURL string) string {
	if i := strings.Index(rawURL, "//"); i >= 0 {
		if j := strings.Index(rawURL[i+2:], "/"); j >= 0 {
			return rawURL[i+2 : i+2+j]
		}
		return rawURL[i+2:]
	}
	return rawURL
}

// sendHeartbeat session.Sender 实现：走通用签名请求（心跳签名按 URL 现算，
// host 自动进签名头——区域 API 的心跳签名要带 Host）。
func (c *Client) sendHeartbeat(status, sessionID string) bool {
	rawURL := config.BaseURL + session.HeartbeatPath + "?status=" + status
	traceID := make([]byte, 16)
	_, _ = rand.Read(traceID)
	extra := map[string]string{
		"accept":          "application/json",
		"x-language":      "zh-cn",
		"x-client-type":   "kernel",
		"user-session-id": sessionID,
		"x-snap-traceid":  hex.EncodeToString(traceID),
	}
	resp, err := c.SignedRequest("PUT", rawURL, []byte("{}"), extra, 30*time.Second)
	if err != nil {
		c.Log.Warn("[session] heartbeat 异常: " + err.Error())
		return false
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	// 上游 200 且 body 为 {"status":"ok"} 才算登记成功：网关层 200 可以包着
	// 业务失败，把这种当成成功会让槽位从来没被登记过
	if resp.StatusCode != 200 {
		c.Log.Warn(fmt.Sprintf("[session] heartbeat %s 未被接受: HTTP %d", status, resp.StatusCode))
		return false
	}
	var body map[string]any
	if json.Unmarshal(raw, &body) != nil || body["status"] != "ok" {
		return false
	}
	return true
}

// ── 上游发送（对应 _send_upstream）────────────────────────────────────────────

// SendUpstream 签名并发给上游，含连接级/限流/凭证三类重试。成功返回包装过
// body 的响应（正文读完或连接关闭时归还闸门槽位并注销心跳）；最终失败返回
// UpstreamError，调用方直接写客户端即可。
func (c *Client) SendUpstream(req map[string]any, model string, benefitChannel bool, rid string) (*http.Response, *UpstreamError) {
	body := marshalCompact(req)
	// CODEARTS_DEBUG_BODY=1 时把真正发上游的 payload 落盘（排障用，默认关）
	if os.Getenv("CODEARTS_DEBUG_BODY") == "1" {
		dir := os.Getenv("CODEARTS_DEBUG_DIR")
		if dir == "" {
			dir = "."
		}
		_ = os.WriteFile(filepath.Join(dir, "upstream-"+rid+".json"), body, 0o600)
	}

	var slot *session.Slot
	if c.Cfg.SessionGateOn {
		slot = session.Acquire(config.BaseURL, c.AccountID(), c.Gate, c.sendHeartbeat, c.Log, rid)
		if !slot.OK() {
			// 本地闸门满员：409（不是 429——429 会被客户端当额度耗尽进长冷却）
			c.Log.Warn(fmt.Sprintf("[%s] [409] 本地并发闸门已满: %s（在途 %d/%d）",
				rid, slot.Denied, c.Gate.InUse(slot.Key), c.Gate.Limit))
			return nil, &UpstreamError{Status: session.GateStatus, Body: map[string]any{
				"error": map[string]any{
					"message": fmt.Sprintf("上游并发已满（每账号 %d 路）：%s。请稍后重试，或调大 CODEARTS_SESSION_LIMIT",
						c.Gate.Limit, slot.Denied),
					"type": "rate_limit_error",
					"code": "local_concurrency_limit",
				},
			}}
		}
	}

	attempt, connAttempt, authRetry := 0, 0, false
	sessionID := ""
	if slot != nil {
		sessionID = slot.SessionID
	}
	for {
		// 每次重发都重签（x-sdk-date 参与签名；凭证失效时重读也走这里）
		cc := c.currentCreds()
		headers := signer.ChatHeaders(cc.AK, cc.SK, cc.Token, string(body),
			benefitChannel, model, sessionID)
		req2, err := http.NewRequest("POST", c.upstreamURL(config.Target), bytes.NewReader(body))
		if err != nil {
			release(slot)
			return nil, &UpstreamError{Status: 502, Body: map[string]any{
				"error": map[string]any{"message": "upstream error: " + err.Error()}}}
		}
		for k, v := range headers {
			req2.Header.Set(k, v)
		}
		resp, err := c.HTTP.Do(req2)
		if err != nil {
			// TLS 中断/连接重置：一个响应字节都没拿到，换新连接重发
			if connAttempt < c.Cfg.ConnRetryMax && isConnRetryable(err) {
				connAttempt++
				delay := time.Duration(c.Cfg.ConnRetryBackoff*float64(connAttempt)) * time.Second
				c.Log.Warn(fmt.Sprintf("[%s] [重试] 上游连接级失败（%v），%.1fs 后第 %d/%d 次重发",
					rid, err, delay.Seconds(), connAttempt, c.Cfg.ConnRetryMax))
				time.Sleep(delay)
				continue
			}
			c.Log.Error(fmt.Sprintf("[%s] 上游请求异常: %v", rid, err))
			release(slot)
			return nil, &UpstreamError{Status: 502, Body: map[string]any{
				"error": map[string]any{"message": "upstream error: " + err.Error()}}}
		}

		if resp.StatusCode == 200 {
			// 槽位活到响应体读完：正文读完/读失败/连接关闭都还槽
			if slot != nil {
				resp.Body = trackBody(resp.Body, slot)
			}
			return resp, nil
		}

		// 响应尚未开始，此处重试对客户端完全透明（流式同样适用）
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		resp.Body.Close()
		var parsed map[string]any
		_ = json.Unmarshal(raw, &parsed)
		errorCode := fmt.Sprint(parsed["error_code"])
		errorMsg := fmt.Sprint(parsed["error_msg"])
		detail := string(raw)
		if len(detail) > 2000 {
			detail = detail[:2000]
		}

		// 积分耗尽：重试无意义，402 避免客户端自动重试循环
		if config.UpstreamCreditCodes[errorCode] {
			c.Log.Warn(fmt.Sprintf("[%s] [402] 上游积分耗尽: %s %s", rid, errorCode, errorMsg))
			release(slot)
			return nil, &UpstreamError{Status: 402, Body: map[string]any{
				"error": map[string]any{
					"message": orStr(errorMsg, "upstream credits exhausted"),
					"type":    "insufficient_quota",
					"code":    errorCode,
				}}}
		}

		// 并发限流：退避重试
		if config.UpstreamRetryCodes[errorCode] && attempt < c.Cfg.UpstreamRetryMax {
			attempt++
			delay := time.Duration(c.Cfg.UpstreamRetryBackoff*float64(attempt)) * time.Second
			c.Log.Warn(fmt.Sprintf("[%s] [重试] 上游 %s，%.0fs 后第 %d/%d 次重试",
				rid, errorCode, delay.Seconds(), attempt, c.Cfg.UpstreamRetryMax))
			time.Sleep(delay)
			continue
		}

		// 凭证失效：30s 缓存里还是同一把死 STS，强制重读（内部触发 refresh）后重发一次
		authBad := resp.StatusCode == 401 || config.UpstreamAuthCodes[errorCode] ||
			strings.Contains(strings.ToLower(errorMsg), "expired")
		if authBad && !authRetry {
			authRetry = true
			c.Log.Warn(fmt.Sprintf("[%s] [凭证] 上游判凭证失效（HTTP %d %s），强制重读凭证后重试一次",
				rid, resp.StatusCode, orStr(errorCode, errorMsg)))
			c.Creds.Get(true)
			continue
		}
		if authBad {
			c.Log.Error(fmt.Sprintf("[%s] [凭证] 重读凭证后仍被上游拒绝（%s）：需重新登录，或看 credential_provider 的刷新失败原因",
				rid, orStr(errorCode, fmt.Sprint(resp.StatusCode))))
		}

		c.Log.Error(fmt.Sprintf("[%s] [502] 上游返回非 200: status=%d body=%.500s",
			rid, resp.StatusCode, detail))
		release(slot)
		return nil, &UpstreamError{Status: 502, Body: map[string]any{
			"error": map[string]any{
				"message": fmt.Sprintf("upstream %d: %s", resp.StatusCode, detail),
			}}}
	}
}

func release(slot *session.Slot) {
	if slot != nil {
		slot.Release()
	}
}

// trackBody 把还槽动作挂在 body 的读尽/读失败/关闭三条出口上
// （对应 Python session.TrackedResponse；收口在这里，三条路由不用各自记 release）。
func trackBody(rc io.ReadCloser, slot *session.Slot) io.ReadCloser {
	return &trackedBody{rc: rc, slot: slot}
}

type trackedBody struct {
	rc   io.ReadCloser
	slot *session.Slot
	done bool
	mu   sync.Mutex
}

func (t *trackedBody) release() {
	t.mu.Lock()
	if t.done {
		t.mu.Unlock()
		return
	}
	t.done = true
	t.mu.Unlock()
	t.slot.Release()
}

func (t *trackedBody) Read(p []byte) (int, error) {
	n, err := t.rc.Read(p)
	if err != nil {
		t.release() // EOF 与读失败都还槽（漏还 3 个之后整条服务全回 409）
	}
	return n, err
}

func (t *trackedBody) Close() error {
	t.release()
	return t.rc.Close()
}

// ReadBody 便捷读全响应体（tracked body 会在 EOF 时自动还槽）。
func ReadBody(resp *http.Response) ([]byte, error) {
	return io.ReadAll(resp.Body)
}
