// Package benefit 对应 Python lanes/benefit.py + admin/checkin.py：
// 免费额度签到/查询（opengw）、积分活动领取（snap-access /v1/ops/* 四步闭环：
// delivery→claim→confirm→回读）、邀请链接，以及后台每日签到编排。
package benefit

import (
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"codearts2api/internal/cred"
	"codearts2api/internal/signer"
)

// 端点（Python lanes/benefit.py 同名常量）。
const (
	OpengwBase = "https://opengw.developer.huaweicloud.com"
	ClaimURL   = OpengwBase + "/api/v1/benefit/claim"
	BalanceURL = OpengwBase + "/api/v1/user/tokens/balance"
	ConfigURL  = OpengwBase + "/api/v1/gateway/config"
	SnapHost   = "snap-access.cn-north-4.myhuaweicloud.com"
	InvitePath = "/v1/ops/invite/generate" // POST-only：GET 回 method not found 404
	InviteCID  = 3                         // 邀请活动兜底 ID（campaignId 随活动上下线漂）
	AutoGrant  = "AUTO_GRANT"              // 服务端自动发放：不用也不该 claim
	CreditCh   = "IDE"
)

// CREDIT_SETTLED 已结清状态（一次性奖励走到这两个状态就不必再查）。
var creditSettled = map[string]bool{"CONFIRMED": true, "CONSUMED": true, "CLAIMED": true}

// manualEventTypes 要真实事件才推进的活动（展示标记，不是领取门槛）。
var manualEventTypes = map[string]bool{"STUDENT_CERTIFIED": true, "INVITE_USER": true}

// Benefit 签到/积分客户端。OpengwBase/SnapHost 可覆盖（测试指假上游）。
type Benefit struct {
	Creds *cred.Provider
	HTTP  *http.Client
	Log   *slog.Logger

	OpengwBase string
	SnapHost   string
	// BaseOverride 测试/冒烟注入口：非空时官方 base（opengw 与 snap-access
	// 两个域）整体替换；生产不设置。
	BaseOverride string

	// invalidate 回调：签到成功后面板缓存清零（Python 的 _invalidate_credit_caches）
	invalidateMu sync.Mutex

	// 面板短缓存（只有面板这一个消费方）
	creditAt, activityAt, inviteAt time.Time
	credit, activity, invite       any
}

func (b *Benefit) opengw() string {
	if b.OpengwBase != "" {
		return b.OpengwBase
	}
	return OpengwBase
}

func (b *Benefit) snap() string {
	if b.SnapHost != "" {
		return b.SnapHost
	}
	return SnapHost
}

// upstream 冒烟/测试注入口：BaseOverride 非空时把两个官方域换成假上游。
func (b *Benefit) upstream(rawURL string) string {
	if b.BaseOverride == "" {
		return rawURL
	}
	rawURL = strings.Replace(rawURL, OpengwBase, b.BaseOverride, 1)
	return strings.Replace(rawURL, "https://"+SnapHost, b.BaseOverride, 1)
}

func (b *Benefit) client() *http.Client {
	if b.HTTP != nil {
		return b.HTTP
	}
	return &http.Client{Timeout: 30 * time.Second}
}

// SignedRequest 带 SDK-HMAC-SHA256 签名的请求（与 IDE 一致：host/content-type/
// x-sdk-date/x-security-token）。extra 头名会转小写参与签名。
func (b *Benefit) SignedRequest(method, rawURL string, body []byte, extra map[string]string) (*http.Response, error) {
	c := b.Creds.Get(false)
	if c == nil {
		c = cred.Empty()
	}
	method = strings.ToUpper(method)
	rawURL = b.upstream(rawURL)
	headers := signer.SignHeaders(c.AK, c.SK, c.Token, method, rawURL, string(body), extra)
	req, err := http.NewRequest(method, rawURL, strings.NewReader(string(body)))
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	return b.client().Do(req)
}

// unwrap 校验 opengw 响应（error_code=0000 成功），返回 result 字段。
func (b *Benefit) unwrap(resp *http.Response) (map[string]any, error) {
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("HTTP %d: %.300s", resp.StatusCode, raw)
	}
	var data map[string]any
	if json.Unmarshal(raw, &data) != nil {
		return nil, fmt.Errorf("响应不是 JSON: %.300s", raw)
	}
	switch code := data["error_code"].(type) {
	case nil, string:
		if code != "" && code != "0000" {
			return nil, fmt.Errorf("业务错误 %v: %v", code, data["error_msg"])
		}
	case float64:
		if code != 0 {
			return nil, fmt.Errorf("业务错误 %v: %v", code, data["error_msg"])
		}
	}
	result, _ := data["result"].(map[string]any)
	if result == nil {
		result = map[string]any{}
	}
	return result, nil
}

// ClaimStatus 签到状态（create_time 即最近一次签到时间，毫秒时间戳）。
func (b *Benefit) ClaimStatus() (map[string]any, error) {
	resp, err := b.SignedRequest("GET", b.opengw()+"/api/v1/benefit/claim", nil, nil)
	if err != nil {
		return nil, err
	}
	return b.unwrap(resp)
}

// Claim 签到领取当日免费额度。
func (b *Benefit) Claim() (map[string]any, error) {
	resp, err := b.SignedRequest("POST", b.opengw()+"/api/v1/benefit/claim", nil, nil)
	if err != nil {
		return nil, err
	}
	return b.unwrap(resp)
}

// Balance 查询免费额度余额。
func (b *Benefit) Balance() (map[string]any, error) {
	resp, err := b.SignedRequest("GET", b.opengw()+"/api/v1/user/tokens/balance", nil, nil)
	if err != nil {
		return nil, err
	}
	return b.unwrap(resp)
}

// Models 查询免费模型列表（opengw gateway/config）。
func (b *Benefit) Models() ([]any, error) {
	resp, err := b.SignedRequest("GET", b.opengw()+"/api/v1/gateway/config", nil, nil)
	if err != nil {
		return nil, err
	}
	result, err := b.unwrap(resp)
	if err != nil {
		return nil, err
	}
	models, _ := result["models"].([]any)
	return models, nil
}

// SnapRequest 调积分福利网关（snap-access），AK/SK 签名即可。
// 自定义头 agent-type 必须全小写，否则 APIG 401 签名失败。
// 返回 (HTTP 状态码, JSON)。
func (b *Benefit) SnapRequest(method, path, body, query string) (int, map[string]any) {
	raw := "https://" + b.snap() + path
	if query != "" {
		raw += "?" + query
	}
	resp, err := b.SignedRequest(method, raw, []byte(body),
		map[string]string{"agent-type": "PromptCenter", "x-language": "zh-cn"})
	if err != nil {
		return 0, nil
	}
	defer resp.Body.Close()
	rawBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var data map[string]any
	if json.Unmarshal(rawBody, &data) != nil {
		return resp.StatusCode, nil
	}
	return resp.StatusCode, data
}

// CreditActivities 查询福利活动列表（/v1/ops/delivery?channel=IDE 的 items）。
func (b *Benefit) CreditActivities() ([]map[string]any, error) {
	code, data := b.SnapRequest("GET", "/v1/ops/delivery", "", "channel=IDE")
	if code != 200 {
		return nil, fmt.Errorf("查询福利活动失败 HTTP %d: %v", code, data)
	}
	dd, _ := data["data"].(map[string]any)
	items, _ := dd["items"].([]any)
	out := make([]map[string]any, 0, len(items))
	for _, it := range items {
		if m := asMap(it); m != nil {
			out = append(out, m)
		}
	}
	return out, nil
}

// ActivityView 上游活动条目 → 展示结构（对齐 activity_view）。
// 状态只描述接口说出口的事，不推断原因：claimable=false + status=null 至少有
// 三种情况（没参与/审核中/被拒），上游没有区分字段，只能写「未可领」。
func (b *Benefit) ActivityView(item map[string]any) map[string]any {
	state := strOr(item["status"], "")
	var todo string
	switch {
	case truthy(item["claimable"]):
		todo = "待领取"
	case creditSettled[state]:
		todo = "已结清"
	case state != "":
		todo = state
	default:
		todo = "未可领"
	}
	typ := strOr(item["type"], "")
	return map[string]any{
		"id":           item["campaignId"],
		"type":         typ,
		"title":        orStr(item["title"], typ),
		"amount":       item["benefitAmount"],
		"manual":       manualEventTypes[typ],
		"claimable":    truthy(item["claimable"]),
		"status":       state,
		"state":        todo,
		"trigger_mode": strOr(asMap(item["extra"])["triggerMode"], ""),
	}
}

// CreditActivityViews 活动展示结构，按 campaignId 排序保证顺序稳定。
func (b *Benefit) CreditActivityViews() ([]map[string]any, error) {
	items, err := b.CreditActivities()
	if err != nil {
		return nil, err
	}
	views := make([]map[string]any, 0, len(items))
	for _, it := range items {
		if v := b.ActivityView(it); v != nil {
			views = append(views, v)
		}
	}
	sort.Slice(views, func(i, j int) bool {
		return numLess(views[i]["id"], views[j]["id"])
	})
	return views, nil
}

// CreditBalance 积分余额（usageTotalPackageCredit：总额/已用/剩余）。
func (b *Benefit) CreditBalance() (map[string]any, error) {
	code, data := b.SnapRequest("GET", "/snap-manager/v1/statistics/plugin", "", "")
	if code != 200 {
		return nil, fmt.Errorf("查询积分余额失败 HTTP %d: %v", code, data)
	}
	metrics, _ := data["metrics"].([]any)
	for _, m := range metrics {
		mm := asMap(m)
		if mm != nil && strOr(mm["name"], "") == "usageTotalPackageCredit" {
			return map[string]any{
				"total":  mm["package_credit_amount"],
				"used":   mm["package_credit_used"],
				"remain": mm["package_credit_remain"],
			}, nil
		}
	}
	return nil, nil
}

// claimableCampaigns 挑「能领」或「领了没确认」的活动，按 campaignId 排序。
// 门槛只有一个：上游说 claimable（status=CLAIMED 是上次断在 claim/confirm
// 之间的补救信号）。AUTO_GRANT 是服务端自己发的，claim 只会白费请求。
func (b *Benefit) claimableCampaigns(items []map[string]any) []map[string]any {
	out := make([]map[string]any, 0, len(items))
	for _, item := range items {
		unit := strOr(item["benefitUnit"], "")
		if unit != "" && unit != "CREDIT" {
			continue // benefitUnit 空表示上游没给单位，别因此漏掉活动
		}
		if item["campaignId"] == nil {
			continue
		}
		if strOr(asMap(item["extra"])["triggerMode"], "") == AutoGrant {
			continue
		}
		if truthy(item["claimable"]) || strOr(item["status"], "") == "CLAIMED" {
			out = append(out, item)
		}
	}
	sort.Slice(out, func(i, j int) bool { return numLess(out[i]["campaignId"], out[j]["campaignId"]) })
	return out
}

func (b *Benefit) campaignLabel(item map[string]any) string {
	return fmt.Sprintf("%s(#%v)", orStr(item["title"], strOr(item["type"], "")), item["campaignId"])
}

// DoCreditClaim 领取可自动领取的积分活动：claim → confirm → 回读验证。
// claim 返回 200 只说明上游「受理了」，不代表积分到账；只认
// CONFIRMED/CONSUMED 算成功。claim 响应 campaignId 必须与请求一致才进
// confirm；status=CLAIMED 但 claimable=false 时跳过 claim 只补 confirm。
func (b *Benefit) DoCreditClaim() (string, error) {
	items, err := b.CreditActivities()
	if err != nil {
		return "", err
	}
	todo := b.claimableCampaigns(items)
	if len(todo) == 0 {
		// 无可领 ≠ 无事可报：AUTO_GRANT 的奖励服务端自己发，同样到账了
		var settled []string
		for _, i := range items {
			if creditSettled[strOr(i["status"], "")] {
				settled = append(settled, b.campaignLabel(i))
			}
		}
		if len(settled) == 0 {
			return "当前没有可领取的积分活动", nil
		}
		return "今日福利已确认到账（" + strings.Join(settled, "、") + "）", nil
	}

	var done, failed []string
	for _, item := range todo {
		cid := fmt.Sprint(item["campaignId"])
		label := b.campaignLabel(item)
		needClaim := truthy(item["claimable"])

		if needClaim {
			key := fmt.Sprintf("claim_%s_%d", cid, time.Now().UnixMilli())
			code, data := b.SnapRequest("POST", "/v1/ops/claim", marshal(map[string]any{
				"campaignId":    item["campaignId"],
				"idempotentKey": key,
				"channel":       CreditCh,
			}), "")
			if code != 200 {
				failed = append(failed, fmt.Sprintf("%s 领取失败 HTTP %d %.120v", label, code, data))
				continue
			}
			biz := numVal(asMap(data)["code"])
			if biz != 0 && biz != 40001 {
				failed = append(failed, fmt.Sprintf("%s 领取失败 code=%v %.120v", label, biz, data))
				continue
			}
			// claim 的成功信封是 {"code":0,...,"data":{...}}——活动记录在 data 里
			payload := asMap(asMap(data)["data"])
			if returned, ok := payload["campaignId"]; ok && returned != nil &&
				fmt.Sprint(returned) != cid {
				failed = append(failed, fmt.Sprintf("%s 领取响应活动 ID 不匹配（要 %s 回 %v），未确认", label, cid, returned))
				continue
			}
			if biz == 40001 {
				// 尚未到达权益刷新时间（今天已领）；confirm 仍要发（幂等补救）
			} else if payload["id"] == nil {
				// code=0 但 id 为空：受理了却没产生待确认记录
				failed = append(failed, fmt.Sprintf("%s 领取未产生记录（响应 id 为空），未确认", label))
				continue
			}
		}

		ccode, cdata := b.SnapRequest("POST", "/v1/ops/confirm", marshal(map[string]any{
			"campaignId": item["campaignId"],
		}), "")
		if ccode != 200 || numVal(cdata["code"]) != 0 {
			failed = append(failed, fmt.Sprintf("%s confirm 失败 HTTP %d %.120v", label, ccode, cdata))
			continue
		}

		// 回读验证：状态没变成 CONFIRMED/CONSUMED 就不算成功
		state := ""
		if after, err := b.CreditActivities(); err == nil {
			for _, a := range after {
				if fmt.Sprint(a["campaignId"]) == cid {
					state = strOr(a["status"], "")
					break
				}
			}
		}
		if creditSettled[state] {
			done = append(done, fmt.Sprintf("%s %v 积分", label, orStr(item["benefitAmount"], "?")))
		} else {
			failed = append(failed, fmt.Sprintf("%s confirm 后状态仍为 %s，未确认到账", label, orStr(state, "空")))
		}
	}

	var parts []string
	if len(done) > 0 {
		parts = append(parts, "已领取 "+strings.Join(done, "、"))
	}
	if len(failed) > 0 {
		parts = append(parts, "失败 "+strings.Join(failed, "；"))
	}
	if len(parts) == 0 {
		return "无可领取的积分活动", nil
	}
	return strings.Join(parts, "；"), nil
}

// GenerateInviteCode 生成（或取回）邀请码与链接。接口是幂等的「生成或取回」，
// 同一账号同一活动反复调用返回同一条。这里只拿链接，绝不顺带 claim 邀请活动
// （奖励必须由真实好友通过链接完成注册才产生）。
func (b *Benefit) GenerateInviteCode() (map[string]any, error) {
	campaignID, items := b.inviteCampaignID()
	code, data := b.SnapRequest("POST", InvitePath, marshal(map[string]any{
		"campaignId": campaignID,
	}), "")
	if code != 200 {
		return nil, fmt.Errorf("生成邀请链接失败 HTTP %d: %v", code, data)
	}
	if c := numVal(data["code"]); data != nil && c != 0 {
		return nil, fmt.Errorf("生成邀请链接失败 %v: %v", c, orStr(data["message"], fmt.Sprint(data)))
	}
	payload := asMap(data["data"])
	if payload == nil {
		return nil, fmt.Errorf("邀请链接响应缺少 data: %v", data)
	}
	_ = items
	return payload, nil
}

// inviteCampaignID 从活动列表找 INVITE_USER 的最小 campaignId，找不到退回常量。
func (b *Benefit) inviteCampaignID() (int, []map[string]any) {
	items, err := b.CreditActivities()
	if err != nil {
		return InviteCID, nil
	}
	ids := []int{}
	for _, it := range items {
		if strOr(it["type"], "") == "INVITE_USER" && it["campaignId"] != nil {
			if n := int(numVal(it["campaignId"])); n > 0 {
				ids = append(ids, n)
			}
		}
	}
	if len(ids) == 0 {
		return InviteCID, items
	}
	sort.Ints(ids)
	return ids[0], items
}

// ── 面板短缓存（Python admin/panel.py 的三缓存）─────────────────────────────

// CreditBalanceCached 积分余额短缓存（60s）。
func (b *Benefit) CreditBalanceCached() map[string]any {
	b.invalidateMu.Lock()
	defer b.invalidateMu.Unlock()
	if b.credit == nil || time.Since(b.creditAt) > 60*time.Second {
		if info, err := b.CreditBalance(); err == nil {
			b.credit = info
		} else {
			b.warnLog("积分余额查询失败: %v", err)
			if b.credit == nil {
				b.credit = map[string]any{}
			}
		}
		b.creditAt = time.Now()
	}
	m, _ := b.credit.(map[string]any)
	return m
}

// CreditActivitiesCached 活动列表短缓存（60s）。
func (b *Benefit) CreditActivitiesCached() []map[string]any {
	b.invalidateMu.Lock()
	defer b.invalidateMu.Unlock()
	if b.activity == nil || time.Since(b.activityAt) > 60*time.Second {
		if views, err := b.CreditActivityViews(); err == nil {
			b.activity = views
		} else {
			b.warnLog("积分活动查询失败: %v", err)
			if b.activity == nil {
				b.activity = []map[string]any{}
			}
		}
		b.activityAt = time.Now()
	}
	m, _ := b.activity.([]map[string]any)
	return m
}

// InviteLinkCached 邀请链接短缓存（1h）；缓存里也存错误信息，
// 避免上游抽风时面板每次刷新都干等一个 30s 超时。
func (b *Benefit) InviteLinkCached() map[string]any {
	b.invalidateMu.Lock()
	defer b.invalidateMu.Unlock()
	if b.invite == nil || time.Since(b.inviteAt) > time.Hour {
		info, err := b.GenerateInviteCode()
		if err != nil {
			b.warnLog("邀请链接查询失败: %v", err)
			info = map[string]any{"error": err.Error()}
		}
		b.invite = info
		b.inviteAt = time.Now()
	}
	m, _ := b.invite.(map[string]any)
	return m
}

// InvalidateCreditCaches 签到成功后清零时间戳，面板立刻看到「待领取→已结清」。
func (b *Benefit) InvalidateCreditCaches() {
	b.invalidateMu.Lock()
	b.creditAt = time.Time{}
	b.activityAt = time.Time{}
	b.invalidateMu.Unlock()
}

func (b *Benefit) warnLog(format string, args ...any) {
	if b.Log != nil {
		b.Log.Warn(fmt.Sprintf(format, args...))
	}
}

// CheckinOnce 执行一轮签到编排，返回汇报文本与是否已有确定结论。
// 两条通道互不依赖，必须分开：福利 Token 通道（opengw）2026-10 起对部分账号
// 稳定回 4000 invalid channel（渠道下线），异常跳过整段积分领取就漏掉唯一
// 还有收益的通道。只有「领到 / 今日已领 / 渠道未开通」才算有结论；通道不可达
// 不算，否则一次上游抽风会把当天永久标记为已签。
func (b *Benefit) CheckinOnce() (text string, done bool) {
	tokenText, tokenDone := b.claimTokenChannel()
	creditText, creditDone := b.claimCreditChannel()
	return tokenText + creditText, tokenDone || creditDone
}

func (b *Benefit) claimTokenChannel() (string, bool) {
	before := time.Now()
	status, err := b.ClaimStatus()
	if err == nil {
		// 上游记录说今天签过就够了，不再多打 balance（同一渠道多半一起挂）
		if lastMs := numVal(status["create_time"]); lastMs > 0 &&
			time.UnixMilli(int64(lastMs)).Local().Format("2006-01-02") == time.Now().Format("2006-01-02") {
			return "", true
		}
	}
	claimResult, err := b.Claim()
	if err != nil {
		msg := err.Error()
		if strings.Contains(msg, "invalid channel") || strings.Contains(msg, "group not found") {
			return " 福利 Token 通道未开通，已跳过", true
		}
		// 签名/网络类错误：确实没结论，留到下一小时重试
		return fmt.Sprintf(" 福利 Token 通道失败（%s）", msg), false
	}
	text := ""
	if info, err := b.Balance(); err == nil {
		text = fmt.Sprintf(" Token %s/%s", comma(numVal(info["total_balance"])), comma(numVal(info["total_quota"])))
	}
	claimMs := numVal(claimResult["create_time"])
	if claimMs > 0 && time.UnixMilli(int64(claimMs)).Before(before.Add(-10*time.Second)) {
		return text + "（福利今日已签）", true
	}
	return text + "（福利领取成功）", true
}

func (b *Benefit) claimCreditChannel() (string, bool) {
	status, err := b.DoCreditClaim()
	if err != nil {
		return fmt.Sprintf("，积分领取失败: %v", err), false
	}
	b.InvalidateCreditCaches()
	creditText := ""
	if credits, err := b.CreditBalance(); err == nil && credits != nil {
		creditText = fmt.Sprintf("，积分 %s/%s", comma(numVal(credits["remain"])), comma(numVal(credits["total"])))
	}
	return fmt.Sprintf("%s（%s）", creditText, status), true
}

// RunCheckinLoop 后台每日自动签到：启动即签一次，之后每小时检查跨天。
// _last_checkin_date 只在领取成功后标记；查不到状态、上游挂了都不标记，
// 下一小时自动重试。
func (b *Benefit) RunCheckinLoop(log *slog.Logger) {
	lastDate := ""
	for {
		today := time.Now().Format("2006-01-02")
		if lastDate != today {
			text, done := b.CheckinOnce()
			if done {
				lastDate = today
			}
			if text != "" {
				fmt.Printf("[签到] %s%s\n", today, text)
			} else {
				fmt.Printf("[签到] %s 两条通道均未取得结果，1 小时后重试\n", today)
			}
		}
		time.Sleep(time.Hour)
	}
}

// ── 小工具 ───────────────────────────────────────────────────────────────────

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func strOr(v any, def string) string {
	if s, ok := v.(string); ok && s != "" {
		return s
	}
	return def
}

func orStr(v any, def string) string {
	if s, ok := v.(string); ok {
		return s
	}
	if v != nil {
		return fmt.Sprint(v)
	}
	return def
}

func truthy(v any) bool {
	switch t := v.(type) {
	case bool:
		return t
	case string:
		return t == "true" || t == "True"
	}
	return false
}

// numVal 宽松取数：int/float64/json.Number/string 都试。
func numVal(v any) float64 {
	switch t := v.(type) {
	case float64:
		return t
	case int:
		return float64(t)
	case int64:
		return float64(t)
	case string:
		var f float64
		if _, err := fmt.Sscanf(t, "%g", &f); err == nil {
			return f
		}
	}
	return 0
}

func numLess(a, b any) bool { return numVal(a) < numVal(b) }

func comma(n float64) string {
	s := fmt.Sprintf("%.0f", n)
	// 千分位
	var out []byte
	for i, c := range []byte(s) {
		if i > 0 && (len(s)-i)%3 == 0 {
			out = append(out, ',')
		}
		out = append(out, c)
	}
	return string(out)
}

func marshal(v any) string {
	raw, _ := json.Marshal(v)
	return string(raw)
}
